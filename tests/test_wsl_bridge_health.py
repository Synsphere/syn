"""Offline verification of the read-only signed bridge check."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("test_syn_wsl_bridge_health", ROOT / "scripts/verify_syn_wsl_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def test_bridge_status_uses_only_readonly_signed_action(monkeypatch, capsys):
    actions = []
    class Client:
        def __init__(self, host, port, secret, *, timeout):
            assert (host, port, secret, timeout) == ("172.20.1.1", 48777, b"private", 3)
        def call(self, action):
            actions.append(action)
            return {"externalIp": "203.0.113.25", "mappingVerified": False,
                    "publicReachability": "unverified"}
    monkeypatch.setattr(bridge, "secret_file", lambda _: b"private")
    monkeypatch.setattr(bridge, "BridgeClient", Client)
    assert bridge.main(["--host", "172.20.1.1", "--secret-file", "private.hex"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert actions == [{"action": "external-ip"}]
    assert result["state"] == "authenticated-control-reachable"
    assert result["publicIngress"] == "not-verified"
    assert "203.0.113" not in json.dumps(result)


def test_bridge_failure_is_redacted(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "secret_file", lambda _: b"private")
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def call(self, action):
            raise bridge.BridgeError("sensitive address and secret")
    monkeypatch.setattr(bridge, "BridgeClient", Client)
    assert bridge.main(["--host", "172.20.1.1", "--secret-file", "private.hex"]) == 2
    assert "sensitive" not in capsys.readouterr().out


def test_bridge_rejects_a_reply_that_claims_unverified_ingress_is_verified(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "secret_file", lambda _: b"private")
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def call(self, action):
            return {"mappingVerified": True, "publicReachability": "verified"}
    monkeypatch.setattr(bridge, "BridgeClient", Client)
    assert bridge.main(["--host", "172.20.1.1", "--secret-file", "private.hex"]) == 2
    assert json.loads(capsys.readouterr().out)["state"] == "unavailable"
