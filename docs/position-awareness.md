# Node position awareness

Implementation patch for `Synsphere/syn` at
`4c43e3adbf78060446c47fcbd0b86c2e05c26366` (master, 13 November 2026).
This supersedes the earlier standalone `src/syn/position.py` foundation.

## Installation and deployment

Apply the patch at the repository root after reviewing/backing up local changes:

```sh
git apply --check syn-position-awareness.patch
git apply syn-position-awareness.patch
uv lock
uv sync --extra position-native
uv run alembic upgrade head
uv run syn-server --config config.toml
```

The patch changes `pyproject.toml`, **not `uv.lock`**. The authoring environment
could not resolve/download dependencies. Regenerate and review the lockfile in
the target checkout before using `uv sync --locked`, publishing a release or
claiming reproducible-build qualification. The optional native extra installs
BLE plus the applicable OS bridge. Serial/gpsd/bootstrap are in the base install.
No hardware libraries are imported or devices opened merely by importing the
position package.

The new migration is `20261117_0016`, after `20261024_0015`. It installs ontology,
normalized position state, immutable history and durable schedule tables. Back up
the database before upgrading. It deliberately refuses an automatic downgrade
that would erase audit history; archive/export and review a rollback separately.
The migration must be qualified on PostgreSQL before production deployment.

`config.position.example.toml` describes every setting. Merge `[position]` into
the existing API configuration. Defaults enable discovery and always create a
geographic prior, including when no receiver/OS permission is available.

**Native operator and Agent deployment change:** their shared service lifecycle
now requires access to the same PostgreSQL semantic core used for Events, Embers
and receipts. Supply `SYN_POSITION_CONFIG=/private/path/position.toml`, place
`position.toml` alongside `operator.json`/the Agent configuration, or provide
`SYN_DATABASE_URL` for an already-migrated database. A current-directory
`config.toml` is also recognized. Protect configuration credentials with native
file permissions; restricted Agents need only their intended database access.
No private wallet configuration, signed readiness fields or custody profile is
rewritten. Missing database configuration or migration fails startup rather than
advertising a node whose required position history was never persisted.

Initialization/maintenance commands do not open hardware. Position bootstrap and
scheduler startup occur on the first actual `serve`/API service start, before the
node advertises readiness. The Agent inherits this integration through the
existing `OperatorServer.serve` lifecycle. Integration users constructing
`createApp` directly must supply `positionRuntime`; the official `syn-server`
entrypoint does so automatically. Existing embedding calls without that keyword
retain their old API-only behaviour.

The API keeps its existing `default` scheduler scope for compatibility with old
unbound Jobs: only one API dispatcher may own that scope in a database. Native
nodes use separate stable node-scoped scheduler keys. Multiple processes must not
claim the same scheduler key or explicit nodeRef. Separate API nodes needing their
own independent dispatchers must instantiate `ProcessScheduler` with distinct
keys and deliberately assign any pre-existing unbound work.

## Semantic objects and arbitrary spaces

```
syn://type/ember
  syn://type/space-position-estimate
    syn://type/geographic-geodetic
      syn://type/gps-reading          (also a subtype of event)
      syn://type/os-location-reading  (also a subtype of event)
    <one distinct concrete subtype per additional coordinate frame>
```

The type is named **`geographic-geodetic`**. Its representation is
**WGS84 geodetic latitude and longitude in decimal degrees**, with altitude in
metres and an explicit altitude datum. These are not Cartesian X/Y/Z values;
no coordinate conversion is needed for this terminology correction. The geographic space is
`syn://thing/space-wgs84-geodetic`. Unknown altitude is null, not sea level.
Altitude datum is `ellipsoid`, `mean-sea-level`, or explicitly `unknown`.

### Upgrading the earlier geographic-cartesian identifier

Stop position services, apply the follow-up rename patch (or install this complete
replacement patch on the clean upstream base), and run `uv run alembic upgrade
head` before restarting. Do not apply the complete patch over the earlier full
position patch. No new dependencies are introduced by the rename.

