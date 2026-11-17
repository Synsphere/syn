# Operator DAG milestone 4: proof-verified checkpoint witnessing

Base: `fa9fdae06b2ddf3d92ee20ca2a33dc04aed4c4a9` (host anchors merged).

This is **public checkpoint replication and witnessing**, not replication of
private Operator transactions, not a new consensus algorithm, and not BFT
finality. Existing authority/adoption journals and original receipt bytes are
unchanged. The existing ProcessScheduler performs all recurring work.

## Closing milestone 3 gaps

The Windows host now verifies a Merkle consistency proof itself before advancing
its own DAG anchor. Merely having a valid signature and the right predecessor
checkpoint digest is no longer sufficient. Existing host anchors retain their
format and slot; the runtime supplies proofs from the verified PostgreSQL DAG.

A PostgreSQL session advisory lock spans **verification, DB commit, and the
separate custody update**, preventing a second synchronizer from racing across
that commit boundary. Database state is committed first. If host persistence
fails, restart replays already committed signed checkpoints without re-signing.
A failed vault write poisons the live broker's cached state until restart: it
cannot acknowledge a value that only survived in memory.

Startup acquires Operator readiness first and synchronizes its DAG once during
scheduler preparation, before the scheduler pump starts. Local DAG/custody job
failures subsequently fence API readiness and stop P2P/worker activity. Remote
unavailability is instead an ordinary recorded observation; it does not fence
the local Operator.

## Explicit peer policy

Add `[witnessing]` to the **same TOML configuration that your launcher passes to
syn-server**. New installations commonly use a private config under the Linux
user's `.local/state/syn/`; an explicitly selected config remains authoritative.
See `config.witnessing.example.toml`.

```
[witnessing]
pollSeconds = 120
timeoutSeconds = 5

[[witnessing.peers]]
operator = "<independently obtained 64-character lowercase public-key hash>"
network = "syn"
url = "https://your-other-operator.example"
```

The hash above is a placeholder, not a usable identity. Each peer needs an
independently obtained Operator public-key hash and an exact network identifier.
The same network name is not membership, trust, or authorization. You cannot
configure yourself as an independent witness or silently change a retained
peer's network. Configure both Operators to witness one another for reciprocal
coverage, or use additional independently operated witnesses.

Peers are opt-in; an empty list causes **no outbound witness traffic**. HTTPS
uses the system trust store and certificate/hostname verification. Deliberate
LAN/loopback testing can set `allowHttp = true` for an `http://` origin. This
permits an observer on that network to see public metadata or delay/replay
answers, though they cannot forge the pinned signatures. There are no redirects,
ambient proxy settings, cookies, credentials-in-URLs, or endpoint discovery.
Only fixed protocol paths on explicitly configured origins are requested.
Responses are limited to 128 KiB, no compression, unambiguous JSON, and bounded
proofs. Socket timeouts and response read budgets apply; DNS uses OS resolver
limits, not a separate resolver sandbox.

An optional `initialCheckpoint` sets an independently saved checkpoint digest
for first contact. Without it the initial signed history is a bootstrap trust
choice under the pinned identity: the witness cannot detect rewriting that
happened before its first observation. If an exact initial digest is configured,
the peer must currently advertise that checkpoint at first contact; pin a
current checkpoint or use an explicit evidence-import workflow later.

## Polling and verification

1. Validate local retained evidence and reconcile it with independent custody
   **before** consulting the network. A missing anchored checkpoint is a local
   continuity failure, never repaired by trusting the remote's latest answer.
2. Pull the peer's signed checkpoint and check its exact identity and network.
3. For growth, request the consistency proof to the exact newly observed size.
   `GET /v1/operator/dag/consistency/{old_size}?new_size={target}` avoids mixing
   proofs with a checkpoint that changed between the two requests. Both proof
   endpoints must match the retained and newly observed signed checkpoints.
4. Atomically append the witness record and a receipt to the node's existing
   transaction hash ledger (`node.dag-witness`), then advance independent custody.
5. On restart, replay the committed archive into custody after interrupted host
   writes. Never automatically reset an anchor, replace a key, or recreate lost
   evidence from a remote reply.

Removing a peer from configuration disables its recurring job and makes already
queued jobs inert. Its historical archive and independent anchor are preserved.

### Outcome distinctions

| Outcome | Meaning and action |
|---|---|
| accepted | First pinned checkpoint or proven append-only extension; retain it. |
| unchanged | Same checkpoint; do not manufacture repeated identical receipts. |
| unavailable / no-checkpoint | Network/service failure or no signed history yet; retry normally. |
| stale | Older signed reply; retain the newer anchor. This is not proof of malice. |
| invalid-checkpoint / invalid-proof | Reject the input; keep existing trust. A bad proof alone is not proof of a signed fork. |
| equivocation | Two valid signatures, same Operator/network/size, different Merkle roots; preserve both and quarantine. |
| checkpoint-conflict | Same size/root but different signed checkpoint metadata; preserve and quarantine separately. |

