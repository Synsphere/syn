"""Actual Ed25519 consent, old-authority enforcement, append-only local governance."""

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from syn_client.consensus import (
    CanonicalTypeDefinition,
    CanonicalTypeRelease,
    ConsensusPolicy,
    NodeKeyHash,
    PolicyBinding,
    PolicyError,
    canonical_bytes,
    load_document,
)
from syn_client.governance import (
    ApprovalAuthority,
    GovernedPolicyRecord,
    GovernedPolicyState,
    verified_chain,
    verify_transition,
)

from syn.governance import GovernedPolicyStore
from syn.policy_adoption import (
    PolicyAdoptionStore,
    PolicyConflict,
    domains,
    metadata,
    signedRecords,
)
from test_policy_adoption import adoption, initialize

NOW = 1_792_800_000
WORKSPACE = "44444444-4444-4444-8444-444444444444"
CATALOG = "11111111-1111-4111-8111-111111111111"
DOMAIN = "22222222-2222-4222-8222-222222222222"
STREAM = "33333333-3333-4333-8333-333333333333"


def private(number):
    # Public deterministic synthetic fixture material, never real account keys.
    return Ed25519PrivateKey.from_private_bytes(bytes([number]) * 32)


def identity(key):
    raw = key.public_key().public_bytes_raw()
    return dict(
        type_ref="synny://type/public-identity-descriptor",
        schema_version=2,
        profile_id="potbot-identity-v1",
        hash_algorithm="SHA-256",
        signer_hash=str(NodeKeyHash.from_public_key(raw)),
        public_key_algorithm="Ed25519",
        public_key_encoding="base64",
        public_key=base64.b64encode(raw).decode(),
    )


def signed(key, prefix, body):
    return {
        **body,
        "signature": base64.b64encode(key.sign(prefix + b"\0" + canonical_bytes(body))).decode(),
    }


def authority(numbers=(1, 2, 3), threshold=2, issuer=200):
    memberships = [
        signed(
            private(issuer),
            b"syn-agent-membership-v1",
            dict(
                schema_version=1,
                profile="syn-agent-account-v1",
                operator=identity(private(issuer)),
                agent=identity(private(n)),
                account_number=n,
                instance_id=f"00000000-0000-4000-8000-{n:012}",
                group_id=DOMAIN,
                group_epoch=0,
            ),
        )
        for n in numbers
    ]
    memberships.sort(key=lambda m: m["agent"]["signer_hash"])
    return ApprovalAuthority(threshold, tuple(canonical_bytes(m).decode() for m in memberships))


def proofs(authority, payload, numbers=None, issued=NOW):
    result = []
    for membership in authority.memberships():
        n = membership["account_number"]
        if numbers is not None and n not in numbers:
            continue
        result.append(
            signed(
                private(n),
                b"syn-agent-proof-v1",
                dict(
                    schema_version=1,
                    profile="syn-agent-proof-v1",
                    membership=membership,
                    request_id=f"00000000-0000-4000-8000-{n:012}",
                    payload_sha256=payload,
                    issued_at=issued,
                    expires_at=issued + 300,
                ),
            )
        )
    return result


def approve(record, previous=None, type_authority=None, policy_authorities=None):
    state = previous.state if previous else record.state
    a = type_authority or state.type_governance
    by_digest = {x.digest: x for x in (policy_authorities or state.policy_authorities)}
    policies = (
        {
            t.consensus_policy.digest: t.consensus_policy
            for t in state.release.types
            if t.consensus_policy
        }
        if previous
        else {}
    )
    sets = {
        d: proofs(
            by_digest[p.membership_anchor_digest],
            record.approval_payload(by_digest[p.membership_anchor_digest], d),
        )
        for d, p in policies.items()
    }
    return replace(
        record,
        approvals_json=canonical_bytes(
            dict(type_governance=proofs(a, record.approval_payload(a)), existing_policies=sets)
        ).decode(),
    )


