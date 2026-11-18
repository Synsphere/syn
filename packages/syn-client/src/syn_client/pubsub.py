"""Public verification for bounded, workspace-local encrypted Syn pub-sub.

Receipts prove signed publication or reception, never permission to execute.
The transport is volatile: it is not the durable workspace execution ledger.
"""
from __future__ import annotations

import hashlib
import re
import time

from .custody import verify_ed25519
from .consensus import PolicyError
from .group import verify_group
from .operator_protocol import SynProtocolError, canonical, exact, hex_bytes, uuid

HELLO_DOMAIN = b"syn.private-pubsub.hello/1\0"
MESSAGE_DOMAIN = b"syn.private-pubsub.message/1\0"
REPLY_DOMAIN = b"syn.private-pubsub.reply/1\0"
RELAY_BINDING_DOMAIN = b"syn.private-pubsub.relay-binding/1\0"
RELAY_BINDING_PROFILE = "syn.private-pubsub-relay-binding/1"
MAX_PEERS = 8
MAX_PAYLOAD = 8192
MAX_QUEUE = 64


def _signature(public, signature, message):
    try:
        verify_ed25519(public, signature, message)
    except PolicyError:
        raise SynProtocolError("Invalid private pub-sub signature") from None


def scope(value):
    value = exact(value, {"profile", "workspace_id", "session_id", "operator", "network_id",
                          "group_id", "group_epoch", "members"}, "private pub-sub scope")
    if value["profile"] != "syn.private-pubsub-scope/1":
        raise SynProtocolError("Unknown private pub-sub scope")
    uuid(value["workspace_id"])
    hex_bytes(value["session_id"], 16, "pub-sub session")
    hex_bytes(value["operator"], 32, "pub-sub operator")
    members = value["members"]
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_PEERS:
        raise SynProtocolError("Private pub-sub membership exceeds its bound")
    nodes = set()
    commitments = set()
    for certificate in members:
        if not isinstance(certificate, dict) or not isinstance(certificate.get("body"), dict):
            raise SynProtocolError("Private pub-sub membership has no canonical certificate")
        node = certificate["body"].get("node_public_key_hash")
        body = verify_group(certificate, expected_operator=value["operator"], expected_node=node,
                            expected_group=value["group_id"], expected_epoch=value["group_epoch"])
        if body["network_id"] != value["network_id"] or node in nodes:
            raise SynProtocolError("Private pub-sub membership is duplicate or outside its network")
        nodes.add(node)
        commitments.add(body["group_key_commitment"])
    if len(commitments) != 1:
        raise SynProtocolError("Private pub-sub members do not share the same current group commitment")
    return value


def group_hash(selected):
    """Public discovery identifier for this exact frozen pub-sub scope.

    The roster order is immaterial, but every certificate and the complete
    workspace/session/group/epoch context is committed. Publishing this hash
    does not grant membership or prove that a provider hosts an Agent.
    """
    selected = scope(selected)
    members = sorted(selected["members"], key=lambda cert: cert["body"]["node_public_key_hash"])
    return hashlib.sha256(canonical({**selected, "members": members})).hexdigest()


def relay_peer_id(public_key):
    """Standard libp2p PeerID for a raw Ed25519 relay public key.

    This only encodes the identity multihash of the 36-byte PublicKey protobuf;
    the public client has no libp2p runtime dependency or transport secrets.
    """
    if type(public_key) is not bytes or len(public_key) != 32:
        raise SynProtocolError("Relay transport public key must be 32-byte Ed25519")
    raw = b"\x00\x24\x08\x01\x12\x20" + public_key
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number, encoded = int.from_bytes(raw, "big"), ""
    while number:
        number, digit = divmod(number, 58)
        encoded = alphabet[digit] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + encoded


def relay_proof_body(hello, selected, relay_public_key, *, now=None):
    """Build the fixed native attestation; no caller-chosen signing payload."""
    hello = exact(hello, {"body", "public_key", "certificate", "signature"}, "pub-sub hello")
    public = hex_bytes(hello["public_key"], 32, "pub-sub sender public key")
    body = verify_hello(hello, selected, expected_node=hashlib.sha256(public).hexdigest(), now=now)
    return {"profile": RELAY_BINDING_PROFILE, "group_hash": group_hash(selected),
            "relay_peer_id": relay_peer_id(relay_public_key), "relay_public_key": relay_public_key.hex(),
            "node": body["node"], "boot_id": body["boot_id"],
            "hello_sha256": hashlib.sha256(canonical(hello)).hexdigest(), "issued_at": body["issued_at"]}


