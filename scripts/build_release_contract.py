"""Build an UNSIGNED release manifest from checked official package candidates.

Run after building both distributions and exporting hashed third-party requirements.
No release publication, signing, package installation or service startup occurs.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import zipfile

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from syn.release import runtimeContract
from syn_client.releases import (
    MAX_ARTIFACT_BYTES, ReleaseArtifact, ReleaseError, ReleaseManifest, artifact_names,
)

MAX_ARCHIVE_FILES = 4096
MAX_UNPACKED_BYTES = 104_857_600
ROOT = Path(__file__).resolve().parents[1]


def _safe_names(names: list[str]) -> None:
    if not names or len(names) > MAX_ARCHIVE_FILES or len(names) != len({n.casefold() for n in names}):
        raise ReleaseError("Invalid or duplicate package paths")
    forbidden_parts = {".git", ".secrets", ".codex", ".venv", "models", "__pycache__"}
    forbidden_names = {"config.toml", ".env", "syn.sql", "syn.zip", "syn.rar", "populatethings.sql.gz"}
    for name in names:
        p = PurePosixPath(name)
        parts = {x.casefold() for x in p.parts}
        if (not name or any(ord(c) < 32 for c in name) or "\\" in name or ":" in name or p.is_absolute()
                or name.rstrip("/") != p.as_posix()
                or ".." in parts or parts & forbidden_parts
                or p.name.casefold() in forbidden_names
                or p.suffix.casefold() in {".key", ".pfx", ".p12", ".sqlite", ".rar", ".zip"}):
            raise ReleaseError("Private, unsafe or non-package input in release archive")


def _metadata(raw: bytes, distribution: str, version: str) -> None:
    if len(raw) > 1_048_576:
        raise ReleaseError("Package metadata is too large")
    data = BytesParser().parsebytes(raw)
    if any(len(data.get_all(name, [])) != 1 for name in ("Name", "Version", "Requires-Python")):
        raise ReleaseError("Package identity metadata is missing or duplicated")
    if (str(data.get("Name", "")).replace("_", "-").lower() != distribution
            or data.get("Version") != version):
        raise ReleaseError("Package metadata differs from its release identity")
    expected_python = ">=3.12,<3.14" if distribution == "syn" else ">=3.12"
    if SpecifierSet(data["Requires-Python"]) != SpecifierSet(expected_python):
        raise ReleaseError("Package Python requirement differs from this release profile")
    deps = [Requirement(x) for x in data.get_all("Requires-Dist", [])]
    if distribution == "syn":
        client = [x for x in deps if x.name.replace("_", "-").lower() == "syn-client"]
        if len(client) != 1 or client[0].url or client[0].marker is not None or str(client[0].specifier) != "==" + version or client[0].extras != {"governance"}:
            raise ReleaseError("Runtime must require its exact governance-capable client wheel")
    elif (data.get_all("Provides-Extra", []) != ["governance"] or len(deps) != 1
          or deps[0].name != "cryptography" or str(deps[0].specifier) != "==50.0.2"
          or deps[0].url or deps[0].extras or str(deps[0].marker) != 'extra == "governance"'):
        raise ReleaseError("Client base must remain dependency-free with the pinned governance extra")


def check_wheel(path: Path, distribution: str, version: str) -> None:
    module = "syn" if distribution == "syn" else "syn_client"
    dist_info = f"{module}-{version}.dist-info"
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            names = [x.filename for x in members]
            _safe_names(names)
            if sum(x.file_size for x in members) > MAX_UNPACKED_BYTES:
                raise ReleaseError("Unpacked package exceeds its bound")
            if any(x.file_size > 10_485_760 or (x.external_attr >> 16) & 0o170000 == 0o120000 for x in members):
                raise ReleaseError("Oversized or symlink wheel member")
            if any(not (n.startswith(module + "/") or n.startswith(dist_info + "/")) for n in names):
                raise ReleaseError("Unexpected wheel payload root")
            required = {module + "/__init__.py", dist_info + "/METADATA", dist_info + "/WHEEL", dist_info + "/RECORD"}
            if distribution == "syn":
                required |= {"syn/release.py", "syn/server.py", "syn/api.py", "syn/schema.sql",
                             "syn/cacert.pem", "syn/cacert.pem.sha256", "syn/templates/index.html",
                             "syn/alembic/versions/20261024_0015_signed_governance.py", dist_info + "/entry_points.txt"}
            else:
                required |= {"syn_client/releases.py", "syn_client/consensus.py", "syn_client/governance.py", "syn_client/dag_signing.py"}
            if not required <= set(names):
                raise ReleaseError("Wheel is missing required runtime/client resources")
            _metadata(archive.read(dist_info + "/METADATA"), distribution, version)
            if b"Tag: py3-none-any" not in archive.read(dist_info + "/WHEEL"):
                raise ReleaseError("Unexpected wheel compatibility tag")
            if distribution == "syn":
                entry_points = archive.read(dist_info + "/entry_points.txt").decode("utf-8")
                if "syn-server = syn.server:main" not in entry_points:
                    raise ReleaseError("Runtime wheel has no supported server entry point")
                ca = hashlib.sha256(archive.read("syn/cacert.pem")).hexdigest()
                expected = archive.read("syn/cacert.pem.sha256").decode("ascii").split()[0].lower()
                if ca != expected:
                    raise ReleaseError("Packaged CA bundle checksum differs")
    except (zipfile.BadZipFile, KeyError, UnicodeError, InvalidRequirement, InvalidSpecifier) as error:
        raise ReleaseError("Invalid release wheel") from error


def check_sdist(path: Path, distribution: str, version: str) -> None:
    module = "syn" if distribution == "syn" else "syn_client"
    prefix = f"{module}-{version}/"
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            _safe_names([x.name for x in members])
            if sum(x.size for x in members) > MAX_UNPACKED_BYTES:
                raise ReleaseError("Source package exceeds its unpacked bound")
            if any(not (x.isfile() or x.isdir()) or not (x.name.startswith(prefix) or (x.isdir() and x.name == prefix[:-1])) for x in members):
                raise ReleaseError("Unsupported source package member")
            member = archive.getmember(prefix + "PKG-INFO")
            source = archive.extractfile(member)
            if source is None or member.size > 1_048_576:
                raise ReleaseError("Missing source metadata")
            _metadata(source.read(), distribution, version)
    except (tarfile.TarError, KeyError, OSError, InvalidRequirement, InvalidSpecifier) as error:
        raise ReleaseError("Invalid release source package") from error


def check_requirements(raw: bytes) -> None:
    """Require hashed exact third-party pins; reject local/Syn/source fallbacks."""
    if not raw or len(raw) > 1_048_576:
        raise ReleaseError("Dependency export is empty or too large")
    try:
        text = raw.decode("utf-8")
        # uv's hash continuations become one logical requirement per line.
        logical = re.sub(r"\\\r?\n[ \t]*", " ", text)
        count = 0
        for line in logical.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            requirement, *hashes = re.split(r"\s+--hash=", line)
            parsed = Requirement(requirement)
            specs = list(parsed.specifier)
            if (parsed.url or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version
                    or parsed.name.replace("_", "-").lower() in {"syn", "syn-client"}
                    or not hashes or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", x.strip()) for x in hashes)):
                raise ReleaseError("Dependencies require exact hashed third-party pins")
            count += 1
        if not count:
            raise ReleaseError("No runtime dependencies in export")
    except (UnicodeError, InvalidRequirement) as error:
        raise ReleaseError("Invalid release dependency export") from error


def build_manifest(directory: Path, source_commit: str) -> ReleaseManifest:
    contract = runtimeContract()
    artifacts = []
    for role, filename in sorted(artifact_names(contract.runtime_version).items()):
        path = directory / filename
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_ARTIFACT_BYTES:
            raise ReleaseError("Missing, linked or oversized release artifact")
        initial_size = path.stat().st_size
        with path.open("rb") as handle:
            initial_digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if role.endswith("wheel"):
            check_wheel(path, "syn" if role.startswith("runtime") else "syn-client", contract.runtime_version)
        elif role.endswith("sdist"):
            check_sdist(path, "syn" if role.startswith("runtime") else "syn-client", contract.runtime_version)
        else:
            check_requirements(path.read_bytes())
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != initial_digest or path.stat().st_size != initial_size:
            raise ReleaseError("Release artifact changed during validation")
        artifacts.append(ReleaseArtifact(role, filename, digest, initial_size))
    return ReleaseManifest(contract.runtime_version, source_commit, contract, tuple(artifacts))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--source-commit", required=True)
    options = parser.parse_args()
    actual = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=normal"], text=True)
    if options.source_commit != actual or dirty:
        raise ReleaseError("Manifest construction requires the exact clean source commit")
    manifest = build_manifest(options.dist_dir.resolve(), actual)
    output = options.dist_dir / f"syn-release-{manifest.version}.json"
    with output.open("xb") as handle:
        handle.write(manifest.canonical)
    print(f"Unsigned candidate manifest: {output.name}; sha256={manifest.digest}")
    print("Not an official release: independent release-authority signature and publication remain required.")


if __name__ == "__main__":
    main()