def genesis(workspace=WORKSPACE):
    roster, governance = authority(), authority((4, 5, 6))
    p = ConsensusPolicy(
        "syn.consensus-policy/1",
        "1",
        "syn.operator-log/1",
        "same-verified-issuer",
        roster.digest,
        "node-public-key-sha256",
        "strict-majority",
        "crash-and-partition",
        "syn.operator-commit/1",
        "tasks",
        "current-policy-and-type-governance",
    )
    t = CanonicalTypeDefinition("syn://type/task", "1", '{"name":"Task"}', p)
    state = GovernedPolicyState(
        workspace,
        CanonicalTypeRelease(CATALOG, "1", (t,)),
        (PolicyBinding(p.digest, DOMAIN, "b" * 64, STREAM, "1", roster.digest),),
        governance,
        (roster,),
    )
    return approve(
        GovernedPolicyRecord("1", "0" * 64, state, '{"existing_policies":{},"type_governance":[]}')
    )


def successor(old, *, rotate=False, name="Changed"):
    t = old.state.release.types[0]
    roster = authority((7, 8, 9)) if rotate else old.state.policy_authorities[0]
    p = replace(
        t.consensus_policy,
        policy_revision=str(int(t.consensus_policy.policy_revision) + 1),
        membership_anchor_digest=roster.digest,
    )
    new = replace(
        old.state,
        release=replace(
            old.state.release,
            release_revision=str(int(old.state.release.release_revision) + 1),
            types=(
                replace(
                    t,
                    type_revision=str(int(t.type_revision) + 1),
                    definition_json=canonical_bytes({"name": name}).decode(),
                    consensus_policy=p,
                ),
            ),
        ),
        bindings=(
            replace(
                old.state.bindings[0],
                policy_digest=p.digest,
                policy_epoch=str(int(old.state.bindings[0].policy_epoch) + 1),
                roster_digest=roster.digest,
            ),
        ),
        policy_authorities=(roster,),
        type_governance=authority((10, 11, 12)) if rotate else old.state.type_governance,
    )
    record = GovernedPolicyRecord(
        str(int(old.sequence) + 1), old.digest, new, '{"existing_policies":{},"type_governance":[]}'
    )
    return approve(record, old)


@pytest.fixture
def rig():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata.create_all(engine)
    store = GovernedPolicyStore(sessionmaker(engine))
    yield store, engine
    engine.dispose()


def admit(store, record, root, now=NOW):
    return store.admit(
        record,
        expected_workspace=record.state.workspace_id,
        expected_catalog=CATALOG,
        expected_genesis_digest=root.digest,
        expected_predecessor_digest=record.previous_record_digest,
        now=now,
    )


def test_signed_genesis_then_rotation_requires_both_old_authorities(rig):
    root = genesis()
    next_record = successor(root, rotate=True)
    store, engine = rig
    assert admit(store, root, root) == root
    assert admit(store, next_record, root) == next_record
    chain = store.chain(WORKSPACE, CATALOG, root.digest, next_record.digest)
    resolver = verified_chain(
        chain,
        expected_workspace=WORKSPACE,
        expected_catalog=CATALOG,
        expected_genesis_digest=root.digest,
        expected_head_digest=next_record.digest,
    )
    assert resolver.release == next_record.state.release
    assert admit(store, next_record, root, now=NOW + 10000) == next_record
    with engine.connect() as c:
        assert len(c.execute(select(signedRecords)).all()) == 2


