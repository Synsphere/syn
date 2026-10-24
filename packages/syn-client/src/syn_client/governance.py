"""Signed local configuration governance; never a Raft or DAG finality proof.

Records are append-only successors. Existing policy rosters AND the previous
Type-governance authority approve the exact successor, including authority changes.
Federated transitions hold until a real finality verifier is implemented.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Self

from .consensus import (
    CanonicalTypePolicyResolver,
    CanonicalTypeRelease,
    ConsensusPolicy,
    PolicyBinding,
    PolicyError,
    _digest,
    _fields,
    _revision,
    _uuid,
    canonical_bytes,
    digest_text,
    load_document,
)
from .custody import verify_agent_approval, verify_membership

MAX_CHAIN = 128


@dataclass(frozen=True, slots=True)
class ApprovalAuthority:
    threshold: int
    memberships_json: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.memberships_json, tuple)
            or not 1 <= len(self.memberships_json) <= 128
            or type(self.threshold) is not int
            or not 1 <= self.threshold <= len(self.memberships_json)
        ):
            raise PolicyError("Authority requires a bounded nonempty roster and threshold")
        members = self.memberships()
        nodes = [m["agent"]["signer_hash"] for m in members]
        assignments = {(m["operator"]["signer_hash"], m["account_number"]) for m in members}
        instances = {m["instance_id"] for m in members}
        if (
            nodes != sorted(set(nodes))
            or len(assignments) != len(nodes)
            or len(instances) != len(nodes)
        ):
            raise PolicyError("Authority nodes, accounts and instances must be unique and sorted")
        canonical_bytes(self.to_dict())

    def memberships(self) -> tuple[dict[str, Any], ...]:
        result = []
        for raw in self.memberships_json:
            data = load_document(raw)
            if raw.encode("utf-8") != canonical_bytes(data):
                raise PolicyError("Authority assignments must use canonical JSON")
            result.append(verify_membership(data))
        return tuple(result)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": "syn.approval-authority/1",
            "threshold": self.threshold,
            "memberships": [load_document(m) for m in self.memberships_json],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        d = _fields(value, {"profile", "threshold", "memberships"})
        if d["profile"] != "syn.approval-authority/1" or not isinstance(d["memberships"], list):
            raise PolicyError("Unsupported approval authority")
        return cls(d["threshold"], tuple(canonical_bytes(m).decode() for m in d["memberships"]))

    @property
    def digest(self) -> str:
        return _digest("syn.approval-authority/1", self.to_dict())

    def verify(self, proofs: object, payload: str, now: int | None) -> None:
        if not isinstance(proofs, list) or not self.threshold <= len(proofs) <= len(
            self.memberships_json
        ):
            raise PolicyError("Approval quorum is missing")
        members = {m["agent"]["signer_hash"]: m for m in self.memberships()}
        nodes = []
        for proof in proofs:
            if not isinstance(proof, dict):
                raise PolicyError("Invalid approval proof")
            credential = proof.get("membership")
            if not isinstance(credential, dict) or not isinstance(credential.get("agent"), dict):
                raise PolicyError("Invalid approval assignment")
            node = credential["agent"].get("signer_hash")
            if not isinstance(node, str) or node not in members:
                raise PolicyError("Approval signer is outside the existing authority")
            nodes.append(
                verify_agent_approval(
                    proof, membership=members[node], payload_digest=payload, now=now
                )
            )
        if nodes != sorted(set(nodes)):
            raise PolicyError("Duplicate or unsorted approval signers")


@dataclass(frozen=True, slots=True)
class GovernedPolicyState:
    workspace_id: str
    release: CanonicalTypeRelease
    bindings: tuple[PolicyBinding, ...]
    type_governance: ApprovalAuthority
    policy_authorities: tuple[ApprovalAuthority, ...]

    def __post_init__(self) -> None:
        _uuid(self.workspace_id)
        if not isinstance(self.release, CanonicalTypeRelease) or not isinstance(
            self.type_governance, ApprovalAuthority
        ):
            raise PolicyError("Governed state requires a canonical release and Type authority")
        if not isinstance(self.bindings, tuple) or any(
            not isinstance(b, PolicyBinding) for b in self.bindings
        ):
            raise PolicyError("Governed state requires immutable bindings")
        if not isinstance(self.policy_authorities, tuple) or any(
            not isinstance(a, ApprovalAuthority) for a in self.policy_authorities
        ):
            raise PolicyError("Governed state requires immutable policy authorities")
        policies = {
            t.consensus_policy.digest: t.consensus_policy
            for t in self.release.types
            if t.consensus_policy
        }
        binding_ids = [b.policy_digest for b in self.bindings]
        authorities = {a.digest: a for a in self.policy_authorities}
        if (
            not policies
            or binding_ids != sorted(policies)
            or len(authorities) != len(self.policy_authorities)
            or [a.digest for a in self.policy_authorities] != sorted(authorities)
            or set(authorities) != {p.membership_anchor_digest for p in policies.values()}
        ):
            raise PolicyError("All Type policies need exact, sorted bindings and roster anchors")
        domain_anchors: dict[str, str] = {}
        for b in self.bindings:
            if domain_anchors.setdefault(b.domain_id, b.genesis_digest) != b.genesis_digest:
                raise PolicyError("A domain cannot have multiple genesis anchors")
        for p in policies.values():
            a = authorities[p.membership_anchor_digest]
            members = a.memberships()
            roots = {m["operator"]["signer_hash"] for m in members}
            if p.protocol == "syn.operator-log/1":
                if len(roots) != 1 or a.threshold != len(members) // 2 + 1:
                    raise PolicyError(
                        "Operator policy requires one verified issuer and strict majority"
                    )
            elif len(roots) != len(members) or a.threshold != (2 * len(members)) // 3 + 1:
                raise PolicyError(
                    "Federated roster requires independent issuers and a supermajority"
                )
        CanonicalTypePolicyResolver(
            self.release, self.release.catalog_id, self.release.digest, self.bindings
        )
        canonical_bytes(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": "syn.governed-policy-state/1",
            "workspace_id": self.workspace_id,
            "release": self.release.to_dict(),
            "bindings": [asdict(b) for b in self.bindings],
            "type_governance": self.type_governance.to_dict(),
            "policy_authorities": [a.to_dict() for a in self.policy_authorities],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        d = _fields(
            value,
            {
                "profile",
                "workspace_id",
                "release",
                "bindings",
                "type_governance",
                "policy_authorities",
            },
        )
        if (
            d["profile"] != "syn.governed-policy-state/1"
            or not isinstance(d["bindings"], list)
            or not isinstance(d["policy_authorities"], list)
        ):
            raise PolicyError("Unsupported governed state")
        return cls(
            d["workspace_id"],
            CanonicalTypeRelease.from_json(canonical_bytes(d["release"]).decode()),
            tuple(PolicyBinding.from_dict(b) for b in d["bindings"]),
            ApprovalAuthority.from_dict(d["type_governance"]),
            tuple(ApprovalAuthority.from_dict(a) for a in d["policy_authorities"]),
        )

    @property
    def digest(self) -> str:
        return _digest("syn.governed-policy-state/1", self.to_dict())


@dataclass(frozen=True, slots=True)
class GovernedPolicyRecord:
    sequence: str
    previous_record_digest: str
    state: GovernedPolicyState
    approvals_json: str

    def __post_init__(self) -> None:
        _revision(self.sequence)
        digest_text(self.previous_record_digest)
        if not isinstance(self.state, GovernedPolicyState):
            raise PolicyError("Record requires a governed state")
        d = _fields(load_document(self.approvals_json), {"type_governance", "existing_policies"})
        if not isinstance(d["type_governance"], list) or not isinstance(
            d["existing_policies"], dict
        ):
            raise PolicyError("Invalid governance approval sets")
        if self.approvals_json.encode() != canonical_bytes(d):
            raise PolicyError("Approval sets must use canonical JSON")
        canonical_bytes(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": "syn.governed-policy-record/1",
            "sequence": self.sequence,
            "previous_record_digest": self.previous_record_digest,
            "state": self.state.to_dict(),
            "approvals": load_document(self.approvals_json),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        d = _fields(value, {"profile", "sequence", "previous_record_digest", "state", "approvals"})
        if d["profile"] != "syn.governed-policy-record/1":
            raise PolicyError("Unsupported governed record")
        return cls(
            d["sequence"],
            d["previous_record_digest"],
            GovernedPolicyState.from_dict(d["state"]),
            canonical_bytes(d["approvals"]).decode(),
        )

    @classmethod
    def from_json(cls, value: str) -> Self:
        return cls.from_dict(load_document(value))

    def to_json(self) -> str:
        return canonical_bytes(self.to_dict()).decode()

    @property
    def digest(self) -> str:
        return _digest("syn.governed-policy-record/1", self.to_dict())

    @property
    def proposal_digest(self) -> str:
        return _digest(
            "syn.policy-proposal/1",
            {
                "sequence": self.sequence,
                "previous_record_digest": self.previous_record_digest,
                "state_digest": self.state.digest,
            },
        )

    def approval_payload(
        self, authority: ApprovalAuthority, policy_digest: str | None = None
    ) -> str:
        if policy_digest is not None:
            digest_text(policy_digest)
        return _digest(
            "syn.governance-authorization/1",
            {
                "role": "existing-policy" if policy_digest is not None else "type-governance",
                "proposal_digest": self.proposal_digest,
                "authority_digest": authority.digest,
                "policy_digest": policy_digest,
            },
        )


def verify_genesis(
    record: GovernedPolicyRecord,
    *,
    expected_workspace: str,
    expected_catalog: str,
    expected_genesis_digest: str,
    now: int | None = None,
) -> None:
    _uuid(expected_workspace)
    _uuid(expected_catalog)
    digest_text(expected_genesis_digest)
    if (
        record.digest != expected_genesis_digest
        or record.state.workspace_id != expected_workspace
        or record.state.release.catalog_id != expected_catalog
        or record.sequence != "1"
        or record.previous_record_digest != "0" * 64
        or any(b.policy_epoch != "1" for b in record.state.bindings)
    ):
        raise PolicyError("Signed genesis does not match independent scope and trust pins")
    approvals = load_document(record.approvals_json)
    if approvals["existing_policies"]:
        raise PolicyError("Genesis has no existing-policy approval authority")
    authority = record.state.type_governance
    authority.verify(approvals["type_governance"], record.approval_payload(authority), now)


def _successor_policies(
    previous: GovernedPolicyRecord, successor: GovernedPolicyRecord
) -> dict[str, ConsensusPolicy]:
    old, new = previous.state, successor.state
    if (
        successor.previous_record_digest != previous.digest
        or int(successor.sequence) != int(previous.sequence) + 1
        or old.workspace_id != new.workspace_id
        or old.release.catalog_id != new.release.catalog_id
        or int(new.release.release_revision) != int(old.release.release_revision) + 1
    ):
        raise PolicyError("Transition scope, predecessor or release sequence mismatch")
    old_bindings = {b.policy_digest: b for b in old.bindings}
    new_bindings = {b.policy_digest: b for b in new.bindings}
    new_types = {t.type_ref: t for t in new.release.types}
    covered_new: set[str] = set()
    policies = {
        t.consensus_policy.digest: t.consensus_policy
        for t in old.release.types
        if t.consensus_policy
    }
    for t in old.release.types:
        n = new_types.get(t.type_ref)
        if n is None or (n != t and int(n.type_revision) != int(t.type_revision) + 1):
            raise PolicyError("Transition cannot drop Types or skip their revisions")
        if t.consensus_policy is None:
            continue
        p, q = t.consensus_policy, n.consensus_policy
        if q is None or q.conflict_namespace != p.conflict_namespace:
            raise PolicyError("Transition cannot remove or rename existing policy protection")
        if q != p and int(q.policy_revision) != int(p.policy_revision) + 1:
            raise PolicyError("Transition must advance changed policy revisions")
        a, b = old_bindings[p.digest], new_bindings[q.digest]
        covered_new.add(q.digest)
        if (a.domain_id, a.genesis_digest, a.stream_id) != (
            b.domain_id,
            b.genesis_digest,
            b.stream_id,
        ) or int(b.policy_epoch) != int(a.policy_epoch) + 1:
            raise PolicyError("Transition cannot fork anchors/streams or skip policy epochs")
    if any(b.policy_epoch != "1" for d, b in new_bindings.items() if d not in covered_new):
        raise PolicyError("New policy streams must begin at epoch one")
    old_refs = {t.type_ref for t in old.release.types}
    if any(t.type_revision != "1" for t in new.release.types if t.type_ref not in old_refs):
        raise PolicyError("New Types must begin at revision one")
    if any(p.protocol != "syn.operator-log/1" for p in policies.values()):
        raise PolicyError(
            "Federated policy transition requires the unimplemented DAG finality verifier"
        )
    return policies


def required_approval_authority(
    proposal: GovernedPolicyRecord,
    previous: GovernedPolicyRecord | None,
    policy_digest: str | None,
) -> ApprovalAuthority:
    """Select authority from the verified predecessor, never the candidate roster."""
    if previous is None:
        if (
            proposal.sequence != "1"
            or proposal.previous_record_digest != "0" * 64
            or policy_digest is not None
            or any(b.policy_epoch != "1" for b in proposal.state.bindings)
        ):
            raise PolicyError("Bootstrap approval requires an explicitly selected genesis proposal")
        return proposal.state.type_governance
    policies = _successor_policies(previous, proposal)
    if policy_digest is None:
        return previous.state.type_governance
    if policy_digest not in policies:
        raise PolicyError("Approval target is not an existing policy")
    anchor = policies[policy_digest].membership_anchor_digest
    return next(a for a in previous.state.policy_authorities if a.digest == anchor)


def verify_transition(
    previous: GovernedPolicyRecord, successor: GovernedPolicyRecord, *, now: int | None = None
) -> None:
    policies = _successor_policies(previous, successor)
    old = previous.state
    # Check every old policy, including unchanged siblings. A proposal cannot
    # omit an inconvenient protected Type or let a weaker new policy ratify itself.
    approvals = load_document(successor.approvals_json)
    if set(approvals["existing_policies"]) != set(policies):
        raise PolicyError("Transition requires approval under every existing Type policy")
    authorities = {a.digest: a for a in old.policy_authorities}
    for digest, policy in policies.items():
        authority = authorities[policy.membership_anchor_digest]
        authority.verify(
            approvals["existing_policies"][digest],
            successor.approval_payload(authority, digest),
            now,
        )
    old.type_governance.verify(
        approvals["type_governance"], successor.approval_payload(old.type_governance), now
    )


def verified_chain(
    records: tuple[GovernedPolicyRecord, ...],
    *,
    expected_workspace: str,
    expected_catalog: str,
    expected_genesis_digest: str,
    expected_head_digest: str,
) -> CanonicalTypePolicyResolver:
    digest_text(expected_head_digest)
    if not isinstance(records, tuple) or not 1 <= len(records) <= MAX_CHAIN:
        raise PolicyError("Governance history exceeds the bounded verification window")
    canonical_bytes(
        {"profile": "syn.governance-chain/1", "records": [r.to_dict() for r in records]}
    )
    verify_genesis(
        records[0],
        expected_workspace=expected_workspace,
        expected_catalog=expected_catalog,
        expected_genesis_digest=expected_genesis_digest,
    )
    for previous, successor in zip(records, records[1:]):
        verify_transition(previous, successor)
    head = records[-1]
    if head.digest != expected_head_digest:
        raise PolicyError("Governance history does not reach the independently retained head")
    state = head.state
    return CanonicalTypePolicyResolver(
        state.release, expected_catalog, state.release.digest, state.bindings
    )
