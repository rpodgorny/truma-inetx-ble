# MEMORY — Truma iNet X → Home Assistant (native BLE integration)

Hard-won knowledge from building & debugging this. Read this before touching the
BLE connection path again — it will save you hours.

---

## TL;DR

- A **native HA custom integration** lives in `custom_components/truma_inetx/`
  (Tier 1: config flow + entities, no MQTT). It reuses the reverse-engineered
  protocol code unchanged under `custom_components/truma_inetx/truma/`.
- The integration + protocol are **PROVEN CORRECT**: after a clean pairing it
  connects and reads all **18 GATT characteristics** instantly.
- **The hard problem is not the code — it's establishing/holding the BLE
  connection from BlueZ on a Raspberry Pi.** See "The connection saga".
- **Root cause of most connection failures = the panel's Bluetooth bond list
  fills up** (~4 slots). When full, the panel *silently rejects* new pairings
  AND connections. Clearing it (in the iNet X app / on the panel) is essential.
- **Reliable RE-connection from BlueZ still does not work** on the test Pi
  (both its BT4.0 CSR dongle and BT5.0 onboard). Phones reconnect fine because
  they use a different BLE stack. Best paths forward: an **RTL8761B USB dongle**
  or an **ESP32 ESPHome Bluetooth Proxy** (phone-like ESP-IDF stack, bypasses
  BlueZ).

---

## The device

- Panel: **Truma iNet X** on a **Combi D 4 (diesel)**. Advertises local name
  `Truma iNetX-FFB4D1`. The `FFB4D1` suffix is the panel's fixed identity and is
  stable; the BLE **address rotates** (resolvable private address) and resolves
  to identity **`50:98:93:FF:B4:D1`** (note `FF:B4:D1` == the name suffix).
- GATT service `fc314000-f3b2-11e8-8eb2-f2801f1b9fd1`; advertised service
  `fc310002-...`. Characteristics: **CMD `fc314001`** (write-request),
  **DATA_W `fc314002`** (write-command / no response), **DATA_R `fc314003`**
  (notify), plus `fc314004` (do NOT subscribe).
- Diesel is supported: `EnergySrc.DieselLevel`. Command set is identical for
  diesel and gas.
- The panel supports **up to 3 mobile clients simultaneously** and stores a
  managed device list (~4). HA and the phone app CAN coexist.

## Protocol (already reverse-engineered — reused, don't touch)

`truma/protocol.py` (framing/CBOR), `truma/const.py` (UUIDs, device IDs, control
/MBP types, topic batches), `truma/state.py` (`TrumaState`). Layers:
`BLE GATT → transport FSM (InitDataTransfer/Ready/DataAck) → TruMessageV3
(16-byte header) → MBP sub-protocol → CBOR payload`.

Startup sequence (in `coordinator._run_startup`, faithful to the original
`main.py`): connect → **register** (wait for assigned addr) → **subscribe** all
`TOPIC_BATCHES` → send **identity** frames → **param discovery** on heater
(0x0201) + panel (0x0101) → listen. Notifications: MsgAck (0x83) auto-confirm
with `0x0300`; DATA_R frames auto-ACK with `0xF001` then `parse_v3_frame`.

## Integration architecture (Tier 1, built here)

- `manifest.json` — BLE discovery matchers (local_name + service_uuid),
  `iot_class: local_push`, `requirements: ["cbor2"]`.
- `config_flow.py` — bluetooth discovery + manual pick. **unique_id = the stable
  NAME, never the address** (the RPA rotates and would spawn duplicate devices);
  rediscovery refreshes the stored address via
  `_abort_if_unique_id_configured(updates={CONF_ADDRESS: ...})`.
- `coordinator.py` — push `DataUpdateCoordinator[TrumaState]`; background session
  task (connect → startup → hold → reconnect w/ backoff); persists HA's app
  identity (muid/uuid) via `helpers.storage.Store`. Optional `CONF_ADAPTER`
  dedicated-adapter path (see below).
- `entity.py` + platforms: `climate`, `sensor`×4, `select`×2, `switch`,
  `number`, `binary_sensor`×2. Entity/device identity uses the stable NAME, not
  the address.
- `ble.py` — `TrumaBleClient`: bleak transport + the send FSM. Two connect paths:
  `connect(ble_device)` via HA's stack (`bleak_retry_connector`), and
  `connect_raw(address, adapter)` via `BleakClientBlueZDBus` directly.

Writes (Stage 3) are stubbed to raise until enabled. Controls appear but error
clearly until then.

## HA API gotchas (2026.7.x, learned the hard way)

- HA **monkeypatches `bleak.BleakClient`** via `habluetooth.wrappers` — a "raw"
  `BleakClient(adapter=...)` gets rerouted through HA's manager and the adapter=
  is ignored. To pin an adapter you must construct the backend client directly:
  `from bleak.backends.bluezdbus.client import BleakClientBlueZDBus`.
