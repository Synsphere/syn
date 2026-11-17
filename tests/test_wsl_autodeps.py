"""WSL bootstrap integration with mocked executables: no sudo, network, or keys."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "Syn checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-wsl.sh",
        scripts / "bootstrap-wsl.sh",
    )
    home = tmp_path / "linux-home"
    tools = home / ".local" / "bin"
    tools.mkdir(parents=True)

    def executable(name: str, content: str):
        path = tools / name
        path.write_text("#!/bin/sh\n" + content, encoding="utf-8")
        path.chmod(0o700)
        return path

    executable("id", """case "$1" in
 -u) echo 1000 ;; -un) echo syn ;; *) /usr/bin/id "$@" ;; esac
""")
    executable("dpkg-query", "echo 'install ok installed'\n")
    executable("uv", """if [ "$1" = --version ]; then echo 'uv 0.13.8'; exit 0; fi
printf '%s\\n' "$@" > "$HOME/syn-uv-args"
printf '%s\\n' "$UV_PROJECT_ENVIRONMENT" > "$HOME/syn-uv-env"
printf '%s\\n' "$SYN_DATABASE_URL" > "$HOME/syn-db-url"
""")
    return root, home, executable


def launch(sandbox, *args, env_changes=None):
    root, home, _ = sandbox
    env = dict(os.environ, HOME=str(home),
               PATH="/fake/windows/bin:/usr/bin:/bin",
               SYN_BOOTSTRAP_OFFLINE="1",
               DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent-test",
               SYN_BOOTSTRAP_SECRETS_READY="1",
               SYN_DATABASE_URL="postgresql+psycopg:///syn_syn?host=/var/run/postgresql")
    if env_changes:
        env.update(env_changes)
    return subprocess.run(["sh", str(root / "scripts/bootstrap-wsl.sh"), *args],
                          env=env, capture_output=True, text=True, timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell simulation")
def test_reuses_existing_deps_without_install_or_privilege(sandbox):
    root, home, _ = sandbox
    result = launch(sandbox, "--port", "8910")
    assert result.returncode == 0, result.stderr
    assert (home / "syn-uv-args").read_text().splitlines() == [
        "run", "--project", str(root), "--extra", "position-native", "--",
        "syn-server", "--port", "8910", "--config", str(home / ".local/state/syn/config.toml"),
    ]
    assert (home / "syn-uv-env").read_text().strip().startswith(
        str(home / ".local/state/syn/venvs"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell simulation")
def test_explicit_config_is_respected(sandbox):
    root, home, _ = sandbox
    path = root / "custom.toml"
    result = launch(sandbox, "--config", str(path))
    assert result.returncode == 0, result.stderr
    assert (home / "syn-uv-args").read_text().splitlines()[-2:] == [
        "--config", str(path)]
    assert not (home / ".local/state/syn/config.toml").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell simulation")
def test_missing_packages_stop_without_tty_and_no_install(sandbox):
    _, home, executable = sandbox
    executable("dpkg-query", "exit 1\n")
    result = launch(sandbox, env_changes={"SYN_DATABASE_URL": "", "SYN_BOOTSTRAP_OFFLINE": "0"})
    assert result.returncode != 0
    assert "Missing OS packages" in result.stdout
    assert "interactive terminal" in result.stderr
    assert not (home / "syn-uv-args").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell simulation")
def test_offline_missing_linux_uv_fails_with_message(sandbox):
    _, home, _ = sandbox
    (home / ".local/bin/uv").unlink()
    result = launch(sandbox)
    assert result.returncode != 0
    assert "Linux uv is missing" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell simulation")
def test_invalid_config_argument_fails_closed(sandbox):
    result = launch(sandbox, "--config")
    assert result.returncode != 0
    assert "requires a file path" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell simulation")
def test_fresh_local_database_uses_peer_auth_without_password(sandbox):
    _, home, executable = sandbox
    executable("pg_lsclusters", "echo '16 main 5432 online postgres /var/lib/postgresql/16/main'\n")
    executable("pg_isready", "exit 0\n")
    executable("sudo", """printf '%s\n' "$*" >> "$HOME/syn-admin-calls"
if [ "$1" = -u ]; then shift 2; fi
"$@"
""")
    executable("psql", """case "$*" in
 *'FROM pg_roles'*) exit 0 ;;
 *'FROM pg_database'*) exit 0 ;;
 *'SELECT 1'*) echo 1 ;;
 *) printf 'psql %s\n' "$*" >> "$HOME/syn-sql-calls" ;;
esac
""")
    executable("createdb", """printf 'createdb %s\\n' "$*" >> "$HOME/syn-sql-calls"
""")
    result = launch(sandbox, env_changes={"SYN_DATABASE_URL": ""})
    assert result.returncode == 0, result.stderr
    calls = (home / "syn-sql-calls").read_text()
    assert 'CREATE ROLE "syn" LOGIN' in calls
    assert 'createdb -p 5432 --owner=syn syn_syn' in calls
    assert (home / "syn-db-url").read_text().strip() == (
        "postgresql+psycopg:///syn_syn?host=/var/run/postgresql&port=5432")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell simulation")
def test_existing_foreign_owned_database_is_not_taken_over(sandbox):
    _, home, executable = sandbox
    executable("pg_lsclusters", "echo '16 main 5432 online'\n")
    executable("pg_isready", "exit 0\n")
    executable("sudo", """if [ "$1" = -u ]; then shift 2; fi
"$@"
""")
    executable("psql", """case "$*" in
 *'FROM pg_roles'*) echo 1 ;;
 *'FROM pg_database'*) echo other_user ;;
esac
""")
    result = launch(sandbox, env_changes={"SYN_DATABASE_URL": ""})
    assert result.returncode != 0
    assert "refusing to take ownership" in result.stderr
    assert not (home / "syn-uv-args").exists()
