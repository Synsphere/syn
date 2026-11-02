"""Confidential operator-directed enrollment and Agent-owned independence choice.

Frames can be carried by the installer; no raw hardware ID or private key is
returned. This enrollment courier is not an Agent-to-Agent messaging channel.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from syn.operator.custody import _agent_key_lock, _native_backend
from syn.operator.enrollment import CANDIDATE_DOMAIN, OFFER_DOMAIN, _verified_offer, pulse, reserve
from syn.operator.adoption_state import SELECTION_DOMAIN, EnrollmentJournal
from syn.operator.service import OperatorServer
from syn_client.custody import verify_ed25519
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes, parse
from .adoption import _verify_grant, prepare_agent_request
from .device import PROFILE, device_id, temporary_signer

PULSE_DOMAIN = b"syn.operator.confidential-enrollment.v1\0"
SEAL_PROFILE = "syn.confidential-pledge/1"
KDF_DOMAIN = b"syn.confidential-pledge.aes256gcm.v1\0"


def _public(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _secret(key):
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def _key(private, peer, salt, digest):
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(hex_bytes(peer, 32, "enrollment peer")))
    except ValueError:
        raise SynProtocolError("Enrollment key agreement rejected its peer") from None
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=KDF_DOMAIN + digest).derive(shared)


def operator_pulse(config_path):
    signed = pulse(config_path)
    server = OperatorServer(config_path)
    with _agent_key_lock(server.directory, server.signer.service, server.signer.account + ".operator"):
        journal = EnrollmentJournal(server.directory, server.signer.service, server.signer.account, server.expected_hash)
        journal.read(recover=True)
        backend = _native_backend()
        encoded = backend.get_password(server.signer.service, server.signer.account + ".operator.enrollment-receiver.v1")
        if encoded is None:
            raise SynProtocolError("Confidential enrollment receiver has not been initialized")
        key = X25519PrivateKey.from_private_bytes(hex_bytes(encoded, 32, "operator receiver"))
        body = {"profile": "syn.confidential-enrollment-pulse/1", "signed_pulse": signed,
                "encryption_public_key": _public(key).hex(), "device_profile": PROFILE}
        return {"body": body, "signature": server.signer.sign(PULSE_DOMAIN + canonical(body)).hex()}


def initialize_operator_enrollment(server):
    """Explicit maintenance initialization; never reconstruct missing anchors."""
    with _agent_key_lock(server.directory, server.signer.service, server.signer.account + ".operator"):
        server.state.read(recover=True)
        backend = _native_backend()
        slot = server.signer.account + ".operator.enrollment-receiver.v1"
        journal = EnrollmentJournal(server.directory, server.signer.service, server.signer.account, server.expected_hash)
        if backend.get_password(server.signer.service, slot) is not None:
            raise SynProtocolError("Confidential enrollment exists or is incomplete")
        journal.create()
        encoded = _secret(X25519PrivateKey.generate()).hex()
        backend.set_password(server.signer.service, slot, encoded)
        if backend.get_password(server.signer.service, slot) != encoded:
            raise SynProtocolError("Native enrollment receiver readback failed")
        return {"profile": "syn.operator-enrollment-created/1", "hardware_attested": False}


def _pulse_body(packet, expected_operator, network_id, now):
    outer = exact(packet, {"body", "signature"}, "confidential operator pulse")
    body = exact(outer["body"], {"profile", "signed_pulse", "encryption_public_key", "device_profile"}, "enrollment pulse")
    signed = exact(body["signed_pulse"], {"pulse", "operator_public_key", "signature"}, "operator pulse")
    pub = hex_bytes(signed["operator_public_key"], 32, "operator public key")
    inner = exact(signed["pulse"], {"schema_version", "type_ref", "network_id", "master_public_key_hash",
                  "challenge", "issued_at", "expires_at"}, "operator pulse fields")
    if (hashlib.sha256(pub).hexdigest() != expected_operator
            or type(inner["schema_version"]) is not int or inner["schema_version"] != 1
            or inner["type_ref"] != "synny://type/syn-operator-pulse-v1"
            or body["profile"] != "syn.confidential-enrollment-pulse/1" or body["device_profile"] != PROFILE
            or inner["master_public_key_hash"] != expected_operator or inner["network_id"] != network_id
            or type(inner["issued_at"]) is not int or type(inner["expires_at"]) is not int
            or not 0 < inner["issued_at"] <= now <= inner["expires_at"]
            or not 1 <= inner["expires_at"] - inner["issued_at"] <= 300):
        raise SynProtocolError("Confidential pulse is stale or differs from independently pinned operator")
    verify_ed25519(pub, hex_bytes(outer["signature"], 64, "enrollment pulse signature"), PULSE_DOMAIN + canonical(body))
    from syn.operator.enrollment import PULSE_DOMAIN as INNER_DOMAIN
    verify_ed25519(pub, hex_bytes(signed["signature"], 64, "pulse signature"), INNER_DOMAIN + canonical(inner))
    hex_bytes(body["encryption_public_key"], 32, "encryption public key")
    if hex_bytes(inner["challenge"], 32, "pulse nonce") == bytes(32):
        raise SynProtocolError("Enrollment pulse nonce cannot be zero")
    return body


def create_offer(config, directory, packet, *, now=None):
    at = int(time.time()) if now is None else now
    body = _pulse_body(packet, config["expected_operator"], config["network_id"], at)
    with _agent_key_lock(directory, config["keyring_service"], config["keyring_account"] + ".adoption"):
        backend = _native_backend()
        slot = config["keyring_account"] + ".agent.pledge.v1"
        if backend.get_password(config["keyring_service"], slot) is not None:
            raise SynProtocolError("Agent already pledged; no automatic re-enrollment")
        signer = temporary_signer(config)
        offer = {"schema_version": 2, "type_ref": "synny://type/syn-agent-pledge-offer-v2",
                 "hardware_unique_id": device_id(config).hex(),
                 "temporary_public_key_hash": config["temporary_public_key_hash"],
                 "timestamp_unix_seconds": at, "target_master_public_key_hash": config["expected_operator"],
                 "pulse_challenge": body["signed_pulse"]["pulse"]["challenge"]}
        pledge = {"offer": offer, "temporary_public_key": signer.public_key.hex(),
                  "signature": signer.sign(OFFER_DOMAIN + canonical(offer)).hex()}
        digest = hashlib.sha256(PULSE_DOMAIN + canonical(body)).digest()
        sender = X25519PrivateKey.generate()
        salt, nonce = os.urandom(32), os.urandom(12)
        header = {"profile": SEAL_PROFILE, "pulse_sha256": digest.hex(), "sender_public_key": _public(sender).hex(),
                  "salt": salt.hex(), "nonce": nonce.hex()}
        encrypted = AESGCM(_key(sender, body["encryption_public_key"], salt, digest)).encrypt(
            nonce, canonical(pledge), canonical(header))
        commitment = canonical({"offer_sha256": hashlib.sha256(OFFER_DOMAIN + canonical(offer)).hexdigest()}).decode("ascii")
        backend.set_password(config["keyring_service"], slot, commitment)
        if backend.get_password(config["keyring_service"], slot) != commitment:
            raise SynProtocolError("Native pledge commitment readback failed")
        return {"pulse": packet, "header": header, "ciphertext": encrypted.hex()}


def open_offer(server, packet, *, now):
    outer = exact(packet, {"pulse", "header", "ciphertext"}, "confidential pledge")
    body = _pulse_body(outer["pulse"], server.expected_hash, server.config["network_id"], now)
    header = exact(outer["header"], {"profile", "pulse_sha256", "sender_public_key", "salt", "nonce"}, "pledge header")
    digest = hashlib.sha256(PULSE_DOMAIN + canonical(body)).digest()
    if header["profile"] != SEAL_PROFILE or header["pulse_sha256"] != digest.hex():
        raise SynProtocolError("Encrypted pledge changes its authenticated pulse")
    text = outer["ciphertext"]
    if not isinstance(text, str) or not 32 <= len(text) <= 8192 or len(text) % 2:
        raise SynProtocolError("Confidential pledge exceeds its bounded profile")
    ciphertext = hex_bytes(text, len(text) // 2, "encrypted pledge")
    backend = _native_backend()
    encoded = backend.get_password(server.signer.service, server.signer.account + ".operator.enrollment-receiver.v1")
    private = X25519PrivateKey.from_private_bytes(hex_bytes(encoded, 32, "native enrollment receiver"))
    if _public(private).hex() != body["encryption_public_key"]:
        raise SynProtocolError("Enrollment receiver differs from signed pulse")
    salt, nonce = hex_bytes(header["salt"], 32, "salt"), hex_bytes(header["nonce"], 12, "nonce")
    try:
        plaintext = AESGCM(_key(private, header["sender_public_key"], salt, digest)).decrypt(nonce, ciphertext, canonical(header))
    except InvalidTag:
        raise SynProtocolError("Confidential pledge authentication failed") from None
    pledge = parse(plaintext)
    _verified_offer(server, body["signed_pulse"], pledge, now)
    return body["signed_pulse"], pledge


def reserve_offer(config_path, packet, operator_byte, *, approve):
    server = OperatorServer(config_path)
    signed_pulse, pledge = open_offer(server, packet, now=int(time.time()))
    return reserve(config_path, signed_pulse, pledge, operator_byte, approve=approve)


def choose(config, directory, signed_candidates):
    packet = exact(signed_candidates, {"candidates", "operator_public_key", "signature"}, "candidates")
    body = exact(packet["candidates"], {"schema_version", "type_ref", "pledge_offer_sha256", "target_master_public_key_hash",
                 "operator_byte", "inheritance", "inheritance_hex", "short_public_key_hashes"}, "candidate body")
    pub = hex_bytes(packet["operator_public_key"], 32, "operator public key")
    verify_ed25519(pub, hex_bytes(packet["signature"], 64, "candidate signature"), CANDIDATE_DOMAIN + canonical(body))
    byte = body["operator_byte"]
    if type(byte) is not int or not 0 <= byte <= 255:
        raise SynProtocolError("Operator inheritance choice is not an octet")
    inheritance = int.from_bytes(hashlib.sha256(device_id(config) + bytes([byte])).digest()[:3], "big")
    values = body["short_public_key_hashes"]
    if (hashlib.sha256(pub).hexdigest() != config["expected_operator"] or type(body["schema_version"]) is not int
            or body["schema_version"] != 2 or body["type_ref"] != "synny://type/syn-agent-identity-candidates-v2"
            or body["target_master_public_key_hash"] != config["expected_operator"]
            or type(body["inheritance"]) is not int or body["inheritance"] != inheritance
            or body["inheritance_hex"] != f"{inheritance:06x}"
            or not isinstance(values, list) or len(values) != 8 or len(set(values)) != 8):
        raise SynProtocolError("Candidates differ from actual pledged device and operator")
    for value in values:
        hex_bytes(value, 3, "candidate abbreviation")
    with _agent_key_lock(directory, config["keyring_service"], config["keyring_account"] + ".adoption"):
        backend = _native_backend()
        encoded = backend.get_password(config["keyring_service"], config["keyring_account"] + ".agent.pledge.v1")
        pledge = exact(parse(encoded.encode("ascii")), {"offer_sha256"}, "pledge commitment") if encoded else None
        slot = config["keyring_account"] + ".agent.choice.v1"
        if pledge is None or pledge["offer_sha256"] != body["pledge_offer_sha256"] or backend.get_password(config["keyring_service"], slot) is not None:
            raise SynProtocolError("Pledge is absent, different or already independently selected")
        index = secrets.randbelow(8) + 1
        choice = {"schema_version": 2, "type_ref": "synny://type/syn-agent-independence-selection-v2",
                  "target_master_public_key_hash": config["expected_operator"], "inheritance": inheritance,
                  "independence": index, "chosen_short_public_key_hash": values[index - 1],
                  "candidate_offer_digest": hashlib.sha256(CANDIDATE_DOMAIN + canonical(body)).hexdigest()}
        signer = temporary_signer(config)
        signature = signer.sign(SELECTION_DOMAIN + canonical(choice)).hex()
        packet = {"selection": choice, "temporary_public_key": signer.public_key.hex(), "signature": signature}
        encoded = canonical(packet).decode("ascii")
        backend.set_password(config["keyring_service"], slot, encoded)
        if backend.get_password(config["keyring_service"], slot) != encoded:
            raise SynProtocolError("Native independent-choice readback failed")
        return packet


def prepare(config, directory, grant, *, now):
    body, _ = _verify_grant(grant, bytes.fromhex(config["expected_operator"]), now)
    backend = _native_backend()
    value = backend.get_password(config["keyring_service"], config["keyring_account"] + ".agent.choice.v1")
    if value is None:
        raise SynProtocolError("Agent has no retained independent selection")
    packet = parse(value.encode("ascii"))
    choice = packet["selection"]
    digest = hashlib.sha256(SELECTION_DOMAIN + canonical(choice) + bytes.fromhex(packet["signature"])).hexdigest()
    if (body["selection_sha256"] != digest or body["inheritance"] != choice["inheritance"]
            or body["independence"] != choice["independence"]
            or body["selected_public_key_hash"][:6] != choice["chosen_short_public_key_hash"]
            or body["temporary_public_key"] != temporary_signer(config).public_key.hex()):
        raise SynProtocolError("Operator grant changes the Agent's independently retained selection")
    return prepare_agent_request(grant_packet=grant, service=config["keyring_service"], account=config["keyring_account"],
                                 lock_dir=directory, expected_master_hash=bytes.fromhex(config["expected_operator"]), now=now)
