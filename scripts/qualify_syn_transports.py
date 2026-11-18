#!/usr/bin/env python3
"""Qualify Syn's real UDP transports from one independent witness vantage.

Runs the existing *pinned* libp2p witness once per trial/transport. No sockets,
identities, custody, router mappings or API state are modified by this driver.
A passing batch does not prove universal reachability or Operator identity.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

KINDS = ("quic-v1", "webrtc-direct", "webtransport")
PROFILE = "syn.cross-network-qualification/1"
MAX_TARGETS = 3
MAX_INPUT_BYTES = 3 * 1024
MAX_WITNESS_BYTES = 8192
WITNESS = Path(__file__).with_name("verify_syn_transport.py")
_SAFE_ID = re.compile(r"[A-Za-z0-9]{8,128}\Z", re.ASCII)
_SAFE_HASH = re.compile(r"[A-Za-z0-9_-]{8,200}\Z", re.ASCII)
_SAFE_REASON = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}\Z", re.ASCII)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)


@dataclass(frozen=True)
class Target:
    transport: str
    peer_id: str
    address: str


def parse_target(value: str, *, allow_private: bool = False) -> Target:
    """Fail closed on malformed or risky destinations; child validates multiaddr.

    The final witness additionally validates the *actual* peer-id, certificate
    hashes, DNS4 resolution and negotiated muxer, with no TCP fallback.
    """
    if type(value) is not str or not 0 < len(value) <= 640 or any(c.isspace() for c in value):
        raise ValueError("InvalidPinnedEndpoint")
    fields = value.split("/")
    if (len(fields) < 8 or fields[0] or fields[1] not in {"ip4", "ip6", "dns4"}
            or fields[3] != "udp" or fields[-2] != "p2p"
            or _SAFE_ID.fullmatch(fields[-1]) is None):
        raise ValueError("InvalidPinnedEndpoint")
    host_type, host, port = fields[1], fields[2], fields[4]
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535 or str(int(port)) != port:
        raise ValueError("InvalidPinnedEndpoint")
    if host_type == "dns4":
        if (len(host) > 253 or host != host.lower() or len(host.split(".")) < 2
                or any(_DNS_LABEL.fullmatch(label) is None for label in host.split("."))):
            raise ValueError("InvalidPinnedEndpoint")
        # The child resolves A records and rejects nonpublic addresses after DNS.
    else:
        try:
            ip = ipaddress.ip_address(host)
            if (ip.version != (4 if host_type == "ip4" else 6)
                    or str(ip) != host or ip.is_unspecified or ip.is_multicast
                    or ip.is_link_local or (not allow_private and not ip.is_global)):
                raise ValueError
        except ValueError:
            raise ValueError("PrivateOrNoncanonicalEndpoint") from None
    tail = fields[5:-2]
    if tail == ["quic-v1"]:
        kind = "quic-v1"
    elif len(tail) == 3 and tail[:2] == ["webrtc-direct", "certhash"]:
        kind = "webrtc-direct"
    elif (len(tail) >= 4 and len(tail) <= 10
          and tail[:2] == ["quic-v1", "webtransport"]
          and len(tail[2:]) % 2 == 0
          and all(tail[i] == "certhash" for i in range(2, len(tail), 2))):
        kind = "webtransport"
    else:
        raise ValueError("InvalidTransportEndpoint")
    if kind != "quic-v1" and any(_SAFE_HASH.fullmatch(tail[i]) is None
                                  for i in range(3 if kind == "webtransport" else 2,
                                                 len(tail), 2)):
        raise ValueError("InvalidCertificateHash")
    return Target(kind, fields[-1], value)


def targets_from_lines(lines: str, *, allow_private: bool = False) -> dict[str, Target]:
    if type(lines) is not str or len(lines.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("TooManyEndpoints")
    entries = [line.strip() for line in lines.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not entries or len(entries) > MAX_TARGETS:
        raise ValueError("ExpectedOneToThreePinnedEndpoints")
    targets: dict[str, Target] = {}
    peer = None
    for line in entries:
        target = parse_target(line, allow_private=allow_private)
        if target.transport in targets:
            raise ValueError("DuplicateTransport")
        if peer is not None and target.peer_id != peer:
            raise ValueError("MixedPeerIdentities")
        targets[target.transport] = target
        peer = target.peer_id
    return targets


def _failure_reason(output: str) -> str:
    try:
        data = json.loads(output) if len(output) <= MAX_WITNESS_BYTES else None
        reason = data.get("reason") if type(data) is dict else None
        if type(reason) is str and _SAFE_REASON.fullmatch(reason):
            return reason
    except (ValueError, TypeError):
        pass
    return "WitnessFailed"


def _witness_verified(output: str, target: Target) -> bool:
    if len(output) > MAX_WITNESS_BYTES:
        return False
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        return False
    return (type(data) is dict and data.get("verified") is True
            and data.get("transport") == target.transport
            and data.get("peerId") == target.peer_id
            and data.get("challengeVerified") is True
            and data.get("authenticatedTransport") is True
            and data.get("operatorIdentityVerified") is False
            and data.get("publicReachabilityProved") is False
            and data.get("scope") == "witness-vantage-only")


def witness_trial(target: Target, *, timeout: float, allow_private: bool,
                  run=subprocess.run) -> tuple[bool, str]:
    """Invoke verified transport witness; never interpret a bare dial as success."""
    command = [sys.executable, str(WITNESS), target.address, "--timeout", str(timeout)]
    if allow_private:
        command.append("--allow-private")
    try:
        # The verifier is a repository-controlled child, not a shell command.
        completed = run(command, capture_output=True, text=True, timeout=timeout + 12,
                        check=False, errors="replace")
    except subprocess.TimeoutExpired:
        return False, "WitnessTimeout"
    except OSError:
        return False, "WitnessUnavailable"
    if completed.returncode != 0:
        return False, _failure_reason(completed.stderr)
    if not _witness_verified(completed.stdout, target):
        return False, "InvalidWitnessEvidence"
    return True, "AuthenticatedChallenge"


def qualify(targets: dict[str, Target], *, trials: int = 3, min_success: int = 2,
            timeout: float = 10.0, required=KINDS, allow_private=False,
            trial=witness_trial) -> dict:
    if (type(trials) is not int or not 1 <= trials <= 5
            or type(min_success) is not int or not 1 <= min_success <= trials
            or not isinstance(timeout, (float, int)) or not 1 <= timeout <= 30):
        raise ValueError("InvalidQualificationBudget")
    if not targets or len(targets) > MAX_TARGETS:
        raise ValueError("InvalidPinnedEndpoints")
    required = tuple(required)
    if not required or len(set(required)) != len(required) or any(k not in KINDS for k in required):
        raise ValueError("InvalidRequiredTransports")
    results: dict[str, dict] = {}
    for kind in KINDS:
        target = targets.get(kind)
        if target is None:
            results[kind] = {"state": "not-tested", "successes": 0, "attempts": 0,
                             "required": kind in required, "reason": "NoPinnedEndpoint"}
            continue
        successes, reasons = 0, []
        for i in range(trials):
            ok, reason = trial(target, timeout=timeout, allow_private=allow_private)
            successes += int(ok)
            if not ok:
                reasons.append(reason if _SAFE_REASON.fullmatch(reason) else "WitnessFailed")
        results[kind] = {"state": "qualified" if successes >= min_success else "failed",
                         "successes": successes, "attempts": i + 1,
                         "required": kind in required,
                         "reason": None if successes >= min_success else (reasons[-1] if reasons else "InsufficientSuccesses")}
    passed = all(results[k]["state"] == "qualified" for k in required)
    return {"profile": PROFILE, "qualified": passed,
            "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scope": "single-witness-vantage-only", "required": list(required),
            "peerIdSha256": hashlib.sha256(next(iter(targets.values())).peer_id.encode("ascii")).hexdigest(),
            "publicReachabilityProved": False, "operatorIdentityVerified": False,
            "source": "pinned-libp2p-transport-challenge", "transports": results}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--peer", action="append", help="One canonical pinned public UDP multiaddr; up to three")
    source.add_argument("--peers-file", type=Path, help="UTF-8 file with one pinned multiaddr per line")
    source.add_argument("--peers-env", help="Environment variable holding newline-separated pinned multiaddrs")
    parser.add_argument("--require", nargs="+", default=list(KINDS), choices=KINDS)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--min-success", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--allow-private", action="store_true", help="Isolated LAN/namespace lab only")
    parser.add_argument("--report", type=Path, help="Write a JSON report; no raw endpoints or credentials")
    parser.add_argument("--summary", type=Path, help="Append a safe Markdown summary (GitHub Actions)")
    args = parser.parse_args(argv)
    try:
        if args.peer is not None:
            raw = "\n".join(args.peer)
        elif args.peers_env is not None:
            if args.peers_env != "SYN_PINNED_ENDPOINTS":
                raise ValueError("UnsupportedEndpointVariable")
            raw = os.environ.get(args.peers_env, "")
        else:
            if args.peers_file.is_symlink() or args.peers_file.stat().st_size > MAX_INPUT_BYTES:
                raise ValueError("InvalidEndpointFile")
            raw = args.peers_file.read_text(encoding="utf-8")
        selected = targets_from_lines(raw, allow_private=args.allow_private)
        result = qualify(selected, trials=args.trials, min_success=args.min_success,
                         timeout=args.timeout, required=args.require,
                         allow_private=args.allow_private)
    except (ValueError, OSError, UnicodeError) as exc:
        reason = str(exc)
        if not _SAFE_REASON.fullmatch(reason):
            reason = "InvalidQualificationInput"
        print(json.dumps({"profile": PROFILE, "qualified": False, "reason": reason},
                         sort_keys=True))
        return 2
    wire = json.dumps(result, sort_keys=True, indent=2) + "\n"
    print(wire, end="")
    if args.report is not None:
        # This is a diagnostic artifact, not an input configuration. No
        # endpoint strings, credentials or external error messages are copied.
        args.report.write_text(wire, encoding="utf-8")
    if args.summary is not None:
        lines = ["## Syn cross-network transport qualification", "",
                 "From this witness only; not global reachability or Operator authentication.", "",
                 "| Transport | Status | Successful probes |", "| --- | --- | --- |"]
        for kind, item in result["transports"].items():
            lines.append(f"| {kind} | {item['state']} | {item['successes']}/{item['attempts']} |")
        lines.append("")
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write("\n".join(lines))
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
