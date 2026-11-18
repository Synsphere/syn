# Bluetooth beacon generation schema

Status: **v1 generation contract**, with a machine-readable JSON Schema and
offline examples. The [native Bluetooth runtime](bluetooth-runtime.md) now
implements scheduled, Syn-branded advertising and reception logging for a
bounded set of formats. This document also describes the broader generic
compiler contract; raw definitions, arbitrary extensions, encrypted Eddystone
and controller-specific extended/periodic procedures are not executed by that
runtime merely because they validate against this schema.

The [JSON Schema](../schemas/bluetooth-beacons.schema.json) uses Draft 2020-12
and the identifier `syn://cog/bluetooth-beacons-schema-v1`. It validates the
`knowledge` payload of a native Syn object, not its complete primitive envelope.
Native identity, `typeRef`, ownership, content hashes and relationship tables
continue to use the existing Syn model. The schema identifier is a proposed
identifier, not a claim that a corresponding Cog already exists in a database.

## 1. Capability-based selection

A beacon format is an encoding of advertising data. The adapter and its OS
backend provide the means of broadcasting that data. Syn determines whether a
particular definition can run by combining:

1. A locally registered encoder for the requested payload profile.
2. A fresh capability snapshot of the selected controller and backend.
3. An encoded payload that fits the selected advertising procedure.
4. Current connection, power, access and advertising-resource availability.

The existing [radio inventory](../radio-awareness.md) identifies interfaces and
stores their evidence. Its `canTransmit` value and feature strings are discovery
hints. They do not prove that a usable advertising API, particular PHY, periodic
advertising or a sufficient payload budget is available.

`supported`, `unsupported` and `unknown` are distinct capability states. An
unknown required capability produces an unresolved target until it can be
established. Missing values must never be converted to positive support.

Named profiles cover the common beacon families. Ordered raw GAP advertising
data and registered encoders accommodate other vendor formats. Representing a
payload does not itself implement a protocol's authentication, key rotation,
response schedule or external service.

### Syn identity is mandatory

Every beacon definition contains this closed identity policy:

```json
{
  "identity": {
    "branding": "Syn",
    "emitter": "nodeRef",
    "marker": "every-frame",
    "fullReference": "required-per-sequence",
    "shortHints": "allowed-with-full-reference",
    "authentication": "advertised-claim"
  }
}
```

The emitter is the enclosing definition's canonical `nodeRef`. Every encoded
frame must contain a recognisable Syn marker, and the sequence must include a
transport for the complete reference. A local-name label alone, an unrelated
vendor namespace, or a short fingerprint without that transport is insufficient.
Short hints remain unauthenticated hints even when the containing format is
otherwise a standard iBeacon or Eddystone frame.

JSON Schema checks the explicit policy. The semantic compiler must also check
that the encoded bytes honor it. The generic catalogue examples are structural
examples, not automatic permission to emit unbranded vendor frames. Adding this
policy object cannot make arbitrary raw bytes compliant. The concrete service
and iBeacon identity examples contain the full sequence and pinned hashes; the
[native runtime guide](bluetooth-runtime.md) specifies their wire formats.

## 2. Native Types and records

The following Types describe the generic compiler contract. The native runtime
seeds the beacon, generation-event and advertising-observation Types, plus
transmission, reception and lifecycle subtypes described in its guide. The
definition, capability, generator and plan projections below remain proposed;
no new Alembic migration is needed for the implemented native objects.

| Proposed Type | Parent | Role |
| --- | --- | --- |
| `syn://type/bluetooth-beacon` | `syn://type/thing` | Stable identity of one logical beacon |
| `syn://type/bluetooth-beacon-definition-ember` | `syn://type/ember` | Immutable definition revision |
| `syn://type/bluetooth-advertising-capability-ember` | `syn://type/radio-capability-ember` | Fresh advertising evidence for one controller/backend binding |
| `syn://type/bluetooth-beacon-generator` | `syn://type/cog` | Generator that compiles and executes definitions |
| `syn://type/bluetooth-advertising-plan-ember` | `syn://type/ember` | Immutable resolution of a definition against adapter evidence |
| `syn://type/bluetooth-beacon-generation-event` | `syn://type/event` | One requested operation or lifecycle transition |
| `syn://type/bluetooth-advertising-observation-ember` | `syn://type/observation-result` | Evidence of a preparation, start, stop, rejection or disconnection |

