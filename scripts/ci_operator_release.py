"""Acquire exact published packages, then qualify their real operator with Potbot."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tarfile
from private_interpreter import copy_private_python

from potbot.domain.errors import ScopeDeniedError
from potbot.syn_release.contracts import Policy, canonical
from potbot.syn_release.source import GitHubSource
from potbot.syn_release.store import SynReleaseStore


def main():
    root = Path(__file__).resolve().parents[1]
    base = Path(tempfile.mkdtemp(prefix="syn-official-", dir=Path("/tmp").resolve())).resolve()
    policy_value = json.loads((root / "release/install-policies/v0.1.0-service.json").read_text())
    policy_value.update(version=os.environ["R03_VERSION"], minimum_version=os.environ["R03_VERSION"],
                        source_commit=os.environ["R03_RELEASE_SOURCE"], manifest_sha256=os.environ["R03_MANIFEST"],
                        mode="operator", python_version=os.environ["R03_PYTHON"],
                        required_profiles=["syn.operator-local/1"],
                        required_capabilities=["authenticated-operator-ipc", "native-os-operator-custody", "retained-operator-identity"])
    verify_recovery = os.environ.get("R03_VERIFY_RECOVERY") == "true"
    if verify_recovery:
        policy_value["required_profiles"].append("syn.operator-recovery/1")
        policy_value["required_capabilities"].append("native-operator-recovery")
        policy_value["required_capabilities"].sort()
    verify_legacy_archive = os.environ.get("R03_VERIFY_LEGACY_ARCHIVE") == "true"
    if verify_legacy_archive:
        if verify_recovery or os.environ.get("R03_EXPORT_TRANSFER") == "true":
            raise AssertionError("Select archive qualification separately")
        policy_value["required_profiles"].append("syn.legacy-state-archive/1")
        policy_value["required_capabilities"].append("read-only-legacy-state-archive")
        policy_value["required_profiles"].sort()
        policy_value["required_capabilities"].sort()
    policy = Policy.parse(canonical(policy_value))
    policy_path = base / "policy.json"
    policy_path.write_bytes(canonical(policy_value))
    policy_path.chmod(0o600)
    source = GitHubSource(os.environ.pop("POTBOT_SYN_GITHUB_TOKEN"))
    store = SynReleaseStore(base / "packages")
    tools = base / "tools"
    tools.mkdir(mode=0o700)
    uv = tools / "uv"
    shutil.copy2(Path(shutil.which("uv") or "missing").resolve(strict=True), uv)
    uv.chmod(0o500)
    python = Path(os.environ["R03_HOST_PYTHON"])
    try:
        store._tool(python)
    except ScopeDeniedError:
        prefix = Path(subprocess.check_output([str(python), "-I", "-B", "-c", "import sys;print(sys.base_prefix)"],
                                             text=True, timeout=20).strip()).resolve()
        python = copy_private_python(python, prefix, tools / "python")
        store._tool(python)
    installed = store.install(policy, python=python, uv=uv, source=source)
    verified = store.verify(policy)
    evidence = root / "artifacts/operator"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "official-installation.json").write_bytes(canonical({
        "consumer_sha": os.environ["R03_POTBOT_SHA"], "installed": installed, "verified": verified}))
    runtime = store.root / "installs" / installed["directory"] / "venv"
    native_python = runtime / "bin/python"
    if verify_legacy_archive:
        qualify_legacy_archive(root, runtime, native_python, evidence)
        return
    argv = [str(native_python), "-I", "-B", str(root / "scripts/ci_operator_native.py"),
            "--consumer-python", sys.executable, "--store", str(store.root), "--policy", str(policy_path)]
    environment = {name: value for name, value in os.environ.items()
                   if name not in {"DISPLAY", "WAYLAND_DISPLAY", "PYTHONPATH", "PYTHONHOME"}}
    if sys.platform == "linux":
        argv = ["/usr/bin/dbus-run-session", "--", *argv]
    result = subprocess.run(argv, env=environment, timeout=360, check=False)
    if result.returncode:
        raise AssertionError("Official operator deployment acceptance failed")
    if verify_recovery:
        recovery = [str(native_python), "-E", "-s", "-B", str(root / "scripts/ci_operator_recovery.py"),
                    "--official-runtime", str(runtime)]
        if sys.platform == "linux":
            recovery = ["/usr/bin/dbus-run-session", "--", *recovery]
        subprocess.run(recovery, env=environment, timeout=360, check=True)
    if (os.environ.get("R03_EXPORT_TRANSFER") == "true" and sys.platform == "linux"
            and policy_value["python_version"] == "3.12"):
        # Explicit transfer of the trusted preparing cache. Never include
        # environments, admission journals, native stores or fixture identities.
        transfer = base / "transfer"
        transfer.mkdir(mode=0o700)
        packages = transfer / "packages"
        packages.mkdir(mode=0o700)
        for name in ("objects", "bundles", "uv-cache"):
            shutil.copytree(store.root / name, packages / name, symlinks=True)
        shutil.copy2(store.root / "dependency-cache-inventory.json", packages / "dependency-cache-inventory.json")
        transferred_policy = {**policy_value, "dependency_cache_sha256": installed["dependency_cache_sha256"]}
        (transfer / "policy.json").write_bytes(canonical(transferred_policy))
        (transfer / "policy.json").chmod(0o600)
        controller = transfer / "controller"
        subprocess.run(["uv", "build", "--project", str(root / "consumer"), "--wheel",
                        "--out-dir", str(controller)], check=True, timeout=180)
        wheels = list(controller.glob("*.whl"))
        if len(wheels) != 1:
            raise AssertionError("Expected exactly one reviewed consumer wheel")
        selection = {"profile": "syn.operator-offline-transfer/1", "manifest_sha256": policy.manifest_sha256,
                     "source_commit": policy_value["source_commit"], "consumer_sha": os.environ["R03_POTBOT_SHA"],
                     "dependency_cache_sha256": installed["dependency_cache_sha256"],
                     "controller_filename": wheels[0].name, "controller_sha256": hashlib.sha256(wheels[0].read_bytes()).hexdigest(),
                     "agent_activation_authorized": False}
        (transfer / "selection.json").write_bytes(canonical(selection))
        archive = evidence / "linux-py312-transfer.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            output.add(transfer, arcname="transfer")
        with archive.open("rb") as source_file:
            archive_digest = hashlib.file_digest(source_file, "sha256").hexdigest()
        receipt = {**selection, "archive_sha256": archive_digest, "archive_size": archive.stat().st_size,
                   "scope": "authenticated package/dependency preparation only; no operator credentials or installed environment"}
        (evidence / "transfer-archive.json").write_bytes(canonical(receipt))
        print(canonical(receipt).decode())


def qualify_legacy_archive(root, runtime, native_python, evidence):
    # Borrow only the consumer's test runner. Imports of Syn must resolve
    # inside the official install, and the test CLI runs with isolation.
    runner = """
import os
from pathlib import Path
import sys

runtime = Path(sys.argv[1]).resolve(strict=True)
from syn.operator import legacy_state
from syn_client import adoption

assert Path(sys.prefix).resolve() == runtime
assert all(Path(module.__file__).resolve().is_relative_to(runtime)
           for module in (legacy_state, adoption))
sys.path.append(sys.argv[2])
os.environ["SYN_LEGACY_FIXTURE"] = sys.argv[3]
os.environ["SYN_LEGACY_EVIDENCE"] = sys.argv[4]
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
import pytest

test = Path(sys.argv[5])
raise SystemExit(pytest.main(["-c", str(test.parents[1] / "pyproject.toml"), "--noconftest",
                             "--import-mode=importlib", "-p", "no:cacheprovider", "-q", str(test)]))
"""
    subprocess.run([
        str(native_python), "-I", "-B", "-c", runner, str(runtime), sysconfig.get_path("purelib"),
        str(root / "consumer"), str(evidence / "legacy-archive.json"),
        str(root / "tests/test_legacy_state.py"),
    ], check=True, timeout=120)
    if not (evidence / "legacy-archive.json").is_file():
        raise AssertionError("The official migration did not produce evidence")


if __name__ == "__main__":
    main()
