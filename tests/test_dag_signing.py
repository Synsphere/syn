"""Protected v2 policy replay; legacy consent cannot impersonate guarded events."""

import base64
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest
from syn_client.consensus import PolicyError
from syn_client.dag_finality import PROTECTED_ALGORITHM, event_payload_digest
from syn_client.dag_signing import protected_event_digest, verify_protected_event
from syn_client.governance import verify_transition

from test_dag_finality import attach, candidate, federated_genesis, history
from test_governance import NOW, approve, private, signed


def protected_genesis(workspace=None):
    root = federated_genesis()
    t = root.state.release.types[0]
    p = replace(
        t.consensus_policy,
        protocol="syn.bft-dag-hashgraph/2",
        proof_profile="syn.dag-hashgraph-finality/2",
    )
    state = replace(
        root.state,
        workspace_id=workspace or root.state.workspace_id,
        release=replace(root.state.release, types=(replace(t, consensus_policy=p),)),
        bindings=(replace(root.state.bindings[0], policy_digest=p.digest),),
    )
    return approve(replace(root, state=state))


def protected_event(root, body):
    body = deepcopy(body)
    body["profile"] = "syn.hashgraph-event/2"
    member = next(
        m
        for m in root.state.policy_authorities[0].memberships()
        if m["agent"]["signer_hash"] == body["creator"]
    )
    n = member["account_number"]
    identifier = f"{n:08x}-0000-4000-8000-{int(body['sequence']):012}"
    payload = event_payload_digest(body)
    proof = signed(
        private(n),
        b"syn-dag-event-proof-v1",
        dict(
            schema_version=1,
            profile="syn-dag-event-proof-v1",
            membership=member,
            reservation_id=identifier,
            payload_sha256=payload,
        ),
    )
    event = {"body": body, "proof": proof}
    # Public verification cannot decrypt a capsule. The Rust-produced fixture
    # separately exercises actual AEAD/recovery; here only its opaque syntax is needed.
    intent = signed(
        private(n),
        b"syn-dag-reservation-intent-v1",
        dict(
            schema_version=1,
            profile="syn-dag-reservation-intent-v1",
            membership=member,
            reservation_id=identifier,
            context_digest=body["context_digest"],
            sequence=body["sequence"],
            self_parent=body["self_parent"],
            timestamp_ms=body["timestamp_ms"],
            payload_sha256=payload,
            event_digest=protected_event_digest(event),
            capsule=dict(
                nonce_b64=base64.b64encode(bytes(12)).decode(),
                ciphertext_b64=base64.b64encode(bytes(16)).decode(),
            ),
        ),
    )
    event["reservation"] = signed(
        private(n + 200),
        b"syn-dag-anchor-receipt-v1",
        dict(schema_version=1, profile="syn-dag-anchor-receipt-v1", intent=intent),
    )
    return event


def protected_history(root, one, stale_first=None):
    def event_with_header(previous, body):
        body = deepcopy(body)
        if body["proposal"] is not None:
            if stale_first is not None and body["timestamp_ms"] == 10000:
                body["proposal"] = deepcopy(stale_first)
            else:
                body["proposal"].update(
                    sequence=one.sequence,
                    previous_record_digest=one.previous_record_digest,
                    state_digest=one.state.digest,
                )
        return protected_event(previous, body)

    with patch("test_dag_finality.signed_event", side_effect=event_with_header):
        evidence = history(root, one, count=90, unauthorized=stale_first is not None)
    evidence.update(profile="syn.dag-hashgraph-finality/2", algorithm=PROTECTED_ALGORITHM)
    return evidence


