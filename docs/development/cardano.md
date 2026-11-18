# Cardano block observations

Syn can follow one to eight explicitly configured Cardano node-to-node TCP
endpoints. It waits for new blocks and rollbacks, creates native Syn Events and
observation Embers, and records each observation in both the observing node's
transaction ledger and the block Event's transaction ledger.

The service is disabled by default. It starts and stops with `syn-server`, after
the existing node identity and transaction ledger have been prepared. No Oura
process, Cardano node installation or new Python dependency is required on the
observing Syn host.

## Relationship to Oura

The implementation follows the small source-side pattern in
[Oura's node-to-node source](https://github.com/txpipe/oura/blob/75370b4780ab6a8c6637577230cf04d65a6d8dc1/src/sources/n2n.rs):
intersect the chain, request the next header, fetch the corresponding block, and
handle roll-forward, roll-backward and await responses. It does not include
Oura's general pipeline, filters, transaction decoding or output sinks.

The wire client implements TCP multiplexing, node-to-node version negotiation
(versions 11–14),
non-pipelined chain-sync, one-block block-fetch and keep-alive. Chain-sync sends
headers; block-fetch retrieves the full block separately. The fetched block's
exact header must match the header announced by chain-sync before an observation
is committed. After `AwaitReply`, the client waits for the peer's response
without sending another `RequestNext` while the peer has agency.

Header parsing supports Shelley, Allegra, Mary, Alonzo, Babbage and Conway.
Unknown eras and Byron history stop that feed with an explicit blocked state.
The block ID is Blake2b-256 of the original received header CBOR. The raw header
bytes are preserved; decoding and re-encoding do not define its identity.

## Native Syn objects

The migration installs this hierarchy and the named blockchain Thing:

| Reference | Role |
| --- | --- |
| `syn://type/blockchain` | Blockchain Type, below the existing `system` Type |
| `syn://thing/cardano` | Thing named **Cardano**, with Type `blockchain` |
| `syn://type/blockchain-block` | Subtype of `event` |
| `syn://type/cardano-block` | Subtype of `blockchain-block` |
| `syn://type/cardano-block-observation` | Subtype of the existing `observation-result` Ember Type |

For block hash `H`, the Event's exact reference is `syn://event/H`, where `H` is
the 64-character lowercase Cardano block hash. For example, the pinned mainnet
test fixture has Event reference:

```text
syn://event/d03a37835e9f1ffb70e8a476334c69183ab5491c911faa7ea0d6ba285dd7c8b4
```

The Event contains immutable block facts: blockchain reference, block hash,
slot, block number, previous block hash, era and SHA-256 of the raw header CBOR.
Its target includes the Cardano Thing. `occurredAt` remains unset: a Cardano slot
is not itself a UTC production timestamp. Local receipt time belongs to the
observation Ember's `observedAt` field.

One Event represents a given block hash. Several trusted endpoints or observing
Syn nodes can supply separate observations of that same Event. The observation
records the observing node, configured endpoint, network magic, source revision
and local observation time. Applied-block observations also retain the fetched
block's byte length and SHA-256 digest. Full block bodies are not archived by
this feature; the original header is retained in `cardanoBlocks`.

## Both transaction ledgers

The store uses the existing `MessageStore.appendInTransaction` implementation.
Each observation Ember has the same immutable payload appended to:

1. The existing node workspace returned by
   `syn.positions.store.workspace_for(node_ref)`.
2. The Event workspace returned by
   `syn.cardano.store.event_workspace(event_ref)`.

The Event workspace is UUIDv5 with `NAMESPACE_URL` and the exact string
`syn-object-ledger:` followed by the canonical Event reference. The node
workspace retains its established `syn-node-ledger:` namespace. The block index
stores the Event workspace explicitly for discovery.

Matching `objectChronicle` entries with `kind = 'ledger'` attach the observation
to both the node Thing and the block Event. Their details retain the Ember
reference, content hash, receipt workspace, sequence and hash. The
`cardanoObservations` index retains both receipts for inspection and validation.

The Event, Ember, two receipts, two Chronicle entries and source checkpoint
commit in one database transaction. Failure rolls back the whole operation.
The node row is locked before ledger heads, sharing the lock order used by
position, clock and radio observations. This is the existing node transaction
ledger; native Operator authority/adoption journals keep their separate scope.

## Rollbacks, restart and multiple peers

Every configured endpoint has an independent durable checkpoint and active
chain projection, keyed by observing node, network magic and endpoint. No
majority vote or global Cardano chain selection is performed.

On the first connection, the default is to intersect the peer's current tip and
observe successors. The initial anchor is recorded in the node ledger without
inventing historical block Events. An explicit starting slot and hash can
replace that initial tip choice. Once a durable checkpoint exists, it takes
precedence over these first-start settings.

Reconnect offers the current point, recent points, exponentially spaced older
points and the retained anchor, with at most 64 candidates. If the peer has no
retained intersection, Syn preserves its checkpoint and retries; it does not
silently jump to a new tip and omit history. A negotiated older intersection
performs the same retraction work as an ordinary rollback.

A rollback creates a new retraction Ember for each affected locally observed
block. It names the source's earlier apply Ember in `retracts` and includes the
rollback destination. Each retraction is recorded in both ledgers and both
Chronicles. Original Events and observations remain unchanged. The source's
active projection and checkpoint change atomically with the retractions.

Other endpoints' observations are unaffected. If the same block is later
applied again, its Event is reused and a new observation Ember records the new
source transition. Replaying an already committed current block does not
duplicate its observation.

An older rollback point outside the locally observed range can become a new
trusted-source anchor. An unknown point inside the retained active range is a
conflict. Recovery transactions retract at most 4,096 local blocks; larger
rollbacks leave the state unchanged and block the feed for explicit operator
attention. The observer never deletes history to resolve a conflict.

## Configuration

Add a `[cardano]` table to the node's existing configuration. The selected
mainnet backbone peers below are documented as bootstrap peers, all on TCP port
3001, in the [Cardano Developer Portal's topology reference](https://developers.cardano.org/docs/operate-a-stake-pool/node-operations/topology/):

```toml
[cardano]
enabled = true
networkMagic = 764824073
peers = [
    "backbone.cardano.iog.io:3001",
    "backbone.mainnet.emurgornd.com:3001",
    "backbone.mainnet.cardanofoundation.org:3001",
]

timeoutSeconds = 15
keepaliveIntervalSeconds = 20
reconnectDelaySeconds = 2
maxReconnectDelaySeconds = 60
```

`764824073` selects Cardano mainnet, as specified by the
[mainnet Shelley genesis configuration](https://book.world.dev.cardano.org/environments/mainnet/shelley-genesis.json).
The configured magic must match the chosen nodes; change both magic and peers
when selecting another network. Endpoint syntax is `hostname:port`, `IPv4:port`
or `[IPv6]:port`, with an explicit TCP port and at most eight unique endpoints.

The main `config.example.toml` includes these peers with `enabled = false`;
set it to `true` to start following them. The programmatic default remains an
empty peer list when no peers are configured. Syn follows each selected endpoint
directly, with its own durable cursor; it does not import Cardano's topology
governor or discover additional Cardano relays through libp2p.

The Developer Portal's three-host example differs from the
[downloadable mainnet topology](https://book.world.dev.cardano.org/environments/mainnet/topology.json),
which listed only the IOG and Cardano Foundation hosts when checked on
18 November 2026. This example retains all three explicitly selected hosts.
Presence in a configuration example is not a reachability check.

To start from a particular known point on a fresh source, supply both
`startSlot` and `startHash`. The hash must be 64 lowercase hexadecimal
characters. The peer must accept that exact point; rejection preserves state.

| Setting | Default | Accepted range |
| --- | ---: | --- |
| `enabled` | `false` | Boolean; enabling requires at least one peer |
| `networkMagic` | `764824073` | Unsigned 32-bit integer |
| `peers` | `[]` | Up to eight unique explicit endpoints |
| `startSlot` / `startHash` | Omitted | A complete slot/hash pair; fresh sources only |
| `timeoutSeconds` | `15` | Finite seconds from 0.1 through 300 |
| `keepaliveIntervalSeconds` | `20` | Finite seconds from 1 through 60 |
| `reconnectDelaySeconds` | `2` | Finite seconds from 0.1 through 60 |
| `maxReconnectDelaySeconds` | `60` | Finite seconds from 0.1 through 600, no less than the initial delay |

Syn's normal database migration process installs the types, Cardano Thing and
projection tables. Then start the service normally:

```bash
uv run syn-server --config config.toml
```

Native Operator and Agent awareness runtimes also use this table when their
PostgreSQL-backed awareness service is configured. Put it in the configuration
selected by `SYN_POSITION_CONFIG`, or in the native installation's adjacent
`position.toml`. The follower starts after that runtime has prepared its node
ledger and acquired its scheduler lease, and stops before the scheduler closes.

## Status and operating limits

Read the local service status with:

```bash
curl --fail http://127.0.0.1:8000/cardano/status
```

The response includes the observer's node reference, network magic, blockchain
reference and per-peer connection state. Each peer reports its persisted point,
revision and active-block count alongside current-process connection attempts,
received-block/rollback counters, latest reported tip and error state.
Connection failures retry with bounded exponential delay. A persistent source
conflict or unsupported era produces a `blocked` feed. Stopping the service
cancels network work and waits for an in-flight database transaction to finish.

The wire path bounds headers at 64 KiB, full fetched blocks at 4 MiB and CBOR
nesting/work. Receipt payloads remain within the existing 64 KiB ledger limit.
Network work runs asynchronously; database writes are serialized for the node.

"Trusted" means the endpoints were explicitly selected by the operator.
Node-to-node TCP and network magic do not authenticate a remote Operator or
provide transport confidentiality. This observer checks framing, protocol state,
header identity, fetched-header agreement and continuity with retained source
history. It does not validate Cardano transactions, KES/VRF consensus proofs,
stake-based chain selection or block finality. An observation records what that
endpoint reported, with sufficient provenance to distinguish competing views.

## Validation and source references

The protocol tests include a real Conway block and independently published hash
from [Pallas at a pinned commit](https://github.com/txpipe/pallas/tree/daecef59a1979d89515524d9583cc28655af182c).
The fixture's original bytes, upstream metadata, provenance and Apache 2.0
license are retained in [tests/fixtures/cardano](../../tests/fixtures/cardano/README.md).

The main implementation files are [protocol.py](../../src/syn/cardano/protocol.py),
[runtime.py](../../src/syn/cardano/runtime.py),
[store.py](../../src/syn/cardano/store.py), and
[schema.py](../../src/syn/cardano/schema.py). The test suites cover wire parsing,
source lifecycle, native objects, dual receipts and rollback/restart behavior.
The [end-to-end test](../../tests/test_cardano_integration.py) connects the actual
wire client and runtime to the actual store, checks both receipt chains and
Chronicles, then exercises rollback and durable restart over loopback TCP.
SQLite projection tests exercise the real store and receipt algorithm but do
not qualify PostgreSQL domain, trigger or concurrency behavior. Separate tests
in `tests/test_cardano_postgres.py` migrate a disposable PostgreSQL schema and
check concurrent writes, native history guards, deferred dual-receipt constraints
and failure atomicity. Run them with `SYN_CARDANO_TEST_DATABASE_URL` pointing to a
disposable database with DDL rights; the tests create and remove their own random
schema. A loopback test server does not establish public-network interoperability
or live endpoint availability; consult the milestone validation report for the
exact runs.
