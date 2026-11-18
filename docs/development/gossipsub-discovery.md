# GossipSub discovery through group providers

**Current-source implementation.** The `syn.private-gossipsub-config/2` relay
discovers other relays by asking the Amino DHT for providers of its pub-sub
group's identifying hash. Each relay advertises that same hash. Its configuration
contains the selected native Agent scope and local endpoints, with no remote
relay PeerID list or relay address assignments.

The DHT supplies candidates. Native Agent signatures authorize each candidate's
current relay identity before private GossipSub traffic is admitted. The
existing complete-cohort readiness, encrypted message verification, and
session-loss behavior remain in force. This source addition does not change the
historical qualification of the [published 0.4.0 release](releases/v0.4.0.md).

## Exact group identifier

The identifier is the existing GossipSub scope digest. It commits to the entire
validated `syn.private-pubsub-scope/1`: profile, workspace UUID, fresh session ID,
Operator hash, network ID, group UUID, group epoch, and signed member
certificates. Certificate order is normalized by sorting on each certificate's
`body.node_public_key_hash`.

```python
from hashlib import sha256
from syn_client.operator_protocol import canonical

members = sorted(selected_scope["members"],
                 key=lambda cert: cert["body"]["node_public_key_hash"])
G = sha256(canonical({**selected_scope, "members": members})).digest()
```

Here `G` is a 32-byte digest. `syn_client.pubsub.group_hash(scope)` returns its
64-character lowercase hexadecimal form.

| Identifier | Exact representation |
| --- | --- |
| Group hash | Lowercase hexadecimal of `G` |
| Amino provider key | Binary `0x12 0x20 || G`, a 34-byte SHA2-256 multihash |
| GossipSub topic | `/syn/private-pubsub/1/` followed by the group hash |
| DHT routing target | `SHA256(provider_key)`, used for Kademlia distance |

`ADD_PROVIDER` and `GET_PROVIDERS` receive the same binary provider key. The
adapter does not hash the hexadecimal text again. Changing a committed scope
field or certificate changes discovery and the topic; reordering identical
certificates does not. The group UUID alone is not sufficient to identify this
workspace's live pub-sub session.

