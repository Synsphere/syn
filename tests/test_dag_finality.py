"""Real signed gossip histories, virtual votes, competing proposals and admission."""

from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
import random

import pytest
from sqlalchemy import select
from syn_client.consensus import PolicyError, canonical_bytes, load_document
from syn_client.dag_finality import (
    ALGORITHM,
    _Replay,
    _virtual_vote,
    event_digest,
    event_payload_digest,
    governance_finality_context,
    verify_governance_finality,
)
from syn_client.governance import (
    ApprovalAuthority,
    GovernedPolicyRecord,
    required_approval_authority,
    verified_chain,
    verify_transition,
)

from syn.policy_adoption import signedRecords
from test_governance import (
    NOW,
    WORKSPACE,
    CATALOG,
    admit,
    approve,
    authority,
    genesis,
    proofs,
    rig as _sqlite_rig,
    successor,
)


@pytest.fixture
def rig():
    yield from _sqlite_rig.__wrapped__()


def federation():
    members = [authority((n,), 1, issuer=200 + n).memberships_json[0] for n in (20, 21, 22, 23)]
    members.sort(key=lambda m: load_document(m)["agent"]["signer_hash"])
    return ApprovalAuthority(3, tuple(members))


@lru_cache
def federated_genesis():
    root = genesis()
    roster = federation()
    t = root.state.release.types[0]
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
        root.state,
        release=replace(root.state.release, types=(replace(t, consensus_policy=p),)),
        bindings=(
            replace(root.state.bindings[0], policy_digest=p.digest, roster_digest=roster.digest),
        ),
        policy_authorities=(roster,),
    )
    return approve(replace(root, state=state, profile="syn.governed-policy-record/2"))


def candidate(root, name="Changed", rotate=False):
    return approve(
        replace(successor(root, name=name, rotate=rotate), profile="syn.governed-policy-record/2"),
        root,
    )


def signed_event(root, body):
    roster = root.state.policy_authorities[0]
    m = next(m for m in roster.memberships() if m["agent"]["signer_hash"] == body["creator"])
    proof = proofs(roster, event_payload_digest(body), numbers=(m["account_number"],))[0]
    return {"body": body, "proof": proof}


def history(
    root, proposal, *, active=4, count=150, seed=7, competing=None, unauthorized=False, fork_at=None
):
    context = governance_finality_context(root, root.state.bindings[0].policy_digest)
    members = root.state.policy_authorities[0].memberships()[:active]
    latest = {}
    events = []
    rng = random.Random(seed)
    g = root.state.type_governance
    payload = dict(
        proposal_digest=proposal.proposal_digest,
        type_governance=proofs(g, proposal.approval_payload(g)),
    )
    for i in range(count):
        m = members[i] if i < active else rng.choice(members)
        creator = m["agent"]["signer_hash"]
        sp = latest.get(creator)
        if i == fork_at:
            sp = None  # A validly signed second root by one Byzantine creator.
        choices = [e for c, e in latest.items() if c != creator]
        op = None if i < active else rng.choice(choices)
        operation = payload if i == 0 else None
        if i == 1 and competing is not None:
            operation = dict(
                proposal_digest=competing.proposal_digest,
                type_governance=proofs(g, competing.approval_payload(g)),
            )
        if unauthorized and i == 0:
            operation = {"proposal_digest": "f" * 64, "type_governance": []}
        if unauthorized and i == active:
            operation = payload
        body = dict(
            profile="syn.hashgraph-event/1",
            context_digest=context,
            creator=creator,
            sequence=str(1 if sp is None else int(sp["body"]["sequence"]) + 1),
            self_parent=None if sp is None else event_digest(sp),
            other_parent=None if op is None else event_digest(op),
            timestamp_ms=10_000 + i,
            proposal=operation,
        )
        event = signed_event(root, body)
        latest[creator] = event
        events.append(event)
    return dict(
        profile="syn.dag-hashgraph-finality/1",
        algorithm=ALGORITHM,
        context_digest=context,
        events=events,
    )


def attach(proposal, root, evidence):
    approvals = load_document(proposal.approvals_json)
    approvals["existing_policies"][root.state.bindings[0].policy_digest] = evidence
    return replace(proposal, approvals_json=canonical_bytes(approvals).decode())


@lru_cache
def valid_pair():
    root = federated_genesis()
    proposal = candidate(root)
    evidence = history(root, proposal)
    return root, attach(proposal, root, evidence)


def evidence_of(record):
    return next(iter(load_document(record.approvals_json)["existing_policies"].values()))


def test_real_replay_and_durable_roundtrip(rig):
    root, one = valid_pair()
    verify_transition(root, one, now=NOW)
    result = verify_governance_finality(
        evidence_of(one),
        previous=root,
        policy_digest=root.state.bindings[0].policy_digest,
        expected_proposal_digest=one.proposal_digest,
    )
    assert result.received_round >= 2
    assert result.event_digest == event_digest(evidence_of(one)["events"][0])
    store, _ = rig
    admit(store, root, root)
    admit(store, one, root)
    assert (
        store.chain(
            workspace=WORKSPACE,
            catalog=CATALOG,
            genesis=root.digest,
            head=one.digest,
        )[-1]
        == one
    )
    verified_chain(
        (root, one),
        expected_workspace=WORKSPACE,
        expected_catalog=CATALOG,
        expected_genesis_digest=root.digest,
        expected_head_digest=one.digest,
    )


