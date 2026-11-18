# Public GossipSub v1.2 and optional QUIC transport

This milestone extends the pinned py-libp2p 0.8.1 transport on Syn `master`.
Public GossipSub now prefers `/meshsub/1.2.0` and offers `/meshsub/1.1.0`
for older peers. Set `publicGossipsubVersion = "1.1"` to pin the original
public profile. Private Agent-cohort GossipSub **remains v1.1**, with its
certificate-bound admissions, revocation and epoch fences unchanged.

## QUIC is an additional transport, not a replacement

`quicEnabled = true` adds the configured UDP/QUIC-v1 listeners alongside the
existing TCP listeners. The default QUIC port is ephemeral, so use a fixed
UDP port for deployment. QUIC uses its own TLS 1.3 transport authentication;
TCP continues using Noise/Yamux. No signing key or Operator custody moves.

Example, merged into the existing `[p2p]` config:

```toml
publicGossipsubVersion = "1.2"
listenAddresses = ["/ip4/0.0.0.0/tcp/4001"]
quicEnabled = true
quicListenAddresses = ["/ip4/0.0.0.0/udp/4001/quic-v1"]
```

This creates both sockets when available. `GET /p2p/status` shows the local
`quicListeners` and `quicPublicReachability = "unverified"`: binding UDP is **not**
proof that an external machine can dial it. Syn's existing public Amino/provider
identity-binding and announce path remains TCP-only. QUIC direct dial is available
to peers that already know the QUIC multiaddress; public QUIC provider publication
requires a separate authenticated UDP reachability qualification.

**Windows 10 / WSL2 NAT:** The current Windows `netsh interface portproxy` helper
for Syn forwards **TCP only**. It cannot forward UDP/QUIC, regardless of DrayTek
UPnP configuration. Keep the TCP service and its verified forwarding intact.
Use a separately approved UDP-capable Windows/WSL forwarding mechanism before
trying WAN QUIC. Never configure a public QUIC announcement solely from a
router mapping receipt or from a successful UDP bind.

## v1.2 IDONTWANT

Syn's public control filter keeps the existing joined-topic, PX-drop and
message-count budgets. It passes bounded `IDONTWANT` controls only from
negotiated v1.2 peers with a registered pub-sub stream. Unsupported v1.3+
extensions and unsolicited peer exchange remain excluded. Private Agent cohorts
continue to use only v1.1.

## Validation

```sh
uv run --locked pytest -q tests/test_public_gossipsub_v12_quic.py \
    tests/test_libp2p_081.py tests/test_public_pubsub_engine.py \
    tests/test_public_pubsub_control.py tests/test_public_pubsub_runtime.py \
    tests/test_mesh_pubsub.py tests/test_gossipsub_discovery.py
uv run --locked pytest -q tests -m 'not database'
```

Qualification additionally requires two real libp2p hosts delivering over
QUIC, mixed v1.1/v1.2 public peers exchanging signed publications, and WAN
UDP external reachability before any public QUIC advertising. No release or
production host acceptance follows solely from local source tests.

Next: run hostile-peer control-flood/failure-injection tests and evaluate
GossipSub v2.0 behind a separate opt-in gate. Do not silently change
private Agent cohorts or operator-public-record authority.
