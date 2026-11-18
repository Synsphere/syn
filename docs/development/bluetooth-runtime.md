# Syn Bluetooth presence and observations

This milestone adds a native service that periodically advertises the local Syn
node, listens between its own transmissions, and records radio activity in the
node's transaction ledger. It runs inside the same managed lifetime as node
awareness and the Cardano observer.

The example enables it with `[bluetooth] enabled = true`. Code that constructs a
configuration without a Bluetooth table still gets an inactive service. The
node must already have its native Thing and ledger workspace prepared.

## What is implemented

Every frame emitted by the native service has an explicit Syn namespace or
marker. A sequence includes a transport for the complete canonical node
reference; compact standard beacon frames carry additional identification hints.
The service will not emit only compact hints when the adapter cannot carry the
configured complete-reference transport. It records that limitation and scans
where scanning is available.

| Backend | Advertising | Reception | Adapter selection |
| --- | --- | --- | --- |
| Linux / BlueZ | Syn UUID128 service fragments, Syn iBeacon identity fragments, compact iBeacon, Eddystone UID and explicitly configured AltBeacon | Unfiltered OS-visible LE discovery reports | Separate session for each usable controller |
| Windows / WinRT | Syn iBeacon identity fragments, compact iBeacon and explicitly configured AltBeacon | Unfiltered BLE advertisement watcher | OS-managed default adapter; independent selection of every attached USB controller is not claimed |
| macOS / CoreBluetooth through Bleak | Not implemented by this backend | BLE advertisement scanning, subject to OS access | OS-managed adapter |
| Other platforms | Unavailable diagnostic | Unavailable diagnostic | No assumed support |

These are backend capabilities, not a claim that every attached controller is
powered, permitted, or has free advertising resources. An absent adapter or
unavailable OS service is retried without fabricating successful transmissions.

Microsoft reserves service UUID and ServiceData sections in the public
[BluetoothLEAdvertisementPublisher API][windows-publisher]. Its manufacturer
data support is why the full-identity iBeacon transport exists. BlueZ exposes
manufacturer and service data through [LEAdvertisement1][bluez-advertisement].
Native dependencies are platform-marked normal project dependencies; `uv sync
--frozen` installs the appropriate packages. OS Bluetooth support and access to
its service still have to exist. The service does not power adapters, pair with
devices, connect to them, or change other applications' discovery sessions.

The backend runs in the OS hosting this Syn awareness process. A server running
inside WSL can use only controllers exposed to that Linux environment; this
milestone does not add a Windows-host Bluetooth relay to the custody bridge.
The WinRT backend requires a native Windows awareness process with its node
database configuration. An adapter visible only to the Windows host is not
reported as a usable Linux adapter merely because the host has Bluetooth.

## Identity on the radio

The source identity is the node's existing canonical `nodeRef`, not an inferred
MAC address and not the Operator's private key. The normal node reference already
comes from Syn's node identity setup. Explicit canonical Thing references are
encoded as supplied. The encoder accepts at most 267 ASCII bytes, including the
`syn://thing/` prefix; oversized references are rejected, never truncated.

| Runtime format | Syn marker | Identity material | Planned maximum |
| --- | --- | --- | ---: |
| `syn` | Service UUID `5fe03a96-c8db-5d52-8077-1dde183c5374` | Complete reference across a sequence of fragments | 31 bytes |
| `syn-ibeacon` | First eight UUID-field bytes `53594e0197860912` | Complete reference across iBeacon-shaped fragments | 30 bytes |
| `ibeacon` | UUID `93f9ca4a-2d3d-5e03-b687-af0874f6683c` | First 32 bits of SHA-256 of the canonical reference in major/minor | 30 bytes |
| `eddystone-uid` | Namespace `53594e013949fcc27f0f` | First 48 bits of that hash as the instance | 31 bytes |
| `altbeacon` | Beacon ID begins `53594e01` | First 128 bits of that hash | 31 bytes |

