"""Explicit local genesis trust; this document is not a governance certificate."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Self

from .consensus import (
    CanonicalTypePolicyResolver,
    CanonicalTypeRelease,
    PolicyBinding,
    PolicyError,
    _digest,
    _fields,
    _uuid,
    canonical_bytes,
    digest_text,
    load_document,
)


@dataclass(frozen=True, slots=True)
class GenesisPolicyAdoption:
    workspace_id: str
    release: CanonicalTypeRelease
    bindings: tuple[PolicyBinding, ...]

    def __post_init__(self) -> None:
        _uuid(self.workspace_id)
        if not isinstance(self.release, CanonicalTypeRelease):
            raise PolicyError("Genesis requires a canonical Type release")
        if (
            not isinstance(self.bindings, tuple)
            or not self.bindings
            or any(not isinstance(b, PolicyBinding) for b in self.bindings)
        ):
            raise PolicyError("Genesis requires explicit immutable stream bindings")
        policies = {
            t.consensus_policy.digest for t in self.release.types if t.consensus_policy is not None
        }
        digests = [b.policy_digest for b in self.bindings]
        if digests != sorted(set(digests)) or not set(digests) <= policies:
            raise PolicyError("Genesis bindings must be unique, sorted and present in the release")
        domains: dict[str, str] = {}
        for binding in self.bindings:
            if binding.policy_epoch != "1":
                raise PolicyError("Local genesis can initialize only policy epoch 1")
            if (
                domains.setdefault(binding.domain_id, binding.genesis_digest)
                != binding.genesis_digest
            ):
                raise PolicyError("Genesis binds one immutable anchor per domain")
        CanonicalTypePolicyResolver(
            self.release, self.release.catalog_id, self.release.digest, self.bindings
        )
        canonical_bytes(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": "syn.policy-genesis/1",
            "trust_basis": "explicit-local-genesis-pin",
            "workspace_id": self.workspace_id,
            "release": self.release.to_dict(),
            "bindings": [asdict(b) for b in self.bindings],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(value, {"profile", "trust_basis", "workspace_id", "release", "bindings"})
        if (
            data["profile"] != "syn.policy-genesis/1"
            or data["trust_basis"] != "explicit-local-genesis-pin"
            or not isinstance(data["bindings"], list)
        ):
            raise PolicyError("Unsupported policy adoption profile or trust basis")
        return cls(
            data["workspace_id"],
            CanonicalTypeRelease.from_json(canonical_bytes(data["release"]).decode("utf-8")),
            tuple(PolicyBinding.from_dict(b) for b in data["bindings"]),
        )

    @classmethod
    def from_json(cls, value: str) -> Self:
        return cls.from_dict(load_document(value))

    def to_json(self) -> str:
        return canonical_bytes(self.to_dict()).decode("utf-8")

    @property
    def digest(self) -> str:
        return _digest("syn.policy-genesis/1", self.to_dict())

    def resolver(
        self, *, expected_workspace_id: str, expected_catalog_id: str, expected_adoption_digest: str
    ) -> CanonicalTypePolicyResolver:
        """The expected digest must be retained independently of the received document."""
        _uuid(expected_workspace_id)
        _uuid(expected_catalog_id)
        digest_text(expected_adoption_digest)
        if (
            self.workspace_id != expected_workspace_id
            or self.release.catalog_id != expected_catalog_id
            or self.digest != expected_adoption_digest
        ):
            raise PolicyError("Policy adoption does not match independently selected trust pins")
        return CanonicalTypePolicyResolver(
            self.release, expected_catalog_id, self.release.digest, self.bindings
        )