Quarantine is latched. Automatic polling will not promote further checkpoints
from that peer or erase the conflict. There is deliberately no unauthenticated
HTTP reset API. Resolution requires reviewed recovery/policy work, not deleting
the old records. Repeated identical outages are coalesced; recovery is recorded.
Local wall-clock observation times are informational, not consensus timestamps.

## Storage and independent anchors

Migration `20261117_0020` adds:

- `operatorDagWitnessRecords`: immutable hash-linked observation records,
  signed public peer checkpoints, prefix proofs, and common-ledger receipt refs;
- `operatorDagWitnessHeads`: the verified current checkpoint, liveness projection,
  and quarantine flag for each `(observerHash, peerHash)`.

PostgreSQL guards prohibit ordinary UPDATE/DELETE/TRUNCATE on witness records.
Every record is also committed into `syn_communication_messages` in the same
transaction, using the existing deterministic node-ledger workspace. Archive
replay verifies signatures, prefix proofs, record links, head correspondence,
and each corresponding ledger receipt. A ledger write failure rolls back both
record and head. Full node-ledger chain replay remains the existing verifier's
responsibility.

Windows host custody holds each peer's latest signed checkpoint in a separate
DPAPI-protected slot. The broker independently checks the pinned identity,
network, monotonic size and prefix proof. Generic credential APIs cannot modify
these slots. Peer checkpoint archives contain public evidence only: collecting
them grants no signing authority and requires no new root-key approval dialog.
Explicit local peer configuration supplies the bootstrap trust choice. Native
Linux/macOS use the existing OS keystore with a separate per-witness native lock.
The database and independent custody must both be retained across upgrades.

DPAPI/OS keyrings are software-backed custody, not monotonic TPM counters. A
compromise or rollback of *both* witness storage and its custody can defeat local
rollback detection. Each independently operated peer must keep its own state;
multiple processes on the same compromised host are not independent witnesses.
The anchor commits to accepted signed checkpoints, not every negative observation
or receipt in the archive. Whole-system rollback resistance still needs remote
retention and later signed witness receipts/checkpoints over that complete log.

## Inspection API

Existing own-DAG proof routes remain compatible. New read-only routes:

- `GET /v1/operator/dag/witnesses`
- `GET /v1/operator/dag/witnesses/{peer_hash}/checkpoint`
- `GET /v1/operator/dag/witnesses/{peer_hash}/conflict`

They return public pins, status, signed checkpoints and conflicting signed
roots, **not peer URLs, credentials, journal bodies, raw device IDs or keys**.
They inherit the server's existing readiness fence. The summary explicitly says
`consensus: false`. These routes are not installed in `--api-only` mode.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/operator/dag/witnesses | ConvertTo-Json -Depth 8
```

## Deployment and qualification

Stop the current launcher **and its Windows custody broker** before applying;
update host and WSL runtime together. Back up PostgreSQL and preserve the existing
Windows DPAPI vault/OS keyring. Apply the incremental patch, then restart
`Start-Syn.cmd`. Normal auto-migration installs revision 0020; deployments with
`autoMigrate = false` must run their existing migration procedure before startup.
No new Python package dependency or identity is needed.

The included tests use real Ed25519 verification, actual loopback HTTP and real
SQLAlchemy transactions/common receipt hashing on SQLite. Separate PostgreSQL
acceptance tests use the existing `database` fixture and must run against the
CI disposable PostgreSQL database. Linux/macOS keyring, Windows DPAPI/named-pipe,
full installed-package CI, and real deployment testing remain required.

Known bounds: existing proof support is 16,384 leaves; replay is bounded to
100,000 witness records per peer and currently uses full replay rather than an
incremental index. Sources beyond those bounds fail closed. This milestone does
not replicate private DAG event bodies, perform multi-hop checkpoint gossip,
issue signed witness votes, or implement Byzantine agreement.

## Next major milestone

Authenticated, permission-scoped DAG record replication: retain canonical node
bytes and causal parents, prove each transferred batch against an independently
anchored checkpoint, quarantine unresolved parents, and resume interrupted sync
without changing existing object identities. Then add signed witness receipts
and cross-witness gossip to expose split views beyond direct peer polling.

## Primary technical references

- RFC 9162, sections 2.1.3–2.1.4: inclusion and append-only consistency proofs.
  https://www.rfc-editor.org/rfc/rfc9162.html
- PostgreSQL explicit locking: session advisory locks survive transactions and
  must be explicitly released or closed.
  https://www.postgresql.org/docs/current/explicit-locking.html
- Microsoft CryptProtectData: user-scoped DPAPI protection and its limitations.
  https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata
