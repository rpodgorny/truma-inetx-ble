#!/usr/bin/env python3
"""Force a DIRECT BLE connection via BlueZ Adapter1.ConnectDevice().

Device.Connect() on a bonded device uses the kernel auto-connect (allow-list)
path, which waits for a connectable advert and can hang. ConnectDevice() does a
direct connection by address+type — the way a phone connects right after seeing
the device. This is the untried path for the "zero LE Create Connection" stall.

Usage: ha_connectdevice.py <address> <hciN> <public|random>
"""

import asyncio
import sys

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

BLUEZ = "org.bluez"
ADDR = sys.argv[1] if len(sys.argv) > 1 else "50:98:93:FF:B4:D1"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "hci0"
ATYPE = sys.argv[3] if len(sys.argv) > 3 else "public"
AP = f"/org/bluez/{ADAPTER}"


async def main() -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    ao = bus.get_proxy_object(BLUEZ, AP, await bus.introspect(BLUEZ, AP))
    adapter = ao.get_interface("org.bluez.Adapter1")

    print(f"ConnectDevice {ADDR} ({ATYPE}) on {ADAPTER} ...", flush=True)
    try:
        res = await asyncio.wait_for(
            adapter.call_connect_device(
                {
                    "Address": Variant("s", ADDR),
                    "AddressType": Variant("s", ATYPE),
                }
            ),
            timeout=30,
        )
        print(f"RESULT: OK connected dev={res}", flush=True)
        # give services a moment, then check chars via ObjectManager
        await asyncio.sleep(3)
        om = (
            bus.get_proxy_object(BLUEZ, "/", await bus.introspect(BLUEZ, "/"))
        ).get_interface("org.freedesktop.DBus.ObjectManager")
        objs = await om.call_get_managed_objects()
        chars = [
            p
            for p, i in objs.items()
            if p.startswith(res) and "org.bluez.GattCharacteristic1" in i
        ]
        print(f"chars={len(chars)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"RESULT: FAIL {type(exc).__name__}: {str(exc)[:140]}", flush=True)


asyncio.run(main())
