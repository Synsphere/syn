"""Public discovery binds an untrusted provider to the pinned Operator."""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.id import ID
import pytest

from syn import peer_record as record


class Signer:
    def __init__(self, seed=b"o" * 32):
        self.key = Ed25519PrivateKey.from_private_bytes(seed)
        self.public_key = self.key.public_key().public_bytes_raw()
        self.payloads = []

    def sign(self, payload):
        self.payloads.append(payload)
        return self.key.sign(payload)


@pytest.fixture
def binding():
    signer = Signer()
    server = SimpleNamespace(signer=signer, expected_hash=hashlib.sha256(signer.public_key).hexdigest(),
                             config={"network_id": "syn"})
    transport = create_new_key_pair(b"t" * 32)
    packet = record.issue(server, transport.public_key.to_bytes(),
                          ["/ip4/127.0.0.1/tcp/4001"], now=1000)
    return server, transport, packet


def verify(packet, operator, *, now=1001, peer=None):
    return record.verify(packet, expected_operator=operator, expected_network="syn",
                         expected_peer_id=peer or packet["body"]["peer_id"], now=now)


def test_provider_multihash_names_exact_raw_base_key(binding):
    server, _, packet = binding
    key = record.provider_key(server.expected_hash)
    assert key == b"\x12\x20" + hashlib.sha256(server.signer.public_key).digest()
    assert key[2:] != hashlib.sha256(bytes.fromhex(packet["body"]["transport_public_key"])).digest()
    import multibase
    # Independently decode the advertised CID with libp2p's multibase library:
    # version 1, raw codec 0x55, followed by the exact provider multihash.
    assert multibase.decode(record.provider_cid(server.expected_hash)) == b"\x01\x55" + key


@pytest.mark.parametrize("seed", [bytes(32), b"a" * 32, b"b" * 32, bytes(range(32))])
def test_transport_peer_id_matches_libp2p_standard_serialization(seed):
    pair = create_new_key_pair(seed)
    assert record.transport_peer_id(pair.public_key.to_bytes()) == str(ID.from_pubkey(pair.public_key))


def test_issuance_and_canonical_wire_roundtrip(binding):
    server, transport, packet = binding
    peer = str(ID.from_pubkey(transport.public_key))
    body = verify(record.decode(record.encode(packet)), server.expected_hash, peer=peer)
    assert body == packet["body"]
    assert server.signer.payloads == [record.DOMAIN + record.canonical(body)]
    assert body["expires_at"] - body["issued_at"] == record.MAX_TTL_SECONDS


def test_untrusted_provider_cannot_claim_another_operator(binding):
    server, transport, packet = binding
    impostor = Signer(b"i" * 32)
    forged = {**packet, "public_key": impostor.public_key.hex(),
              "signature": impostor.sign(record.DOMAIN + record.canonical(packet["body"])).hex()}
    with pytest.raises(ValueError, match="pinned Operator base hash"):
        verify(forged, server.expected_hash)


def test_same_record_cannot_be_served_as_another_authenticated_peer(binding):
    server, _, packet = binding
    other = create_new_key_pair(b"q" * 32)
    with pytest.raises(ValueError, match="authenticated transport peer"):
        verify(packet, server.expected_hash, peer=str(ID.from_pubkey(other.public_key)))


@pytest.mark.parametrize("field,value", [
    ("operator", "0" * 64),
    ("network_id", "other"),
    ("peer_id", "12D3KooWForged"),
    ("transport_public_key", "0" * 64),
    ("addresses", ["/ip4/127.0.0.1/tcp/4002"]),
    ("issued_at", 1001),
    ("expires_at", 4599),
])
def test_signature_commits_every_public_binding_field(binding, field, value):
    server, _, packet = binding
    modified = copy.deepcopy(packet)
    modified["body"][field] = value
    with pytest.raises(ValueError):
        verify(modified, server.expected_hash)


@pytest.mark.parametrize("mutations,now", [
    ({"expires_at": 1000}, 1000),
    ({"expires_at": 4601}, 1000),
    ({"issued_at": True}, 1000),
    ({"expires_at": 4600.0}, 1000),
    ({"issued_at": 1061, "expires_at": 4600}, 1000),
    ({}, 4600),
])
def test_invalid_times_are_rejected_even_when_signed(binding, mutations, now):
    server, _, packet = binding
    modified = copy.deepcopy(packet)
    modified["body"].update(mutations)
    modified["signature"] = server.signer.sign(record.DOMAIN + record.canonical(modified["body"])).hex()
    with pytest.raises(ValueError, match="validity interval|expired"):
        verify(modified, server.expected_hash, now=now)


@pytest.mark.parametrize("addresses", [
    [], ["/ip4/0.0.0.0/tcp/4001"], ["/ip6/::/tcp/4001"],
    ["/ip4/224.0.0.1/tcp/4001"], ["/ip4/127.0.0.1/tcp/0"],
    ["/ip4/127.0.0.1/tcp/04001"], ["/ip4/127.0.0.1/udp/4001"],
    ["/dns4/example.com/tcp/4001"], ["/ip6/fe80::1%eth0/tcp/4001"],
    ["/ip4/127.0.0.1/tcp/4001/p2p/wrong"],
    ["/ip4/127.0.0.1/tcp/4001"] * 2,
])
def test_unsafe_or_noncanonical_endpoints_rejected_before_signing(binding, addresses):
    server, _, packet = binding
    body = {**packet["body"], "addresses": addresses}
    with pytest.raises(ValueError):
        record.validate_body(body, expected_operator=server.expected_hash, now=1000)


def test_peer_suffix_and_ipv6_are_bound_to_the_transport(binding):
    server, transport, packet = binding
    peer = packet["body"]["peer_id"]
    signed = record.issue(server, transport.public_key.to_bytes(),
        ["/ip6/::1/tcp/4001/p2p/" + peer, "/ip4/127.0.0.1/tcp/4001"], now=1000)
    assert verify(signed, server.expected_hash)["addresses"] == sorted(signed["body"]["addresses"])


def test_unknown_fields_cannot_extend_the_discovery_statement(binding):
    server, _, packet = binding
    modified = copy.deepcopy(packet)
    modified["body"]["grant_execution"] = True
    with pytest.raises(ValueError, match="fields"):
        verify(modified, server.expected_hash)
    with pytest.raises(ValueError, match="fields"):
        verify({**packet, "authorization": True}, server.expected_hash)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a": 1}', b'[]', b'{"x":NaN}', b' ' * 8193])
def test_decoder_rejects_ambiguous_noncanonical_or_oversized_data(raw):
    with pytest.raises(ValueError):
        record.decode(raw)


def test_issuer_rejects_a_different_installed_identity(binding):
    server, transport, _ = binding
    server.expected_hash = "1" * 64
    with pytest.raises(ValueError, match="installed base identity"):
        record.issue(server, transport.public_key.to_bytes(), ["/ip4/127.0.0.1/tcp/4001"], now=1000)


def test_native_peer_signing_is_used_and_forged_native_response_rejected(binding):
    server, transport, packet = binding
    called = []
    def native(body):
        called.append(body)
        return server.signer.key.sign(record.DOMAIN + record.canonical(body))
    server.signer.sign_peer_record = native
    refreshed = record.OperatorRecordSigner(server).issue(transport.public_key.to_bytes(),
        packet["body"]["addresses"], now=1100)
    assert called == [refreshed["body"]]
    server.signer.sign_peer_record = lambda body: b"x" * 64
    with pytest.raises(ValueError, match="signature failed"):
        record.issue(server, transport.public_key.to_bytes(), packet["body"]["addresses"], now=1100)
