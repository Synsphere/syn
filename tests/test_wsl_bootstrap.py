"""Validate Linux bootstrap offline using mock executables; no external downloads."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "source with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-wsl.sh"
    shutil.copyfile(source, scripts / "bootstrap-wsl.sh")
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    # Isolate OS-discovery commands: never touch the CI runner's package manager.
    for name, script in {
        "id": 'case "$1" in -u) echo 1000 ;; -un) echo syn ;; *) /usr/bin/id "$@" ;; esac\n',
        "dpkg-query": "echo 'install ok installed'\n",
    }.items():
        tool = home / ".local" / "bin" / name
        tool.write_text("#!/bin/sh\n" + script)
        tool.chmod(0o700)
    return root, home


def run(project, *args, offline=False, extra_env=None):
    root, home = project
    env = dict(os.environ, HOME=str(home), PATH="/a/fake/windows/bin:/usr/bin:/bin",
               DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/fake-test-bus",
               SYN_BOOTSTRAP_SECRETS_READY="1",
               SYN_DATABASE_URL="postgresql+psycopg:///syn_syn?host=/var/run/postgresql")
    if offline:
        env["SYN_BOOTSTRAP_OFFLINE"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["sh", str(root / "scripts" / "bootstrap-wsl.sh"), *args],
        capture_output=True, text=True, env=env, timeout=10,
    )


def fake_uv(project):
    _, home = project
    uv = home / ".local" / "bin" / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then echo 'uv 0.13.8'; exit 0; fi\n"
        "printf '%s\\n' \"$@\" > \"$HOME/uv-args\"\n"
        "printf '%s\\n' \"$UV_PROJECT_ENVIRONMENT\" > \"$HOME/uv-environment\"\n"
    )
    uv.chmod(0o700)
    return uv


@pytest.mark.skipif(os.name == "nt", reason="Linux shell harness")
def test_existing_linux_uv_uses_user_venv_and_forwards_arguments(project):
    fake_uv(project)
    result = run(project, "--api-only", "--port", "8001", offline=True)
    assert result.returncode == 0, result.stderr
    root, home = project
    args = (home / "uv-args").read_text().splitlines()
    assert args == ["run", "--project", str(root), "--extra", "position-native", "--", "syn-server", "--api-only", "--port", "8001", "--config", str(home / ".local/state/syn/config.toml")]
    assert (home / "uv-environment").read_text().strip().startswith(str(home / ".local/state/syn/venvs"))
    assert not (root / ".venv").exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux shell harness")
def test_offline_missing_uv_fails_without_executing_windows_binary(project):
    result = run(project, offline=True)
    assert result.returncode != 0
    assert "Linux uv is missing" in result.stderr
    assert "Windows" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="Linux shell harness")
def test_missing_uv_installs_using_official_versioned_url(project):
    root, home = project
    curl = home / ".local" / "bin" / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$HOME/curl-args\"\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        " if [ \"$1\" = --output ]; then shift; cp \"$SYN_TEST_INSTALLER\" \"$1\"; exit 0; fi\n"
        " shift\n"
        "done\nexit 2\n"
    )
    curl.chmod(0o700)
    installer = home / "mock-installer.sh"
    installer.write_text(
        "#!/bin/sh\n"
        "mkdir -p \"$UV_INSTALL_DIR\"\n"
        "printf '#!/bin/sh\\nif [ \"$1\" = --version ]; then exit 0; fi\\nprintf "
        "\"%%s\\\\n\" \"$@\" > \"$HOME/uv-args\"\\n' > \"$UV_INSTALL_DIR/uv\"\n"
        "chmod 700 \"$UV_INSTALL_DIR/uv\"\n"
    )
    result = run(project, "--api-only", extra_env={"SYN_TEST_INSTALLER": str(installer)})
    assert result.returncode == 0, result.stderr
    args = (home / "curl-args").read_text()
    assert "https://astral.sh/uv/0.13.8/install.sh" in args
    assert (home / ".local/bin/uv").exists()
    assert "--api-only" in (home / "uv-args").read_text().splitlines()
