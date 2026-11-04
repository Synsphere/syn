"""Separate current-epoch group custody; never transfer derivation authority.

The group key uses a versioned HKDF domain independent of HD account keys.
Initial custody and epoch rollover both keep the group secret inside Syn.
"""
from __future__ import annotations

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from syn.operator.custody import _agent_key_lock, _native_backend
from syn.operator.hd_keys import _load_operator
from syn_client.custody import verify_ed25519
from syn_client.group import CERT_DOMAIN, COMMIT_DOMAIN, group_id, group_epoch, verify_group
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes
from .adoption import _read_keyring, _verify_grant, _verify_request, _x25519_public

DERIVE_DOMAIN = b"syn.private-group.hkdf-sha256.v1\0"
TRANSFER_DOMAIN = b"syn.agent.group-transfer.v1\0"
SEAL_DOMAIN = b"syn.agent.group-seal.v1\0"


def _derive_group_key(signer, network_id, identifier, epoch):
    group_id(identifier)
    group_epoch(epoch)
    values, identity = _load_operator(signer.service, signer.account)
    if identity["base_public_key_hash"] != signer.expected_public_hash.hex():
        raise SynProtocolError("Operator identity changed before group derivation")
    context = canonical({"operator": identity["base_public_key_hash"], "network_id": network_id,
                         "group_id": identifier, "epoch": epoch})
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=hashlib.sha256(DERIVE_DOMAIN + b"extract").digest(), info=DERIVE_DOMAIN + context).derive(values["entropy"])


def _transfer_key(private, peer, salt, header, certificate):
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(hex_bytes(peer, 32, "group transfer peer")))
    except ValueError:
        raise SynProtocolError("Group transfer peer was rejected") from None
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=TRANSFER_DOMAIN + canonical(header) + canonical(certificate)).derive(shared)


def group_certificate(server, node, identifier, epoch, key):
    body = {"profile": "syn.agent-group-certificate/1", "operator_public_key_hash": server.expected_hash,
            "node_public_key_hash": node, "network_id": server.config["network_id"],
            "group_id": identifier, "group_epoch": epoch, "group_key_commitment": hashlib.sha256(COMMIT_DOMAIN + key).hexdigest()}
    return {"body": body, "operator_public_key": server.signer.public_key.hex(),
                   "signature": server.signer.sign(CERT_DOMAIN + canonical(body)).hex()}


def seal_group_key(*, server, journal, grant_packet, request_packet, identifier, epoch, now, approve):
    grant, grant_hash = _verify_grant(grant_packet, server.signer.expected_public_hash, now)
    request, request_hash = _verify_request(request_packet, grant, grant_hash, now)
    journal.require_selected({key: grant[key] for key in ("offer_sha256", "master_public_key_hash", "temporary_public_key",
        "inheritance", "independence", "selected_public_key_hash", "selection_sha256")})
    group_id(identifier)
    group_epoch(epoch)
    from .group_rollover import group_history
    if group_history(journal, identifier):
        raise SynProtocolError("Initial assignment is closed after group rollover")
    offer = grant["offer_sha256"]
    if len(journal._events(offer, "confirmed")) != 1 or journal._events(offer, "groupsealed"):
        raise SynProtocolError("Group transfer requires an unused confirmed selected-key adoption")
    decision = {"operator": server.expected_hash, "selected_public_key_hash": grant["selected_public_key_hash"],
                "group_id": identifier, "group_epoch": epoch}
    if approve(decision) is not True:
        raise SynProtocolError("Explicit initial group assignment was not authorized")
    import time
    _verify_grant(grant_packet, server.signer.expected_public_hash, int(time.time()))
    # Reserve BEFORE deriving/releasing the secret; failures remain fenced.
    journal._append("groupsealed", offer, {"grant": grant_hash.hex(), "request": request_hash.hex(),
                                          "group_id": identifier, "epoch": epoch})
    key = _derive_group_key(server.signer, server.config["network_id"], identifier, epoch)
    certificate = group_certificate(server, grant["selected_public_key_hash"], identifier, epoch, key)
    sender = X25519PrivateKey.generate()
    salt, nonce = os.urandom(32), os.urandom(12)
    header = {"profile": "syn.agent-group-transfer/1", "grant_sha256": grant_hash.hex(), "request_sha256": request_hash.hex(),
              "sender_public_key": _x25519_public(sender).hex(), "salt": salt.hex(), "nonce": nonce.hex()}
    cipher = AESGCM(_transfer_key(sender, request["transfer_public_key"], salt, header, certificate)).encrypt(
        nonce, key, canonical(header) + canonical(certificate))
    return {"header": header, "certificate": certificate, "ciphertext": cipher.hex(),
            "signature": server.signer.sign(SEAL_DOMAIN + canonical(header) + canonical(certificate) + cipher).hex()}


