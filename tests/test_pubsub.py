"""Real cryptographic/socket fault tests with explicit synthetic custody only."""
from contextlib import contextmanager
import copy
import hashlib
import secrets
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from syn.agent import pubsub
from syn.mesh import PrivateRelay
from syn_client.group import CERT_DOMAIN, COMMIT_DOMAIN
from syn_client.operator_protocol import SynProtocolError, canonical, send
from syn_client.pubsub import scope, verify_message, verify_reply


def public(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@pytest.fixture
def fixture(monkeypatch):
    issuer = Ed25519PrivateKey.generate()
    issuer_public = public(issuer)
    operator = hashlib.sha256(issuer_public).hexdigest()
    group_key = secrets.token_bytes(32)
    identifier = str(uuid4())
    servers, certificates = [], []
    for _ in range(2):
        key = Ed25519PrivateKey.generate()
        selected = public(key)
        node = hashlib.sha256(selected).hexdigest()
        body = {"profile": "syn.agent-group-certificate/1", "operator_public_key_hash": operator,
                "node_public_key_hash": node, "network_id": "fixture", "group_id": identifier,
                "group_epoch": 1, "group_key_commitment": hashlib.sha256(COMMIT_DOMAIN + group_key).hexdigest()}
        certificate = {"body": body, "operator_public_key": issuer_public.hex(),
                       "signature": issuer.sign(CERT_DOMAIN + canonical(body)).hex()}
        config = {"network_id": "fixture", "group_id": identifier, "group_epoch": 1,
                  "expected_operator": operator, "keyring_service": "synthetic", "keyring_account": node}
        server = SimpleNamespace(expected_hash=node, boot_id=secrets.token_hex(16), config_sha=secrets.token_hex(32),
            config=config, signer=SimpleNamespace(public_key=selected, sign=key.sign),
            health_request=lambda: {}, status=lambda _: None)
        servers.append(server)
        certificates.append(certificate)
    selected_scope = scope({"profile": "syn.private-pubsub-scope/1", "operator": operator,
        "workspace_id": str(uuid4()), "session_id": secrets.token_hex(16), "group_id": identifier,
        "group_epoch": 1, "network_id": "fixture", "members": certificates})
    mapping = dict(zip((server.expected_hash for server in servers), certificates))
    monkeypatch.setattr(pubsub, "load_group", lambda config, node: mapping[node])
    monkeypatch.setattr(pubsub, "load_group_custody", lambda config, node: (mapping[node], group_key, None))
    return servers, selected_scope


@contextmanager
def connected(fixture):
    servers, selected = fixture
    nodes = [pubsub.NativePubSub(server) for server in servers]
    relay = PrivateRelay({"profile": "syn.private-relay-config/1", "scope": selected,
        "endpoints": [{"node": server.expected_hash, "socket": "/synthetic/agent.sock", "owner_uid": 1000 + i}
                      for i, server in enumerate(servers)]})
    captured = []
    for node in nodes:
        child, parent = socket.socketpair()
        child.settimeout(1)
        parent.settimeout(1)
        node.selected, node.connection = selected, child
        node.reader = threading.Thread(target=node._read, daemon=True)
        node.reader.start()
        key = node.server.expected_hash
        relay.connections[key] = parent
        relay.writes[key] = threading.Lock()
        relay.hellos[key] = node.hello()
    real_send = relay._send

    def record(node, packet):
        captured.append(copy.deepcopy(packet))
        return real_send(node, packet)

    relay._send = record
    try:
        for conn in relay.connections.values():
            send(conn, {"kind": "ready", "hellos": list(relay.hellos.values())})
        for node in nodes:
            assert node.ready.wait(2)
            node.require_live()
        for key, conn in relay.connections.items():
            worker = threading.Thread(target=relay._reader, args=(key, conn), daemon=True)
            relay.threads.append(worker)
            worker.start()
        yield nodes, relay, captured, selected
    finally:
        relay.close()
        for node in nodes:
            node.close()


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Bounded protocol fixture did not reach its expected state")
        time.sleep(0.01)


def control(node, operation, payload=None):
    request = {"profile": "syn.private-pubsub-request/1", "challenge": secrets.token_hex(32),
        "expected_public_key_hash": node.server.expected_hash, "configuration_sha256": node.server.config_sha,
        "operation": operation, "payload": payload}
    packet = node.control(request)
    return verify_reply(packet, request, node.selected, expected_node=node.server.expected_hash,
                        expected_boot=node.server.boot_id), packet, request


def test_real_encryption_signed_reception_and_no_execution_permission(fixture):
    with connected(fixture) as (nodes, relay, captured, selected):
        left, right = nodes
        message = {"private_fixture": "payload-is-not-a-relay-plaintext", "run": str(uuid4())}
        accepted = control(left, "publish", {"topic": "synny://type/potbot-worker-heartbeat-v1", "value": message})[0]
        assert accepted["result"]["relay_accepted"] is True
        assert accepted["result"]["peer_reception_proved"] is False
        wait_for(lambda: bool(right.messages))
        body, reply, request = control(right, "receive")
        entry, = body["result"]["messages"]
        assert entry["payload"] == message
        wire, = [packet for packet in captured if packet["kind"] == "message"]
        assert message["private_fixture"].encode() not in canonical(wire)
        assert "payload" not in wire["envelope"]
        assert verify_message(entry["envelope"], selected)["node"] == left.server.expected_hash
        assert control(right, "status")[0]["result"]["cog_execution_authorized"] is False
        wrong = copy.deepcopy(selected)
        wrong["workspace_id"] = str(uuid4())
        with pytest.raises(SynProtocolError):
            verify_reply(reply, request, wrong, expected_node=right.server.expected_hash)


@pytest.mark.parametrize("field,value", [("workspace_id", str(uuid4())), ("session_id", "01" * 16),
    ("boot_id", "02" * 16), ("group_epoch", 2), ("network_id", "other"), ("topic", "arbitrary-code"),
    ("issued_at", 0), ("sequence", 0), ("sequence", True)])
def test_changed_scope_signature_stale_boot_and_invalid_sequence_are_rejected(fixture, field, value):
    with connected(fixture) as (nodes, relay, captured, selected):
        nodes[0].publish("syn://type/event", {"fixture": True})
        wait_for(lambda: bool(nodes[1].messages))
        envelope = copy.deepcopy(nodes[1].messages[0]["envelope"])
        envelope["header"][field] = value
        with pytest.raises(SynProtocolError):
            verify_message(envelope, selected, expected_boot=nodes[0].server.boot_id)


def test_duplicate_and_changed_ciphertext_fence_actual_reader(fixture):
    with connected(fixture) as (nodes, relay, captured, selected):
        nodes[0].publish("syn://type/event", {"fixture": True})
        wait_for(lambda: bool(nodes[1].messages))
        packet = next(packet for packet in captured if packet["kind"] == "message")
        relay._send(nodes[1].server.expected_hash, packet)
        wait_for(nodes[1].fault.is_set)
        with pytest.raises(SynProtocolError):
            nodes[1].require_live()
        envelope = copy.deepcopy(packet["envelope"])
        envelope["ciphertext"] = "00" + envelope["ciphertext"][2:]
        with pytest.raises(SynProtocolError):
            verify_message(envelope, selected)


def test_native_custody_loss_and_relay_loss_fence_pubsub(fixture, monkeypatch):
    with connected(fixture) as (nodes, relay, captured, selected):
        def locked(*_):
            raise SynProtocolError("Native keyring is locked")

        monkeypatch.setattr(pubsub, "load_group_custody", locked)
        with pytest.raises(SynProtocolError):
            nodes[0].publish("syn://type/event", {})
        assert nodes[0].fault.is_set()
        relay.close()
        wait_for(nodes[1].fault.is_set)


def test_group_roster_rejects_duplicates_different_epochs_and_malformed_certificates(fixture):
    _, selected = fixture
    for members in ([selected["members"][0]] * 2, [None], [{"body": []}]):
        with pytest.raises(SynProtocolError):
            scope({**selected, "members": members})
    changed = copy.deepcopy(selected)
    changed["group_epoch"] = 2
    with pytest.raises(SynProtocolError):
        scope(changed)