@pytest.mark.parametrize("active,seed", [(3, 1), (3, 8), (4, 2), (4, 9)])
def test_finalizes_with_one_silent_byzantine_and_multiple_gossip_schedules(active, seed):
    root = federated_genesis()
    one = candidate(root)
    evidence = history(root, one, active=active, seed=seed)
    verify_transition(root, attach(one, root, evidence), now=NOW)


@pytest.mark.parametrize(
    "attack",
    [
        "count",
        "truncated",
        "missing_parent",
        "duplicate",
        "signature",
        "context",
        "algorithm",
        "sequence",
        "timestamp",
        "creator",
        "governance",
        "downgrade",
    ],
)
def test_tampered_incomplete_or_signature_only_evidence_fails(attack):
    root, one = valid_pair()
    data = deepcopy(one.to_dict())
    d = data["approvals"]["existing_policies"][root.state.bindings[0].policy_digest]
    if attack == "count":
        data["approvals"]["existing_policies"][root.state.bindings[0].policy_digest] = proofs(
            root.state.policy_authorities[0],
            one.approval_payload(
                root.state.policy_authorities[0], root.state.bindings[0].policy_digest
            ),
        )
    elif attack == "truncated":
        d["events"] = d["events"][:4]
    elif attack == "missing_parent":
        d["events"].pop(0)
    elif attack == "duplicate":
        d["events"].append(d["events"][0])
    elif attack == "signature":
        d["events"][0]["proof"]["signature"] = "A" * 86 + "=="
    elif attack == "context":
        d["context_digest"] = "0" * 64
    elif attack == "algorithm":
        d["algorithm"] = "hiero-inferred-from-signature-count"
    elif attack in ("sequence", "timestamp", "creator"):
        event = d["events"][-1]
        b = event["body"]
        if attack == "sequence":
            b["sequence"] = "999"
        elif attack == "timestamp":
            b["timestamp_ms"] = 0
        else:
            b["creator"] = root.state.type_governance.memberships()[0]["agent"]["signer_hash"]
        if attack != "creator":
            d["events"][-1] = signed_event(root, b)
    elif attack == "governance":
        data["approvals"]["type_governance"] = []
    else:
        data["profile"] = "syn.governed-policy-record/1"
    with pytest.raises(PolicyError):
        verify_transition(root, GovernedPolicyRecord.from_dict(data), now=NOW)


def test_later_authorized_competing_proposal_cannot_replace_first_finalized_choice():
    root = federated_genesis()
    a, b = candidate(root, "A"), candidate(root, "B")
    evidence = history(root, a, competing=b)
    # Determine the protocol winner, independently of caller/proof claims.
    context = governance_finality_context(root, root.state.bindings[0].policy_digest)
    replay = _Replay(
        evidence["events"],
        context=context,
        roster=root.state.policy_authorities[0],
        governance=root.state.type_governance,
    )
    first = replay.first_proposal().proposal_digest
    winner, loser = (a, b) if first == a.proposal_digest else (b, a)
    verify_transition(root, attach(winner, root, evidence), now=NOW)
    with pytest.raises(PolicyError, match="first finalized"):
        verify_transition(root, attach(loser, root, evidence), now=NOW)


def test_sibling_delivery_order_does_not_change_finality_and_a_minority_cannot_finalize():
    root, one = valid_pair()
    evidence = deepcopy(evidence_of(one))
    evidence["events"][:4] = reversed(evidence["events"][:4])
    verify_transition(root, attach(one, root, evidence), now=NOW)
    minority = history(root, one, active=2, count=80)
    with pytest.raises(PolicyError, match="no finalized"):
        verify_transition(root, attach(one, root, minority), now=NOW)


@pytest.mark.parametrize(
    "values,distance,expected",
    [
        ([True, True, True], 2, (True, True)),
        ([False, False, False], 2, (False, True)),
        ([True, True, False, False], 2, (True, False)),
        ([True, True, False, False], 3, (None, False)),
        ([True, True, True], 3, (True, False)),
        ([True, True, None], 4, (None, False)),
    ],
)
def test_coin_ambiguity_and_unknown_votes_cannot_manufacture_fame(values, distance, expected):
    assert _virtual_vote(values, 3, distance) == expected


def test_observed_equivocation_beyond_the_old_fault_budget_holds():
    root, one = valid_pair()
    evidence = deepcopy(evidence_of(one))
    evidence["events"] = evidence["events"][:4]
    for i in (0, 1):
        body = deepcopy(evidence["events"][i]["body"])
        body["timestamp_ms"] += 100
        evidence["events"].append(signed_event(root, body))
    with pytest.raises(PolicyError, match="fault budget"):
        verify_transition(root, attach(one, root, evidence), now=NOW)


