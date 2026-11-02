"""Operator-only maintenance authority; transport/adoption remains a later gate.

These operations require exclusive authority ownership. They cannot overlap a
running operator. Reservation requires an explicit private terminal decision.
No raw hardware ID or private descendant is written to the allocation journal.
"""
from __future__ import annotations

import hashlib
import secrets
import time

from syn_client.custody import verify_ed25519
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes
from .custody import _agent_key_lock
from .service import OperatorServer

PULSE_DOMAIN = b"syn.operator.pulse.v1\0"
OFFER_DOMAIN = b"syn.agent.pledge.offer.v2\0"
CANDIDATE_DOMAIN = b"syn.agent.candidates.v2\0"


def pulse(config_path):
    server = OperatorServer(config_path)
    with _agent_key_lock(server.directory, server.signer.service, server.signer.account + ".operator"):
        server.state.read(recover=True)
        now = int(time.time())
        body = {"schema_version": 1, "type_ref": "synny://type/syn-operator-pulse-v1",
                "network_id": server.config["network_id"], "master_public_key_hash": server.expected_hash,
                "challenge": secrets.token_hex(32), "issued_at": now, "expires_at": now + 120}
        return {"pulse": body, "operator_public_key": server.signer.public_key.hex(),
                "signature": server.signer.sign(PULSE_DOMAIN + canonical(body)).hex()}


def _verified_offer(server, signed_pulse, signed_offer, now):
    envelope = exact(signed_pulse, {"pulse", "operator_public_key", "signature"}, "signed pulse")
    body = exact(envelope["pulse"], {"schema_version", "type_ref", "network_id", "master_public_key_hash",
                 "challenge", "issued_at", "expires_at"}, "pulse")
    if (type(body["schema_version"]) is not int or body["schema_version"] != 1
            or body["type_ref"] != "synny://type/syn-operator-pulse-v1"
            or body["network_id"] != server.config["network_id"]
            or body["master_public_key_hash"] != server.expected_hash
            or envelope["operator_public_key"] != server.signer.public_key.hex()
            or type(body["issued_at"]) is not int or type(body["expires_at"]) is not int
            or not 0 < body["issued_at"] <= now <= body["expires_at"]
            or not 1 <= body["expires_at"] - body["issued_at"] <= 300):
        raise SynProtocolError("Pulse is stale or belongs to another operator/network")
    if hex_bytes(body["challenge"], 32, "pulse challenge") == bytes(32):
        raise SynProtocolError("Pulse challenge cannot be zero")
    verify_ed25519(server.signer.public_key, hex_bytes(envelope["signature"], 64, "pulse signature"),
                   PULSE_DOMAIN + canonical(body))
    pledge = exact(signed_offer, {"offer", "temporary_public_key", "signature"}, "signed pledge")
    offer = exact(pledge["offer"], {"schema_version", "type_ref", "temporary_public_key_hash", "hardware_unique_id",
                  "timestamp_unix_seconds", "target_master_public_key_hash", "pulse_challenge"}, "pledge")
    temporary = hex_bytes(pledge["temporary_public_key"], 32, "temporary public key")
    if (type(offer["schema_version"]) is not int or offer["schema_version"] != 2
            or offer["type_ref"] != "synny://type/syn-agent-pledge-offer-v2"
            or offer["temporary_public_key_hash"] != hashlib.sha256(temporary).hexdigest()
            or offer["target_master_public_key_hash"] != server.expected_hash
            or offer["pulse_challenge"] != body["challenge"]
            or type(offer["timestamp_unix_seconds"]) is not int
            or not body["issued_at"] <= offer["timestamp_unix_seconds"] <= min(now, body["expires_at"])):
        raise SynProtocolError("Pledge is not fresh and bound to this authenticated pulse")
    hardware = offer["hardware_unique_id"]
    if (not isinstance(hardware, str) or len(hardware) % 2 or not 12 <= len(hardware) <= 512
            or any(ch not in "0123456789abcdef" for ch in hardware)):
        raise SynProtocolError("Pledge hardware identifier is not canonical bounded bytes")
    transcript = OFFER_DOMAIN + canonical(offer)
    verify_ed25519(temporary, hex_bytes(pledge["signature"], 64, "pledge signature"), transcript)
    return bytes.fromhex(hardware), hashlib.sha256(transcript).hexdigest(), hashlib.sha256(PULSE_DOMAIN + canonical(body)).hexdigest()


def reserve(config_path, signed_pulse, signed_offer, operator_byte, *, approve):
    if type(operator_byte) is not int or not 0 <= operator_byte <= 255:
        raise SynProtocolError("Operator choice must be one unsigned byte")
    server = OperatorServer(config_path)
    with _agent_key_lock(server.directory, server.signer.service, server.signer.account + ".operator"):
        current = server.state.read(recover=True)
        now = int(time.time())
        hardware, offer_digest, pulse_digest = _verified_offer(server, signed_pulse, signed_offer, now)
        inheritance = int.from_bytes(hashlib.sha256(hardware + bytes([operator_byte])).digest()[:3], "big")
        device = server.signer.device_commitment(hardware)
        for record in current["reservations"]:
            if (record["inheritance"] == inheritance or record["device_commitment"] == device
                    or record["pledge_offer_sha256"] == offer_digest or record["pulse_sha256"] == pulse_digest):
                raise SynProtocolError("Device, pledge, pulse or inheritance is already reserved")
        hashes = server.signer.derive_candidate_public_hashes(inheritance)
        candidates = {"schema_version": 2, "type_ref": "synny://type/syn-agent-identity-candidates-v2",
                      "pledge_offer_sha256": offer_digest, "target_master_public_key_hash": server.expected_hash,
                      "operator_byte": operator_byte, "inheritance": inheritance, "inheritance_hex": f"{inheritance:06x}",
                      "short_public_key_hashes": [value[:3].hex() for value in hashes]}
        decision = {"operator": server.expected_hash, "pledge_offer_sha256": offer_digest,
                    "inheritance_hex": f"{inheritance:06x}", "hardware_provenance_verified": False,
                    "agent_activation_authorized": False}
        if approve(decision) is not True:
            raise SynProtocolError("Explicit operator reservation permission was not granted")
        # Human decisions can take time; recheck freshness before durable commit.
        _verified_offer(server, signed_pulse, signed_offer, int(time.time()))
        record = {"pledge_offer_sha256": offer_digest, "pulse_sha256": pulse_digest, "device_commitment": device,
                  "inheritance": inheritance, "reserved_at": now,
                  "candidates_sha256": hashlib.sha256(CANDIDATE_DOMAIN + canonical(candidates)).hexdigest()}
        server.state.commit({**current, "reservations": current["reservations"] + [record]})
        # Release the signature only AFTER the anchor and journal commit.
        return {"candidates": candidates, "operator_public_key": server.signer.public_key.hex(),
                "signature": server.signer.sign(CANDIDATE_DOMAIN + canonical(candidates)).hex()}


def terminal_approval(decision):
    from .unlock import _private_terminal
    with _private_terminal() as terminal:
        terminal.write("Reserve public candidates only. Hardware provenance is not qualified; no agent activation.\n")
        terminal.write("Operator: " + decision["operator"] + "\n")
        phrase = "RESERVE " + decision["pledge_offer_sha256"][:12] + " " + decision["inheritance_hex"]
        terminal.write("Type " + phrase + " to approve: ")
        terminal.flush()
        return terminal.readline(128).strip() == phrase