The byte budgets include a conservative three-byte Flags reservation. Native
APIs may supply or omit their own Flags; `advertisingDataHex` is the compiled
payload/budget, not a captured packet. No local-name field is required to identify
Syn, because adding it would overflow some legacy frames. A receiver must know
these Syn namespaces; ordinary scanners may show a UUID rather than a friendly
"Syn" label.

AltBeacon is opt-in and requires `companyId` explicitly. No Bluetooth SIG
company ID is invented for Syn. The iBeacon transports use ordinary iBeacon
framing under company identifier `0x004c`; Syn's identity occupies the beacon
UUID/major/minor fields. Power bytes are nominal template values, not calibrated
distance measurements or selected controller transmit power.

### Complete-reference wire format

Let `R` be the ASCII canonical reference and `L` its byte length:

```text
envelope = uint16_be(L) || R
payload  = envelope || uint32_be(CRC32("SYN-BEACON/1\0" || envelope))
token    = SHA256(payload)[0]
header   = byte((1 << 6) | fragment_index) || byte(fragment_count) || byte(token)
```

Indices are zero based. Native Syn service data carries `header` plus up to seven
payload bytes in AD type `0x21`. The UUID128 uses Bluetooth little-endian byte
order. There is no redundant service UUID list in this frame.

For the Windows-compatible transport, the manufacturer body is:

```text
02 15 || 53594e0197860912 || header[3] || payload_chunk[9] || c5
```

The prefix, header and nine-byte chunk occupy the 16-byte UUID and four-byte
major/minor region. The final chunk is padded with zero bytes. The decoder
checks explicit reference length, expected fragment count, padding, stream token
and checksum before returning the canonical reference. The maximum supported
reference uses 39 native service fragments or 31 iBeacon fragments.

The decoder binds an assembly to the receiving adapter, the OS source identity,
the transport and the token. It never assembles data from different source
addresses merely because short tokens match. Assemblies expire 120 seconds
after the first fragment, and the assembly and resolved-identity caches each
have a 128-entry default bound. Attachment changes clear that receiver's state.

All required fragments must arrive from the same OS source identity within the
expiry window. Missing fragments and address rotation can prevent resolution.
In particular, Windows does not guarantee source-address continuity between
separate publications. A Windows sender can remain recognisably Syn while its
full reference cannot yet be reconstructed. The service does not bypass source
isolation to conceal this limitation. Repeated sequences and staggered listen
windows provide further opportunities to receive the full identity.

### Claims and peer references

A checksum detects accidental corruption. It does not authenticate the sender.
No beacon in this milestone proves ownership of the advertised node reference,
possession of an Operator key, proximity, or a unique physical device.

| Reception result | Stored identity | Native Event link |
| --- | --- | --- |
| Complete canonical reference already represented by a local Thing | `claimed-known`, `authentication: unverified` | `advertised-peer` target to that existing Thing |
| Complete reference not represented locally | `claimed-unknown`, full `claimedNodeRef` retained | Reference remains in Event/Ember knowledge; no placeholder Thing is created |
| The local node's own reference | `claimed-self` | Existing observing-node target; no duplicate peer target |
| Syn namespace plus short hash only | `hint-only` | No peer link from the short hash alone |
| Syn fragment sequence still incomplete | Branded but unresolved | No fabricated node reference |
| Ordinary Bluetooth advertisement | `unidentified` | Observation of its source and available advertisement data |

A compact hint may reuse a matching unexpired full reference reconstructed from
the same receiver and source. This remains an unauthenticated advertised claim.
An incoming packet cannot change an existing peer Thing or elevate a claim to a
verified identity. The observation store ignores incoming assertions of trust.

## Send/listen schedule

Each distinct eligible controller has one worker. Duplicate inventory entries
do not create duplicate workers. On the Windows backend there is one
OS-managed worker; it does not pretend to own every enumerated USB adapter.

1. Discover the backend's actual current adapters and record their capabilities.
2. Listen during the initial random delay, if scanning is supported.
3. Stop this worker's scan before requesting advertising.
4. Send the complete reference sequence and compatible compact frames in order.
5. Stop each advertisement before starting another frame.
6. After the burst, scan for `listenSeconds` plus a fresh random jitter.
7. Repeat while the service owns the adapter.

