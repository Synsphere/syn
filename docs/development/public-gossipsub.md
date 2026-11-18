# Public GossipSub groups for Syn URLs

The managed Syn libp2p node supports open public groups identified by Syn object
URLs. A Type, Thing, Event, Ember, Cog, Credential, Enigma, Pondle, Snug or Om can
have its own group. Any participant in the selected network namespace may join
and publish. Joining does not require an invitation, a fixed peer roster, an
installed Operator, a private Agent certificate, or a matching local database
object.

This is a current-source addition to the API node's existing TCP/Noise/Yamux
host. It uses the existing Amino client for provider discovery and standard
GossipSub v1.1 for messages. The [private Agent relay](private-pubsub.md) retains
its separate native credentials, group encryption and complete-cohort rules.

## Group names and exact discovery key

Public groups accept the canonical local object grammar:

```text
syn://<kind>/<slug>
```

`kind` is one of the ten primitive kinds listed above, in lowercase. A slug is
1–255 lowercase ASCII letters or digits, with single hyphens or underscores
between nonempty components. For example:

| URL | Group subject |
| --- | --- |
| `syn://type/weather-observation` | A Type |
| `syn://cog/weather-summary` | A Cog |
| `syn://ember/shared-curiosity` | An Ember |
| `syn://event/community-meeting` | An Event |

The `synny://` scheme alias normalizes to `syn://` before hashing. Thus
`synny://type/weather-observation` and `syn://type/weather-observation` join the
same group. Uppercase forms, query strings, fragments, percent escapes, extra
path segments, userinfo, ports and unknown primitive kinds are rejected. The
runtime does not silently rewrite an arbitrary string into a different object
identifier.

The network identifier is a lowercase name matching
`[a-z][a-z0-9-]{0,63}`. If omitted when joining, it defaults to the installed
Operator's network, or `syn` when the node has no Operator. An explicit network
identifier selects that namespace; one node can join groups in several
namespaces.

The group identifier is independent of any publisher, Operator, boot or private
workspace session. Its exact preimage is:

```python
from hashlib import sha256
from syn_client.protocol import canonical

body = {
    "profile": "syn.public-gossipsub-group/1",
    "network_id": "syn",
    "url": "syn://type/weather-observation",
}
digest = sha256(canonical(body)).digest()
```

`canonical` produces ASCII JSON with sorted keys, compact separators, escaped
non-ASCII strings and no non-finite numbers.

| Purpose | Exact representation |
| --- | --- |
| Group hash | Lowercase hexadecimal of the 32-byte digest |
| GossipSub topic | `/syn/public-pubsub/1/` followed by the group hash |
| Amino provider key | Binary `0x12 0x20` followed by the digest: a SHA2-256 multihash |
| DHT routing target | SHA-256 of the binary provider key |

Changing the URL or network produces a different group. The primitive kind is
part of the URL, so identically named Types, Cogs, Embers and Events remain
separate groups. `syn_client.public_pubsub.group_descriptor(network_id, url)`
returns this complete descriptor; `group_hash`, `provider_key` and `topic` are
also available as standalone helpers without importing libp2p or the runtime.

