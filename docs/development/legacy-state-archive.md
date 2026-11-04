# Archive legacy enrollment records

Use the offline archive when retiring the old Potbot Phase 41/42 projection and
Syn Phase 42/47/48 journals. The archive keeps exact SQLite file bytes, including
the signed receipt envelopes, handoff commitments, encrypted pending LAN rows,
and expired LAN replay tombstones with their original opaque device handles.
Receipt lookups return the original bytes and verify both signatures at the
receipt's recorded adoption time.

```mermaid
flowchart LR
    A[Quiesced legacy SQLite snapshots] --> B[Pin snapshot hashes and operator identity]
    B --> C[Verify receipts and replay commitments]
    C --> D[Seal a read-only archive with a manifest digest]
    D --> E[Read original receipts and replay records]
```

Stop the legacy writers and take standalone rollback-journal SQLite snapshots
under the same operator and network scope. Keep them in a private directory.
If the old database uses WAL, produce a standalone snapshot with SQLite's backup
facility and switch the snapshot to rollback-journal mode before pinning it.
The archive rejects sidecar files, wrong source hashes, invalid receipt
signatures, and conflicting confirmed handoff commitments. Use dedicated
legacy databases. The Potbot input supports the enrollment table and its
optional Phase 42 adoption projection in the same database.

Write an owner-private canonical JSON plan with these fields:

- `profile` is `syn.legacy-state-plan/1`.
- `operator` is the independently known legacy operator base key SHA-256.
- `network` is the legacy network identifier.
- `sources` has four keys, `receipts`, `handoffs`, `lan`, and `potbot`. For each,
  supply `{"path":"/absolute/private/snapshot.sqlite","sha256":"<64 lowercase hex>"}`
  using a digest recorded at the trusted snapshot step. Use `null` only for a
  source accounted for as absent. A confirmed handoff requires its exact
  signed receipt in a supplied source.

```sh
syn-operator legacy-state archive --plan /private/legacy-plan.json \
  --output /private/syn-legacy-archive
```

Use a new destination. The command writes `manifest.json` after validating all
selected files. A refused or interrupted attempt may leave an incomplete
directory without a manifest. Retain the source snapshots and use a new
directory for the next attempt. Save the returned `archive_sha256` separately
alongside the operator pin. The archive relies on independently retained pins
to detect rollback of the files, including unsigned tombstone rows.

```sh
syn-operator legacy-state receipt --archive /private/syn-legacy-archive \
  --expected-operator "$OPERATOR_PIN" --expected-archive-sha256 "$ARCHIVE_PIN" \
  --offer-sha256 "$OFFER_DIGEST"
syn-operator legacy-state replay --archive /private/syn-legacy-archive \
  --expected-operator "$OPERATOR_PIN" --expected-archive-sha256 "$ARCHIVE_PIN" \
  --offer-sha256 "$OFFER_DIGEST"
```

The receipt command is the offline replacement for historical lookup. Replay
lookup reports recorded handoff, LAN, and Potbot commitments by offer digest.
The archive grants no enrollment or activation authority. It does not
translate opaque legacy device handles into the current native identity
scheme. Preserve any separately held legacy LAN key under the existing
custody policy if later authorized access to its ciphertext is needed.
The retired protocol must stay closed at cutover. Current Syn enrollment
continues to enforce its own replay state.

The diagram explains the headless flow. Qualification uses disposable
databases and generated test identities. Private journal or key material
does not appear in screenshots or recordings.
