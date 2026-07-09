# Truma iNet X native HA integration — implementation plan (Tier 1)

Goal: a real Home Assistant custom integration (config flow, native entities,
HACS-installable) for the Truma iNet X BLE panel — no MQTT bridge, no Mosquitto.
Reuses the reverse-engineered protocol/state code from this repo verbatim
(vendored under `truma/`).

## Stage 1: Scaffolding [Complete]
Integration loads in HA; BLE discovery → config flow → entities register on one
device (climate, 4 sensors, 2 selects, switch, number, 2 binary sensors).
Verified on the van under 2026.7.1.

## Stage 2: BLE transport + live data [Complete (code); hardware-gated for reliability]
Coordinator connects over HA's Bluetooth stack (`async_ble_device_from_address(
connectable=True)` + `establish_connection`), runs register/subscribe/identity/
param-discovery, parses V3/CBOR into `TrumaState`. Proven correct: connects +
reads all 18 GATT chars after a clean pairing. RELIABLE reconnect needs capable
BLE hw (RTL8761B dongle or ESP32 proxy) — the onboard BCM / CSR clone can pair
but not hold reconnects. See repo MEMORY.md.

## Stage 3: Commands [Not Started]
`async_write` builds MBP_WRITE frames; climate/select/switch/number controls
change the heater. Validation via `TrumaState.validate_command`.

## Stage 4: In-flow pairing + polish (upstream-readiness) [Complete — re-validate scoping on target BLE hw]
Make it user-friendly and upstreamable. Most of this is transport-agnostic and
buildable/testable now with the current dongle (pairing works at pairing-time;
only reliable *reconnect* is hardware-gated).

### 4a: Graceful reconnect/backoff + availability [Complete]
**Goal**: the session is a good BLE citizen — exponential backoff with a cap,
resets after a session that actually connected, and an interruptible wait so
unload is fast. Fixes the adapter-hogging that starved Renogy AND is required
for upstream.
**Success Criteria**: after repeated connect failures the retry interval grows
to a cap (≤5 min), not a fixed ~15 s hammer; entities read `unavailable` cleanly;
`async_stop()` returns promptly (no multi-minute sleep). No behavior change on a
healthy link (quick reconnect on drop).
**Tests**: unit-style reasoning + deploy; confirm log shows growing delays and
Renogy stays healthy with the entry enabled.

### 4b: Config-flow pairing UX [Complete]
Discovery/manual → instruction step → bond+verify (retry ~60 s) → create entry,
with a translated `pairing_failed` error. Strings in `strings.json`/translations.

### 4c: `ensure_bonded()` abstraction + BlueZ impl [Complete]
`pairing.py::ensure_bonded()` guarantees a bond before the entry is created:
temporary NoInputNoOutput auto-accept BlueZ agent + `Device1.Pair()` loop via
dbus-fast (ported from `scripts/ha_pair.py`). Scoped to the connectable adapter
(`adapter_path` from `async_ble_device_from_address(..., connectable=True)`) so
the bond lands where the integration connects; proxy-native pairing drops in
behind the same seam. Dropped the `CONF_ADAPTER`/`connect_raw` hack.

### 4d: Live validation [Complete]
Validated on the van via a standalone harness mirroring `ensure_bonded`: agent
registered, panel matched by NAME across an RPA rotation, `Pair()` bonded it —
Renogy undisturbed (pairing ran on a separate radio). Surfaced (and fixed) the
connectable-adapter scoping. Re-validate the scoping on the target BLE hardware.

### 4e: Docs / HACS metadata / polish [Complete]
`hacs.json` + README (native-integration section: install, pairing, re-pair,
hardware reality). Re-pair shipped as `async_step_reconfigure` (device →
Reconfigure) reusing the pairing step — cleaner than a button (shows the
add-device instructions). Identity persists via `helpers.storage.Store`.

## Reused (vendored, unchanged) under `truma/`
- `protocol.py` — TruMessageV3 framing + CBOR (transport-agnostic)
- `const.py` — device IDs, control/MBP types, char UUIDs, topic batches
- `state.py` — `TrumaState` model, command validation/routing
