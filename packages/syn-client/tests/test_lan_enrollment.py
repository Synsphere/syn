"""Signed LAN endpoint checks at the portable public SDK boundary."""

import copy
import hashlib

import pytest

from syn_client.consensus import PolicyError
from syn_client.lan_enrollment import (
    ADVERTISEMENT_DOMAIN,
    ADVERTISEMENT_PROFILE,
    CONFIDENTIAL_PULSE_DOMAIN,
    DEVICE_PROFILE,
    OPERATOR_PULSE_DOMAIN,
    private_endpoint,
    verify_advertisement,
)
from syn_client.operator_protocol import SynProtocolError, canonical


@pytest.fixture
def signed_route():
    ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    key = ed25519.Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    operator = hashlib.sha256(public).hexdigest()
    at = 1_800_000_000
    inner = {
        "schema_version": 1,
        "type_ref": "synny://type/syn-operator-pulse-v1",
        "network_id": "lab",
        "master_public_key_hash": operator,
        "challenge": "ab" * 32,
        "issued_at": at,
        "expires_at": at + 120,
    }
    pulse = {
        "profile": "syn.confidential-enrollment-pulse/1",
        "signed_pulse": {
            "pulse": inner,
            "operator_public_key": public.hex(),
            "signature": key.sign(OPERATOR_PULSE_DOMAIN + canonical(inner)).hex(),
        },
        "encryption_public_key": "ca" * 32,
        "device_profile": DEVICE_PROFILE,
    }
    body = {
        "profile": ADVERTISEMENT_PROFILE,
        "endpoint": {"host": "192.168.1.7", "port": 45121},
        "pulse": {
            "body": pulse,
            "signature": key.sign(CONFIDENTIAL_PULSE_DOMAIN + canonical(pulse)).hex(),
        },
    }
    return (
        {
            "advertisement": body,
            "signature": key.sign(ADVERTISEMENT_DOMAIN + canonical(body)).hex(),
        },
        {
            "expected_operator": operator,
            "network_id": "lab",
            "host": "192.168.1.7",
            "port": 45121,
            "now": at,
        },
    )


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "100.64.0.1",
        "169.254.1.2",
        "172.32.0.2",
        "8.8.8.8",
        "::1",
        "localhost",
        "192.168.001.1",
    ],
)
def test_rejects_non_rfc1918_or_ambiguous_endpoints(host):
    with pytest.raises(SynProtocolError, match="private IPv4"):
        private_endpoint(host, 45121)


@pytest.mark.parametrize("port", [0, 65536, True, "45121"])
def test_rejects_noncanonical_ports(port):
    with pytest.raises(SynProtocolError, match="TCP port"):
        private_endpoint("10.1.2.3", port)


@pytest.mark.parametrize("host", ["10.1.2.3", "172.31.1.2", "192.168.1.7"])
def test_accepts_literal_rfc1918_endpoint(host):
    assert private_endpoint(host, 45121) == {"host": host, "port": 45121}


def test_signed_endpoint_binds_port_and_inner_encryption_key(signed_route):
    packet, pinned = signed_route
    assert verify_advertisement(packet, **pinned) == packet["advertisement"]
    changed = copy.deepcopy(packet)
    changed["advertisement"]["endpoint"]["port"] += 1
    with pytest.raises(PolicyError, match="signature"):
        verify_advertisement(changed, **{**pinned, "port": pinned["port"] + 1})
    changed = copy.deepcopy(packet)
    changed["advertisement"]["pulse"]["body"]["encryption_public_key"] = "bd" * 32
    with pytest.raises(PolicyError, match="signature"):
        verify_advertisement(changed, **pinned)


@pytest.mark.parametrize(
    "replacement",
    [
        {"host": "192.168.1.8"},
        {"port": 45122},
        {"network_id": "wrong"},
        {"expected_operator": "00" * 32},
        {"now": 1_800_000_121},
    ],
)
def test_advertisement_refuses_wrong_pins_or_expired_pulse(signed_route, replacement):
    packet, pinned = signed_route
    with pytest.raises(SynProtocolError):
        verify_advertisement(packet, **{**pinned, **replacement})
