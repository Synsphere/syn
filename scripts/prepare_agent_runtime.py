"""Stage disposable, root-owned candidate wheels for native sandbox CI.

Never a production installer. Official release acceptance uses Potbot's verified
immutable store. Only this new fixture copy is changed; hosted tools stay intact.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from private_interpreter import copy_private_python


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-prefix", type=Path, required=True)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    args = parser.parse_args()
    if os.getuid() != 0 or sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Disposable Linux CI scope and root are required")
    destination = args.destination
    if (not destination.is_absolute() or destination.resolve() != destination
            or destination.exists() or destination.is_symlink() or not destination.is_relative_to(Path("/var/lib/potbot"))):
        raise SystemExit("Select a new exact fixture directory under /var/lib/potbot")
    parent = Path("/var/lib/potbot")
    if not parent.exists():
        parent.mkdir(mode=0o700)
    if parent.stat().st_uid != 0 or parent.stat().st_mode & 0o077:
        raise SystemExit("Unsafe native fixture parent")
    destination.mkdir(mode=0o700)
    runtime = destination / "runtime"
    source = args.source_prefix.resolve(strict=True)
    selected = source / "bin/python"
    metadata = json.loads(subprocess.check_output([str(selected), "-I", "-c",
        "import json,sys;print(json.dumps({'prefix':sys.base_prefix,'executable':sys.executable}))"], timeout=10))
    prefix = Path(metadata["prefix"]).resolve(strict=True)
    shutil.copytree(source, runtime, symlinks=True)
    runtime.chmod(0o755)
    if prefix == Path("/usr"):
        interpreter = selected.resolve(strict=True)
    else:
        interpreter = copy_private_python(selected, prefix, destination / "python")
    for name in ("python", "python3", f"python{sys.version_info.major}.{sys.version_info.minor}"):
        path = runtime / "bin" / name
        if path.exists() or path.is_symlink():
            path.unlink()
        path.symlink_to(interpreter)
    config = runtime / "pyvenv.cfg"
    lines = config.read_text().splitlines()
    lines = ["home = " + str(interpreter.parent) if row.startswith("home = ") else row for row in lines]
    config.write_text("\n".join(lines) + "\n")
    runtime_wheel, = args.wheels.glob("syn-*-py3-none-any.whl")
    client_wheel, = args.wheels.glob("syn_client-*-py3-none-any.whl")
    subprocess.run([str(args.uv), "pip", "install", "--python", str(runtime / "bin/python"), "--no-deps", "--reinstall",
        "--cache-dir", str(destination / "cache"), str(runtime_wheel), str(client_wheel)], check=True, timeout=60)
    print(json.dumps({"profile": "syn.agent-source-runtime/1", "runtime_root": str(runtime), "official_release_qualified": False}))


if __name__ == "__main__":
    main()