The root schema accepts four payload profiles:

| Profile | Contents |
| --- | --- |
| `syn.bluetooth-advertising-capabilities/1` | Inventory identity, live binding, controller/backend capabilities and availability |
| `syn.bluetooth-beacon/1` | Beacon reference, revision, adapter selection, transport, payload frames and scheduling |
| `syn.bluetooth-advertising-plan/1` | Pinned definition and capability evidence, per-adapter outcome, effective parameters and compiled bytes |
| `syn.bluetooth-advertising-observation/1` | Execution evidence associated with a plan, beacon, node and Event |

The beacon Thing may point to its current definition Ember. Reconfiguration
creates another immutable Ember and revision. The generator Cog has its own
identity; each invocation has an Event identity. References in portable payloads
must also be represented through native `eventTargets` and object relationships
where appropriate. `TypeProperty.propertyTypeRef` retains its existing meaning
and does not become a JSON Schema or Ember-type lookup mechanism.

## 3. Identifying connected adapters

The current inventory does not materialize a separate Thing for every adapter.
Use the established identities:

- `nodeRef`: the observing Syn node Thing.
- `inventoryRef`: the node's radio inventory Thing.
- `deviceId`: the inventory's provider-local interface identifier.
- `capabilityEmberRef`: the original radio descriptor Ember.
- `capabilitiesRef`: this new immutable advertising-capability snapshot Ember.
- `binding.backend` and `binding.adapterLocator`: the advertising backend and
  its current controller handle.
- `binding.connectionEpoch`: a generation token changed after disconnect,
  reconnect, controller replacement or backend-handle recreation.
- `binding.controllerKey`: a node-local correlation key for deduplicating
  different discovery records of the same controller.

`RadioDevice.id` hashes `[provider, transport, path]`. It is not a permanent
hardware identity: a reused `hci0` index can refer to a different USB device.
Bind a prepared plan to its connection epoch, and recheck that binding before
starting. Never silently substitute a new controller under an old plan.

The backend must reconcile duplicate records, such as a Bluetooth interface and
its rfkill record. If it cannot establish that a candidate corresponds to a
distinct controllable adapter, keep it unresolved rather than starting a second
advertiser. Inventory entries retained after a provider error are not proof of
current attachment. Freshness uses `observedAt` and `validUntil`; timestamps must
be ordered and checked against the node's current clock when resolving a plan.

### Selection policies

`all-compatible` creates one target for every distinct connected adapter that
can run the complete definition. `first-compatible` chooses one deterministically
from eligible targets. `explicit` resolves the supplied inventory device IDs on
the specified node. The compiler reports excluded or unresolved candidates with
reasons, including exhausted capacity, unsupported PHY, stale evidence, missing
encoder or an OS API that cannot address the requested controller.

`unavailable` selects `wait`, `skip` or `fail`. It does not permit lowering the
requested protocol, truncating payloads or replacing an explicit adapter.
Availability is evaluated again at execution because capability discovery is not
a reservation. An empty candidate set produces `targets: []` and a plan-level
reason, without inventing an adapter ID. A whole-definition prepared/rejected
observation uses a null `deviceId`. Wait/retry behavior must be bounded and
cancellable by the caller.

## 4. Payload formats

The `frames` array is ordered. Each frame has a unique `frameId` within its
definition and a discriminated `payload`. All byte strings use lowercase,
even-length hexadecimal. Integers have explicit byte widths and units; schema
validation rejects unrecognized fields in the built-in structures.

| Payload format | Required information | Encoding contract |
| --- | --- | --- |
| `ibeacon` | UUID, major, minor, measured RSSI at 1 m | Apple's manufacturer payload; big-endian major/minor |
| `altbeacon` | Manufacturer ID, 20-byte beacon ID, reference RSSI, reserved byte | AltBeacon marker and manufacturer-specific payload |
| `eddystone-uid` | 10-byte namespace, 6-byte instance, calibrated power at 0 m | UID frame, including two zero reserved bytes |
| `eddystone-url` | HTTP(S) URL, calibrated power | Standard Eddystone URL compression; encoded remainder must fit 17 bytes |
| `eddystone-tlm` | Battery millivolts, temperature, advertisement count, uptime | Unencrypted telemetry; explicit unavailable-temperature representation |
| `eddystone-eid` | Local credential reference, counter/clock binding, rotation exponent, calibrated power | EID generated by the registered cryptographic encoder |
| `eddystone-etlm` | Telemetry plus EID credential/clock association | Encrypted telemetry generated with the required salt and integrity field |
| `gap` | Ordered AD structures | Explicit AD type and value bytes; no map that collapses repeated types |
| `manufacturer-data` | Company ID and vendor bytes | AD type `0xff`, little-endian company ID |
| `service-data` | UUID width/value and bytes or a Syn URL template | AD types `0x16`, `0x20` or `0x21` with the corresponding UUID prefix |
| `extension` | Encoder Type reference, schema reference and parameters | A locally registered encoder validates and compiles its own profile |

