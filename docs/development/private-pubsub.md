# Private Syn pub-sub and Cog admission

**Published and native-mesh qualified Syn 0.4.0, 2 November 2026.** Syn 0.4.0 adds an authenticated,
encrypted, volatile local transport between independently enrolled Agent nodes.
The source-wheel native WSL fixture passed two-node delivery, signed decrypted
reception and relay-loss fencing. The [release record](releases/v0.4.0.md)
retains verified publication and successful Python 3.12/3.13 official-package acceptance;
the existing 0.3.1 release remains unchanged.

![Architecture illustration: task authorization and native Syn transport are separate gates](media/syn-private-pubsub.svg)

*Generated architecture illustration, not evidence of a running node or Cog.*

## Ownership and trust

The official Syn runtime owns native credentials, group encryption, source
signatures, membership, replay checks and ciphertext routing. Potbot invokes the
installed public SDK and canonical runtime commands. It never receives a group
key, imports Syn runtime internals or substitutes an in-process heartbeat relay
for real nodes. The privileged local controller and its OS are trusted; this
profile does not protect against a compromised host administrator.

One Agent owns one long-lived Syn node. Each Cog invocation has a separate
logical heartbeat monitor associated with that Agent, not a newly fabricated
Syn identity. A native Syn UID and a Cog guest UID may differ because they are
separate restricted processes bound to the same logical Agent by the operator.

```mermaid
sequenceDiagram
    participant P as Potbot controller
    participant A as Agent A Syn node
    participant R as Canonical Syn relay
    participant B as Agent B Syn node
    P->>A: Fresh selected identity and native process check
    P->>B: Fresh selected identity and native process check
    R->>A: Frozen workspace/session/group roster
    R->>B: Frozen workspace/session/group roster
    A-->>R: Signed node and boot hello
    B-->>R: Signed node and boot hello
    R-->>A: Complete signed hello roster
    R-->>B: Complete signed hello roster
    P->>A: Publish typed heartbeat through public SDK
    A->>R: Signed ciphertext from native group custody
    R->>B: Same signed ciphertext
    R-->>A: Relay acknowledgement
    B-->>P: Fresh signed reception with verified plaintext
    Note over P,B: Neither acknowledgement nor reception grants Cog authority
```

## First supported profile

The additive runtime profile is `syn.private-pubsub/1`, capability
`authenticated-private-pubsub`. Qualification targets Linux x86-64, including
managed WSL2. Existing native Agent sandbox, custody and deployment requirements
remain mandatory. macOS Agent isolation and remote IP transport are unqualified.

The controller starts the official `python -I -B -m syn.mesh --config FILE`.
Its root-private configuration has profile `syn.private-relay-config/1`, a
`scope`, and exact `endpoints` containing node hash, private socket and owner UID.
The relay opens no IP listener. It connects to each existing native Agent socket,
checks ownership and peer credentials, and relays public certificates and signed
ciphertext. No signing or group secret is held by the relay implementation.

The frozen `syn.private-pubsub-scope/1` records workspace UUID, random session ID,
operator hash, network ID, group UUID, current group epoch and signed member
certificates. Every certificate must have the same current group commitment.
Signed hellos bind each selected leaf to its live boot and this exact session.
An Agent cannot be silently rebound to another mesh while running.

The public `syn_client.pubsub_client` module exposes `request(...)` and a CLI:

```sh
python -I -B -m syn_client.pubsub_client \
  --socket /private/agent.sock --owner-uid 53102 \
  --expected-key SELECTED_NODE_HASH --configuration-sha256 CONFIG_HASH \
  --expected-boot BOOT_ID --request /private/public-request.json
```

The request file contains exactly `scope`, `operation` and `payload`.
`status` and `receive` require a null payload; `publish` requires
`{"topic":"synny://type/potbot-worker-heartbeat-v1","value":{...}}`.
Topic names are exact bounded Syn type references. The SDK creates a fresh
challenge, checks the private endpoint and verifies the selected node's signed
reply, request digest, scope and boot. It needs the client's governance extra
for cryptographic verification. It neither exports keys nor provides a general
signing RPC. Root-only controller operations are distinct from Agent readiness.

## Encryption and evidence

Each sender derives an AES-256-GCM key from its current native group key using
HKDF-SHA256. The derivation binds network, group/epoch, workspace, random session,
selected sender and boot. A monotonic per-sender sequence provides the nonce;
authenticated metadata and the member certificate bind the ciphertext. The
selected leaf signs the complete envelope. Recipients independently check the
membership, signature, boot and increasing sequence before decrypting, then
check the sender's plaintext commitment. There is no new identity derivation or
private-key export contract.

| Evidence | What it establishes |
| --- | --- |
| Signed `status` | Current native custody and complete connected roster; always `cog_execution_authorized=false` |
| Signed `publish` | A particular ciphertext was accepted by the relay; `peer_reception_proved=false` |
| Signed `receive` | The selected recipient reports verified plaintext bound to a signed source envelope |
| Potbot task grant | Separate controller authorization for one exact Cog invocation after existing task/policy checks |
| Host process/audit receipt | Separate evidence of actual effects and confirmed process-tree termination |

The initial limits are eight native peers, 8 KiB canonical JSON per publication,
64 queued messages per recipient and two messages per receive call. Messages
are fresh within a bounded clock window. Consumers must drain queues even when
their Agents have no active Cog. The transport is volatile: no durable delivery,
exactly-once execution, transaction ledger, Type consensus or cross-host claim
follows from it. PostgreSQL and existing execution journals remain authoritative.

Lost relay or native custody, malformed or replayed traffic, queue exhaustion
and uncertain publication fence the session. Native health then refuses work,
the Agent exits and removes its endpoint. Potbot propagates the fault to Cog
supervision and treats possibly dispatched effects as requiring reconciliation.
General restart/recovery and membership rotation remain SYN-R06 work.

## Qualification and media

`tests/test_pubsub.py` uses real signatures/encryption with explicitly synthetic
custody for protocol/fault tests. `scripts/ci_private_pubsub.py` retains the
lower-level two-native-node delivery fixture. The source-wheel lane now runs
`scripts/ci_potbot_private_mesh.py` with actual encrypted stores and real
restricted processes, substituting only the package/assignment registry in an
explicit source-only deployment fixture. It cannot prove official installation.
The official-package lane runs the same complete adapter with the actual
installer/deployment registry, exact reviewed Potbot controller and independently
pinned signed packages; no registry is substituted there.
It exercises the complete public adapter, real heartbeat exchange, one-use task
grant rejection and relay-loss fencing. It explicitly records that no Cog
process is dispatched; qualified Cog images, cgroups, syscall/TASKSTATS evidence,
budgets and host admission retain their own mandatory tests and U16 acceptance.

There is no GUI in this profile. The diagram and sequence explain the behavior;
native signed receipts and CI logs supply evidence. No keystore/prompt footage
is captured. Browser capture is unavailable on the currently permitted external
connection, so no screenshot or demonstration video is claimed.