@pytest.mark.parametrize(
    "attack",
    [
        "new-governors",
        "new-roster",
        "missing-policy",
        "missing-governance",
        "duplicate-vote",
        "one-vote",
        "wrong-role",
        "alter-proposal",
        "expired",
    ],
)
def test_replacements_cannot_self_ratify_or_reuse_approval(rig, attack):
    store, _ = rig
    root = genesis()
    candidate = successor(root, rotate=True)
    admit(store, root, root)
    approval = load_document(candidate.approvals_json)
    policy_digest = root.state.release.types[0].consensus_policy.digest
    if attack == "new-governors":
        approval["type_governance"] = proofs(
            candidate.state.type_governance,
            candidate.approval_payload(candidate.state.type_governance),
        )
    elif attack == "new-roster":
        a = candidate.state.policy_authorities[0]
        approval["existing_policies"][policy_digest] = proofs(
            a, candidate.approval_payload(a, policy_digest)
        )
    elif attack == "missing-policy":
        approval["existing_policies"] = {}
    elif attack == "missing-governance":
        approval["type_governance"] = []
    elif attack == "duplicate-vote":
        approval["type_governance"] = [approval["type_governance"][0]] * 2
    elif attack == "one-vote":
        approval["existing_policies"][policy_digest] = approval["existing_policies"][policy_digest][
            :1
        ]
    elif attack == "wrong-role":
        approval["type_governance"] = proofs(
            root.state.type_governance,
            candidate.approval_payload(root.state.type_governance, policy_digest),
        )
    elif attack == "alter-proposal":
        candidate = replace(
            candidate, state=replace(candidate.state, type_governance=root.state.type_governance)
        )
    candidate = replace(candidate, approvals_json=canonical_bytes(approval).decode())
    with pytest.raises(PolicyError):
        admit(store, candidate, root, now=NOW + 300 if attack == "expired" else NOW)
    assert store.chain(WORKSPACE, CATALOG, root.digest, root.digest) == (root,)


@pytest.mark.parametrize(
    "attack",
    [
        "parent",
        "sequence",
        "workspace",
        "catalog",
        "drop-type",
        "drop-policy",
        "namespace",
        "genesis",
        "stream",
        "epoch",
        "type-revision",
        "policy-revision",
        "release-revision",
    ],
)
def test_signed_but_structurally_invalid_successors_hold(attack):
    root = genesis()
    candidate = successor(root)
    state = candidate.state
    t = state.release.types[0]
    if attack in {"parent", "sequence"}:
        candidate = replace(
            candidate,
            **({"previous_record_digest": "f" * 64} if attack == "parent" else {"sequence": "3"}),
        )
    elif attack in {"workspace", "catalog"}:
        state = (
            replace(state, workspace_id=CATALOG)
            if attack == "workspace"
            else replace(state, release=replace(state.release, catalog_id=WORKSPACE))
        )
    elif attack == "drop-type":
        state = replace(
            state, release=replace(state.release, types=(replace(t, type_ref="syn://type/other"),))
        )
    elif attack in {"drop-policy", "namespace", "policy-revision"}:
        if attack == "drop-policy":
            t2 = replace(t, consensus_policy=None)
            # Keep another bound Type so the state itself remains well-formed.
            state = replace(
                state,
                release=replace(state.release, types=(replace(t, type_ref="syn://type/other"), t2)),
            )
        else:
            p = replace(
                t.consensus_policy,
                **(
                    {"conflict_namespace": "escape"}
                    if attack == "namespace"
                    else {"policy_revision": "9"}
                ),
            )
            state = replace(
                state,
                release=replace(state.release, types=(replace(t, consensus_policy=p),)),
                bindings=(replace(state.bindings[0], policy_digest=p.digest),),
            )
    elif attack in {"genesis", "stream", "epoch"}:
        options = (
            {"genesis_digest": "f" * 64}
            if attack == "genesis"
            else {"stream_id": WORKSPACE}
            if attack == "stream"
            else {"policy_epoch": "4"}
        )
        state = replace(state, bindings=(replace(state.bindings[0], **options),))
    elif attack == "type-revision":
        state = replace(
            state, release=replace(state.release, types=(replace(t, type_revision="4"),))
        )
    else:
        state = replace(state, release=replace(state.release, release_revision="4"))
    candidate = approve(replace(candidate, state=state), root)
    with pytest.raises(PolicyError):
        verify_transition(root, candidate, now=NOW)


