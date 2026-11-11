"""Two-machine Syn GossipSub acceptance with fresh synthetic custody."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import trio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.id import ID

from syn.agent import pubsub
from syn.mesh_gossipsub import PROFILE, GossipRelay
from syn_client.group import CERT_DOMAIN, COMMIT_DOMAIN
from syn_client.operator_protocol import canonical, receive, send
from syn_client.pubsub import MESSAGE_DOMAIN, scope, verify_message, verify_reply


def public(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def prepare(directory, address):
    directory.mkdir()
    issuer = Ed25519PrivateKey.generate()
    operator = hashlib.sha256(public(issuer)).hexdigest()
    group_key = secrets.token_bytes(32)
    identifier = str(uuid4())
    agents, certificates = [], []
    for _ in range(2):
        seed = secrets.token_bytes(32)
        node = hashlib.sha256(public(Ed25519PrivateKey.from_private_bytes(seed))).hexdigest()
        relay_seed = secrets.token_bytes(32)
        relay_peer = str(ID.from_pubkey(create_new_key_pair(relay_seed).public_key))
        body = {"profile": "syn.agent-group-certificate/1", "operator_public_key_hash": operator,
                "node_public_key_hash": node, "network_id": "gossip-fixture", "group_id": identifier,
                "group_epoch": 1, "group_key_commitment": hashlib.sha256(COMMIT_DOMAIN + group_key).hexdigest()}
        certificates.append({"body": body, "operator_public_key": public(issuer).hex(),
                             "signature": issuer.sign(CERT_DOMAIN + canonical(body)).hex()})
        agents.append({"node": node, "seed": seed.hex(), "relay_seed": relay_seed.hex(), "peer": relay_peer})
    selected = scope({"profile": "syn.private-pubsub-scope/1", "operator": operator,
                      "workspace_id": str(uuid4()), "session_id": secrets.token_hex(16),
                      "group_id": identifier, "group_epoch": 1, "network_id": "gossip-fixture",
                      "members": certificates})
    outsider = Ed25519PrivateKey.generate()
    outsider_body = {**certificates[0]["body"], "node_public_key_hash": hashlib.sha256(public(outsider)).hexdigest()}
    outsider_cert = {"body": outsider_body, "operator_public_key": public(issuer).hex(),
                     "signature": issuer.sign(CERT_DOMAIN + canonical(outsider_body)).hex()}
    for index, agent in enumerate(agents):
        peer = agents[1 - index]
        path = directory / ("left.json" if index == 0 else "right.json")
        value = {"agent": agent, "scope": selected, "group_key": group_key.hex(),
                 "label": "left" if index == 0 else "right",
                 "outsider": {"seed": outsider.private_bytes_raw().hex(), "certificate": outsider_cert},
                 "listen": "/ip4/127.0.0.1/tcp/0" if index == 0 else address,
                 "peer": {"peer_id": peer["peer"], "nodes": [peer["node"]],
                          "address": address if index == 0 else None}}
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(canonical(value))


def run(fixture):
    value = json.loads(fixture.read_bytes())
    own, selected, label = value["agent"], scope(value["scope"]), value["label"]
    other = "right" if label == "left" else "left"
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(own["seed"]))
    cert = next(item for item in selected["members"] if item["body"]["node_public_key_hash"] == own["node"])
    group_key = bytes.fromhex(value["group_key"])
    pubsub.load_group = lambda *_: cert
    pubsub.load_group_custody = lambda *_: (
        cert, group_key, sorted(item["body"]["node_public_key_hash"] for item in selected["members"])
    )
    server = SimpleNamespace(expected_hash=own["node"], boot_id=secrets.token_hex(16), config_sha=secrets.token_hex(32),
        config={**{name: selected[name] for name in ("network_id", "group_id", "group_epoch")},
                "expected_operator": selected["operator"]},
        signer=SimpleNamespace(public_key=public(key), sign=key.sign),
        status=lambda _: None, health_request=lambda: {})
    node = pubsub.NativePubSub(server)

    def control(operation, payload=None):
        request = {"profile": "syn.private-pubsub-request/1", "challenge": secrets.token_hex(32),
                   "expected_public_key_hash": own["node"], "configuration_sha256": server.config_sha,
                   "operation": operation, "payload": payload}
        return verify_reply(node.control(request), request, selected, expected_node=own["node"],
                            expected_boot=server.boot_id)["result"]

    with tempfile.TemporaryDirectory(prefix="syn-gossip-", dir="/tmp") as temporary:
        identity = Path(temporary) / "seed"
        with os.fdopen(os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(bytes.fromhex(own["relay_seed"]))
        relay = GossipRelay({"profile": PROFILE, "scope": selected, "identity_path": str(identity),
                            "listen": value["listen"], "peers": [value["peer"]],
                            "endpoints": [{"node": own["node"], "socket": "/fixture/agent.sock", "owner_uid": 1000}]})

        # The synthetic Agent runs in-process. Exercise the actual native link
        # attach, custody check, encryption, decryption and signed receive APIs.
        child, parent = socket.socketpair()
        child.settimeout(3)
        parent.settimeout(3)

        def attach():
            send(parent, {"profile": "syn.private-pubsub-link/1", "scope": selected})
            node.attach(child, receive(child))
            relay.connections[own["node"]] = parent
            relay.writes[own["node"]] = threading.Lock()
            relay.hellos[own["node"]] = receive(parent)

        relay._attach_local = attach
        forwarded = []
        real_forward = relay._forward

        def capture(sender, packet):
            forwarded.append(copy.deepcopy(packet))
            real_forward(sender, packet)

        relay._forward = capture
        result = {}

        def exchange():
            def publish(sequence):
                accepted = control("publish", {"topic": "syn://type/gossip-fixture",
                    "value": {"host": label, "sequence": sequence}})
                assert accepted["relay_accepted"] and not accepted["peer_reception_proved"]

            try:
                assert node.ready.wait(3), "Agent did not accept the signed relay roster"
                publish(1)
                original, = forwarded
                altered = copy.deepcopy(original)
                altered["envelope"]["signature"] = "00" * 64
                real_forward(own["node"], altered)
                outsider = value["outsider"]
                outsider_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(outsider["seed"]))
                envelope = original["envelope"]
                unsigned = {"header": {**envelope["header"],
                            "node": outsider["certificate"]["body"]["node_public_key_hash"]},
                            "public_key": public(outsider_key).hex(), "certificate": outsider["certificate"],
                            "ciphertext": envelope["ciphertext"]}
                real_forward(own["node"], {"kind": "message", "envelope": {
                    **unsigned, "signature": outsider_key.sign(MESSAGE_DOMAIN + canonical(unsigned)).hex()}})
                real_forward(own["node"], original)
                publish(2)
                entries = []
                deadline = time.monotonic() + 12
                while len(entries) < 2 and time.monotonic() < deadline:
                    entries.extend(control("receive")["messages"])
                    time.sleep(0.05)
                assert [entry["payload"] for entry in entries] == [
                    {"host": other, "sequence": 1}, {"host": other, "sequence": 2}]
                for entry in entries:
                    header = verify_message(entry["envelope"], selected, expected_node=value["peer"]["nodes"][0])
                    assert header["group_epoch"] == 1 and "payload" not in entry["envelope"]
                assert b'"host"' not in canonical(original)
                assert not control("receive")["messages"]
                result.update(host=socket.gethostname(), side=label, received=2, invalid_delivered=0,
                              rejected=["invalid_signature", "nonmember", "replay"])
                time.sleep(1)
            finally:
                relay.stopped.set()

        def ready():
            nonlocal thread
            thread = threading.Thread(target=exchange)
            thread.start()

        thread = None
        try:
            trio.run(relay.run, ready)
            assert thread is not None
            thread.join(15)
            assert result and not thread.is_alive()
            print(json.dumps(result), flush=True)
        finally:
            node.close()
            child.close()
            parent.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare_args = commands.add_parser("prepare")
    prepare_args.add_argument("directory", type=Path)
    prepare_args.add_argument("address")
    run_args = commands.add_parser("run")
    run_args.add_argument("fixture", type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        prepare(args.directory, args.address)
    else:
        run(args.fixture)