The relay advertises itself with `ADD_PROVIDER` and looks up other providers
with `GET_PROVIDERS` under the same binary key. This uses the
[libp2p routing-discovery pattern](https://github.com/libp2p/go-libp2p/blob/v0.52.0/p2p/discovery/routing/routing.go).
It does not publish an object, message history or private scope as a Bitswap
block. The key is separate from the existing Operator base-key discovery key
and private GossipSub scope hash.

## Open participation and message meaning

The network name is a public namespace, not a network admission certificate.
The implementation does not introduce an authority that approves public group
membership. A peer that implements the protocol can participate in a known
namespace and URL group.

Every GossipSub publication must carry a valid libp2p transport signature.
Received messages identify their author as `sender_peer_id`. This proves the
transport signer; it does not by itself establish an Operator's stable base
identity or ownership of the referenced object. Independent identity records,
credentials and semantic policies remain separate contracts.

Messages are public JSON in the `syn.public-gossipsub-message/1` envelope:

| Field | Meaning |
| --- | --- |
| `profile` | Exactly `syn.public-gossipsub-message/1` |
| `network_id` | The exact selected network namespace |
| `url` | Canonical `syn://` group URL |
| `message_id` | Fresh 128-bit identifier represented by 32 lowercase hexadecimal characters |
| `sent_at` | Integer Unix timestamp |
| `data` | Application JSON data |

The complete canonical envelope is limited to 65,536 bytes and a nesting depth
of 64. Receivers reject messages more than five minutes old or more than
30 seconds in the future, and messages whose URL/network differs from their
topic. Duplicate suppression is bounded and volatile; it is not a durable
exactly-once delivery contract.

Noise protects transport hops. Group participants and forwarding peers can read
public message contents; there is no private group encryption layer. Provider
advertisements and public subscriptions reveal participation metadata. The
identifying hash is computable from a known URL and network and provides no
anonymity guarantee.

Receiving a public message does not execute a Cog, create or update an object,
accept an Event as evidence, confer a Credential, write to a DAG, or authorize
consensus. Applications decide how to interpret received data and must apply
their existing authority and validation rules before taking effects.

## Join, publish, read and leave

Public subscriptions live on the API node; no private relay configuration file
or exchanged peer-address list is needed. The node starts with no joined public
groups. A successful join creates the local subscription immediately. Repeating
that join is idempotent and preserves its existing receive stream.

Use the updated dependency-free synchronous client:

```python
from syn_client.public_pubsub import PublicPubsubClient

client = PublicPubsubClient("http://127.0.0.1:8000")
joined = client.join("syn://type/weather-observation", network_id="syn")
group = joined["group_hash"]

accepted = client.publish(group, {
    "kind": "observation",
    "temperature_c": 18.5,
})
print(accepted["message_id"], accepted["accepted"])

page = client.messages(group, limit=100)
for message in page["messages"]:
    print(message["sender_peer_id"], message["data"])
cursor = page["cursor"]

# Poll later, preserving this consumer's own cursor.
next_page = client.messages(group, after=cursor, limit=100)
print(next_page["dropped"], next_page["has_more"])

# When this node no longer wants the subscription:
client.leave(group)
```

Other nodes join the same canonical URL and network through their own local
Syn service. `client.groups()` reports joined groups and their discovery and
mesh state. The client uses loopback HTTP or HTTPS for a remote control origin;
it does not follow redirects or automatically retry a publication after an
uncertain HTTP result.

Local publication returns `accepted: true` and
`peer_reception_proved: false`. It proves local submission, not delivery to any
other participant. The node is itself subscribed, so its own publications may
also appear in its receive buffer.

`peer_count` reports connected peers subscribed to the topic;
`mesh_peer_count` reports the current GossipSub mesh. Discovery and mesh formation
continue after joining, and a zero-peer group is valid. A late participant can
join an already active group without restarting existing participants. Peer
departures are handled through normal GossipSub maintenance and provider
rediscovery. Immediate leave/rejoin may encounter remote PRUNE backoff; local
join success does not promise immediate remote mesh reattachment.

### HTTP interface

The same controls are available directly:

| Method and path | Input or result |
| --- | --- |
| `GET /p2p/pubsub/groups` | Service status, default namespace, limits and joined-group descriptors |
| `POST /p2p/pubsub/groups` | JSON `{ "url": "syn://type/weather-observation", "network_id": "syn" }`; `network_id` is optional |
| `DELETE /p2p/pubsub/groups/{group_hash}` | Unsubscribe locally; repeating leave succeeds with `already_left: true` |
| `POST /p2p/pubsub/groups/{group_hash}/messages` | JSON `{ "data": ... }`; returns a publication identifier and local acceptance |
| `GET /p2p/pubsub/groups/{group_hash}/messages` | Optional `after` cursor and `limit` from 1–256; default limit is 100 |

These are controls for the local node's subscriptions and publications. The
existing API server binds to `127.0.0.1` by default. Open P2P group membership does
not change the API into an unrestricted remote administration service or grant
credentials for controlling someone else's node.

Invalid inputs return 422; oversized requests/messages return 413. Reading or
publishing to a group that this node has not joined returns 404. A cursor from
another subscription generation returns 409. Capacity exhaustion returns 429,
and unavailable transport returns 503. Status remains readable while the public
service is disabled or stopped. The HTTP request body itself is bounded to
128 KiB before JSON parsing.

## Receive cursors and lifetime

Each local join generation receives a random `stream_id`. Its opaque cursor is
that 32-character hexadecimal identifier followed by `:` and a local offset.
Offsets are local receive order, not network-wide sequencing or causal order.

Reads are nondestructive. Multiple consumers can keep separate cursors over the
same buffer. A page includes `messages`, the next `cursor`, `has_more`, and
`dropped`: the number of messages between the requested offset and the oldest
message still retained. Group status separately reports cumulative buffer
evictions in its `dropped` field.

Retention is bounded by the configured count per group and a shared 16 MiB
application-message byte budget. Older messages may be evicted before a slow
consumer reads them. There is no disk history, replay download, database inbox
or delivery acknowledgement. Applications requiring durable processing must
use a separately authorized persistence contract.

Leaving cancels that group's reader and maintenance, unsubscribes locally, and
discards its receive state. Rejoining the same URL creates a new `stream_id`;
the old cursor is rejected instead of being interpreted against unrelated new
offsets. Other joined groups remain active. Node shutdown cancels outstanding
discovery work, and subscriptions are not restored automatically after restart.

Provider advertisements already stored in the DHT can remain until their TTL
expires. Leaving stops refreshing them; there is no immediate global provider
withdrawal. A peer may therefore discover a former participant and find that it
no longer subscribes to the topic.

## Configuration and reachability

Public GossipSub uses the ordinary `[p2p]` table:

```toml
[p2p]
publicPubsubEnabled = true
publicPubsubMaxGroups = 32
publicPubsubQueueSize = 256
publicPubsubDiscoveryIntervalSeconds = 15
publicPubsubProviderRepublishSeconds = 10800
```

| Setting | Default | Accepted range |
| --- | ---: | --- |
| `publicPubsubEnabled` | `true` | Boolean; enabling starts the service, not subscriptions |
| `publicPubsubMaxGroups` | `32` | Integer 1–128 |
| `publicPubsubQueueSize` | `256` | Integer 1–4096 per group, also subject to the shared byte budget |
| `publicPubsubDiscoveryIntervalSeconds` | `15` | Finite number from 1 through 300 |
| `publicPubsubProviderRepublishSeconds` | `10800` | Finite number from 60 through 79200, inclusive |

The host's existing `enabled`, `aminoEnabled`, `bootstrapPeers`,
`listenAddresses`, `announceAddresses`, `allowPrivateAddresses`,
`requestTimeoutSeconds` and `lookupTimeoutSeconds` settings also apply. Public
group reannouncement uses its own interval, separate from Operator peer-record
publication. Maintenance is shared and bounded: at most two public-group DHT
jobs and three candidate dials run concurrently. A maintenance attempt has at
most ten seconds, further limited by the host lookup timeout. Discovery
continues during ordinary peer churn and while other groups are active.

If Amino is disabled, local subscriptions still work but DHT discovery and
provider publication are disabled. A WAN discovery outage does not remove
local subscriptions or existing GossipSub connections.
When no DHT servers answer a lookup, the group remains locally joined while
`discovery.state` reports `unavailable`. This differs from a successful lookup
that returns no other providers; neither result establishes remote delivery.

Public advertisements require reachable public IP/TCP addresses. Wildcard bind
addresses are not advertisements; use an actual public interface, explicit
announce addresses, or successfully established mappings supported by the
managed host. A successful outbound connection alone does not establish inbound
reachability. The [Amino guide](p2p-amino.md) describes the current address and
transport limits. An isolated lab may explicitly use private addresses and its
own pinned bootstrap server; this override must not use the official bootstrap
configuration.
Public group discovery dials use address-filtered Amino results. Incoming
GossipSub PRUNE peer-exchange suggestions are discarded, so they do not create
an alternative discovery path around the host's address policy.

## Implementation and validation

The implementation consists of the [public service](../../src/syn/public_pubsub.py),
the [managed node integration](../../src/syn/p2p.py), the
[HTTP router](../../src/syn/public_pubsub_http.py), and the
[client and wire contract](../../packages/syn-client/src/syn_client/public_pubsub.py).

The [managed-node integration tests](../../tests/test_public_pubsub_runtime.py)
use actual TCP, Noise, Yamux, GossipSub signatures and a local DHT wire server.
Two independent nodes without Operators advertise and discover five groups
covering Types, Cogs, Embers, Events and a separate network namespace. The tests
verify alias convergence, exact provider keys, signed public JSON delivery,
group isolation, a third late participant, nondestructive reads, leave/rejoin
cursor isolation, continued delivery on other groups and cancellation of a
blocked lookup during shutdown. No static group-peer list or database object
is injected.

Public WAN group connectivity has not been qualified in this workspace. The
earlier public-bootstrap diagnostic could not reach the environment's DNS
resolver. Local protocol tests do not establish NAT/router configuration,
public-network performance, admission resistance or a new published release
qualification.
