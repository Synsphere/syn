"""Native approval cannot be widened by a network-facing caller."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p.crypto.ed25519 import create_new_key_pair
import pytest

from syn import peer_record as record

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host-custody"))
from syn_host_custody import core, peer_record as host_record


class Vault:
    def __init__(self, public):
        self.public, self.slots = public, {}
        self.fail_write = False

    def identity(self, name):
        assert name == "syn.operator/owner"
        return b"s" * 32, self.public

    def get_slot(self, service, slot):
        return self.slots.get((service, slot))

    def set_slot(self, service, slot, value):
        if self.fail_write:
            raise OSError("vault flush failed")
        self.slots[service, slot] = value


@pytest.fixture
def authority(monkeypatch):
    base = Ed25519PrivateKey.from_private_bytes(b"o" * 32)
    public = base.public_key().public_bytes_raw()
    pin = hashlib.sha256(public).hexdigest()
    vault = Vault({"base_public_key": public.hex(), "base_public_key_hash": pin})
    approvals, signatures = [], []
    def sign(seed, payload):
        signatures.append(payload)
        return base.sign(payload)
    monkeypatch.setattr(core, "sign_operator", sign)
    monkeypatch.setattr(host_record.time, "time", lambda: 1000)
    broker = core.Authority(vault, approve=lambda message: approvals.append(message) or True)
    transport = create_new_key_pair(b"t" * 32).public_key.to_bytes()
    server = SimpleNamespace(expected_hash=pin, config={"network_id": "syn"},
                             signer=SimpleNamespace(public_key=public, sign=base.sign))
    body = record.issue(server, transport, ["/ip4/127.0.0.1/tcp/4001"], now=1000)["body"]
    req = {"op": "peer_record", "service": "syn.operator", "account": "owner", "pin": pin, "body": body}
    return broker, vault, req, approvals, signatures


def test_first_approval_is_retained_and_exact_binding_can_refresh(authority):
    broker, vault, req, approvals, signatures = authority
    signature = broker.request(req)
    packet = {"body": req["body"], "public_key": vault.public["base_public_key"], "signature": signature}
    record.verify(packet, expected_operator=req["pin"], expected_network="syn", now=1000)
    assert len(approvals) == 1
    assert req["pin"] in approvals[0] and req["body"]["peer_id"] in approvals[0]
    broker.approve = lambda message: pytest.fail("same binding must not prompt again")
    refreshed = {**req, "body": {**req["body"], "issued_at": 1020, "expires_at": 4620}}
    broker.request(refreshed)
    assert len(vault.slots) == 1 and len(signatures) == 2


@pytest.mark.parametrize("change", ["address", "transport", "network"])
def test_changed_binding_requires_new_approval(authority, change):
    broker, vault, req, approvals, signatures = authority
    broker.request(req)
    before = dict(vault.slots)
    changed = copy.deepcopy(req)
    if change == "address":
        changed["body"]["addresses"] = ["/ip4/127.0.0.1/tcp/4002"]
    elif change == "network":
        changed["body"]["network_id"] = "other"
    else:
        key = create_new_key_pair(b"n" * 32).public_key.to_bytes()
        changed["body"]["transport_public_key"] = key.hex()
        changed["body"]["peer_id"] = record.transport_peer_id(key)
    broker.approve = lambda message: False
    with pytest.raises(ValueError, match="approval declined"):
        broker.request(changed)
    assert vault.slots == before and len(signatures) == 1


def test_generic_credentials_cannot_read_or_change_approval(authority):
    broker, vault, req, _, _ = authority
    broker.request(req)
    slot = "owner" + host_record.APPROVAL_SUFFIX
    for op in ("get", "set"):
        with pytest.raises(ValueError, match="Root HD material|protected host custody"):
            broker.request({"op": op, "service": "syn.operator", "account": "slot", "slot": slot,
                            "value": "forged approval"})


def test_generic_signing_cannot_use_retained_peer_approval(authority):
    broker, _, req, _, signatures = authority
    broker.request(req)
    broker.approve = lambda message: False
    with pytest.raises(ValueError, match="signature approval declined"):
        broker.request({"op": "sign", "service": req["service"], "account": req["account"],
                        "pin": req["pin"], "payload": (record.DOMAIN + record.canonical(req["body"])).hex()})
    assert len(signatures) == 1


@pytest.mark.parametrize("mutations", [
    {"operator": "1" * 64}, {"profile": "syn.operator-status/1"},
    {"network_id": "NOT-CANONICAL"}, {"peer_id": "forged-peer"},
    {"transport_public_key": "0" * 64}, {"expires_at": 4601},
    {"expires_at": 1000}, {"issued_at": True},
    {"addresses": ["/ip4/0.0.0.0/tcp/4001"]},
    {"addresses": ["/ip6/fe80::1%eth0/tcp/4001"]},
    {"grant": "extra-authority"},
])
def test_host_rejects_invalid_statement_before_prompt_or_sign(authority, mutations):
    broker, vault, req, approvals, signatures = authority
    changed = {**req, "body": {**req["body"], **mutations}}
    with pytest.raises(ValueError):
        broker.request(changed)
    assert not approvals and not signatures and not vault.slots


def test_failed_approval_persistence_does_not_release_signature(authority):
    broker, vault, req, _, signatures = authority
    vault.fail_write = True
    with pytest.raises(OSError, match="flush failed"):
        broker.request(req)
    assert not signatures and not vault.slots


def test_record_expiring_during_native_prompt_is_not_signed(authority, monkeypatch):
    broker, vault, req, _, signatures = authority
    def approve(message):
        monkeypatch.setattr(host_record.time, "time", lambda: req["body"]["expires_at"])
        return True
    broker.approve = approve
    with pytest.raises(ValueError, match="expired"):
        broker.request(req)
    assert not signatures


def test_corrupt_retained_approval_is_not_silently_replaced(authority):
    broker, vault, req, approvals, signatures = authority
    vault.slots[(req["service"], req["account"] + host_record.APPROVAL_SUFFIX)] = '{"untrusted":true}'
    with pytest.raises(ValueError, match="approval is invalid"):
        broker.request(req)
    assert not signatures and not approvals


def test_real_hd_signature_roundtrips_through_host_signer_adapter(monkeypatch):
    """Exercise actual HD signing and the WSL adapter without native IPC/DPAPI."""
    from syn.operator import host_custody
    public = core.public_identity(b"s" * 32)
    vault = Vault(public)
    broker = core.Authority(vault, approve=lambda message: True)
    operations = []
    def request(body):
        operations.append(body["op"])
        return broker.request(body)
    monkeypatch.setattr(host_custody, "request", request)
    pin = public["base_public_key_hash"]
    signer = host_custody.HostHdOperatorSigner("syn.operator", "owner", bytes.fromhex(pin))
    server = SimpleNamespace(signer=signer, expected_hash=pin, config={"network_id": "syn"})
    transport = create_new_key_pair(b"t" * 32).public_key.to_bytes()
    packet = record.issue(server, transport, ["/ip4/127.0.0.1/tcp/4001"])
    assert packet["public_key"] == public["base_public_key"]
    assert packet["public_key"] != public["root_public_key"]
    record.verify(packet, expected_operator=pin, expected_network="syn",
                  expected_peer_id=record.transport_peer_id(transport))
    assert "peer_record" in operations
    assert set(operations) == {"identity", "peer_record"}
