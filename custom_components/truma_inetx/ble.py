"""BLE transport for Truma iNet X over Home Assistant's Bluetooth stack.

This is the bleak/HA-bluetooth port of the project's original dbus-fast
transport. The framing/CBOR protocol (``truma/protocol.py``) is reused
unchanged; only the connection + GATT I/O layer differs.

Transport FSM (per ``send``):
  1. Write ``[0x01, len_lo, len_hi]`` (InitDataTransfer) to CMD (with response).
  2. Wait for a Ready notification (0x81) on CMD.
  3. Write the packet to DATA_W (without response).
  4. Wait for a DataAck notification (0xF0) on CMD.

Incoming DATA_R notifications are auto-ACKed (0xF001) and parsed into V3 frames
dispatched to registered callbacks. A short (<=4 byte) MsgAck (0x83) is
auto-confirmed with 0x0300.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .truma.const import (
    CHAR_CMD,
    CHAR_DATA_R,
    CHAR_DATA_W,
    DEV_APP_DEFAULT,
    TRANSPORT_ACK,
    TRANSPORT_CONFIRM,
    TRANSPORT_MSG_ACK,
    TRANSPORT_INIT,
)
from .truma.protocol import parse_v3_frame

_LOGGER = logging.getLogger(__name__)

_READY_TIMEOUT = 3.0
_ACK_TIMEOUT = 3.0


class TrumaBleClient:
    """Manage the BLE connection and transport FSM to a Truma iNet X panel."""

    def __init__(self, identity: dict) -> None:
        """Initialize with the app identity (muid/uuid/username)."""
        self._identity = identity
        self._client: BleakClientWithServiceCache | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._data_callbacks: list[Callable[[dict], None]] = []
        self._send_lock = asyncio.Lock()
        self._transport_event: asyncio.Event | None = None
        self._transport_ack: bytes | None = None
        self.assigned_addr = DEV_APP_DEFAULT

    def on_data(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for decoded V3 frames."""
        self._data_callbacks.append(callback)

    @property
    def connected(self) -> bool:
        """Whether the BLE link is up."""
        return self._client is not None and self._client.is_connected

    async def connect(
        self,
        ble_device: BLEDevice,
        disconnected_callback: Callable[[BleakClientWithServiceCache], None]
        | None = None,
    ) -> None:
        """Establish the connection (via HA's stack) and subscribe."""
        self._loop = asyncio.get_running_loop()
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            ble_device.address,
            disconnected_callback=disconnected_callback,
        )
        await self._subscribe()

    async def connect_raw(
        self,
        address: str,
        adapter: str,
        disconnected_callback: Callable[[BleakClient], None] | None = None,
    ) -> None:
        """Connect on a dedicated adapter via raw bleak, bypassing HA's stack.

        Used when a weak dongle (e.g. a CSR8510 clone) cannot scan and connect
        concurrently: the dongle is dedicated to connecting here while another
        adapter does HA's scanning, so there is no scan/connect contention.

        Uses the BlueZ backend client directly rather than ``bleak.BleakClient``:
        inside HA the latter is monkeypatched by habluetooth and rerouted
        through HA's manager (ignoring ``adapter=``), defeating the adapter pin.
        """
        self._loop = asyncio.get_running_loop()
        client = BleakClientBlueZDBus(
            address,
            bluez={"adapter": adapter},
            disconnected_callback=disconnected_callback,
            timeout=20.0,
        )
        await client.connect()
        self._client = client
        await self._subscribe()

    async def _subscribe(self) -> None:
        """Enable notifications on CMD (transport acks) and DATA_R (data)."""
        assert self._client is not None
        await self._client.start_notify(CHAR_CMD, self._notify_cmd)
        await self._client.start_notify(CHAR_DATA_R, self._notify_data)
        _LOGGER.debug("Truma BLE connected and subscribed")

    async def disconnect(self) -> None:
        """Disconnect the BLE link."""
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001 - best effort
                _LOGGER.debug("Truma BLE disconnect error: %s", exc)

    # -- notifications ---------------------------------------------------

    def _notify_cmd(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self._handle_notification(CHAR_CMD, bytes(data))

    def _notify_data(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self._handle_notification(CHAR_DATA_R, bytes(data))

    def _handle_notification(self, char_uuid: str, data: bytes) -> None:
        if len(data) <= 4:
            # MsgAck (0x83) must be auto-confirmed with 0x0300.
            if data and data[0] == TRANSPORT_MSG_ACK:
                self._fire_write(CHAR_CMD, bytes([TRANSPORT_CONFIRM, 0x00]))
            self._transport_ack = data
            if self._transport_event is not None:
                self._transport_event.set()
            return

        if char_uuid == CHAR_CMD:
            if self._transport_event is not None:
                self._transport_event.set()
            return

        # DATA_R: incoming V3 data frame — auto-ACK, parse, dispatch.
        self._fire_write(CHAR_CMD, bytes([TRANSPORT_ACK, 0x01]))
        frame = parse_v3_frame(data)
        if frame is not None:
            for callback in self._data_callbacks:
                try:
                    callback(frame)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Truma data callback error")

    def _fire_write(self, char_uuid: str, data: bytes) -> None:
        """Schedule a fire-and-forget GATT write from a notification handler."""
        if self._loop is not None:
            self._loop.create_task(self._write(char_uuid, data))

    # -- sending ---------------------------------------------------------

    async def _write(self, char_uuid: str, data: bytes) -> None:
        if self._client is None:
            return
        # CMD uses Write Request (with response); DATA_W uses Write Command.
        response = char_uuid == CHAR_CMD
        await self._client.write_gatt_char(char_uuid, data, response=response)

    async def send(self, packet: bytes) -> bool:
        """Send a V3 packet through the transport FSM. Returns True on DataAck."""
        async with self._send_lock:
            return await self._send_locked(packet)

    async def _send_locked(self, packet: bytes) -> bool:
        success = False
        try:
            self._transport_event = asyncio.Event()
            self._transport_ack = None

            # 1. InitDataTransfer announce.
            announce = bytes(
                [TRANSPORT_INIT, len(packet) & 0xFF, (len(packet) >> 8) & 0xFF]
            )
            await self._write(CHAR_CMD, announce)

            # 2. Wait for Ready.
            try:
                await asyncio.wait_for(self._transport_event.wait(), _READY_TIMEOUT)
            except TimeoutError:
                _LOGGER.debug("Truma transport: timeout waiting for Ready")
            self._transport_event.clear()

            # 3. Send payload on DATA_W.
            await self._write(CHAR_DATA_W, packet)

            # 4. Wait for DataAck.
            try:
                await asyncio.wait_for(self._transport_event.wait(), _ACK_TIMEOUT)
                if self._transport_ack and self._transport_ack[0] == TRANSPORT_ACK:
                    success = True
            except TimeoutError:
                _LOGGER.debug("Truma transport: timeout waiting for DataAck")

            # 5. Let any async MsgAck settle.
            await asyncio.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Truma transport error: %s", exc)
        finally:
            self._transport_event = None
            self._transport_ack = None
        return success