def test_guarded_profile_requires_its_own_domain_and_independent_operator_receipts():
    root = protected_genesis()
    one = candidate(root)
    evidence = protected_history(root, one)
    verify_transition(root, attach(one, root, evidence), now=NOW)
    for attack in ("receipt", "signature", "generic-prefix", "context", "event-hash"):
        wrong = deepcopy(evidence)
        e = wrong["events"][-1]
        m = e["proof"]["membership"]
        n = m["account_number"]
        if attack == "receipt":
            e.pop("reservation")
        elif attack == "signature":
            e["reservation"]["signature"] = base64.b64encode(bytes(64)).decode()
        elif attack == "generic-prefix":
            e["proof"] = signed(
                private(n),
                b"syn-agent-proof-v1",
                {k: v for k, v in e["proof"].items() if k != "signature"},
            )
        elif attack == "context":
            e["reservation"]["intent"]["context_digest"] = "f" * 64
        else:
            i = e["reservation"]["intent"]
            i["event_digest"] = "e" * 64
            i = signed(
                private(n),
                b"syn-dag-reservation-intent-v1",
                {k: v for k, v in i.items() if k != "signature"},
            )
            e["reservation"] = signed(
                private(n + 200),
                b"syn-dag-anchor-receipt-v1",
                dict(schema_version=1, profile="syn-dag-anchor-receipt-v1", intent=i),
            )
        with pytest.raises(PolicyError):
            verify_transition(root, attach(one, root, wrong), now=NOW)


def test_legacy_evidence_cannot_upgrade_itself_to_protected_policy():
    root = protected_genesis()
    one = candidate(root)
    old = history(root, one)
    old.update(profile="syn.dag-hashgraph-finality/2", algorithm=PROTECTED_ALGORITHM)
    with pytest.raises(PolicyError):
        verify_transition(root, attach(one, root, old), now=NOW)


def test_agent_signature_does_not_authorize_a_changed_body_or_assignment():
    root = protected_genesis()
    e = protected_history(root, candidate(root))["events"][0]
    member = e["proof"]["membership"]
    assert verify_protected_event(e, membership=member) == e
    changed = deepcopy(e)
    changed["body"]["timestamp_ms"] += 1
    with pytest.raises(PolicyError):
        verify_protected_event(changed, membership=member)
    other = root.state.policy_authorities[0].memberships()[1]
    with pytest.raises(PolicyError):
        verify_protected_event(e, membership=other)


def test_installing_protected_policy_requires_finality_under_the_old_research_profile():
    old = federated_genesis()
    one = candidate(old)
    t = one.state.release.types[0]
    p = replace(
        t.consensus_policy,
        protocol="syn.bft-dag-hashgraph/2",
        proof_profile="syn.dag-hashgraph-finality/2",
    )
    state = replace(
        one.state,
        release=replace(one.state.release, types=(replace(t, consensus_policy=p),)),
        bindings=(replace(one.state.bindings[0], policy_digest=p.digest),),
    )
    one = approve(replace(one, state=state), old)
    verify_transition(old, attach(one, old, history(old, one)), now=NOW)
    with pytest.raises(PolicyError):
        verify_transition(old, attach(one, old, protected_history(old, one)), now=NOW)


def test_a_previous_epochs_valid_governor_certificate_cannot_capture_the_next_epoch():
    root = protected_genesis()
    one = candidate(root)
    original = protected_history(root, one)
    stale = original["events"][0]["body"]["proposal"]
    head = attach(one, root, original)
    two = candidate(head)
    # The signed obsolete candidate is retained as a causal event, then ignored;
    # the first CURRENT candidate still finalizes under the current old policy.
    evidence = protected_history(head, two, stale_first=stale)
    verify_transition(head, attach(two, head, evidence), now=NOW)


def write_fixture(path):
    from syn_client.consensus import canonical_bytes

    root = protected_genesis()
    one = candidate(root)
    one = attach(one, root, protected_history(root, one))
    path.write_text(
        canonical_bytes(
            dict(
                genesis_digest=root.digest,
                head_digest=one.digest,
                records=[root.to_dict(), one.to_dict()],
            )
        ).decode()
        + "\n",
        encoding="utf-8",
    )
