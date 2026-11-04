"""Install exact signed upstream bytes and qualify the complete Potbot controller."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from private_interpreter import copy_private_python
from potbot.domain.errors import ScopeDeniedError
from potbot.syn_release.contracts import Policy, canonical
from potbot.syn_release.source import GitHubSource
from potbot.syn_release.store import SynReleaseStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-disposable-fixture", action="store_true")
    parser.add_argument("--consumer-dir", type=Path)
    parser.add_argument("--lan-enrollment", action="store_true")
    parser.add_argument("--group-rollover", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.getuid() != 0:
        raise SystemExit("Official native Agent qualification requires disposable Linux root")
    if os.environ.get("GITHUB_ACTIONS") != "true" and not args.local_disposable_fixture:
        raise SystemExit("Explicit disposable-fixture scope is required outside CI")
    if args.local_disposable_fixture and args.consumer_dir is None:
        raise SystemExit("Local qualification requires the exact reviewed consumer checkout")
    root = Path(__file__).resolve().parents[1]
    consumer = (args.consumer_dir or root / "consumer-agent").resolve(strict=True)
    base = Path(tempfile.mkdtemp(prefix="syn-official-agent-", dir="/tmp")).resolve()
    value = json.loads((root / "release/install-policies/v0.1.0-service.json").read_text())
    value.update(version=os.environ["R04_VERSION"], minimum_version=os.environ["R04_VERSION"],
        source_commit=os.environ["R04_RELEASE_SOURCE"], manifest_sha256=os.environ["R04_MANIFEST"], mode="agent",
        python_version=os.environ["R04_PYTHON"], required_profiles=["syn.agent-group/1", "syn.agent-local/1"],
        required_capabilities=["authenticated-agent-ipc", "confidential-selected-key-adoption", "host-bound-virtual-device",
            "native-os-agent-custody", "native-os-group-custody", "retained-agent-identity"])
    if tuple(map(int, value["version"].split("."))) >= (0, 4, 0):
        value["required_profiles"].append("syn.private-pubsub/1")
        value["required_capabilities"].append("authenticated-private-pubsub")
    if args.lan_enrollment:
        value["required_profiles"].append("syn.lan-enrollment-request/1")
        value["required_capabilities"].append("authenticated-lan-enrollment")
    if args.group_rollover:
        value["required_profiles"].append("syn.agent-group-rollover/1")
        value["required_capabilities"].append("group-member-revocation")
    value["required_profiles"].sort()
    value["required_capabilities"].sort()
    policy = Policy.parse(canonical(value))
    policy_path = base / "policy.json"
    policy_path.write_bytes(canonical(value))
    policy_path.chmod(0o600)
    token = os.environ.pop("POTBOT_SYN_GITHUB_TOKEN", None)
    source = GitHubSource(token)
    store = SynReleaseStore(base / "packages")
    tools = base / "tools"
    tools.mkdir(mode=0o700)
    uv = tools / "uv"
    shutil.copy2(Path(os.environ["R04_UV"]).resolve(strict=True), uv)
    uv.chmod(0o500)
    python = Path(os.environ["R04_HOST_PYTHON"])
    try:
        store._tool(python)
    except ScopeDeniedError:
        prefix = Path(subprocess.check_output([str(python), "-I", "-B", "-c", "import sys;print(sys.base_prefix)"], text=True, timeout=10).strip()).resolve()
        python = copy_private_python(python, prefix, tools / "python")
        store._tool(python)
    installed = store.install(policy, python=python, uv=uv, source=source)
    verified = store.verify(policy)
    evidence = root / "artifacts/agent"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "official-installation.json").write_bytes(canonical({"consumer_sha": os.environ["R04_POTBOT_SHA"],
        "source_commit": value["source_commit"], "manifest_sha256": policy.manifest_sha256,
        "installed": installed, "verified": verified}))
    runtime = store.root / "installs" / installed["directory"] / "venv"
    argv = ["/usr/bin/env", "-u", "DISPLAY", "-u", "WAYLAND_DISPLAY", "/usr/bin/dbus-run-session", "--",
        str(runtime / "bin/python"), "-I", "-B", str(root / "scripts/ci_agent_native.py"),
        "--consumer-dir", str(consumer), "--consumer-sha", os.environ["R04_POTBOT_SHA"],
        "--consumer-python", sys.executable, "--runtime-root", str(runtime),
        "--official-store", str(store.root), "--official-policy", str(policy_path)]
    if args.lan_enrollment:
        argv = ["/usr/bin/unshare", "--net", "--", *argv, "--lan-enrollment"]
    if args.group_rollover:
        argv.append("--group-rollover")
    if args.local_disposable_fixture:
        argv.append("--local-disposable-fixture")
    environment = {name: item for name, item in os.environ.items() if name not in {"PYTHONPATH", "PYTHONHOME", "POTBOT_SYN_GITHUB_TOKEN"}}
    environment["XDG_RUNTIME_DIR"] = str(base)
    result = subprocess.run(argv, env=environment, timeout=420, check=False)
    if result.returncode:
        raise AssertionError("Exact official-package Agent controller acceptance failed")


if __name__ == "__main__":
    main()