This uses the provider-based service-discovery pattern implemented by
[libp2p RoutingDiscovery](https://github.com/libp2p/go-libp2p/blob/v0.52.0/p2p/discovery/routing/routing.go).
No Bitswap block containing the private scope, certificates, or group key is
created or exported. Syn uses the exact digest above; it does not claim the
namespace mapping of Go GossipSub's
[`floodsub:` discovery wrapper](https://github.com/libp2p/go-libp2p-pubsub/blob/master/discovery.go).

## Discovery and admission

The relay creates its own TCP host using its retained Ed25519 transport seed,
Noise authentication and Yamux multiplexing. Its Amino adapter uses
`/ipfs/kad/1.0.0` as a client. It does not register an inbound Kademlia handler.
By default it resolves the five pinned
[official IPFS bootstrap peers](https://docs.ipfs.tech/concepts/public-utilities/#amino-dht-bootstrappers).

After routing warmup and local Agent attachment, the relay advertises its own
PeerID and permitted addresses under the group provider key. Before the cohort
is active, it repeatedly looks up that key and attempts bounded authentication
with returned providers. Each candidate is contacted using the PeerID returned
with its addresses; Noise verifies that the connected endpoint owns that
transport identity.

The additional `/syn/private-gossipsub/auth/1.0.0` exchange uses
`syn.private-gossipsub-auth/1`, the group hash, and the candidate's local Agent
proofs. Each proof has two parts:

- The existing Agent-signed hello, including the selected workspace, session,
  node, live boot and issuance time, plus its Operator-signed member certificate.
- An Agent-signed `syn.private-pubsub-relay-binding/1` statement committing to
  the group hash, relay public key and PeerID, node, boot, complete hello digest,
  and issuance time.

The binding is produced by the native Agent in response to the privileged local
`syn.private-pubsub-link/2` attachment. It is a fixed-purpose attestation;
the relay receives no Agent signing key, group encryption key, or general
signing interface. Verification checks the selected membership and current
scope, signature, freshness, and the actual authenticated remote PeerID.
Replaying another relay's otherwise valid proof does not authorize a different
transport identity. Duplicate or overlapping Agent assignments are rejected.

The initial outbound authentication request is sent to a candidate before that
candidate has proved admission. Noise encrypts this exchange in transit, but
the receiving candidate can read the initiating Agent's hello and member
certificate: Agent and boot identity, Operator, group, network, workspace, and
session metadata. Group encryption keys and application plaintext are never
supplied. This protocol does not promise complete group-metadata privacy from
unverified provider candidates.

`AuthenticatedPubsub` gates inbound and outbound pub-sub streams, subscription
announcements, control messages, and publications until admission. Public DHT
contacts and failed candidates do not receive the private pub-sub topic or
roster through GossipSub. A topic-message validator alone would not provide
this protection.

The admitted relays must still account for every member of the selected cohort,
exchange the same signed live roster, and agree on its digest before native
Agents become ready. The scope supports up to eight native Agents. Admission
does not grant Cog execution authority, enrollment, or DAG authority.

Implementation: [relay and discovery loop](../../src/syn/mesh_gossipsub.py),
[pub-sub admission gate](../../src/syn/mesh_pubsub.py),
[native attachment](../../src/syn/agent/pubsub.py), and
[public proof verification](../../packages/syn-client/src/syn_client/pubsub.py).

## Configuration

Update the Syn runtime, native Agent processes, and `syn-client` together before
using the v2 relay. The Agent must understand `syn.private-pubsub-link/2`, and
the installed client must provide group-hash and relay-proof verification.
The source change does not upgrade an already installed 0.4.0 package.

The configuration is canonical JSON in a file owned by the privileged local
controller with mode `0600`. Its directory and the relay seed directory must
pass Syn's private ownership and path checks. The CLI runs as the privileged
local deployment controller, as the existing native relay does.

| Top-level field | Meaning |
| --- | --- |
| `profile` | Exactly `syn.private-gossipsub-config/2` |
| `scope` | The same complete validated scope on every participating host |
| `endpoints` | This host's local Agents, each with `node`, `socket`, and `owner_uid` |
| `identity_path` | Canonical absolute path to this relay's private 32-byte seed |
| `listen` | Explicit canonical IP/TCP listening multiaddress |
| `discovery` | Optional object containing the settings below |

There is no `peers` field in v2. The controller selects the native Agent cohort;
provider discovery locates the relays hosting those Agents. An Agent must belong
to exactly one admitted relay within the live cohort.

### Build a canonical configuration

Prepare `scope.json` with the already selected scope and `endpoints.json` with
a single `endpoints` field containing this host's actual local Agent endpoint
array. Both input files should be
canonical JSON, mode `0600`, in the private relay directory. The example below
runs from that directory; it reads the real inputs and writes a new config
instead of inventing certificate or node values.

Export `SYN_RELAY_ANNOUNCE` with this relay's real, publicly reachable IP/TCP
multiaddress. If forwarding through a router, its public port must lead to
local TCP port `4002` in this example. Run the builder with the updated installed
Syn environment as the privileged controller:

```sh
: "${SYN_RELAY_ANNOUNCE:?Set this to the relay's real public IP/TCP multiaddress}"
export SYN_RELAY_ANNOUNCE
python - <<'PY'
import os
from pathlib import Path
import secrets

from syn.mesh_gossipsub import GossipRelay, PROFILE
from syn.operator.custody import private_read
from syn_client.operator_protocol import canonical, exact, parse
from syn_client.pubsub import group_hash, scope

directory = Path.cwd().resolve()
selected = scope(parse(private_read(directory / "scope.json")))
endpoints = exact(parse(private_read(directory / "endpoints.json")),
                  {"endpoints"}, "local endpoint input")["endpoints"]
seed = directory / "relay.seed"
if not seed.exists():
    with os.fdopen(os.open(seed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(secrets.token_bytes(32))

config = {
    "profile": PROFILE,
    "scope": selected,
    "endpoints": endpoints,
    "identity_path": str(seed),
    "listen": "/ip4/0.0.0.0/tcp/4002",
    "discovery": {"announceAddresses": [os.environ["SYN_RELAY_ANNOUNCE"]]},
}
GossipRelay(config)  # Validate configuration and private seed; opens no listener.
with os.fdopen(os.open(directory / "gossip.json",
                      os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
    output.write(canonical(config))
print("Group hash:", group_hash(selected))
PY
python -I -B -m syn.mesh --config "$PWD/gossip.json"
```

The builder preserves an existing valid seed and refuses to overwrite an
existing `gossip.json`. Each host keeps its own seed. The same selected scope
produces the same group hash without exchanging relay identities or addresses.

### Discovery settings and bounds

All settings below belong inside the optional `discovery` object. The relay does
not load them from the API service's `[p2p]` table.

| Setting | Default | Accepted values and effect |
| --- | --- | --- |
| `bootstrapPeers` | Five official IPFS DNSaddr entries with pinned PeerIDs | Up to 32 unique address strings; `[]` disables configured seed contacts |
| `announceAddresses` | `[]` | Up to 16 canonical bare IP/TCP addresses; otherwise use permitted addresses reported by the host |
| `allowPrivateAddresses` | `false` | Boolean; the explicit isolated-test override described below |
| `requestTimeoutSeconds` | `3` | Finite number greater than 0 and at most 60; individual DNS/RPC/authentication deadline |
| `lookupTimeoutSeconds` | `10` | Finite number greater than 0 and at most 120; bounds bootstrap, announcement, and discovery work |
| `bootstrapRefreshSeconds` | `600` | Finite number greater than 9 and at most 86,400 |
| `providerRepublishSeconds` | `10800` (3 hours) | Finite number greater than 59 and at most 79,200 (22 hours) |
| `discoveryIntervalSeconds` | `3` | Finite number from 1 through 30; delay between discovery passes before readiness |

Bootstrap and announcement failures retry with increasing delays, capped at
300 seconds. An address change makes a new advertisement due immediately.
The client also bounds DHT frames, responses, address counts, concurrent RPCs,
and iterative queries; see the [Amino adapter guide](p2p-amino.md).

## Public reachability and isolated tests

Public discovery accepts globally routable concrete IP/TCP addresses. A wildcard
listener such as `/ip4/0.0.0.0/tcp/4002` is a bind instruction and cannot be
advertised as a reachable endpoint. Use an actual public interface or explicit
`announceAddresses`, with firewall and forwarding rules that reach this relay.
The relay does not enable UPnP or mDNS. It does not implement NAT hole punching
or circuit-relay reachability. A successful outbound bootstrap connection does
not prove inbound reachability.

For an isolated laboratory swarm, explicitly supply the local DHT server's
full pinned bootstrap address and set `allowPrivateAddresses` to `true`.
For example, construct these values from a running test server's address:

```python
config["listen"] = "/ip4/127.0.0.1/tcp/0"
config["discovery"] = {
    "bootstrapPeers": [local_dht_bootstrap_address],
    "allowPrivateAddresses": True,
    "requestTimeoutSeconds": 2,
    "lookupTimeoutSeconds": 3,
    "discoveryIntervalSeconds": 1,
}
```

The bootstrap value comes from that test server and includes its actual
`/p2p/<PeerID>` suffix. The private-address override is rejected when configured
with the official bootstrap names. It is not a substitute for isolating a test
swarm, and it does not start a local DHT server. Keep public and laboratory
bootstrap configurations separate.

Provider advertisements are public metadata: observers can associate a known
group hash with provider PeerIDs and addresses. Hashing the scope does not
provide anonymity or make discovery an authorization mechanism. No scope,
certificate roster, plaintext message, or group secret is placed in a DHT value.

## Readiness, lifetime, and status

Startup first gives routing a bounded warmup, ten seconds by default, before
requesting short-lived native proofs. After local attachment, the existing
30-second cohort-readiness deadline begins. Start all participating relays
within the same startup window. A DHT outage is retried within these bounds;
it cannot mark an incomplete native cohort ready.

Once active, the cohort is fixed. DHT maintenance and advertisement continue,
but provider discovery does not add new relay assignments to an active session.
Loss of an admitted relay, native custody, or required live membership fences
the session. Recovery starts a fresh scope using the existing native deployment
procedure. DHT rediscovery does not silently replace a running Agent's relay or
resume a fenced session. Delivery remains volatile; publication acknowledgement
establishes local submission, while a verified signed receive response
establishes reception.

Programmatic `GossipRelay.discovery_status()` reports the group hash, binary
provider key as hex, routing state, publication state, announcements sent,
candidate count, authenticated peer count, rejected candidate count, and nested
Amino status. `announced` means advertisement messages were sent, not that remote
storage, global visibility, or public reachability was independently verified.
The CLI retains its existing JSON readiness report. The API application's
`/p2p/status` describes its separate Operator-discovery host.

## Compatibility and qualification

Existing `syn.private-gossipsub-config/1` configurations still use their explicit
`peers` rows and manual addresses. The old native link and signed message formats
remain supported. Use v2 on every relay in a discovered cohort and update its
native Agent runtime and client package together. No new immutable release or
installed-host qualification follows merely from source support.

The [group-discovery integration test](../../tests/test_gossipsub_discovery.py)
uses a real local DHT wire server, Noise/Yamux connections, two relays with no
static peer rows, and actual native v2 attachment and encrypted message handling
with explicitly synthetic custody. It verifies exact group keys, rejection of a
replayed proof from a different transport, a different workspace's provider
separation, complete readiness, encrypted delivery in both directions, and
absence of private pub-sub streams to the DHT contact.

The [binding tests](../../tests/test_gossipsub_binding.py) exercise native proof
verification, and the [admission-gate tests](../../tests/test_mesh_pubsub.py)
exercise stream and metadata isolation. The
[configuration and lifecycle tests](../../tests/test_gossipsub_config.py)
cover validation, admission conflicts, bounded startup, and shutdown. The
[legacy loopback test](../../tests/test_gossipsub_legacy.py) exercises actual
delivery with explicit v1 peer assignments. Run the focused tests with the
updated runtime and client installed:

```sh
uv run pytest tests/test_gossipsub_config.py tests/test_gossipsub_discovery.py \
  tests/test_gossipsub_binding.py tests/test_mesh_pubsub.py tests/test_gossipsub_legacy.py
```

Public WAN GossipSub group discovery has not been qualified in this workspace.
The earlier public bootstrap diagnostic could not reach the environment's DNS
resolver. Local cryptographic and protocol tests establish the behavior above;
they do not establish public connectivity, router configuration, or native
custody qualification on a deployed machine.
