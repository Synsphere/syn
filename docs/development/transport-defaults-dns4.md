# Default-on libp2p transport adapters and bounded DNS4 dialling

**Source target:** Syn `master` at `7d647cb55633181362b6b2a5e55ee35a76f5a1f7`.
This is a source milestone and not an immutable Syn release or an accepted
public WAN connectivity claim.

## Defaults and overrides

The managed Syn host now *requests* TCP, QUIC-v1, WebRTC Direct, WebTransport,
WebSocket and DNS4 support by default. It registers outbound transports
independently from its actual listen-address list: the py-libp2p 0.8.1
constructor otherwise picks only the protocols present in the listen list,
which causes the misleading `registered: ['TCP']` warnings on perfectly usable
QUIC peers. Existing manually chosen TCP ports, Operator custody, GossipSub
v1.2 with v1.1 fallback and DrayTek/Windows 10 forwarding remain unchanged.

Defaults (an explicit `false` disables a component):

```toml
[p2p]
quicEnabled = true
webRtcEnabled = true
webTransportEnabled = true
webSocketEnabled = true
dns4Enabled = true
```

The local listeners use ephemeral UDP ports by default for QUIC-v1,
WebRTC Direct and WebTransport. A listener or authenticated outbound connection
is **not** proof of LAN or WAN ingress. In particular, Windows 10 `portproxy`
does not forward UDP. The separate host-owned UDP sandbox bridge must be
provisioned and permitted to map inbound UDP by its owner. No unsolicited
firewall, router or host-custody changes occur during Syn startup.

A configured `quicEnabled = false` also prevents creation of the QUIC adapter;
WebTransport remains independent and can still be used if enabled. WebRTC
Direct is not the relay-based `/webrtc` transport; relay circuit addresses
remain outside this profile.

### WebRTC media dependencies

The upstream py-libp2p 0.8.1 wheel declares `aiortc`, `aioice` and `av` as
optional extra packages. To avoid breaking normal TCP/QUIC startup if those
platform-specific binaries are unavailable, Syn reports
`transportCapabilities.webRtcDirect = "missing-optional-dependency"` and
continues with the other transports. The capability is *configured on* by
default but cannot function until those packages are installed in Syn's
managed environment; this source milestone does not silently install packages
or forge a lockfile update. A future package-qualification milestone should
add the reviewed `libp2p[webrtc]` extra and regenerate and test the lock on
Python 3.12/3.13. The current app and release wheels remain pinned.

### DNS4 resolution and security

Syn's managed Swarm resolves `/dns4/<hostname>/...` entries from its
peerstore **before** upstream transport selection. It accepts A records only,
bounds name length, result count and total lookup time, and preserves the
original transport suffix (TCP, WS/WSS, QUIC, WebRTC Direct with certhash,
WebTransport with certhash). A mismatching `/p2p/<peer-id>` suffix is rejected.
Public mode accepts only globally routable IPv4 results; private resolution
requires the existing `allowPrivateAddresses` isolated-lab configuration.
DNS results are addresses, not credentials: transport peer authentication and
Syn Operator/Agent proof checks are unchanged.

`/p2p/status` now reports `registeredTransports` and `transportCapabilities`.
A configured flag is not evidence that its implementation was registered.
Known unsupported remote address warnings are grouped in `dialDiagnostics`,
without suppressing the first occurrence or unknown failures.

## Qualification

```bash
uv sync --locked --dev
uv run --locked python -m pytest -q tests/test_dns4_transport_defaults.py \
  tests/test_default_transports_config.py tests/test_public_gossipsub_v12_quic.py \
  tests/test_p2p.py tests/test_public_pubsub_runtime.py tests/test_p2p_diagnostics.py
uv run --locked python -m pytest -q tests -m 'not database'
```

Independent offline tests cover the adapter-construction flags, strict DNS4
transport grammar, immutable pinned PeerIDs and public/private resolution
policy. They do not establish successful real WebRTC, WebTransport or remote
DNS4 sessions on the Windows 10 host. Installed native transport integration,
real two-node interoperability, UDP forwarding, certificate verification and
WAN reachability remain explicit acceptance gates before release.
