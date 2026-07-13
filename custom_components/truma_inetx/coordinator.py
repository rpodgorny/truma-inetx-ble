"""Coordinator for the Truma iNet X (BLE) integration.

Owns the shared :class:`TrumaState` and a background session that connects to
the panel over HA's Bluetooth stack, runs the register/subscribe/identity/
param-discovery startup, and feeds notifications into the state. Reconnects
with backoff on drop.
"""

from __future__ import annotations

import asyncio
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .ble import TrumaBleClient
from .bt import async_resolve_proxy_device
from .const import DOMAIN, LOGGER
from .truma.const import (
    CTRL_MBP,
    DEV_APP_DEFAULT,
    DEV_HEATER,
    DEV_PANEL,
    MBP_PARAM_DISC,
    TOPIC_BATCHES,
)
from .truma.protocol import (
    build_identity_frames,
    build_register_frame,
    build_subscribe_frame,
    build_v3_frame,
    build_write_frame,
)
from .truma.state import TrumaState

type TrumaConfigEntry = ConfigEntry[TrumaCoordinator]

# Reconnect backoff. Start quick (a healthy link that just dropped should come
# back fast) and grow exponentially to a cap when the panel stays unreachable,
# so an out-of-range/unbonded device does not hammer — and monopolize — the
# shared Bluetooth adapter. The delay resets after any session that connected.
_RECONNECT_DELAY_BASE = 15  # seconds
_RECONNECT_DELAY_MAX = 300  # seconds (5 min cap)
_STORAGE_VERSION = 1


