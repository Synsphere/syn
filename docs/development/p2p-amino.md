# Public Operator discovery through Amino

This milestone implements public Operator discovery with `libp2p==0.7.0`,
TCP, Noise and Yamux. Syn joins the public IPFS Amino DHT as a **client** on
`/ipfs/kad/1.0.0`. It bootstraps, follows closer peers, announces its own provider
record and resolves other providers. It does not register or advertise an
incoming Kademlia server handler.

The stable discovery name is the installed Operator's existing base-public-key
hash. The libp2p host receives a separate, newly generated Ed25519 transport key
on every start. A short-lived, Operator-signed public record binds that transport
to the stable identity. Native custody remains responsible for Operator signing.

## Lookup key and records

Let `P` be the Operator's raw 32-byte base public key and `H = SHA256(P)`.
These identifiers serve different purposes:

| Value | Exact representation | Purpose |
| --- | --- | --- |
| Operator hash | Lowercase hexadecimal of the 32-byte `H` | Stable identity selected by the caller; 64 characters |
| Amino provider key | Binary `0x12 0x20 || H`, 34 bytes | SHA2-256 multihash supplied in `FIND_NODE`, `ADD_PROVIDER` and `GET_PROVIDERS` |
| Public-key CID | Binary `0x01 0x55 0x12 0x20 || H`; displayed as base32 multibase | CIDv1 with the raw codec for the exact public-key block `P` |
| Kademlia target | `SHA256(provider_key)` | Routing distance in the DHT's keyspace |
| Transport PeerID | Standard libp2p PeerID derived from the separate transport public key | Authenticated endpoint for this host lifetime |

The adapter sends the binary multihash unchanged. It does not hash the ASCII
hexadecimal Operator hash as the content name. The outer Kademlia routing hash
is part of routing and does not change the Operator's identity.

An Amino provider entry associates the public-key content name with a transport
PeerID and addresses. Syn makes that claim true by serving `P` over Bitswap
`/ipfs/bitswap/1.2.0` and `/ipfs/bitswap/1.1.0`. The service serves only this
public block, handles bounded Block/Have/DontHave requests and cancellation,
and neither stores incoming blocks nor exposes arbitrary files.

