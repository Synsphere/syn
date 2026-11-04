"""Public verification of the signed endpoint for confidential Syn enrollment."""

from __future__ import annotations

import hashlib
from ipaddress import IPv4Address, IPv4Network

from .custody import verify_ed25519
from .operator_protocol import SynProtocolError, canonical, exact, hex_bytes

ADVERTISEMENT_PROFILE = "syn.lan-enrollment-advertisement/1"
ADVERTISEMENT_DOMAIN = b"syn.lan-enrollment-advertisement.v1\0"
OPERATOR_PULSE_DOMAIN = b"syn.operator.pulse.v1\0"
CONFIDENTIAL_PULSE_DOMAIN = b"syn.operator.confidential-enrollment.v1\0"
DEVICE_PROFILE = "syn.host-bound-virtual-device/1"

PRIVATE_NETWORKS = tuple(
    IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def private_endpoint(host: object, port: object) -> dict[str, str | int]:
    """Accept only a literal RFC1918 IPv4 endpoint and an explicit TCP port."""
    try:
        if not isinstance(host, str) or len(host) > 15:
            raise ValueError
        address = IPv4Address(host)
    except ValueError:
        raise SynProtocolError("LAN enrollment requires a canonical private IPv4 host") from None
    if str(address) != host or not any(
        address in network and address not in {network.network_address, network.broadcast_address}
        for network in PRIVATE_NETWORKS
    ):
        raise SynProtocolError("LAN enrollment requires a canonical private IPv4 host")
    if type(port) is not int or not 1 <= port <= 65535:
        raise SynProtocolError("LAN enrollment requires a bounded TCP port")
    return {"host": host, "port": port}


def verify_enrollment_pulse(packet, expected_operator, network_id, now):
    """Verify the existing confidential pulse and its nested operator signature."""
    outer = exact(packet, {"body", "signature"}, "confidential operator pulse")
    body = exact(
        outer["body"],
        {"profile", "signed_pulse", "encryption_public_key", "device_profile"},
        "enrollment pulse",
    )
    signed = exact(
        body["signed_pulse"], {"pulse", "operator_public_key", "signature"}, "operator pulse"
    )
    public = hex_bytes(signed["operator_public_key"], 32, "operator public key")
    inner = exact(
        signed["pulse"],
        {
            "schema_version",
            "type_ref",
            "network_id",
            "master_public_key_hash",
            "challenge",
            "issued_at",
            "expires_at",
        },
        "operator pulse fields",
    )
    if (
        hashlib.sha256(public).hexdigest() != expected_operator
        or type(inner["schema_version"]) is not int
        or inner["schema_version"] != 1
        or inner["type_ref"] != "synny://type/syn-operator-pulse-v1"
        or body["profile"] != "syn.confidential-enrollment-pulse/1"
        or body["device_profile"] != DEVICE_PROFILE
        or inner["master_public_key_hash"] != expected_operator
        or inner["network_id"] != network_id
        or type(inner["issued_at"]) is not int
        or type(inner["expires_at"]) is not int
        or not 0 < inner["issued_at"] <= now <= inner["expires_at"]
        or not 1 <= inner["expires_at"] - inner["issued_at"] <= 300
    ):
        raise SynProtocolError(
            "Confidential pulse is stale or differs from independently pinned operator"
        )
    verify_ed25519(
        public,
        hex_bytes(outer["signature"], 64, "enrollment pulse signature"),
        CONFIDENTIAL_PULSE_DOMAIN + canonical(body),
    )
    verify_ed25519(
        public,
        hex_bytes(signed["signature"], 64, "pulse signature"),
        OPERATOR_PULSE_DOMAIN + canonical(inner),
    )
    hex_bytes(body["encryption_public_key"], 32, "encryption public key")
    if hex_bytes(inner["challenge"], 32, "pulse nonce") == bytes(32):
        raise SynProtocolError("Enrollment pulse nonce cannot be zero")
    return body


def verify_advertisement(value, *, expected_operator, network_id, host, port, now):
    """Verify the signed route against the independently selected endpoint."""
    packet = exact(value, {"advertisement", "signature"}, "signed LAN enrollment advertisement")
    body = exact(
        packet["advertisement"], {"profile", "endpoint", "pulse"}, "LAN enrollment advertisement"
    )
    endpoint = exact(body["endpoint"], {"host", "port"}, "advertised endpoint")
    if body["profile"] != ADVERTISEMENT_PROFILE or private_endpoint(
        endpoint["host"], endpoint["port"]
    ) != private_endpoint(host, port):
        raise SynProtocolError("LAN enrollment advertisement differs from the selected endpoint")
    pulse = verify_enrollment_pulse(body["pulse"], expected_operator, network_id, now)
    public = hex_bytes(pulse["signed_pulse"]["operator_public_key"], 32, "operator public key")
    verify_ed25519(
        public,
        hex_bytes(packet["signature"], 64, "advertisement signature"),
        ADVERTISEMENT_DOMAIN + canonical(body),
    )
    return body
