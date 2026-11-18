# Cross-network transport qualification

This milestone adds *opt-in*, read-only qualification of the existing Syn
`/syn/transport/probe/1.0.0` responder. The evidence is a **fresh challenge over
the actual negotiated QUIC-v1, WebRTC Direct or WebTransport connection** to a
pinned libp2p PeerID, not a socket bind, local UPnP readback or DNS resolution.
The existing `scripts/verify_syn_transport.py` performs the authenticated test;
`scripts/qualify_syn_transports.py` coordinates retries and reports one result
per transport. It does not replace the existing verifier or change custody.

## Independent Internet witness (GitHub Actions)

1. Start Syn and inspect `uv run --locked syn-server --check-runtime`. Retrieve
   the actual UDP multiaddrs and PeerID from `GET /p2p/status`. Use real
   certificate-hash-bearing WebRTC Direct and WebTransport addresses, not the
   configured wildcard/port-zero listen addresses. **Do not guess hashes.**
2. Confirm that external UDP ingress is already explicitly provisioned. On
   Windows 10 / WSL2 NAT, TCP `netsh portproxy` cannot forward these UDP ports.
   The separately provisioned Syn Windows-host bridge must forward UDP through
   the firewall/router, or configure networking with verified equivalent ingress.
3. In GitHub Actions, select **Syn external transport qualification** →
   **Run workflow** and enter exactly three *public* pinned multiaddrs, one for
   each transport, all with the same PeerID. The workflow runs on a separate
   GitHub-hosted runner, **without** `--allow-private`, with the pinned lockfile.
   The input is a public endpoint, never a secret or operator configuration.
4. Read the job summary and the 14-day `syn-transport-qualification` artifact.
   Each transport needs at least two authenticated challenge successes across
   three independent trials. Failures (including missing WebRTC native runtime
   wheels) remain failures and cannot be silently passed as TCP fallback.

Manual invocation from a **different host/network** is equivalent:

```sh
uv run --locked python scripts/qualify_syn_transports.py \
  --peer '/ip4/ACTUAL_PUBLIC_IPV4/udp/4001/quic-v1/p2p/ACTUAL_PEER_ID' \
  --peer '/ip4/ACTUAL_PUBLIC_IPV4/udp/4002/webrtc-direct/certhash/ACTUAL_CERT_HASH/p2p/ACTUAL_PEER_ID' \
  --peer '/ip4/ACTUAL_PUBLIC_IPV4/udp/4003/quic-v1/webtransport/certhash/ACTUAL_CERT_HASH/p2p/ACTUAL_PEER_ID' \
  --trials 3 --min-success 2 --timeout 10 --report qualification.json
```

Use the actual addresses printed by Syn, including `certhash` elements. DNS4
A-record endpoints are supported; DNS resolution must still end at a public
IP address and the same pinned identity. This command is intentionally strict:
absent, mismatched, incorrectly authenticated, or unverifiable transports
produce a nonzero exit code. `--require quic-v1` permits a narrowly scoped
one-transport diagnostic but **does not** certify the full three-transport
stack. Private addresses are accepted only with `--allow-private` for isolated
LAN/netns tests, and then the result is not an Internet ingress test.

Reports contain no raw endpoint strings or credentials. `qualified=true`
means **these transports worked from this witness's network at this time**;
it does not prove global reachability, ongoing uptime, operator identity, or
third-party trust. The witness subprocess itself explicitly reports
`operatorIdentityVerified=false` and `publicReachabilityProved=false`, which
the batch runner checks rather than silently upgrading its claims.

## WSL/LAN multicast witness (separate scope)

mDNS/multicast is inherently LAN/link-local or scoped-relay traffic. An
Internet runner **must not** claim to certify it. For a Windows↔WSL or
LAN↔LAN relay, use two separate hosts (or WSL and Windows), each with a locally
assigned IPv4 address, and a **separate temporary 32-byte witness key**. Do
not reuse Operator keys, Syn identity keys, or the WSL bridge control secret.

For example, on a trusted Linux machine, create the *same* test-only witness
key on both peers through a secure out-of-band transfer (also restrict the
witness file to an owner-only ACL on Windows):

```sh
umask 077
python -c 'import os,secrets; fd=os.open("multicast-witness.hex",os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); os.write(fd,secrets.token_hex(32).encode()); os.close(fd)'
```

On the *receiver* (start first):

```sh
python scripts/qualify_syn_multicast.py listen \
  --secret-file multicast-witness.hex --interface RECEIVER_LAN_IPV4 \
  --group 239.255.42.99 --port 49871 --timeout 60
```

On the *sender* (different machine/interface):

```sh
python scripts/qualify_syn_multicast.py send \
  --secret-file multicast-witness.hex --interface SENDER_LAN_IPV4 \
  --group 239.255.42.99 --port 49871 --timeout 10
```

The sender multicasts a fresh nonce with a bounded TTL of 1. The receiver
checks its HMAC, timestamp, group and port and sends a signed unicast receipt.
The sender only succeeds when the corresponding receipt is authenticated.
`verified=true` proves the configured multicast *and receipt* path between
those two machines at that time. A failure can mean dropped multicast, blocked
unicast receipt, absent relay, wrong interface or firewall policy; neither
machine changes network settings.

The administratively scoped default test group/port must be explicitly added
to the Syn multicast bridge's approved group list on both sides when testing
the relay. To check the actual mDNS forwarding path, it is possible to select
`--group 224.0.0.251 --port 5353`, but this may conflict with an existing mDNS
service and **does not** validate that mDNS service discovery itself succeeds.
Use `--allow-loopback` only for the optional local socket smoke test. Multicast
is never treated as proof of WAN reachability.

## CI and regression coverage

`tests/test_cross_network_qualification.py` exercises destination validation,
identity pinning, per-transport challenge evidence, mismatch rejection, scope,
result redaction and retry/failure thresholds. `tests/test_multicast_qualification.py`
checks challenge/receipt HMACs, freshness, scoped interfaces and private key
permissions. The existing main CI pytest command discovers these automatically.
For an additional **local-only** UDP multicast socket test:

```sh
SYN_TEST_MULTICAST_LOOPBACK=1 uv run --locked python -m pytest -q tests/test_multicast_qualification.py
```

No test claims an Internet path works without real remote evidence. No
production listener, operator identity, P2P routing policy, UPnP ownership,
multicast bridge configuration or database schema is changed by this milestone.