- That backend's connect API on current bleak: `BleakClientBlueZDBus(addr,
  bluez={"adapter": "hciN"}, ...)` then `await client.connect(pair=False)`
  (`bluez=` is required; `adapter=` is deprecated; `pair` has no default).
- `Adapter1.ConnectDevice()` (direct connect by address) is gated behind BlueZ
  **D-Bus Experimental** (`Experimental = true` in main.conf).
- `type` alias / PEP 695 works; use `AddConfigEntryEntitiesCallback`.

## Pairing

- The panel uses **Just Works** pairing (NO passkey shown). It only bonds while a
  client is **actively hammering** Pair() AND the panel is in add-device mode.
  Use `scripts/ha_pair.py <adapter_path> Truma 120` (busy-loops a
  `NoInputNoOutput` auto-accept BlueZ agent). Trigger the panel's add-device mode
  and re-trigger every ~15s.
- **CRITICAL: if pairing or connecting fails, the panel's stored device list is
  probably FULL.** Delete all Bluetooth connections on the panel (iNet X App
  button → device → dustbin) and/or in the app (Settings → Device Manager). This
  is Truma's own documented fix ("otherwise it is not possible to establish a new
  connection"). Every failed re-pair leaves a dead bond on the panel eating a
  slot.
- Bonds are **per-adapter** (stored in `/var/lib/bluetooth/<adapter-mac>/`), so a
  new adapter needs its own pairing.

## The connection saga (the whole point of this file)

Symptom: `Device.Connect()` to the bonded panel **stalls — BlueZ issues zero
`LE Create Connection` commands** (confirmed via `btmon`), then times out /
`le-connection-abort-by-local` / `In Progress`.

What was tried and RULED OUT as the fix:
- HA scan/connect contention; scan-stop-connect (the original `ble_connect.py`
  sequence: StartDiscovery → StopDiscovery → Connect); dedicated raw adapter;
  `ConnectDevice()` direct connect; BlueZ `Experimental` + `KernelExperimental`
  LL-Privacy (never activated — no `privacy` in `btmgmt … info` current
  settings; the CSR dongle is BT4.0, too old); adapter power-cycle; USB reset of
  the dongle; full Pi reboot; keeping the panel display awake.

What DID matter:
- **Clearing the panel's bond list → fresh pairing → the connection works** and
  reads all 18 chars. So pairing-time connection is fine.
- But a *fresh reconnect afterward* stalls on BOTH adapters (BT4.0 CSR dongle and
  BT5.0 onboard `bcm`/`hci_uart_bcm`, mfr 305). Only the pairing-time link works.
- Phones reconnect fine → it's a **BlueZ-on-this-Pi limitation** with this RPA
  panel, not the integration.

### Hardware gotchas
- Test host is a **Raspberry Pi**: onboard `hci_uart_bcm` (mfr 305, BT5.0,
  `E4:5F:01:0B:37:DD`) + a cheap **CSR8510 clone** USB dongle (`0a12:0001`,
  "cyber-blue(HK)Ltd", BT4.0, `00:1A:7D:DA:71:13`).
- **hciN numbers SWAP across reboots** (dongle was hci1, became hci0). Never pin
  by `hciN` — resolve by MAC or `btusb`/`hci_uart_bcm` driver.
- The CSR dongle can *scan* the panel fine (RSSI ~-44) but reliably fails to
  *initiate/hold* a connection under HA. Renogy/BThome work on the onboard
  because they use **static/public** addresses (no RPA/privacy).

## Deploy / test loop (for the campervan HA)

- HA runs in **rootful podman**, container `homeassistant`
  (`ghcr.io/home-assistant/home-assistant:stable`), config dir on host
  `/docker_volumes/homeassistant` (= `/config` in container), D-Bus socket
  mounted. Access via a root shell in a local tmux session (drive with
  `tmux send-keys` + `tmux capture-pane`).
- Deploy: push branch → on host
  `curl -fsSL https://codeload.github.com/rpodgorny/truma-inetx-ble/tar.gz/refs/heads/<branch>`
  → extract `custom_components/truma_inetx` into the config dir →
  `podman restart homeassistant`.
- One-off BLE tests run in a throwaway container using the HA image (has
  dbus-fast + bleak): `podman run --rm --net=host --entrypoint python3
  -v /tmp/X.py:/x.py:ro -v /run/dbus:/run/dbus <ha-image> /x.py ...`.
- Config entries live in `/docker_volumes/homeassistant/.storage/core.config_entries`
  (edit with HA stopped; backup `.bak`). Disable/enable a BT adapter =
  `disabled_by: "user"|null` on its bluetooth entry (keyed by adapter MAC).

## Diagnostic scripts (`scripts/`)

- `ha_pair.py <adapter_path> <name> <timeout>` — busy-loop Just Works pairing.
- `ha_conntest.py <addr> <hciN>` — bleak connect + list services (runs the real
  bleak, useful *outside* HA where BleakClient isn't monkeypatched).
- `ha_scanconnect.py <addr> <hciN>` — dbus-fast scan → STOP → connect (the
  original project's sequence); reports ServicesResolved + char count.
- `ha_connectdevice.py <addr> <hciN> <public|random>` — `Adapter1.ConnectDevice`
  direct-connect test (needs BlueZ `Experimental = true`).

## Recommended next steps (in priority order)

1. **ESP32 ESPHome Bluetooth Proxy.** Different BLE stack (ESP-IDF), phone-like;
   HA connects the panel *through* it, bypassing BlueZ. Best chance given phones
   work. Point the integration at the proxy-provided connectable device.
2. **RTL8761B USB dongle** for the Pi — well-supported BLE5 chip; may let BlueZ
   itself connect/reconnect reliably. Then drop any `CONF_ADAPTER` pin and use
   the plain HA-stack path.
3. Whenever connections fail: **first clear the panel's bond list**, then re-pair.
4. Stages remaining once a link holds: Stage 3 (writes/commands — the send FSM is
   already implemented, just unstub `async_write`), Stage 4 (in-flow pairing,
   HACS metadata, README).

## Do NOT repeat these dead ends
- Don't blame the panel/hardware for "can't connect" — check the **bond list**.
- Don't pin adapters by `hciN` (they renumber on reboot).
- Don't expect a "raw" `bleak.BleakClient(adapter=...)` to honor the adapter in
  HA (it's wrapped).
- Don't expect LL-Privacy to fix it on a CSR8510/BT4.0 dongle (unsupported).
- Don't keep re-pairing without clearing the panel list first (fills its slots).
