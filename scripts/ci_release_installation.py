"""Qualify an explicitly reviewed Potbot installer against the actual release.

Runs in private Syn CI; no release mutation, identity creation or node launch.
The reviewed consumer is installed by the workflow, never built or copied here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from potbot.syn_release.contracts import Policy, canonical
from potbot.syn_release.source import GitHubSource
from potbot.syn_release.store import SynReleaseStore

from potbot.domain.errors import ScopeDeniedError, StateMismatchError


def main() -> None:
    base = Path("/tmp") / (
        "syn-r02-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["R02_PYTHON"]
    )
    base.mkdir(mode=0o700)
    evidence = Path("artifacts/r02")
    evidence.mkdir(parents=True)
    policy_value = json.loads(Path("release/install-policies/v0.1.0-service.json").read_text())
    policy_value["python_version"] = os.environ["R02_PYTHON"]
    policy = Policy.parse(canonical(policy_value))
    # Consume the GitHub token only in the acquisition client, never in children.
    source = GitHubSource(os.environ.pop("POTBOT_SYN_GITHUB_TOKEN"))
    python = Path(os.environ["R02_HOST_PYTHON"])
    tools = base / "tools"
    tools.mkdir(mode=0o700)
    # setup-uv is reviewed and SHA-pinned, but the hosted tool-cache ancestors
    # are writable. Provision a private copy rather than weaken the installer.
    uv = tools / "uv"
    shutil.copy2(Path(shutil.which("uv") or "missing").resolve(strict=True), uv)
    uv.chmod(0o500)
    online = SynReleaseStore(base / "online")
    try:
        online._tool(python)
    except ScopeDeniedError:
        # uv's standalone CPython distribution is relocatable. Copy only that
        # exact interpreter prefix; never enumerate/copy /usr or a whole cache.
        prefix = Path(
            subprocess.check_output(
                [str(python), "-I", "-B", "-c", "import sys;print(sys.base_prefix)"],
                text=True,
                timeout=20,
            ).strip()
        ).resolve()
        if len(prefix.parts) < 4 or not python.resolve().is_relative_to(prefix):
            raise AssertionError("Refusing an ambiguous interpreter-prefix copy") from None
        private_python = tools / "python"
        shutil.copytree(prefix, private_python, symlinks=True)
        python = private_python / python.resolve().relative_to(prefix)
        online._tool(python)
    online._tool(uv)
    try:
        first = online.install(policy, python=python, uv=uv, source=source)
        assert first["node_qualified"] is False
        (evidence / "online.json").write_bytes(canonical(first))
        verified = online.verify(policy)
        (evidence / "verified.json").write_bytes(canonical(verified))
        private_policy = base / "policy.json"
        offline_policy_value = {
            **policy_value,
            "dependency_cache_sha256": first["dependency_cache_sha256"],
        }
        private_policy.write_bytes(canonical(offline_policy_value))
        private_policy.chmod(0o600)
        # A fresh environment and empty admission history, with retained payloads/dependencies.
        offline_root = base / "offline"
        offline_root.mkdir(mode=0o700)
        for name in ("objects", "bundles", "uv-cache"):
            shutil.copytree(online.root / name, offline_root / name, symlinks=True)
        shutil.copyfile(
            online.root / "dependency-cache-inventory.json",
            offline_root / "dependency-cache-inventory.json",
        )
        argv = [
            "sudo",
            "unshare",
            "--net",
            "--setgid",
            str(os.getgid()),
            "--setuid",
            str(os.getuid()),
            "--",
            sys.executable,
            "-m",
            "potbot.syn_release.cli",
            "install",
            "--offline",
            "--store",
            str(offline_root),
            "--policy",
            str(private_policy),
            "--python",
            str(python),
            "--uv",
            str(uv),
        ]
        outcome = subprocess.run(argv, capture_output=True, timeout=900, check=True)
        offline = json.loads(outcome.stdout)
        assert offline["acquisition"] == "offline_retained_signature_verified"
        assert (
            offline["manifest_sha256"] == policy.manifest_sha256
            and offline["node_qualified"] is False
        )
        (evidence / "offline-network-disabled.json").write_bytes(canonical(offline))
        # The official client wheel also installs without dependencies or a node claim.
        client_policy = Policy.parse(
            canonical({**policy_value, "component": "client", "mode": None})
        )
        client = online.install(client_policy, python=python, uv=uv, offline=True)
        assert client["observed"]["runtime"] is None
        (evidence / "client.json").write_bytes(canonical(client))
        for mode in ("operator", "agent"):
            unsupported = Policy.parse(canonical({**policy_value, "mode": mode}))
            try:
                online.install(unsupported, python=python, uv=uv, offline=True)
            except ScopeDeniedError:
                pass
            else:
                raise AssertionError("Service package admitted an unsupported node mode")
        try:
            Policy.parse(
                canonical({**policy_value, "revoked_keys": [policy_value["release_key_sha256"]]})
            )
        except ScopeDeniedError:
            pass
        else:
            raise AssertionError("Revoked release authority was admitted")
        # Alter only a private copied cache, never an upstream immutable asset.
        altered_root = base / "tampered"
        altered_root.mkdir(mode=0o700)
        for name in ("objects", "bundles"):
            shutil.copytree(online.root / name, altered_root / name)
        altered = SynReleaseStore(altered_root)
        asset = next((altered_root / "objects").iterdir())
        asset.chmod(0o600)
        asset.write_bytes(b"tampered private cache fixture")
        try:
            altered.fetch(policy, offline=True)
        except StateMismatchError:
            pass
        else:
            raise AssertionError("Altered official payload cache was admitted")
        (evidence / "acceptance.json").write_bytes(
            canonical(
                {
                    "qualification": "official_package_installation_only",
                    "published_manifest_sha256": policy.manifest_sha256,
                    "consumer_sha": os.environ["R02_POTBOT_SHA"],
                    "python": policy_value["python_version"],
                    "online": True,
                    "fresh_offline_network_disabled": True,
                    "client": True,
                    "tamper_refused": True,
                    "revocation_refused": True,
                    "unsupported_node_modes_refused": True,
                    "node_qualified": False,
                }
            )
        )
        print((evidence / "acceptance.json").read_text())
    finally:
        for log in (base / "online").glob("process-*.log"):
            shutil.copyfile(log, evidence / log.name)
            print(
                log.read_text(errors="replace")[-6000:]
            )  # Child env carries no GitHub token or signing key.


if __name__ == "__main__":
    main()
