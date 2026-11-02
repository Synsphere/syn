"""Public selected-key receipt verification, reconciled from Phase 47.
Two signatures establish assignment and key possession, not physical attestation.
No private-key storage, generation or signing exists in this client module.
"""
from __future__ import annotations
import hashlib
from .consensus import PolicyError
from .custody import verify_ed25519
from .operator_protocol import SynProtocolError, canonical, exact, hex_bytes
PROOF_DOMAIN = b"syn.agent.selected-key-proof.v1\x00"
RECEIPT_DOMAIN = b"syn.operator.agent-adoption.v1\x00"
PROOF_TYPE = "synny://type/syn-agent-selected-key-proof-v1"
RECEIPT_TYPE = "synny://type/syn-agent-adoption-receipt-v1"

def _int(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SynProtocolError(f"invalid SYN {label}")
    return value



def _verify(public_key: bytes, signature: bytes, signed: bytes) -> None:
    try:
        verify_ed25519(public_key, signature, signed)
    except PolicyError as exc:
        raise SynProtocolError("SYN adoption signature invalid") from exc



def verify_adoption(record: object, *, expected_master_hash: bytes, now: int) -> str:
    """Independently check Phase 42 public custody CLAIM against two signatures.

    This authenticates an operator's statement and a chosen-key signature, not
    third-party hardware attestation or independent control of the child key.
    """
    if type(expected_master_hash) is not bytes or len(expected_master_hash) != 32:
        raise SynProtocolError("SYN receipt verifier needs a pinned operator hash")
    outer = exact(
        record, {"receipt", "key_proof", "operator_public_key", "operator_signature"}, "adoption"
    )
    signed_proof = exact(outer["key_proof"], {"proof", "signature"}, "key proof")
    proof = exact(
        signed_proof["proof"],
        {
            "schema_version",
            "type_ref",
            "offer_sha256",
            "master_public_key_hash",
            "inheritance",
            "independence",
            "selected_public_key",
            "challenge",
            "issued_at",
            "expires_at",
        },
        "selected-key proof",
    )
    receipt = exact(
        outer["receipt"],
        {
            "schema_version",
            "type_ref",
            "offer_sha256",
            "master_public_key_hash",
            "inheritance",
            "independence",
            "selected_public_key_hash",
            "proof_sha256",
            "adopted_at",
            "agent_installation_mode",
            "custody_claim",
        },
        "adoption receipt",
    )
    if (
        type(proof["schema_version"]) is not int
        or proof["schema_version"] != 1
        or proof["type_ref"] != PROOF_TYPE
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["type_ref"] != RECEIPT_TYPE
    ):
        raise SynProtocolError("SYN adoption type revision not supported")
    offer = hex_bytes(receipt["offer_sha256"], 32, "offer hash")
    if not offer or not any(offer):
        raise SynProtocolError("SYN adoption offer digest cannot be zero")
    hash_ = hex_bytes(receipt["master_public_key_hash"], 32, "master hash")
    pub = hex_bytes(outer["operator_public_key"], 32, "operator public key")
    if hash_ != expected_master_hash or hashlib.sha256(pub).digest() != expected_master_hash:
        raise SynProtocolError("SYN adoption receipt belongs to an unpinned operator")
    if (
        proof["offer_sha256"] != receipt["offer_sha256"]
        or proof["master_public_key_hash"] != receipt["master_public_key_hash"]
        or proof["inheritance"] != receipt["inheritance"]
        or proof["independence"] != receipt["independence"]
    ):
        raise SynProtocolError("SYN receipt and signed agent proof disagree")
    _int(receipt["inheritance"], 0, 16_777_215, "inheritance")
    _int(receipt["independence"], 1, 8, "independence")
    chosen = hex_bytes(proof["selected_public_key"], 32, "chosen key")
    if hashlib.sha256(chosen).digest() == expected_master_hash:
        raise SynProtocolError("An Agent assignment cannot reuse its operator base identity")
    if hashlib.sha256(chosen).hexdigest() != receipt["selected_public_key_hash"]:
        raise SynProtocolError("SYN selected public key does not match adopted identity")
    sig = hex_bytes(signed_proof["signature"], 64, "proof signature")
    issued = _int(proof["issued_at"], 1, 2**63 - 1, "issued time")
    expires = _int(proof["expires_at"], 1, 2**63 - 1, "expiry")
    adopted = _int(receipt["adopted_at"], 1, 2**63 - 1, "adoption time")
    if (
        not 1 <= expires - issued <= 300
        or type(now) is not int
        or not issued <= now <= expires
        or not issued <= adopted <= expires
    ):
        raise SynProtocolError("SYN selected-key proof outside its accepted validity window")
    if hex_bytes(proof["challenge"], 32, "adoption challenge") == bytes(32):
        raise SynProtocolError("SYN adoption challenge cannot be zero")
    preimage = PROOF_DOMAIN + canonical(proof)
    _verify(chosen, sig, preimage)
    if hashlib.sha256(preimage + sig).hexdigest() != receipt["proof_sha256"]:
        raise SynProtocolError("SYN operator receipt does not bind the signed key proof")
    if (
        receipt["agent_installation_mode"] != "agent"
        or receipt["custody_claim"] != "syn-os-keystore"
    ):
        raise SynProtocolError("SYN receipt does not declare separate agent custody")
    _verify(
        pub,
        hex_bytes(outer["operator_signature"], 64, "operator signature"),
        RECEIPT_DOMAIN + canonical(receipt),
    )
    return hashlib.sha256(
        RECEIPT_DOMAIN
        + canonical(receipt)
        + hex_bytes(outer["operator_signature"], 64, "signature")
    ).hexdigest()


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    import sys
    from .operator_protocol import parse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-operator", required=True)
    parser.add_argument("--historical-assignment", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        with args.receipt.open("rb") as source:
            value = parse(source.read(65537))
        verify_adoption(value, expected_master_hash=hex_bytes(args.expected_operator, 32, "independent operator pin"),
                        now=value["receipt"]["adopted_at"])
        result = {"profile": "syn.verified-adoption/1", "operator_public_key_hash": args.expected_operator,
                  "selected_public_key_hash": value["receipt"]["selected_public_key_hash"],
                  "receipt_sha256": hashlib.sha256(canonical(value)).hexdigest(),
                  "verification": "historical-assignment-and-selected-key-proof"}
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print("Syn assignment verification refused", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
