"""Local signed governance import with append-only records and serialized heads."""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pgInsert
from sqlalchemy.dialects.sqlite import insert as sqliteInsert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from syn_client.consensus import MAX_DOCUMENT_BYTES, PolicyError, digest_text
from syn_client.governance import (
    MAX_CHAIN,
    GovernedPolicyRecord,
    verified_chain,
    verify_genesis,
    verify_transition,
)
from syn_client.protocol import uuid_text

from syn.policy_adoption import (
    PolicyConflict,
    PolicyNotFound,
    adoptions,
    domains,
    owners,
    signedCatalogs,
    signedClaims,
    signedRecords,
    workspaces,
)


@dataclass
class GovernedPolicyStore:
    sessions: sessionmaker[Session]

    def _lock(self, session: Session, workspace: str) -> None:
        dialect = session.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise PolicyError("Unsupported governance store dialect")
        insert = pgInsert if dialect == "postgresql" else sqliteInsert
        session.execute(insert(workspaces).values(workspace_id=workspace).on_conflict_do_nothing())
        session.execute(
            select(workspaces).where(workspaces.c.workspace_id == workspace).with_for_update()
        ).one()

    def admit(
        self,
        record: GovernedPolicyRecord,
        *,
        expected_workspace: str,
        expected_catalog: str,
        expected_genesis_digest: str,
        expected_predecessor_digest: str,
        now: int | None = None,
    ) -> GovernedPolicyRecord:
        uuid_text(expected_workspace)
        uuid_text(expected_catalog)
        digest_text(expected_genesis_digest)
        digest_text(expected_predecessor_digest)
        if (
            record.state.workspace_id != expected_workspace
            or record.state.release.catalog_id != expected_catalog
            or record.previous_record_digest != expected_predecessor_digest
        ):
            raise PolicyError(
                "Governance proposal does not match independently selected scope/predecessor"
            )
        workspace, catalog = expected_workspace, expected_catalog
        instant = int(time.time()) if now is None else now
        try:
            with self.sessions.begin() as session:
                self._lock(session, workspace)
                if (
                    session.scalar(
                        select(adoptions.c.catalog_id).where(
                            adoptions.c.workspace_id == workspace, adoptions.c.catalog_id == catalog
                        )
                    )
                    is not None
                ):
                    raise PolicyConflict(
                        "Unsigned legacy genesis cannot be silently promoted or replaced"
                    )
                anchor = session.scalar(
                    select(signedCatalogs.c.genesis_record_digest).where(
                        signedCatalogs.c.workspace_id == workspace,
                        signedCatalogs.c.catalog_id == catalog,
                    )
                )
                if anchor is None:
                    verify_genesis(
                        record,
                        expected_workspace=workspace,
                        expected_catalog=catalog,
                        expected_genesis_digest=expected_genesis_digest,
                        now=instant,
                    )
                    session.execute(
                        signedCatalogs.insert().values(
                            workspace_id=workspace,
                            catalog_id=catalog,
                            genesis_record_digest=record.digest,
                        )
                    )
                else:
                    if anchor != expected_genesis_digest:
                        raise PolicyConflict("Governance genesis does not match independent pin")
                    existing = session.scalar(
                        select(signedRecords.c.record_digest).where(
                            signedRecords.c.workspace_id == workspace,
                            signedRecords.c.catalog_id == catalog,
                            signedRecords.c.sequence == int(record.sequence),
                        )
                    )
                    if existing is not None:
                        if existing != record.digest:
                            raise PolicyConflict(
                                "Governance sequence already has a different successor"
                            )
                        # Exact historical retry is allowed after expiry, with no
                        # state mutation and no claim that its approval is fresh.
                        chain = self._chain(session, workspace, catalog, anchor, existing)
                        return chain[-1]
                    head = session.scalar(
                        select(signedRecords.c.record_digest)
                        .where(
                            signedRecords.c.workspace_id == workspace,
                            signedRecords.c.catalog_id == catalog,
                        )
                        .order_by(signedRecords.c.sequence.desc())
                        .limit(1)
                    )
                    if head != expected_predecessor_digest:
                        raise PolicyConflict(
                            "Stale governance predecessor; re-read the independently trusted head"
                        )
                    chain = self._chain(session, workspace, catalog, anchor, head)
                    if len(chain) >= MAX_CHAIN:
                        raise PolicyError(
                            "Governance history needs a future authenticated checkpoint"
                        )
                    verify_transition(chain[-1], record, now=instant)
                self._claim(session, record)
                session.execute(
                    signedRecords.insert().values(
                        workspace_id=workspace,
                        catalog_id=catalog,
                        sequence=int(record.sequence),
                        record_digest=record.digest,
                        document=record.to_json(),
                    )
                )
                self._chain(session, workspace, catalog, expected_genesis_digest, record.digest)
                return record
        except IntegrityError:
            raise PolicyConflict(
                "Governance catalog, sequence or namespace conflicts with existing authority"
            ) from None

    def _claim(self, session: Session, record: GovernedPolicyRecord) -> None:
        state = record.state
        workspace, catalog = state.workspace_id, state.release.catalog_id
        bindings = {b.policy_digest: b for b in state.bindings}
        for binding in state.bindings:
            previous = session.scalar(
                select(domains.c.genesis_digest).where(
                    domains.c.workspace_id == workspace, domains.c.domain_id == binding.domain_id
                )
            )
            if previous is None:
                session.execute(
                    domains.insert().values(
                        workspace_id=workspace,
                        domain_id=binding.domain_id,
                        genesis_digest=binding.genesis_digest,
                    )
                )
            elif previous != binding.genesis_digest:
                raise PolicyConflict("Domain already has a different immutable genesis anchor")
        claims = {
            (bindings[p.digest].domain_id, p.conflict_namespace)
            for t in state.release.types
            if (p := t.consensus_policy) is not None
        }
        for domain, namespace in sorted(claims):
            if (
                session.scalar(
                    select(owners.c.catalog_id).where(
                        owners.c.workspace_id == workspace,
                        owners.c.domain_id == domain,
                        owners.c.conflict_namespace == namespace,
                    )
                )
                is not None
            ):
                raise PolicyConflict("Legacy genesis already owns this namespace")
            previous = session.scalar(
                select(signedClaims.c.catalog_id).where(
                    signedClaims.c.workspace_id == workspace,
                    signedClaims.c.domain_id == domain,
                    signedClaims.c.conflict_namespace == namespace,
                )
            )
            if previous is None:
                session.execute(
                    signedClaims.insert().values(
                        workspace_id=workspace,
                        domain_id=domain,
                        conflict_namespace=namespace,
                        catalog_id=catalog,
                    )
                )
            elif previous != catalog:
                raise PolicyConflict("Another governed catalog already owns this namespace")

    def chain(
        self, workspace: str, catalog: str, genesis: str, head: str
    ) -> tuple[GovernedPolicyRecord, ...]:
        uuid_text(workspace)
        uuid_text(catalog)
        digest_text(genesis)
        digest_text(head)
        with self.sessions() as session:
            return self._chain(session, workspace, catalog, genesis, head)

    def _chain(
        self, session: Session, workspace: str, catalog: str, genesis: str, head: str
    ) -> tuple[GovernedPolicyRecord, ...]:
        anchor = session.scalar(
            select(signedCatalogs.c.genesis_record_digest).where(
                signedCatalogs.c.workspace_id == workspace, signedCatalogs.c.catalog_id == catalog
            )
        )
        target = session.scalar(
            select(signedRecords.c.sequence).where(
                signedRecords.c.workspace_id == workspace,
                signedRecords.c.catalog_id == catalog,
                signedRecords.c.record_digest == head,
            )
        )
        if anchor != genesis or target is None:
            raise PolicyNotFound("Pinned governance chain is unavailable")
        rows = (
            session.execute(
                select(signedRecords)
                .where(
                    signedRecords.c.workspace_id == workspace,
                    signedRecords.c.catalog_id == catalog,
                    signedRecords.c.sequence <= target,
                )
                .order_by(signedRecords.c.sequence)
                .limit(MAX_CHAIN + 1)
            )
            .mappings()
            .all()
        )
        if sum(len(row["document"].encode("utf-8")) for row in rows) > MAX_DOCUMENT_BYTES:
            raise PolicyError("Governance history needs a future authenticated checkpoint")
        records = tuple(GovernedPolicyRecord.from_json(row["document"]) for row in rows)
        if any(
            int(r.sequence) != row["sequence"] or r.digest != row["record_digest"]
            for r, row in zip(records, rows)
        ):
            raise PolicyError("Stored governance record metadata mismatch")
        verified_chain(
            records,
            expected_workspace=workspace,
            expected_catalog=catalog,
            expected_genesis_digest=genesis,
            expected_head_digest=head,
        )
        # Check all historical claims; namespace reservation cannot disappear
        # when a Type's current protection is replaced.
        for record in records:
            state = record.state
            by_policy = {b.policy_digest: b for b in state.bindings}
            for b in state.bindings:
                actual = session.scalar(
                    select(domains.c.genesis_digest).where(
                        domains.c.workspace_id == workspace, domains.c.domain_id == b.domain_id
                    )
                )
                if actual != b.genesis_digest:
                    raise PolicyError("Stored governed domain anchor mismatch")
            for t in state.release.types:
                p = t.consensus_policy
                if p is None:
                    continue
                b = by_policy[p.digest]
                owner = session.scalar(
                    select(signedClaims.c.catalog_id).where(
                        signedClaims.c.workspace_id == workspace,
                        signedClaims.c.domain_id == b.domain_id,
                        signedClaims.c.conflict_namespace == p.conflict_namespace,
                    )
                )
                if owner != catalog:
                    raise PolicyError("Stored governed namespace ownership mismatch")
        return records
