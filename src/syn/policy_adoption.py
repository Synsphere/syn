"""Atomic, immutable local genesis adoption. No remote or replacement authority."""

from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    Text,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pgInsert
from sqlalchemy.dialects.sqlite import insert as sqliteInsert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from syn_client.consensus import PolicyError, digest_text
from syn_client.policy_adoption import GenesisPolicyAdoption
from syn_client.protocol import uuid_text

metadata = MetaData()
workspaces = Table(
    "syn_policy_workspaces", metadata, Column("workspace_id", String(36), primary_key=True)
)
adoptions = Table(
    "syn_policy_adoptions",
    metadata,
    Column("workspace_id", String(36), ForeignKey(workspaces.c.workspace_id), primary_key=True),
    Column("catalog_id", String(36), primary_key=True),
    Column("adoption_digest", String(64), nullable=False),
    Column("release_digest", String(64), nullable=False),
    Column("document", Text, nullable=False),
)
domains = Table(
    "syn_policy_domains",
    metadata,
    Column("workspace_id", String(36), ForeignKey(workspaces.c.workspace_id), primary_key=True),
    Column("domain_id", String(36), primary_key=True),
    Column("genesis_digest", String(64), nullable=False),
)
owners = Table(
    "syn_policy_conflict_owners",
    metadata,
    Column("workspace_id", String(36), primary_key=True),
    Column("domain_id", String(36), primary_key=True),
    Column("conflict_namespace", String(128), primary_key=True),
    Column("catalog_id", String(36), nullable=False),
    Column("policy_digest", String(64), nullable=False),
    Column("stream_id", String(36), nullable=False),
    ForeignKeyConstraint(
        ["workspace_id", "domain_id"], [domains.c.workspace_id, domains.c.domain_id]
    ),
    ForeignKeyConstraint(
        ["workspace_id", "catalog_id"], [adoptions.c.workspace_id, adoptions.c.catalog_id]
    ),
)


class PolicyConflict(PolicyError):
    """An existing anchor/namespace cannot be replaced by local bootstrap."""


class PolicyNotFound(PolicyError):
    """No adoption matches this independently selected anchor."""


def _claims(document: GenesisPolicyAdoption) -> set[tuple[str, str, str, str]]:
    by_policy = {b.policy_digest: b for b in document.bindings}
    return {
        (
            by_policy[p.digest].domain_id,
            p.conflict_namespace,
            p.digest,
            by_policy[p.digest].stream_id,
        )
        for t in document.release.types
        if (p := t.consensus_policy) is not None and p.digest in by_policy
    }


class PolicyAdoptionStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def initialize(
        self,
        document: GenesisPolicyAdoption,
        *,
        expected_workspace_id: str,
        expected_catalog_id: str,
        expected_adoption_digest: str,
    ) -> GenesisPolicyAdoption:
        document.resolver(
            expected_workspace_id=expected_workspace_id,
            expected_catalog_id=expected_catalog_id,
            expected_adoption_digest=expected_adoption_digest,
        )
        workspace, catalog = document.workspace_id, document.release.catalog_id
        try:
            with self.sessions.begin() as session:
                dialect = session.get_bind().dialect.name
                if dialect not in {"postgresql", "sqlite"}:
                    raise PolicyError("Unsupported policy store dialect")
                insert = pgInsert if dialect == "postgresql" else sqliteInsert
                # Serialize all catalog/domain claims within a workspace. The
                # unique workspace insert also serializes concurrent first use.
                session.execute(
                    insert(workspaces).values(workspace_id=workspace).on_conflict_do_nothing()
                )
                session.execute(
                    select(workspaces)
                    .where(workspaces.c.workspace_id == workspace)
                    .with_for_update()
                ).one()
                existing = (
                    session.execute(
                        select(adoptions).where(
                            adoptions.c.workspace_id == workspace, adoptions.c.catalog_id == catalog
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    if existing["adoption_digest"] != document.digest:
                        raise PolicyConflict(
                            "Catalog already adopted; governed transitions are required"
                        )
                    return self._load(session, workspace, catalog, expected_adoption_digest)
                for binding in document.bindings:
                    previous = session.scalar(
                        select(domains.c.genesis_digest).where(
                            domains.c.workspace_id == workspace,
                            domains.c.domain_id == binding.domain_id,
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
                        raise PolicyConflict(
                            "Domain already has a different immutable genesis anchor"
                        )
                session.execute(
                    adoptions.insert().values(
                        workspace_id=workspace,
                        catalog_id=catalog,
                        adoption_digest=document.digest,
                        release_digest=document.release.digest,
                        document=document.to_json(),
                    )
                )
                for domain, namespace, policy, stream in sorted(_claims(document)):
                    session.execute(
                        owners.insert().values(
                            workspace_id=workspace,
                            domain_id=domain,
                            conflict_namespace=namespace,
                            catalog_id=catalog,
                            policy_digest=policy,
                            stream_id=stream,
                        )
                    )
                return self._load(session, workspace, catalog, expected_adoption_digest)
        except IntegrityError:
            raise PolicyConflict("Policy ownership conflicts with an existing adoption") from None

    def load(
        self, workspace: str, catalog: str, expected_adoption_digest: str
    ) -> GenesisPolicyAdoption:
        uuid_text(workspace)
        uuid_text(catalog)
        digest_text(expected_adoption_digest)
        with self.sessions() as session:
            return self._load(session, workspace, catalog, expected_adoption_digest)

    def _load(
        self, session: Session, workspace: str, catalog: str, anchor: str
    ) -> GenesisPolicyAdoption:
        row = (
            session.execute(
                select(adoptions).where(
                    adoptions.c.workspace_id == workspace,
                    adoptions.c.catalog_id == catalog,
                    adoptions.c.adoption_digest == anchor,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise PolicyNotFound("Pinned policy adoption is unavailable")
        document = GenesisPolicyAdoption.from_json(row["document"])
        document.resolver(
            expected_workspace_id=workspace,
            expected_catalog_id=catalog,
            expected_adoption_digest=anchor,
        )
        if row["release_digest"] != document.release.digest:
            raise PolicyError("Stored policy release digest mismatch")
        actual = set(
            session.execute(
                select(
                    owners.c.domain_id,
                    owners.c.conflict_namespace,
                    owners.c.policy_digest,
                    owners.c.stream_id,
                ).where(owners.c.workspace_id == workspace, owners.c.catalog_id == catalog)
            ).tuples()
        )
        if actual != _claims(document):
            raise PolicyError("Stored policy conflict ownership mismatch")
        for binding in document.bindings:
            genesis = session.scalar(
                select(domains.c.genesis_digest).where(
                    domains.c.workspace_id == workspace, domains.c.domain_id == binding.domain_id
                )
            )
            if genesis != binding.genesis_digest:
                raise PolicyError("Stored domain genesis anchor mismatch")
        return document
