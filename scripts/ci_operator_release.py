"""Acquire exact published packages, then qualify their real operator with Potbot."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from potbot.domain.errors import ScopeDeniedError
from potbot.syn_release.contracts import Policy, canonical
from potbot.syn_release.source import GitHubSource
from potbot.syn_release.store import SynReleaseStore


def main():
    root = Path(__file__).resolve().parents[1]
    base = Path(tempfile.mkdtemp(prefix="syn-official-", dir=Path("/tmp").resolve())).resolve()
    policy_value = json.loads((root / "release/install-policies/v0.1.0-service.json").read_text())
    policy_value.update(version="0.2.0", minimum_version="0.2.0",
                        source_commit=os.environ["R03_RELEASE_SOURCE"], manifest_sha256=os.environ["R03_MANIFEST"],
                        mode="operator", python_version=os.environ["R03_PYTHON"],
                        required_profiles=["syn.operator-local/1"],
                        required_capabilities=["authenticated-operator-ipc", "native-os-operator-custody", "retained-operator-identity"])
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
        if len(prefix.parts) < 4 or not python.resolve().is_relative_to(prefix):
            raise AssertionError("Ambiguous interpreter prefix") from None
        shutil.copytree(prefix, tools / "python", symlinks=True)
        python = tools / "python" / python.resolve().relative_to(prefix)
    installed = store.install(policy, python=python, uv=uv, source=source)
    verified = store.verify(policy)
    evidence = root / "artifacts/operator"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "official-installation.json").write_bytes(canonical({
        "consumer_sha": os.environ["R03_POTBOT_SHA"], "installed": installed, "verified": verified}))
    native_python = store.root / "installs" / installed["directory"] / "venv/bin/python"
    argv = [str(native_python), "-I", "-B", str(root / "scripts/ci_operator_native.py"),
            "--consumer-python", sys.executable, "--store", str(store.root), "--policy", str(policy_path)]
    environment = {name: value for name, value in os.environ.items()
                   if name not in {"DISPLAY", "WAYLAND_DISPLAY", "PYTHONPATH", "PYTHONHOME"}}
    if sys.platform == "linux":
        argv = ["/usr/bin/dbus-run-session", "--", *argv]
    result = subprocess.run(argv, env=environment, timeout=360, check=False)
    if result.returncode:
        raise AssertionError("Official operator deployment acceptance failed")


if __name__ == "__main__":
    main()
