# Clock awareness and first-run operator setup

Incremental patch for Synsphere/syn, feature/position-geodetic at
`f4504f3d8e1c5e35a9a69e3c58f84ef6229aeafc`.

## Apply and start

Stop the node and back up the database before applying. Apply from the repository
root, to the branch with the position-awareness CI fixes already included:

```sh
git apply --check /path/to/syn-clock-awareness.patch
git apply /path/to/syn-clock-awareness.patch
uv sync --extra position-native
uv run syn-server
```

This patch does not add package dependencies or change the lockfile. It reuses
`ProcessScheduler`; no parallel timer service or second scheduler is introduced.

`uv run syn-server` now runs operator mode by default. A missing operator.json
starts interactive operator setup before the API starts. Explicit confirmation
is required before generating a NEW identity. Existing configuration is validated
and reused; corrupt/partial provisioning is not silently overwritten. Setup uses
the existing native operator initializer and OS keystore, not a new wallet format.
The default operator state location is the native home directory under
`.local/state/syn/operator`. Select an existing identity with `--operator-config`
or `SYN_OPERATOR_CONFIG`. `--api-only` explicitly opts out of operator mode.

If config.toml is missing, setup uses SYN_DATABASE_URL or prompts for an existing
PostgreSQL connection URL with hidden input, writing a private config file.
It does not install PostgreSQL, create a PostgreSQL role or bypass authentication.
Existing config files are never overwritten. A noninteractive first start reports
that setup must be performed from a terminal rather than silently running unprovisioned.

With database.autoMigrate=true, normal createEngine migration runs install the
new schema. With automatic migration disabled, apply migration 20261117_0018
through the repository's usual Alembic procedure before starting. The preceding
position migration 20261117_0017 is required. The migration refuses destructive
downgrade because retained evidence is append-only.

On Windows, default operator mode hands off to an installed WSL distribution
using a separate `.venv-wsl`. It requires uv and the existing native keystore
prerequisites inside WSL, plus PostgreSQL connectivity from WSL. It does not
install these automatically. Linux/macOS use the native operator service directly.
No real Windows/WSL/keystore qualification was performed in this build environment.

## Clock semantics

Each new runtime session begins at UTC correction 0 ms, with precision and
uncertainty null (unknown), even when previous history exists. Each session reset
is itself ledger-recorded. Host clock resolution is a separate field; a
nanosecond API does not establish nanosecond UTC accuracy.

    estimated UTC = raw host realtime + utcOffsetMs

The correction is internal to Syn. Nothing changes the operating system clock,
RTC registers, PTP servo, chrony configuration or a receiver's settings.

The estimator maintains bounded per-source samples, linear offset drift in ppm,
detrended sample jitter, source errors and freshness. It chooses the freshest
eligible source with the smallest aged uncertainty rather than pretending
correlated sources are independent. Conflicting offsets inflate uncertainty.
Stale references enter holdover with growing uncertainty. Wall/monotonic
clock-pair discontinuities invalidate earlier correction samples. Older samples
do not move the anchor backwards. Job session identifiers fence late work from a
previous runtime. Scheduler ownership is acquired before session initialization.

## Sources and limitations

* NTP: independent recurring jobs for every configured server, regardless of
  GPS/PTP availability. Defaults are three pool hostnames, every 64 seconds.
  Four-timestamp SNTP calculation, origin/mode/stratum/leap/length validation,
  clock-step checks, bounded network reads, era unfolding and server/root delay
  uncertainty. This is not NTS; network time is unauthenticated and not a security
  trust root. Kiss-of-death responses are rejected; normal configured cadence is
  retained. Use servers you are permitted to poll.
* GPS: reuses existing serial/gpsd/network/ModemManager/BLE position readings
  without competing for the same serial port. UTC timecode, device identity and
  satellite metadata are retained. Ordinary position timing needs three distinct
  stable 3D fixes and includes a conservative, configurable transport/error budget.
  Separate gpsd PPS/TOFF timing messages use paired reference/system timestamps.
  Receiver-reported UTC validity is not an independent leap-second attestation.
