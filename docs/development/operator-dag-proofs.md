# Operator DAG proof APIs and independent witnesses (milestone 2)

The existing source of truth is the private, OS-keystore-anchored Operator
journal. `operatorDagNodes` and the signed `operatorDagCheckpoints` are a
PostgreSQL projection. This milestone adds **public-only proof export** and an
**offline verifier**. It does not change or rewrite those source journals.

## Public endpoints

When `syn-server` runs with a configured Operator, these read-only routes are
installed:

- `GET /v1/operator/dag/checkpoint` — latest signed checkpoint;
- `GET /v1/operator/dag/inclusion/{index}` — zero-based node digest inclusion;
- `GET /v1/operator/dag/consistency/{old_size}` — consistency proof from a
  prior **signed** checkpoint of that exact sequence to the current checkpoint.

Only the signed public-key commitment, sequence, Merkle roots, leaf digests and
hash paths are returned. Neither the original journal event nor hardware ID,
private group key, or secrets are returned. No proof endpoints exist in
`--api-only` mode. A missing or inconsistent projection returns 409.

Proofs are SHA-256 over a domain-separated Operator leaf (`NODE_DOMAIN`), then
RFC 6962 / RFC 9162-style Merkle tree prefixes: `0x00` for leaves, `0x01` for
interior nodes. The resulting 32-byte checkpoint root is signed under
`syn.operator.dag.checkpoint/1\0` with the existing Operator key.

The first signed checkpoint is not an independently trusted anchor merely
because its signature verifies. Obtain the expected Operator public-key hash
through a trusted channel and retain checkpoint bytes **outside the database**.
A witness should compare a later consistency proof with a *previously saved*
checkpoint, preventing substitution of an unrelated but correctly signed
checkpoint. Database admins who can also replace the off-host witness are not
in scope for this milestone; independent host DPAPI anchoring and multi-witness
publication remain further work.

## Offline verification

Save the API JSON responses as files and verify them on an independent system
with the Syn Python package installed:

```sh
python -m syn.operator.dag_witness checkpoint checkpoint.json --operator <trusted-64-char-public-key-hash>
python -m syn.operator.dag_witness inclusion inclusion.json --operator <trusted-64-char-public-key-hash>
python -m syn.operator.dag_witness consistency consistency.json --operator <trusted-64-char-public-key-hash> --trusted-checkpoint previous.json
```

`--trusted-checkpoint` must contain the signed prior checkpoint that the
witness already retained, not a checkpoint freshly retrieved from the same
untrusted server. The verifier prints `{"valid":true}` and exits 0 on success,
or returns nonzero on failure. Evidence is limited to at most 16,384 leaves per
request; larger ledgers require the next streaming/subtree-cache milestone.

## Non-goals

No P2P dissemination, consensus, BFT finality, globally ordered transactions,
historical attestation at an event's original timestamp, or new operator key
provisioning is claimed here. Signature and prefix proof validation do **not**
prove that a checkpoint is globally unique: witnesses must compare anchors.
