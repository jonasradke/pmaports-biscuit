# SPDX-License-Identifier: MIT
"""Talk from one Echo to another. BISCUIT.

Only between Echos paired for wake word arbitration, and with the same key: pairing is where the owner
compared codes, so that is what says these two belong together. Off until then, with no socket open.
No cloud and no Home Assistant in the path: the audio goes straight from one Echo to the other.

A CALL

The caller asks, the other Echo chimes and shows it on the ring, and someone presses its action button.
"Answer by itself" skips the press. That makes a room's microphone reachable from another Echo with nobody
there agreeing, so it is off until the owner turns it on, per Echo, on the Echo being called. Either end
hangs up with the action button, by saying "stop", from Home Assistant or on the settings page. So does
silence from the far end for a few seconds. The ring shows the listening light on both while the microphone is open.
A muted Echo sends nothing, whatever the call's state.

Half duplex: it sends while someone is talking here and nobody is talking there. The microphone chain cancels
what this Echo plays, and even so a voice coming back from the other room is what turns a full-duplex
speakerphone into a howl. The far end talking wins, because it is already talking.

ON THE WIRE

UDP, one frame per packet, each sealed with ChaCha20-Poly1305. Every process picks a random sender id and
uses its own key derived from the shared one, so a nonce (the frame's counter) never repeats under a key,
even across restarts. A counter already seen is refused, and a call request older than a minute is too, so
a recorded one cannot make an Echo chime later. Audio is uncompressed 16 kHz: 256 kbit/s on a home network.
"""
import collections
import hashlib
import json
import logging
import math
import os
import queue
import secrets
import select
import socket
import struct
import subprocess
import threading
import time

try:
    from . import biscuit_crypto
except ImportError:  # run on its own by a test
    import biscuit_crypto

_LOGGER = logging.getLogger("biscuit-intercom")

PORT = 6056
MAGIC = b"BIC1"
CONFIG_FILE = os.environ.get("BISCUIT_INTERCOM_CONFIG", "/opt/persist/intercom.json")
RUN_DIR = os.environ.get("BISCUIT_INTERCOM_RUN", "/run/biscuit-intercom")

IDLE, CALLING, RINGING, TALKING = "idle", "calling", "ringing", "talking"
RING_S = 30.0
RESEND_S = 1.0
KEEPALIVE_S = 1.0
LOST_S = 6.0
MAX_TALK_S = 900.0
FRESH_S = 60.0
FRAME_BYTES = 960               # 30 ms; well inside one packet, so nothing fragments
PLAY_QUEUE = 34                 # about a second behind is already not a conversation
SEEN = 512

GATE_MIN_RMS = 150.0
GATE_RATIO = 3.0
GATE_HANG_S = 0.45
FAR_HOLD_S = 0.25


def _derive(key, sender):
    return hashlib.sha256(b"biscuit-intercom\0" + key + sender).digest()


def _nonce(counter):
    return b"\0\0\0\0" + struct.pack("<Q", counter)


class Gate:
    """Is someone talking at this end? The floor follows the room, so a fan does not hold it open."""

    def __init__(self):
        self.floor = None
        self.open_until = 0.0

    def feed(self, pcm, now):
        n = len(pcm) // 2
        if not n:
            return now < self.open_until
        values = struct.unpack("<%dh" % n, pcm[:n * 2])
        rms = math.sqrt(sum(v * v for v in values) / n)
        if self.floor is None:
            # Not this block's level: the call may begin with someone already talking.
            self.floor = GATE_MIN_RMS / GATE_RATIO
        # Quick down, slow up: it should learn a quiet room at once and speech hardly at all.
        self.floor += (rms - self.floor) * (0.2 if rms < self.floor else 0.002)
        if rms > max(GATE_MIN_RMS, self.floor * GATE_RATIO):
            self.open_until = now + GATE_HANG_S
        return now < self.open_until


