"""Portable public group-certificate verification; ephemeral test issuer only."""
import hashlib
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from syn_client.group import CERT_DOMAIN, group_epoch, group_id, verify_group
from syn_client.operator_protocol import SynProtocolError, canonical


@pytest.fixture
def certificate():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    issuer = hashlib.sha256(public).hexdigest()
    body = {"profile": "syn.agent-group-certificate/1", "operator_public_key_hash": issuer,
            "node_public_key_hash": "12" * 32, "network_id": "fixture", "group_id": "be31002b-b7a6-4e46-bd29-4e39fb04e31e",
            "group_epoch": 7, "group_key_commitment": "34" * 32}
    return {"body": body, "operator_public_key": public.hex(),
            "signature": private.sign(CERT_DOMAIN + canonical(body)).hex()}


def test_certificate_requires_independent_operator_node_group_and_epoch_pins(certificate):
    expected = dict(expected_operator=certificate["body"]["operator_public_key_hash"], expected_node="12" * 32,
                    expected_group=certificate["body"]["group_id"], expected_epoch=7)
    assert verify_group(certificate, **expected)["group_epoch"] == 7
    for key, value in (("expected_operator", "56" * 32), ("expected_node", "56" * 32),
                       ("expected_group", "4f672321-11ba-4405-843d-a5961a784bf6"), ("expected_epoch", 8)):
        with pytest.raises(SynProtocolError):
            verify_group(certificate, **{**expected, key: value})


def test_unsigned_group_commitment_change_is_not_a_membership(certificate):
    from syn_client.consensus import PolicyError
    expected = dict(expected_operator=certificate["body"]["operator_public_key_hash"], expected_node="12" * 32,
                    expected_group=certificate["body"]["group_id"], expected_epoch=7)
    changed = deepcopy(certificate)
    changed["body"]["group_key_commitment"] = "78" * 32
    with pytest.raises(PolicyError):
        verify_group(changed, **expected)


@pytest.mark.parametrize("value", [True, 0, -1, 2**31, "1"])
def test_epochs_are_typed_and_bounded(value):
    with pytest.raises(SynProtocolError):
        group_epoch(value)


@pytest.mark.parametrize("value", [None, "", "not-a-uuid", "00000000-0000-0000-0000-000000000000", "BE31002B-B7A6-4E46-BD29-4E39FB04E31E"])
def test_group_ids_are_canonical_nonzero_uuids(value):
    with pytest.raises(SynProtocolError):
        group_id(value)