The exact JSON property names and accepted ranges are normative in the JSON
Schema. The [examples](../examples/bluetooth-beacons/) demonstrate each built-in
payload family and the four record profiles.

The wire sources are Apple's [iBeacon generation documentation][apple-beacon],
ST's [iBeacon layout][st-ibeacon], the [AltBeacon specification][altbeacon],
Google's [common Eddystone framing][eddystone-common] and its individual
[UID][eddystone-uid], [URL][eddystone-url], [plain TLM][eddystone-tlm],
[EID][eddystone-eid], [EID computation][eddystone-computation] and
[encrypted TLM][eddystone-etlm] specifications.

Eddystone frames include both the complete 16-bit service UUID list containing
`FEAA` and service data for `FEAA`. Frame-specific multi-byte integers are
big-endian; the service UUID prefix is little-endian. Telemetry must retain the
required association with its identifying frame. A conforming compiler checks
that association and address lifetime across a round-robin schedule.

EID/eTLM definitions contain local credential references, never secret key bytes.
The encoder resolves credentials through the local key facility and requires a
usable counter/timebase. Static random bytes are not a substitute for EID
generation. Telemetry values are explicit inputs: submission counts must not be
misreported as measured over-the-air transmissions. Support for these archived
wire specifications does not imply availability of a Google resolver service.

### Syn URLs

An ordinary `syn://...` URL is not valid Eddystone URL data because that profile
has HTTP/HTTPS scheme codes. The `service-data` profile provides a `syn-url-utf8`
template for a canonical Syn reference. Its compiler emits the actual UTF-8
bytes and includes the service UUID and AD headers in the payload budget.
Normalize supported `synny://` aliases to canonical `syn://` before constructing
and hashing the immutable definition; the schema accepts canonical references.

Use a project-owned 128-bit service UUID unless an appropriate short UUID has
been assigned. No Bluetooth SIG company ID or short service UUID is assigned by
this proposal. A full reference or 32-byte hash often requires extended
advertising. Do not silently replace it with a truncated identifier. A future
compact discovery profile can explicitly define a short hint and a resolution
mechanism, with collision and authentication semantics of its own.

## 5. Advertising procedures and scheduling

The v1 transport contract describes non-connectable, non-scannable broadcast
advertising. It does not introduce GATT services, pairing or a scan-response
publisher. Capability snapshots can record scan-response limits for later
extensions. The standard named beacon templates use legacy LE 1M; custom GAP,
manufacturer, service-data and extension profiles may request the other modes.

| Mode | PHY settings | Interval fields | Payload location |
| --- | --- | --- | --- |
| `legacy` | Primary LE 1M | Min/max in 0.625 ms units, 32 through 16,384 | Ordinary advertising data |
| `extended` | Primary LE 1M or LE Coded; secondary LE 1M/2M/Coded | Min/max in 0.625 ms units, 32 through 16,777,215 | Extended advertising data |
| `periodic` | Extended announcement plus a supported secondary PHY | Announcement interval above; periodic interval in 1.25 ms units, 6 through 65,535 | Periodic data with the required announcement/synchronization setup |

The compiler also applies narrower controller/backend constraints. It compares
minimum with maximum; these relationships cannot be enforced by ordinary JSON
Schema numeric bounds alone. Regular periodic repetition of a legacy packet is
not Bluetooth Periodic Advertising. See the Bluetooth SIG's [LE primer][primer]
and [HCI specification][hci] for the underlying procedures.

An ordinary `interval` of `null` explicitly delegates timing to the backend.
This allows APIs without a configurable advertising interval to participate.
`backend.intervalControl` records whether interval selection is supported;
concrete min/max requests require the corresponding control. Effective intervals
remain null when the backend cannot report them. The v1 `periodicInterval`
remains a concrete request because periodic synchronization needs that separate
parameter set.

