"""Amazon's own wake word engine and sound detector, for an owner who imported them. BISCUIT.

Nothing of Amazon's ships with this project. The owner imports the engine from their own Fire OS backup
(the settings page's Stock files), and without that import this module does nothing at all and "Alexa
(Amazon engine)" is not offered as a wake word.

The engine is a 32-bit Android library, so it cannot live in this process. biscuit-pryon-helper runs it
through Amazon's own linker, with no network and as nobody, and two pipes are all it can reach:

    this process   -- audio, hints --> helper
                   <-- results     --

Nothing here may hold up the audio thread. feed() and hint() only queue, a full queue loses audio rather
than delaying the microphone, and the engine dying costs the wake word, never the voice assistant: it is
restarted with a growing delay while the rest of the device carries on.

The layout of the protocol is described in biscuit-pryon-helper.c.
"""

from __future__ import annotations

import array
import collections
import ctypes
import json
import logging
import math
import os
import queue
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

_LOGGER = logging.getLogger(__name__)

HELPER = "/usr/lib/biscuit-pryon/biscuit-pryon-helper"
STORE = "/var/lib/biscuit-amazon/amazonwake"
WAKE_MANIFEST = STORE + "/models/ALEXA/pryon.manifest"
SOUND_MANIFEST = STORE + "/models/AED/pryon.manifest"
WHISPER_MANIFEST = STORE + "/whisper/pryon_whisper.manifest"
SOUND_FILE = "/opt/persist/sound-detection"
STATE_DIR = "/run/biscuit-amazon-wake"
WAKE_ID = "amazon_alexa"
NOBODY = 65534

HINT_ALARM, HINT_MUSIC, HINT_PLAYBACK, COMMAND_SOUND = 0, 1, 2, 3
COMMAND_WHISPER_BEGIN, COMMAND_WHISPER_END = 4, 5
ACCEPTED = 2                    # the engine's result type for a keyword it stands behind
MAX_FRAME_BYTES = 8192          # the helper takes at most 4096 samples at a time
QUEUE_FRAMES = 64               # several seconds of the microphone's blocks
IDLE_STOP_S = 15.0
RESTART_DELAYS_S = (1.0, 2.0, 5.0, 15.0, 60.0)
STABLE_AFTER_S = 120.0          # this long running forgets earlier crashes
SOUND_WINDOW_S = 11.0           # a scoring window, 9.98 s, and the decoder's lag behind it
SOUND_REPEAT_S = 30.0
SOUND_POLL_S = 2.0
# The wake word threshold slider. The model's own figure for quiet rooms is 0.923; far below this range it
# would wake on anything, and at 1 it would never wake.
THRESHOLD_MIN = 0.7
THRESHOLD_MAX = 0.99
THRESHOLD_SETTLE_S = 2.0        # a slider being dragged is not worth an engine restart per step
MODEL_DEFAULT_THRESHOLD = 0.923
LOUDNESS_WINDOW_S = 2.0
PLAYBACK_POLL_S = 3.0
PLAYBACK_IDLE_S = 10.0

# Amazon's names for what its model hears, to one event each. On echo-dot-assist's test windows the three
# alarm types scored the same; the event must not pretend to tell them apart.
SOUND_EVENTS = {
    "smokeAlarm": "smoke_or_co_alarm",
    "smokeSiren": "smoke_or_co_alarm",
    "carbonMonoxideSiren": "smoke_or_co_alarm",
    "glassBreak": "glass_break",
    "dogBark": "dog_bark",
    "babyCry": "baby_cry",
    "snore": "snoring",
    "cough": "cough",
    "waterSounds": "water",
    "beepingAppliance": "beeping_appliance",
}
SOUND_EVENT_TYPES = sorted(set(SOUND_EVENTS.values()))