def test_chain_replay_fork_and_unpinned_head_fail(rig):
    store, _ = rig
    root = genesis()
    one, fork = successor(root), successor(root, name="Fork")
    admit(store, root, root)
    admit(store, one, root)
    with pytest.raises(PolicyConflict):
        admit(store, fork, root)
    for chain, head in (
        ((root,), one.digest),
        ((root, fork, one), one.digest),
        ((one,), one.digest),
    ):
        with pytest.raises(PolicyError):
            verified_chain(
                chain,
                expected_workspace=WORKSPACE,
                expected_catalog=CATALOG,
                expected_genesis_digest=root.digest,
                expected_head_digest=head,
            )


def test_legacy_unsigned_and_signed_owners_cannot_bypass_each_other(rig):
    store, engine = rig
    root = genesis()
    legacy = adoption()
    old = PolicyAdoptionStore(sessionmaker(engine))
    initialize(old, legacy)
    with pytest.raises(PolicyConflict):
        admit(store, root, root)
    # Another catalog also cannot take the legacy namespace.
    state = replace(root.state, release=replace(root.state.release, catalog_id=WORKSPACE))
    other = approve(replace(root, state=state))
    with pytest.raises(PolicyConflict):
        store.admit(
            other,
            expected_workspace=WORKSPACE,
            expected_catalog=WORKSPACE,
            expected_genesis_digest=other.digest,
            expected_predecessor_digest="0" * 64,
            now=NOW,
        )
    with engine.connect() as c:
        assert not c.execute(select(signedRecords)).all()


def test_privileged_sql_tampering_is_detected(rig):
    store, engine = rig
    root = genesis()
    admit(store, root, root)
    with engine.begin() as c:
        c.execute(domains.update().values(genesis_digest="f" * 64))
    with pytest.raises(PolicyError):
        store.chain(WORKSPACE, CATALOG, root.digest, root.digest)


def test_federated_successor_cannot_replace_finality_with_signature_count(rig):
    store, _ = rig
    root = genesis()
    candidate = successor(root)
    member_json = tuple(
        sorted(
            (authority((n,), 1, issuer=200 + n).memberships_json[0] for n in (7, 8, 9)),
            key=lambda raw: load_document(raw)["agent"]["signer_hash"],
        )
    )
    roster = ApprovalAuthority(3, member_json)
    t = candidate.state.release.types[0]
    p = replace(
        t.consensus_policy,
        protocol="syn.bft-dag-hashgraph/1",
        custody_constraint="independent-issuer-weight-caps",
        membership_anchor_digest=roster.digest,
        quorum_rule="bft-supermajority",
        fault_model="byzantine-less-than-one-third-weight",
        proof_profile="syn.dag-hashgraph-finality/1",
    )
    state = replace(
        candidate.state,
        release=replace(candidate.state.release, types=(replace(t, consensus_policy=p),)),
        bindings=(
            replace(
                candidate.state.bindings[0], policy_digest=p.digest, roster_digest=roster.digest
            ),
        ),
        policy_authorities=(roster,),
    )
    bft = approve(replace(candidate, state=state), root)
    admit(store, root, root)
    admit(store, bft, root)
    next_record = successor(bft)
    with pytest.raises(PolicyError, match="DAG finality"):
        admit(store, next_record, root)


def test_signed_namespace_cannot_be_taken_by_unsigned_catalog(rig):
    store, engine = rig
    root = genesis()
    admit(store, root, root)
    legacy = adoption()
    other = replace(legacy, release=replace(legacy.release, catalog_id=WORKSPACE))
    with pytest.raises(PolicyConflict, match="namespace"):
        initialize(PolicyAdoptionStore(sessionmaker(engine)), other)


