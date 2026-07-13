"""Bonding (pairing) for the Truma iNet X panel.

The panel uses **Just Works** pairing (no passkey shown) and only bonds while a
client is *actively* attempting to pair AND the panel is in add-device mode. It
also silently rejects new bonds when its stored device list is full, so the user
must clear that list first if pairing fails repeatedly.

``ensure_bonded()`` hides that behind one call and dispatches by transport:

* **Bluetooth proxy** (``_ensure_bonded_proxy``) — connect through an ESP32
  ESPHome ``bluetooth_proxy`` with bleak and ``pair()``, then verify the bond
  by accessing a protected characteristic. This is the reliable path for this
  fast-rotating-RPA panel (BlueZ can pair it but cannot GATT-reconnect it), and
  the one validated end-to-end against the real panel.
* **Local BlueZ** (``_ensure_bonded_bluez``) — a faithful port of
  ``scripts/ha_pair.py``: register a NoInputNoOutput auto-accept agent, then
  busy-loop ``Device1.Pair()`` until the device reports ``Paired``. Kept for a
  future ESP-less (direct local-adapter) setup; not yet validated end-to-end
  from inside HA against a capable adapter.
"""

from __future__ import annotations

import asyncio
import time

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method
from homeassistant.core import HomeAssistant

from .bt import async_resolve_proxy_device
from .const import LOGGER
from .truma.const import CHAR_CMD

BLUEZ = "org.bluez"
_AGENT_PATH = "/truma_inetx/agent"
_PAIR_CALL_TIMEOUT = 8.0
_POLL_INTERVAL = 1.0


# --- transport dispatch ----------------------------------------------------


async def ensure_bonded(
    hass: HomeAssistant,
    name: str,
    address: str,
    *,
    adapter_path: str | None = None,
    timeout: float = 60.0,
) -> bool:
    """Ensure the Truma panel is BLE-bonded, over whichever transport reaches it.

    Prefers a Bluetooth proxy (the reliable path for this fast-rotating-RPA
    panel that local BlueZ cannot GATT-reconnect); falls back to direct local
    BlueZ when no proxy route is available (e.g. an ESP-less setup). The caller
    must have prompted the user to put the panel into add-device mode (and to
    clear its device list if it is full). Returns ``True`` if bonded. Safe to
    call when already bonded.
    """
    # A proxy setup surfaces the panel through a remote scanner within a couple
    # of seconds of it advertising in add-device mode; probe briefly for that.
    probe_deadline = time.monotonic() + min(8.0, timeout / 2)
    while time.monotonic() < probe_deadline:
        if async_resolve_proxy_device(hass, name) is not None:
            return await _ensure_bonded_proxy(hass, name, timeout=timeout)
        await asyncio.sleep(1.0)
    LOGGER.debug("Truma %s: no Bluetooth proxy route; using local BlueZ pairing", name)
    return await _ensure_bonded_bluez(
        name, address, adapter_path=adapter_path, timeout=timeout
    )


# --- Bluetooth-proxy pairing (bleak) ---------------------------------------


def _noop_notify(_sender: object, _data: bytearray) -> None:
    """Discard notifications during the pairing bond test."""


async def _ensure_bonded_proxy(
    hass: HomeAssistant, name: str, *, timeout: float = 60.0
) -> bool:
    """Bond via a Bluetooth proxy: connect with bleak, ``pair()``, then verify.

    The proxy encrypts lazily, so pair()/encrypt first, then confirm the bond
    took by subscribing to a protected characteristic (a CCCD write only
    succeeds on an encrypted link). Retries while the panel is in add-device
    mode until ``timeout``.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        device = async_resolve_proxy_device(hass, name)
        if device is None:
            await asyncio.sleep(1.5)
            continue
        client: BleakClientWithServiceCache | None = None
        try:
            client = await establish_connection(
                BleakClientWithServiceCache, device, device.address, max_attempts=1
            )
            for _ in range(3):
                try:
                    await client.pair()
                except Exception as exc:  # noqa: BLE001 - not all paths need it
                    LOGGER.debug("Truma %s proxy pair(): %s", name, exc)
                try:
                    # A protected CCCD write only lands on an encrypted (bonded)
                    # link — success here means the bond took.
                    await client.start_notify(CHAR_CMD, _noop_notify)
                    await client.stop_notify(CHAR_CMD)
                    LOGGER.info("Truma %s bonded via proxy", name)
                    return True
                except Exception as exc:  # noqa: BLE001 - retry through encrypt race
                    last_exc = exc
                    await asyncio.sleep(1.5)
        except Exception as exc:  # noqa: BLE001 - transient connect failures
            last_exc = exc
            LOGGER.debug("Truma %s proxy connect: %s", name, exc)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception as exc:  # noqa: BLE001 - best effort
                    LOGGER.debug("Truma %s proxy disconnect: %s", name, exc)
        await asyncio.sleep(2.0)
    LOGGER.warning("Truma %s: proxy pairing timed out (%s)", name, last_exc)
    return False


# --- local BlueZ pairing (D-Bus) — kept for a future ESP-less setup ---------


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


def _find_device(
    objects: dict, *, name: str, address: str, adapter_path: str | None = None
) -> str | None:
    """Return the BlueZ device path matching ``name`` (or ``address``).

    When ``adapter_path`` is given, only devices under that adapter are
    considered, so the bond lands on the adapter HA connects through rather
    than any adapter that happens to see the panel.
    """
    address = address.upper()
    name_lc = name.lower()
    for path, ifaces in objects.items():
        if adapter_path and not path.startswith(f"{adapter_path}/"):
            continue
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


async def _ensure_bonded_bluez(
    name: str,
    address: str,
    *,
    adapter_path: str | None = None,
    timeout: float = 60.0,
) -> bool:
    """Bond the Truma panel over local BlueZ (D-Bus). Return ``True`` if bonded.

    Registers a temporary Just Works agent and busy-loops ``Device1.Pair()``
    until the panel reports ``Paired`` or ``timeout`` elapses. The caller must
    have prompted the user to put the panel into add-device mode (and to clear
    its device list if it is full).

    ``adapter_path`` (e.g. ``/org/bluez/hci0``) scopes the bond to the adapter
    HA connects through; when omitted, any adapter that sees the panel is used.

    BlueZ transport only. Safe to call when already bonded (returns quickly).
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = _JustWorksAgent()
    registered = False
    try:
        object_manager = await _get_interface(
            bus, "/", "org.freedesktop.DBus.ObjectManager"
        )

        # Fast path: already bonded (on the connecting adapter)?
        objects = await object_manager.call_get_managed_objects()
        path = _find_device(
            objects, name=name, address=address, adapter_path=adapter_path
        )
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
            path = _find_device(
                objects, name=name, address=address, adapter_path=adapter_path
            )
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