def _isolate() -> None:
    """Between fork and exec: the engine gets pipes and nothing else on the network or as a user."""
    os.unshare(os.CLONE_NEWNET)
    _LIBC.prctl(38, 1, 0, 0, 0)         # PR_SET_NO_NEW_PRIVS
    _LIBC.prctl(1, 9, 0, 0, 0)          # PR_SET_PDEATHSIG, SIGKILL: never outlives the assistant
    os.setgroups([])
    os.setgid(NOBODY)
    os.setuid(NOBODY)


# Loaded up front: a library load between fork and exec is what makes preexec functions unsafe.
_LIBC = ctypes.CDLL(None)


def installed(store: str = STORE) -> bool:
    return (os.path.isfile(store + "/bin/linker") and os.path.isfile(store + "/lib/libpryon.so")
            and os.path.isfile(store + "/models/ALEXA/pryon.manifest") and os.path.isfile(HELPER))


def sound_installed(store: str = STORE) -> bool:
    return installed(store) and os.path.isfile(store + "/models/AED/pryon.manifest")


def whisper_installed(store: str = STORE) -> bool:
    return installed(store) and os.path.isfile(store + "/whisper/pryon_whisper.manifest")


def _alexa_thresholds(doc: dict) -> dict:
    """The model's ALEXA thresholds, in its keyword configuration. KeyError or the like if it has none."""
    for spotter in doc["keyword-orchestra"]["keyword-spotters"]:
        if spotter["name"] == "ALEXA":
            return spotter["spotter"]["classification-thresholds"]
    raise KeyError("ALEXA")


def model_threshold(model_dir: str = STORE + "/models/ALEXA") -> float:
    """What the imported model accepts in a quiet room, for the slider's starting point."""
    try:
        with open(os.path.join(model_dir, "kw.cfg.json"), encoding="utf-8") as f:
            return float(_alexa_thresholds(json.load(f))["accept-threshold"])
    except (OSError, ValueError, KeyError, TypeError):
        return MODEL_DEFAULT_THRESHOLD


def retuned(doc: dict, value: float) -> dict:
    """The keyword configuration with ALEXA's threshold at value.

    The model holds lower figures for while an alarm or music plays, so it still hears over them. They
    move with the slider, as does the near-miss report, which must stay below what is accepted.
    """
    limits = _alexa_thresholds(doc)
    shift = value - float(limits["accept-threshold"])
    limits["accept-threshold"] = value
    if "notify-threshold" in limits:
        limits["notify-threshold"] = min(float(limits["notify-threshold"]), value)
    for override in limits.get("overrides", []):
        if "accept-threshold" in override:
            override["accept-threshold"] = round(
                max(0.5, min(0.999, float(override["accept-threshold"]) + shift)), 3)
    return doc


