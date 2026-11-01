"""Installed-wheel signature, native compatibility and pinned-chain verification."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from syn_client.consensus import PolicyError, canonical_bytes, load_document
from syn_client.custody import verify_agent_approval, verify_ed25519, verify_membership
from syn_client.governance import (
    ApprovalAuthority,
    GovernedPolicyRecord,
    verified_chain,
    verify_genesis,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_actual_rust_protected_event_and_operator_reservation_bytes():
    from syn_client.dag_signing import protected_event_digest, verify_protected_event

    event = json.loads((FIXTURES / "protected-native-dag-event.json").read_bytes())
    member = event["proof"]["membership"]
    assert member["account_number"] == 7
    assert verify_protected_event(event, membership=member) == event
    assert protected_event_digest(event) == event["reservation"]["intent"]["event_digest"]
    wrong = json.loads(json.dumps(event))
    wrong["reservation"]["signature"] = "A" * 86 + "=="
    with pytest.raises(PolicyError):
        verify_protected_event(wrong, membership=member)


def test_rfc8032_ed25519_vector_and_corruption():
    key = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
        "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    verify_ed25519(key, signature, b"")
    for message, bad_signature in (
        (b"changed", signature),
        (b"", signature[:-1]),
        (b"", b"\0" * 64),
    ):
        with pytest.raises(PolicyError):
            verify_ed25519(key, bad_signature, message)


def test_actual_native_account_proof_bytes_remain_compatible():
    proof = json.loads((FIXTURES / "native-agent-public-proof.json").read_bytes())
    membership = verify_membership(proof["membership"])
    assert (
        verify_agent_approval(
            proof,
            membership=membership,
            payload_digest=proof["payload_sha256"],
            now=proof["issued_at"],
        )
        == membership["agent"]["signer_hash"]
    )
    with pytest.raises(PolicyError):
        verify_agent_approval(proof, membership=membership, payload_digest="f" * 64, now=None)
    changed = json.loads(json.dumps(membership))
    changed["account_number"] += 1
    with pytest.raises(PolicyError):
        verify_membership(changed)


def test_portable_signed_chain_and_independent_genesis_head_pins():
    data = json.loads((FIXTURES / "governed-chain.json").read_bytes())
    records = tuple(GovernedPolicyRecord.from_dict(r) for r in data["records"])
    assert records[0].digest == data["genesis_digest"]
    assert records[-1].digest == data["head_digest"]
    options = dict(
        expected_workspace=records[0].state.workspace_id,
        expected_catalog=records[0].state.release.catalog_id,
        expected_genesis_digest=data["genesis_digest"],
        expected_head_digest=data["head_digest"],
    )
    assert verified_chain(records, **options).release == records[-1].state.release
    for key in ("expected_genesis_digest", "expected_head_digest"):
        with pytest.raises(PolicyError):
            verified_chain(records, **(options | {key: "f" * 64}))
    with pytest.raises(PolicyError):
        verified_chain(records[1:], **options)
    # A live admission check rejects old proofs; historical verification does not
    # manufacture a trusted acceptance timestamp or prove that this head is latest.
    with pytest.raises(PolicyError):
        verify_genesis(
            records[0],
            expected_workspace=options["expected_workspace"],
            expected_catalog=options["expected_catalog"],
            expected_genesis_digest=data["genesis_digest"],
            now=1_792_801_000,
        )


def test_closed_governance_profiles_and_invalid_native_encodings():
    data = json.loads((FIXTURES / "governed-chain.json").read_bytes())
    root = GovernedPolicyRecord.from_dict(data["records"][0])
    with pytest.raises(PolicyError):
        GovernedPolicyRecord.from_dict(root.to_dict() | {"force": True})
    with pytest.raises(PolicyError):
        ApprovalAuthority.from_dict(root.state.type_governance.to_dict() | {"operator_id": "bad"})
    with pytest.raises(PolicyError):
        replace(
            root.state.type_governance,
            memberships_json=root.state.type_governance.memberships_json * 2,
        )
    approvals = load_document(root.approvals_json)
    approvals["type_governance"][0]["signature"] = "not base64"
    changed = replace(root, approvals_json=canonical_bytes(approvals).decode())
    with pytest.raises(PolicyError):
        verify_genesis(
            changed,
            expected_workspace=root.state.workspace_id,
            expected_catalog=root.state.release.catalog_id,
            expected_genesis_digest=changed.digest,
        )


@pytest.mark.parametrize("attack", [None, "truncated-dag", "governors", "v1-downgrade"])
@pytest.mark.parametrize(
    "filename", ["federated-governed-chain.json", "protected-governed-chain.json"]
)
def test_portable_federated_history_replays_finality_and_old_governance(attack, filename):
    data = json.loads((FIXTURES / filename).read_bytes())
    if attack == "truncated-dag":
        proof = next(iter(data["records"][1]["approvals"]["existing_policies"].values()))
        proof["events"] = proof["events"][:4]
    elif attack == "governors":
        data["records"][1]["approvals"]["type_governance"] = []
    elif attack == "v1-downgrade":
        data["records"][1]["profile"] = "syn.governed-policy-record/1"
    records = tuple(GovernedPolicyRecord.from_dict(r) for r in data["records"])
    options = dict(
        expected_workspace=records[0].state.workspace_id,
        expected_catalog=records[0].state.release.catalog_id,
        expected_genesis_digest=data["genesis_digest"],
        expected_head_digest=data["head_digest"],
    )
    if attack is None:
        assert verified_chain(records, **options).release == records[-1].state.release
        from syn_client.dag_finality import verify_governance_finality

        proof = next(iter(load_document(records[-1].approvals_json)["existing_policies"].values()))
        result = verify_governance_finality(
            proof,
            previous=records[0],
            policy_digest=records[0].state.bindings[0].policy_digest,
            expected_proposal_digest=records[-1].proposal_digest,
        )
        assert result.received_round == 2
        assert result.timestamp_twice_ms == 20010
        if filename == "federated-governed-chain.json":
            assert (
                result.event_digest
                == "b6a2cacd8f90b18335edc8169e732a9b0e4d8e772aad756e7d95e4f46298a3a6"
            )
    else:
        with pytest.raises(PolicyError):
            verified_chain(records, **options)
