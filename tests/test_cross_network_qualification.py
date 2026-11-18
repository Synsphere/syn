"""Offline witness orchestration tests: no libp2p, network or custody required."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qualify_syn_transports as q

PEER = "12D3KooW" + "x" * 44
OTHER = "12D3KooW" + "y" * 44
HASH = "uEi" + "A" * 46
ENDPOINTS = {
    "quic-v1": f"/ip4/8.8.8.8/udp/4001/quic-v1/p2p/{PEER}",
    "webrtc-direct": f"/ip4/8.8.8.8/udp/4002/webrtc-direct/certhash/{HASH}/p2p/{PEER}",
    "webtransport": f"/dns4/syn.example.org/udp/4003/quic-v1/webtransport/certhash/{HASH}/p2p/{PEER}",
}


def _good(target):
    return json.dumps({"verified": True, "transport": target.transport,
                       "peerId": target.peer_id, "challengeVerified": True,
                       "authenticatedTransport": True,
                       "operatorIdentityVerified": False,
                       "publicReachabilityProved": False,
                       "scope": "witness-vantage-only"})


def test_public_endpoints_classify_and_pin_same_identity():
    targets = q.targets_from_lines("\n".join(ENDPOINTS.values()))
    assert set(targets) == set(q.KINDS)
    assert all(x.peer_id == PEER for x in targets.values())
    assert targets["webtransport"].transport == "webtransport"
    assert q.parse_target(ENDPOINTS["webtransport"].replace("/p2p/", f"/certhash/{HASH}/p2p/")).transport == "webtransport"


@pytest.mark.parametrize("value", [
    "", "/ip4/8.8.8.8/tcp/4001/p2p/" + PEER,
    ENDPOINTS["quic-v1"].replace("udp/4001", "udp/0"),
    ENDPOINTS["quic-v1"].replace("udp/4001", "udp/04001"),
    ENDPOINTS["quic-v1"].replace("8.8.8.8", "127.0.0.1"),
    ENDPOINTS["quic-v1"].replace("8.8.8.8", "0.0.0.0"),
    ENDPOINTS["quic-v1"].replace("8.8.8.8", "224.0.0.251"),
    ENDPOINTS["quic-v1"].replace("8.8.8.8", "169.254.1.2"),
    ENDPOINTS["webtransport"].replace("syn.example.org", "localhost"),
    ENDPOINTS["webtransport"].replace("syn.example.org", "BAD.example.org"),
    ENDPOINTS["quic-v1"] + "/unexpected",
    ENDPOINTS["quic-v1"].replace("quic-v1", "webtransport"),
    ENDPOINTS["webrtc-direct"].replace(HASH, "!!!"),
])
def test_bad_or_private_destinations_are_refused(value):
    with pytest.raises(ValueError):
        q.parse_target(value)


def test_lab_only_private_opt_in_is_explicit():
    value = ENDPOINTS["quic-v1"].replace("8.8.8.8", "127.0.0.1")
    assert q.parse_target(value, allow_private=True).transport == "quic-v1"
    with pytest.raises(ValueError):
        q.parse_target(ENDPOINTS["quic-v1"].replace("8.8.8.8", "224.0.0.251"), allow_private=True)


def test_reject_duplicates_and_mixed_pinned_ids():
    with pytest.raises(ValueError, match="DuplicateTransport"):
        q.targets_from_lines(ENDPOINTS["quic-v1"] + "\n" + ENDPOINTS["quic-v1"])
    with pytest.raises(ValueError, match="MixedPeerIdentities"):
        q.targets_from_lines(ENDPOINTS["quic-v1"] + "\n" + ENDPOINTS["webrtc-direct"].replace(PEER, OTHER))
    with pytest.raises(ValueError, match="TooManyEndpoints"):
        q.targets_from_lines("x" * 4000)


def test_witness_requires_authenticated_peer_transport_and_challenge():
    target = q.parse_target(ENDPOINTS["quic-v1"])
    result = SimpleNamespace(returncode=0, stdout=_good(target), stderr="")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return result

    assert q.witness_trial(target, timeout=5, allow_private=False, run=runner) == (True, "AuthenticatedChallenge")
    assert calls[0][0][-2:] == ["--timeout", "5"]
    assert "--allow-private" not in calls[0][0]
    assert calls[0][1]["timeout"] == 17
    for change in ({"verified": "true"}, {"challengeVerified": False},
                   {"authenticatedTransport": False}, {"transport": "webtransport"},
                   {"peerId": OTHER}, {"operatorIdentityVerified": True},
                   {"publicReachabilityProved": True}, {"scope": "global"}):
        forged = json.loads(_good(target))
        forged.update(change)
        assert not q._witness_verified(json.dumps(forged), target)


def test_nonzero_failure_reason_is_bounded_and_does_not_leak_peer_output():
    target = q.parse_target(ENDPOINTS["quic-v1"])
    def rejected(*args, **kwargs):
        return SimpleNamespace(returncode=1, stderr=json.dumps({"reason": "DNS4NoPermittedAddresses"}), stdout="")
    assert q.witness_trial(target, timeout=3, allow_private=False, run=rejected) == (False, "DNS4NoPermittedAddresses")
    def hostile(*args, **kwargs):
        return SimpleNamespace(returncode=1, stderr='{"reason":"'+PEER+'secret"}', stdout="")
    assert q.witness_trial(target, timeout=3, allow_private=False, run=hostile) == (False, "WitnessFailed")


def test_three_transport_batch_retries_and_required_failures_are_honest():
    targets = q.targets_from_lines("\n".join(ENDPOINTS.values()))
    attempts = {k: 0 for k in targets}
    def trial(target, *, timeout, allow_private):
        attempts[target.transport] += 1
        return ((target.transport != "webrtc-direct" and attempts[target.transport] >= 2), "WitnessFailed")
    report = q.qualify(targets, trial=trial, trials=3, min_success=2)
    assert report["qualified"] is False
    assert report["transports"]["quic-v1"]["state"] == "qualified"
    assert report["transports"]["webtransport"]["state"] == "qualified"
    assert report["transports"]["webrtc-direct"]["state"] == "failed"
    assert report["publicReachabilityProved"] is False
    assert report["operatorIdentityVerified"] is False
    assert PEER not in json.dumps(report)
    assert all(target.address not in json.dumps(report) for target in targets.values())


def test_missing_required_transport_never_claimed_as_qualified():
    selected = q.targets_from_lines(ENDPOINTS["quic-v1"])
    report = q.qualify(selected, trials=1, min_success=1,
                       trial=lambda *args, **kwargs: (True, "AuthenticatedChallenge"))
    assert not report["qualified"]
    assert report["transports"]["webrtc-direct"]["state"] == "not-tested"
    assert q.qualify(selected, trials=1, min_success=1, required=("quic-v1",),
                     trial=lambda *args, **kwargs: (True, "AuthenticatedChallenge"))["qualified"]


def test_cli_environment_report_and_summary_without_raw_endpoint(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SYN_PINNED_ENDPOINTS", "\n".join(ENDPOINTS.values()))
    monkeypatch.setattr(q, "witness_trial", lambda *a, **kw: (True, "AuthenticatedChallenge"))
    # Default argument in qualify is intentionally injectable for pure tests.
    original = q.qualify
    def fake_qualify(targets, **kwargs):
        kwargs["trial"] = lambda *a, **kw: (True, "AuthenticatedChallenge")
        return original(targets, **kwargs)
    monkeypatch.setattr(q, "qualify", fake_qualify)
    report = tmp_path / "report.json"
    summary = tmp_path / "summary.md"
    assert q.main(["--peers-env", "SYN_PINNED_ENDPOINTS", "--trials", "1",
                   "--min-success", "1", "--report", str(report),
                   "--summary", str(summary)]) == 0
    output = capsys.readouterr().out
    assert json.loads(report.read_text())["qualified"] is True
    assert "| webtransport | qualified |" in summary.read_text()
    assert PEER not in output and PEER not in summary.read_text()


def test_cli_rejects_arbitrary_environment_name(capsys):
    assert q.main(["--peers-env", "AWS_SECRET_ACCESS_KEY"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "UnsupportedEndpointVariable"
