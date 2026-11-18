"""Native Agent proofs bind a discovered relay to its authenticated transport.

Socketpairs and explicitly synthetic root credentials/custody exercise the
native protocol and real signatures without claiming OS custody qualification.
"""
from contextlib import contextmanager
import copy
import hashlib
import secrets
import socket
import time
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.id import ID
import pytest

from syn.agent import pubsub
from syn_client.operator_protocol import SynProtocolError, canonical, receive, send
from syn_client.pubsub import (
    HELLO_DOMAIN, RELAY_BINDING_DOMAIN, group_hash, relay_peer_id,
    verify_hello, verify_relay_proof,
)

from test_pubsub import control, fixture  # noqa: F401 - shared synthetic custody fixture


@contextmanager
def attached(fixture, monkeypatch, *, version=2, public=None):
    servers, selected = fixture
    node = pubsub.NativePubSub(servers[0])
    child, parent = socket.socketpair()
    child.settimeout(1)
    parent.settimeout(1)
    monkeypatch.setattr(pubsub, "peer_uid", lambda _: 0)
    public = public or create_new_key_pair().public_key.to_bytes()
    request = {"profile": f"syn.private-pubsub-link/{version}", "scope": selected}
    if version == 2:
        request["relay_public_key"] = public.hex()
    try:
        node.attach(child, request)
        packet = receive(parent)
        yield node, packet, parent, public
    finally:
        node.close()
        child.close()
        parent.close()


def sign_binding(packet, server):
    packet["binding"]["signature"] = server.signer.sign(
        RELAY_BINDING_DOMAIN + canonical(packet["binding"]["body"])).hex()


def sign_hello(packet, server):
    packet["hello"]["signature"] = server.signer.sign(
        HELLO_DOMAIN + canonical(packet["hello"]["body"])).hex()


def test_native_v2_attach_proves_selected_agent_and_live_transport(fixture, monkeypatch):
    servers, selected = fixture
    with attached(fixture, monkeypatch) as (node, packet, connection, public):
        hello = verify_relay_proof(packet, selected, expected_peer_id=relay_peer_id(public))
        assert hello == packet["hello"]
        assert verify_hello(hello, selected, expected_node=servers[0].expected_hash)["boot_id"] == servers[0].boot_id
        body = packet["binding"]["body"]
        assert body["group_hash"] == group_hash(selected)
        assert body["hello_sha256"] == hashlib.sha256(canonical(hello)).hexdigest()
        assert body["relay_public_key"] == public.hex()
        assert set(packet) == {"hello", "binding"}
        assert set(packet["binding"]) == {"body", "signature"}

        # The original hello still enters the existing full native ready roster.
        other = pubsub.NativePubSub(servers[1])
        other.selected = selected
        send(connection, {"kind": "ready", "hellos": [hello, other.hello()]})
        assert node.ready.wait(2)
        status = control(node, "status")[0]["result"]
        assert status["peers"] == sorted(server.expected_hash for server in servers)
        assert status["custody"] == "native"
        assert status["cog_execution_authorized"] is False


def test_legacy_link_still_returns_unchanged_signed_hello(fixture, monkeypatch):
    servers, selected = fixture
    with attached(fixture, monkeypatch, version=1) as (_, packet, _, _):
        assert set(packet) == {"body", "public_key", "certificate", "signature"}
        verify_hello(packet, selected, expected_node=servers[0].expected_hash)
        with pytest.raises(SynProtocolError):
            verify_relay_proof(packet, selected, expected_peer_id="not-a-peer")


def test_group_identifier_commits_exact_scope_but_not_roster_order(fixture):
    _, selected = fixture
    expected = group_hash(selected)
    assert expected == group_hash({**selected, "members": list(reversed(selected["members"]))})
    assert expected != group_hash({**selected, "session_id": secrets.token_hex(16)})
    assert expected != group_hash({**selected, "workspace_id": str(uuid4())})
    assert expected != group_hash({**selected, "members": selected["members"][:1]})
    with pytest.raises(SynProtocolError):
        group_hash({**selected, "group_epoch": selected["group_epoch"] + 1})


def test_public_peer_id_encoding_matches_libp2p():
    keys = create_new_key_pair(bytes(range(32)))
    assert relay_peer_id(keys.public_key.to_bytes()) == str(ID.from_pubkey(keys.public_key))
    for invalid in (None, "00" * 32, b"", b"x" * 31, b"x" * 33, bytearray(32)):
        with pytest.raises(SynProtocolError):
            relay_peer_id(invalid)


def test_copied_proof_does_not_authorize_a_different_transport(fixture, monkeypatch):
    _, selected = fixture
    with attached(fixture, monkeypatch) as (_, packet, _, public):
        foreign = create_new_key_pair().public_key.to_bytes()
        with pytest.raises(SynProtocolError, match="authenticated transport"):
            verify_relay_proof(packet, selected, expected_peer_id=relay_peer_id(foreign))
        rewritten = copy.deepcopy(packet)
        rewritten["binding"]["body"]["relay_peer_id"] = relay_peer_id(foreign)
        rewritten["binding"]["body"]["relay_public_key"] = foreign.hex()
        with pytest.raises(SynProtocolError, match="signature"):
            verify_relay_proof(rewritten, selected, expected_peer_id=relay_peer_id(foreign))
        verify_relay_proof(packet, selected, expected_peer_id=relay_peer_id(public))


@pytest.mark.parametrize("field,value", [
    ("profile", "arbitrary-signing"), ("group_hash", "00" * 32),
    ("node", "00" * 32), ("boot_id", "00" * 16), ("hello_sha256", "00" * 32),
    ("relay_peer_id", "not-a-peer"), ("relay_public_key", "00" * 32),
    ("issued_at", 0), ("issued_at", True),
])
def test_even_a_signed_binding_must_match_hello_scope_and_transport(fixture, monkeypatch, field, value):
    servers, selected = fixture
    with attached(fixture, monkeypatch) as (_, packet, _, public):
        packet["binding"]["body"][field] = value
        sign_binding(packet, servers[0])
        with pytest.raises(SynProtocolError):
            verify_relay_proof(packet, selected, expected_peer_id=relay_peer_id(public))


def test_binding_timestamp_requires_integer_not_equal_float(fixture, monkeypatch):
    servers, selected = fixture
    with attached(fixture, monkeypatch) as (_, packet, _, public):
        packet["binding"]["body"]["issued_at"] = float(packet["binding"]["body"]["issued_at"])
        sign_binding(packet, servers[0])
        with pytest.raises(SynProtocolError):
            verify_relay_proof(packet, selected, expected_peer_id=relay_peer_id(public))


def test_binding_rejects_stale_and_future_hellos_despite_valid_signatures(fixture, monkeypatch):
    servers, selected = fixture
    with attached(fixture, monkeypatch) as (_, original, _, public):
        now = int(time.time())
        for issued in (now - 61, now + 6):
            packet = copy.deepcopy(original)
            packet["hello"]["body"]["issued_at"] = issued
            sign_hello(packet, servers[0])
            packet["binding"]["body"].update(
                issued_at=issued, hello_sha256=hashlib.sha256(canonical(packet["hello"])).hexdigest())
            sign_binding(packet, servers[0])
            with pytest.raises(SynProtocolError, match="stale|clock window"):
                verify_relay_proof(packet, selected, expected_peer_id=relay_peer_id(public), now=now)


def test_binding_requires_selected_node_signature_and_complete_scope(fixture, monkeypatch):
    _, selected = fixture
    with attached(fixture, monkeypatch) as (_, packet, _, public):
        altered = copy.deepcopy(packet)
        altered["binding"]["signature"] = Ed25519PrivateKey.generate().sign(
            RELAY_BINDING_DOMAIN + canonical(altered["binding"]["body"])).hex()
        with pytest.raises(SynProtocolError, match="signature"):
            verify_relay_proof(altered, selected, expected_peer_id=relay_peer_id(public))
        for changed in ({**selected, "session_id": secrets.token_hex(16)},
                        {**selected, "workspace_id": str(uuid4())},
                        {**selected, "members": selected["members"][:1]}):
            with pytest.raises(SynProtocolError):
                verify_relay_proof(packet, changed, expected_peer_id=relay_peer_id(public))


@pytest.mark.parametrize("change", ["non-root", "extra-payload", "bad-key", "wrong-epoch"])
def test_native_attach_refuses_unprivileged_or_unbounded_signing_requests(fixture, monkeypatch, change):
    servers, selected = fixture
    node = pubsub.NativePubSub(servers[0])
    public = create_new_key_pair().public_key.to_bytes()
    request = {"profile": "syn.private-pubsub-link/2", "scope": selected, "relay_public_key": public.hex()}
    monkeypatch.setattr(pubsub, "peer_uid", lambda _: 1000 if change == "non-root" else 0)
    if change == "extra-payload":
        request["payload"] = "sign arbitrary bytes"
    elif change == "bad-key":
        request["relay_public_key"] = "00" * 31
    elif change == "wrong-epoch":
        request["scope"] = {**selected, "group_epoch": 2}
    child, parent = socket.socketpair()
    try:
        with pytest.raises(SynProtocolError):
            node.attach(child, request)
        assert node.selected is None
        assert node.connection is None
    finally:
        node.close()
        child.close()
        parent.close()


def test_live_native_link_cannot_be_rebound_to_another_relay_key(fixture, monkeypatch):
    _, selected = fixture
    with attached(fixture, monkeypatch) as (node, _, connection, _):
        request = {"profile": "syn.private-pubsub-link/2", "scope": selected,
                   "relay_public_key": create_new_key_pair().public_key.to_bytes().hex()}
        with pytest.raises(SynProtocolError, match="cannot be replaced or rebound"):
            node.attach(connection, request)