class Player:
    """pw-cat with a queue in front, so a stalled sound server costs audio and never this thread."""

    COMMAND = ["pw-cat", "--playback", "--raw", "--rate", "16000", "--channels", "1", "--format", "s16", "-"]

    def __init__(self, command=None):
        self.queue = queue.Queue(PLAY_QUEUE)
        self.proc = subprocess.Popen(command or self.COMMAND, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._run, daemon=True, name="intercom-play").start()

    def _run(self):
        try:
            while True:
                pcm = self.queue.get()
                if pcm is None:
                    break
                self.proc.stdin.write(pcm)
                self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def write(self, pcm):
        try:
            self.queue.put_nowait(pcm)
        except queue.Full:
            pass

    def close(self):
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            self.proc.kill()
            return
        try:
            self.proc.wait(1)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Intercom(threading.Thread):
    def __init__(self, name, key, peers, on_change=None, busy=None, player=Player, config=CONFIG_FILE,
                 run_dir=RUN_DIR, port=PORT, bind="", muted=None):
        super().__init__(daemon=True, name="intercom")
        self.name_ = name
        self.key = key                  # callable: the shared key, or None when not paired
        self.peers = peers              # callable: [{"id", "name", "addr": (host, port)}]
        self.on_change = on_change or (lambda state, peer: None)
        self.busy = busy or (lambda: False)
        self.muted = muted or (lambda: False)
        self.make_player = player
        self.config = config
        self.run_dir = run_dir
        self.port = port
        self.bind = bind
        self.sock = None
        self.running = True
        self.sender = secrets.token_bytes(8)
        self.counter = 0
        self.send_lock = threading.Lock()
        self.send_key = None
        self.send_key_from = None
        self.senders = {}
        self.state = IDLE
        self.call = None
        self.message = ""
        self.player = None
        self.gate = Gate()
        self.far_until = 0.0
        self.sent_frames = 0
        self.played_frames = 0
        self._cmds = collections.deque()
        self._write_state()

    # ------------------------------------------------------------ for other threads

    @property
    def active(self):
        return self.state != IDLE

    def command(self, word):
        self._cmds.append(word)

    def auto_answer(self):
        try:
            with open(self.config, encoding="utf-8") as f:
                return json.load(f).get("auto_answer") is True
        except (OSError, ValueError):
            return False

    def feed_mic(self, pcm, now=None):
        """From the audio thread. Never blocks, and sends nothing unless a call is up."""
        if self.state != TALKING:
            return
        now = time.monotonic() if now is None else now
        talking = self.gate.feed(pcm, now)
        call = self.call
        if call is None or not talking or now < self.far_until or self.muted():
            return
        for at in range(0, len(pcm), FRAME_BYTES):
            self._send(b"P" + bytes.fromhex(call["id"]) + pcm[at:at + FRAME_BYTES], call["addr"])
            self.sent_frames += 1

    # ------------------------------------------------------------ the thread

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            try:
                self._socket_as_needed()
                readable = []
                if self.sock is not None:
                    readable, _w, _x = select.select([self.sock], [], [], 0.05)
                else:
                    time.sleep(0.2)
                now = time.monotonic()
                if readable:
                    self._receive(now)
                self._commands(now)
                self._tick(now)
            except Exception:  # noqa: BLE001 - this must not take the voice assistant down
                _LOGGER.exception("Intercom error")
                time.sleep(1.0)
        self._set(IDLE, None)
        if self.sock is not None:
            self.sock.close()

    def _socket_as_needed(self):
        wanted = self.key() is not None
        if wanted and self.sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            try:
                sock.bind((self.bind, self.port))
            except OSError as err:
                sock.close()
                _LOGGER.warning("Intercom could not open its port: %s", err)
                time.sleep(5.0)
                return
            self.sock = sock
            self._write_state()
        elif not wanted and self.sock is not None:
            self._end(time.monotonic(), "", tell=False)
            self.sock.close()
            self.sock = None
            self.senders.clear()
            self._write_state()

    def _send(self, plain, addr):
        key = self.key()
        sock = self.sock
        if key is None or sock is None:
            return
        with self.send_lock:
            if self.send_key_from != key:
                self.send_key = _derive(key, self.sender)
                self.send_key_from = key
            self.counter += 1
            counter = self.counter
            header = MAGIC + self.sender + struct.pack("<Q", counter)
            data = header + biscuit_crypto._seal(self.send_key, _nonce(counter), header, plain)
        try:
            sock.sendto(data, addr)
        except OSError as err:
            _LOGGER.debug("Intercom could not send: %s", err)

    def _control(self, kind, body, addr):
        self._send(kind + json.dumps(body, separators=(",", ":")).encode(), addr)

    def _receive(self, now):
        for _ in range(64):
            try:
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            plain = self._open(data)
            if plain is None:
                continue
            kind, body = plain[:1], plain[1:]
            if kind == b"P":
                self._on_audio(body, addr, now)
                continue
            try:
                self._on_control(kind, json.loads(body), addr, now)
            except (ValueError, KeyError, TypeError):
                _LOGGER.debug("Intercom frame could not be understood")

    def _open(self, data):
        key = self.key()
        if key is None or len(data) < 20 + 16 or data[:4] != MAGIC:
            return None
        sender, counter = data[4:12], struct.unpack("<Q", data[12:20])[0]
        if sender == self.sender:
            return None
        known = self.senders.get(sender)
        derived = known["key"] if known else _derive(key, sender)
        if known and counter in known["seen"]:
            return None
        if known and counter + SEEN < known["top"]:
            return None
        try:
            plain = biscuit_crypto._open(derived, _nonce(counter), data[:20], data[20:])
        except Exception:  # noqa: BLE001 - the two crypto libraries raise different things
            return None
        # Only now, so a forged packet cannot fill this up.
        if known is None:
            if len(self.senders) > 16:
                self.senders.clear()
            known = self.senders[sender] = {
                "key": derived, "seen": set(), "order": collections.deque(), "top": 0}
        known["seen"].add(counter)
        known["order"].append(counter)
        known["top"] = max(known["top"], counter)
        if len(known["order"]) > SEEN:
            known["seen"].discard(known["order"].popleft())
        return plain

    def _on_audio(self, body, addr, now):
        call = self.call
        if self.state != TALKING or call is None or body[:8].hex() != call["id"]:
            return
        call["heard"] = now
        self.far_until = now + FAR_HOLD_S
        if self.player is not None:
            self.player.write(body[8:])
            self.played_frames += 1

    def _on_control(self, kind, msg, addr, now):
        call_id = str(msg["call"])
        call = self.call
        if kind == b"C":
            self._on_call(call_id, msg, addr, now)
            return
        if call is None or call_id != call["id"]:
            return
        call["heard"] = now
        if kind == b"R":
            if self.state == CALLING:
                call["rang"] = True
                self._write_state()
        elif kind == b"A":
            if self.state == CALLING:
                call["addr"] = addr
                self._set(TALKING, call["peer"], now)
        elif kind == b"B":
            if self.state == CALLING:
                self._end(now, "%s is busy." % call["peer"], tell=False)
        elif kind == b"E":
            self._end(now, "%s hung up." % call["peer"] if self.state == TALKING else "", tell=False)

    def _on_call(self, call_id, msg, addr, now):
        if abs(time.time() - float(msg["t"])) > FRESH_S or len(call_id) != 16:
            return
        peer = str(msg.get("name") or "the other Echo")[:64]
        call = self.call
        if call is not None and call["id"] == call_id:
            call["heard"] = now
            if self.state == RINGING:
                self._control(b"R", {"call": call_id}, addr)
            elif self.state == TALKING:
                self._control(b"A", {"call": call_id}, addr)
            return
        if self.state == CALLING and call["addr"][0] == addr[0]:
            # Both pressed call at once. Both want to talk: the lower id stands, and is answered.
            if call_id < call["id"]:
                self.call = {"id": call_id, "addr": addr, "peer": peer, "since": now, "heard": now}
                self._answer(now)
            return
        if self.state != IDLE or self.busy():
            self._control(b"B", {"call": call_id}, addr)
            return
        self.call = {"id": call_id, "addr": addr, "peer": peer, "since": now, "heard": now}
        self._control(b"R", {"call": call_id}, addr)
        if self.auto_answer():
            self._answer(now)
        else:
            self._set(RINGING, peer, now)

    def _answer(self, now):
        call = self.call
        self._control(b"A", {"call": call["id"]}, call["addr"])
        self._set(TALKING, call["peer"], now)

    def _end(self, now, message, tell=True):
        call = self.call
        if call is not None and tell:
            for _ in range(3):
                self._control(b"E", {"call": call["id"]}, call["addr"])
        self.call = None
        self.message = message
        self._set(IDLE, None, now)

    def _set(self, state, peer, now=None):
        if state == self.state:
            self._write_state()
            return
        self.state = state
        if state == TALKING:
            now = time.monotonic() if now is None else now
            self.call["talking_since"] = now
            self.call["heard"] = now
            self.gate = Gate()
            try:
                self.player = self.make_player()
            except OSError as err:
                _LOGGER.warning("Intercom cannot play: %s", err)
                self.player = None
        elif self.player is not None:
            self.player.close()
            self.player = None
        self._write_state()
        _LOGGER.info("Intercom: %s%s", state, " with %s" % peer if peer else "")
        try:
            self.on_change(state, peer)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Intercom could not tell the rest of the device")

    def _commands(self, now):
        while self._cmds:
            word = self._cmds.popleft()
            self._do(word, now)
        path = os.path.join(self.run_dir, "command")
        try:
            with open(path, encoding="utf-8") as f:
                word = f.read().strip()
            os.unlink(path)
        except OSError:
            return
        self._do(word, now)

    def _do(self, word, now):
        if word == "call" or word.startswith("call:"):
            self._call(word[5:], now)
        elif word == "answer":
            if self.state == RINGING:
                self._answer(now)
        elif word == "end":
            if self.state != IDLE:
                self._end(now, "")
        elif word == "button":
            if self.state == RINGING:
                self._answer(now)
            elif self.state != IDLE:
                self._end(now, "")
        elif word in ("auto:on", "auto:off"):
            self._save_auto(word == "auto:on")
        else:
            _LOGGER.debug("Unknown intercom command: %s", word)

    def _save_auto(self, on):
        try:
            os.makedirs(os.path.dirname(self.config) or ".", exist_ok=True)
            tmp = self.config + ".new"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"auto_answer": bool(on)}, f)
            os.replace(tmp, self.config)
        except OSError as err:
            _LOGGER.warning("Could not save the intercom setting: %s", err)
        self._write_state()

    def _call(self, wanted, now):
        if self.state == RINGING:
            # Both pressed call within a moment of each other. It is what they wanted.
            self._answer(now)
            return
        if self.state != IDLE:
            return
        if self.busy():
            self.message = "Busy here right now."
            self._write_state()
            return
        peers = list(self.peers())
        match = [p for p in peers if not wanted or wanted in (p.get("id"), p.get("name"))]
        if self.sock is None or not match:
            self.message = "No paired Echo in reach."
            self._write_state()
            return
        peer = match[0]
        self.message = ""
        self.call = {"id": secrets.token_hex(8), "addr": tuple(peer["addr"]), "peer": peer.get("name") or "the other Echo",
                     "since": now, "heard": now, "sent": 0.0, "rang": False}
        self._set(CALLING, self.call["peer"], now)

    def _tick(self, now):
        call = self.call
        if call is None:
            return
        if self.state == CALLING:
            if now - call["since"] > RING_S:
                self._end(now, "%s did not answer." % call["peer"])
            elif now - call["sent"] >= RESEND_S:
                call["sent"] = now
                self._control(b"C", {"call": call["id"], "name": self.name_, "t": time.time()}, call["addr"])
        elif self.state == RINGING:
            if now - call["since"] > RING_S or now - call["heard"] > LOST_S:
                self._end(now, "Missed a call from %s." % call["peer"], tell=False)
        elif self.state == TALKING:
            if now - call["heard"] > LOST_S:
                self._end(now, "Lost %s." % call["peer"], tell=False)
            elif now - call["talking_since"] > MAX_TALK_S:
                self._end(now, "That call reached the 15 minute limit.")
            elif now - call.get("sent", 0.0) >= KEEPALIVE_S:
                call["sent"] = now
                self._control(b"K", {"call": call["id"]}, call["addr"])

    def _write_state(self):
        """For the settings page, and the mark that says the action button belongs to a call."""
        call = self.call
        doc = {
            "state": self.state,
            "peer": call["peer"] if call else "",
            "message": self.message,
            "auto_answer": self.auto_answer(),
            "available": self.sock is not None,
        }
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            # The mark first, on its own: the button must follow the call whatever becomes of the page's copy.
            mark = os.path.join(self.run_dir, "active")
            if self.state == IDLE:
                try:
                    os.unlink(mark)
                except FileNotFoundError:
                    pass
            else:
                with open(mark, "w", encoding="utf-8"):
                    pass
            tmp = os.path.join(self.run_dir, ".state.json.new")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            os.replace(tmp, os.path.join(self.run_dir, "state.json"))
        except OSError as err:
            _LOGGER.debug("Could not write the intercom state: %s", err)


def from_arbiter(state, arbiter, on_change):
    """The intercom for this device: arbitration's key and the peers it knows."""

    def peers():
        return [{"id": pid, "name": p.get("name", ""), "addr": (p["addr"][0], PORT)}
                for pid, p in list(arbiter.peers.items()) if p.get("addr")]

    def busy():
        satellite = state.satellite
        return bool(satellite is not None and (satellite._pipeline_active or satellite._timer_finished))

    return Intercom(state.name, lambda: arbiter.key, peers, on_change=on_change, busy=busy,
                    muted=lambda: bool(state.muted))
