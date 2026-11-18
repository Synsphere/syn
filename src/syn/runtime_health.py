"""Bounded, read-only localhost health verification of an already-running Syn.

A listener, a registered transport, router mapping readback, or a DNS4 setting
is NOT evidence of Internet ingress. External QUIC/WebRTC/WebTransport must be
verified separately from another network using verify_syn_transport.py.

No credentials, addresses, peer IDs, user-provided strings, or raw HTTP errors
are copied into the report. This command does not start Syn or mutate a bridge.
"""
from __future__ import annotations

import http.client
import json
import math
from typing import Any

PROTOCOL = "syn.runtime-health/1"
MAX_RESPONSE = 256 * 1024
ROUTES = {
    "api": "/v1/capabilities",
    "p2p": "/p2p/status",
    "cardano": "/cardano/status",
    "bluetooth": "/bluetooth/status",
}
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
UDP_LISTENERS = {
    "quic": "quicListeners",
    "webRtcDirect": "webRtcListeners",
    "webTransport": "webTransportListeners",
}


def _fetch(host: str, port: int, route: str, timeout: float) -> tuple[dict | None, str]:
    """Never follow redirects, use proxies, or print untrusted server content."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", route, headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            return None, "http-error"
        length = response.getheader("Content-Length")
        if length is not None:
            try:
                if int(length) < 0 or int(length) > MAX_RESPONSE:
                    return None, "oversized-response"
            except ValueError:
                return None, "invalid-response"
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            return None, "oversized-response"
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeError):
            return None, "invalid-response"
        if type(obj) is not dict:
            return None, "invalid-response"
        return obj, "ok"
    except (OSError, TimeoutError, http.client.HTTPException, UnicodeError):
        return None, "unreachable"
    finally:
        connection.close()


def _capability(state: object, listeners: object = None) -> dict[str, str]:
    if state == "disabled":
        return {"state": "disabled", "evidence": "runtime-reported"}
    if state == "missing-optional-dependency":
        return {"state": "dependency-missing", "evidence": "runtime-reported"}
    if state != "enabled":
        return {"state": "unknown", "evidence": "runtime-reported"}
    if listeners is None:
        return {"state": "configured-not-probed", "evidence": "runtime-reported"}
    if isinstance(listeners, list) and any(type(v) is str for v in listeners):
        return {"state": "locally-listening", "evidence": "runtime-reported"}
    return {"state": "no-listener", "evidence": "runtime-reported"}


def _subsystem(value: dict | None, label: str, *, running_required: bool = True) -> dict[str, str]:
    if value is None:
        return {"state": "unavailable", "evidence": "http-request"}
    if value.get("enabled") is False:
        return {"state": "disabled", "evidence": "runtime-reported"}
    if value.get("enabled") is not True:
        return {"state": "unknown", "evidence": "runtime-reported"}
    if running_required and value.get("running") is not True:
        return {"state": "not-running", "evidence": "runtime-reported"}
    if label == "bluetooth" and value.get("state") == "failed":
        return {"state": "failed", "evidence": "runtime-reported"}
    if label == "bluetooth" and not value.get("adapters"):
        return {"state": "no-os-visible-adapter", "evidence": "runtime-reported"}
    return {"state": "running", "evidence": "runtime-reported"}


def classify(snapshots: dict[str, dict | None], errors: dict[str, str] | None = None) -> dict[str, Any]:
    """Interpret actual Syn status shapes; never infer reachability from flags."""
    errors = errors or {}
    api, p2p = snapshots.get("api"), snapshots.get("p2p")
    checks: dict[str, Any] = {}
    checks["api"] = {"state": "responding" if api is not None else "unavailable",
                     "evidence": "http-200" if api is not None else errors.get("api", "unavailable")}
    checks["p2p"] = _subsystem(p2p, "p2p")
    if p2p is not None and p2p.get("enabled") is True and checks["p2p"]["state"] == "running":
        if not p2p.get("peerId") or not isinstance(p2p.get("listenAddresses"), list) or not p2p.get("listenAddresses"):
            checks["p2p"] = {"state": "missing-listener-identity", "evidence": "runtime-reported"}

    caps = p2p.get("transportCapabilities") if isinstance(p2p, dict) else {}
    if not isinstance(caps, dict):
        caps = {}
    transports: dict[str, dict[str, str]] = {}
    for key in ("tcp", "quic", "webRtcDirect", "webTransport", "webSocket", "dns4"):
        # A disabled P2P host cannot claim working transports.
        if p2p is None or p2p.get("enabled") is not True:
            transports[key] = {"state": "disabled" if p2p is not None and p2p.get("enabled") is False else "unknown",
                               "evidence": "runtime-reported"}
        elif key in UDP_LISTENERS:
            transports[key] = _capability(caps.get(key), p2p.get(UDP_LISTENERS[key], []))
        elif key == "tcp":
            listeners = p2p.get("listenAddresses", [])
            tcp = [v for v in listeners if type(v) is str and "/tcp/" in v] if isinstance(listeners, list) else []
            transports[key] = _capability(caps.get(key), tcp)
        else:
            transports[key] = _capability(caps.get(key))
    checks["transports"] = transports

    upnp = p2p.get("upnp") if isinstance(p2p, dict) else None
    upnp = upnp if isinstance(upnp, dict) else {}
    mappings = upnp.get("mappings")
    verified = (sum(1 for m in mappings if isinstance(m, dict) and m.get("verified") is True)
                if isinstance(mappings, list) else 0)
    if p2p is not None and (p2p.get("enabled") is False or p2p.get("upnpEnabled") is False):
        upnp_state = "disabled"
    elif verified:
        upnp_state = "mapping-readback-verified"
    elif upnp.get("state") in {"pending", "waiting", "discovering"}:
        upnp_state = "pending"
    elif upnp.get("state") in {"unavailable", "disabled"}:
        upnp_state = "unavailable" if upnp.get("state") == "unavailable" else "disabled"
    else:
        upnp_state = "no-verified-mapping"
    checks["upnp"] = {"state": upnp_state, "publicIngress": "not-probed",
                      "evidence": "mapping-readback" if verified else "runtime-reported"}
    checks["mdns"] = {"state": ("configured-not-probed" if p2p and p2p.get("enabled") is True and p2p.get("mdnsEnabled") is True
                                 else "disabled" if p2p and (p2p.get("mdnsEnabled") is False or p2p.get("enabled") is False) else "unknown"),
                      "evidence": "runtime-reported"}

    environment = upnp.get("environment")
    environment = environment if isinstance(environment, dict) else {}
    mode = environment.get("networkingMode")
    mode = mode if mode in {"nat", "virtioproxy", "mirrored", "bridged", "none", "unknown"} else "unknown"
    wsl = environment.get("wsl") is True
    checks["wslBridge"] = {"state": "external-bridge-check-required" if wsl and mode in {"nat", "virtioproxy", "none"} else "not-probed",
                           "networkingMode": mode if wsl else "not-wsl",
                           "evidence": "network-mode-only"}

    checks["cardano"] = _subsystem(snapshots.get("cardano"), "cardano")
    checks["bluetooth"] = _subsystem(snapshots.get("bluetooth"), "bluetooth")
    # A live API/host is essential; hardware absence, external reachability and
    # upstream outages must not fence local API readiness.
    failed = checks["api"]["state"] != "responding" or checks["p2p"]["state"] in {
        "unavailable", "not-running", "missing-listener-identity", "unknown"
    }
    degraded = any(item["state"] in {"dependency-missing", "no-listener", "unknown"}
                   for item in transports.values())
    degraded = degraded or any(checks[name]["state"] in {
        "unavailable", "not-running", "no-os-visible-adapter", "failed", "unknown"
    } for name in ("cardano", "bluetooth"))
    degraded = degraded or (upnp_state in {"unavailable", "no-verified-mapping"}
                            and p2p is not None and p2p.get("upnpEnabled") is True)
    # The networking-mode advisory is not a failure: only remote witnesses can
    # verify ingress. P2P status may legitimately be disabled by user policy.
    return {"schema": PROTOCOL, "overall": "failed" if failed else "degraded" if degraded else "ready",
            "checks": checks, "remoteIngress": "not-verified"}


def probe(host: str = "127.0.0.1", port: int = 8000, timeout: float = 2.0) -> dict[str, Any]:
    """All HTTP calls stay on loopback and each has a bounded timeout/size."""
    if host not in LOOPBACK or type(port) is not int or not 1 <= port <= 65535 or (
            type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 10.0):
        return {"schema": PROTOCOL, "overall": "failed", "checks": {"api": {
            "state": "invalid-probe-parameters", "evidence": "loopback-only"}},
            "remoteIngress": "not-verified"}
    snapshots: dict[str, dict | None] = {}
    errors: dict[str, str] = {}
    for name, route in ROUTES.items():
        snapshots[name], errors[name] = _fetch(host, port, route, timeout)
    return classify(snapshots, errors)


def run(host: str = "127.0.0.1", port: int = 8000, timeout: float = 2.0,
        strict: bool = False) -> int:
    result = probe(host, port, timeout)
    print(json.dumps(result, sort_keys=True), flush=True)
    if result["overall"] == "failed":
        return 2
    if strict and result["overall"] != "ready":
        return 1
    return 0
