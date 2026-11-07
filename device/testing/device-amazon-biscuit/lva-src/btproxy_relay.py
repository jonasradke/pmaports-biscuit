"""BISCUIT: the Bluetooth proxy, on the voice assistant's connection.

Home Assistant shows one device per ESPHome API endpoint. biscuit-btproxy used
to be its own endpoint on port 6054, so with the voice assistant installed one
Echo showed up as two devices. Now, when the voice assistant is installed, the
proxy listens on loopback only and this module joins the two: the device info
on 6053 carries the proxy's feature flags and Bluetooth address, and each Home
Assistant connection gets its own loopback connection to the proxy, over which
the Bluetooth messages pass unchanged.

The scanning and GATT code stays in biscuit-btproxy, in the core package,
because the proxy also has to work on an Echo that never installs this one.
Keeping the relay byte-for-byte also leaves the proxy's per-client behaviour
alone: when Home Assistant leaves, the loopback connection closes, so the proxy
stops scanning and releases its GATT connections as it always did.

Home Assistant reads the feature flags only when it connects. So when they
change, or the proxy restarts under an open connection, the connection is closed
and Home Assistant reconnects and reads them again, the same way changed
entities are handled in PeripheralAPIServer._reconnect_when_settled.

biscuit-btproxy looks for this file to know that the voice assistant can do
this, so it is the same version check whichever package updates first.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

STATE_FILE = "/run/biscuit-btproxy/state"
TICK_S = 2.0

FEATURE_PASSIVE_SCAN = 1 << 0
FEATURE_ACTIVE_CONNECTIONS = 1 << 1
FEATURE_RAW_ADVERTISEMENTS = 1 << 5
FEATURE_STATE_AND_MODE = 1 << 6

# ESPHome API message types. Numbered rather than named because they pass
# through without being parsed. Everything else the proxy answers (its own hello
# and device info, say) stays between it and this module.
TO_PROXY = frozenset((
    66,  # SubscribeBluetoothLEAdvertisementsRequest
    68,  # BluetoothDeviceRequest
    70,  # BluetoothGATTGetServicesRequest
    73,  # BluetoothGATTReadRequest
    75,  # BluetoothGATTWriteRequest
    76,  # BluetoothGATTReadDescriptorRequest
    77,  # BluetoothGATTWriteDescriptorRequest
    78,  # BluetoothGATTNotifyRequest
    80,  # SubscribeBluetoothConnectionsFreeRequest
    87,  # UnsubscribeBluetoothLEAdvertisementsRequest
    127,  # BluetoothScannerSetModeRequest
))
FROM_PROXY = frozenset((
    69,  # BluetoothDeviceConnectionResponse
    71,  # BluetoothGATTGetServicesResponse
    72,  # BluetoothGATTGetServicesDoneResponse
    74,  # BluetoothGATTReadResponse
    79,  # BluetoothGATTNotifyDataResponse
    81,  # BluetoothConnectionsFreeResponse
    82,  # BluetoothGATTErrorResponse
    83,  # BluetoothGATTWriteResponse
    84,  # BluetoothGATTNotifyResponse
    85,  # BluetoothDevicePairingResponse
    86,  # BluetoothDeviceUnpairingResponse
    88,  # BluetoothDeviceClearCacheResponse
    93,  # BluetoothLERawAdvertisementsResponse
    126,  # BluetoothScannerStateResponse
))

# (feature flags, Bluetooth address, port)
Features = Tuple[int, str, int]
OFF: Features = (0, "", 0)


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def encode_frame(msg_type: int, payload: bytes) -> bytes:
    return b"\x00" + _varint(len(payload)) + _varint(msg_type) + payload


def _read_varint(buf: bytearray, pos: int) -> Tuple[Optional[int], int]:
    value = shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return None, pos


def take_frames(buf: bytearray) -> Optional[List[Tuple[int, bytes]]]:
    """Remove whole frames from buf as (type, frame). None if it is out of sync."""
    frames: List[Tuple[int, bytes]] = []
    while buf:
        if buf[0] != 0:
            return None
        length, pos = _read_varint(buf, 1)
        if length is None:
            break
        msg_type, pos = _read_varint(buf, pos)
        if msg_type is None or len(buf) < pos + length:
            break
        frames.append((msg_type, bytes(buf[:pos + length])))
        del buf[:pos + length]
    return frames


def read_features(path: str = STATE_FILE) -> Optional[Features]:
    """What the proxy offers right now. None while that cannot be known yet.

    The state file is missing for a moment during a restart, and has no address
    until the proxy is up and listening, which on this device is about 40 s
    after boot. Neither is a change worth making Home Assistant reconnect for.
    """
    state: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep:
                    state[key] = value
    except OSError:
        return None
    if state.get("enabled") != "1" or state.get("relay") != "1":
        return OFF
    mac = state.get("mac")
    if not mac:
        return None
    flags = FEATURE_PASSIVE_SCAN | FEATURE_RAW_ADVERTISEMENTS | FEATURE_STATE_AND_MODE
    if state.get("active") == "1":
        flags |= FEATURE_ACTIVE_CONNECTIONS
    try:
        return (flags, mac, int(state.get("port") or 0))
    except ValueError:
        return None


class ProxyRelay(asyncio.Protocol):
    """One Home Assistant connection's link to the proxy, on its event loop."""

    def __init__(self, satellite: Any) -> None:
        self.satellite = satellite
        self.reported: Optional[Features] = None
        self.link: Optional[asyncio.Transport] = None
        self.buf = bytearray()
        self.pending: List[bytes] = []
        self.connecting = False
        self.link_lost = False
        self.closed = False
        self.tick_handle: Optional[asyncio.TimerHandle] = None
        self.held: Optional[str] = None

    def device_fields(self) -> Dict[str, Any]:
        """The DeviceInfoResponse fields that make this device the proxy."""
        self.reported = read_features() or OFF
        if self.tick_handle is None and not self.closed:
            self.tick_handle = asyncio.get_running_loop().call_later(TICK_S, self.tick)
        flags, mac, _port = self.reported
        if not flags:
            return {}
        return {"bluetooth_proxy_feature_flags": flags, "bluetooth_mac_address": mac}

    def forward(self, msg_type: int, payload: bytes) -> None:
        """A Bluetooth message from Home Assistant."""
        if self.reported is None or not self.reported[0]:
            return
        frame = encode_frame(msg_type, payload)
        if self.link is not None:
            self.link.write(frame)
            return
        self.pending.append(frame)
        self.connect()

    def connect(self) -> None:
        if self.connecting or self.closed or self.link is not None:
            return
        self.connecting = True
        asyncio.get_running_loop().create_task(self.open_link())

    async def open_link(self) -> None:
        try:
            await asyncio.get_running_loop().create_connection(
                lambda: self, "127.0.0.1", self.reported[2])
        except OSError as err:
            # Retried from tick(): the proxy may be between restarts.
            _LOGGER.debug("Bluetooth proxy not reachable yet: %s", err)
        finally:
            self.connecting = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if self.closed:
            transport.close()
            return
        self.link = transport  # type: ignore[assignment]
        pending, self.pending = self.pending, []
        for frame in pending:
            self.link.write(frame)

    def data_received(self, data: bytes) -> None:
        self.buf += data
        frames = take_frames(self.buf)
        if frames is None:
            _LOGGER.warning("Lost frame sync with the Bluetooth proxy")
            if self.link is not None:
                self.link.close()
            return
        home_assistant = getattr(self.satellite, "_transport", None)
        if home_assistant is None:
            return
        for msg_type, frame in frames:
            if msg_type in FROM_PROXY:
                home_assistant.write(frame)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self.link = None
        self.buf.clear()
        if not self.closed:
            # Advertisement subscriptions and GATT connections went with it,
            # and Home Assistant has no way to hear that except by reconnecting.
            self.link_lost = True

    def tick(self) -> None:
        self.tick_handle = None
        if self.closed:
            return
        self.tick_handle = asyncio.get_running_loop().call_later(TICK_S, self.tick)
        now = read_features()
        if now is None:
            return
        if now != self.reported or self.link_lost:
            self.refresh()
        elif self.pending:
            self.connect()

    def refresh(self) -> None:
        """Have Home Assistant reconnect, once nothing audible would be cut off."""
        satellite = self.satellite
        state = satellite.state
        api = getattr(state, "peripheral_api", None)
        # pylint: disable=protected-access
        busy = api._busy_for_reconnect(state, satellite) if api is not None else None
        if busy is not None:
            if busy != self.held:
                _LOGGER.info("HA reconnect for the Bluetooth proxy held while %s", busy)
                self.held = busy
            return
        transport = getattr(satellite, "_transport", None)
        if transport is None:
            return
        _LOGGER.info("Bluetooth proxy changed; forcing one HA reconnect")
        # Ours, not HA leaving: see satellite.connection_lost.
        satellite._forced_refresh = True
        transport.close()

    def close(self) -> None:
        self.closed = True
        self.pending = []
        if self.tick_handle is not None:
            self.tick_handle.cancel()
            self.tick_handle = None
        if self.link is not None:
            self.link.close()
            self.link = None
