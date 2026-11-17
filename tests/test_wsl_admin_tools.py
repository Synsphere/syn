"""Guard Linux administrative utility discovery in Syn's WSL bootstrap."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-wsl.sh"


def test_native_administrative_paths_and_packages_are_present():
    source = SCRIPT.read_text(encoding="utf-8")
    path_statement = next(line for line in source.splitlines() if line.startswith("PATH="))
    native_paths = path_statement.removeprefix('PATH="').removesuffix('"').split(":")
    assert "/usr/sbin" in native_paths
    assert "/sbin" in native_paths
    assert "/usr/local/sbin" in native_paths
    # This PATH must not inherit Windows executables via a trailing $PATH.
    assert "$PATH" not in path_statement
    packages = next(line for line in source.splitlines() if line.strip().startswith("packages='"))
    assert " passwd " in packages
    assert " util-linux " in packages


@pytest.mark.skipif(sys.platform != "linux", reason="Linux account-management utilities")
def test_bootstrap_can_find_standard_linux_useradd_and_runuser(tmp_path):
    if not Path("/usr/sbin/useradd").is_file() or not Path("/usr/sbin/runuser").is_file():
        pytest.skip("This host does not provide the standard Linux account tools")
    statement = next(line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
                     if line.startswith("PATH="))
    # Execute the bootstrap's PATH declaration in a clean POSIX shell. No
    # account changes, package installation or privileged operations occur.
    result = subprocess.run(
        ["sh", "-c", statement + "\ncommand -v useradd\ncommand -v runuser"],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        text=True, capture_output=True, check=False, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    resolved = result.stdout.splitlines()
    assert resolved[0] in {"/usr/sbin/useradd", "/sbin/useradd"}
    assert resolved[1] in {"/usr/sbin/runuser", "/sbin/runuser"}
