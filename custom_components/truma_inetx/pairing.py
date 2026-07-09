"""Bonding (pairing) for the Truma iNet X panel.

The panel uses **Just Works** pairing (no passkey shown) and only bonds while a
client is *actively* attempting to pair AND the panel is in add-device mode. It
also silently rejects new bonds when its stored device list is full, so the user
must clear that list first if pairing fails repeatedly.

``ensure_bonded()`` hides that behind one call. Today it drives **BlueZ over
D-Bus** (a faithful port of ``scripts/ha_pair.py``: register a NoInputNoOutput
auto-accept agent, then busy-loop ``Device1.Pair()`` until the device reports
``Paired``). When connecting through an ESP32 Bluetooth proxy the bond lives on
the proxy instead — that implementation slots in behind this same function, so
nothing else in the integration needs to know which transport bonded the panel.

NOTE: the BlueZ path here is not yet validated end-to-end against the panel from
inside HA (Stage 4d) — it is the porting of a proven standalone script.
"""

from __future__ import annotations

import asyncio
import time

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from .const import LOGGER

BLUEZ = "org.bluez"
_AGENT_PATH = "/truma_inetx/agent"
_PAIR_CALL_TIMEOUT = 8.0
_POLL_INTERVAL = 1.0


class _JustWorksAgent(ServiceInterface):
    """A BlueZ agent that auto-accepts everything (Just Works, no passkey)."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):  # noqa: N802
        """Agent released by BlueZ."""

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802,F821
        """Return a dummy PIN (not used by Just Works)."""
        return "0000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802,F821
        """No display."""

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802,F821
        """Return a dummy passkey (not used by Just Works)."""
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802,F821
        """No display."""

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802,F821
        """Auto-confirm (no exception raised == accept)."""

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
        """Auto-authorize."""

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
        """Auto-authorize the service."""

    @method()
    def Cancel(self):  # noqa: N802
        """Pairing cancelled by BlueZ."""


async def _get_interface(bus: MessageBus, path: str, interface: str):
    """Return a proxy interface at ``path``."""
    introspection = await bus.introspect(BLUEZ, path)
    obj = bus.get_proxy_object(BLUEZ, path, introspection)
    return obj.get_interface(interface)


def _find_device(objects: dict, *, name: str, address: str) -> str | None:
    """Return the BlueZ device path matching ``name`` (or ``address``)."""
    address = address.upper()
    name_lc = name.lower()
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        dev_addr = dev.get("Address")
        dev_name = dev.get("Name")
        dev_addr_v = dev_addr.value.upper() if dev_addr else ""
        dev_name_v = str(dev_name.value) if dev_name else ""
        if dev_addr_v == address or (name_lc and name_lc in dev_name_v.lower()):
            return path
    return None


def _is_paired(objects: dict, path: str) -> bool:
    """Whether the device at ``path`` reports ``Paired``."""
    dev = objects.get(path, {}).get("org.bluez.Device1", {})
    paired = dev.get("Paired")
    return bool(paired and paired.value)


async def ensure_bonded(
    name: str, address: str, *, timeout: float = 60.0
) -> bool:
    """Ensure the Truma panel is BLE-bonded. Return ``True`` if bonded.

    Registers a temporary Just Works agent and busy-loops ``Device1.Pair()``
    until the panel reports ``Paired`` or ``timeout`` elapses. The caller must
    have prompted the user to put the panel into add-device mode (and to clear
    its device list if it is full).

    BlueZ transport only. Safe to call when already bonded (returns quickly).
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = _JustWorksAgent()
    registered = False
    try:
        object_manager = await _get_interface(
            bus, "/", "org.freedesktop.DBus.ObjectManager"
        )

        # Fast path: already bonded?
        objects = await object_manager.call_get_managed_objects()
        path = _find_device(objects, name=name, address=address)
        if path and _is_paired(objects, path):
            LOGGER.debug("Truma %s already bonded", name)
            return True

        # Register our auto-accept agent as the default for the pairing window.
        bus.export(_AGENT_PATH, agent)
        agent_manager = await _get_interface(
            bus, "/org/bluez", "org.bluez.AgentManager1"
        )
        await agent_manager.call_register_agent(_AGENT_PATH, "NoInputNoOutput")
        await agent_manager.call_request_default_agent(_AGENT_PATH)
        registered = True

        LOGGER.info("Truma %s: attempting Just Works bond (%ss)", name, timeout)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            objects = await object_manager.call_get_managed_objects()
            path = _find_device(objects, name=name, address=address)
            if path and _is_paired(objects, path):
                LOGGER.info("Truma %s bonded", name)
                return True
            if path:
                await _try_pair(bus, path)
            await asyncio.sleep(_POLL_INTERVAL)

        LOGGER.warning("Truma %s: pairing timed out after %ss", name, timeout)
        return False
    finally:
        if registered:
            try:
                await agent_manager.call_unregister_agent(_AGENT_PATH)
            except Exception as exc:  # noqa: BLE001 - best effort cleanup
                LOGGER.debug("Truma agent unregister failed: %s", exc)
        bus.disconnect()


async def _try_pair(bus: MessageBus, path: str) -> None:
    """One pairing attempt against the device at ``path`` (best effort)."""
    device = await _get_interface(bus, path, "org.bluez.Device1")
    properties = await _get_interface(
        bus, path, "org.freedesktop.DBus.Properties"
    )
    try:
        await properties.call_set(
            "org.bluez.Device1", "Trusted", Variant("b", True)
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("Truma set-trusted failed: %s", exc)
    try:
        await asyncio.wait_for(device.call_pair(), timeout=_PAIR_CALL_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - expected until the panel accepts
        LOGGER.debug("Truma pair attempt: %s", str(exc)[:80])
