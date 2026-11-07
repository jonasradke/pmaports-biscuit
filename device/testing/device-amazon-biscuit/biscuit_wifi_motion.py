"""Someone moving in the room, from the Wi-Fi level. Experimental, and off unless turned on.

A person between the Echo and the access point shadows the path, and the level the radio reports moves.

The good reading is per frame, from /proc/net/wlan/rcpi, which needs a kernel with the patch in
tools/pending-kernel. Taken ten times a second, it goes by how unsteady the level is:
the method of echo-dot-assist, https://github.com/Gamer92000/echo-dot-assist, MIT licensed.

Without that file this falls back to /proc/net/wireless: one figure in whole dB, which this driver smooths, and
which on 2026-11-07 stayed at -61 dBm for 286 of 291 seconds with -62 for the rest. So small changes are not
movement, and it goes by how far the last few seconds sit from the last minute. That catches someone walking
right through the path and little else.
"""
import collections
import json
import os
import statistics
import time

PROC = "/proc/net/wireless"
FRAMES = "/proc/net/wlan/rcpi"
CONFIG = "/opt/persist/wifi-motion.json"
INTERFACE = "wlan0"
POLL_S = 1.0
FRAME_POLL_S = 0.1
BASELINE_S = 60
RECENT_S = 3
HOLD_S = 30.0
THRESHOLD_DEFAULT = 2
SENSITIVITY_DEFAULT = 5
SENSITIVITY_MIN = 1
SENSITIVITY_MAX = 10
LEVEL_S = 30.0                  # what counts as this path's usual level
WINDOW = 20                     # readings: two seconds of them
GAP_S = 5.0                     # no frames for this long starts the spread over


def read_level(path=PROC, interface=INTERFACE):
    """dBm, or None while there is no reading. The driver gives it as an unsigned byte."""
    try:
        with open(path, encoding="ascii") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in lines:
        if line.strip().startswith(interface + ":"):
            try:
                level = float(line.split()[3].rstrip("."))
            except (IndexError, ValueError):
                return None
            if level <= 0:
                return None
            return level - 256 if level > 63 else level
    return None


def read_frame(path=FRAMES):
    """(RCPI, frames so far, kind) of the last data frame, or None. Kind is 1 for group frames: they come at
    another rate, so their level differs from the rest."""
    try:
        with open(path, encoding="ascii") as f:
            fields = {}
            for line in f:
                key, _, value = line.partition("=")
                fields[key.strip()] = int(value)
        return fields["RCPI RX0"], fields["FRAMES"], fields["KIND"]
    except (OSError, ValueError, KeyError):
        return None


def coarse_threshold(sensitivity):
    """dB on the coarse reading. It has few steps, so the slider has few too."""
    if sensitivity <= 2:
        return 4
    if sensitivity <= 4:
        return 3
    if sensitivity <= 8:
        return THRESHOLD_DEFAULT
    return 1


def frame_threshold(sensitivity):
    return 1.2 * 1.15 ** (SENSITIVITY_DEFAULT - sensitivity)


class Detector:
    """The coarse reading."""

    def __init__(self, threshold=THRESHOLD_DEFAULT, hold=HOLD_S):
        self.threshold = threshold
        self.hold = hold
        self.samples = []
        self.until = 0.0
        self.state = False

    def feed(self, level, now):
        """Takes one reading, gives the state. A gap in the readings starts over: the Wi-Fi was
        away, and whatever it reads when it is back says nothing about the room."""
        if level is None:
            self.samples.clear()
            return self.state and now < self.until
        self.samples.append(level)
        del self.samples[:-(BASELINE_S + RECENT_S)]
        if len(self.samples) >= BASELINE_S // 2:
            recent = statistics.median(self.samples[-RECENT_S:])
            baseline = statistics.median(self.samples[:-RECENT_S])
            if abs(recent - baseline) >= self.threshold:
                self.until = now + self.hold
        self.state = now < self.until
        return self.state


class FrameDetector:
    """Per-frame levels: the spread over two seconds, against the threshold, for at least half of them."""

    def __init__(self, threshold=1.2, hold=HOLD_S):
        self.threshold = threshold
        self.hold = hold
        self.level = {}
        self.last_at = {}
        self.last_frames = None
        self.last_seen = 0.0
        self.recent = collections.deque(maxlen=3)
        self.window = collections.deque(maxlen=WINDOW)
        self.above = collections.deque(maxlen=WINDOW)
        self.until = 0.0

    def feed(self, reading, now):
        if reading is None:
            self.recent.clear()
            self.window.clear()
            self.above.clear()
            return now < self.until
        rcpi, frames, kind = reading
        if frames == self.last_frames:      # nothing new arrived: the same figure says nothing
            return now < self.until
        self.last_frames = frames
        if now - self.last_seen > GAP_S:    # what came before a silence is not this moment's spread
            self.recent.clear()
            self.window.clear()
            self.above.clear()
        self.last_seen = now
        db = rcpi / 2.0
        if kind not in self.level:
            self.level[kind] = db
            self.last_at[kind] = now
        gap = min(LEVEL_S, max(0.0, now - self.last_at[kind]))
        self.level[kind] += (db - self.level[kind]) * gap / LEVEL_S
        self.last_at[kind] = now
        self.recent.append(db - self.level[kind])
        self.window.append(statistics.median(self.recent))
        if len(self.window) == WINDOW:
            self.above.append(statistics.pstdev(self.window) > self.threshold)
            if len(self.above) == WINDOW and sum(self.above) * 2 >= WINDOW:
                self.until = now + self.hold
        return now < self.until


class Motion:
    """Whichever reading this kernel has."""

    def __init__(self, frames=FRAMES, proc=PROC):
        self.frames = frames
        self.proc = proc
        self.coarse = Detector()
        self.fine = FrameDetector()
        self.delay = POLL_S

    def configure(self, sensitivity):
        self.coarse.threshold = coarse_threshold(sensitivity)
        self.fine.threshold = frame_threshold(sensitivity)

    def step(self, now=None):
        now = time.monotonic() if now is None else now
        if os.path.exists(self.frames):
            self.delay = FRAME_POLL_S
            return self.fine.feed(read_frame(self.frames), now)
        self.delay = POLL_S
        return self.coarse.feed(read_level(self.proc), now)


def load(path=CONFIG):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    try:
        sensitivity = int(doc.get("sensitivity", SENSITIVITY_DEFAULT))
    except (TypeError, ValueError):
        sensitivity = SENSITIVITY_DEFAULT
    return {"on": doc.get("on") is True,
            "sensitivity": max(SENSITIVITY_MIN, min(SENSITIVITY_MAX, sensitivity))}


def save(path=CONFIG, **changes):
    doc = load(path)
    doc.update(changes)
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)
    return doc
