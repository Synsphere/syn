"""Private copy of one explicitly selected trusted managed Python prefix."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat


def copy_private_python(source: Path, prefix: Path, destination: Path) -> Path:
    prefix = prefix.resolve(strict=True)
    source = source.resolve(strict=True)
    if len(prefix.parts) < 4 or not source.is_relative_to(prefix):
        raise ValueError("Refusing an ambiguous interpreter-prefix copy")
    before = hashlib.sha256(source.read_bytes()).digest()
    shutil.copytree(prefix, destination, symlinks=True)
    # Only the new operator-owned copy is changed. Never chmod hosted tools,
    # their caches or targets reached through a symlink.
    for directory, names, files in os.walk(destination, followlinks=False):
        for path in (Path(directory), *(Path(directory) / name for name in names + files)):
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)
    selected = destination / source.relative_to(prefix)
    if (not selected.resolve(strict=True).is_relative_to(destination.resolve(strict=True))
            or hashlib.sha256(selected.read_bytes()).digest() != before):
        raise ValueError("Private interpreter differs from the selected trusted executable")
    return selected
