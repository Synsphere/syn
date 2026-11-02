"""Stage a signed, byte-verified draft; never publish or replace a release.

The source-controlled public authority is independent of downloaded candidates.
The private seed is supplied only to the signing process and removed from its
child environment. A separate read-only mode verifies an immutable publication.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from syn_client.consensus import canonical_bytes, load_document
from syn_client.releases import (
    MAX_MANIFEST_BYTES, REPOSITORY, SIGNATURE_DOMAIN, SIGNATURE_PROFILE,
    ReleaseError, ReleaseManifest, ReleasePin, verify_release,
)
from scripts.build_release_contract import build_manifest

ROOT = Path(__file__).resolve().parents[1]
KEY_ENV = "SYN_RELEASE_SIGNING_KEY_BASE64"
API_VERSION = "2026-03-10"


@dataclass(frozen=True)
class Authority:
    key_id: str
    public_key: bytes
    fingerprint: str


def load_authority(path: Path) -> Authority:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 4096:
        raise ReleaseError("Missing, linked or oversized authority policy")
    data = load_document(path.read_text(encoding="utf-8"))
    if set(data) != {"profile", "key_id", "public_key", "release_key_sha256"}:
        raise ReleaseError("Authority policy has missing or unknown fields")
    if data["profile"] != "syn.release-authority/1" or not isinstance(data["key_id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", data["key_id"]):
        raise ReleaseError("Unsupported release authority")
    if any(not isinstance(data[x], str) or not re.fullmatch(r"[0-9a-f]{64}", data[x]) for x in ("public_key", "release_key_sha256")):
        raise ReleaseError("Invalid release authority identity")
    public = bytes.fromhex(data["public_key"])
    if hashlib.sha256(public).hexdigest() != data["release_key_sha256"]:
        raise ReleaseError("Authority public key and fingerprint differ")
    return Authority(data["key_id"], public, data["release_key_sha256"])


def decode_seed(value: str | None) -> bytes:
    try:
        if not isinstance(value, str) or len(value) != 44:
            raise ValueError
        seed = base64.b64decode(value, validate=True)
        if len(seed) != 32 or base64.b64encode(seed).decode("ascii") != value:
            raise ValueError
        return seed
    except (ValueError, binascii.Error) as error:
        raise ReleaseError("A canonical base64 32-byte release signing seed is required") from error


def names(manifest: ReleaseManifest) -> set[str]:
    return {x.filename for x in manifest.artifacts} | {
        f"syn-release-{manifest.version}.json", f"syn-release-{manifest.version}.sig.json",
    }


def bounded_document(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_MANIFEST_BYTES:
        raise ReleaseError("Missing, linked or oversized release document")
    return path.read_bytes()


def exact_files(directory: Path, expected: set[str]) -> None:
    files = list(directory.iterdir())
    if {p.name for p in files} != expected or any(p.is_symlink() or not p.is_file() for p in files):
        raise ReleaseError("Release bundle must contain exactly the selected flat files")


def sign_manifest(manifest: ReleaseManifest, seed: bytes, authority: Authority) -> tuple[bytes, ReleasePin]:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if public != authority.public_key:
        raise ReleaseError("Signing key does not match the selected release authority")
    pin = ReleasePin(manifest.version, manifest.source_commit, manifest.digest, authority.fingerprint)
    signature = canonical_bytes({"profile": SIGNATURE_PROFILE, "manifest_sha256": manifest.digest,
                                 "release_key_sha256": authority.fingerprint,
                                 "signature": key.sign(SIGNATURE_DOMAIN + manifest.canonical).hex()})
    verify_release(manifest.canonical, signature, pin, authority.public_key)
    return signature, pin


def verify_bundle(directory: Path, pin: ReleasePin, authority: Authority) -> ReleaseManifest:
    raw = bounded_document(directory / f"syn-release-{pin.version}.json")
    signature = bounded_document(directory / f"syn-release-{pin.version}.sig.json")
    manifest = verify_release(raw, signature, pin, authority.public_key)
    exact_files(directory, names(manifest))
    for artifact in manifest.artifacts:
        path = directory / artifact.filename
        if path.stat().st_size != artifact.size:
            raise ReleaseError("Downloaded artifact size differs from the signed manifest")
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if digest != artifact.sha256:
            raise ReleaseError("Downloaded artifact digest differs from the signed manifest")
    return manifest


class GitHub:
    """Fixed repository, argument-vector CLI calls, and no signing seed in children."""
    def run(self, args: list[str], *, missing: bool = False) -> str | None:
        environment = dict(os.environ)
        environment.pop(KEY_ENV, None)
        result = subprocess.run(["gh", *args], env=environment, capture_output=True, text=True, check=False, timeout=180)
        if result.returncode:
            if missing and "HTTP 404" in result.stderr:
                return None
            # Remote output is diagnostic data, never shell input or key-bearing logs.
            raise ReleaseError("GitHub operation failed; inspect the retained draft without replacing it")
        return result.stdout

    def api(self, path: str, *, missing: bool = False) -> Any:
        raw = self.run(["api", "-H", f"X-GitHub-Api-Version: {API_VERSION}", f"repos/{REPOSITORY}/{path}"], missing=missing)
        return None if raw is None else json.loads(raw)

    def release(self, tag: str) -> dict[str, Any] | None:
        releases = self.api("releases?per_page=100")
        if not isinstance(releases, list) or len(releases) > 100:
            raise ReleaseError("Invalid GitHub release listing")
        selected = [x for x in releases if isinstance(x, dict) and x.get("tag_name") == tag]
        if len(selected) > 1:
            raise ReleaseError("Ambiguous GitHub release identity")
        return selected[0] if selected else None

    def tag(self, tag: str) -> dict[str, Any] | None:
        return self.api("git/ref/tags/" + tag, missing=True)

    def create_draft(self, tag: str, source: str, files: list[Path], notes: Path) -> None:
        args = ["release", "create", tag, *map(str, files), "--repo", REPOSITORY,
                "--target", source, "--title", "Syn " + tag[1:], "--notes-file", str(notes), "--draft"]
        if re.search(r"(?:a|b|rc)[1-9][0-9]*$", tag):
            args.append("--prerelease")
        self.run(args)

    def download(self, tag: str, expected: set[str], directory: Path) -> None:
        # Exact admitted names prevent downloading arbitrary extra assets or source archives.
        for filename in sorted(expected):
            self.run(["release", "download", tag, "--repo", REPOSITORY, "--pattern", filename, "--dir", str(directory)])


def metadata(release: dict[str, Any] | None, manifest: ReleaseManifest, *, draft: bool) -> dict[str, Any]:
    if not isinstance(release, dict) or release.get("tag_name") != "v" + manifest.version or release.get("draft") is not draft:
        raise ReleaseError("Release state or version does not match the selected handoff")
    if not draft and release.get("immutable") is not True:
        raise ReleaseError("Published release is not immutable")
    if draft and release.get("target_commitish") != manifest.source_commit:
        raise ReleaseError("Draft target differs from the selected source commit")
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) != 7 or any(not isinstance(x, dict) for x in assets):
        raise ReleaseError("Release must contain exactly seven assets")
    selected = {x.get("name") for x in assets}
    if selected != names(manifest):
        raise ReleaseError("Release asset names or duplicates differ from the official set")
    expected_sizes = {x.filename: x.size for x in manifest.artifacts}
    expected_sizes[f"syn-release-{manifest.version}.json"] = len(manifest.canonical)
    expected_sizes[f"syn-release-{manifest.version}.sig.json"] = len(canonical_bytes({
        "profile": SIGNATURE_PROFILE, "manifest_sha256": manifest.digest,
        "release_key_sha256": "0" * 64, "signature": "0" * 128,
    }))
    for asset in assets:
        size = asset.get("size")
        if type(size) is not int or size != expected_sizes.get(asset.get("name")):
            raise ReleaseError("Release asset size differs from the selected bytes")
    return release


def receipt(manifest: ReleaseManifest, authority: Authority, release: dict[str, Any], *, published: bool) -> dict[str, Any]:
    return {"profile": "syn.release-publication-evidence/1", "repository": REPOSITORY,
            "tag": "v" + manifest.version, "source_commit": manifest.source_commit,
            "manifest_sha256": manifest.digest, "release_key_sha256": authority.fingerprint,
            "key_id": authority.key_id, "release_id": release["id"], "release_url": release["html_url"],
            "assets": sorted(names(manifest)), "node_modes": list(manifest.runtime.node_modes),
            "published": published, "immutable": release.get("immutable") is True,
            "qualification": "published_bytes_only_not_installed_operator_or_agent_acceptance" if published else "verified_signed_draft_not_an_official_release"}


def stage(directory: Path, source: str, tag: str, seed: bytes, authority: Authority, github: GitHub) -> dict[str, Any]:
    manifest = build_manifest(directory, source)
    if tag != "v" + manifest.version:
        raise ReleaseError("Requested tag differs from package versions")
    unsigned_names = names(manifest) - {f"syn-release-{manifest.version}.sig.json"}
    exact_files(directory, unsigned_names)
    if bounded_document(directory / f"syn-release-{manifest.version}.json") != manifest.canonical:
        raise ReleaseError("Candidate manifest differs from the exact retained package bytes")
    signature, pin = sign_manifest(manifest, seed, authority)
    if github.release(tag) is not None or github.tag(tag) is not None:
        raise ReleaseError("A release or tag already exists; refusing replacement")
    with (directory / f"syn-release-{manifest.version}.sig.json").open("xb") as file:
        file.write(signature)
    verify_bundle(directory, pin, authority)
    with tempfile.TemporaryDirectory(prefix="syn-release-notes-") as scratch:
        notes = Path(scratch) / "notes.md"
        notes.write_text(f"Syn {manifest.version}\n\nOfficial service-mode package set. Operator and Agent node modes remain unavailable.\n\nSource: `{source}`\nManifest SHA-256: `{manifest.digest}`\nRelease authority SHA-256: `{authority.fingerprint}`\n\nThe two wheels, two source distributions, hashed dependency lock, canonical manifest and detached signature form the seven-asset release. Consumers must supply independent trusted pins and verify all bytes before installation. No installed-node or private-mesh qualification is implied.\n", encoding="utf-8")
        github.create_draft(tag, source, [directory / name for name in sorted(names(manifest))], notes)
    remote = metadata(github.release(tag), manifest, draft=True)
    with tempfile.TemporaryDirectory(prefix="syn-release-readback-") as scratch:
        destination = Path(scratch)
        github.download(tag, names(manifest), destination)
        verify_bundle(destination, pin, authority)
    return receipt(manifest, authority, remote, published=False)


def verify_published(tag: str, pin: ReleasePin, authority: Authority, github: GitHub) -> dict[str, Any]:
    if tag != "v" + pin.version:
        raise ReleaseError("Requested published tag differs from the independent pin")
    with tempfile.TemporaryDirectory(prefix="syn-published-readback-") as scratch:
        destination = Path(scratch)
        # Build the fixed names from the selected version before trusting metadata.
        from syn_client.releases import artifact_names
        expected = set(artifact_names(pin.version).values()) | {f"syn-release-{pin.version}.json", f"syn-release-{pin.version}.sig.json"}
        github.download(tag, expected, destination)
        manifest = verify_bundle(destination, pin, authority)
    remote = metadata(github.release(tag), manifest, draft=False)
    reference = github.tag(tag)
    target = reference.get("object") if isinstance(reference, dict) else None
    if not isinstance(target, dict) or target.get("type") != "commit" or target.get("sha") != pin.source_commit:
        raise ReleaseError("Published tag does not resolve directly to the selected commit")
    return receipt(manifest, authority, remote, published=True)


def require_workflow_context(source: str) -> None:
    if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF") != "refs/heads/master"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or os.environ.get("GITHUB_SHA") != source):
        raise ReleaseError("Release operations require an exact master workflow dispatch")
    actual = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=normal"], text=True)
    if actual != source or dirty:
        raise ReleaseError("Release operations require the exact clean source revision")


def main() -> None:
    # Consume before any child can inherit this capability, including validation.
    private_value = os.environ.pop(KEY_ENV, None)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "verify-published"))
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--authority", type=Path, default=ROOT / "release/authority.json")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--record", type=Path, required=True)
    options = parser.parse_args()
    execution_source = os.environ.get("GITHUB_SHA", "")
    require_workflow_context(execution_source)
    if options.operation == "stage" and options.source_commit != execution_source:
        raise ReleaseError("Staging source must match the admitted workflow checkout")
    authority = load_authority(options.authority)
    github = GitHub()
    if options.operation == "stage":
        result = stage(options.dist_dir, options.source_commit, options.tag, decode_seed(private_value), authority, github)
    else:
        if not options.tag.startswith("v"):
            raise ReleaseError("Expected an exact version tag")
        pin = ReleasePin(options.tag[1:], options.source_commit, options.manifest_sha256, authority.fingerprint)
        result = verify_published(options.tag, pin, authority, github)
    options.record.parent.mkdir(parents=True, exist_ok=True)
    with options.record.open("x", encoding="utf-8") as file:
        json.dump(result, file, indent=2)
        file.write("\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
