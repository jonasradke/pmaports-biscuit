# SPDX-License-Identifier: MIT
"""A press of the Echo's own button, as the second half of signing in.

The settings page is plain HTTP on the owner's network. The password is sealed on
its way, but a password can still be guessed, phished or reused. With this turned
on, knowing it is not enough: someone also has to be in the room.

Only one sign-in can wait at a time, and a second one is refused rather than
queued. A press approves exactly one sign-in, and the person pressing has just
asked for one. If a stranger's attempt is already waiting, the owner's own attempt
is refused and says so, instead of the owner's press approving the stranger.

The button is only read while a sign-in waits. A marker in /run tells biscuit-va-leds
to leave that press alone, so it does not also pause the music. The marker stays a
moment after the press, because a gesture is only decided once the press has ended.
"""
import glob
import logging
import os
import secrets
import select
import struct
import threading
import time

_LOGGER = logging.getLogger("biscuit-login-wait")

BUTTON_DEVICE = "mt6779-keypad"
KEY_ASSISTANT = 583
EV_KEY = 1
TTL_S = 60
KEEP_MARK_S = 2.0
MARK = "/run/biscuit-settings/login-wait"

# struct input_event: its time is two longs, whatever size this kernel has them.
_FORMAT = "@llHHi"
_SIZE = struct.calcsize(_FORMAT)


def presses(data, fmt=_FORMAT):
    """Whether these input events hold a press of the button going down."""
    size = struct.calcsize(fmt)
    for off in range(0, len(data) - size + 1, size):
        _s, _us, etype, code, value = struct.unpack_from(fmt, data, off)
        if etype == EV_KEY and code == KEY_ASSISTANT and value == 1:
            return True
    return False


class _Button:
    def __init__(self):
        self.fd = None
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                with open("/sys/class/input/%s/device/name" % os.path.basename(path)) as f:
                    if f.read().strip() == BUTTON_DEVICE:
                        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                        return
            except OSError:
                continue
        raise OSError("the button is not available")

    def wait_press(self, timeout):
        ready, _w, _x = select.select([self.fd], [], [], timeout)
        if not ready:
            return False
        try:
            return presses(os.read(self.fd, _SIZE * 32))
        except BlockingIOError:
            return False

    def close(self):
        os.close(self.fd)


class LoginWait:
    def __init__(self, open_button=_Button, mark=MARK, ttl=TTL_S, keep_mark=KEEP_MARK_S):
        self.open_button = open_button
        self.mark = mark
        self.ttl = ttl
        self.keep_mark = keep_mark
        self.lock = threading.Lock()
        self.entry = None

    def _set_mark(self, on):
        try:
            if on:
                os.makedirs(os.path.dirname(self.mark), exist_ok=True)
                with open(self.mark, "w", encoding="ascii"):
                    pass
            else:
                os.unlink(self.mark)
        except OSError:
            pass

    def begin(self, user, addr):
        """A token to poll with, or None when another sign-in is already waiting.
        OSError when the button cannot be read: refused, not let through."""
        with self.lock:
            now = time.monotonic()
            cur = self.entry
            if cur is not None and cur["state"] == "waiting" and now < cur["deadline"]:
                return None
            button = self.open_button()
            entry = {"token": secrets.token_urlsafe(24), "user": user, "addr": addr,
                     "state": "waiting", "deadline": now + self.ttl}
            self.entry = entry
        self._set_mark(True)
        threading.Thread(target=self._watch, args=(entry, button),
                         name="login-wait", daemon=True).start()
        return entry["token"]

    def _watch(self, entry, button):
        try:
            while time.monotonic() < entry["deadline"]:
                if button.wait_press(0.25):
                    with self.lock:
                        entry["state"] = "approved"
                        entry["deadline"] = time.monotonic() + self.ttl
                    _LOGGER.info("sign-in by %s from %s approved at the button",
                                 entry["user"], entry["addr"])
                    break
        except OSError as err:
            _LOGGER.warning("the button stopped answering: %s", err)
        finally:
            button.close()
        with self.lock:
            if entry["state"] == "waiting":
                entry["state"] = "expired"
        time.sleep(self.keep_mark)
        with self.lock:
            cur = self.entry
            waiting = cur is not None and cur is not entry and cur["state"] == "waiting"
        if not waiting:
            self._set_mark(False)

    def poll(self, token, addr):
        """("waiting", None), ("approved", user) once, or ("expired", None)."""
        with self.lock:
            entry = self.entry
            # Tokens are random, but they crossed the network in the clear.
            if (entry is None or not secrets.compare_digest(str(token), entry["token"])
                    or addr != entry["addr"]):
                return "expired", None
            if entry["state"] == "approved" and time.monotonic() < entry["deadline"]:
                self.entry = None
                return "approved", entry["user"]
            if entry["state"] == "waiting" and time.monotonic() < entry["deadline"]:
                return "waiting", None
            return "expired", None
