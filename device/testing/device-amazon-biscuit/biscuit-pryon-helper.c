/* Runs Amazon's wake word engine and, when asked, its sound detector, for a device whose owner imported
 * those files from their own Fire OS.  This program holds nothing of Amazon's: it is built against stubs
 * (biscuit-pryon-stubs.c) and only finds the real libpryon.so and libc.so when it starts, through Amazon's
 * own linker, which is what PT_INTERP names.
 *
 * It is 32-bit and bionic because the engine is.  It has one job so the engine stays out of the voice
 * assistant's process: biscuit LVA starts it with no network and no privileges and talks over two pipes.
 *
 *   arguments  <wake manifest> [<sound manifest> or -] [<whisper manifest> or -]
 *   stdin    u16le n, n > 0: then n signed 16-bit samples at 16 kHz, mono
 *            u16le 0, u8 id, u8 value: id 0 AlarmState, 1 AudioPlayerState, 2 audio_playback, 3 sound detection,
 *                                      4 whisper: a request begins, 5 whisper: it ended, value 1 to score it
 *   stdout   R                          ready
 *            K <keyword> <type> <start> <end>    keyword result; type 2 accepted, 0 near miss
 *            A <type>[,<type>...]       sounds heard in a window
 *            S on|off|failed            the sound detector changed
 *            W ready|failed             the whisper model loaded, on the first request
 *            W <0|1> <confidence> <threshold>    a request was scored; 1 is whispered
 *   The end of stdin ends it.
 *
 * What the engine expects (types, field offsets, property names) comes from echo-dot-assist's reverse
 * engineering, https://github.com/Gamer92000/echo-dot-assist, MIT licensed: docs/re-pryon.md, docs/re-aed.md.
 */
#include <stddef.h>
#include <stdint.h>

long read(int, void *, unsigned long);
long write(int, const void *, unsigned long);
int snprintf(char *, unsigned long, const char *, ...);
char *strstr(const char *, const char *);
unsigned long strlen(const char *);
void exit(int);
int strcmp(const char *, const char *);
int atoi(const char *);

typedef struct {
    int32_t encoding, sampleRate, bitsPerSample, numChannels, channelTypes[32];
} Format;

typedef struct {
    uint8_t a[8];
    uint64_t begin;             /* +0x08 */
    uint8_t b[12];
    const char *keyword;        /* +0x1c */
    uint8_t c[4];
    int32_t type;               /* +0x24 */
    uint32_t metadata_size;
    const void *metadata;
    uint8_t d[16];
    uint64_t end;               /* +0x40 */
} KeywordResult;

typedef struct {
    uint8_t a[8];
    uint64_t sample;            /* +0x08 */
    int32_t type;               /* +0x10; 2 is a detection */
    const char *json;           /* +0x14 */
} SoundResult;

typedef struct {
    const char *name;
    uint32_t pad;
    int64_t value;
} Property;

typedef struct {
    uint32_t one;
    const Property *property;
} ClientEvent;

int PryonApi_SetLoggingCallback(void (*)(int, const char *, const char *));
int PryonApi_SetEnumeratedResultCallback(void (*)(const char *, KeywordResult *));
__attribute__((weak)) int PryonApi_SetAcousticEventDetectionResultCallback(void (*)(const char *, SoundResult *));
int PryonModelSet_New(const char *, const char *, const char *);
int PryonModelSet_Delete(const char *);
void PryonDecoder_NewPryonMultichannelAudioFormat_Default(Format *);
int PryonDecoder_NewSpotterAudioDecoder(const char *, const char *, const char *, Format, const char *);
int PryonDecoder_Delete(const char *);
int PryonDecoder_PushAudioEventSamples(const char *, uint64_t, const int16_t *, uint32_t);
int PryonDecoder_PushClientEvents(const char *, const ClientEvent *, uint32_t);

/* Weak: a library from another Fire OS build may lack these, and that must cost only what they do. */
typedef struct {
    int32_t encoding, sampleRate, bitsPerSample, numChannels;
} WhisperFormat;

