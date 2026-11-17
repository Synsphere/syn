"""Host-only, bounded root-key operations for Syn's Icarus/Ed25519-BIP32 v2.

The root seed is sealed in the Windows user's DPAPI vault; never returned to WSL.
The host may return a *group epoch key* for Syn's existing X25519 group-transfer
contract, which is explicitly weaker than a completely isolated HSM protocol.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from uuid import UUID
from typing import Callable

ROOT_PATH = "m/6969'/420'"
BASE_PATH = "m/6969'/420'/0'/0/0"
PROFILE = "syn-icarus-ed25519-bip32-v2"
PUBLIC_TYPE = "synny://type/syn-operator-public-identity-v1"
DERIVE_DOMAIN = b"syn.private-group.hkdf-sha256.v1\0"
MAX_REQUEST = 140_000

# A caller never receives generic/raw signing. All currently supported operator
# transcripts have a domain prefix from the installed version of Syn.
SIGN_DOMAINS = (
    b"syn.operator-status/1\0",
    b"syn.operator.dag.checkpoint/1\0",
    b"syn.operator.pulse.v1\0",
    b"syn.operator.confidential-enrollment.v1\0",
    b"syn.agent.candidates.v2\0",
    b"syn.agent.group-certificate.v1\0",
    b"syn.agent.group-seal.v1\0",
    b"syn.agent.group-rollover.v1\0",
    b"syn.lan-enrollment-advertisement.v1\0",
    b"syn.agent.independence.v2\0",
    b"syn.agent.inheritance.grant.v1\0",
    b"syn.lan-transport-receipt.v1\0",
    b"syn.lan-transport-proof.v1\0",
    b"syn.local-rpc-receipt/1\0",
)
ROOT_SUFFIXES = frozenset((
    "entropy", "root_xprivate", "root_public", "root_chain", "base_xprivate",
    "base_public", "base_chain", "manifest",
))


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def safe_ref(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", value) is not None


def safe_slot(value):
    return (isinstance(value, str) and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}", value) is not None)


def root_slot(slot):
    """Never expose any historical HD slots via a generic keystore RPC."""
    return (isinstance(slot, str) and
            (re.search(r"\.operator\.(?:" + "|".join(ROOT_SUFFIXES) + r")\.v1$", slot) is not None
             or slot.endswith(DAG_ANCHOR_SUFFIX)
             or re.search(r"\.operator\.witness-[0-9a-f]{64}\.v1$", slot) is not None))


def check_account(service, account):
    if not safe_ref(service) or not safe_ref(account):
        raise ValueError("Invalid bounded host custody account")


def public_identity(entropy):
    from pycardano.crypto.bip32 import HDWallet
    if not isinstance(entropy, bytes) or len(entropy) != 32:
        raise ValueError("Operator seed must contain exactly 256 bits")
    wallet = HDWallet.from_entropy(entropy.hex(), passphrase="")
    root = bytes(wallet.derive_from_path(ROOT_PATH).public_key)
    base = bytes(wallet.derive_from_path(BASE_PATH).public_key)
    if len(root) != 32 or len(base) != 32 or root == base:
        raise ValueError("HD provider returned invalid root/base identities")
    return {"schema_version": 1, "type_ref": PUBLIC_TYPE, "profile": PROFILE,
            "root_path": ROOT_PATH, "base_path": BASE_PATH,
            "root_public_key": root.hex(), "base_public_key": base.hex(),
            "root_public_key_hash": hashlib.sha256(root).hexdigest(),
            "base_public_key_hash": hashlib.sha256(base).hexdigest()}


def sign_operator(entropy, payload):
    from pycardano.crypto.bip32 import HDWallet
    from pycardano.key import ExtendedSigningKey
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(payload, bytes) or not 0 < len(payload) <= 65_536:
        raise ValueError("Signing payload must be bounded nonempty bytes")
    if not any(payload.startswith(domain) for domain in SIGN_DOMAINS):
        raise ValueError("Arbitrary signing is forbidden; unrecognized Syn transcript domain")
    base = HDWallet.from_entropy(entropy.hex(), passphrase="").derive_from_path(BASE_PATH)
    public = bytes(base.public_key)
    signing = ExtendedSigningKey(bytes(base.xprivate_key) + public + bytes(base.chain_code))
    signature = signing.sign(payload)
    Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
    return signature


def candidate_hashes(entropy, inheritance):
    from pycardano.crypto.bip32 import HDWallet
    if type(inheritance) is not int or not 0 <= inheritance <= 0xFFFFFF:
        raise ValueError("Inheritance must be a uint24")
    wallet = HDWallet.from_entropy(entropy.hex(), passphrase="")
    hashes = [hashlib.sha256(bytes(wallet.derive_from_path(
        f"{ROOT_PATH}/{inheritance}'/{n}/0").public_key)).digest() for n in range(1, 9)]
    if len({h[:3] for h in hashes}) != 8:
        raise ValueError("Candidate short hashes collide")
    return [h.hex() for h in hashes]


def device_commitment(entropy, raw):
    if not isinstance(raw, bytes) or not 6 <= len(raw) <= 256:
        raise ValueError("Hardware identifier must be 6..256 bytes")
    return hmac.new(entropy, b"syn.operator.device.v1\0"+raw, hashlib.sha256).hexdigest()


def group_key(entropy, operator_hash, network_id, group_id, epoch):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    if not isinstance(operator_hash, str) or re.fullmatch("[0-9a-f]{64}", operator_hash) is None:
        raise ValueError("Invalid operator pin")
    if not isinstance(network_id, str) or re.fullmatch("[a-z][a-z0-9-]{0,63}", network_id) is None:
        raise ValueError("Invalid network")
    try:
        valid_group = (type(group_id) is str and str(UUID(group_id)) == group_id
                       and UUID(group_id).int != 0)
    except (TypeError, ValueError):
        valid_group = False
    if not valid_group:
        raise ValueError("Invalid canonical group UUID")
    if type(epoch) is not int or not 1 <= epoch < 2**31:
        raise ValueError("Invalid group epoch")
    context = canonical({"operator": operator_hash, "network_id": network_id,
                         "group_id": group_id, "epoch": epoch})
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=hashlib.sha256(DERIVE_DOMAIN+b"extract").digest(),
                info=DERIVE_DOMAIN+context).derive(entropy)


DAG_ANCHOR_DOMAIN = b"syn.operator.dag.checkpoint/1\0"
DAG_ANCHOR_SUFFIX = ".operator.dag-anchor.v1"


def verified_dag_checkpoint(packet, expected_pin):
    """Verify a public-only DAG checkpoint without trusting WSL or PostgreSQL."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(packet, dict) or set(packet) != {"body", "public_key", "signature"}:
        raise ValueError("Invalid host anchor checkpoint envelope")
    body = packet["body"]
    if not isinstance(body, dict) or set(body) != {
            "profile", "operator", "network_id", "sequence", "root", "previous_checkpoint"}:
        raise ValueError("Invalid host anchor checkpoint body")
    def hex64(v):
        return isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) is not None
    if (body["profile"] != "syn.operator-dag-checkpoint/1"
            or body["operator"] != expected_pin or not hex64(body["root"])
            or (body["previous_checkpoint"] is not None and not hex64(body["previous_checkpoint"]))
            or type(body["sequence"]) is not int or not 1 <= body["sequence"] < 2**63
            or not isinstance(body["network_id"], str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", body["network_id"]) is None):
        raise ValueError("Invalid signed DAG checkpoint scope")
    public_hex, signature_hex = packet["public_key"], packet["signature"]
    if (not hex64(public_hex) or not isinstance(signature_hex, str)
            or re.fullmatch(r"[0-9a-f]{128}", signature_hex) is None):
        raise ValueError("Invalid DAG checkpoint cryptographic fields")
    public = bytes.fromhex(public_hex)
    if hashlib.sha256(public).hexdigest() != expected_pin:
        raise ValueError("DAG checkpoint differs from Operator identity")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(
            bytes.fromhex(signature_hex), DAG_ANCHOR_DOMAIN + canonical(body))
    except (InvalidSignature, ValueError) as error:
        raise ValueError("DAG checkpoint signature is invalid") from error
    commitment = hashlib.sha256(DAG_ANCHOR_DOMAIN + canonical(body)
                                + bytes.fromhex(signature_hex)).hexdigest()
    return body, commitment


class Authority:
    """Pure request dispatcher; store supplies transactional DPAPI-backed records."""

    def __init__(self, store, *, approve: Callable[[str], bool] | None = None):
        self.store = store
        self.approve = approve or (lambda _: False)

    def request(self, req):
        if not isinstance(req, dict) or len(canonical(req)) > MAX_REQUEST:
            raise ValueError("Malformed or oversized host custody request")
        op = req.get("op")
        if op == "health":
            return {"profile": "syn.host-custody/1", "ready": True}
        service, account = req.get("service"), req.get("account")
        check_account(service, account)
        identity_ref = service+"/"+account
        if op == "get":
            slot = req.get("slot")
            if not safe_slot(slot) or root_slot(slot):
                raise ValueError("Root HD material is non-exportable through the broker")
            return self.store.get_slot(service, slot)
        if op == "set":
            slot, value = req.get("slot"), req.get("value")
            if not safe_slot(slot) or root_slot(slot) or not isinstance(value, str) or len(value) > 131_072:
                raise ValueError("Invalid or protected host custody slot")
            self.store.set_slot(service, slot, value)
            return True
        if op == "create":
            if service != "syn.operator":
                raise ValueError("Only Syn Operator authorities may be created")
            if self.store.has_identity(identity_ref):
                raise ValueError("Operator identity already exists; refusing replacement")
            if not self.approve("Create a new Windows-protected Syn Operator identity?"):
                raise ValueError("Windows host operator creation was declined")
            seed = os.urandom(32)
            public = public_identity(seed)
            self.store.create_identity(identity_ref, seed, public)
            return public
        seed, identity = self.store.identity(identity_ref)
        pin = req.get("pin")
        if not isinstance(pin, str) or not hmac.compare_digest(identity["base_public_key_hash"], pin):
            raise ValueError("Operator identity does not match the pinned hash")
        if op == "dag_anchor":
            # The generic get/set RPCs cannot read or rewrite this monotonic
            # host-only anchor. The broker is single-threaded under a vault lock.
            slot = account + DAG_ANCHOR_SUFFIX
            action = req.get("action")
            saved = self.store.get_slot(service, slot)
            previous = json.loads(saved) if saved is not None else None
            if previous is not None:
                old_body, old_commitment = verified_dag_checkpoint(previous, pin)
            if action == "read":
                return previous
            if action != "advance":
                raise ValueError("Unknown DAG anchor action")
            packet = req.get("checkpoint")
            body, commitment = verified_dag_checkpoint(packet, pin)
            if previous is None:
                if not self.approve("Retain this Syn Operator DAG checkpoint in the Windows host vault? "
                                    "Only approve if the displayed Operator is yours: " + pin):
                    raise ValueError("First independent DAG anchor was declined")
            else:
                if packet == previous:
                    return {"sequence": body["sequence"], "digest": commitment}
                if (body["network_id"] != old_body["network_id"]
                        or body["sequence"] <= old_body["sequence"]
                        or body["previous_checkpoint"] != old_commitment):
                    raise ValueError("DAG anchor cannot rewind, fork or skip a checkpoint")
                from .merkle import verify_prefix
                if not verify_prefix(old_body["sequence"], body["sequence"], old_body["root"],
                                     body["root"], req.get("proof")):
                    raise ValueError("DAG anchor Merkle prefix proof failed")
            if previous is None and req.get("proof", []) != []:
                raise ValueError("First DAG anchor cannot supply an unrelated proof")
            self.store.set_slot(service, slot, canonical(packet).decode("ascii"))
            return {"sequence": body["sequence"], "digest": commitment}
        if op == "dag_witness_anchor":
            # Archiving public peer evidence does not grant signing authority or
            # disclose secrets. Peer trust is explicitly pinned in local config.
            # Each remote identity has its own protected monotonic slot.
            peer, network = req.get("peer"), req.get("network_id")
            if (not isinstance(peer, str) or re.fullmatch(r"[0-9a-f]{64}", peer) is None
                    or peer == pin or not isinstance(network, str)
                    or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", network) is None):
                raise ValueError("Invalid independently pinned witness peer")
            slot = account + ".operator.witness-" + peer + ".v1"
            saved = self.store.get_slot(service, slot)
            previous = json.loads(saved) if saved is not None else None
            if previous is not None:
                old_body, _ = verified_dag_checkpoint(previous, peer)
                if old_body["network_id"] != network:
                    raise ValueError("Retained witness network cannot change")
            if req.get("action") == "read":
                return previous
            if req.get("action") != "advance":
                raise ValueError("Unknown witness anchor action")
            packet = req.get("checkpoint")
            body, commitment = verified_dag_checkpoint(packet, peer)
            if body["network_id"] != network:
                raise ValueError("Witness checkpoint differs from its pinned network")
            proof = req.get("proof")
            if previous is None:
                if proof != []:
                    raise ValueError("Initial witness checkpoint has no prior proof")
            elif packet == previous:
                if proof != []:
                    raise ValueError("Same witness checkpoint requires an empty proof")
                return {"sequence": body["sequence"], "digest": commitment}
            else:
                from .merkle import verify_prefix
                if (body["sequence"] <= old_body["sequence"]
                        or not verify_prefix(old_body["sequence"], body["sequence"],
                                             old_body["root"], body["root"], proof)):
                    raise ValueError("Witness anchor cannot rewind or accept inconsistent history")
            self.store.set_slot(service, slot, canonical(packet).decode("ascii"))
            return {"sequence": body["sequence"], "digest": commitment}
        if op == "identity":
            return identity
        if op == "sign":
            payload = bytes.fromhex(req["payload"])
            if not any(payload.startswith(domain) for domain in SIGN_DOMAINS):
                raise ValueError("Unknown Syn signature domain")
            # Signing enrollment/grants/certificates/receipts changes trust.
            # The Windows owner must authorize all such signatures.
            if not payload.startswith((b"syn.operator-status/1\0", b"syn.operator.pulse.v1\0")):
                label = payload.split(b"\0", 1)[0].decode("ascii", errors="replace")
                if not self.approve("Authorize Syn Operator signature for " + label + "?"):
                    raise ValueError("Host Operator signature approval declined")
            return sign_operator(seed, payload).hex()
        if op == "candidates":
            return candidate_hashes(seed, req.get("inheritance"))
        if op == "device_commitment":
            return device_commitment(seed, bytes.fromhex(req["hardware"]))
        if op == "group_key":
            network, group, epoch = req.get("network_id"), req.get("group_id"), req.get("epoch")
            key = group_key(seed, pin, network, group, epoch)
            if not self.store.is_approved(identity_ref, network, group, epoch):
                if not self.approve(f"Release Syn group epoch key for {network}/{group} epoch {epoch}?"):
                    raise ValueError("Windows group key approval was declined")
                self.store.approve_group(identity_ref, network, group, epoch)
            return key.hex()
        raise ValueError("Unknown host custody operation")
