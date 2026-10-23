"""Immutable Type policy contracts and pinned, offline resolution.

This is not an agreement engine, membership verifier or governance authority.
Trust pins and stream bindings must come from the caller's adopted configuration.
New hashes use syn.consensus-json/1, not JCS or the legacy receipt codec.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Self
from uuid import UUID

MAX_DOCUMENT_BYTES = 2_097_152
MAX_TYPES = 10_000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REF = re.compile(r"(?:syn|synny)://type/([a-z0-9]+(?:[-_][a-z0-9]+)*)\Z")
_NAME = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*\Z")
_REVISION = re.compile(r"[1-9][0-9]{0,15}\Z")
_PROFILES = {
    "syn.operator-log/1": (
        "same-verified-issuer",
        "strict-majority",
        "crash-and-partition",
        "syn.operator-commit/1",
    ),
    "syn.bft-dag-hashgraph/1": (
        "independent-issuer-weight-caps",
        "bft-supermajority",
        "byzantine-less-than-one-third-weight",
        "syn.dag-hashgraph-finality/1",
    ),
}


class PolicyError(ValueError):
    """Closed policy/identity validation failure; no payload or secrets in errors."""


def digest_text(value: object) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise PolicyError("Expected a canonical SHA-256 digest")
    return value


class NodeKeyHash(str):
    """A node's account-key hash, never an issuer label or transport locator."""

    __slots__ = ()

    def __new__(cls, value: object) -> Self:
        return str.__new__(cls, digest_text(value))

    @classmethod
    def from_public_key(cls, key: bytes) -> Self:
        if type(key) is not bytes or len(key) != 32:
            raise PolicyError("Node identity requires a raw 32-byte Ed25519 public key")
        return cls(hashlib.sha256(key).hexdigest())


@dataclass(frozen=True, slots=True)
class NodeIdentity:
    node_key_hash: NodeKeyHash
    public_key: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.node_key_hash, NodeKeyHash):
            raise PolicyError("Node reference requires a NodeKeyHash")
        if NodeKeyHash.from_public_key(self.public_key) != self.node_key_hash:
            raise PolicyError("Node public key does not match the expected node hash")


def type_ref(value: object) -> str:
    if not isinstance(value, str):
        raise PolicyError("Expected a canonical Type reference")
    match = _REF.fullmatch(value)
    if match is None or len(match[1]) > 255:
        raise PolicyError("Expected a canonical Type reference")
    return "syn://type/" + match[1]


def _revision(value: object) -> None:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise PolicyError("Revision/epoch must be a bounded positive decimal string")


def _uuid(value: object) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise PolicyError("Expected a canonical UUID") from error