def verify_relay_proof(packet, selected, *, expected_peer_id, now=None):
    """Authenticate a discovered relay's assignment using a selected Agent.

    ``expected_peer_id`` must come from the encrypted, authenticated stream.
    Returning the original signed hello preserves the existing ready roster
    and message verification. Callers still reject overlapping assignments and
    require the complete selected cohort before enabling delivery.
    """
    packet = exact(packet, {"hello", "binding"}, "pub-sub relay proof")
    binding = exact(packet["binding"], {"body", "signature"}, "pub-sub relay binding")
    body = exact(binding["body"], {"profile", "group_hash", "relay_peer_id", "relay_public_key",
                                  "node", "boot_id", "hello_sha256", "issued_at"}, "pub-sub relay binding body")
    relay_public = hex_bytes(body["relay_public_key"], 32, "relay transport public key")
    expected = relay_proof_body(packet["hello"], selected, relay_public, now=now)
    if (type(body["issued_at"]) is not int or body != expected or not isinstance(expected_peer_id, str)
            or body["relay_peer_id"] != expected_peer_id):
        raise SynProtocolError("Relay proof differs from its selected scope, live Agent or authenticated transport")
    public = hex_bytes(packet["hello"]["public_key"], 32, "pub-sub sender public key")
    _signature(public, hex_bytes(binding["signature"], 64, "relay binding signature"),
               RELAY_BINDING_DOMAIN + canonical(body))
    return packet["hello"]


def member(public, certificate, selected):
    raw = hex_bytes(public, 32, "pub-sub sender public key")
    node = hashlib.sha256(raw).hexdigest()
    body = verify_group(certificate, expected_operator=selected["operator"], expected_node=node,
                        expected_group=selected["group_id"], expected_epoch=selected["group_epoch"])
    if body["network_id"] != selected["network_id"] or certificate not in selected["members"]:
        raise SynProtocolError("Pub-sub sender is outside the independently selected roster")
    return raw, node


def verify_hello(packet, selected, *, expected_node, now=None):
    packet = exact(packet, {"body", "public_key", "certificate", "signature"}, "pub-sub hello")
    body = exact(packet["body"], {"profile", "workspace_id", "session_id", "node", "boot_id", "issued_at"}, "pub-sub hello body")
    public, node = member(packet["public_key"], packet["certificate"], selected)
    if (body["profile"] != "syn.private-pubsub-hello/1" or node != expected_node or body["node"] != node
            or body["workspace_id"] != selected["workspace_id"] or body["session_id"] != selected["session_id"]):
        raise SynProtocolError("Pub-sub hello belongs to a different node, workspace or session")
    hex_bytes(body["boot_id"], 16, "pub-sub boot")
    fresh(body["issued_at"], now)
    _signature(public, hex_bytes(packet["signature"], 64, "pub-sub hello signature"), HELLO_DOMAIN + canonical(body))
    return body


def fresh(issued, now=None):
    now = int(time.time()) if now is None else now
    if type(issued) is not int or not now - 60 <= issued <= now + 5:
        raise SynProtocolError("Pub-sub frame is stale or outside the accepted clock window")


