"""Bounded Hashgraph governance replay, not a network engine or Hiero wire codec.

Equal-weight, fixed-roster epochs only. Coin elections that need randomness stay
unresolved. No liveness, freshness, availability or production qualification is
inferred from accepting a finite, ancestor-complete history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .consensus import (
    NodeKeyHash,
    PolicyError,
    _digest,
    _fields,
    _revision,
    canonical_bytes,
    digest_text,
)
from .custody import _b64, _integer, verify_agent_approval

if TYPE_CHECKING:
    from .governance import ApprovalAuthority, GovernedPolicyRecord

ALGORITHM = "syn.hashgraph-governance-replay/1"
PROTECTED_ALGORITHM = "syn.hashgraph-governance-replay/2"
MAX_EVENTS = 256
MAX_VOTERS = 16
MAX_ROUNDS = 32
COIN_PERIOD = 3


def governance_finality_context(previous: GovernedPolicyRecord, policy_digest: str) -> str:
    """Derive every replay parameter from the verified predecessor, never a proof."""
    bindings = {b.policy_digest: b for b in previous.state.bindings}
    policies = {
        t.consensus_policy.digest: t.consensus_policy
        for t in previous.state.release.types
        if t.consensus_policy
    }
    p = policies.get(policy_digest)
    if p is None or p.protocol not in ("syn.bft-dag-hashgraph/1", "syn.bft-dag-hashgraph/2"):
        raise PolicyError("DAG finality requires an existing federated Type policy")
    return _digest(
        "syn.hashgraph-governance-context/1",
        {
            "algorithm": PROTECTED_ALGORITHM if p.protocol.endswith("/2") else ALGORITHM,
            "workspace_id": previous.state.workspace_id,
            "catalog_id": previous.state.release.catalog_id,
            "predecessor_digest": previous.digest,
            "binding": asdict(bindings[policy_digest]),
            "policy": p.to_dict(),
        },
    )


def event_payload_digest(body: object) -> str:
    version = (
        "2" if isinstance(body, dict) and body.get("profile") == "syn.hashgraph-event/2" else "1"
    )
    return _digest("syn.hashgraph-event-payload/" + version, body)


def event_digest(event: object) -> str:
    # Parent references bind the signature envelope as well as the event body.
    if (
        isinstance(event, dict)
        and isinstance(event.get("body"), dict)
        and event["body"].get("profile") == "syn.hashgraph-event/2"
    ):
        from .dag_signing import protected_event_digest

        return protected_event_digest(event)
    return _digest("syn.hashgraph-event/1", event)


def _authorization(proposal_digest: str, governance: ApprovalAuthority) -> str:
    digest_text(proposal_digest)
    return _digest(
        "syn.governance-authorization/1",
        {
            "role": "type-governance",
            "proposal_digest": proposal_digest,
            "authority_digest": governance.digest,
            "policy_digest": None,
        },
    )


@dataclass(frozen=True, slots=True)
class FinalizedProposal:
    proposal_digest: str
    event_digest: str
    received_round: int
    timestamp_twice_ms: int


def _virtual_vote(
    values: list[bool | None], quorum: int, distance: int
) -> tuple[bool | None, bool]:
    if any(v is None for v in values):
        return None, False
    yes = sum(v is True for v in values)
    value = yes * 2 >= len(values)
    count = yes if value else len(values) - yes
    coin = distance % COIN_PERIOD == 0
    # Native consent signatures are not a qualified common coin.
    return (None if coin and count < quorum else value), (not coin and count >= quorum)


class _Replay:
    def __init__(
        self,
        events: list[Any],
        *,
        context: str,
        roster: ApprovalAuthority,
        governance: ApprovalAuthority,
        protected: bool = False,
        predecessor_digest: str | None = None,
        successor_sequence: str | None = None,
    ) -> None:
        members = {m["agent"]["signer_hash"]: m for m in roster.memberships()}
        issuers = {m["operator"]["signer_hash"] for m in members.values()}
        if (
            not 4 <= len(members) <= MAX_VOTERS
            or len(issuers) != len(members)
            or roster.threshold != 2 * len(members) // 3 + 1
        ):
            raise PolicyError("Replay requires 4..16 equal voters with distinct admitted issuers")
        if not 1 <= len(events) <= MAX_EVENTS:
            raise PolicyError("DAG history exceeds the bounded replay window")
        if protected:
            digest_text(predecessor_digest)
            _revision(successor_sequence)
        self.quorum = roster.threshold
        self.ids: list[str] = []
        self.creators: list[str] = []
        self.parents: list[int | None] = []
        self.ancestors: list[int] = []
        self.self_ancestors: list[int] = []
        self.forked: list[set[str]] = []
        self.times: list[int] = []
        self.signatures: list[int] = []
        self.proposals: list[str | None] = []
        self.rounds: list[int] = []
        self.witnesses: dict[int, list[int]] = {}
        self.strong_cache: dict[tuple[int, int], bool] = {}
        by_id: dict[str, int] = {}
        sequences: list[int] = []
        global_tips: dict[str, int] = {}
        observed_forkers: set[str] = set()
        for i, raw in enumerate(events):
            e = _fields(raw, {"body", "proof", "reservation"} if protected else {"body", "proof"})
            b = _fields(
                e["body"],
                {
                    "profile",
                    "context_digest",
                    "creator",
                    "sequence",
                    "self_parent",
                    "other_parent",
                    "timestamp_ms",
                    "proposal",
                },
            )
            creator = str(NodeKeyHash(b["creator"]))
            if (
                b["profile"] != ("syn.hashgraph-event/2" if protected else "syn.hashgraph-event/1")
                or b["context_digest"] != context
                or creator not in members
            ):
                raise PolicyError("DAG event is outside the existing policy context or roster")
            if protected:
                from .dag_signing import verify_protected_event

                verify_protected_event(e, membership=members[creator])
            else:
                verify_agent_approval(
                    e["proof"],
                    membership=members[creator],
                    payload_digest=event_payload_digest(b),
                    now=None,
                )
            identifier = event_digest(e)
            if identifier in by_id:
                raise PolicyError("Duplicate DAG event")
            parents = []
            for field in ("self_parent", "other_parent"):
                ref = b[field]
                if ref is not None and (not isinstance(ref, str) or ref not in by_id):
                    raise PolicyError("DAG parents must be present before their child")
                parents.append(None if ref is None else by_id[ref])
            sp, op = parents
            _revision(b["sequence"])
            sequence = int(b["sequence"])
            timestamp = _integer(b["timestamp_ms"], 2**52 - 1)
            if (
                sequence != (1 if sp is None else sequences[sp] + 1)
                or (
                    sp is not None and (self.creators[sp] != creator or timestamp <= self.times[sp])
                )
                or (op is not None and self.creators[op] == creator)
            ):
                raise PolicyError("Invalid DAG creator chain, sequence or timestamp")
            ancestors = 1 << i
            for parent in parents:
                if parent is not None:
                    ancestors |= self.ancestors[parent]
            self.ids.append(identifier)
            self.creators.append(creator)
            self.parents.append(sp)
            self.times.append(timestamp)
            self.signatures.append(int.from_bytes(_b64(e["proof"]["signature"], 64), "big"))
            self.ancestors.append(ancestors)
            self.self_ancestors.append((1 << i) | (0 if sp is None else self.self_ancestors[sp]))
            tip = global_tips.get(creator)
            if tip is not None and not self.self_ancestors[i] & (1 << tip):
                observed_forkers.add(creator)
            global_tips[creator] = i
            if 3 * len(observed_forkers) >= len(members):
                raise PolicyError("Observed DAG forkers exceed the existing policy fault budget")
            sequences.append(sequence)
            # A creator is forked in THIS event's ancestry only. Later discovery
            # of a fork must never rewrite an earlier event's view or virtual vote.
            tips: dict[str, int] = {}
            forked: set[str] = set()
            for j in range(i + 1):
                if not ancestors & (1 << j):
                    continue
                c = self.creators[j]
                tip = tips.get(c)
                if tip is not None and not self.self_ancestors[j] & (1 << tip):
                    forked.add(c)
                tips[c] = j
            self.forked.append(forked)
            proposal = b["proposal"]
            eligible = None
            if proposal is not None:
                # Unauthorized application payload is a deterministic no-op,
                # not a reason to discard an otherwise valid signed ancestor.
                try:
                    if protected:
                        from .dag_signing import verify_governance_candidate

                        assert predecessor_digest is not None and successor_sequence is not None
                        eligible = verify_governance_candidate(
                            proposal,
                            governance=governance,
                            previous_record_digest=predecessor_digest,
                            successor_sequence=successor_sequence,
                        )
                    else:
                        p = _fields(proposal, {"proposal_digest", "type_governance"})
                        payload = _authorization(p["proposal_digest"], governance)
                        governance.verify(p["type_governance"], payload, None)
                        eligible = p["proposal_digest"]
                except PolicyError:
                    pass
            self.proposals.append(eligible)
            r = max((self.rounds[p] for p in parents if p is not None), default=1)
            visible = [w for w in self.witnesses.get(r, []) if self.strongly_sees(i, w)]
            if len({self.creators[w] for w in visible}) >= self.quorum:
                r += 1
            if r > MAX_ROUNDS:
                raise PolicyError("DAG rounds exceed the bounded replay window")
            self.rounds.append(r)
            if sp is None or r > self.rounds[sp]:
                self.witnesses.setdefault(r, []).append(i)
            by_id[identifier] = i

    def sees(self, x: int, y: int) -> bool:
        return bool(self.ancestors[x] & (1 << y)) and self.creators[y] not in self.forked[x]

    def strongly_sees(self, x: int, y: int) -> bool:
        key = x, y
        if key not in self.strong_cache:
            creators = (
                {self.creators[z] for z in range(x + 1) if self.sees(x, z) and self.sees(z, y)}
                if self.sees(x, y)
                else set()
            )
            self.strong_cache[key] = len(creators) >= self.quorum
        return self.strong_cache[key]

    def fame(self, target: int) -> bool | None:
        votes: dict[int, tuple[bool | None, bool]] = {}
        decision = None
        for y in sorted(range(len(self.ids)), key=lambda i: (self.rounds[i], i)):
            d = self.rounds[y] - self.rounds[target]
            if d < 1:
                continue
            sp = self.parents[y]
            inherited = votes.get(sp, (None, False)) if sp is not None else (None, False)
            if y not in self.witnesses[self.rounds[y]] or inherited[1]:
                votes[y] = inherited
                continue
            if d == 1:
                votes[y] = self.sees(y, target), False
                continue
            prior = [
                w for w in self.witnesses.get(self.rounds[y] - 1, []) if self.strongly_sees(y, w)
            ]
            if len({self.creators[w] for w in prior}) != len(prior):
                raise PolicyError("Forks cannot contribute duplicate virtual voting weight")
            values = [votes[w][0] for w in prior]
            v, decided = _virtual_vote(values, self.quorum, d)
            votes[y] = v, decided
            if decided:
                if decision is not None and decision != v:
                    raise PolicyError("Conflicting DAG fame decisions")
                decision = v
        return decision

    def first_proposal(self) -> FinalizedProposal:
        fame = {w: self.fame(w) for ws in self.witnesses.values() for w in ws}
        ordered: dict[int, tuple[int, int, int, str]] = {}
        for r in sorted(self.witnesses):
            ws = self.witnesses[r]
            if any(fame[w] is None for w in ws):
                break
            famous = [w for w in ws if fame[w]]
            unique = [
                w for w in famous if sum(self.creators[v] == self.creators[w] for v in famous) == 1
            ]
            if not unique:
                raise PolicyError("Decided DAG round has no unique famous witnesses")
            whiten = 0
            for w in unique:
                whiten ^= self.signatures[w]
            for x in range(len(self.ids)):
                if x in ordered or not all(self.ancestors[w] & (1 << x) for w in unique):
                    continue
                times = []
                for w in unique:
                    z = w
                    while self.parents[z] is not None:
                        parent = self.parents[z]
                        assert parent is not None
                        if not self.ancestors[parent] & (1 << x):
                            break
                        z = parent
                    times.append(self.times[z])
                times.sort()
                middle = len(times) // 2
                median2 = times[middle] * 2 if len(times) % 2 else times[middle - 1] + times[middle]
                ordered[x] = r, median2, self.signatures[x] ^ whiten, self.ids[x]
        candidates = [x for x in ordered if self.proposals[x] is not None]
        if not candidates:
            raise PolicyError(
                "DAG has no finalized Type-governed proposal; more history is required"
            )
        first = min(candidates, key=lambda x: ordered[x])
        proposal = self.proposals[first]
        assert proposal is not None
        return FinalizedProposal(proposal, self.ids[first], ordered[first][0], ordered[first][1])


def verify_governance_finality(
    evidence: object,
    *,
    previous: GovernedPolicyRecord,
    policy_digest: str,
    expected_proposal_digest: str,
) -> FinalizedProposal:
    """Replay the OLD roster's first authorized proposal in this exact policy epoch."""
    digest_text(expected_proposal_digest)
    canonical_bytes(evidence)
    d = _fields(evidence, {"profile", "algorithm", "context_digest", "events"})
    context = governance_finality_context(previous, policy_digest)
    policy = next(
        t.consensus_policy
        for t in previous.state.release.types
        if t.consensus_policy and t.consensus_policy.digest == policy_digest
    )
    protected = policy.protocol.endswith("/2")
    if (
        d["profile"] != policy.proof_profile
        or d["algorithm"] != (PROTECTED_ALGORITHM if protected else ALGORITHM)
        or d["context_digest"] != context
        or not isinstance(d["events"], list)
    ):
        raise PolicyError("Unsupported DAG finality proof or predecessor context")
    binding = next(b for b in previous.state.bindings if b.policy_digest == policy_digest)
    roster = next(a for a in previous.state.policy_authorities if a.digest == binding.roster_digest)
    result = _Replay(
        d["events"],
        context=context,
        roster=roster,
        governance=previous.state.type_governance,
        protected=protected,
        predecessor_digest=previous.digest,
        successor_sequence=str(int(previous.sequence) + 1),
    ).first_proposal()
    if result.proposal_digest != expected_proposal_digest:
        raise PolicyError("Replacement is not the existing policy epoch's first finalized proposal")
    return result