def adopt_group_key(config, directory, *, grant_packet, request_packet, envelope, now):
    from .service import AgentSigner
    grant, grant_hash = _verify_grant(grant_packet, bytes.fromhex(config["expected_operator"]), now)
    request, request_hash = _verify_request(request_packet, grant, grant_hash, now)
    packet = exact(envelope, {"header", "certificate", "ciphertext", "signature"}, "encrypted group key")
    header = exact(packet["header"], {"profile", "grant_sha256", "request_sha256", "sender_public_key", "salt", "nonce"}, "group transfer")
    if (header["profile"] != "syn.agent-group-transfer/1" or header["grant_sha256"] != grant_hash.hex()
            or header["request_sha256"] != request_hash.hex()):
        raise SynProtocolError("Group transfer differs from this Agent's selected-key handoff")
    certificate = verify_group(packet["certificate"], expected_operator=config["expected_operator"],
        expected_node=grant["selected_public_key_hash"], expected_group=config["group_id"], expected_epoch=config["group_epoch"])
    if certificate["network_id"] != config["network_id"]:
        raise SynProtocolError("Group certificate belongs to another network")
    cipher = hex_bytes(packet["ciphertext"], 48, "encrypted group secret")
    verify_ed25519(hex_bytes(packet["certificate"]["operator_public_key"], 32, "group issuer"),
        hex_bytes(packet["signature"], 64, "group seal signature"), SEAL_DOMAIN + canonical(header) + canonical(packet["certificate"]) + cipher)
    with _agent_key_lock(directory, config["keyring_service"], config["keyring_account"] + ".adoption"):
        backend = _native_backend()
        slot = config["keyring_account"] + ".agent.group-key.v1"
        marker = config["keyring_account"] + ".agent.group-certificate.v1"
        if backend.get_password(config["keyring_service"], slot) is not None or backend.get_password(config["keyring_service"], marker) is not None:
            raise SynProtocolError("Group custody exists or is incomplete; no automatic epoch replacement")
        signer = AgentSigner(config, grant["selected_public_key_hash"])
        signer.sign(TRANSFER_DOMAIN + grant_hash)  # actual retained leaf, not a caller claim
        encoded = _read_keyring(backend, config["keyring_service"], config["keyring_account"], "transfer_private")
        recipient = X25519PrivateKey.from_private_bytes(hex_bytes(encoded, 32, "native group receiver"))
        if _x25519_public(recipient).hex() != request["transfer_public_key"]:
            raise SynProtocolError("Group receiver differs from retained selected-key request")
        try:
            secret = AESGCM(_transfer_key(recipient, header["sender_public_key"], hex_bytes(header["salt"], 32, "group salt"),
                header, packet["certificate"])).decrypt(hex_bytes(header["nonce"], 12, "group nonce"), cipher,
                                                      canonical(header) + canonical(packet["certificate"]))
        except InvalidTag:
            raise SynProtocolError("Group envelope could not be authenticated") from None
        if len(secret) != 32 or hashlib.sha256(COMMIT_DOMAIN + secret).hexdigest() != certificate["group_key_commitment"]:
            raise SynProtocolError("Group key differs from operator certificate")
        encoded = secret.hex()
        backend.set_password(config["keyring_service"], slot, encoded)
        if backend.get_password(config["keyring_service"], slot) != encoded:
            raise SynProtocolError("Native group key readback failed")
        encoded = canonical(packet["certificate"]).decode("ascii")
        backend.set_password(config["keyring_service"], marker, encoded)
        if backend.get_password(config["keyring_service"], marker) != encoded:
            raise SynProtocolError("Native group marker readback failed")
        return {"profile": "syn.agent-group-adopted/1", "group_id": certificate["group_id"],
                "group_epoch": certificate["group_epoch"], "cog_execution_authorized": False}


def load_group_custody(config, expected_node):
    from .group_rollover import read_rollover

    backend = _native_backend()
    record, policy = read_rollover(backend, config)
    if policy is None:
        certificate = record
        secret = backend.get_password(config["keyring_service"], config["keyring_account"] + ".agent.group-key.v1")
        epoch, members = config["group_epoch"], None
    else:
        if expected_node not in policy["retained"]:
            raise SynProtocolError("Selected Agent was revoked from this group")
        certificate, secret = record["certificate"], record["secret"]
        epoch, members = policy["epoch"], policy["retained"]
    body = verify_group(certificate, expected_operator=config["expected_operator"], expected_node=expected_node,
                        expected_group=config["group_id"], expected_epoch=epoch)
    key = hex_bytes(secret, 32, "retained group secret")
    if (body["network_id"] != config["network_id"]
            or hashlib.sha256(COMMIT_DOMAIN + key).hexdigest() != body["group_key_commitment"]):
        raise SynProtocolError("Current group secret differs from its group or epoch certificate")
    return certificate, key, members


def load_group(config, expected_node):
    return load_group_custody(config, expected_node)[0]