The default is 0.5 seconds per frame, 20 seconds of listening, up to five seconds
of initial delay and up to five seconds of extra listening per cycle. For a
typical 81-byte node reference, a native identity needs 13 fragments. With one
iBeacon and one Eddystone frame, its requested burst is 7.5 seconds followed by
20–25 seconds of listening. Native registration and database latency add to the
elapsed cycle. This is a software send/listen schedule; it is not Bluetooth
Periodic Advertising, a requested controller advertising interval, or a promise
of an exact packet count.

The full-reference native service transport is preferred when usable. The
iBeacon identity transport is selected when native service data cannot be sent.
Both are not sent redundantly by default. Requested formats that cannot be
encoded by that adapter appear in status. If a usable transmitter is absent,
the service continues listening wherever possible.

Half-duplex ordering applies to Syn's owned sessions. The service does not stop
other applications from using the same radio. BlueZ merges discovery requests
from multiple clients; [StopDiscovery releases only the caller's session][bluez-adapter].

## What is recorded

The store uses existing native Things, Types, Events, Embers, `eventTargets`,
`objectChronicle` and `MessageStore` receipts. It adds no migration or separate
shadow transaction ledger.

| Type | Parent / purpose |
| --- | --- |
| `syn://type/bluetooth-beacon` | Thing representing this node's beacon service |
| `syn://type/bluetooth-beacon-generation-event` | Event for beacon operations |
| `syn://type/bluetooth-beacon-transmission-event` | Generation-event subtype for transmission transitions |
| `syn://type/bluetooth-reception-event` | Event for received advertisements |
| `syn://type/bluetooth-lifecycle-event` | Event for discovery, operational errors and observation loss |
| `syn://type/bluetooth-advertising-observation-ember` | Observation-result subtype containing the evidence |

Preparation registers a deterministic beacon Thing and one idempotent
`syn.bluetooth-beacon-registration/1` receipt in the node ledger. Its knowledge
profile is `syn.bluetooth-node-beacon/1`. Each subsequent observation uses
`syn.bluetooth-event/1` Event knowledge and `syn.bluetooth-observation/1` Ember
knowledge. These runtime profiles are distinct from the broader proposed
generic compiler profiles.

For each operation, one database transaction creates the Event and Ember,
creates native targets, appends the observation message to the node and Event
ledgers, and records both Chronicle links. Failure of either ledger append
rolls the entire operation back. The receipts have the same message/payload but
belong to their own chains. Explicit operation IDs support durable idempotent
replay; contradictory reuse is rejected.

### Transmission transitions

`transmission-requested` is persisted before requesting the native advertiser.
`transmission-started` follows the native backend's acknowledgement.
`transmission-stopped` follows confirmed release. Start errors, unexpected stops
and uncertain cleanup are recorded as failures when the ledger remains usable.
Each payload identifies the adapter and attachment epoch, frame, format, burst,
compiled bytes, requested duration and native evidence.

`airReceptionVerified` is always false. A native acknowledgement cannot prove
that another radio heard the packet. Nor can a database transaction be committed
atomically with an OS radio operation: a process crash can leave a request with
no recorded start/stop outcome. Such an incomplete sequence remains evidence of
an attempt, not evidence that a packet was delivered. This implementation does
not fabricate a successful outcome during restart.

### Reception and aggregation

The monitor accepts ordinary advertisements as well as Syn beacons. It stores
the receiving adapter, source identity/address when available, name, service
UUIDs, service/manufacturer data as hexadecimal, RSSI, timestamps and the
decoder's identity evidence. OS metadata is source-qualified; missing fields
are not invented.

Identical reports from the same receiver/source/attachment and with the same
advertisement payload and identity result are grouped for the configured
observation window. The default window is one second from the first report;
repeats do not extend it indefinitely. The Event retains `reportCount`,
`firstSeenAt`, `lastSeenAt`, `rssiMinimum`, `rssiMaximum`, and `lastRssi`. Changed
payloads and newly resolved identities create distinct groups. A value of zero
disables intentional waiting, though reports delivered in one event-loop turn
may still share a group before the writer runs.

The runtime holds at most 512 pending distinct groups by default. WinRT also
has a bounded 256-report native-thread bridge. Queue overflow, invalid native
reports, and reports discarded while shutting that bridge down produce `loss`
Events with counts and reasons. Input is bounded and validated before database
work so malformed radio data cannot escape a callback or poison the writer.
Counts refer to reports delivered by the OS; frames never delivered by the
controller/OS cannot be counted. A ledger outage itself can prevent persistence
of remaining reports; the service records a fault in status and stops activity.

BlueZ requests an empty UUID filter and duplicate advertisement updates. Its
initial cached Device objects supply metadata only, not fresh reception events.
RSSI-only updates do not replay previously cached service/manufacturer bytes.
These choices follow the [Adapter discovery API][bluez-adapter].

Monitoring is bounded by public OS APIs: there is no packet capture of arbitrary
Bluetooth Classic exchanges, established connections, or encrypted application
traffic. This milestone implements BLE advertising/discovery observation.

## Lifecycle and failures

The managed server starts Bluetooth after node preparation, and checks it with
the node's readiness supervisor. Native Operator/Agent awareness uses the same
service. It stops Bluetooth before closing the remaining transport/scheduler
lifetime. Shutdown releases owned hardware, drains received observations, and
joins in-flight database work. Cancellation waits for that cleanup.

Missing OS dependencies, unavailable services, absent adapters and ordinary
operation failures remain visible and are retried. A database failure fences
radio work. An inability to confirm release of an owned native session also
fences further activity, so a retry cannot overlap a possibly live advertiser
or scanner. Backend reconnection changes the attachment epoch and clears cached
source identities. No old device inventory entry establishes current support.

## Configuration and status

```toml
[bluetooth]
enabled = true
formats = ["syn", "syn-ibeacon", "ibeacon", "eddystone-uid"]
frameDurationSeconds = 0.5
listenSeconds = 20
startupJitterSeconds = 5
cycleJitterSeconds = 5
inventoryRefreshSeconds = 10
retrySeconds = 5
backendTimeoutSeconds = 10
observationWindowSeconds = 1
maxPendingObservations = 512
maxAdapters = 16
```

At least one of `syn` or `syn-ibeacon` is required. Add `altbeacon` only with an
explicit valid `companyId`. No raw or encrypted beacon format is silently
substituted. Configuration rejects unknown fields, invalid ranges, duplicate
formats and nonfinite numbers.

`GET /bluetooth/status` exposes enabled/running/state, `node_ref`, `beacon_ref`,
current adapters and epochs, active/unavailable formats, worker activity, queue
depth, counters, last Event and last error. `running` means the service owns its
background lifetime; consult `state` and each adapter for actual radio activity.
`waiting-for-adapter` and `unavailable` are distinct from a successful broadcast.

The regular API readiness fence applies after a service fault; the read-only
`/bluetooth/status` endpoint remains available for diagnosis without database
work. Persisted Events and receipts remain in native objects/ledgers; the status
endpoint is not a new separate log or a radio-control endpoint.

## Scope and validation

The accompanying patch includes codec/model tests, OS-boundary tests, async
scheduling and cancellation tests, actual runtime-to-SQLite ledger integration,
and PostgreSQL tests with the real native triggers and receipt chains. The
validation report records exact commands and counts. No physical Bluetooth
adapter or real Windows radio was used for RF qualification in this workspace.

The generic [generation schema](bluetooth-beacons.md) still represents more
formats and controller procedures than this native runtime executes. Its
required Syn identity policy does not make arbitrary raw bytes safe or branded;
semantic compilation must check every encoded frame and a complete-reference
transport for the sequence. Executing arbitrary raw definitions, extension
code, Eddystone EID/eTLM, extended advertising and controller-level periodic
advertising remains outside this milestone.

[bluez-adapter]: https://github.com/bluez/bluez/blob/master/doc/org.bluez.Adapter.rst
[bluez-advertisement]: https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisement.rst
[windows-publisher]: https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.advertisement.bluetoothleadvertisementpublisher
