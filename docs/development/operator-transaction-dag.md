# Operator transaction DAG — first implementation

This milestone mirrors **keystore-verified** Operator authority reservations and
Agent adoption journal events to PostgreSQL as content-addressed DAG nodes.
It does not replace `authority.json`, `adoptions.json`, or their OS-keystore
anchors; the original journals remain the source of truth.

## Data model and provenance

`operatorDagNodes` stores one canonical JSON record per immutable journal event,
with `operator`, `network_id`, `source`, zero-based `source_index`, full original
journal event and `parents` (previous DAG tip plus same-offer causal head where
different). The leaf identifier is SHA-256(`syn.operator.dag.node/1\0` +
canonical record). Source ordering is exact within each journal; events across
different journals are ingested in deterministic order **without claiming their
historical wall-clock ordering**. Already-imported journal records are verified
against the newly read, keystore-anchored source before appending.

`operatorDagCheckpoints` stores append-only signed roots and sequences. Each checkpoint links the previous checkpoint digest, and its root uses
RFC-6962-style SHA-256 (`0x00` + leaf digest / `0x01` + two subtree hashes).
The Operator signs the canonical checkpoint with a dedicated
`syn.operator.dag.checkpoint/1\0` domain; Windows host custody explicitly
requires approval for this signing operation. The signature is verified before
DB commit. An unchanged graph is not re-signed.

## Runtime

At startup, Syn starts the native Operator, verifies and synchronizes its DAG,
and then starts network services. Its existing ProcessScheduler owns a periodic
`operator-dag-sync` job. A missing old enrollment journal may be skipped only
when it has **no** retained keystore anchor. A mismatch stops startup or fails
the scheduled job; Syn does not overwrite old receipts or silently recover.

`--api-only` has no Operator DAG signer, so it does not create/check Operator
DAG nodes. Alembic revision `20261117_0019` installs both tables and PostgreSQL
triggers preventing ordinary update/delete/truncate of DAG leaf rows and signed checkpoints.

## Verification and limitations

Replay checks every node digest, contiguous sequence, source index and content,
causal parents, aggregate root, operator public-key hash, and signed checkpoint.
It verifies the current private source journals, too.

**Not yet:** independent host-retained checkpoint anchors, cross-host
replication, signed historical event timestamps, causal parent edges for all
non-adoption event types, RFC-9162 inclusion/consistency proof APIs, consensus,
or recovery from malicious database-owner rollback. The live WSL/Windows named
pipe and PostgreSQL trigger paths require native integration testing. SQLite
unit tests do not qualify those platform properties.

Signed checkpoints are *current attestations*: backfilled journal events were
not individually signed at their original event time. Do not advertise them
as a fully distributed, independently anchored BFT transaction DAG.

## Milestone 2: public Merkle proofs

See [operator-dag-proofs.md](operator-dag-proofs.md) for signed checkpoint, inclusion and consistency proof APIs and the independent witness verifier.