def build_overlay(model_dir: str, overlay: str, value: float) -> str:
    """The model with another threshold, without touching the owner's files: links to all of them and our
    own keyword configuration. Returns the manifest to load."""
    shutil.rmtree(overlay, ignore_errors=True)
    os.makedirs(overlay)
    os.chmod(overlay, 0o755)
    for name in os.listdir(model_dir):
        if name != "kw.cfg.json":
            os.symlink(os.path.join(model_dir, name), os.path.join(overlay, name))
    with open(os.path.join(model_dir, "kw.cfg.json"), encoding="utf-8") as f:
        doc = retuned(json.load(f), value)
    path = os.path.join(overlay, "kw.cfg.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.chmod(path, 0o644)
    return os.path.join(overlay, "pryon.manifest")


class Engine:
    """The helper process: started when something needs it, restarted when it dies."""

    def __init__(
        self,
        command: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        preexec: Optional[Callable[[], None]] = _isolate,
        state_dir: str = STATE_DIR,
        delays=RESTART_DELAYS_S,
        model_dir: str = STORE + "/models/ALEXA",
    ) -> None:
        self.command = command or [
            HELPER, WAKE_MANIFEST,
            SOUND_MANIFEST if sound_installed() else "-",
            WHISPER_MANIFEST if whisper_installed() else "-",
        ]
        self.env = env if env is not None else {
            "ANDROID_ROOT": "/system",
            "ANDROID_DATA": "/data",
            "LD_LIBRARY_PATH": STORE + "/lib",
        }
        self.preexec = preexec
        self.state_dir = state_dir
        self.delays = delays
        self.model_dir = model_dir
        self._manifest = os.path.join(model_dir, "pryon.manifest").replace("\\", "/")
        self.default_threshold = model_threshold(model_dir)
        self._threshold_applied = self.default_threshold
        self._threshold_wanted: Optional[float] = None
        self._threshold_since = 0.0
        self._applying = False
        self._whisper: Optional[bool] = None
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._queue: queue.Queue = queue.Queue(QUEUE_FRAMES)
        self._writer: Optional[threading.Thread] = None
        self._accepted: Set[str] = set()
        self._sounds: List[str] = []
        self._hints: Dict[int, int] = {}
        self._want_sound = False
        self._retry_at = 0.0
        self._crashes = 0
        self._started_at = 0.0
        self._last_use = 0.0
        self._state = "off"
        self._sound_state = "off"
        self.dropped = 0
        self.error = ""
        self._write_state()

    # ------------------------------------------------------------ from the audio thread

    def feed(self, pcm: bytes) -> None:
        now = time.monotonic()
        self._last_use = now
        if not self._running():
            self._start(now)
        for at in range(0, len(pcm), MAX_FRAME_BYTES):
            part = pcm[at:at + MAX_FRAME_BYTES]
            self._put(struct.pack("<H", len(part) // 2) + part)

    def hint(self, which: int, value: bool) -> None:
        """Tell the engine what is playing. Its thresholds depend on it, so the wake word is still
        heard over music and alarms."""
        value = int(bool(value))
        if self._hints.get(which) != value:
            self._hints[which] = value
            if self._running():
                self._put(struct.pack("<HBB", 0, which, value))

    def set_sound(self, on: bool) -> None:
        if self._want_sound != bool(on):
            self._want_sound = bool(on)
            if self._running():
                self._put(struct.pack("<HBB", 0, COMMAND_SOUND, int(self._want_sound)))
            if not on:
                self._sound_state = "off"
                self._write_state()

    def set_threshold(self, value: float, now: Optional[float] = None) -> None:
        """The sensitivity slider. The engine takes its threshold from a file when it loads, so a new one
        means a new file and a restart, off this thread and only once the slider has come to rest."""
        now = time.monotonic() if now is None else now
        value = round(max(THRESHOLD_MIN, min(THRESHOLD_MAX, float(value))), 3)
        if value != self._threshold_wanted:
            first = self._threshold_wanted is None
            self._threshold_wanted = value
            self._threshold_since = now - (THRESHOLD_SETTLE_S if first else 0.0)
        if self._applying or value == self._threshold_applied:
            return
        if now - self._threshold_since < THRESHOLD_SETTLE_S:
            return
        self._applying = True       # before the thread starts: the next block must not start the engine
        threading.Thread(target=self._apply_threshold, args=(value,), daemon=True, name="amazon-tune").start()

    def _apply_threshold(self, value: float) -> None:
        try:
            self.stop()
            manifest = os.path.join(self.model_dir, "pryon.manifest").replace("\\", "/")
            if value != self.default_threshold:
                try:
                    manifest = build_overlay(self.model_dir, os.path.join(self.state_dir, "model"), value)
                    manifest = manifest.replace("\\", "/")
                    _LOGGER.info("Amazon engine: wake word threshold is now %.3f", value)
                except (OSError, ValueError, KeyError, TypeError) as err:
                    _LOGGER.warning("Amazon engine keeps its own threshold: %s", err)
            self.command = [manifest if arg == self._manifest else arg for arg in self.command]
            self._manifest = manifest
        finally:
            self._threshold_applied = value
            self._applying = False

    def whisper_begin(self) -> None:
        with self._lock:
            self._whisper = None
        if self._running():
            self._put(struct.pack("<HBB", 0, COMMAND_WHISPER_BEGIN, 0))

    def whisper_end(self) -> None:
        if self._running():
            self._put(struct.pack("<HBB", 0, COMMAND_WHISPER_END, 1))

    def pop_whisper(self) -> Optional[bool]:
        """True or False once, when a request has been scored. Otherwise None."""
        with self._lock:
            result, self._whisper = self._whisper, None
            return result

    def idle(self) -> None:
        """Nothing needs the engine this block: let it go after a while, it costs 30% of a core."""
        if self._running() and time.monotonic() - self._last_use > IDLE_STOP_S:
            self.stop()

    def pop_accepted(self, keyword: str) -> bool:
        with self._lock:
            if keyword in self._accepted:
                self._accepted.discard(keyword)
                return True
            return False

    def pop_sounds(self) -> List[str]:
        with self._lock:
            sounds, self._sounds = self._sounds, []
            return sounds

    # ------------------------------------------------------------ the process

    def _put(self, frame: bytes) -> None:
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self.dropped += 1
            if self.dropped % 200 == 1:
                _LOGGER.warning("Amazon engine is not keeping up: lost %d blocks", self.dropped)

    def _running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _start(self, now: float) -> None:
        if now < self._retry_at or self._applying:
            return
        self._reap(now)
        self._queue = queue.Queue(QUEUE_FRAMES)
        try:
            proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                cwd="/",
                preexec_fn=self.preexec,
                close_fds=True,
            )
        except (OSError, subprocess.SubprocessError) as err:
            self._failed(now, str(err))
            return
        self._proc = proc
        self._started_at = now
        self._retry_at = now + self.delays[0]
        self._state = "starting"
        self.error = ""
        self._write_state()
        for which, value in self._hints.items():
            self._put(struct.pack("<HBB", 0, which, value))
        if self._want_sound:
            self._put(struct.pack("<HBB", 0, COMMAND_SOUND, 1))
        threading.Thread(target=self._read_results, args=(proc,), daemon=True, name="amazon-read").start()
        threading.Thread(target=self._read_errors, args=(proc,), daemon=True, name="amazon-err").start()
        self._writer = threading.Thread(
            target=self._write_frames, args=(proc, self._queue), daemon=True, name="amazon-write")
        self._writer.start()

    def _reap(self, now: float) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        code = proc.poll()
        if now - self._started_at > STABLE_AFTER_S:
            self._crashes = 0
        self._failed(now, "stopped with status %s" % code if code else "stopped")

    def _failed(self, now: float, why: str) -> None:
        delay = self.delays[min(self._crashes, len(self.delays) - 1)]
        self._crashes += 1
        self._retry_at = now + delay
        self.error = why
        self._state = "failed"
        _LOGGER.warning("Amazon engine: %s; trying again in %.0f s", why, delay)
        self._write_state()

    def _write_frames(self, proc: subprocess.Popen, frames: queue.Queue) -> None:
        stdin = proc.stdin
        try:
            while True:
                frame = frames.get()
                if frame is None:
                    break
                stdin.write(frame)
                stdin.flush()
        except (OSError, ValueError):
            pass            # the reader sees the same death and reports it
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def _read_results(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            self._result(raw.decode("ascii", "replace").strip())

    def _read_errors(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line and "DT_FLAGS_1" not in line and "tzdata" not in line:
                _LOGGER.info("Amazon engine: %s", line[:300])

    def _result(self, line: str) -> None:
        parts = line.split()
        if not parts:
            return
        if parts[0] == "R":
            self._state = "listening"
            self._crashes = 0 if time.monotonic() - self._started_at > STABLE_AFTER_S else self._crashes
            _LOGGER.info("Amazon engine is listening")
            self._write_state()
        elif parts[0] == "K" and len(parts) >= 3:
            try:
                accepted = int(parts[2]) == ACCEPTED
            except ValueError:
                return
            if accepted:
                with self._lock:
                    self._accepted.add(parts[1])
        elif parts[0] == "A" and len(parts) == 2:
            names = [SOUND_EVENTS[n] for n in parts[1].split(",") if n in SOUND_EVENTS]
            with self._lock:
                self._sounds.extend(n for n in dict.fromkeys(names))
        elif parts[0] == "W" and len(parts) == 4:
            try:
                whispered = int(parts[1]) == 1
                _LOGGER.info("Whisper score %s against %s", parts[2], parts[3])
            except ValueError:
                return
            with self._lock:
                self._whisper = whispered
        elif parts[0] == "W" and len(parts) == 2:
            if parts[1] == "failed":
                _LOGGER.warning("Amazon's whisper model did not load")
        elif parts[0] == "S" and len(parts) == 2:
            self._sound_state = parts[1]
            if parts[1] == "failed":
                _LOGGER.warning("Amazon's sound detector did not load")
            self._write_state()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                pass
        self._state = "off"
        self._sound_state = "off"
        self._retry_at = 0.0
        self._write_state()

    def _write_state(self) -> None:
        """What the settings page shows. /run, so nothing is left of it after a restart."""
        if not self.state_dir:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = os.path.join(self.state_dir, ".state.json.new")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"engine": self._state, "sound": self._sound_state, "error": self.error}, f)
            os.replace(tmp, os.path.join(self.state_dir, "state.json"))
        except OSError:
            pass


class AmazonWakeWord:
    """One keyword of the engine, in the shape the audio loop expects of a wake word."""

    def __init__(self, engine: Engine, wake_word_id: str = WAKE_ID, phrase: str = "Alexa",
                 keyword: str = "ALEXA") -> None:
        self.engine = engine
        self.id = wake_word_id
        self.wake_word = phrase         # what arbitration and Home Assistant are told was said
        self.keyword = keyword
        self.probability_cutoff = engine.default_threshold

    def detected(self) -> bool:
        return self.engine.pop_accepted(self.keyword)


class AmazonWakeEntry:
    """What LVA keeps for a wake word on offer, in the shape find_available_wake_words makes them."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.id = WAKE_ID
        self.type = "amazon"
        # In the list next to microWakeWord's own Alexa, which is a different thing.
        self.wake_word = "Alexa (Amazon engine)"
        self.trained_languages = ["en"]
        self.wake_word_path = Path(WAKE_MANIFEST)
        # Where the sensitivity slider starts: the model's own figure, which the slider then moves.
        self.probability_cutoff = engine.default_threshold

    def load(self) -> AmazonWakeWord:
        return AmazonWakeWord(self.engine)


class PlaybackWatch:
    """Whether anything at all is playing on this Echo: Sendspin and Bluetooth are not the voice
    assistant's own players, and the engine should know of them too. Looks only while the engine is in use.
    """

    COMMAND = ["pactl", "list", "sink-inputs"]
    ENV = {"XDG_RUNTIME_DIR": "/run/pipewire", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}

    def __init__(self, command: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None,
                 poll: float = PLAYBACK_POLL_S, idle: float = PLAYBACK_IDLE_S) -> None:
        self.command = command or self.COMMAND
        self.env = self.ENV if env is None else env
        self.poll = poll
        self.idle = idle
        self.playing = False
        self._used = 0.0
        self._thread: Optional[threading.Thread] = None

    def poke(self) -> None:
        self._used = time.monotonic()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="amazon-playback")
            self._thread.start()

    def look(self) -> bool:
        try:
            done = subprocess.run(self.command, env=self.env, capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return b"Corked: no" in done.stdout

    def _run(self) -> None:
        while True:
            if time.monotonic() - self._used > self.idle:
                self.playing = False
            else:
                self.playing = self.look()
            time.sleep(self.poll)


class AudioSide:
    """What the audio loop does for the engine on each block, kept out of that long loop."""

    def __init__(self, engine: Engine, sound_file: str = SOUND_FILE, sound_ok: Optional[bool] = None,
                 whisper_ok: Optional[bool] = None, playback: Optional[PlaybackWatch] = None) -> None:
        self.engine = engine
        self.sound_file = sound_file
        self.sound_ok = sound_installed() if sound_ok is None else sound_ok
        self.whisper_ok = whisper_installed() if whisper_ok is None else whisper_ok
        self.playback = playback or PlaybackWatch()
        self.rms = 0.0
        self.peak = 0.0
        self._levels: collections.deque = collections.deque()
        self._listening = False
        self._sound_on = False
        self._next_poll = 0.0
        self._own_sound_until = 0.0
        self._last_event: Dict[str, float] = {}

    def want_sound(self, now: float) -> bool:
        if now >= self._next_poll:
            self._next_poll = now + SOUND_POLL_S
            self._sound_on = self.sound_ok and os.path.exists(self.sound_file)
            self.engine.set_sound(self._sound_on)
        return self._sound_on

    def _measure(self, pcm: bytes, now: float) -> None:
        """How loud this block is, and the loudest of the last two seconds: how "Alexa, stop" tells whether
        the speaker is still talking. Every fourth sample is plenty."""
        samples = array.array("h")
        samples.frombytes(pcm[:len(pcm) & ~1])
        samples = samples[::4]
        if not samples:
            return
        self.rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        levels = self._levels
        levels.append((now, self.rms))
        while levels and levels[0][0] < now - LOUDNESS_WINDOW_S:
            levels.popleft()
        self.peak = max(level for _at, level in levels)

    def block(self, pcm: bytes, active: bool, alarm: bool, music: bool, speaking: bool,
              now: Optional[float] = None, listening: bool = False) -> List[str]:
        """One microphone block. Returns the sounds to report to Home Assistant."""
        now = time.monotonic() if now is None else now
        engine = self.engine
        sound = self.want_sound(now)
        listening = listening and self.whisper_ok
        begin = listening and not self._listening
        if self._listening and not listening:
            engine.whisper_end()
        self._listening = listening
        if not (active or sound or listening):
            engine.idle()
            return []
        self._measure(pcm, now)
        self.playback.poke()
        music = music or self.playback.playing
        engine.hint(HINT_ALARM, alarm)
        engine.hint(HINT_MUSIC, music)
        engine.hint(HINT_PLAYBACK, alarm or music or speaking)
        if alarm or music or speaking:
            self._own_sound_until = now + SOUND_WINDOW_S
        engine.feed(pcm)            # first: this is what starts the engine, and a begin needs it running
        if begin:
            engine.whisper_begin()
        heard = engine.pop_sounds()
        if not heard:
            return []
        # Amazon checks a detection in its cloud before telling anyone; nothing here can. A window in
        # which this Echo made sound is dropped: echo cancellation leaves enough of a timer ringing to
        # pass for a beeping appliance.
        if now < self._own_sound_until:
            _LOGGER.debug("Sound ignored while this Echo was playing: %s", heard)
            return []
        fresh = []
        for name in heard:
            if now - self._last_event.get(name, -SOUND_REPEAT_S) >= SOUND_REPEAT_S:
                self._last_event[name] = now
                fresh.append(name)
        return fresh


ENGINE: Optional[Engine] = None
AUDIO_SIDE: Optional[AudioSide] = None


def offer(available_wake_words: dict) -> Optional[AudioSide]:
    """Add the engine's wake word to what Home Assistant can choose, if the owner imported it."""
    global ENGINE, AUDIO_SIDE
    if not installed():
        _LOGGER.debug("Amazon's engine is not imported: not offered")
        return None
    if ENGINE is None:
        ENGINE = Engine()
        AUDIO_SIDE = AudioSide(ENGINE)
    available_wake_words[WAKE_ID] = AmazonWakeEntry(ENGINE)
    _LOGGER.info("Amazon's wake word engine is imported and on offer")
    return AUDIO_SIDE