Revision `20261117_0017` installs the canonical `geographic-geodetic` Type,
its properties, and the GPS/OS-reading subtype edges. The original `0016`
migration stays byte-for-byte unchanged. Its old `geographic-cartesian` Type is
retained only as a deprecated historical alias inheriting `geographic-geodetic`.
New estimates and frame definitions use the corrected identifier.

The upgrade changes only the type identifier in the existing WGS84 frame's
metadata, not the frame's reference, axes, units, metric, datum or coordinates.
This narrowly scoped metadata correction runs under a PostgreSQL exclusive table
lock in the Alembic transaction; ordinary frame immutability is restored before
commit. Unexpected legacy definitions are rejected instead of silently changed.
No historical Event, Ember, position snapshot or hash-chain receipt is edited.

At node startup, a legacy canonical position is replaced by a **new** correctly
typed Ember through the normal transactional ledger path. The replacement links
to its predecessor and preserves its coordinates, uncertainty, observation epoch,
last-confirmed freshness and observation Event. Repeating the upgrade creates no
additional Ember. Other coordinate spaces are unaffected. The trusted local
`PositionStore.upgrade_geographic_type(node_ref)` method provides the same
operation explicitly; `bootstrap` and the next reconciled fix also handle it.
A read-only `current` call can still return a legacy record until one of those
upgrade paths runs, and old audit records intentionally retain the old identifier.

Every frame is a Thing of type `coordinate-space`, with an immutable definition:
axes, units, error unit, metric and a unique concrete position subtype. This keeps
coordinates in different reference frames from being accidentally compared.
There is no implicit transformation, axis swap or unit conversion. Changed axes,
origin/datum or units require a new frame and type. Named TypeProperties declare
the base, geographic and GPS fields; custom axes get their own declarations.

A node's canonical list is the normalized `nodePositions` relation, keyed by
`(nodeRef, spaceRef)`, referencing a native immutable Ember. `PositionStore.current`
returns a stable, space-sorted list. Historical Embers remain untouched when the
current pointer changes. An Event's native `eventTargets` entry identifies the
node that observed it. Coordinate frames and their Types are definitions, not
new ad-hoc replacements for Syn primitives.

Example trusted local producer:

```python
from datetime import UTC, datetime
from syn.positions import Estimate, Space
from syn.positions.store import PositionStore

store = PositionStore(sessions)  # Existing SQLAlchemy session factory
frame = Space(
    "syn://thing/studio-frame-v1", "syn://type/studio-frame-v1-position",
    ("x", "y", "z"), ("metre", "metre", "metre"), "metre", "euclidean",
    "Origin at the surveyed studio doorway; Z points up",
)
store.register_space(frame)
store.put(node_ref, Estimate(
    frame.reference, frame.typeRef, {"x": 2.5, "y": 1.0, "z": 0.8},
    error=0.2, errorUnit="metre", observedAt=datetime.now(UTC),
    source="studio-tracker", uncertaintyMethod="tracker-reported-radius",
))
positions = store.current(node_ref)
```

The node must already have been bootstrapped by its runtime. Writer APIs are local
trusted code, not newly exposed unauthenticated HTTP operations.

## Uncertainty, validity and reconciliation

Estimates retain `error`, `errorUnit`, `uncertaintyMethod`, optional
`confidenceLevel`, optional vertical error and source provenance. Geographic
estimates additionally expose latitude, longitude, altitude and `errorMeters`.
Unknown accuracy is null; zero is a real reported value, not a missing-value
sentinel. Confidence level is only supplied when its interpretation is known.
There is no invented universal “95% GPS accuracy”. Numbers must be finite;
booleans, NaN, infinity, negative error and out-of-range coordinates are rejected.

