"""Offline, loopback-only health verification; no database or LAN required."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from threading import Thread
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syn.runtime_health import MAX_RESPONSE, ROUTES, classify, probe, run


def _snapshot(*, webrtc="enabled", mapped=True, bluetooth_adapters=True):
    p2p = {
        "enabled": True, "running": True, "peerId": "secret-not-for-output",
        "listenAddresses": ["/ip4/0.0.0.0/tcp/4222", "/ip4/0.0.0.0/udp/4222/quic-v1"],
        "transportCapabilities": {"tcp": "enabled", "quic": "enabled", "webRtcDirect": webrtc,
                                  "webTransport": "enabled", "webSocket": "enabled", "dns4": "enabled"},
        "quicListeners": ["/ip4/0.0.0.0/udp/4222/quic-v1"],
        "webRtcListeners": (["/ip4/0.0.0.0/udp/0/webrtc-direct"] if webrtc == "enabled" else []),
        "webTransportListeners": ["/ip4/0.0.0.0/udp/0/quic-v1/webtransport"],
        "upnpEnabled": True, "mdnsEnabled": True,
        "upnp": {"state": "mapped", "mappings": ([{"verified": True}] if mapped else []),
                 "environment": {"wsl": True, "networkingMode": "nat"}},
        "database_url": "postgres://user:password@localhost/syn",
    }
    return {"api": {"version": "secret-not-reported"}, "p2p": p2p,
            "cardano": {"enabled": True, "running": True, "peers": [{"private": "unused"}]},
            "bluetooth": {"enabled": True, "running": True,
                          "state": "listening", "adapters": ([{"id": "sensitive"}] if bluetooth_adapters else [])}}


def test_all_transports_are_local_evidence_not_public_proof():
    report = classify(_snapshot())
    assert report["overall"] == "ready"
    assert report["remoteIngress"] == "not-verified"
    assert report["checks"]["transports"]["quic"]["state"] == "locally-listening"
    assert report["checks"]["transports"]["webRtcDirect"]["state"] == "locally-listening"
    assert report["checks"]["transports"]["webTransport"]["state"] == "locally-listening"
    assert report["checks"]["transports"]["dns4"]["state"] == "configured-not-probed"
    assert report["checks"]["upnp"]["state"] == "mapping-readback-verified"
    assert report["checks"]["upnp"]["publicIngress"] == "not-probed"
    assert report["checks"]["mdns"]["state"] == "configured-not-probed"
    assert report["checks"]["wslBridge"]["state"] == "external-bridge-check-required"
    assert "password" not in json.dumps(report)
    assert "secret-not-for-output" not in json.dumps(report)


def test_unavailable_hardware_and_optional_dependency_degrade_not_fence():
    report = classify(_snapshot(webrtc="missing-optional-dependency", mapped=False,
                                bluetooth_adapters=False))
    assert report["overall"] == "degraded"
    assert report["checks"]["transports"]["webRtcDirect"]["state"] == "dependency-missing"
    assert report["checks"]["bluetooth"]["state"] == "no-os-visible-adapter"
    assert report["checks"]["upnp"]["state"] == "no-verified-mapping"


def test_explicit_disabled_features_stay_disabled():
    sample = _snapshot()
    sample["p2p"].update(enabled=False, running=False, mdnsEnabled=False, upnpEnabled=False)
    sample["cardano"].update(enabled=False, running=False)
    sample["bluetooth"].update(enabled=False, running=False)
    report = classify(sample)
    assert report["overall"] == "ready"
    assert report["checks"]["p2p"]["state"] == "disabled"
    assert report["checks"]["upnp"]["state"] == "disabled"
    assert all(item["state"] == "disabled" for item in report["checks"]["transports"].values())


def test_stopped_p2p_is_fatal_for_local_readiness():
    sample = _snapshot()
    sample["p2p"]["running"] = False
    assert classify(sample)["overall"] == "failed"
    sample["p2p"]["running"] = True
    sample["p2p"]["peerId"] = None
    assert classify(sample)["overall"] == "failed"


def test_missing_api_is_fatal_without_leaking_errors():
    sample = _snapshot()
    sample["api"] = None
    report = classify(sample, {"api": "http-error"})
    assert report["overall"] == "failed"
    assert report["checks"]["api"]["evidence"] == "http-error"


def test_invalid_host_cannot_probe_external_destinations(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network must not be used")
    monkeypatch.setattr("syn.runtime_health._fetch", forbidden)
    for host in ("0.0.0.0", "8.8.8.8", "evil.example", "127.0.0.2", "[::1]"):
        assert probe(host)["overall"] == "failed"
    assert probe(timeout=float("nan"))["overall"] == "failed"
    assert probe(port=True)["overall"] == "failed"


def _serve(routes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            response = routes.get(self.path, (404, b""))
            code, payload = response
            self.send_response(code)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    return http, thread


def test_real_loopback_http_probe_and_exit_codes(capsys):
    sample = _snapshot()
    routes = {ROUTES[k]: (200, json.dumps(v).encode()) for k, v in sample.items()}
    http, thread = _serve(routes)
    try:
        assert run(port=http.server_port) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["overall"] == "ready"
        assert "secret-not-for-output" not in json.dumps(output)
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def test_503_does_not_mask_fenced_startup():
    sample = _snapshot()
    routes = {ROUTES[k]: (200, json.dumps(v).encode()) for k, v in sample.items()}
    routes[ROUTES["p2p"]] = (503, b'{"detail":"secret host error"}')
    http, thread = _serve(routes)
    try:
        report = probe(port=http.server_port)
        assert report["overall"] == "failed"
        assert "secret host error" not in json.dumps(report)
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def test_http_response_size_and_json_are_bounded():
    sample = _snapshot()
    routes = {ROUTES[k]: (200, json.dumps(v).encode()) for k, v in sample.items()}
    routes[ROUTES["p2p"]] = (200, b"x" * (MAX_RESPONSE + 1))
    http, thread = _serve(routes)
    try:
        assert probe(port=http.server_port)["overall"] == "failed"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def test_strict_health_exit_distinguishes_degraded(monkeypatch, capsys):
    monkeypatch.setattr("syn.runtime_health.probe", lambda *args: {"overall": "degraded"})
    assert run() == 0
    assert run(strict=True) == 1
    assert capsys.readouterr().out.count('"overall": "degraded"') == 2


def test_syn_server_runtime_switch_is_early_and_never_initializes_operator(monkeypatch):
    from syn import server
    with patch("syn.runtime_health.run", return_value=0) as health:
        with pytest.raises(SystemExit) as result:
            server.main(["--check-runtime", "--port", "8001", "--strict-runtime"])
        assert result.value.code == 0
        health.assert_called_once_with("127.0.0.1", 8001, 2.0, True)


def test_syn_server_refuses_conflicting_maintenance_actions():
    from syn import server
    with pytest.raises(ValueError, match="cannot be combined"):
        server.main(["--check-runtime", "--check-config"])
