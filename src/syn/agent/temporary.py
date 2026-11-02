"""Canonical Syn temporary OS-random identity; reconciled from Phase 47.
Uses the current noninteractive native backend and host-wide authority lock.
Never a fallback for the assigned extended key or a hardware identifier.
"""
from __future__ import annotations
import hashlib
from dataclasses import dataclass
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from syn.operator.custody import _native_backend, _entry, _agent_key_lock
from syn_client.operator_protocol import SynProtocolError, hex_bytes

def _raw_public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )



@dataclass(frozen=True, slots=True)
class AgentTemporaryOsSigner:
    """Existing Ed25519 private seed held by the platform credential store.

    This seed-based helper is ONLY suitable for temporary agent keys. It does
    NOT reconstruct an Ed25519-BIP32 V2 extended private key. The operator's
    base key at m/6969'/420'/0'/0/0 must use the qualified native HD signer.
    """

    service: str
    account: str
    expected_public_hash: bytes

    def __post_init__(self) -> None:
        _entry(self.service, self.account)
        if type(self.expected_public_hash) is not bytes or len(self.expected_public_hash) != 32:
            raise SynProtocolError("SYN signer must be pinned to a 32-byte public-key hash")
        _native_backend()
        if hashlib.sha256(self.public_key).digest() != self.expected_public_hash:
            raise SynProtocolError("SYN keystore key differs from pinned installation identity")

    def _private(self) -> Ed25519PrivateKey:
        backend = _native_backend()
        try:
            encoded = backend.get_password(self.service, self.account)
        except Exception as exc:
            raise SynProtocolError("SYN OS credential store is locked or unavailable") from exc
        if encoded is None:
            raise SynProtocolError("SYN OS credential is absent; no fallback key")
        private = Ed25519PrivateKey.from_private_bytes(hex_bytes(encoded, 32, "keystore key"))
        return private

    @property
    def public_key(self) -> bytes:
        return _raw_public(self._private())

    def sign(self, payload: bytes) -> bytes:
        if type(payload) is not bytes or not 0 < len(payload) <= 65_536:
            raise SynProtocolError("invalid bounded SYN signing request")
        private = self._private()
        if hashlib.sha256(_raw_public(private)).digest() != self.expected_public_hash:
            raise SynProtocolError("SYN operator signing identity rotated unexpectedly")
        return private.sign(payload)



def create_agent_temporary_key(*, service: str, account: str, lock_dir: Path) -> dict[str, str]:
    """One explicit agent-mode SYN temporary key from OS CSPRNG.

    This is NOT a base/root CIP-3 key, and its hash is never treated as the
    installation's durable post-adoption address. Only SYN calls this function.
    """
    _entry(service, account)
    # OS credential stores generally do not implement compare-and-swap. The
    # installation lock serializes same-account initialization across SYN
    # processes, never exposing key material to Potbot.
    with _agent_key_lock(lock_dir, service, account):
        backend = _native_backend()
        try:
            existing = backend.get_password(service, account)
        except Exception as exc:
            raise SynProtocolError(
                "SYN keystore cannot confirm whether key already exists"
            ) from exc
        if existing is not None:
            raise SynProtocolError("SYN temporary key already exists; refuse replacement")
        private = Ed25519PrivateKey.generate()  # OS CSPRNG, not a hardware identifier
        secret = private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ).hex()
        try:
            backend.set_password(service, account, secret)
            verified = backend.get_password(service, account)
        except Exception as exc:
            raise SynProtocolError("SYN OS keystore did not confirm temporary-key custody") from exc
        if verified != secret:
            raise SynProtocolError("SYN OS keystore readback differed; enrollment is blocked")
        pub = _raw_public(private)
        return {
            "schema": "syn-agent-temporary-public-identity/v1",
            "temporary_public_key": pub.hex(),
            "temporary_public_key_hash": hashlib.sha256(pub).hexdigest(),
            "custody": "native-os-credential-store",
            "agent_adopted": "false",
        }
