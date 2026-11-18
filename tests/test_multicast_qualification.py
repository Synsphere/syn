"""Authenticated multicast wire tests are offline and require no sockets."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qualify_syn_multicast as q

KEY = b"a" * 32
NONCE = "ab" * 16
GROUP = q.DEFAULT_GROUP
PORT = q.DEFAULT_PORT


def packet(kind="challenge", **kwargs):
    return q.make_packet(KEY, kind=kind, group=GROUP, port=PORT, nonce=NONCE, **kwargs)


def test_signed_witness_request_and_response_bind_path_and_nonce():
    message = packet()
    assert q.verify_packet(KEY, message, kind="challenge", group=GROUP, port=PORT)["nonce"] == NONCE
    response = packet("receipt")
    assert q.verify_packet(KEY, response, kind="receipt", group=GROUP, port=PORT, nonce=NONCE)
    for changes in ({"kind": "receipt"}, {"group": "239.255.99.98"},
                    {"port": PORT + 1}, {"nonce": "cd" * 16}):
        with pytest.raises(ValueError, match="InvalidWitnessPacket"):
            q.verify_packet(KEY, message, kind=changes.get("kind", "challenge"),
                            group=changes.get("group", GROUP), port=changes.get("port", PORT),
                            nonce=changes.get("nonce"))
    with pytest.raises(ValueError):
        q.verify_packet(b"b" * 32, message, kind="challenge", group=GROUP, port=PORT)


def test_packets_reject_old_timestamp_and_tampered_mac():
    old = packet(when=int(time.time()) - 120)
    with pytest.raises(ValueError):
        q.verify_packet(KEY, old, kind="challenge", group=GROUP, port=PORT)
    changed = packet().replace(b'"challenge"', b'"receipt"')
    with pytest.raises(ValueError):
        q.verify_packet(KEY, changed, kind="receipt", group=GROUP, port=PORT)
    with pytest.raises(ValueError):
        q.verify_packet(KEY, packet() + b"x", kind="challenge", group=GROUP, port=PORT)
    with pytest.raises(ValueError):
        q.verify_packet(KEY, b"x" * (q.MAX_WIRE + 1), kind="challenge", group=GROUP, port=PORT)


def test_reject_nonprivate_secret_and_symlink(tmp_path):
    path = tmp_path / "secret.hex"
    path.write_text(KEY.hex(), encoding="ascii")
    if os.name != "nt":
        path.chmod(0o600)
    assert q.read_secret(path) == KEY
    link = tmp_path / "alias"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        q.read_secret(link)
    if os.name != "nt":
        path.chmod(0o644)
        with pytest.raises(ValueError):
            q.read_secret(path)


def test_only_multicast_and_explicit_lan_interfaces():
    assert q.validate_route("224.0.0.251", "192.168.1.10", 5353)[0] == "224.0.0.251"
    for group in ("127.0.0.1", "8.8.8.8", "255.255.255.255"):
        with pytest.raises(ValueError):
            q.validate_route(group, "192.168.1.10", PORT)
    with pytest.raises(ValueError):
        q.validate_route(GROUP, "127.0.0.1", PORT)
    assert q.validate_route(GROUP, "127.0.0.1", PORT, allow_loopback=True)
    with pytest.raises(ValueError):
        q.validate_route(GROUP, "192.168.1.10", 0)


@pytest.mark.skipif(os.getenv("SYN_TEST_MULTICAST_LOOPBACK") != "1",
                    reason="Opt-in local UDP multicast socket smoke test")
def test_real_socket_receipt_loopback():
    import socket
    import threading
    # Local test only; separate-host receipt needs two different machines.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("0.0.0.0", 0))
        port = s.getsockname()[1]
    if port < 1024:
        pytest.skip("Ephemeral privileged port")
    results = []
    def listener():
        results.append(q.receive_once(KEY, group=GROUP, interface="127.0.0.1", port=port, timeout=3))
    worker = threading.Thread(target=listener, daemon=True)
    worker.start()
    time.sleep(0.2)
    assert q.send_once(KEY, group=GROUP, interface="127.0.0.1", port=port, timeout=3)
    worker.join(3)
    assert results == [True]