`requestedTxPowerDbm` selects radio output power. Payload fields such as measured
RSSI at 1 m or calibrated power at 0 m are separate values. `txPowerPolicy`
selects exact power, an explicitly permitted nearest supported value, or the
controller default. The plan records effective values where the backend can
establish them; an accepted power preference is not proof of measured RF power.

`schedule` is either a single frame or explicit round-robin rotation with a dwell
time. Multiple frames consume one logical beacon schedule; multiple simultaneous
beacons require separately allocated advertising resources. A scheduler must not
claim simultaneous broadcasts by silently rotating one available set. Rotation
time, advertising interval and periodic interval are different quantities.

Bluetooth Classic discoverability/EIR, Mesh network beacons, PAwR and LE Audio
broadcasts have additional procedures or state. Raw bytes and an extension
schema can describe their data, but an installed protocol implementation and
explicit capability contract are still required. They are not automatically
implemented by this v1 broadcast contract.

## 6. Byte budgets and compilation

Each GAP AD structure serializes as one length byte, one AD type byte and its
value bytes. The length is `1 + value length`, so a single AD value has a maximum
of 254 bytes. Manufacturer values include a two-byte company ID, leaving at most
252 vendor bytes. Service values include a 2-, 4- or 16-byte UUID, leaving at
most 252, 250 or 238 bytes respectively. These structure limits are separate
from the aggregate advertising budget. See [GAP data encoding][gap].

The compiler sums every emitted structure, including flags, service lists and
other backend-inserted fields. Legacy advertising has a 31-byte data budget;
extended advertising can provide a larger controller-reported budget up to
1,650 bytes. Use the effective limit of the selected mode and backend, not the
largest theoretical Bluetooth limit. A legacy budget never becomes larger
because another adapter supports extended advertising.

Compiled plans retain exact encoded buffers containing the AD structures, plus
the encoder identity/implementation digest and requested/effective transport
parameters. If a backend
inserts or normalizes fields, its encoder must account for that transformation;
unknown final bytes remain unresolved. A backend's advertising start notification
does not verify that another radio received those bytes.

Each compiled frame records `generatedAt` and `validUntil`. The compiler bounds
the frame's validity by its capability evidence and, for rotating formats, the
next identifier epoch after allowing for clock uncertainty. A changed credential
revision or timebase invalidates the earlier encoding. The executor recompiles
or stops before expiry; it must never keep emitting stale EID/eTLM bytes merely
because the initial plan was compatible.

The definition and capability digests use the existing
`syn.radios.types.canonical` serialization: sorted JSON keys, compact separators,
ASCII escaping and no non-finite numbers, followed by SHA-256. Object-envelope
hashes remain governed by the native Syn primitive hash contract. Do not confuse
these payload digests with native object content hashes.

## 7. Resolution and execution

Schema validation precedes semantic compilation. A conforming compiler must:

1. Resolve the native references, pinned definition revision and capability
   Embers; verify their payload hashes and node association.
2. Reconcile live controller identities, deduplicate candidates and reject stale
   or changed connection epochs.
3. Find the installed encoder and validate extension parameters against its
   pinned schema. A definition cannot cause arbitrary code or a remote encoder
   to be loaded merely by naming a Type.
4. Resolve mode, PHY, AD access, power and timing requirements for each adapter.
   Required unknown evidence yields `unresolved`; a known incompatibility yields
   `rejected` with a specific reason.
5. Encode the complete payload, check combined byte budgets, profile pairing,
   duplicate AD-type rules, unique frame IDs and scheduling requirements.
6. Reserve the required resources and record the immutable plan before starting.
   Revalidate the reservation and current binding when execution begins.

`compatible` means the target has a valid compiled plan for the recorded
evidence. It does not mean the radio has started. Execution observations record
`prepared`, `started`, `stopped`, `rejected` or `disconnected` with their evidence
kind and backend details. Disconnect invalidates the active binding. Reconnect
requires fresh evidence and a new plan; it does not rewrite prior history.
`stopped` requires a confirmed backend outcome. A local stop intention is not
such evidence. `disconnected` requires inventory or backend evidence that
actually establishes loss of the selected binding.

