"""Signed operator status supports explicit host custody without weakening pins."""

from __future__ import annotations

import hashlib
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from syn.release import runtimeContract
from syn_client.operator import STATUS_DOMAIN, STATUS_PROFILE, verify_status
from syn_client.operator_protocol import SynProtocolError, canonical


@pytest.mark.parametrize(
    "custody,accepted",
    [
        ("linux-secret-service", True),
        ("macos-keychain", True),
        ("windows-dpapi-host", True),
        ("fake-untrusted-keystore", False),
    ],
)
def test_signed_operator_status_explicit_custody_allowlist(custody, accepted):
    signer = Ed25519PrivateKey.generate()
    public = signer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    now = int(time.time())
    challenge = "ab" * 32
    config_hash = "bc" * 32
    runtime = runtimeContract()
    body = {
        "profile": STATUS_PROFILE,
        "challenge": challenge,
        "base_public_key": public.hex(),
        "mode": "operator",
        "runtime": runtime.to_dict(),
        "configuration_sha256": config_hash,
        "pid": 1234,
        "boot_id": "cd" * 16,
        "issued_at": now,
        "state_sha256": "de" * 32,
        "boot": 1,
        "allocation_count": 0,
        "custody": custody,
        "network_id": "syn",
        "agent_activation_authorized": False,
    }
    envelope = {
        "status": body,
        "signature": signer.sign(STATUS_DOMAIN + canonical(body)).hex(),
    }
    kwargs = {
        "challenge": challenge,
        "expected_hash": hashlib.sha256(public).hexdigest(),
        "expected_version": runtime.runtime_version,
        "configuration_sha256": config_hash,
        "now": now,
    }
    if accepted:
        assert verify_status(envelope, **kwargs)["custody"] == custody
    else:
        with pytest.raises(SynProtocolError, match="Invalid operator custody/network profile"):
            verify_status(envelope, **kwargs)
