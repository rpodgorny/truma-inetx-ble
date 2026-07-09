#!/usr/bin/env python3
"""Test the original project's connect sequence: scan -> STOP scan -> connect.

The panel (like the phone app) only connects reliably when discovery is stopped
first. HA scans continuously, which blocks the connect on marginal adapters.

Usage: ha_scanconnect.py <address> <hciN>
"""

import asyncio
import sys

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

BLUEZ = "org.bluez"
ADDR = sys.argv[1] if len(sys.argv) > 1 else "50:98:93:FF:B4:D1"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "hci0"
ADAPTER_PATH = f"/org/bluez/{ADAPTER}"


async def main() -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    om = (
        bus.get_proxy_object(BLUEZ, "/", await bus.introspect(BLUEZ, "/"))
    ).get_interface("org.freedesktop.DBus.ObjectManager")

    ao = bus.get_proxy_object(
        BLUEZ, ADAPTER_PATH, await bus.introspect(BLUEZ, ADAPTER_PATH)
    )
    adapter = ao.get_interface("org.bluez.Adapter1")

    try:
        await adapter.call_stop_discovery()
    except Exception:
        pass
    try:
        await adapter.call_set_discovery_filter({"Transport": Variant("s", "le")})
    except Exception as exc:
        print("filter warn:", exc, flush=True)

    print(f"scanning 6s on {ADAPTER} ...", flush=True)
    await adapter.call_start_discovery()
    await asyncio.sleep(6)
    await adapter.call_stop_discovery()
    print("scan STOPPED; locating device ...", flush=True)

    objs = await om.call_get_managed_objects()
    dev_path = None
    for path, ifaces in objs.items():
        if not path.startswith(ADAPTER_PATH):
            continue
        d = ifaces.get("org.bluez.Device1")
        if not d:
            continue
        addr = d.get("Address")
        addr = addr.value if addr else ""
        name = d.get("Name")
        name = name.value if name else ""
        if addr.upper() == ADDR.upper() or "iNet" in str(name):
            dev_path = path
            print(f"device {path} addr={addr} name={name}", flush=True)
            break

    if not dev_path:
        print("RESULT: FAIL device-object-not-found", flush=True)
        return

    do = bus.get_proxy_object(BLUEZ, dev_path, await bus.introspect(BLUEZ, dev_path))
    device = do.get_interface("org.bluez.Device1")
    props = do.get_interface("org.freedesktop.DBus.Properties")

    print("connecting (discovery stopped) ...", flush=True)
    try:
        await asyncio.wait_for(device.call_connect(), timeout=20)
    except Exception as exc:
        print(f"RESULT: FAIL connect: {str(exc)[:110]}", flush=True)
        return

    for i in range(30):
        r = await props.call_get("org.bluez.Device1", "ServicesResolved")
        if r.value:
            print(f"ServicesResolved after {i * 0.5}s", flush=True)
            break
        await asyncio.sleep(0.5)
    else:
        print("RESULT: FAIL services-not-resolved", flush=True)
        await device.call_disconnect()
        return

    objs2 = await om.call_get_managed_objects()
    chars = [
        p
        for p, i in objs2.items()
        if p.startswith(dev_path) and "org.bluez.GattCharacteristic1" in i
    ]
    print(f"RESULT: OK chars={len(chars)}", flush=True)
    await device.call_disconnect()


asyncio.run(main())
