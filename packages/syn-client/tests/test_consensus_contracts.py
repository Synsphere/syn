"""Dependency-free policy, pinning, identity and canonical-byte tests."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest
from syn_client.consensus import (
    CanonicalTypeDefinition,
    CanonicalTypePolicyResolver,
    CanonicalTypeRelease,
    ConsensusPolicy,
    NodeIdentity,
    NodeKeyHash,
    PolicyBinding,
    PolicyError,
    TypePolicyPin,
    canonical_bytes,
    load_document,
)

CATALOG = "11111111-1111-4111-8111-111111111111"
DOMAIN = "22222222-2222-4222-8222-222222222222"
STREAM = "33333333-3333-4333-8333-333333333333"


def policy(**changes):
    values = dict(
        profile="syn.consensus-policy/1",
        policy_revision="1",
        protocol="syn.operator-log/1",
        custody_constraint="same-verified-issuer",
        membership_anchor_digest="a" * 64,
        participant_reference="node-public-key-sha256",
        quorum_rule="strict-majority",
        fault_model="crash-and-partition",
        proof_profile="syn.operator-commit/1",
        conflict_namespace="tasks",
        change_rule="current-policy-and-type-governance",
    )
    values.update(changes)
    return ConsensusPolicy.from_dict(values)


def definition(ref="syn://type/task", **changes):
    return CanonicalTypeDefinition(ref, "1", '{"name":"Task"}', changes.get("policy", policy()))


def resolver(*definitions, bindings=None):
    definitions = definitions or (definition(),)
    release = CanonicalTypeRelease(
        CATALOG, "1", tuple(sorted(definitions, key=lambda d: d.type_ref))
    )
    if bindings is None:
        bindings = (PolicyBinding(policy().digest, DOMAIN, "b" * 64, STREAM, "1", "a" * 64),)
    return CanonicalTypePolicyResolver(release, CATALOG, release.digest, bindings)


def pin(item):
    return TypePolicyPin(item.type_ref, item.digest, item.consensus_policy.digest)


def test_node_hash_is_raw_public_key_hash_and_never_an_operator_label():
    public = bytes(range(32))
    expected = "630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd"
    assert NodeKeyHash.from_public_key(public) == expected
    assert NodeIdentity(NodeKeyHash(expected), public).node_key_hash == expected
    assert NodeKeyHash.from_public_key(public) != hashlib.sha256(public.hex().encode()).hexdigest()
    for value in ("operator-7", "7", "QmPeer", "A" * 64, " " + expected, b"a" * 64):
        with pytest.raises(PolicyError):
            NodeKeyHash(value)
    with pytest.raises(PolicyError):
        NodeIdentity(NodeKeyHash("a" * 64), public)
    with pytest.raises(PolicyError):
        NodeIdentity(expected, public)
    for value in (b"x" * 31, b"x" * 33, "x" * 32, bytearray(32)):
        with pytest.raises(PolicyError):
            NodeKeyHash.from_public_key(value)


def test_new_canonical_profile_has_exact_unicode_and_integer_bytes():
    assert canonical_bytes({"z": 3, "a": "é😀"}) == '{"a":"é😀","z":3}'.encode()
    assert canonical_bytes({"a": "e\u0301"}) != canonical_bytes({"a": "é"})
    for value in (1.0, float("nan"), 2**53, {1: "value"}, "\ud800", (1, 2)):
        with pytest.raises(PolicyError):
            canonical_bytes(value)
    for text in ('{"x":1,"x":2}', '{"x":1.0}', '{"x":NaN}', "[]", '{"x":"\\ud800"}'):
        with pytest.raises(PolicyError):
            load_document(text)
    deep = 0
    for _ in range(18):
        deep = [deep]
    with pytest.raises(PolicyError):
        canonical_bytes(deep)


@pytest.mark.parametrize(
    "changes",
    [
        {"quorum_rule": "one-signature"},
        {"proof_profile": "syn.dag-hashgraph-finality/1"},
        {"participant_reference": "operator-id"},
        {"protocol": "unknown/1"},
        {"membership_anchor_digest": "A" * 64},
        {"policy_revision": True},
        {"change_rule": "new-policy-approves-itself"},
        {"future_field": 1},
    ],
)
def test_policy_rejects_protocol_mismatch_unknown_fields_and_downgrades(changes):
    with pytest.raises(PolicyError):
        policy(**changes)


def test_independent_custody_profile_has_its_own_fixed_trust_and_proof_rules():
    federated = policy(
        protocol="syn.bft-dag-hashgraph/1",
        custody_constraint="independent-issuer-weight-caps",
        quorum_rule="bft-supermajority",
        fault_model="byzantine-less-than-one-third-weight",
        proof_profile="syn.dag-hashgraph-finality/1",
    )
    assert federated.digest != policy().digest
    with pytest.raises(PolicyError):
        replace(federated, custody_constraint="same-verified-issuer")


def test_release_roundtrip_detaches_input_and_preserves_domain_separated_hashes():
    selected = resolver()
    data = selected.release.to_dict()
    loaded = CanonicalTypeRelease.from_json(json.dumps(data))
    assert loaded == selected.release
    assert loaded.digest == selected.release.digest
    data["types"][0]["consensusPolicy"]["policy_revision"] = "2"
    assert loaded.types[0].consensus_policy.policy_revision == "1"
    assert loaded.digest != hashlib.sha256(loaded.to_json().encode()).hexdigest()
    with pytest.raises(FrozenInstanceError):
        loaded.release_revision = "2"


def test_resolution_uses_exact_catalog_release_definition_policy_and_stream():
    selected = resolver()
    item = selected.release.types[0]
    context = selected.resolve(replace(pin(item), type_ref="synny://type/task"))
    assert context.canonical_type_ref == "syn://type/task"
    assert context.binding.stream_id == STREAM
    assert context.semantic_release_digest == selected.release.digest
    for bad_pin in (
        replace(pin(item), type_definition_digest="c" * 64),
        replace(pin(item), consensus_policy_digest="c" * 64),
        replace(pin(item), type_ref="syn://type/unknown"),
    ):
        with pytest.raises(PolicyError):
            selected.resolve(bad_pin)
    for changes in ({"expected_catalog_id": DOMAIN}, {"expected_release_digest": "c" * 64}):
        with pytest.raises(PolicyError):
            replace(selected, **changes)
    with pytest.raises(PolicyError):
        selected.resolve({"mode": "operator"})


def test_changed_type_or_policy_cannot_reuse_an_adopted_release_pin():
    selected = resolver()
    item = selected.release.types[0]
    for change in (
        replace(item, definition_json='{"name":"Changed"}'),
        replace(item, consensus_policy=replace(policy(), policy_revision="2")),
    ):
        with pytest.raises(PolicyError):
            replace(selected, release=replace(selected.release, types=(change,)))


def test_missing_policy_never_inherits_or_selects_a_default():
    selected = resolver(definition(policy=None))
    with pytest.raises(PolicyError, match="no explicit"):
        selected.resolve(
            TypePolicyPin("syn://type/task", selected.release.types[0].digest, policy().digest)
        )
    selected = resolver(bindings=())
    with pytest.raises(PolicyError, match="no adopted stream"):
        selected.resolve(pin(selected.release.types[0]))


def test_roster_conflict_ownership_and_atomic_write_sets_fail_closed():
    selected = resolver()
    with pytest.raises(PolicyError, match="membership anchor"):
        replace(selected, bindings=(replace(selected.bindings[0], roster_digest="c" * 64),))
    weaker = policy(policy_revision="2")
    binding = replace(selected.bindings[0], policy_digest=weaker.digest)
    with pytest.raises(PolicyError, match="Conflicting policy ownership"):
        resolver(
            definition(),
            definition("syn://type/weaker", policy=weaker),
            bindings=selected.bindings + (binding,),
        )
    unrelated = policy(conflict_namespace="other-tasks")
    other = definition("syn://type/other", policy=unrelated)
    mixed = resolver(
        other,
        definition(),
        bindings=selected.bindings
        + (replace(binding, policy_digest=unrelated.digest, stream_id=CATALOG),),
    )
    with pytest.raises(PolicyError, match="incompatible"):
        mixed.resolve_write_set(tuple(pin(t) for t in mixed.release.types))
    compatible = resolver(definition(), definition("syn://type/task-result"))
    assert len(compatible.resolve_write_set(tuple(pin(t) for t in compatible.release.types))) == 2


def test_release_rejects_ambiguous_alias_duplicates_and_noncanonical_definition():
    with pytest.raises(PolicyError):
        definition("synny://type/task")
    with pytest.raises(PolicyError):
        CanonicalTypeRelease(CATALOG, "1", (definition(), definition()))
    with pytest.raises(PolicyError):
        replace(definition(), definition_json='{ "name": "Task" }')
    with pytest.raises(PolicyError):
        replace(definition(), definition_json='{"consensusPolicy":{}}')
