# Bluetooth beacon schema examples

These files demonstrate the specification in
[bluetooth-beacons.schema.json](../../schemas/bluetooth-beacons.schema.json).
Each document is a native object's **knowledge payload**, not a Thing, Ember or
Event envelope. All node, controller, credential, encoder and session identities
are synthetic. No attached adapter was queried and no beacon was transmitted.

## Validate offline

Use a Python environment with `jsonschema` installed. This documentation
validator dependency is separate from the Bluetooth service's runtime
dependencies.

```bash
python3 docs/examples/bluetooth-beacons/validate.py
```

The validator checks the Draft 2020-12 meta-schema, accepts every `valid/*.json`
fixture and verifies rejection of every `invalid/*.json` fixture. It rejects
duplicate JSON properties, non-finite numbers, documents larger than 256 KiB,
and instance nesting deeper than 16. Remote schema retrieval is disabled.

To validate particular knowledge payloads:

```bash
python3 docs/examples/bluetooth-beacons/validate.py path/to/payload.json
```

## Example sets

| File in `valid/` | Purpose |
| --- | --- |
| `capabilities.json` | Synthetic controller/backend/availability evidence, node-scoped inventory binding and effective limits |
| `beacon-ibeacon.json` | Complete native Syn reference fragments followed by a Syn iBeacon hint, all within the legacy packet budget |
| `beacon-syn-ibeacon.json` | Complete reference transported inside Syn-marked iBeacon frames for a manufacturer-data-only publisher |
| `beacon-backend-selected-interval.json` | The same complete identity sequence with ordinary advertising timing delegated to the backend |
| `beacon-legacy-format-catalogue.json` | iBeacon, AltBeacon, Eddystone UID, URL, TLM, EID and eTLM payload shapes; EID/eTLM use opaque credential and clock references |
| `beacon-syn-url.json` | Full canonical Syn URL as UTF-8 service data under a custom 128-bit UUID, using extended advertising |
| `beacon-periodic.json` | Generic periodic advertising with separate ordinary and periodic interval units |
| `beacon-raw-format-catalogue.json` | Ordered raw AD structures, manufacturer data, and 16/32/128-bit service-data shapes |
| `beacon-extension.json` | Independently pinned custom encoder and parameter schema |
| `plan.json` | Compatible, rejected and unresolved targets; pinned source snapshots, exact bytes for five Syn reference fragments and one iBeacon hint, and validity deadlines |
| `plan-no-adapters.json` | Empty target set with an explicit reason, so no adapter identity is fabricated |
| `observation-*.json` | Preparation, backend start/stop, rejection and disconnect evidence, including a whole-definition rejection with no adapter |

The format-catalogue examples show syntax, not an operational deployment plan.
The placeholder `0xffff` company identifier and `ffff`/`ffffffff` service UUIDs
illustrate field widths only; replace them with identifiers appropriate for the
actual profile and validate their assignments through the codec contract. The
extension example names no installed codec. Historical timestamps and synthetic
capabilities make these documents unsuitable as live advertising instructions.
The complete identity examples use nominal power bytes for illustration; no
physical calibration was performed. The iBeacon's power byte is separate from
the controller's requested transmit power.

## Required Syn identity policy

Every beacon definition includes this closed policy:

```json
"identity": {
  "branding": "Syn",
  "emitter": "nodeRef",
  "marker": "every-frame",
  "fullReference": "required-per-sequence",
  "shortHints": "allowed-with-full-reference",
  "authentication": "advertised-claim"
}
```

`emitter: "nodeRef"` means the canonical `nodeRef` on the enclosing definition.
There is no second identity field that can silently select another node. Every
emitted frame must contain a recognized Syn marker, and every complete scheduled
sequence must transport that entire reference, either in one packet or in a
bounded fragment sequence. A separate identity packet does not excuse an
unbranded packet elsewhere in the sequence. A local name containing “Syn” alone
does not meet the marker requirement.