Every decoded position observation, including no-fix and stale readings, creates
an Event. Transport noise and malformed/checksum-failing records do not create
fictional GPS fixes. An identical instrument sample re-delivered at a different
arrival time is idempotent; altered telemetry or coordinates are a distinct
observation. Events contain model/serial as reported when available, locked and
visible satellite counts, instrument timecode, received time, fix status and
available telemetry. OS USB product strings are metadata, not proof of a
receiver's hardware model or identity.

A fresh usable observation replaces the timezone prior. Older epochs, excessive
future times and stale observations do not replace the canonical belief. A
less-precise observation at essentially the same place does not degrade a better
estimate. Uncertainty ages using the configured motion allowance, so an older
precise reading cannot freeze a moving node forever. A changed position outside
that allowance can supersede it. Same-epoch incomplete data does not erase a
known altitude. Improved error or changed position/datum produces a new Ember.
An unchanged fresh observation creates an Event and a ledger-recorded freshness
confirmation without manufacturing another identical Ember.

The stored error is the source's error at observation time; `lastObservedAt` is
separate, and the motion allowance used for selection is not a promise that the
node is stationary. This is not Kalman fusion, spoofing detection, hardware
attestation or a safety-rated navigation system. Arbitrary spaces use the same
freshness and precision rules but have no fabricated geographic distance metric.

## First-start timezone prior

The runtime resolves the system IANA timezone, including common tzdb aliases,
using `TZ`, `tzlocal` and OS timezone files. It loads geographic polygons from
`timezonefinder` and city/capital data from `geonamescache`, entirely offline.
Installed dataset versions and selection provenance accompany the initial Ember.

Where a timezone contains a national capital, the most populous matching capital
is selected deterministically. Otherwise it uses the timezone's named reference
city, then the largest city in that timezone. A timezone does not always contain
a national capital, and more than one can share a region; these choices are
explicit policy rather than a claim that the timezone standard defines a capital.

The uncertainty radius is half the timezone's west–east span, converted to metres
along the WGS84 parallel at the selected city. The span is the smallest longitude
arc containing the exterior polygon vertices, handling disconnected regions and
the antimeridian. It is a heuristic, not a circle guaranteed to enclose all parts
of an irregular timezone, and says nothing about north–south extent.

Fixed offsets such as `Etc/UTC`, missing geometry/city data, unknown zones or an
unavailable catalogue cannot locate a host. These still produce an explicitly
labelled London fallback with approximately half-Earth-circumference uncertainty,
not a falsely precise London inference. All bootstrap altitudes remain unknown.
Missing datasets are visible in the prior's provenance. Restart retains and
verifies existing canonical state instead of resetting a GPS-derived position.

To inspect and pin the actual priors for a deployment:

```sh
uv run python scripts/buildPositionCatalogue.py Europe/London Europe/Paris --output timezone-catalogue.json
```

Set `position.timezoneCatalogue` to that reviewed file. `--all` builds available
geographic zones and reports missing ones. It will not silently overwrite a
previously reviewed catalogue. Dataset updates may change city populations,
boundaries or selections; retain source attribution and review upstream dataset
licence requirements when redistributing a precomputed catalogue.

## Discovery and provider coverage

| Provider | Implementation | Boundaries |
|---|---|---|
| gpsd | Local/configured numeric-IP endpoints; DEVICES, WATCH, TPV and SKY | Physical receiver protocols depend on gpsd's installed drivers. Ports claimed by gpsd are not also serial-probed. |
| Serial | OS pySerial enumeration; USB serial, UART, COM and connected RFCOMM; passive NMEA at configured baud rates | No vendor binary commands, pairing or device reconfiguration. Explicit allowlist/exclusions; POSIX exclusivity. |
| Linux ModemManager | D-Bus device/GPS capability discovery; already enabled NMEA/raw location | Does not enable GPS, change modem state or bypass permissions. Owned modem ports excluded from serial probing. |
| Windows | WinRT Geolocator, accuracy and available satellite metadata | Satellite source becomes GPS Event; Wi-Fi/cellular/etc. remain OS-location Events. OS permission required; remote redirected fixes rejected. |
| macOS | Authorized Core Location requests with bounded run loop | Does not force permission prompts. Core Location's mixed source is not mislabeled GPS. |
| Bluetooth LE | Standard Location and Navigation service, metadata characteristics and notifications | No proprietary UART profiles, pairing or control-point writes. Valid/estimated/last-known status distinguished; GPS origin not assumed. |
| Network NMEA | Explicit TCP receiver or locally bound UDP listener | Numeric IPs only; no arbitrary network/subnet scans. |

