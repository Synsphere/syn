"""Genesis is explicit pinned configuration, never self-authorising governance."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from syn_client.consensus import PolicyBinding, PolicyError, TypePolicyPin
from syn_client.policy_adoption import GenesisPolicyAdoption

FIXTURE = Path(__file__).parent / "fixtures/policy-genesis.json"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
CATALOG = "11111111-1111-4111-8111-111111111111"


def test_portable_genesis_vector_and_independent_scope_pins():
    document = GenesisPolicyAdoption.from_json(FIXTURE.read_text(encoding="utf-8"))
    assert document.digest == "b84d19cffb6076fa9e1a322842975fa56a5d764d55aa4fbea259407e22e7abc2"
    # Frozen synthetic wire vector: no credentials, live roster or finality proof.
    assert (
        document.digest
        == hashlib.sha256(
            b"syn.policy-genesis/1\0" + FIXTURE.read_bytes().rstrip(b"\r\n")
        ).hexdigest()
    )
    restored = GenesisPolicyAdoption.from_json(document.to_json())
    assert restored == document
    resolver = restored.resolver(
        expected_workspace_id=WORKSPACE,
        expected_catalog_id=CATALOG,
        expected_adoption_digest=document.digest,
    )
    definition = document.release.types[0]
    assert (
        resolver.resolve(
            TypePolicyPin(
                definition.type_ref, definition.digest, definition.consensus_policy.digest
            )
        ).binding
        == document.bindings[0]
    )
    for workspace, catalog, digest in (
        (CATALOG, CATALOG, document.digest),
        (WORKSPACE, WORKSPACE, document.digest),
        (WORKSPACE, CATALOG, "f" * 64),
    ):
        with pytest.raises(PolicyError):
            document.resolver(
                expected_workspace_id=workspace,
                expected_catalog_id=catalog,
                expected_adoption_digest=digest,
            )


def test_manifest_tampering_cannot_reuse_external_anchor():
    document = GenesisPolicyAdoption.from_json(FIXTURE.read_text(encoding="utf-8"))
    for changed in (
        replace(document, release=replace(document.release, release_revision="2")),
        replace(document, bindings=(replace(document.bindings[0], stream_id=WORKSPACE),)),
    ):
        with pytest.raises(PolicyError):
            changed.resolver(
                expected_workspace_id=WORKSPACE,
                expected_catalog_id=CATALOG,
                expected_adoption_digest=document.digest,
            )


def test_genesis_rejects_unknown_authority_or_unbound_forked_config():
    document = GenesisPolicyAdoption.from_json(FIXTURE.read_text(encoding="utf-8"))
    for update in (
        {"trust_basis": "new-policy-approves-itself"},
        {"signature": "pretend"},
        {"profile": "syn.policy-governed-transition/1"},
        {"bindings": []},
    ):
        with pytest.raises(PolicyError):
            GenesisPolicyAdoption.from_dict({**document.to_dict(), **update})
    for bindings in (
        (document.bindings[0],) * 2,
        (replace(document.bindings[0], policy_epoch="2"),),
        (replace(document.bindings[0], policy_digest="f" * 64),),
    ):
        with pytest.raises(PolicyError):
            replace(document, bindings=bindings)
    with pytest.raises(PolicyError):
        PolicyBinding.from_dict({**document.to_dict()["bindings"][0], "operator_id": "bad"})
    with pytest.raises(PolicyError):
        GenesisPolicyAdoption.from_json(
            document.to_json().replace('"trust_basis":', '"trust_basis":"duplicate","trust_basis":')
        )