__attribute__((weak)) int WhisperApi_setLogEventHandler(void (*)(int, const char *, const char *));
__attribute__((weak)) int WhisperApi_setEventHandler(void (*)(const char *, const char *, const char *, const char *,
                                                              uint32_t, uint32_t));
__attribute__((weak)) int WhisperApi_loadWhisperModelset(const char *, const char *);
__attribute__((weak)) int WhisperApi_createWhisperDetector(const char *, const char *, const char *, WhisperFormat);
__attribute__((weak)) int WhisperApi_pushAudioEvent(const char *, const int16_t *, uint32_t, uint64_t);
__attribute__((weak)) int WhisperApi_pushEndOfUtterance(const char *, const char *, uint64_t, uint64_t);
__attribute__((weak)) int WhisperApi_backlogWait(const char *, int32_t);
__attribute__((weak)) int WhisperApi_pushSessionEnd(const char *);
__attribute__((weak)) int WhisperApi_deleteWhisperDetector(const char *);

#define WAKE "biscuit-wake"
#define SOUND "biscuit-sound"
#define MAX_SAMPLES 4096

/* humanPresence is left out on purpose: it fires on any talk, every window while someone is about. */
static const char *const sound_types[] = {
    "smokeAlarm", "smokeSiren", "carbonMonoxideSiren", "glassBreak", "dogBark", "babyCry",
    "snore", "cough", "waterSounds", "beepingAppliance",
};
#define SOUND_TYPES (int)(sizeof sound_types / sizeof sound_types[0])

static const char *sound_manifest;
static int sound_open;

static void say(const char *text, int n)
{
    /* One write per line: results arrive on the engine's threads, and a pipe keeps this whole. */
    if (write(1, text, (unsigned long)n) != n)
        exit(0);
}

static void on_log(int level, const char *tag, const char *msg)
{
    char line[300];
    int n;
    if (level > 1)
        return;
    n = snprintf(line, sizeof line, "pryon %s: %s\n", tag ? tag : "", msg ? msg : "");
    if (n > 0)
        write(2, line, (unsigned long)(n < (int)sizeof line ? n : (int)sizeof line - 1));
}

static void on_keyword(const char *decoder, KeywordResult *r)
{
    char line[120];
    int n;
    (void)decoder;
    n = snprintf(line, sizeof line, "K %s %d %llu %llu\n", r->keyword ? r->keyword : "?", r->type,
                 (unsigned long long)r->begin, (unsigned long long)r->end);
    if (n > 0 && n < (int)sizeof line)
        say(line, n);
}

/* "detected":true inside the type's own object; an object ends where the next type's begins. */
static int detected(const char *json, const char *type)
{
    char pattern[48];
    const char *from, *end, *hit;
    snprintf(pattern, sizeof pattern, "\"%s\":{", type);
    from = strstr(json, pattern);
    if (!from)
        return 0;
    from += strlen(pattern);
    end = strstr(from, ":{\"aed_type\"");
    hit = strstr(from, "\"detected\":true");
    return hit && (!end || hit < end);
}

static void on_sound(const char *decoder, SoundResult *r)
{
    char line[256];
    int i, n = 0, any = 0;
    (void)decoder;
    if (r->type != 2 || !r->json)
        return;
    n = snprintf(line, sizeof line, "A ");
    for (i = 0; i < SOUND_TYPES; i++) {
        if (detected(r->json, sound_types[i])) {
            n += snprintf(line + n, sizeof line - (unsigned long)n, "%s%s", any ? "," : "", sound_types[i]);
            any = 1;
        }
    }
    if (any) {
        n += snprintf(line + n, sizeof line - (unsigned long)n, "\n");
        say(line, n);
    }
}

static int new_decoder(const char *decoder, const char *set, const char *manifest)
{
    Format format;
    if (PryonModelSet_New(set, manifest, ""))
        return -1;
    PryonDecoder_NewPryonMultichannelAudioFormat_Default(&format);
    if (PryonDecoder_NewSpotterAudioDecoder(decoder, set, "pryon", format, "{}")) {
        PryonModelSet_Delete(set);
        return -1;
    }
    return 0;
}