NMEA processing handles RMC/GGA and time/error supplements GSA/GST/ZDA, validates
checksums, separates fix validity from satellite visibility, handles midnight and
prevents stale altitude/error metadata from leaking across epochs. HDOP × UERE is
labelled a heuristic, not receiver-reported metres; configure UERE appropriately.
The implementation records available satellite totals, not a complete GSV sky map.

All discovery/read attempts have time and byte/record bounds. Individual provider
failures are diagnostic, not a reason to suppress other providers. Unconfirmed
serial candidates are tested on the discovery cadence; confirmed receivers use
the polling cadence. Enumeration errors retain retry schedules; successful
absence disables them. Removed/disabled/excluded providers are rechecked at job
execution time so queued polls cannot override operator configuration.

“Comprehensive probe” does not mean universal driver support. Raw proprietary
USB devices require gpsd/a platform driver; vendor-specific binary/BLE protocols
are not guessed. Unknown model/serial/accuracy fields remain null. Opening serial
ports can still cause a driver RTS/DTR pulse despite setting both false before
open: exclude control equipment or sensitive consoles. No serial writes are
performed. Bluetooth/OS permission and platform sandbox rules remain in force.
WSL can only see devices forwarded to it; it cannot transparently access the
Windows host's location service.

## Existing scheduler integration

`position-discover` and `position-poll` are ordinary registered handlers executed
by Syn's **existing `ProcessScheduler` and asyncprocessscheduler backend**.
`jobSchedules` adds durable fixed-delay recurrence; `jobDispatch` binds ordinary
Jobs to their node/scheduler. There is no competing hardware polling daemon or
second scheduling implementation. Native services host the same scheduler in an
owned event-loop thread because their socket lifecycle is synchronous.

A recurring schedule has at most one unfinished Job. Missed intervals coalesce;
a successful read schedules the next run after completion, not at a claimed exact
wall-clock sampling frequency. Failures back off exponentially with a cap. Hotplug
rescans continue. PostgreSQL advisory ownership prevents two schedulers from
recovering/dispatching the same scope; receiver locks also prevent overlapping
local node reads. A crashed scope recovers its own running Jobs only. Sample
idempotence handles a crash after an observation committed but before Job success
was recorded. Cancellation/termination and shutdown use the existing execution
backend. Cancelling a discovered schedule is temporary until rediscovery;
permanent suppression belongs in provider/port configuration.

Configure sensible database connection/statement timeouts for the deployment.
The scheduler fences itself when its ownership connection fails. These lock and
multiprocessing behaviours still require real PostgreSQL and installed-backend
qualification, not just the isolated tests.

## Existing ledger and transactional integrity

`MessageStore.appendInTransaction(session, workspace, body)` exposes the existing
communication receipt chain inside the caller's database transaction; existing
`append()` delegates to it. Receipt fields and canonical hash protocol do not
change. Positioning does not invent a separate incompatible hash-chain format.
The node's ledger workspace is deterministic UUID5 over
`"syn-node-ledger:" + nodeRef`; records use channel `node.position`.

A change transaction includes the native reading Event, matching hashed snapshot,
replacement native Ember, its hashed snapshot, canonical pointer/revision and a
canonical-update receipt. Any failure rolls all of it back. Node locks precede
ledger-head locks. An unchanged observation records its Event and freshness
receipt. The canonical list is normalized; historical snapshots do not mutate.

