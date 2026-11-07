# SPDX-License-Identifier: MIT
"""Only one Echo answers when a wake word is heard by several.

Each Echo that hears the word says how clearly it heard it, the others do too,
and the clearest takes it. Home Assistant is not involved: by the time it hears
about a wake word, the chime has played on every Echo in earshot.

Off until two Echos are paired on their settings pages. Before that nothing here
opens a socket, and a wake word goes straight to the assistant as it always has.
With no paired Echo in reach it still does: arbitration can only ever delay a
wake, by a fraction of a second, when there is someone to ask.

ONE ROUND

An Echo that hears the word sends a claim - (priority, score, id) - to its peers,
twice, because Wi-Fi drops frames. Highest wins. An Echo in a conversation or
ringing a timer has priority, so the one being talked to keeps the conversation.
A round ends at once when someone beats us, or when every peer that listens for
this word has claimed and lost. Otherwise it ends when the window closes, which
is the wait a peer's claim has actually taken here: learned from the last 50
rounds, and half a second until there are ten.

A claim that arrives before our own detection is kept for a moment, since the
same word reaches two rooms at different times and two detectors take different
times to fire. An Echo that already won answers a late claim, so the late one
stands down.

WHAT THE NETWORK CAN DO

Frames carry a truncated HMAC under a key the Echos share, and a counter that only
rises, so nobody on the network can silence an Echo by claiming with a high score,
now or by replaying one. The counter is reserved in blocks on disk, so a restart
never reuses a value. A lost file means a new id, not a lower counter.

PAIRING

"Pair" on both pages within two minutes. The two do an X25519 exchange in the open,
and one sends the key sealed under the result. Exactly one other Echo may be pairing,
otherwise it fails, and both show a code derived from the exchange. Someone in the
middle gets different codes, which is what the owner compares.
"""
import base64
import collections
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import select
import socket
import threading
import time

try:
    from . import biscuit_crypto
except ImportError:  # run on its own by a test
    import biscuit_crypto

_LOGGER = logging.getLogger("biscuit-arbitration")

PORT = 6055
MAGIC = b"BARB1"
TAG = 16
BEACON_S = 15.0
PEER_TTL_S = 50.0
RESEND_S = 0.06
LOOKBACK_S = 1.5
WINDOW_DEFAULT_S = 0.5
WINDOW_MIN_S = 0.15
WINDOW_MAX_S = 1.0
WINDOW_SAMPLES = 10
PAIR_S = 120.0
PAIR_HELLO_S = 2.0
PAIR_KEY_S = 1.0
COUNTER_BLOCK = 1000

CONFIG_FILE = os.environ.get("BISCUIT_ARBITRATION_CONFIG", "/opt/persist/arbitration.json")
COUNTER_FILE = os.environ.get("BISCUIT_ARBITRATION_COUNTER", "/var/lib/biscuit/arbitration-counter.json")
RUN_DIR = os.environ.get("BISCUIT_ARBITRATION_RUN", "/run/biscuit-arbitration")


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(json.dumps(data))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, encoding="ascii") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class _Counter:
    """This Echo's id and its frames' rising number. See the module's docstring."""

    def __init__(self, path):
        self.path = path
        stored = _read_json(path)
        self.id = stored.get("id")
        self.next = stored.get("next")
        if not isinstance(self.id, str) or not isinstance(self.next, int):
            self.id, self.next = secrets.token_hex(8), 1
        self.limit = self.next
        self._reserve()

    def _reserve(self):
        self.limit = self.next + COUNTER_BLOCK
        try:
            _write_private(self.path, {"id": self.id, "next": self.limit})
        except OSError as err:
            _LOGGER.warning("could not save the frame counter: %s", err)

    def take(self):
        if self.next >= self.limit:
            self._reserve()
        value = self.next
        self.next += 1
        return value


