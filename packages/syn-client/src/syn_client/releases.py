"""SYN-R01 release identity, authenticity and declared compatibility contracts.

Parsing and compatibility do not establish trust, installation or host acceptance.
Release keys/pins are supplied independently; no network or private-key operation
is implemented here. Existing receipt/governance signature bytes are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

from .consensus import PolicyError, canonical_bytes, load_document

REPOSITORY = "Synsphere/syn"
MANIFEST_PROFILE = "syn.release-manifest/1"
SIGNATURE_PROFILE = "syn.release-signature/1"
RUNTIME_PROFILE = "syn.runtime-contract/1"
SIGNATURE_DOMAIN = b"syn.release-manifest/1\0"
MAX_MANIFEST_BYTES = 65_536
MAX_ARTIFACT_BYTES = 536_870_912
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:(?:a|b|rc)[1-9][0-9]*)?\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TOKEN = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")
_PLATFORMS = frozenset({"darwin-arm64", "darwin-x86_64", "linux-x86_64", "windows-x86_64"})
_PYTHONS = frozenset({"3.12", "3.13"})
_MODES = frozenset({"service", "operator", "agent"})


class ReleaseError(ValueError):
    """A closed release contract, pin, signature or compatibility check failed."""


def _fields(value: object, expected: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise ReleaseError("Release contract has missing or unknown fields")
    return value


def _version(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64 or not _VERSION.fullmatch(value):
        raise ReleaseError("Expected an exact normalized release version")
    return value


def _hex(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ReleaseError("Expected a lowercase release digest or commit")
    return value


def _strings(value: object, *, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= 32:
        raise ReleaseError("Expected a bounded nonempty release string list")
    if any(not isinstance(x, str) for x in value):
        raise ReleaseError("Invalid release string")
    if any(not _TOKEN.fullmatch(x) for x in value):
        # Python versions start with a number; allow only the fixed versions.
        if allowed != _PYTHONS or any(x not in _PYTHONS for x in value):
            raise ReleaseError("Invalid release string")
    if value != sorted(set(value)) or (allowed is not None and not set(value) <= allowed):
        raise ReleaseError("Release strings must be sorted, unique and supported")
    return tuple(value)


def _document(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_MANIFEST_BYTES:
        raise ReleaseError("Release document exceeds its byte bound")
    try:
        data = load_document(raw.decode("utf-8"))
        if canonical_bytes(data) != raw:
            raise ReleaseError("Release document must use exact syn.consensus-json/1 bytes")
        return data
    except (PolicyError, UnicodeError) as error:
        raise ReleaseError("Invalid release JSON") from error


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    runtime_version: str
    client_version: str
    python_versions: tuple[str, ...]
    runtime_platforms: tuple[str, ...]
    client_platforms: tuple[str, ...]
    node_modes: tuple[str, ...]
    api_profiles: tuple[str, ...]
    capabilities: tuple[str, ...]
    database_revision: str

    def __post_init__(self) -> None:
        _version(self.runtime_version)
        _version(self.client_version)
        if self.runtime_version != self.client_version:
            raise ReleaseError("Runtime and client must be an exact co-release pair")
        for name, allowed in (
            ("python_versions", _PYTHONS), ("runtime_platforms", _PLATFORMS),
            ("client_platforms", _PLATFORMS), ("node_modes", _MODES),
            ("api_profiles", None), ("capabilities", None),
        ):
            value = getattr(self, name)
            if type(value) is not tuple:
                raise ReleaseError("Runtime contract lists must be immutable tuples")
            _strings(list(value), allowed=allowed)
        if not isinstance(self.database_revision, str) or not re.fullmatch(r"[0-9]{8}_[0-9]{4}", self.database_revision):
            raise ReleaseError("Expected a packaged database migration revision")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": RUNTIME_PROFILE,
            "runtime_version": self.runtime_version,
            "client_version": self.client_version,
            "python_versions": list(self.python_versions),
            "runtime_platforms": list(self.runtime_platforms),
            "client_platforms": list(self.client_platforms),
            "node_modes": list(self.node_modes),
            "api_profiles": list(self.api_profiles),
            "capabilities": list(self.capabilities),
            "database_revision": self.database_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> RuntimeContract:
        data = _fields(value, {"profile", "runtime_version", "client_version", "python_versions", "runtime_platforms", "client_platforms", "node_modes", "api_profiles", "capabilities", "database_revision"})
        if data["profile"] != RUNTIME_PROFILE:
            raise ReleaseError("Unsupported runtime contract profile")
        return cls(data["runtime_version"], data["client_version"],
                   _strings(data["python_versions"], allowed=_PYTHONS),
                   _strings(data["runtime_platforms"], allowed=_PLATFORMS),
                   _strings(data["client_platforms"], allowed=_PLATFORMS),
                   _strings(data["node_modes"], allowed=_MODES),
                   _strings(data["api_profiles"]), _strings(data["capabilities"]),
                   data["database_revision"])


def artifact_names(version: str) -> dict[str, str]:
    """Exact flat names; GitHub-generated source archives are not release inputs."""
    version = _version(version)
    return {
        "client-sdist": f"syn_client-{version}.tar.gz",
        "client-wheel": f"syn_client-{version}-py3-none-any.whl",
        "dependency-lock": f"syn-runtime-requirements-{version}.txt",
        "runtime-sdist": f"syn-{version}.tar.gz",
        "runtime-wheel": f"syn-{version}-py3-none-any.whl",
    }


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    role: str
    filename: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _hex(self.sha256, _HASH)
        if type(self.size) is not int or not 1 <= self.size <= MAX_ARTIFACT_BYTES:
            raise ReleaseError("Release artifact size is out of bounds")
        if not isinstance(self.role, str) or not isinstance(self.filename, str):
            raise ReleaseError("Invalid release artifact identity")

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "filename": self.filename, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_dict(cls, value: object) -> ReleaseArtifact:
        data = _fields(value, {"role", "filename", "sha256", "size"})
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    version: str
    source_commit: str
    runtime: RuntimeContract
    artifacts: tuple[ReleaseArtifact, ...]

    def __post_init__(self) -> None:
        _version(self.version)
        _hex(self.source_commit, _COMMIT)
        if not isinstance(self.runtime, RuntimeContract) or self.runtime.runtime_version != self.version:
            raise ReleaseError("Manifest and runtime versions differ")
        if type(self.artifacts) is not tuple or not all(isinstance(x, ReleaseArtifact) for x in self.artifacts):
            raise ReleaseError("Manifest artifacts must be immutable contracts")
        expected = artifact_names(self.version)
        if [x.role for x in self.artifacts] != sorted(expected):
            raise ReleaseError("Manifest must contain exactly the sorted official package roles")
        if any(expected[x.role] != x.filename for x in self.artifacts):
            raise ReleaseError("Unexpected artifact name or path")

    def to_dict(self) -> dict[str, Any]:
        return {"profile": MANIFEST_PROFILE, "repository": REPOSITORY,
                "version": self.version, "tag": "v" + self.version,
                "source_commit": self.source_commit, "runtime": self.runtime.to_dict(),
                "artifacts": [x.to_dict() for x in self.artifacts]}

    @property
    def canonical(self) -> bytes:
        raw = canonical_bytes(self.to_dict())
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ReleaseError("Release manifest exceeds its byte bound")
        return raw

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()

    @classmethod
    def from_bytes(cls, raw: bytes) -> ReleaseManifest:
        data = _fields(_document(raw), {"profile", "repository", "version", "tag", "source_commit", "runtime", "artifacts"})
        if data["profile"] != MANIFEST_PROFILE or data["repository"] != REPOSITORY:
            raise ReleaseError("Unsupported manifest profile or repository")
        version = _version(data["version"])
        if data["tag"] != "v" + version or type(data["artifacts"]) is not list or len(data["artifacts"]) != 5:
            raise ReleaseError("Unexpected release tag or artifact set")
        return cls(version, data["source_commit"], RuntimeContract.from_dict(data["runtime"]),
                   tuple(ReleaseArtifact.from_dict(x) for x in data["artifacts"]))


@dataclass(frozen=True, slots=True)
class ReleasePin:
    """Independently selected inputs, never copied from the downloaded manifest."""
    version: str
    source_commit: str
    manifest_sha256: str
    release_key_sha256: str

    def __post_init__(self) -> None:
        _version(self.version)
        _hex(self.source_commit, _COMMIT)
        _hex(self.manifest_sha256, _HASH)
        _hex(self.release_key_sha256, _HASH)


def verify_release(raw: bytes, signature: bytes, pin: ReleasePin, public_key: bytes) -> ReleaseManifest:
    """Verify a separate release authority, not an Agent/operator signing key.

    Requires the existing governance extra for public Ed25519 verification.
    Successful verification does not establish freshness or authorize installation.
    """
    if not isinstance(pin, ReleasePin) or type(public_key) is not bytes or len(public_key) != 32:
        raise ReleaseError("Expected an independent release pin and public key")
    manifest = ReleaseManifest.from_bytes(raw)
    if (manifest.version != pin.version or manifest.source_commit != pin.source_commit
            or manifest.digest != pin.manifest_sha256
            or hashlib.sha256(public_key).hexdigest() != pin.release_key_sha256):
        raise ReleaseError("Release does not match independently selected pins")
    envelope = _fields(_document(signature), {"profile", "manifest_sha256", "release_key_sha256", "signature"})
    if (envelope["profile"] != SIGNATURE_PROFILE
            or envelope["manifest_sha256"] != manifest.digest
            or envelope["release_key_sha256"] != pin.release_key_sha256
            or not isinstance(envelope["signature"], str)
            or not re.fullmatch(r"[0-9a-f]{128}", envelope["signature"])):
        raise ReleaseError("Invalid release signature envelope")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:
        raise ReleaseError("Install syn-client[governance] for release signature verification") from error
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            bytes.fromhex(envelope["signature"]), SIGNATURE_DOMAIN + raw,
        )
    except (InvalidSignature, ValueError) as error:
        raise ReleaseError("Release signature verification failed") from error
    return manifest


def verify_artifact(artifact: ReleaseArtifact, raw: bytes) -> None:
    """Pure bounded byte verification; this function writes or executes nothing."""
    if not isinstance(artifact, ReleaseArtifact) or type(raw) is not bytes:
        raise ReleaseError("Expected a release artifact and its exact bytes")
    if len(raw) != artifact.size or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise ReleaseError("Artifact size or digest differs from its manifest")


def check_compatibility(
    manifest: ReleaseManifest,
    runtime: RuntimeContract,
    *,
    component: str,
    platform: str,
    python_version: str,
    required_profiles: tuple[str, ...],
    required_capabilities: tuple[str, ...],
    required_mode: str | None = None,
) -> None:
    """Check exact declared compatibility after trust/artifact verification.

    A descriptor is a claim, not runtime attestation. Host/migration/custody
    acceptance and live admission remain SYN-R03 onward.
    """
    if not isinstance(manifest, ReleaseManifest) or not isinstance(runtime, RuntimeContract):
        raise ReleaseError("Expected validated release and runtime contracts")
    if runtime != manifest.runtime:
        raise ReleaseError("Installed runtime contract differs from the selected release")
    if not isinstance(component, str) or component not in {"runtime", "client"}:
        raise ReleaseError("Unknown Syn release component")
    platforms = runtime.runtime_platforms if component == "runtime" else runtime.client_platforms
    if platform not in platforms or python_version not in runtime.python_versions:
        raise ReleaseError("Unsupported declared platform or Python version")
    for required, available in ((required_profiles, runtime.api_profiles), (required_capabilities, runtime.capabilities)):
        if type(required) is not tuple or any(not isinstance(x, str) for x in required) or len(required) != len(set(required)):
            raise ReleaseError("Compatibility requirements must be explicit unique tuples")
        if not set(required) <= set(available):
            raise ReleaseError("Required API profile or capability is unavailable")
    if component == "client" and required_mode is not None:
        raise ReleaseError("A client wheel is not a Syn node")
    if required_mode is not None and (not isinstance(required_mode, str) or required_mode not in _MODES or required_mode not in runtime.node_modes):
        raise ReleaseError("Required Syn node mode is unavailable")