def test_authenticated_chain_read_and_actual_shared_parser(rig):
    from io import BytesIO
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from syn_client import SynClient, IntegrityError

    from syn.communication import communicationRouter

    store, _ = rig
    root = genesis()
    next_record = successor(root)
    admit(store, root, root)
    admit(store, next_record, root)
    token = "synthetic-" + "x" * 40
    app = FastAPI()
    app.include_router(communicationRouter(store.sessions, {WORKSPACE: token}))
    url = f"/v1/communication/workspaces/{WORKSPACE}/policies/{CATALOG}/governance/{root.digest}/chains/{next_record.digest}"
    with TestClient(app) as http:
        with patch.object(
            GovernedPolicyStore, "chain", side_effect=AssertionError("authorize first")
        ):
            assert http.get(url).status_code == 403
        http.headers["Authorization"] = "Bearer " + token
        assert http.post(url, json={}).status_code == 405
        assert http.get(url + "?latest=true").status_code == 422
        assert http.get(url.replace(next_record.digest, "f" * 64)).status_code == 404
        response = http.get(url)
        assert response.status_code == 200
        client = SynClient("http://127.0.0.1", token)
        with patch.object(client._opener, "open", return_value=BytesIO(response.content)):
            assert client.governed_policy_chain(
                WORKSPACE,
                catalog_id=CATALOG,
                expected_genesis_digest=root.digest,
                expected_head_digest=next_record.digest,
            ) == (root, next_record)
        incomplete = {"profile": "syn.governance-chain/1", "records": [root.to_dict()]}
        with patch.object(
            client._opener, "open", return_value=BytesIO(canonical_bytes(incomplete))
        ):
            with pytest.raises(IntegrityError):
                client.governed_policy_chain(
                    WORKSPACE,
                    catalog_id=CATALOG,
                    expected_genesis_digest=root.digest,
                    expected_head_digest=next_record.digest,
                )


def write_fixture(path):
    root = genesis()
    next_record = successor(root, rotate=True)
    Path(path).write_text(
        json.dumps(
            {
                "genesis_digest": root.digest,
                "head_digest": next_record.digest,
                "records": [root.to_dict(), next_record.to_dict()],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("phase", ["after-lock", "before-commit"])
def test_live_clock_rechecked_inside_lock_and_before_commit(rig, phase):
    from unittest.mock import patch

    store, engine = rig
    root = genesis()
    times = [NOW + 300] if phase == "after-lock" else [NOW, NOW + 300]
    original_lock = store._lock
    acquired = False

    def tracked_lock(session, workspace):
        nonlocal acquired
        original_lock(session, workspace)
        acquired = True

    def clock():
        assert acquired, "Admission time must not be sampled before the lock"
        return times.pop(0)

    with (
        patch.object(store, "_lock", side_effect=tracked_lock),
        patch("syn.governance.time.time", side_effect=clock),
    ):
        with pytest.raises(PolicyError, match="admission window"):
            store.admit(
                root,
                expected_workspace=WORKSPACE,
                expected_catalog=CATALOG,
                expected_genesis_digest=root.digest,
                expected_predecessor_digest="0" * 64,
            )
    with engine.connect() as c:
        assert not c.execute(select(signedRecords)).all()


def test_governance_import_command_commits_synthetic_disk_database(tmp_path, capsys):
    from unittest.mock import patch

    from syn.governance_import import main

    database_url = "sqlite:///" + (tmp_path / "governance.sqlite").as_posix()
    engine = create_engine(database_url)
    metadata.create_all(engine)
    config = tmp_path / "explicit.toml"
    config.write_text('[database]\nurl="' + database_url + '"\n', encoding="utf-8")
    root = genesis()
    next_record = successor(root)
    try:
        with patch("syn.governance.time.time", return_value=NOW):
            for record in (root, next_record):
                manifest = tmp_path / (record.sequence + ".json")
                manifest.write_text(record.to_json(), encoding="utf-8")
                assert (
                    main(
                        [
                            "--config",
                            str(config),
                            "--record",
                            str(manifest),
                            "--workspace",
                            WORKSPACE,
                            "--catalog",
                            CATALOG,
                            "--genesis-digest",
                            root.digest,
                            "--predecessor-digest",
                            record.previous_record_digest,
                        ]
                    )
                    == 0
                )
        assert GovernedPolicyStore(sessionmaker(engine)).chain(
            WORKSPACE, CATALOG, root.digest, next_record.digest
        ) == (root, next_record)
        assert next_record.digest in capsys.readouterr().out
    finally:
        engine.dispose()