The mutable Syn binding is fetched directly from a candidate over
`/syn/operator/peer-record/1.0.0`. It is not stored as an arbitrary DHT value.
Amino's standard value namespaces have their own validation rules; this
implementation uses provider routing and sends no custom `PUT_VALUE`.
See the [IPFS Kademlia specification](https://specs.ipfs.tech/routing/kad-dht/)
and [Bitswap specification](https://specs.ipfs.tech/bitswap-protocol/).

### Signed binding

The public envelope contains exactly `body`, `public_key` and `signature`.
`public_key` is the raw base public key encoded as 64 lowercase hexadecimal
characters; `signature` is the 64-byte Ed25519 signature encoded as 128 lowercase
hexadecimal characters. The body contains:

| Field | Meaning |
| --- | --- |
| `profile` | `syn.operator-peer-record/1` |
| `operator` | Stable Operator base hash |
| `network_id` | Installed Operator network, such as `syn` |
| `peer_id` | Current libp2p transport PeerID |
| `transport_public_key` | Raw 32-byte transport public key in lowercase hex |
| `addresses` | Sorted, unique list of 1–16 canonical IP/TCP multiaddresses |
| `issued_at`, `expires_at` | Integer Unix seconds; validity no longer than one hour |

Signing bytes are `b"syn.operator.peer-record/1\0" + canonical(body)`.
Canonical JSON uses sorted keys, compact separators, ASCII escaping and no
non-finite numbers. Wire packets must be canonical, have no duplicate fields
and fit within 8 KiB. An address may have a terminal `/p2p/<peer_id>` only when
it matches the binding. Transport public-key derivation must also match
`peer_id`. This is Syn's application record format, distinct from libp2p's
own signed peer-record envelope.

Lookup first treats DHT results as untrusted candidates. It connects over the
authenticated transport and fetches the signed binding. The verifier checks the
independently selected Operator hash, network, base signature, transport public
key, actual remote PeerID, address grammar and expiry. Records issued more than
60 seconds into the future are rejected; expiry has no grace period. Public
discovery also requires every returned address to satisfy its public-address
policy. Only verified packets are returned to the caller.

Anyone can copy a public-key block or claim to provide it. Possession of that
block does not allow a peer to create an Operator-signed binding to its own
transport. Verification grants no enrollment, private-mesh membership, group
keys, DAG authority or permission to execute work.

Implementation: [binding and verifier](../../src/syn/peer_record.py),
[public streams](../../src/syn/peer_record_transport.py),
[Amino adapter](../../src/syn/amino.py).

## Bootstrap defaults

Omitting `p2p.bootstrapPeers` selects these five official DNSaddr/PeerID pins,
reviewed against the [IPFS bootstrap list](https://docs.ipfs.tech/concepts/public-utilities/#amino-dht-bootstrappers)
on 18 November 2026:

```toml
bootstrapPeers = [
  "/dnsaddr/sg1.bootstrap.libp2p.io/p2p/QmcZf59bWwK5XFi76CZX8cbJ4BhTzzA3gU1ZjYZcYW3dwt",
  "/dnsaddr/sv15.bootstrap.libp2p.io/p2p/QmNnooDu7bfjPFoTZYxMNLWUQJyrVwtbZg5gBMjTezGAJN",
  "/dnsaddr/am6.bootstrap.libp2p.io/p2p/QmbLHAnMoJPWSCR5Zhtx6BHJX9KiKNN6tpvbUcqanj75Nb",
  "/dnsaddr/ny5.bootstrap.libp2p.io/p2p/QmQCU2EcMqAqQPR2i9bChDtGNJchTbq5TbXJJ16u19uLTa",
  "/dnsaddr/va1.bootstrap.libp2p.io/p2p/12D3KooWKnDdG3iXw9eTFijk3EWSunZcFi54Zka4wmtqtt6rPxc8",
]
```

DNSaddr recursion and DNS/IP resolution are bounded, and resolved records must
preserve the configured PeerID. The supported resolved transport is concrete
IPv4/IPv6 TCP. Unsupported transports and impermissible addresses are ignored.

An explicit `bootstrapPeers = []` remains empty. This preserves existing
configurations; remove that line to adopt the defaults. An empty seed list does
not disable the DHT client itself. Set `aminoEnabled = false` for that.

## Configuration and startup

Install the locked project dependencies and start the normal Operator application:

```sh
uv sync --frozen
uv run syn-server --config config.toml
```

Use the existing [Operator setup](operator-node.md) and, on Windows, the
[host-custody launch flow](windows-host-custody.md). The default API bind remains
`127.0.0.1:8000`; the libp2p listener is a separate TCP service. The database and
Operator readiness requirements still apply.

The [example configuration](../../config.example.toml) contains these settings:

| `[p2p]` setting | Default | Effect |
| --- | --- | --- |
| `enabled` | `true` | Run the managed libp2p host |
| `aminoEnabled` | `true` | Run DHT bootstrap, provider routing and lookup |
| `publishOperatorRecord` | `true` | Serve the installed Operator's signed binding and public-key block; announce when Amino is available |
| `listenAddresses` | IPv4/IPv6 wildcard TCP port `0` | Bind available ports; use fixed ports for manual forwarding |
| `bootstrapPeers` | Five pins above | Initial DHT contacts; an explicit empty array is preserved |
| `announceAddresses` | `[]` | Optional public IP/TCP endpoints; empty uses public listeners, confirmed Identify observations and successful public UPnP mappings |
| `allowPrivateAddresses` | `false` | Permit private addresses only for explicitly configured, isolated test swarms |
| `mdnsEnabled` | `true` | Advertise and discover peers on the LAN |
| `upnpEnabled` | `true` | Discover and maintain verified, owned IPv4 TCP router mappings after listener readiness; remove them during orderly shutdown |
| `upnpMulticastInterface` | unset | Optional local LAN IPv4 address for SSDP discovery when VPN/virtual adapters interfere |
| `upnpDiscoveryDelayMilliseconds` | `2000` | SSDP discovery wait, between 100 and 10000 ms |
| `startupTimeoutSeconds` | `15` | Bound local host startup/shutdown waits |
| `requestTimeoutSeconds` | `10` | Bound individual DHT/public-stream requests |
| `lookupTimeoutSeconds` | `45` | Bound a complete lookup or maintenance operation |
| `bootstrapRefreshSeconds` | `600` | Refresh DHT contacts after successful bootstrap |
| `providerRepublishSeconds` | `79200` | Reannounce the provider after 22 hours |

`announceAddresses` accepts canonical `/ip4/<address>/tcp/<port>` or
`/ip6/<address>/tcp/<port>` values without a PeerID suffix. Ports must be
1–65535. Public mode requires globally routable addresses; wildcard, private,
loopback, multicast and link-local announcements are rejected. Explicit
announcements must lead to the listener through the actual firewall/NAT setup.
They are configuration assertions, not reachability measurements.

For manual forwarding, choose a fixed listener such as
`/ip4/0.0.0.0/tcp/4001` and configure its actual public IP and externally forwarded
port in `announceAddresses`. A reported UPnP external IP alone is insufficient:
automatic announcements require a successful mapping for an active IPv4 TCP
listener. IPv6 can use a directly reachable public listener. This milestone
does not supply a relay or hole-punching fallback.

The `upnp` object in `GET /p2p/status` reports the discovery/mapping stage,
selected LAN address, mapping readback and WSL networking mode. See
[runtime network recovery](runtime-network-recovery.md) for the Windows/WSL
helper and DrayTek troubleshooting.

The private-address option requires explicitly selected isolated bootstrap
peers; configuration rejects its combination with the official bootstrap
domains. Never connect a private test swarm to public seeds. A stock Kubo WAN
DHT still filters loopback provider addresses even when the Syn test client
allows them; the independent acceptance below checks that behavior explicitly.

`aminoEnabled = false` leaves the host and, when enabled, its local signed-record
service available. `publishOperatorRecord = false` disables both public record
serving and the single-block Bitswap provider service, while lookup can continue.
`enabled = false` disables the managed host entirely. `--api-only` has no
Operator signer, so it cannot issue or announce an Operator record; its enabled
libp2p host can still perform discovery.

## Lifecycle and Windows approval

The Operator is ready before the public host starts. One Trio runtime owns the
live libp2p host, connections, DHT maintenance and public streams; the HTTP API
bridges into that runtime. Bootstrap and publication run in the background after
listeners are ready. DNS failures and unavailable public peers are retryable
discovery failures and do not make an otherwise healthy local node unready.
Retry delays grow from five seconds to five minutes.

Publication waits until there is a permitted advertised address. It then asks
the existing native Operator signer for a binding valid for one hour. The
binding is refreshed when no more than 30 minutes remain, and immediately when
the address set changes. Provider reannouncements have their separate configured
interval. Address changes cause an immediate new announcement attempt once the
new binding is signed and a DHT server is available.

On the Windows host-custody profile, first publication uses a dedicated
`peer_record` request. The native prompt shows the Operator hash, network,
transport PeerID and exact addresses. The host retains approval for that exact
binding before releasing a signature. Changes to the network, transport key,
PeerID or addresses require another approval. Only the issue/expiry times may
refresh under the retained approval, and the host independently checks the
one-hour lifetime on every request. General credential access cannot replace
the retained approval. Generic signing continues to require separate native
approval and does not gain the retained-refresh exemption.

A new host start creates a new transport key, so restarting Syn requires fresh
Windows approval when publication resumes. The base Operator identity does not
change. Update and restart both the Windows host broker and WSL runtime together
so the broker supports the new request profile. A pending prompt does not block
local host readiness or shutdown, and
a signature arriving after shutdown is not published. A declined or failed
native signing operation stops the public transport and triggers the existing
application readiness fence. These are code-tested semantics; real Windows
DPAPI/dialog/WSL acceptance retains the separate gates in the host-custody guide.

Shutdown clears the served binding and closes the host. Existing provider
entries can remain in remote DHT caches; remote discovery must still fetch and
verify a current signed binding. There is no immediate global revocation or
consensus over all providers in this milestone.

## HTTP inspection and lookup

These routes expose public discovery information on the configured API listener:

| Route | Result |
| --- | --- |
| `GET /p2p/status` | Host status, actual listeners, configured seeds, discovered peer IDs, Amino and publication state |
| `GET /p2p/peer-record` | Current local signed envelope; HTTP 409 when none is available |
| `GET /p2p/operators/{operator_hash}?network=syn` | `{"operator": "…", "network": "syn", "peers": [...]}` containing verified signed envelopes |

```sh
curl --fail http://127.0.0.1:8000/p2p/status
curl --fail http://127.0.0.1:8000/p2p/peer-record
operator_hash='REPLACE_WITH_THE_64_CHARACTER_LOWERCASE_OPERATOR_BASE_HASH'
curl --fail "http://127.0.0.1:8000/p2p/operators/${operator_hash}?network=syn"
```

Select the expected Operator hash through the application's existing trust
relationship. Change `network` to the Operator's actual network when it differs
from `syn`. The network grammar is `[a-z][a-z0-9-]{0,63}`.

Lookup returns at most 20 verified providers, deduplicated by PeerID and ordered
by newest issue time, then PeerID. HTTP 200 with an empty `peers` array means the
answered lookup produced no currently verified, reachable binding. Malformed
inputs return 422; exhausted lookup capacity returns 429; disabled, unavailable
or timed-out discovery returns 503. Native readiness failures also fence the
API with 503. Provider results that fail identity or address checks are omitted.

### Interpreting status

Always inspect top-level `enabled` and `running` first. `amino.state` can be
`stopped`, `disabled`, `bootstrapping`, `ready`, `unavailable` or `failed`.
`ready` records successful DHT queries, not public inbound reachability.
The detailed `bootstrapStatus`, `routingPeers`, `queryFailures` and lookup fields
describe the adapter's most recent work.

| `publication.state` | Meaning |
| --- | --- |
| `disabled` | Public Operator publication is configured off |
| `no-operator` | No native Operator was supplied, including `--api-only` |
| `waiting-addresses` | No permitted advertised TCP endpoint is available |
| `signing` | Native binding issuance or approval is in progress |
| `signed` | A current binding exists; provider announcement has not completed |
| `announced` | At least one provider message was successfully sent |
| `unavailable` | The last attempted provider announcement failed; it will retry |

`providerKey` is the multihash in hex and `cid` names the raw public-key block.
`addresses` and `expiresAt` describe the current binding. `announcedTo` and
`lastAnnouncedAt` describe sent provider messages. Kubo's `ADD_PROVIDER` handling
does not return a storage acknowledgement, so these fields establish neither
remote persistence nor visibility from another public node. In particular,
`discoveredPeers` is a transport peerstore view and does not mean those peers
are authenticated Syn Operators.

## Bounds and qualification

The DHT adapter bounds response frames to 128 KiB, a lookup to 64 queried peers,
candidate sets to 256, known peers to 512 and concurrent DHT queries to ten.
The API permits four concurrent Operator lookups and three parallel candidate
fetches within each lookup. Public record/Bitswap handling has a shared inbound
stream limit, finite request timeouts and bounded frames/wantlists. Cancellation
propagates across the HTTP/Trio bridge so abandoned requests release their slots.
These limits are implementation capacities, not claims of DHT completeness
under arbitrary network churn.

Focused validation can run without PostgreSQL or public network access:

```sh
uv run pytest -q tests/test_amino.py tests/test_peer_record.py \
  tests/test_peer_record_transport.py tests/test_peer_record_host.py \
  tests/test_p2p_identity_lifecycle.py tests/test_p2p_amino_runtime.py \
  tests/test_p2p.py tests/test_config.py tests/test_api.py
```

The existing mDNS test requires LAN interface enumeration and multicast support;
when those capabilities are unavailable, exclude only that test with
`-k 'not two_nodes_discover_each_other_over_mdns'` and retain it as a native
deployment check.

Tests cover binary-key routing, pinned DNSaddr resolution, closer-peer lookup,
timeouts and cancellation, real Noise/Yamux streams, exact public-key Bitswap
retrieval, forged/expired/mismatched bindings, address filtering, approval
retention and changes, host restart, pending approval and custody failure.

The optional independent acceptance uses a separately installed official Kubo:

```sh
uv run python scripts/verify_amino_interop.py \
  --kubo /absolute/path/to/ipfs --report amino-interop.json
```

The script starts a disposable loopback-only Kubo repository with synthetic
identities, no bootstrap peers, no delegated routing, no automatic configuration,
no mDNS/relays and no reproviding. It cleans up that process and repository.
It does not publish a real Operator or send synthetic provider records to the
public network.

On 18 November 2026, an official Kubo **0.43.1** binary, checked against its
published SHA-512 checksum, passed these independent exchanges:

| Exchange | Observed result |
| --- | --- |
| Kubo retrieves Syn's raw public-key CID over Bitswap | Exact 32-byte block; SHA-256 equals the Operator hash |
| Two independent Syn clients bootstrap to Kubo on `/ipfs/kad/1.0.0` | Both completed `FIND_NODE` exchanges |
| Syn sends `ADD_PROVIDER`; the second host sends `GET_PROVIDERS` | Kubo returned the exact provider PeerID under the binary provider key |
| WAN address handling | Kubo removed loopback addresses; Syn correctly rejected the undialable candidate |
| Signed binding fetched using the explicit loopback fixture address | Signature, Operator/network and authenticated transport PeerID verified |

The last fetch uses an explicitly known test address because Kubo's WAN DHT
filters loopback. It is separate from address discovery. The JSON report records
that source and `publicReachabilityTested: false`; this test does not establish
public propagation, real NAT traversal or public-provider address retrieval.

A read-only attempt to contact the five official public bootstrap peers in the
build environment failed at DNS: its configured resolver was unreachable
(`Network is unreachable`). No public TCP/Noise/DHT exchange was reached.
External lookup and inbound reachability therefore remain deployment acceptance
steps on a host with working DNS, outbound TCP and a reachable advertised port.
For the independent implementation and release binary, see
[Kubo 0.43.1](https://github.com/ipfs/kubo/releases/tag/v0.43.1).

Repeat the read-only public bootstrap check from the deployment host:

```sh
uv run python scripts/check_amino_bootstrap.py
```

It uses a temporary transport identity, reports DNS resolution separately from
authenticated DHT responses, and publishes no provider records. Exit status zero
means at least one server answered. A successful bootstrap alone does not verify
that another public node can find and dial the Operator's advertised endpoint.