static int set_property(const char *decoder, const char *name, int value)
{
    Property property;
    ClientEvent event;
    property.name = name;
    property.pad = 0;
    property.value = value;
    event.one = 1;
    event.property = &property;
    return PryonDecoder_PushClientEvents(decoder, &event, 1);
}

static uint64_t sound_at;

static void sound_on(void)
{
    static char names[SOUND_TYPES][48];
    int i;
    if (sound_open || !sound_manifest)
        return;
    if (new_decoder(SOUND, SOUND "-models", sound_manifest)) {
        say("S failed\n", 9);
        return;
    }
    set_property(SOUND, "AcousticEventDetectionEnabled", 1);
    for (i = 0; i < SOUND_TYPES; i++) {
        snprintf(names[i], sizeof names[i], "aed_%s_enabled", sound_types[i]);
        set_property(SOUND, names[i], 1);
    }
    sound_open = 1;
    sound_at = 0;
    say("S on\n", 5);
}

static void sound_off(void)
{
    if (!sound_open)
        return;
    sound_open = 0;
    PryonDecoder_Delete(SOUND);
    PryonModelSet_Delete(SOUND "-models");
    say("S off\n", 6);
}

/* As stock calls it: a detector per request, audio with its running index, the end of the utterance in 10 ms
 * frames, one "result" event after it.  From echo-dot-assist, docs/re-whisper.md and src/hassmic/whisper_pryon.c,
 * which also found that the stream starts before the wake word has died away, and that its tail spoils the score. */
#define WHISPER_SET "biscuit-whisper-models"
#define WHISPER "biscuit-whisper"
#define WHISPER_SKIP 8000           /* samples: 0.5 s after the wake word */

static const char *whisper_manifest;
static int whisper_opened, whisper_active, whisper_pending;
static uint64_t whisper_fed, whisper_skipped;

static int number_after(const char *json, const char *key)
{
    const char *at = strstr(json, key);
    return at ? atoi(at + strlen(key)) : -1;
}

static void on_whisper(const char *detector, const char *utterance, const char *type, const char *json,
                       uint32_t zero0, uint32_t zero1)
{
    char line[64];
    int n, confidence, threshold;
    (void)detector; (void)utterance; (void)zero0; (void)zero1;
    if (!type || strcmp(type, "result") || !json)
        return;
    confidence = number_after(json, "\"confidence\":");
    threshold = number_after(json, "\"threshold\":");
    if (confidence < 0)
        return;
    if (threshold <= 0)
        threshold = 500;            /* stock's own cut-off */
    n = snprintf(line, sizeof line, "W %d %d %d\n", confidence > threshold, confidence, threshold);
    if (n > 0 && n < (int)sizeof line)
        say(line, n);
}

static int whisper_open(void)
{
    if (whisper_opened)
        return whisper_opened > 0;
    whisper_opened = -1;
    if (!whisper_manifest || !WhisperApi_loadWhisperModelset || !WhisperApi_createWhisperDetector)
        return 0;
    WhisperApi_setLogEventHandler(on_log);
    if (WhisperApi_setEventHandler(on_whisper) || WhisperApi_loadWhisperModelset(WHISPER_SET, whisper_manifest)) {
        say("W failed\n", 9);
        return 0;
    }
    whisper_opened = 1;
    say("W ready\n", 8);
    return 1;
}

/* The request before's detector goes now: deleting it right after its end of utterance could lose the result. */
static void whisper_retire(void)
{
    if (!whisper_pending)
        return;
    WhisperApi_backlogWait(WHISPER, -1);
    WhisperApi_pushSessionEnd(WHISPER);
    WhisperApi_deleteWhisperDetector(WHISPER);
    whisper_pending = 0;
}

static void whisper_begin(void)
{
    WhisperFormat format = { 0, 16000, 16, 1 };
    if (!whisper_open())
        return;
    whisper_retire();
    if (WhisperApi_createWhisperDetector(WHISPER, WHISPER_SET, "pryon", format)) {
        write(2, "whisper: no detector\n", 21);
        return;
    }
    whisper_active = whisper_pending = 1;
    whisper_fed = whisper_skipped = 0;
}