Compact hints may accompany the complete reference. A hint alone cannot resolve
a node reference or authenticate a node. The current native decoder only
associates a hint with a matching complete reference already reconstructed from
the same receiving adapter and OS source identifier before its cache expires.
Fragments received under different source identifiers are never combined using
their short stream token. Missed packets, address rotation or unguaranteed address
continuity between Windows publishers can leave a source recognizable as Syn
without a resolved node reference.

The structural validator enforces the declaration. It does **not** inspect
encoded identity bytes, generate companion frames or prove compliance with the
policy. `beacon-ibeacon.json`, `beacon-syn-ibeacon.json` and
`beacon-backend-selected-interval.json` provide complete reference sequences using
the native protocol. The generic raw, extended, periodic, Eddystone telemetry and
custom-encoder catalogues require further semantic work. Their example payloads
must be rejected for transmission wherever a recognized per-frame marker or
complete reference transport is absent; adding this policy object cannot make
unbranded bytes compliant. Current runtime format support is documented in
[Bluetooth beacons](../../development/bluetooth-beacons.md).

The invalid fixtures cover closed-object enforcement, broadcast-only scope,
legacy/periodic interval bounds, hardware-power policy, Eddystone URL schemes,
inline secret rejection, EID rotation bounds, AD/manufacturer/service payload
limits, conventional named-profile transport, immutable capability pins,
rejected-target buffer exclusion, empty-target reasons, frame expiry fields,
adapter lifecycle evidence, tri-state capabilities and false reception claims.
They also reject a missing identity policy, a different brand, a separately
selected emitter, optional markers, hint-only sequences, identity resolution
from a short hint alone, and claims of authentication.

## Structural and semantic validation

Passing JSON Schema establishes shape and local constraints only. A semantic
compiler is still required to check:

- Adapter identity, current attachment epoch, deduplication and actual backend
  routing; existing radio-inventory presence alone is insufficient.
- Freshness, immutable source hashes, encoder availability, permissions and
  live advertising-set allocation.
- Ordered interval values and all controller/backend ranges. A concrete
  ordinary interval requires supported `backend.intervalControl`; `interval:
  null` delegates timing to the backend and remains null if unreported. Periodic
  intervals remain explicit in this version.
- Complete encoded byte budgets, AD length framing, UUID byte order, inserted
  OS fields and correspondence of wire buffers to their source payloads.
- The required identity policy: a recognized Syn marker in **every** emitted
  packet and a complete canonical emitter reference transport in **every**
  scheduled sequence, with the advertised reference equal to the definition's
  `nodeRef`. Reject unsupported profiles, missing identity fragments and
  hint-only sequences instead of silently reducing the identity requirement.
- Fixed Syn namespaces and node-reference-derived hint values for supported
  named formats. Generic service/manufacturer data and extension codecs need
  an explicit recognized profile; a company ID, the FEAA UUID, a name, or an
  arbitrary caller-supplied UUID does not establish Syn branding.
- Unique frame IDs, schedule feasibility and associations between identifier
  frames and Eddystone telemetry.
- Eddystone URL compression to the protocol's encoded byte budget.
- Full Syn URL template length; there is no implicit truncation or hashing.
- Credential access, EID time mapping and rotation, authenticated eTLM
  generation, and expiry before capability or ephemeral-identifier deadlines.
- Requested/effective settings and the narrower constraints of an extension's
  independently pinned, closed parameter schema.

For example, an inverted interval range, duplicated frame IDs, a long HTTP URL,
an expired snapshot or two views of the same physical adapter may be
structurally valid but are not a usable advertising plan. The validator does
not implement those checks or execute an advertiser. Backend acceptance is
recorded with `airReceptionVerified = false`; reception needs independent
observation evidence.

The pinned definition, capabilities and plan digests in these fixtures use
`syn.radios.types.canonical`: sorted compact ASCII JSON with `ensure_ascii=True`
and `allow_nan=False`, followed by SHA-256. Each encoded frame's `payloadSha256`
pins its corresponding definition payload. Source changes require updating
those pins and the observation fixtures' plan digests.
