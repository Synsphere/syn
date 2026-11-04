"""Canonical native-keystore encryption and bounded volatile pub-sub endpoint.

Only a privileged local Syn relay can join an immutable session. Potbot never
receives group keys or implements signing, encryption, replay or fan-out.
"""
from __future__ import annotations

from collections import deque
import hashlib
import re
import socket
import threading
import time
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes, parse, peer_uid, receive, send
from syn_client.pubsub import (HELLO_DOMAIN, MAX_PAYLOAD, MAX_QUEUE, MESSAGE_DOMAIN, REPLY_DOMAIN,
                               scope, verify_hello, verify_message)
from .group import load_group, load_group_custody

KEY_DOMAIN = b"syn.private-pubsub.sender-key.hkdf-sha256/1\0"


class NativePubSub:
    def __init__(self, server):
        self.server = server
        self.selected = None
        self.connection = None
        self.reader = None
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.fault = threading.Event()
        self.ready = threading.Event()
        self.changed = threading.Condition(self.lock)
        self.hellos = {}
        self.last_sequence = {}
        self.messages = deque()
        self.sequence = 0
        self.acknowledged = None
        self.closed = False

    def hello(self):
        server, selected = self.server, self.selected
        body = {"profile": "syn.private-pubsub-hello/1", "node": server.expected_hash,
                "workspace_id": selected["workspace_id"], "session_id": selected["session_id"],
                "boot_id": server.boot_id, "issued_at": int(time.time())}
        return {"body": body, "public_key": server.signer.public_key.hex(),
                "certificate": load_group(server.config, server.expected_hash),
                "signature": server.signer.sign(HELLO_DOMAIN + canonical(body)).hex()}

    def attach(self, conn, request):
        request = exact(request, {"profile", "scope"}, "private relay link")
        if request["profile"] != "syn.private-pubsub-link/1" or peer_uid(conn) != 0:
            raise SynProtocolError("Private Syn relay requires the privileged local controller")
        selected = scope(request["scope"])
        own, _, members = load_group_custody(self.server.config, self.server.expected_hash)
        selected_nodes = sorted(packet["body"]["node_public_key_hash"] for packet in selected["members"])
        if (own not in selected["members"] or selected["group_epoch"] != own["body"]["group_epoch"]
                or (members is not None and selected_nodes != members)
                or any(selected[key] != self.server.config[key] for key in ("network_id", "group_id"))
                or selected["operator"] != self.server.config["expected_operator"]):
            raise SynProtocolError("Private relay scope differs from retained Agent custody")
        with self.lock:
            if self.selected is not None or self.closed:
                raise SynProtocolError("A running Agent mesh session cannot be replaced or rebound")
            self.selected = selected
            try:
                send(conn, self.hello())
                self.connection = conn.dup()
                self.connection.settimeout(1)
                self.reader = threading.Thread(target=self._read, name="syn-private-pubsub", daemon=True)
                self.reader.start()
            except BaseException:
                self.fault.set()
                if self.connection is not None:
                    self.connection.close()
                raise

    def _key(self, header):
        # Recheck actual native custody on each operation. No key material is
        # returned over IPC or made available to the public-only client/relay.
        certificate, secret, _ = load_group_custody(self.server.config, self.server.expected_hash)
        if certificate["body"]["group_epoch"] != header["group_epoch"]:
            raise SynProtocolError("Native pub-sub key belongs to another epoch")
        context = {name: header[name] for name in ("network_id", "group_id", "group_epoch", "workspace_id", "session_id", "node", "boot_id")}
        return HKDF(algorithm=hashes.SHA256(), length=32,
                    salt=hashlib.sha256(KEY_DOMAIN).digest(), info=KEY_DOMAIN + canonical(context)).derive(secret)

    def require_live(self):
        if self.selected is None or not self.ready.is_set() or self.fault.is_set() or self.closed:
            raise SynProtocolError("Private Syn pub-sub session is unavailable or fenced")

    def _write(self, packet):
        with self.write_lock:
            send(self.connection, packet)

    def _read(self):
        try:
            # A timeout is fatal after any partial frame; do not resume parsing
            # a stream from an unknown byte offset. Idle sockets use select.
            import select
            while not self.closed:
                if not select.select([self.connection], [], [], 0.2)[0]:
                    continue
                packet = receive(self.connection)
                kind = packet.get("kind")
                if kind == "ready":
                    exact(packet, {"kind", "hellos"}, "private relay roster")
                    if not isinstance(packet["hellos"], list):
                        raise SynProtocolError("Relay roster is invalid")
                    hellos = {}
                    expected = {member["body"]["node_public_key_hash"] for member in self.selected["members"]}
                    for hello in packet["hellos"]:
                        node = hello.get("body", {}).get("node")
                        body = verify_hello(hello, self.selected, expected_node=node)
                        if node in hellos:
                            raise SynProtocolError("Relay repeated a member")
                        hellos[node] = body
                    if set(hellos) != expected or hellos[self.server.expected_hash]["boot_id"] != self.server.boot_id:
                        raise SynProtocolError("Relay roster differs from the actual selected Agent set")
                    with self.changed:
                        if self.ready.is_set():
                            raise SynProtocolError("Relay cannot replace a live roster")
                        self.hellos = hellos
                        self.ready.set()
                        self.changed.notify_all()
                elif kind == "ack":
                    exact(packet, {"kind", "message_sha256"}, "publication acknowledgement")
                    with self.changed:
                        self.acknowledged = hex_bytes(packet["message_sha256"], 32, "publication digest").hex()
                        self.changed.notify_all()
                elif kind == "message":
                    exact(packet, {"kind", "envelope"}, "private pub-sub delivery")
                    self.require_live()
                    envelope = packet["envelope"]
                    node = envelope.get("header", {}).get("node")
                    if node == self.server.expected_hash or node not in self.hellos:
                        raise SynProtocolError("Unexpected private pub-sub sender")
                    header = verify_message(envelope, self.selected, expected_node=node,
                                            expected_boot=self.hellos[node]["boot_id"])
                    if header["sequence"] <= self.last_sequence.get(node, 0):
                        raise SynProtocolError("Replayed or reordered private pub-sub message")
                    aad = canonical(header) + canonical(envelope["certificate"])
                    nonce = header["sequence"].to_bytes(12, "big")
                    try:
                        raw = AESGCM(self._key(header)).decrypt(nonce, bytes.fromhex(envelope["ciphertext"]), aad)
                    except InvalidTag:
                        raise SynProtocolError("Private pub-sub payload could not be authenticated") from None
                    if len(raw) > MAX_PAYLOAD or hashlib.sha256(raw).hexdigest() != header["plaintext_sha256"]:
                        raise SynProtocolError("Private pub-sub plaintext commitment differs")
                    payload = parse(raw)
                    with self.lock:
                        if len(self.messages) >= MAX_QUEUE:
                            raise SynProtocolError("Private pub-sub reception queue is full")
                        self.messages.append({"envelope": envelope, "payload": payload})
                        self.last_sequence[node] = header["sequence"]
                else:
                    raise SynProtocolError("Unknown private relay frame")
        except BaseException:
            if not self.closed:
                self.fault.set()
                with self.changed:
                    self.changed.notify_all()

    def publish(self, topic, payload):
        try:
            return self._publish(topic, payload)
        except BaseException:
            # Custody/encryption/signing failures before the send are just as
            # fatal as an uncertain publication or acknowledgement failure.
            self.fault.set()
            raise

    def _publish(self, topic, payload):
        self.require_live()
        if (not isinstance(topic, str) or re.fullmatch(r"(?:syn|synny)://type/[a-z0-9][a-z0-9-]{0,95}", topic) is None
                or not isinstance(payload, dict) or len(canonical(payload)) > MAX_PAYLOAD):
            raise SynProtocolError("Publication requires an exact typed topic and bounded JSON object")
        with self.changed:
            self.sequence += 1
            if self.sequence >= 2**53:
                self.fault.set()
                raise SynProtocolError("Private pub-sub sequence exhausted")
            raw = canonical(payload)
            header = {"profile": "syn.private-pubsub-message/1", "node": self.server.expected_hash,
                "boot_id": self.server.boot_id, "message_id": str(uuid4()), "sequence": self.sequence,
                "issued_at": int(time.time()), "topic": topic, "plaintext_sha256": hashlib.sha256(raw).hexdigest(),
                **{name: self.selected[name] for name in ("workspace_id", "session_id", "network_id", "group_id", "group_epoch")}}
            certificate = load_group(self.server.config, self.server.expected_hash)
            cipher = AESGCM(self._key(header)).encrypt(self.sequence.to_bytes(12, "big"), raw,
                                                      canonical(header) + canonical(certificate))
            unsigned = {"header": header, "public_key": self.server.signer.public_key.hex(),
                        "certificate": certificate, "ciphertext": cipher.hex()}
            envelope = {**unsigned, "signature": self.server.signer.sign(MESSAGE_DOMAIN + canonical(unsigned)).hex()}
            digest = hashlib.sha256(canonical(envelope)).hexdigest()
            try:
                self.acknowledged = None
                self._write({"kind": "message", "envelope": envelope})
                deadline = time.monotonic() + 2
                while self.acknowledged != digest:
                    self.require_live()
                    if not self.changed.wait(timeout=max(0, deadline - time.monotonic())):
                        raise SynProtocolError("Private publication acknowledgement timed out")
                return {"message_sha256": digest, "sequence": self.sequence,
                        "relay_accepted": True, "peer_reception_proved": False}
            except BaseException:
                self.fault.set()
                raise

    def control(self, request):
        request = exact(request, {"profile", "challenge", "expected_public_key_hash", "configuration_sha256", "operation", "payload"}, "pub-sub request")
        if (request["profile"] != "syn.private-pubsub-request/1"
                or request["expected_public_key_hash"] != self.server.expected_hash
                or request["configuration_sha256"] != self.server.config_sha
                or hex_bytes(request["challenge"], 32, "pub-sub challenge") == bytes(32)):
            raise SynProtocolError("Pub-sub request differs from its selected Agent")
        self.require_live()
        # Check the retained assignment, state and actual OS credentials too.
        self.server.status(self.server.health_request())
        operation, payload = request["operation"], request["payload"]
        if operation == "status":
            if payload is not None:
                raise SynProtocolError("Pub-sub status takes no payload")
            result = {"connected": True, "peers": sorted(self.hellos), "custody": "native",
                      "volatile": True, "cog_execution_authorized": False}
        elif operation == "publish":
            payload = exact(payload, {"topic", "value"}, "typed publication")
            result = self.publish(payload["topic"], payload["value"])
        elif operation == "receive":
            if payload is not None:
                raise SynProtocolError("Pub-sub receive takes no payload")
            with self.lock:
                result = {"messages": [self.messages.popleft() for _ in range(min(2, len(self.messages)))]}
        else:
            raise SynProtocolError("Unknown private pub-sub operation")
        body = {"profile": "syn.private-pubsub-reply/1", "challenge": request["challenge"],
                "request_sha256": hashlib.sha256(canonical(request)).hexdigest(), "operation": operation,
                "node": self.server.expected_hash, "boot_id": self.server.boot_id,
                "workspace_id": self.selected["workspace_id"], "session_id": self.selected["session_id"], "result": result}
        return {"reply": body, "public_key": self.server.signer.public_key.hex(),
                "signature": self.server.signer.sign(REPLY_DOMAIN + canonical(body)).hex()}

    def close(self):
        self.closed = True
        self.fault.set()
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
        if self.reader is not None:
            self.reader.join(timeout=3)