The migration adds PostgreSQL protections against changes to indexed position
Events/Embers, snapshots and frame definitions. Deferred constraints require
matching ledger receipts for records and pointer/freshness updates. A local
verifier checks chain linkage, hashes, receipts, native object values and canonical
state. Startup currently verifies retained history; large deployments should plan
for its growing audit time and storage cost before selecting high sample rates.
There is no silent retention/truncation job.

These are integrity checks, not distributed consensus, encryption or resistance
to a privileged attacker restoring a whole older database. Retain independently
trusted head checkpoints/backups for rollback detection. Existing generic
primitive API and database permissions still govern visibility; this patch does
not make location secret from those interfaces. No location is automatically
published to P2P, and no new unauthenticated location HTTP route is added.

Inspect from a trusted shell:

```sh
uv run syn-position --config config.toml current
uv run syn-position --config config.toml providers
uv run syn-position --config config.toml verify
# For a native service, use its explicit nodeRef (or set it in position.toml):
uv run syn-position --config position.toml --node-ref syn://thing/my-native-node current
```

## Verification and remaining qualification

The patch includes isolated tests for coordinate validation, timezone geometry,
NMEA/gpsd/BLE decoding, synthetic TCP and serial transports, arbitrary frames,
canonical state, idempotence, history tamper detection, transaction rollback,
provider enable/disable, startup/restart and real scheduler control flow. SQLite
projections are only test fixtures, **not a production fallback**. Scheduler tests
replace the child-process executor with a deterministic stand-in; ledger tests
use the actual MessageStore and receipt protocol.

```sh
uv run pytest tests/positions
# Separate qualification against an expendable PostgreSQL database:
SYN_POSITION_TEST_DATABASE_URL='postgresql+psycopg://user:password@localhost/syn_test' \
  uv run pytest tests/positions/test_postgres.py
```

The PostgreSQL tests run the full Alembic chain in an isolated randomly named
schema, test required ancestry, immutable/deferred constraints, concurrent sample
deduplication and exclusive scheduler ownership. The role needs schema DDL rights.
Without the explicit URL these tests are **skipped**, never silently simulated.

The delivery report records what actually ran. Full upstream regression tests,
real PostgreSQL migration/trigger execution, native Windows/macOS/ModemManager/BLE
hardware and actual asyncprocessscheduler child-process runs were not available
in the authoring environment. Run those before production rollout. This is an
implemented integration patch with explicit qualification gates, not a claim
that every host/device combination has been tested.

## Primary implementation references

- Existing Syn scheduler, models, API/native lifecycles and communication ledger at the pinned commit above.
- PROJ geodetic/Cartesian definitions: https://proj.org/en/stable/operations/conversions/cart.html
- GPSD JSON protocol: https://gpsd.io/gpsd_json.html
- pySerial API/port enumeration: https://pyserial.readthedocs.io/en/latest/pyserial_api.html and https://pyserial.readthedocs.io/en/latest/tools.html
- TimezoneFinder geometry API: https://timezonefinder.readthedocs.io/
- GeoNamesCache source/data catalogue: https://github.com/mr-clean/geonamescache
- ModemManager D-Bus location interface: https://www.freedesktop.org/software/ModemManager/api/latest/gdbus-org.freedesktop.ModemManager1.Modem.Location.html
- Windows Geolocator: https://learn.microsoft.com/en-us/uwp/api/windows.devices.geolocation.geolocator
- Apple Core Location: https://developer.apple.com/documentation/corelocation
- Bluetooth SIG Location and Navigation / GATT specifications: https://www.bluetooth.com/specifications/specs/


### Legacy native socket compatibility

Native Agent/operator socket services retain their original availability when
no position database has been configured. Position Embers, GPS events and scheduled
polling require `SYN_POSITION_CONFIG`, `SYN_DATABASE_URL` or an adjacent
`position.toml`. Unconfigured legacy native nodes are not fully position-aware.
An invalid explicit positioning database continues to fail readiness.