class TrumaCoordinator(DataUpdateCoordinator[TrumaState]):
    """Hold Truma state and run the live BLE session."""

    config_entry: TrumaConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: TrumaConfigEntry, address: str
    ) -> None:
        """Initialize the coordinator (push model, no polling interval)."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {address}",
            update_interval=None,
        )
        self.address = address
        # Stable identity for entity/device unique IDs. The BLE address rotates
        # (resolvable private address), so it must NOT be used as identity.
        self.unique_id = entry.unique_id or address
        self._state = TrumaState()
        self._client: TrumaBleClient | None = None
        self._identity: dict | None = None
        self._store: Store = Store(hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self._stop = False
        # Set on stop to interrupt the reconnect wait immediately (so unload is
        # not blocked for up to the full backoff delay).
        self._stop_event = asyncio.Event()

    async def _async_update_data(self) -> TrumaState:
        """Return the current shared state (updated by BLE notifications)."""
        return self._state

    async def async_start(self) -> None:
        """Load identity and launch the background BLE session."""
        self._identity = await self._load_identity()
        self.config_entry.async_create_background_task(
            self.hass, self._run(), name=f"{DOMAIN} session {self.address}"
        )

    async def async_stop(self) -> None:
        """Stop the session and disconnect."""
        self._stop = True
        self._stop_event.set()
        if self._client is not None:
            await self._client.disconnect()

    async def _load_identity(self) -> dict:
        """Load the persisted app identity, or create and store a new one."""
        data = await self._store.async_load()
        if not data:
            data = {
                "muid": str(uuid.uuid4()).upper(),
                "uuid": str(uuid.uuid4()).lower(),
                "username": "Home Assistant",
            }
            await self._store.async_save(data)
        return data

    async def _run(self) -> None:
        """Maintain the BLE session, reconnecting with exponential backoff."""
        delay = _RECONNECT_DELAY_BASE
        while not self._stop:
            connected = False
            try:
                connected = await self._connect_and_run()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Truma session ended: %s", exc)
            self._mark_disconnected()
            if self._stop:
                break
            # A session that actually connected resets the backoff (a healthy
            # link that just dropped should return fast); a failed attempt grows
            # it after the wait, so a persistently unreachable panel backs off
            # the shared adapter instead of hammering it.
            if connected:
                delay = _RECONNECT_DELAY_BASE
            LOGGER.debug("Truma %s reconnecting in %ss", self.unique_id, delay)
            await self._wait_before_retry(delay)
            if not connected:
                delay = min(delay * 2, _RECONNECT_DELAY_MAX)

    async def _wait_before_retry(self, delay: float) -> None:
        """Sleep ``delay`` seconds, but wake immediately on stop."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _connect_and_run(self) -> bool:
        """Connect, run startup, then hold until the link drops.

        Returns ``True`` once the connection was established (so the caller
        resets the backoff). Raises if the connection could not be established.
        """
        assert self._identity is not None
        client = TrumaBleClient(self._identity)
        client.on_data(self._on_frame)

        ble_device = async_resolve_proxy_device(self.hass, self.unique_id)
        if ble_device is None:
            raise HomeAssistantError(
                f"Truma {self.unique_id} not currently advertising"
            )
        await client.connect(ble_device)
        self._client = client

        await self._run_startup(client)

        self._state.connected = True
        self._state.assigned_addr = client.assigned_addr
        self.async_set_updated_data(self._state)
        LOGGER.info("Truma %s connected and subscribed", self.unique_id)

        while not self._stop and client.connected:
            await asyncio.sleep(1)
        return True

    async def _run_startup(self, client: TrumaBleClient) -> None:
        """Register, subscribe to all topics, send identity, discover params."""
        # 1. Register and wait for an assigned address.
        await client.send(build_register_frame(client.assigned_addr))
        for _ in range(20):
            await asyncio.sleep(1)
            if client.assigned_addr != DEV_APP_DEFAULT:
                break

        # 2. Subscribe to all topic batches.
        for batch in TOPIC_BATCHES:
            await client.send(build_subscribe_frame(client.assigned_addr, batch))
            await asyncio.sleep(0.5)
        await asyncio.sleep(3)

        # 3. Send the identity sequence.
        for frame in build_identity_frames(client.assigned_addr, self._identity):
            await client.send(frame)
            await asyncio.sleep(0.5)

        # 4. Request current values from heater and panel.
        for dev_addr in (DEV_HEATER, DEV_PANEL):
            await client.send(
                build_v3_frame(
                    dev_addr, client.assigned_addr, CTRL_MBP, MBP_PARAM_DISC, 0, b""
                )
            )
            await asyncio.sleep(3)

    @callback
    def _on_frame(self, parsed: dict) -> None:
        """Handle a decoded V3 frame and update state."""
        control = parsed.get("control_raw")
        sub_type = parsed.get("sub_type")
        cbor = parsed.get("cbor")
        if not isinstance(cbor, dict):
            return

        # Registration response -> assigned address.
        if control == 0x01 and sub_type == 0x02:
            addr = cbor.get("addr")
            if addr and self._client is not None:
                self._client.assigned_addr = addr
                self._state.assigned_addr = addr
            return

        # Info message -> single parameter update.
        if control == 0x03 and sub_type == 0x00:
            tn, pn, v = cbor.get("tn"), cbor.get("pn"), cbor.get("v")
            if tn and pn and v is not None:
                self._state.update(tn, pn, v)
                self.async_set_updated_data(self._state)
            return

        # Parameter-discovery response -> nested current values.
        if control == 0x03 and sub_type == 0x84:
            for topic in cbor.get("topics", []) or []:
                if not isinstance(topic, dict):
                    continue
                tn = topic.get("tn", "")
                for param in topic.get("parameters", []) or []:
                    if not isinstance(param, dict):
                        continue
                    pn, v = param.get("pn"), param.get("v")
                    if tn and pn and v is not None:
                        self._state.update(tn, pn, v)
            self.async_set_updated_data(self._state)
            return

    @callback
    def _mark_disconnected(self) -> None:
        """Flag the link as down and notify entities."""
        if self._state.connected:
            self._state.connected = False
            self.async_set_updated_data(self._state)

    async def async_write(self, topic: str, param: str, value: int) -> None:
        """Validate and send a parameter write to the panel/heater.

        The panel confirms by pushing an updated value, which flows back through
        the normal notification path and updates the entity.
        """
        ok, msg = TrumaState.validate_command(topic, param, value)
        if not ok:
            raise HomeAssistantError(f"Invalid Truma command: {msg}")

        client = self._client
        if client is None or not client.connected:
            raise HomeAssistantError("Truma panel is not connected")

        dest = TrumaState.get_command_dest(topic)
        frame = build_write_frame(client.assigned_addr, dest, topic, param, value)
        LOGGER.debug("Truma write %s.%s = %s -> 0x%04X", topic, param, value, dest)
        if not await client.send(frame):
            raise HomeAssistantError(
                f"Truma did not acknowledge write {topic}.{param}={value}"
            )