def _text(value: object, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PolicyError("Expected bounded nonempty text")
    if any(0xD800 <= ord(c) <= 0xDFFF or ord(c) < 32 for c in value):
        raise PolicyError("Text contains an unsupported control or surrogate")


def canonical_bytes(value: object) -> bytes:
    """syn.consensus-json/1: UTF-8, sorted Unicode keys, bounded exact integers.

    No floats, duplicate keys, surrogates or implicit normalization. This profile
    deliberately does not claim RFC 8785 compatibility. New signed contracts must
    name it explicitly; legacy receipt and membership bytes remain untouched.
    """

    def check(item: object, depth: int = 0) -> None:
        if depth > 16:
            raise PolicyError("Consensus document is too deeply nested")
        if item is None or type(item) is bool:
            return
        if type(item) is int and abs(item) <= 9_007_199_254_740_991:
            return
        if isinstance(item, str):
            if any(0xD800 <= ord(c) <= 0xDFFF for c in item):
                raise PolicyError("Consensus text contains a surrogate")
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(isinstance(k, str) for k in item):
            for key, child in item.items():
                check(key, depth + 1)
                check(child, depth + 1)
            return
        raise PolicyError("Consensus JSON contains an unsupported value")

    check(value)
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    if len(result) > MAX_DOCUMENT_BYTES:
        raise PolicyError("Consensus document exceeds its byte limit")
    return result


def load_document(value: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise PolicyError("Consensus document must be JSON text")
    try:
        if len(value.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise PolicyError("Consensus document exceeds its byte limit")

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, child in items:
                if key in result:
                    raise PolicyError("Duplicate consensus JSON field")
                result[key] = child
            return result

        data = json.loads(value, object_pairs_hook=pairs)
        if not isinstance(data, dict):
            raise PolicyError("Consensus document must be an object")
        canonical_bytes(data)
        return data
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise PolicyError("Invalid consensus JSON document") from error


def _fields(value: object, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise PolicyError("Consensus fields do not match the declared profile")
    return value


def _digest(profile: str, value: object) -> str:
    return hashlib.sha256(profile.encode("ascii") + b"\0" + canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ConsensusPolicy:
    profile: str
    policy_revision: str
    protocol: str
    custody_constraint: str
    membership_anchor_digest: str
    participant_reference: str
    quorum_rule: str
    fault_model: str
    proof_profile: str
    conflict_namespace: str
    change_rule: str

    def __post_init__(self) -> None:
        if self.profile != "syn.consensus-policy/1":
            raise PolicyError("Unsupported consensus policy profile")
        _revision(self.policy_revision)
        digest_text(self.membership_anchor_digest)
        if not isinstance(self.protocol, str) or self.protocol not in _PROFILES:
            raise PolicyError("Unsupported consensus protocol; hold without fallback")
        actual = (self.custody_constraint, self.quorum_rule, self.fault_model, self.proof_profile)
        if actual != _PROFILES[self.protocol]:
            raise PolicyError("Consensus protocol and trust/proof rules disagree")
        if self.participant_reference != "node-public-key-sha256":
            raise PolicyError("Participants must use node public-key hashes")
        if self.change_rule != "current-policy-and-type-governance":
            raise PolicyError("Policy changes require existing policy and Type governance")
        if (
            not isinstance(self.conflict_namespace, str)
            or len(self.conflict_namespace) > 128
            or not _NAME.fullmatch(self.conflict_namespace)
        ):
            raise PolicyError("Invalid conflict namespace")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(value, set(cls.__dataclass_fields__))
        return cls(**data)

    @property
    def digest(self) -> str:
        return _digest("syn.consensus-policy/1", self.to_dict())


@dataclass(frozen=True, slots=True)
class CanonicalTypeDefinition:
    type_ref: str
    type_revision: str
    definition_json: str
    consensus_policy: ConsensusPolicy | None

    def __post_init__(self) -> None:
        # Definition contains the exact, bounded application schema chosen by
        # its author. Its hash commits all fields, not only the policy.
        if type_ref(self.type_ref) != self.type_ref:
            raise PolicyError("Canonical Type definitions use the syn:// spelling")
        _revision(self.type_revision)
        definition = load_document(self.definition_json)
        if "consensusPolicy" in definition:
            raise PolicyError("Consensus policy must be first-class, not nested in definition")
        if self.definition_json.encode("utf-8") != canonical_bytes(definition):
            raise PolicyError("Type definition JSON must use syn.consensus-json/1")
        if self.consensus_policy is not None and not isinstance(
            self.consensus_policy, ConsensusPolicy
        ):
            raise PolicyError("Expected an explicit typed consensus policy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": "syn.canonical-type/1",
            "type_ref": self.type_ref,
            "type_revision": self.type_revision,
            "definition": load_document(self.definition_json),
            "consensusPolicy": self.consensus_policy.to_dict() if self.consensus_policy else None,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _fields(
            value, {"profile", "type_ref", "type_revision", "definition", "consensusPolicy"}
        )
        if data["profile"] != "syn.canonical-type/1" or not isinstance(data["definition"], dict):
            raise PolicyError("Unsupported canonical Type definition")
        policy = data["consensusPolicy"]
        return cls(
            data["type_ref"],
            data["type_revision"],
            canonical_bytes(data["definition"]).decode("utf-8"),
            ConsensusPolicy.from_dict(policy) if policy is not None else None,
        )

    @property
    def digest(self) -> str:
        return _digest("syn.canonical-type/1", self.to_dict())


@dataclass(frozen=True, slots=True)
class CanonicalTypeRelease:
    catalog_id: str
    release_revision: str
    types: tuple[CanonicalTypeDefinition, ...]

    def __post_init__(self) -> None:
        _uuid(self.catalog_id)
        _revision(self.release_revision)
        if (
            not isinstance(self.types, tuple)
            or not 1 <= len(self.types) <= MAX_TYPES
            or any(not isinstance(t, CanonicalTypeDefinition) for t in self.types)
        ):
            raise PolicyError("Release requires a bounded tuple of canonical Types")
        refs = [t.type_ref for t in self.types]
        if refs != sorted(set(refs)):
            raise PolicyError("Release Types must be unique and sorted by canonical reference")
        canonical_bytes(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": "syn.type-release/1",
            "catalog_id": self.catalog_id,
            "release_revision": self.release_revision,
            "types": [t.to_dict() for t in self.types],
        }

    @classmethod
    def from_json(cls, value: str) -> Self:
        data = _fields(load_document(value), {"profile", "catalog_id", "release_revision", "types"})
        if data["profile"] != "syn.type-release/1" or not isinstance(data["types"], list):
            raise PolicyError("Unsupported canonical Type release")
        return cls(
            data["catalog_id"],
            data["release_revision"],
            tuple(CanonicalTypeDefinition.from_dict(t) for t in data["types"]),
        )

    def to_json(self) -> str:
        return canonical_bytes(self.to_dict()).decode("utf-8")

    @property
    def digest(self) -> str:
        return _digest("syn.type-release/1", self.to_dict())


@dataclass(frozen=True, slots=True)
class PolicyBinding:
    """Caller-trusted stream configuration, not evidence of governance approval."""

    policy_digest: str
    domain_id: str
    genesis_digest: str
    stream_id: str
    policy_epoch: str
    roster_digest: str

    def __post_init__(self) -> None:
        for value in (self.policy_digest, self.genesis_digest, self.roster_digest):
            digest_text(value)
        for value in (self.domain_id, self.stream_id):
            _uuid(value)
        _revision(self.policy_epoch)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(**_fields(value, set(cls.__dataclass_fields__)))


@dataclass(frozen=True, slots=True)
class TypePolicyPin:
    type_ref: str
    type_definition_digest: str
    consensus_policy_digest: str

    def __post_init__(self) -> None:
        type_ref(self.type_ref)
        digest_text(self.type_definition_digest)
        digest_text(self.consensus_policy_digest)


@dataclass(frozen=True, slots=True)
class PolicyContext:
    catalog_id: str
    semantic_release_digest: str
    canonical_type_ref: str
    type_definition_digest: str
    consensus_policy_digest: str
    protocol: str
    proof_profile: str
    conflict_namespace: str
    binding: PolicyBinding

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CanonicalTypePolicyResolver:
    """Resolve an independently pinned release; never auto-adopt remote policy."""

    release: CanonicalTypeRelease
    expected_catalog_id: str
    expected_release_digest: str
    bindings: tuple[PolicyBinding, ...]
    _definitions: Mapping[str, CanonicalTypeDefinition] = field(
        init=False, repr=False, compare=False
    )
    _contexts: Mapping[str, PolicyContext] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _uuid(self.expected_catalog_id)
        digest_text(self.expected_release_digest)
        if (
            not isinstance(self.release, CanonicalTypeRelease)
            or self.release.catalog_id != self.expected_catalog_id
            or self.release.digest != self.expected_release_digest
        ):
            raise PolicyError("Canonical release does not match independently selected trust pins")
        if not isinstance(self.bindings, tuple) or any(
            not isinstance(b, PolicyBinding) for b in self.bindings
        ):
            raise PolicyError("Bindings require an immutable tuple")
        by_policy = {b.policy_digest: b for b in self.bindings}
        if len(by_policy) != len(self.bindings):
            raise PolicyError("Duplicate policy stream binding")
        owners: dict[tuple[str, str, str], tuple[str, str]] = {}
        contexts: dict[str, PolicyContext] = {}
        for definition in self.release.types:
            policy = definition.consensus_policy
            if policy is None or policy.digest not in by_policy:
                continue  # Readable legacy/unbound Type; resolve fails closed.
            binding = by_policy[policy.digest]
            if binding.roster_digest != policy.membership_anchor_digest:
                raise PolicyError("Policy membership anchor disagrees with stream roster")
            key = (binding.domain_id, binding.genesis_digest, policy.conflict_namespace)
            owner = (policy.digest, binding.stream_id)
            if key in owners and owners[key] != owner:
                raise PolicyError("Conflicting policy ownership of one mutable namespace")
            owners[key] = owner
            contexts[definition.type_ref] = PolicyContext(
                self.release.catalog_id,
                self.expected_release_digest,
                definition.type_ref,
                definition.digest,
                policy.digest,
                policy.protocol,
                policy.proof_profile,
                policy.conflict_namespace,
                binding,
            )
        object.__setattr__(
            self, "_definitions", MappingProxyType({t.type_ref: t for t in self.release.types})
        )
        object.__setattr__(self, "_contexts", MappingProxyType(contexts))

    def resolve(self, pin: TypePolicyPin) -> PolicyContext:
        if not isinstance(pin, TypePolicyPin):
            raise PolicyError("Resolution requires exact Type and policy pins")
        definition = self._definitions.get(type_ref(pin.type_ref))
        if definition is None:
            raise PolicyError("Unknown Type in the selected canonical release")
        policy = definition.consensus_policy
        if policy is None:
            raise PolicyError("Type has no explicit consensus policy; hold")
        context = self._contexts.get(definition.type_ref)
        if context is None:
            raise PolicyError("Type policy has no adopted stream binding; hold")
        if (
            context.type_definition_digest != pin.type_definition_digest
            or context.consensus_policy_digest != pin.consensus_policy_digest
        ):
            raise PolicyError("Type definition or policy pin mismatch")
        return context

    def resolve_write_set(self, pins: tuple[TypePolicyPin, ...]) -> tuple[PolicyContext, ...]:
        if not isinstance(pins, tuple) or not 1 <= len(pins) <= 256:
            raise PolicyError("Write set requires 1..256 exact Type policy pins")
        contexts = tuple(self.resolve(pin) for pin in pins)
        first = contexts[0]
        if any(
            c.binding != first.binding or c.consensus_policy_digest != first.consensus_policy_digest
            for c in contexts
        ):
            raise PolicyError("Atomic write set spans incompatible policy contexts")
        return contexts