* chrony: read-only `chronyc -n tracking`, refusing unsynchronised/local-clock
  results. Uses the raw-system correction sign, root distance and query latency.
* Linux PTP: discover/read `/dev/ptpN` through the PHC dynamic-clock interface.
  An optional configured `pmc` socket queries the matching linuxptp instance for
  time properties and grandmaster health. Syn does not implement a PTP servo or
  install/start ptp4l/phc2sys. UTC/TAI conversion requires known timescale and
  valid conversion metadata. Absolute error must be supplied by calibration;
  the PHC interface's resolution is not used as its error bound.
* Linux RTC: discover/read `/dev/rtcN` through RTC Class drivers, including
  kernel-managed SPI and I2C devices. Records model/bus, resolution, voltage
  validity when supported, and dated calibration/oscillator drift. Unknown UTC
  convention/calibration is not treated as precise time. No blind I2C/SPI scans,
  chip-register writes or device binding operations are performed.
* Linux PPS: records kernel assert timestamps and period jitter. An unpaired
  pulse alone cannot establish absolute UTC or replace the canonical offset.
* External: trusted local producers can submit validated `ClockReading` objects
  to ClockStore.observe. There is no unauthenticated remote clock-setting API.

These are estimates, not metrological certification, spoofing detection, or a
safety-grade time service. Real hardware and upstream CI remain unqualified.

## Persistence and inspection

`nodeClocks` contains the current canonical estimate Ember and estimator state per
node. `clockRecords` holds immutable, hashed snapshots and receipt references.
Native `events`, `embers`, `eventTargets`, `types`, `typeParents` and `typeProperties`
are used, not replacement clock-only primitives. Every reading creates an Event,
a source-statistics Ember and a canonical estimate Ember. Initialization and host
clock steps are Events. Error/failure observations remain in history.

All records and pointer updates are committed atomically to the SAME node
workspace hash ledger as position observations (`syn_communication_messages`).
Receipt content hashes, chaining, state hashes and native primitive contents are
verified. PostgreSQL triggers enforce record immutability and matching receipts.
SQLite fixtures only test logic; they do not qualify these PostgreSQL triggers.

On startup the server prints setup/database progress, then the clock logger emits
node reference, initial zero offset/unknown precision, estimate Ember and NTP list.
The API is not advertised before awareness initialization and authenticated native
operator readiness. Operator/scheduler failure fences API requests.

Inspect from SQL:

```sql
SELECT "nodeRef", "sessionId", "emberRef", revision FROM "nodeClocks";
SELECT kind, count(*) FROM "clockRecords" GROUP BY kind;
SELECT channel, count(*) FROM syn_communication_messages GROUP BY channel;
```

Or use the module CLI with the node reference from the startup log:

```sh
uv run python -m syn.clocks status --config config.toml --node-ref syn://thing/node-...
uv run python -m syn.clocks verify --config config.toml --node-ref syn://thing/node-...
```

See config.clock.example.toml for tuning. Keep any database credentials private.

## Protocol references

- RFC 5905: https://www.rfc-editor.org/rfc/rfc5905
- Linux PTP management client: https://www.linuxptp.org/documentation/pmc/
- Linux RTC Class interface: https://docs.kernel.org/admin-guide/rtc.html

## Test scope

The accompanying validation report records actual executed checks and their
limitations. Tests include estimator drift/jitter/holdover/steps, local UDP NTP,
ledger verification and transactional rollback, old-session fencing, restart
history, startup prompting/cancellation and actual server main control flow with
mocked API, feed-handler and native-custody boundaries. PostgreSQL acceptance,
physical GPS/PTP/RTC, native OS keystore and complete installed-package CI were
not qualified. No claim is made that the earlier reported 187-test run is part
of this independently rebuilt patch's validation.
