# Authenticated libp2p transport qualification (source milestone)

Target: Syn `a3a271c6ac832085672ab820f4acaeb0979b83ed`.

This milestone adds `/syn/transport/probe/1.0.0` to the **managed public P2P
host** and a witness executable for QUIC-v1, WebRTC Direct, and WebTransport.
The probe is public and unauthoritative: it has a small byte/frame budget,
eight concurrent slots, a four-second timeout and no database or custody
access. It cannot enroll an Agent, attest a Syn Operator, or publish an
address. The successful response is a challenge acknowledgement **inside** a
libp2p-authenticated session, not a standalone signature.

## Why a new probe?

`/p2p/status` already reports which transports are registered and which UDP
sockets opened. A registered adapter or open socket does not establish that a
remote party can connect, nor that libp2p did not fall back to TCP. The witness
verifies a fresh nonce and the actual QUIC/WebRTC/WebTransport muxer class and
transport PeerID. It accepts exactly one pinned destination address. It does
not fall back to TCP/Noise/Yamux when QUIC or WebTransport fails.

The witness additionally accepts a canonical `/dns4/.../p2p/PeerID` address. It
performs a bounded IPv4 A lookup and rejects privately routed results unless
`--allow-private` is supplied for an isolated laboratory. The resulting IP is
validated against the requested protocol and the original pinned PeerID.

## Local and WAN tests

To exercise a loopback two-node test with real signed transports:

```sh
uv run --locked pytest -q tests/test_p2p_transport_probe.py
SYN_TEST_TRANSPORT_LOOPBACK=1 uv run --locked pytest -q \
  tests/test_p2p_transport_probe.py -k test_real_libp2p
```

If `aiortc`, `aioice` or `av` are absent, pytest explicitly **skips** only the
WebRTC test and does not pretend that WebRTC works. The package still pins
`libp2p==0.8.1`; this milestone does not edit `uv.lock`, install optional
wheels at startup, or silently bypass the signed installed-runtime manifest.
Native WebRTC packaging is the next release-qualification dependency gate.

From a genuinely **separate host or network**, use a peer ID and a verified
public UDP endpoint from the managed Syn node:

```sh
uv run --locked python scripts/verify_syn_transport.py \
  '/ip4/PUBLIC-IP/udp/4001/quic-v1/p2p/PEER_ID'
uv run --locked python scripts/verify_syn_transport.py \
  '/ip4/PUBLIC-IP/udp/4002/quic-v1/webtransport/certhash/HASH/p2p/PEER_ID'
uv run --locked python scripts/verify_syn_transport.py \
  '/ip4/PUBLIC-IP/udp/4003/webrtc-direct/certhash/HASH/p2p/PEER_ID'
```

The IP, port, certificate hash and PeerID must be the **actual values** of the
running peer, not the examples above. WebTransport/WebRTC advertised addresses
include their certificate hashes. Check that any published endpoint is really
reachable before public announcement. Do not use the witness as evidence of
other networks' reachability or as an Operator identity proof.

For private loopback testing, pass `--allow-private` and a private address.
This does **not** permit use of public Amino bootstrap peers with private
addresses or lower Syn's existing validation policy.

## Windows 10 / WSL2 / DrayTek

This does not alter the working TCP portproxy, Windows custody, router UPnP,
Windows firewall, NAT or multicast bridge. Windows `netsh interface portproxy`
forwards TCP, not UDP. QUIC/WebRTC Direct/WebTransport need a separately
provisioned Windows-host UDP mapping/proxy; the existing optional Syn bridge
can supply that function when explicitly configured and permitted. A router
mapping receipt is **not** a successful WAN transport probe.

## Qualification result boundaries

The pure challenge-contract tests run without libp2p. Real transport tests
require the pinned 0.8.1 runtime and optional WebRTC dependencies. They must
pass on the supported Linux/Python 3.12 and 3.13 matrix, then on Windows/WSL,
and finally from an independent external network. The only production
behaviour added here is a bounded read-only responder; no new credentials,
protocol version or routing authority is introduced.