@pytest.mark.parametrize("field", ["domain_id", "genesis_digest", "stream_id", "policy_epoch"])
def test_history_cannot_replay_in_another_policy_epoch_or_domain(field):
    root, one = valid_pair()
    value = (
        "2" if field == "policy_epoch" else ("a" * 64 if field == "genesis_digest" else WORKSPACE)
    )
    other = approve(
        replace(
            root,
            state=replace(
                root.state, bindings=(replace(root.state.bindings[0], **{field: value}),)
            ),
        )
    )
    proposed = candidate(other)
    with pytest.raises(PolicyError, match="context"):
        verify_transition(other, attach(proposed, other, evidence_of(one)), now=NOW)


def test_unauthorized_proposal_is_noop_without_dropping_its_signed_event():
    root = federated_genesis()
    one = candidate(root)
    verify_transition(root, attach(one, root, history(root, one, unauthorized=True)), now=NOW)


def test_fork_detection_is_ancestor_local_and_one_forker_does_not_inflate_weight():
    root = federated_genesis()
    one = candidate(root)
    evidence = history(root, one, count=200, fork_at=10)
    context = governance_finality_context(root, root.state.bindings[0].policy_digest)
    replay = _Replay(
        evidence["events"],
        context=context,
        roster=root.state.policy_authorities[0],
        governance=root.state.type_governance,
    )
    assert any(replay.forked)
    verify_transition(root, attach(one, root, evidence), now=NOW)
    # Independent set/graph-walk oracle: no production bitsets or visibility cache.
    ancestors, self_ancestors = [], []
    ids = {}
    for i, e in enumerate(evidence["events"]):
        b = e["body"]
        ps = [ids[b[k]] for k in ("self_parent", "other_parent") if b[k] is not None]
        ancestors.append({i}.union(*(ancestors[p] for p in ps)))
        sp = None if b["self_parent"] is None else ids[b["self_parent"]]
        self_ancestors.append({i} | (set() if sp is None else self_ancestors[sp]))
        ids[event_digest(e)] = i

    def sees(x, y):
        relevant = [a for a in ancestors[x] if replay.creators[a] == replay.creators[y]]
        return y in ancestors[x] and not any(
            a not in self_ancestors[b] and b not in self_ancestors[a]
            for a in relevant
            for b in relevant
        )

    for x in (9, 10, 30, 90, 199):
        for y in range(x + 1):
            assert replay.sees(x, y) == sees(x, y)
            assert replay.strongly_sees(x, y) == (
                sees(x, y)
                and len({replay.creators[z] for z in ancestors[x] if sees(x, z) and sees(z, y)})
                >= 3
            )
    prefix = _Replay(
        evidence["events"][:10],
        context=context,
        roster=root.state.policy_authorities[0],
        governance=root.state.type_governance,
    )
    assert replay.rounds[:10] == prefix.rounds
    assert replay.forked[:10] == prefix.forked


def test_new_governors_or_roster_cannot_self_approve_and_rejected_append_is_atomic(rig):
    root, valid = valid_pair()
    rotated = candidate(root)
    roster = authority((7, 8, 9))
    t = rotated.state.release.types[0]
    p = replace(
        t.consensus_policy,
        membership_anchor_digest=roster.digest,
        protocol="syn.operator-log/1",
        custody_constraint="same-verified-issuer",
        quorum_rule="strict-majority",
        fault_model="crash-and-partition",
        proof_profile="syn.operator-commit/1",
    )
    state = replace(
        rotated.state,
        release=replace(rotated.state.release, types=(replace(t, consensus_policy=p),)),
        bindings=(
            replace(rotated.state.bindings[0], policy_digest=p.digest, roster_digest=roster.digest),
        ),
        policy_authorities=(roster,),
        type_governance=authority((10, 11, 12)),
    )
    rotated = approve(replace(rotated, state=state), root)
    evidence = history(root, rotated)
    rotated = attach(rotated, root, evidence)
    verify_transition(root, rotated, now=NOW)
    bad = approve(rotated, root, type_authority=rotated.state.type_governance)
    bad = attach(bad, root, evidence)
    store, engine = rig
    admit(store, root, root)
    with pytest.raises(PolicyError):
        admit(store, bad, root)
    with engine.connect() as c:
        assert len(c.execute(select(signedRecords)).all()) == 1
    admit(store, rotated, root)
    with pytest.raises(PolicyError):
        required_approval_authority(valid, root, root.state.bindings[0].policy_digest)


def write_fixture(path):
    root, one = valid_pair()
    path.write_text(
        canonical_bytes(
            {
                "profile": "syn.governance-chain/1",
                "records": [root.to_dict(), one.to_dict()],
                "genesis_digest": root.digest,
                "head_digest": one.digest,
            }
        ).decode()
        + "\n",
        encoding="utf-8",
    )
