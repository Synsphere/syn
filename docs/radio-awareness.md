# Node radio-awareness subsystem

This module builds a **bounded, passive, evidence-bearing radio inventory** for
Syn nodes. It runs during position-aware node startup, before API readiness, and
periodically via the **existing** `ProcessScheduler` (`radio-discover` handler).
Every scan writes a typed `radio-discovery-event`, and new/changed devices create
`radio-capability-ember` objects. Inventory revisions create a
`radio-inventory-ember`. The current inventory lives in a deterministically
addressed native `Thing`, while all Events, Embers and canonical updates are
appended to the node's existing `MessageStore` SHA-256 hash chain in the same
PostgreSQL transaction. Previous Embers are never rewritten.

## Clock-enabled integration target

This revision is an **incremental radio patch on top of the clock update**.
Its verified base is `Synsphere/syn` commit
`0a3736dab76a4a94ab106baa201cc067c086af63` (`feature/position-geodetic`,
commit message `clock`). Use `syn-radio-awareness-after-clock.patch` instead of
`syn-radio-awareness.patch` (the earlier standalone radio patch); do not apply
both versions. Keep the clock update installed.

The shared `PositionRuntime` now accepts both `clockConfig` and `radioConfig`.
It preserves the clock session, unconditional NTP scheduling, GPS-to-clock
forwarding, and the first-run operator setup introduced by the clock patch.
Radio inventory preparation is added to the same scheduler preparation callback;
there is no second scheduler, no replacement clock estimator, and no overwrite
of previously hashed clock or position records. Disabling radio discovery does
not disable clock jobs. API-only mode remains an explicit operator-setup opt-out.
Native operator/Agent startup passes both configurations to the shared runtime.

The clock migration `20261117_0018` is neither replaced nor renumbered, and this
radio patch introduces no additional database migration or dependency. Run
`uv sync --extra position-native` to refresh the editable install so `syn-radio`
is registered. Run the clock migration as instructed by the clock update if it
has not yet been installed in the database.

## Limitations and truthfulness of capability reporting

**Detection is not a complete RF driver, and discovery is not authorization to
transmit.** It is not technically possible to guarantee native support for
*every* radio transmitter, receiver or transceiver across every proprietary
chipset, firmware revision, bus and operating system. This implementation
covers the enumerators below and exposes a `RadioProvider` adapter protocol
for additional drivers. Actual transmit/receive operations require a
compatible driver/firmware, hardware authorization, and applicable spectrum
regulations. Startup **never transmits RF, tunes an SDR/receiver, connects to a
Wi-Fi access point, scans nearby Bluetooth peers, or writes to I2C/SPI**.

Each descriptor differentiates `support` (`identified`, `driver`, `protocol`)
from `canReceive`/`canTransmit` (`true`, `false`, **unknown/null**) and includes
bounded evidence and observed capabilities. Identification alone is never
presented as a guaranteed working interface. OS blocked state, radio power,
firmware and regional restrictions can further reduce available operations.

## Discovery backends

| Interface family | Source | Information extracted | Limits |
| --- | --- | --- | --- |
| Wi-Fi 2.4/5/6 GHz | Linux nl80211 `iw phy`, sysfs; Windows PnP/`netsh`; macOS system_profiler | PHY modes, supported channels, capabilities and driver where available | No active BSS scan, no SSIDs |
| Wi-Fi HaLow (802.11ah/S1G) | S1G PHY reporting, Windows driver information | 802.11ah mode and reported MHz channels | Must be advertised; sub-GHz band alone is insufficient |
| Bluetooth Classic and BLE | Linux BlueZ `btmgmt info` and sysfs; OS PnP/profiler | BR/EDR, LE, advertising, extended advertising and security settings when reported | No pairing or device discovery |
| LoRa/LoRaWAN radios | Kernel SPI/I2C `modalias`/device-tree | SX126x/SX127x/SX128x/LR11xx family, bound driver | `spidev` alone cannot identify the radio |
| LoRa concentrators | SPI/I2C | SX1301/SX1302/SX1303, SX1250 and drivers where available | Packet-forwarder HAL is not magically implemented |
| FM/broadcast receiver ICs | I2C/SPI and kernel V4L2 `radioN` | SI470x, Si473x, TEA5767, RDA5807, QN8035, BK1080 | No auto-tuning or writing hardware registers |
| SDR | USB VID/PID and optionally SoapySDR `--find` | RTL2832, HackRF, bladeRF, Airspy, Pluto; Soapy driver and label | USB IDs may match a family rather than exact RF frontend |
| Other radios | Linux rfkill, udev serial candidates, 802.15.4/NFC/UWB/WWAN sysfs classes | Host-owned interface presence, RF family | Identity/capabilities often unknown until a driver is installed |
| RadioLib firmware | **Explicitly allowlisted** serial bridge | Chipset, features, RX/TX modes returned by cooperative firmware | RadioLib is a firmware **library**, not an OS-identifiable device class |