def verify_message(packet, selected, *, expected_node=None, expected_boot=None, now=None):
    packet = exact(packet, {"header", "public_key", "certificate", "ciphertext", "signature"}, "encrypted pub-sub message")
    header = exact(packet["header"], {"profile", "workspace_id", "session_id", "node", "boot_id", "message_id",
        "sequence", "issued_at", "network_id", "group_id", "group_epoch", "topic", "plaintext_sha256"}, "pub-sub header")
    public, node = member(packet["public_key"], packet["certificate"], selected)
    if (header["profile"] != "syn.private-pubsub-message/1" or header["node"] != node
            or expected_node is not None and node != expected_node
            or expected_boot is not None and header["boot_id"] != expected_boot
            or any(header[name] != selected[name] for name in ("workspace_id", "session_id", "network_id", "group_id", "group_epoch"))):
        raise SynProtocolError("Pub-sub message differs from its node, boot or workspace scope")
    hex_bytes(header["boot_id"], 16, "pub-sub message boot")
    hex_bytes(header["plaintext_sha256"], 32, "pub-sub plaintext commitment")
    uuid(header["message_id"])
    if (type(header["sequence"]) is not int or not 1 <= header["sequence"] < 2**53
            or not isinstance(header["topic"], str) or re.fullmatch(r"(?:syn|synny)://type/[a-z0-9][a-z0-9-]{0,95}", header["topic"]) is None):
        raise SynProtocolError("Pub-sub sequence or exact typed topic is invalid")
    fresh(header["issued_at"], now)
    cipher = packet["ciphertext"]
    if not isinstance(cipher, str) or not 32 <= len(cipher) <= 2 * (MAX_PAYLOAD + 16) or len(cipher) % 2:
        raise SynProtocolError("Encrypted pub-sub payload exceeds its bound")
    hex_bytes(cipher, len(cipher) // 2, "encrypted pub-sub payload")
    unsigned = {key: packet[key] for key in ("header", "public_key", "certificate", "ciphertext")}
    _signature(public, hex_bytes(packet["signature"], 64, "pub-sub signature"), MESSAGE_DOMAIN + canonical(unsigned))
    return header


def verify_reply(packet, request, selected, *, expected_node, expected_boot=None):
    packet = exact(packet, {"reply", "public_key", "signature"}, "pub-sub control reply")
    body = exact(packet["reply"], {"profile", "challenge", "request_sha256", "operation", "node", "boot_id",
                                 "workspace_id", "session_id", "result"}, "pub-sub reply body")
    public = hex_bytes(packet["public_key"], 32, "pub-sub reply public key")
    if (body["profile"] != "syn.private-pubsub-reply/1" or body["challenge"] != request["challenge"]
            or body["request_sha256"] != hashlib.sha256(canonical(request)).hexdigest()
            or body["operation"] != request["operation"] or body["node"] != expected_node
            or hashlib.sha256(public).hexdigest() != expected_node
            or expected_boot is not None and body["boot_id"] != expected_boot
            or body["workspace_id"] != selected["workspace_id"] or body["session_id"] != selected["session_id"]):
        raise SynProtocolError("Pub-sub reply is not fresh or bound to the selected live node")
    hex_bytes(body["boot_id"], 16, "pub-sub reply boot")
    _signature(public, hex_bytes(packet["signature"], 64, "pub-sub reply signature"), REPLY_DOMAIN + canonical(body))
    result = body["result"]
    if request["operation"] == "status":
        exact(result, {"connected", "peers", "custody", "volatile", "cog_execution_authorized"}, "pub-sub status")
        if (result["connected"] is not True or result["custody"] != "native"
                or result["volatile"] is not True or result["cog_execution_authorized"] is not False
                or result["peers"] != sorted(item["body"]["node_public_key_hash"] for item in selected["members"])):
            raise SynProtocolError("Pub-sub status does not prove its complete selected cohort")
    elif request["operation"] == "publish":
        exact(result, {"message_sha256", "sequence", "relay_accepted", "peer_reception_proved"}, "pub-sub publication")
        hex_bytes(result["message_sha256"], 32, "pub-sub publication commitment")
        if (type(result["sequence"]) is not int or not 1 <= result["sequence"] < 2**53
                or result["relay_accepted"] is not True or result["peer_reception_proved"] is not False):
            raise SynProtocolError("Pub-sub publication is not a bounded relay acknowledgement")
    elif request["operation"] == "receive":
        exact(result, {"messages"}, "received pub-sub messages")
        if not isinstance(result["messages"], list) or len(result["messages"]) > 2:
            raise SynProtocolError("Pub-sub receive batch exceeds its bound")
        for entry in result["messages"]:
            exact(entry, {"envelope", "payload"}, "decrypted pub-sub receipt")
            header = verify_message(entry["envelope"], selected)
            if (not isinstance(entry["payload"], dict)
                    or hashlib.sha256(canonical(entry["payload"])).hexdigest() != header["plaintext_sha256"]):
                raise SynProtocolError("Received plaintext differs from its sender's commitment")
    else:
        raise SynProtocolError("Unknown pub-sub reply operation")
    return body
