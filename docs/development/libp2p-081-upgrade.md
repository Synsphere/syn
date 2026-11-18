# py-libp2p 0.8.1 migration — next P2P milestone

## Scope and authority

Upgrade Syn from pinned `libp2p==0.7.0` to `libp2p==0.8.1`, keeping
TCP/Noise/Yamux and the existing `/meshsub/1.1.0` public and private profiles.
This milestone changes **no** Windows 10 portproxy, DrayTek UPnP, P2P listener,
Operator custody, Agent admission, provider key, peer-record or `syn://` contract.
The Operator base-public-key hash remains the authoritative lookup identity.

The upstream release brings hardened varint decoding, binary GossipSub control
message IDs, pub-sub subscription/frame quotas, new transport options and
substantial protocol changes. We preserve Syn's current wire profiles rather
than enabling GossipSub v1.2/2.0 opportunistically during a dependency upgrade.

## Compatibility adaptations

- Public GossipSub restricts inbound RPCs to 1 MiB, and inbound subscription
  counts to 128 per RPC and per peer, in addition to the existing local-topic
  and accepted-provider gates. No public DHT discovery grants private admission.
- Private GossipSub bounds inbound RPCs to 64 KiB and subscription counts to
  `MAX_PEERS` (8). Overrides exceeding these limits are rejected before opening
  a stream. Its native Agent proof, epoch and revocation checks are unchanged.
- Both routers remain on `/meshsub/1.1.0` with adaptive mesh geometry and
  multi-IP eclipse requirements turned off for this *legacy* profile.
  Upstream 0.8.1 defaults to a 10-msg/s per-topic rate limit, which would
  truncate Syn's existing burst tests. We retain a finite per-topic bound:
  512 messages/s publicly, 128 privately. This does not replace Syn's signed
  message validation, per-group byte limits, retention quotas or admission.
- Upstream 0.8.1's signed peer-record, yamux and transport fixes are used; no
  new private-key persistence, transport protocol or public authority is added.

## Validation before merge/release

The source and hash-verified `uv.lock` record `libp2p==0.8.1` and
`prometheus-client==0.26.0`. On a machine with PyPI access, qualify both
Python 3.12 and 3.13, and inspect the lock before accepting the change:

```sh
uv lock --check
uv sync --locked --dev
uv run --locked python -m pytest -q tests/test_libp2p_081.py \
    tests/test_p2p.py tests/test_p2p_amino_runtime.py \
    tests/test_public_pubsub_engine.py tests/test_public_pubsub_runtime.py \
    tests/test_mesh_pubsub.py tests/test_gossipsub_discovery.py \
    tests/test_gossipsub_legacy.py tests/test_gossipsub_binding.py
uv run --locked python -m pytest -q tests -m 'not database'
```

A successful lock/suite qualifies source-level behaviour, not a successful WAN
bootstrap or DrayTek mapping on a deployed Windows/WSL host. Repeat native
Operator, private Agent cohort, and public DHT/remote peer acceptance on the
real deployment before publishing a release. Keep signed installed-runtime
and private-custody acceptance gates from existing release contracts.

## Next milestone after acceptance

With v1.1 migration validated, add a separately configurable, opt-in v1.2 and
then v2.0 public GossipSub profile. Use dedicated interoperability and abuse
fixtures for IDONTWANT, binary message IDs, PEER_EXCHANGE rejection, scoring,
message-size bounds, churn, signed envelope compatibility and hostile peers.
Do not silently upgrade private Agent cohorts; require updated native clients,
new proof-bound session acceptance and evidence under the existing release gates.
