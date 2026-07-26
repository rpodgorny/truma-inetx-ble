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

## Stage 5: Resilience + UX hardening for public release [In Progress]
**Goal**: survive real-world churn without a manual panel power-cycle, and guide
the user through the failure modes we hit during dev.

### 5a: Data-stall watchdog [Complete]
The hold loop could sit in a half-open link (`client.connected` True, no frames)
forever, showing stale data — the state that forced a power-cycle. Now the
coordinator tracks the last-frame loop-clock time (seeded on connect, refreshed
in `_on_frame`) and, if no frame arrives for `_DATA_STALL_TIMEOUT` (90 s), drops
the link and reconnects. Panel pushes ~25 frames/min, so 90 s of silence is
unambiguously dead — no false trips.

### 5b: Deterministic client teardown [Complete]
`_run` now tears the client down in a `finally` (new `_disconnect_client`) on
every attempt, and the client is tracked *before* connecting — so a stall, a
failed connect, or a partial connect never leaves a half-open link holding the
proxy's connection slot (the ghost that needed a power-cycle). `async_stop`
routes through the same helper.

### 5c: Re-pair for the proxy stale-bond case (reason=97) [Complete]
A re-pair looked like it needed both sides cleared: the panel's device list is
cleared (or rolls our entry out of its ~4 slots) while the proxy still holds
its half, so the panel rejects the new bond with `error: 97`. The old
onboarding text told the user to press a **Clear BLE bonds** button — a lambda
in our own proxy YAML that no stock-firmware user has. That made the
integration unshippable.

**It needs no clearing at all.** The proxy's stale bond is *per address*. The
panel rejects the bond only on the RPA the proxy has a bond for; its next RPA
is one the proxy has never seen, and pairing there is clean. The `avoid`
rotation from 5d already does exactly this, so re-pairing works on stock
firmware with no button, no custom YAML and no reflash. Verified end-to-end on
the van twice on 2026-07-26 (deleted the HA entry, cleared the panel's list,
deliberately left the proxy's bonds in place): rejected on the stale RPA,
rotated, bonded on the fresh one, ~9s each time.

Rejected alternative — issuing the ESPHome bluetooth-proxy `UNPAIR` request
(stock since ESPHome 2024.3.0). The ESP applies it without a connection, but
`bleak_esphome` gates `BleakClient.unpair()` on a live one, and **the panel
drops the link the instant it rejects the bond** — measured at 100ms, far
inside the request round-trip, so the call always lands on a dead connection.
The only way through is `aioesphomeapi.bluetooth_device_unpair()` reached via
`hass.data["esphome"]` internals: private API, not worth it when rotation
already works. Onboarding strings now point at a panel power-cycle, which
forces a fresh RPA, as the escalation.

Checked offline by `scripts/test_pairing_rotation.py`.

### 5d: Phantom-RPA rotation [Complete — pending live re-validation]
Observed live (2026-07-16): after pairing on one RPA, the panel keeps
advertising that address but stops accepting connections on it, while
advertising a fresh live RPA. The resolver always returned that first (dead)
address — same name, cached connectable route — so the coordinator hammered it
with ESP_GATT_CONN_FAIL_ESTABLISH (0x3e) forever and the device stayed
unavailable until a panel power-cycle. Fix: `async_resolve_proxy_device` takes
an `avoid` set; the coordinator adds any address whose *connect* fails and
rotates to the next advertised RPA, clearing on success or when every candidate
is exhausted (so nothing wedges permanently). This is the last known cause of
the post-pairing power-cycle.

### 5e: Live re-validation of the full onboarding [Not Started]
Re-run clean onboarding end-to-end and deliberately re-trigger the phantom-RPA
state to prove the coordinator now recovers on its own (no power-cycle), then
confirm the watchdog/teardown handle steady-state drops.

## Reused (vendored, unchanged) under `truma/`
- `protocol.py` — TruMessageV3 framing + CBOR (transport-agnostic)
- `const.py` — device IDs, control/MBP types, char UUIDs, topic batches
- `state.py` — `TrumaState` model, command validation/routing