class WakeScore:
    """How clearly the last second was heard: its loudest quarter over the room's
    noise floor, in dB. Called from the microphone thread, ~100 times a second."""

    def __init__(self):
        self.recent = collections.deque()
        self.floor = None

    def feed(self, chunk, now=None):
        import numpy as np

        now = time.monotonic() if now is None else now
        samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
        power = float(np.mean(samples * samples)) if samples.size else 0.0
        level = 10.0 * math.log10(power + 1.0)
        if self.floor is None:
            self.floor = level
        # Down fast and up slowly: speech must not raise what counts as silence.
        self.floor += (level - self.floor) * (0.05 if level < self.floor else 0.0005)
        self.recent.append((now, level))
        while self.recent and now - self.recent[0][0] > 1.0:
            self.recent.popleft()

    def value(self):
        if not self.recent:
            return 0.0
        levels = sorted((lv for _t, lv in self.recent), reverse=True)
        top = levels[:max(1, len(levels) // 4)]
        return round(sum(top) / len(top) - (self.floor or 0.0), 2)


class _Round:
    def __init__(self, word, key, started, deadline, on_win):
        self.word = word
        self.key = key
        self.started = started
        self.deadline = deadline
        self.on_win = on_win
        self.claimed = {}
        # Who has spoken during this round, as opposed to a claim kept from just
        # before it: that peer may have answered by now, and only it can say so.
        self.heard = set()
        self.sends = 0
        self.next_send = started


class Arbiter(threading.Thread):
    daemon = True

    def __init__(self, name, words, config=CONFIG_FILE, counter=COUNTER_FILE, run_dir=RUN_DIR,
                 port=PORT, targets=None, bind="0.0.0.0"):
        super().__init__(name="arbitration")
        self.device_name = name
        self.words = words
        self.config_path = config
        self.run_dir = run_dir
        self.port = port
        self.bind_to = bind
        self.targets = targets or [("255.255.255.255", port)]
        self.counter = _Counter(counter)
        self.id = self.counter.id
        cfg = _read_json(config)
        self.key = None
        try:
            if cfg.get("key"):
                self.key = base64.b64decode(cfg["key"], validate=True)
        except ValueError:
            _LOGGER.error("the arbitration key is unusable, ignoring it")
        self.enabled = cfg.get("enabled", True) is not False
        self.lock = threading.Lock()
        self.sock = None
        self.wake_r, self.wake_w = socket.socketpair()
        self.wake_r.setblocking(False)
        self.peers = {}
        self.seen = {}
        self.recent = {}
        self.round = None
        self.won = None
        self.ended = None
        self.samples = collections.deque(maxlen=50)
        self.next_beacon = 0.0
        self.pair = None
        self.pair_note = {"state": "idle"}
        self.last = None
        self.stopping = False
        self._state_at = 0.0
        self._state_text = ""

    # ---- what the microphone thread uses. Nothing here touches disk or network.

    def contested(self, word):
        """Is there anyone to ask about this word? False: wake right away."""
        if self.sock is None or self.key is None or not self.enabled:
            return False
        now = time.monotonic()
        with self.lock:
            return bool(self._rivals(word, now))

    def claim(self, word, score, priority, on_win):
        now = time.monotonic()
        with self.lock:
            if self.round is not None:
                return
            key = (int(priority), float(score), self.id)
            self.round = _Round(word, key, now, now + self.window(), on_win)
            for (peer, seen_word), (other, at) in list(self.recent.items()):
                if seen_word != word or now - at > LOOKBACK_S:
                    continue
                self.samples.append(now - at)
                self.round.claimed[peer] = other
        self.wake_w.send(b"x")

    # ---- the thread

    def stop(self):
        self.stopping = True
        try:
            self.wake_w.send(b"x")
        except OSError:
            pass

    def window(self):
        if len(self.samples) < WINDOW_SAMPLES:
            return WINDOW_DEFAULT_S
        ordered = sorted(self.samples)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return max(WINDOW_MIN_S, min(WINDOW_MAX_S, p95 + 0.05))

    def run(self):
        while not self.stopping:
            try:
                self._commands()
                self._socket_as_needed()
                now = time.monotonic()
                wait = self._next_due(now)
                ready = [self.wake_r] + ([self.sock] if self.sock else [])
                readable, _w, _x = select.select(ready, [], [], wait)
                if self.wake_r in readable:
                    try:
                        self.wake_r.recv(64)
                    except OSError:
                        pass
                if self.sock in readable:
                    self._receive()
                self._tick(time.monotonic())
                self._publish(time.monotonic())
            except Exception:  # noqa: BLE001 - a bug here must not stop the assistant's wakes
                _LOGGER.exception("arbitration")
                with self.lock:
                    stuck = self.round
                    self.round = None
                if stuck:
                    self._fire(stuck.on_win)
                time.sleep(1.0)
        if self.sock:
            self.sock.close()

    def _socket_as_needed(self):
        wanted = self.pair is not None or (self.key is not None and self.enabled)
        if wanted and self.sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_to, self.port))
            sock.setblocking(False)
            self.sock = sock
            self.next_beacon = 0.0
        elif not wanted and self.sock is not None:
            self.sock.close()
            self.sock = None
            with self.lock:
                self.peers.clear()

    def _next_due(self, now):
        due = [now + 1.0]
        if self.sock is not None:
            due.append(self.next_beacon)
        if self.pair is not None:
            due.append(self.pair["next"])
        rnd = self.round
        if rnd is not None:
            due.append(rnd.deadline)
            if rnd.sends < 2:
                due.append(rnd.next_send)
        return max(0.0, min(due) - now)

    # ---- frames

    def _frame(self, kind, obj):
        body = MAGIC + kind + json.dumps(obj, separators=(",", ":")).encode()
        return body + hmac.new(self.key, body, hashlib.sha256).digest()[:TAG]

    def _send(self, data, to=None):
        if self.sock is None:
            return
        for target in ([to] if to else self._addresses()):
            try:
                self.sock.sendto(data, target)
            except OSError as err:
                _LOGGER.debug("send to %s: %s", target, err)

    def _addresses(self):
        # Unicast too: Wi-Fi retries it, and does not retry a broadcast.
        out = list(self.targets)
        with self.lock:
            out.extend(p["addr"] for p in self.peers.values() if p["addr"] not in out)
        return out

    def _authed(self, kind, obj, to=None):
        if self.key is None:
            return
        self._send(self._frame(kind, dict(obj, id=self.id, ctr=self.counter.take())), to)

    def _receive(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if len(data) < len(MAGIC) + 1 or not data.startswith(MAGIC):
                continue
            kind = data[len(MAGIC):len(MAGIC) + 1]
            if kind in (b"P", b"K"):
                self._pairing_frame(kind, data[len(MAGIC) + 1:], addr)
            elif self.key is not None and (self.enabled or self.pair is not None):
                self._authed_frame(kind, data, addr)

    def _authed_frame(self, kind, data, addr):
        body, tag = data[:-TAG], data[-TAG:]
        if not hmac.compare_digest(hmac.new(self.key, body, hashlib.sha256).digest()[:TAG], tag):
            return
        try:
            msg = json.loads(body[len(MAGIC) + 1:])
            peer, ctr = msg["id"], int(msg["ctr"])
        except (ValueError, KeyError, TypeError):
            return
        if peer == self.id or ctr <= self.seen.get(peer, 0):
            return
        self.seen[peer] = ctr
        now = time.monotonic()
        answer = None
        with self.lock:
            known = self.peers.setdefault(peer, {"name": "", "words": [], "on": True})
            known.update(addr=addr, at=now)
            if kind == b"B":
                known.update(name=str(msg.get("name", ""))[:64],
                             words=list(msg.get("words", []))[:8], on=bool(msg.get("on", True)))
            elif kind == b"C":
                answer = self._on_claim(peer, msg, now)
            elif kind == b"W":
                rnd = self.round
                if rnd is not None and rnd.word == msg.get("word"):
                    self.samples.append(now - rnd.started)
                    self._lost(now, "%s was already answering" % known["name"])
        if self.pair is not None and self.pair.get("other") == peer and self.pair.get("sender"):
            self._pair_done()
        if answer:
            self._authed(*answer, to=addr)

    def _on_claim(self, peer, msg, now):
        word = str(msg.get("word", ""))
        other = (int(msg.get("prio", 0)), float(msg.get("score", 0)), peer)
        rnd = self.round
        if rnd is not None and rnd.word == word:
            first = peer not in rnd.heard
            if first:
                self.samples.append(now - rnd.started)
            rnd.heard.add(peer)
            rnd.claimed[peer] = other
            return (b"C", self._claim_body(rnd)) if first else None
        # A claim right after a round of ours belongs to that round. Kept, it
        # would count against whatever is said next.
        if not (self.ended and self.ended[0] == word and now - self.ended[1] < LOOKBACK_S):
            self.recent[(peer, word)] = (other, now)
        if self.won and self.won[0] == word and now - self.won[1] < LOOKBACK_S:
            return (b"W", {"word": word})
        return None

    @staticmethod
    def _claim_body(rnd):
        return {"word": rnd.word, "prio": rnd.key[0], "score": rnd.key[1]}

    # ---- rounds

    def _rivals(self, word, now):
        return [pid for pid, p in self.peers.items()
                if now - p["at"] < PEER_TTL_S and p.get("on") and word in p.get("words", ())]

    def _lost(self, now, why):
        rnd, self.round = self.round, None
        self.ended = (rnd.word, now)
        self.last = {"word": rnd.word, "won": False, "why": why, "at": time.time(),
                     "score": rnd.key[1]}
        _LOGGER.info("wake word '%s': another Echo takes it (%s)", rnd.word, why)

    def _won(self, now, why):
        rnd, self.round = self.round, None
        self.won = self.ended = (rnd.word, now)
        self.last = {"word": rnd.word, "won": True, "why": why, "at": time.time(),
                     "score": rnd.key[1]}
        _LOGGER.debug("wake word '%s': ours (%s)", rnd.word, why)
        return rnd.on_win

    def _fire(self, fn):
        threading.Thread(target=fn, name="arbitration-win", daemon=True).start()

    def _tick(self, now):
        start = None
        send = None
        with self.lock:
            rnd = self.round
            if rnd is not None:
                # First, however this round ends. A winner that decided before
                # saying so would leave the others waiting out their windows,
                # and winning as well.
                if rnd.sends < 2 and now >= rnd.next_send:
                    rnd.sends += 1
                    rnd.next_send = now + RESEND_S
                    send = self._claim_body(rnd)
                beaten = [c for c in rnd.claimed.values() if c > rnd.key]
                rivals = self._rivals(rnd.word, now)
                if beaten:
                    self._lost(now, "score %.1f against %.1f" % (rnd.key[1], beaten[0][1]))
                elif now >= rnd.deadline:
                    start = self._won(now, "window closed")
                elif rivals and all(r in rnd.heard for r in rivals):
                    start = self._won(now, "everyone claimed")
            for key, (_other, at) in list(self.recent.items()):
                if now - at > LOOKBACK_S:
                    del self.recent[key]
            for pid, p in list(self.peers.items()):
                if now - p["at"] > 2 * PEER_TTL_S:
                    del self.peers[pid]
        if send:
            self._authed(b"C", send)
        if start:
            self._fire(start)
        if self.sock is not None and self.key is not None and self.enabled and now >= self.next_beacon:
            self.next_beacon = now + BEACON_S
            self._authed(b"B", {"name": self.device_name, "on": True, "words": list(self.words())})
        self._pair_tick(now)

    # ---- pairing

    def _commands(self):
        path = os.path.join(self.run_dir, "command")
        try:
            with open(path, encoding="ascii") as f:
                command = f.read().strip()
            os.unlink(path)
        except OSError:
            return
        if command == "pair":
            self._pair_begin()
        elif command == "cancel":
            self._pair_end("idle")
        elif command == "forget":
            self._pair_end("idle")
            self.key = None
            self._save()
            with self.lock:
                self.peers.clear()
        elif command in ("on", "off"):
            self.enabled = command == "on"
            self._save()
        self._state_at = 0.0

    def _save(self):
        try:
            if self.key is None:
                try:
                    os.unlink(self.config_path)
                except FileNotFoundError:
                    pass
                self.enabled = True
            else:
                _write_private(self.config_path, {"key": base64.b64encode(self.key).decode(),
                                                  "enabled": self.enabled})
        except OSError as err:
            _LOGGER.error("could not save the arbitration settings: %s", err)

    def _pair_begin(self):
        priv, pub = biscuit_crypto._keypair()
        self.pair = {"priv": priv, "pub": pub, "deadline": time.monotonic() + PAIR_S, "next": 0.0,
                     "other": None, "sender": False, "made_key": False, "seal": None, "code": None,
                     "had_key": self.key is not None}
        self.pair_note = {"state": "waiting"}

    def _pair_end(self, state, **more):
        self.pair = None
        self.pair_note = dict(state=state, **more)

    def _pair_done(self):
        code, name = self.pair["code"], self.pair.get("name", "")
        self._pair_end("paired", code=code, name=name)
        self.enabled = True
        self._save()
        self.next_beacon = 0.0

    def _pair_fail(self, why):
        if self.pair["made_key"]:
            self.key = None
        self._pair_end("failed", message=why)

    def _pair_tick(self, now):
        pair = self.pair
        if pair is None:
            return
        if now >= pair["deadline"]:
            self._pair_fail("No other Echo answered. Press Pair on both within two minutes.")
            return
        if now < pair["next"]:
            return
        pair["next"] = now + (PAIR_KEY_S if pair["seal"] else PAIR_HELLO_S)
        # The hello goes on after the key: the other side cannot open it until
        # it has heard our half, and it may have missed every hello so far.
        self._pair_send(MAGIC + b"P" + json.dumps(
            {"id": self.id, "name": self.device_name, "has_key": pair["had_key"],
             "pub": base64.b64encode(pair["pub"]).decode()}).encode())
        if pair["seal"]:
            self._pair_send(MAGIC + b"K" + pair["seal"])

    def _pair_send(self, data):
        self._send(data)
        addr = self.pair.get("addr")
        if addr:
            self._send(data, to=addr)

    def _pairing_frame(self, kind, raw, addr):
        pair = self.pair
        if pair is None:
            return
        try:
            msg = json.loads(raw)
            if msg.get("id") == self.id:
                return
            if kind == b"P":
                self._pair_hello(pair, msg, addr)
            elif msg.get("to") == self.id and msg.get("from") == pair["other"]:
                nonce = base64.b64decode(msg["nonce"])
                key = biscuit_crypto._open(pair["seal_key"], nonce, b"",
                                          base64.b64decode(msg["sealed"]))
                self.key = key
                self.enabled = True
                self._pair_done()
        except (ValueError, KeyError, TypeError) as err:
            _LOGGER.debug("pairing frame ignored: %s", err)
        except Exception as err:  # noqa: BLE001 - a forged frame's bad tag, by either backend
            _LOGGER.warning("pairing frame refused: %s", err)

    def _pair_hello(self, pair, msg, addr):
        other = msg["id"]
        if pair["other"] == other:
            return
        if pair["other"] is not None:
            self._pair_fail("More than one other Echo is pairing. Pair two at a time.")
            return
        remote = base64.b64decode(msg["pub"])
        if len(remote) != 32:
            raise ValueError("bad public key")
        shared = biscuit_crypto._dh(pair["priv"], remote)
        pubs = b"".join(sorted((pair["pub"], remote)))
        seal_key, code_key = biscuit_crypto._hkdf(pubs, shared, 2)
        pair.update(other=other, addr=addr, name=str(msg.get("name", ""))[:64], seal_key=seal_key,
                    code="%04d" % (int.from_bytes(hmac.new(code_key, b"code", hashlib.sha256).digest()[:4], "big") % 10000))
        self.pair_note = {"state": "exchanging", "code": pair["code"], "name": pair["name"]}
        has_key = pair["had_key"]
        ours, theirs = (has_key, self.id), (bool(msg.get("has_key")), other)
        pair["next"] = 0.0
        if ours > theirs:
            pair["sender"] = True
            if not has_key:
                self.key = secrets.token_bytes(32)
                pair["made_key"] = True
            nonce = secrets.token_bytes(12)
            pair["seal"] = json.dumps(
                {"id": self.id, "from": self.id, "to": other,
                 "nonce": base64.b64encode(nonce).decode(),
                 "sealed": base64.b64encode(biscuit_crypto._seal(seal_key, nonce, b"", self.key)).decode()}).encode()
            self.next_beacon = 0.0

    # ---- what the settings page reads

    def _publish(self, now):
        if now - self._state_at < 1.0:
            return
        self._state_at = now
        note = dict(self.pair_note)
        if self.pair is not None:
            note["seconds_left"] = int(self.pair["deadline"] - now)
        with self.lock:
            peers = [{"name": p.get("name") or pid[:8], "seconds_ago": int(now - p["at"])}
                     for pid, p in self.peers.items() if now - p["at"] < PEER_TTL_S and p.get("on")]
        text = json.dumps({"paired": self.key is not None, "enabled": self.enabled, "peers": peers,
                           "pairing": note, "window_ms": int(self.window() * 1000), "last": self.last})
        if text == self._state_text:
            return
        self._state_text = text
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            tmp = os.path.join(self.run_dir, "state.tmp")
            with open(tmp, "w", encoding="ascii") as f:
                f.write(text)
            os.replace(tmp, os.path.join(self.run_dir, "state.json"))
        except OSError as err:
            _LOGGER.debug("could not publish the state: %s", err)