SoapySDR `--find` is opt-in because some installed SDR modules probe local
network devices or can hang in vendor discovery. Timeouts bound the child
process runtime, though a broken native driver may require OS-level mitigation.
The `radioLibPorts` list is opt-in because opening embedded serial ports can
reset microcontroller firmware and generic probing can disrupt other equipment.
Only a device responding with `SYN-RADIOLIB/1 {JSON}` is recognized as a RadioLib
firmware bridge. It must report `library: "RadioLib"`, `chipset`, optionally
`rx`, `tx`, and `features`. Firmware must implement the query
`SYN-RADIOLIB/1?\n`; no assumption is made that unmodified RadioLib programs
support this protocol.

## Configuration

Merge [`config.radio.example.toml`](../config.radio.example.toml) into the normal
Syn `config.toml`. Defaults: enabled, a 300-second rescan, passive sysfs/OS
queries, and no serial handshake or Soapy network/device probe. Startup uses
the existing node identity from `PositionRuntime` and serializes database writes
under the same per-node lock as position/clock observations. Native Agent and
operator processes record inventories when their position database is configured.
Legacy native services with no database continue their established startup,
but **cannot claim durable radio inventory or Events**.

`useCommands` allows bounded calls to `iw`, `btmgmt`, `system_profiler`,
`Get-PnpDevice` and `netsh`. Absent tools or insufficient permissions are
reported as unknown capabilities instead of invented support. No network peers
or Wi-Fi SSIDs are recorded. USB serial numbers and private PnP instance IDs
are SHA-256 hashed before entering records.

Inspect without a database (uses passive default settings):

```sh
uv run syn-radio probe
```

Inspect the node's persisted best-known radio inventory:

```sh
uv run syn-radio current --config config.toml
uv run syn-radio verify --config config.toml
uv run syn-radio record --config config.toml
```

Read-only API publication is intentionally not exposed without authentication,
as host hardware inventories and MAC/device identifiers may be sensitive. Use the
local CLI or add an authorized client that calls `RadioStore.current` directly.

## Provider extensions

Implement a class with a unique `name` and `scan() -> list[RadioDevice]`, and
pass it to `RadioDiscovery(..., providers=(...))`. Each result must include an
OS/bus provenance; false reports are rejected before persistence. A complete
scan can delete previously observed devices from *that provider only*. Provider
failures and truncation preserve old entries rather than falsely declaring a
receiver unplugged. There is no transmission permission or generic register
access in this interface.

## Verification and deployment

No new Alembic migration is necessary: Types are seeded transactionally during
scan and native `Thing`, `Event`, `Ember`, `eventTargets` and `MessageStore` tables
are reused. The existing position-aware migrations must already be installed.
SQL writes and ledger appends are performed in one transaction. If an append
fails the whole scan rolls back. `RadioStore.verify(node_ref)` checks hash-chain
continuity, primitive object content hashes, inventory/descriptor relationships
and the current canonical pointer. For strong rollback resistance, pin ledger
heads externally as documented in the position subsystem.

Tests use the actual Syn message hash implementation with disposable SQLite
projections and mocked OS outputs; physical devices, driver permissions, and
PostgreSQL production deployment remain separate qualification steps.
