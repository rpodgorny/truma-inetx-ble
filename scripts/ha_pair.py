#!/usr/bin/env python3
"""Busy-loop Just Works pairing for the Truma iNet X panel over BlueZ D-Bus.

The panel accepts a bond only while a client is actively attempting to pair,
and shows no passkey (Just Works). This registers a NoInputNoOutput agent
(auto-accepts), scans, and repeatedly calls Pair() until bonded, then marks the
device trusted.

Run anywhere with dbus-fast + system bus + a BLE adapter, e.g. inside a
throwaway container from the HA image:

  podman run --rm --net=host --entrypoint python3 \
    -v /tmp/ha_pair.py:/pair.py:ro -v /run/dbus:/run/dbus \
    ghcr.io/home-assistant/home-assistant:stable /pair.py /org/bluez/hci0 Truma 120

Usage: ha_pair.py [adapter_path] [name_substr] [timeout_s]
"""

import asyncio
import sys
import time

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

BLUEZ = "org.bluez"
AGENT_PATH = "/truma/agent"

ADAPTER = sys.argv[1] if len(sys.argv) > 1 else "/org/bluez/hci0"
NAME = sys.argv[2] if len(sys.argv) > 2 else "Truma"
TIMEOUT = int(sys.argv[3]) if len(sys.argv) > 3 else 120


class JustWorksAgent(ServiceInterface):
    """A BlueZ agent that auto-accepts everything (Just Works, no passkey)."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):  # noqa: N802
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802,F821
        return "0000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802,F821
        pass

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802,F821
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802,F821
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802,F821
        pass  # no exception = accept

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
        pass

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
        pass

    @method()
    def Cancel(self):  # noqa: N802
        pass


async def _props(bus, path):
    intr = await bus.introspect(BLUEZ, path)
    obj = bus.get_proxy_object(BLUEZ, path, intr)
    return obj


async def main() -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    om_obj = await _props(bus, "/")
    om = om_obj.get_interface("org.freedesktop.DBus.ObjectManager")

    agent = JustWorksAgent()
    bus.export(AGENT_PATH, agent)
    am_obj = await _props(bus, "/org/bluez")
    am = am_obj.get_interface("org.bluez.AgentManager1")
    await am.call_register_agent(AGENT_PATH, "NoInputNoOutput")
    await am.call_request_default_agent(AGENT_PATH)

    ad_obj = await _props(bus, ADAPTER)
    adapter = ad_obj.get_interface("org.bluez.Adapter1")
    adprops = ad_obj.get_interface("org.freedesktop.DBus.Properties")
    await adprops.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
    try:
        await adapter.call_set_discovery_filter({"Transport": Variant("s", "le")})
        await adapter.call_start_discovery()
    except Exception as exc:  # noqa: BLE001
        print("discovery warn:", exc, flush=True)

    print(
        f"PAIRING: busy-looping Pair() for {TIMEOUT}s on {ADAPTER}. "
        "Trigger the panel's add-device mode NOW (repeat if it times out).",
        flush=True,
    )

    start = time.monotonic()
    last = None
    while time.monotonic() - start < TIMEOUT:
        objs = await om.call_get_managed_objects()
        dev_path = None
        dev_name = ""
        for path, ifaces in objs.items():
            d = ifaces.get("org.bluez.Device1")
            if not d:
                continue
            n = d.get("Name")
            n = n.value if n else ""
            if NAME.lower() in str(n).lower():
                dev_path, dev_name = path, n
                paired = d.get("Paired")
                if paired and paired.value:
                    dp = (await _props(bus, path)).get_interface(
                        "org.freedesktop.DBus.Properties"
                    )
                    await dp.call_set(
                        "org.bluez.Device1", "Trusted", Variant("b", True)
                    )
                    print(f"RESULT: SUCCESS {path} {dev_name}", flush=True)
                    return
                break
        if dev_path:
            if dev_path != last:
                print(f"found {dev_path} ({dev_name})", flush=True)
                last = dev_path
            do = await _props(bus, dev_path)
            dev = do.get_interface("org.bluez.Device1")
            dprops = do.get_interface("org.freedesktop.DBus.Properties")
            try:
                await dprops.call_set(
                    "org.bluez.Device1", "Trusted", Variant("b", True)
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(dev.call_pair(), timeout=8)
                print("PAIR call returned", flush=True)
            except Exception as exc:  # noqa: BLE001
                try:
                    await asyncio.wait_for(dev.call_connect(), timeout=5)
                except Exception as exc2:  # noqa: BLE001
                    print(f"attempt: {str(exc)[:70]} | {str(exc2)[:40]}", flush=True)
        await asyncio.sleep(1)

    print("RESULT: TIMEOUT (no pairing)", flush=True)


asyncio.run(main())