The proposed persistence transaction creates the lifecycle Event and observation
Ember and appends the same observation to the existing node ledger and the
beacon's object ledger, with native Chronicle links. The actual hardware call
cannot participate in that database transaction. The implementation must record
intent, perform the operation, then record its observed outcome and reconcile
incomplete operations after a crash. It must not promise exactly-once RF emission.

## 8. Backend evidence

For Linux, [BlueZ's Advertising Manager][bluez-manager] provides a per-adapter
interface, advertising instance counts, supported includes/PHYs and reported
payload/power limits. [The advertisement interface][bluez-advertisement]
provides manufacturer data, service data and some raw AD access, subject to
property conflicts and platform restrictions. Query the installed version's
actual properties. Kernel capability without an accessible backend operation is
insufficient.

On Windows, [BluetoothAdapter][windows-adapter] exposes device enumeration,
advertising-related flags and data limits. The public
[BluetoothLEAdvertisementPublisher][windows-publisher] does not expose a
per-adapter selector and restricts certain AD fields. Model binding as
`explicit`, `default-only`, `system-managed` or `unknown`; enumerating several
USB controllers must not become several claimed independent publishers.

Apple's general [CoreBluetooth advertising API][apple-advertising] documents a
limited set of advertising fields, while iBeacon has a separate documented
generation path. Receiving manufacturer data in a scan result is not evidence
that the general advertising API can emit it. These restrictions are backend
capabilities, independent of the controller's Bluetooth version.

The capability examples are synthetic design fixtures. The native runtime
probes its actual backend at startup and on attachment changes, but the automated
validation used mocked OS boundaries; no physical adapter was RF-qualified.

## 9. Validation and implementation boundary

The schema checks reference syntax, required fields, discriminators, integer
ranges, byte-string widths, fixed profile relationships and closed objects.
The offline example validator exercises positive and negative structural cases.
It is not the production compiler or a hardware qualification tool.

JSON Schema cannot query controller attachment, reserve advertising slots,
prove a secret exists, check live timestamp freshness, verify transmitted RF or
establish correspondence between a claimed digest and another native object.
Those responsibilities belong to the compiler, backend and observation store.

The native runtime now implements a bounded encoder set, BlueZ and WinRT
backends, scan-only macOS support, per-adapter scheduling and atomic native
ledger observations. The remaining generic compiler work includes executing
arbitrary validated definitions through an encoder registry, emitting the full
capability/plan projections, and supporting additional backend procedures only
where measured platform capabilities allow them. The generic schema does not
claim those additional procedures are implemented.

## Sources

Checked on 18 November 2026. Protocol descriptions above use primary specification
and platform documentation. The Syn contracts and integration choices are this
proposal's design.

[primer]: https://www.bluetooth.com/bluetooth-le-primer/
[hci]: https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core_v7.0/out/en/host-controller-interface/host-controller-interface--hci--functional-specification.html
[gap]: https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-62/out/en/host/generic-access-profile.html
[bluez-manager]: https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisingManager.rst
[bluez-advertisement]: https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisement.rst
[windows-adapter]: https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.bluetoothadapter
[windows-publisher]: https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.advertisement.bluetoothleadvertisementpublisher
[apple-advertising]: https://developer.apple.com/documentation/CoreBluetooth/CBPeripheralManager/startAdvertising(_:)
[apple-beacon]: https://developer.apple.com/documentation/corelocation/turning-an-ios-device-into-an-ibeacon-device
[st-ibeacon]: https://www.st.com/resource/en/application_note/an5508-social-distancing-detection-using-bluetooth-low-energy-stmicroelectronics.pdf
[altbeacon]: https://github.com/AltBeacon/spec
[eddystone-common]: https://github.com/google/eddystone/blob/master/protocol-specification.md
[eddystone-uid]: https://github.com/google/eddystone/blob/master/eddystone-uid/README.md
[eddystone-url]: https://github.com/google/eddystone/blob/master/eddystone-url/README.md
[eddystone-tlm]: https://github.com/google/eddystone/blob/master/eddystone-tlm/tlm-plain.md
[eddystone-eid]: https://github.com/google/eddystone/blob/master/eddystone-eid/README.md
[eddystone-computation]: https://github.com/google/eddystone/blob/master/eddystone-eid/eid-computation.md
[eddystone-etlm]: https://github.com/google/eddystone/blob/master/eddystone-tlm/tlm-encrypted.md
