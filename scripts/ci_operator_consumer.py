"""Actual reviewed Potbot adapter against installed official Syn; no Syn import."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

from potbot.syn_release.contracts import Policy, canonical
from potbot.syn_release.operator import SynOperatorDeployment
from potbot.syn_release.store import SynReleaseStore, private_read


def arguments(args):
    return ["--store", str(args.store), "--policy", str(args.policy), "--operator-root", str(args.root)]


def start(args):
    environment = {name: value for name, value in os.environ.items()
                   if name in {"HOME", "PATH", "LANG", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}}
    return subprocess.Popen([sys.executable, "-I", "-B", "-m", "potbot", "syn-operator", "serve", *arguments(args)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=environment, start_new_session=True)


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        # A timeout is failure; do not hide a broken cleanup behind success.
        process.kill()
        process.wait(timeout=5)
        raise AssertionError("Potbot did not stop its operator child") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("store", "policy", "root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expect-locked", action="store_true")
    args = parser.parse_args()
    deployment = SynOperatorDeployment(SynReleaseStore(args.store), Policy.parse(private_read(args.policy)),
                                       args.root, policy_path=args.policy)
    if args.expect_locked:
        before = private_read(deployment.registration)
        config = private_read(deployment.state_dir / "operator.json")
        process = start(args)
        try:
            result = process.wait(timeout=40)
            assert result != 0
            lifecycle = json.loads(private_read(args.root / "lifecycle.json"))
            assert lifecycle["state"] == "fenced"
            assert private_read(deployment.registration) == before
            assert private_read(deployment.state_dir / "operator.json") == config
        finally:
            stop(process)
        print(json.dumps({"native_locked_store_refused": True, "identity_replacement_refused": True}))
        return
    created = deployment.bootstrap(new_identity=True, keyring_service="syn.ci." + secrets.token_hex(8),
                                   keyring_account="operator", network_id="ci-official")
    attestations = []
    for _ in range(2):
        process = start(args)
        try:
            deadline = time.monotonic() + 40
            while True:
                try:
                    lifecycle = json.loads(private_read(args.root / "lifecycle.json"))
                except FileNotFoundError:
                    lifecycle = {}
                if lifecycle.get("state") == "ready" and lifecycle.get("pid") is not None:
                    attachment = deployment.attach()
                    if attachment["attestation"]["status"]["pid"] != lifecycle["pid"]:
                        raise AssertionError("Potbot attachment does not match its owned Syn child")
                    assert attachment["process_owned"] is False
                    assert attachment["base_public_key_hash"] == created["base_public_key_hash"]
                    assert attachment["operator_ready"] is True
                    attestations.append(attachment)
                    break
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("Reviewed Potbot supervisor failed official Syn readiness")
                time.sleep(0.2)
        finally:
            stop(process)
        assert process.returncode == 0
        assert json.loads(private_read(args.root / "lifecycle.json"))["state"] == "stopped"
    assert attestations[0]["attestation"]["status"]["boot_id"] != attestations[1]["attestation"]["status"]["boot_id"]
    assert attestations[1]["attestation"]["status"]["boot"] > attestations[0]["attestation"]["status"]["boot"]
    print(canonical({"profile": "potbot.official-operator-acceptance/1", "result": "passed",
                     "manifest_sha256": deployment.policy.manifest_sha256, "same_identity_after_restart": True,
                     "authenticated_attachments": attestations, "agent_activation_authorized": False}).decode())


if __name__ == "__main__":
    main()
