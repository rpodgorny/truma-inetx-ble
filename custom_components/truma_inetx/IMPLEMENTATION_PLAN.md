# Truma iNet X native HA integration — implementation plan (Tier 1)

Goal: a real Home Assistant custom integration (config flow, native entities,
HACS-installable) for the Truma iNet X BLE panel — no MQTT bridge, no Mosquitto.
Reuses the reverse-engineered protocol/state code from this repo verbatim
(vendored under `truma/`).

## Stage 1: Scaffolding [In Progress]
**Goal**: Integration loads in HA; BLE discovery → config flow → entities register.
**Success Criteria**:
- HA recognizes `truma_inetx`, no import/setup errors in the log.
- Panel is auto-discovered via BLE (manifest matcher); config flow completes.
- All entities register on one device: climate, 4 sensors, 2 selects, switch,
  number, 2 binary sensors. They read as *unavailable* (no transport yet).
**Tests**: deploy to van HA, restart, grep log for the integration; confirm
config entry + device + entities exist.
**Non-goals**: live BLE connection, reading data, sending commands, pairing.

## Stage 2: BLE transport + live data [Not Started]
**Goal**: Coordinator connects over HA's Bluetooth stack (bleak /
`async_ble_device_from_address(connectable=True)` + `establish_connection`),
runs the InitDataTransfer handshake, registers identity, subscribes to topic
batches, parses V3/CBOR notifications into `TrumaState` → entities go available.
**Success Criteria**: room/water/internal temps, modes, flame, voltage populate;
`binary_sensor` connection = on. Assumes device already BLE-bonded out of band.

## Stage 3: Commands [Not Started]
**Goal**: `async_write` builds MBP_WRITE frames and sends them; climate/select/
switch/number controls actually change the heater. Validation via
`TrumaState.validate_command`.

## Stage 4: Pairing helper + polish [Not Started]
**Goal**: document/ship the one-time bonding step (6-digit passkey), identity
persistence in the config entry, HACS metadata, README, reconnect/retry.

## Reused (vendored, unchanged) under `truma/`
- `protocol.py` — TruMessageV3 framing + CBOR (transport-agnostic)
- `const.py` — device IDs, control/MBP types, char UUIDs, topic batches
- `state.py` — `TrumaState` model, command validation/routing