static void whisper_feed(const int16_t *samples, unsigned count)
{
    if (!whisper_active)
        return;
    if (whisper_skipped < WHISPER_SKIP) {
        unsigned skip = WHISPER_SKIP - whisper_skipped < count ? (unsigned)(WHISPER_SKIP - whisper_skipped) : count;
        whisper_skipped += skip;
        samples += skip;
        count -= skip;
        if (!count)
            return;
    }
    if (WhisperApi_pushAudioEvent(WHISPER, samples, count, whisper_fed)) {
        whisper_active = 0;
        return;
    }
    whisper_fed += count;
}

static void whisper_end(int score)
{
    if (!whisper_active)
        return;
    whisper_active = 0;
    if (score && whisper_fed >= 160)
        WhisperApi_pushEndOfUtterance(WHISPER, "Utterance-1", 0, whisper_fed / 160);
    else
        whisper_retire();
}

static int read_all(void *to, unsigned long want)
{
    char *at = to;
    while (want) {
        long n = read(0, at, want);
        if (n <= 0)
            return -1;
        at += n;
        want -= (unsigned long)n;
    }
    return 0;
}

int main(int argc, char **argv)
{
    static int16_t samples[MAX_SAMPLES];
    uint64_t wake_at = 0;
    if (argc < 2) {
        write(2, "usage: biscuit-pryon-helper <wake> [<sound> or -] [<whisper> or -]\n", 67);
        return 64;
    }
    if (argc > 2 && strcmp(argv[2], "-"))
        sound_manifest = argv[2];
    if (argc > 3 && strcmp(argv[3], "-"))
        whisper_manifest = argv[3];
    PryonApi_SetLoggingCallback(on_log);
    PryonApi_SetEnumeratedResultCallback(on_keyword);
    if (PryonApi_SetAcousticEventDetectionResultCallback)
        PryonApi_SetAcousticEventDetectionResultCallback(on_sound);
    if (new_decoder(WAKE, WAKE "-models", argv[1])) {
        write(2, "the wake word model did not load\n", 33);
        return 2;
    }
    say("R\n", 2);
    for (;;) {
        uint8_t head[2];
        unsigned count;
        if (read_all(head, 2))
            break;
        count = (unsigned)head[0] | (unsigned)head[1] << 8;
        if (!count) {
            uint8_t cmd[2];
            if (read_all(cmd, 2))
                break;
            switch (cmd[0]) {
            case 0: set_property(WAKE, "AlarmState", cmd[1]); break;
            case 1: set_property(WAKE, "AudioPlayerState", cmd[1]); break;
            case 2: set_property(WAKE, "audio_playback", cmd[1]); break;
            case 3: if (cmd[1]) sound_on(); else sound_off(); break;
            case 4: whisper_begin(); break;
            case 5: whisper_end(cmd[1]); break;
            default: break;
            }
            continue;
        }
        if (count > MAX_SAMPLES) {
            write(2, "frame too long\n", 15);
            return 3;
        }
        if (read_all(samples, count * 2UL))
            break;
        PryonDecoder_PushAudioEventSamples(WAKE, wake_at, samples, count);
        wake_at += count;
        if (sound_open) {
            PryonDecoder_PushAudioEventSamples(SOUND, sound_at, samples, count);
            sound_at += count;
        }
        whisper_feed(samples, count);
    }
    whisper_end(0);
    whisper_retire();
    sound_off();
    PryonDecoder_Delete(WAKE);
    PryonModelSet_Delete(WAKE "-models");
    return 0;
}

/* bionic's start-up: there is no crt here, because the only libc this ever runs against is Amazon's. */
extern void __libc_init(void *, void (*)(void), int (*)(int, char **, char **), const void *);
static const void *const structors[3];

__attribute__((used, visibility("hidden"))) void start_main(void *raw)
{
    __libc_init(raw, NULL, (int (*)(int, char **, char **))main, structors);
}

__asm__(".text\n"
        ".globl _start\n"
        ".type _start, %function\n"
        "_start:\n"
        "    mov r0, sp\n"
        "    b start_main\n");
