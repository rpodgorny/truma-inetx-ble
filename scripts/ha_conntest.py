#!/usr/bin/env python3
"""Test a BLE GATT connect to the Truma panel on a specific adapter.

Usage: ha_conntest.py [address] [hciN]
Run e.g. in a throwaway HA-image container:
  podman run --rm --net=host --entrypoint python3 -v /tmp/ha_conntest.py:/t.py:ro \
    -v /run/dbus:/run/dbus ghcr.io/home-assistant/home-assistant:stable \
    /t.py 50:98:93:FF:B4:D1 hci1
"""

import asyncio
import sys

from bleak import BleakClient

ADDR = sys.argv[1] if len(sys.argv) > 1 else "50:98:93:FF:B4:D1"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "hci1"


async def main() -> None:
    print(f"connecting to {ADDR} via {ADAPTER} ...", flush=True)
    try:
        async with BleakClient(ADDR, adapter=ADAPTER, timeout=25) as client:
            services = list(client.services)
            print(f"CONNECTED via {ADAPTER}; {len(services)} services", flush=True)
            for svc in services:
                print(f"  svc {svc.uuid}", flush=True)
            print("RESULT: OK", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"RESULT: FAIL {type(exc).__name__}: {str(exc)[:120]}", flush=True)


asyncio.run(main())
