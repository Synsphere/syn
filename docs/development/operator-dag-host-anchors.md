# Windows-host Operator DAG checkpoint anchors — milestone 3

This milestone closes the *PostgreSQL-only* rollback gap for Syn Operators that
run with `windows-dpapi-host` custody. Existing authority/adoption journals
remain authoritative, and the DAG still retains its signed checkpoint chain.
The host stores only a **public** Ed25519-signed checkpoint, not journal events
or root entropy. The latest anchor is encrypted alongside the rest of the
Windows host vault by the logged-in Windows user's DPAPI protection.

## Trust and write ordering

1. The WSL DAG synchronizer reads the host anchor through `dag_anchor/read`.
   The Windows host independently verifies its retained signature and Operator
   identity. WSL verifies the same packet against its pinned identity/network.
2. Under the existing PostgreSQL advisory lock, Syn validates every source
   journal node, Merkle root, checkpoint signature and checkpoint chain.
   If a Windows anchor exists, the PostgreSQL chain **must contain the exact
   retained checkpoint** at its original sequence. A missing, modified,
   shorter or forked database fails startup before any new DAG records.
3. The DAG transaction commits leaves and signed checkpoints to PostgreSQL.
   Syn then advances the host anchor, in checkpoint order. The host refuses
   rewind, fork, wrong identity, wrong network, invalid Ed25519 signature and
   missing signed predecessor. Only the **first** anchor requires explicit
   Windows-side approval; later valid chain advances do not.
4. If Windows custody fails after PostgreSQL commits, Syn surfaces the error
   through its existing readiness fence. On restart the unchanged signed
   database checkpoint chain is replayed into the vault, without re-signing.

The Windows host broker owns a single DPAPI vault behind an exclusive file
lock and SID-restricted named pipe. Generic keystore `get` and `set` operations
cannot access the DAG anchor slot.

## Limitations and recovery

* A Windows user/malware with access to the vault may still defeat this
  software-backed host. DPAPI is **not** a TPM monotonic counter, HSM or a
  third-party witness. There is no claim of distributed consensus or finality.
* The first anchor is a **trust-on-first-use** commitment to a *validly signed*
  checkpoint. For higher assurance, compare the initial root with an
  independently saved checkpoint before approving its adoption.
* The host vault retains its latest checkpoint only. Preserve older public
  checkpoints and consistency proofs elsewhere if historical witness evidence
  is required.
* Moving the vault to a new Windows user or computer requires an explicit,
  independently verified identity/state recovery; never delete or reset the
  anchor to silence a mismatch.
* Native Linux/macOS deployment still uses the existing local custody and
  does not gain an independent checkpoint anchor through this change.
* Changes to PostgreSQL cannot roll back an already retained Windows anchor,
  **provided the Windows vault itself is not replaced or rolled back**.

No Alembic migration is required. The new broker operation and the WSL DAG
synchronizer must be updated together. It remains necessary to test native
Windows named-pipe/DPAPI transport and PostgreSQL trigger semantics in CI.
