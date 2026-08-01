from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    delete,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as SAUUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    declared_attr,
    mapped_column,
)

from .refs import ObjectRef
from .sqltypes import (
    cogRefType,
    emberRefType,
    enigmaRefType,
    eventRefType,
    objectRefType,
    omRefType,
    pondleRefType,
    snugRefType,
    slugType,
    thingRefType,
    typeRefType,
)


class Base(DeclarativeBase):
    pass


class InvalidTypeParentsError(ValueError):
    pass


class IntelMixin:
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    statsUpdated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    knowledge: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    knowledgeUpdated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    confidenceUpdated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NamedLocalMixin(IntelMixin):
    name: Mapped[str] = mapped_column(Text, nullable=False)
    locality: Mapped[int | None] = mapped_column(Integer)

    @declared_attr.directive
    def __table_args__(cls):
        tableName = cls.__tablename__
        return (
            CheckConstraint(
                "confidence IS NULL OR confidence BETWEEN 0 AND 1",
                name=f"{tableName}ConfidenceCheck",
            ),
            Index(f"{tableName}StatsGinIdx", "stats", postgresql_using="gin"),
        )


class WikipediaMixin:
    wikipediaRef: Mapped[str | None] = mapped_column(Text)


class ObjectRegistry(Base):
    __tablename__ = "objectRegistry"

    id: Mapped[ObjectRef] = mapped_column(objectRefType, primary_key=True)


class Type(WikipediaMixin, NamedLocalMixin, Base):
    __tablename__ = "types"

    id: Mapped[ObjectRef] = mapped_column(typeRefType, primary_key=True)

    def replaceParents(
        self,
        session: Session,
        parents: Sequence[str | ObjectRef],
    ) -> None:
        try:
            parentRefs = tuple(ObjectRef(str(parent)) for parent in parents)
        except (TypeError, ValueError) as error:
            raise InvalidTypeParentsError(str(error)) from error

        invalidRefs = [
            str(parent)
            for parent in parentRefs
            if not parent.isLocal or parent.objectTable != "type"
        ]
        if invalidRefs:
            raise InvalidTypeParentsError(
                "Parents must be local syn://type/ references: "
                + ", ".join(sorted(invalidRefs))
            )

        uniqueRefs = {str(parent) for parent in parentRefs}
        if len(uniqueRefs) != len(parentRefs):
            raise InvalidTypeParentsError("Parent list contains duplicate references")
        if str(self.id) in uniqueRefs:
            raise InvalidTypeParentsError(f"A type cannot be its own parent: {self.id}")

        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('syn.typeParents', 0))")
        )

        if parentRefs:
            knownRefs = {
                str(reference)
                for reference in session.scalars(
                    select(Type.id).where(Type.id.in_(parentRefs))
                ).all()
            }
            missingRefs = uniqueRefs - knownRefs
            if missingRefs:
                raise InvalidTypeParentsError(
                    "Unknown parent types: " + ", ".join(sorted(missingRefs))
                )

        if len(parentRefs) > 1:
            parentEdge = aliased(TypeParent)
            candidateAncestry = (
                select(
                    TypeParent.childRef.label("descendantRef"),
                    TypeParent.parentRef.label("ancestorRef"),
                )
                .where(TypeParent.childRef.in_(parentRefs))
                .cte("candidateAncestry", recursive=True)
            )
            candidateAncestry = candidateAncestry.union(
                select(candidateAncestry.c.descendantRef, parentEdge.parentRef).where(
                    parentEdge.childRef == candidateAncestry.c.ancestorRef
                )
            )
            conflicts = session.execute(
                select(
                    candidateAncestry.c.descendantRef,
                    candidateAncestry.c.ancestorRef,
                ).where(candidateAncestry.c.ancestorRef.in_(parentRefs))
            ).all()
            if conflicts:
                descendantRef, ancestorRef = sorted(
                    ((str(descendant), str(ancestor)) for descendant, ancestor in conflicts)
                )[0]
                raise InvalidTypeParentsError(
                    f"Parent list is redundant: {ancestorRef} is an ancestor of "
                    f"{descendantRef}"
                )

        currentRefs = {
            str(reference)
            for reference in session.scalars(
                select(TypeParent.parentRef).where(TypeParent.childRef == self.id)
            ).all()
        }
        if currentRefs == uniqueRefs:
            return

        session.execute(delete(TypeParent).where(TypeParent.childRef == self.id))
        session.add_all(
            TypeParent(childRef=self.id, parentRef=parent) for parent in parentRefs
        )


class Thing(WikipediaMixin, NamedLocalMixin, Base):
    __tablename__ = "things"

    id: Mapped[ObjectRef] = mapped_column(thingRefType, primary_key=True)


class Event(NamedLocalMixin, Base):
    __tablename__ = "events"

    id: Mapped[ObjectRef] = mapped_column(eventRefType, primary_key=True)
    occurredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ember(NamedLocalMixin, Base):
    __tablename__ = "embers"

    id: Mapped[ObjectRef] = mapped_column(emberRefType, primary_key=True)


class Cog(NamedLocalMixin, Base):
    __tablename__ = "cogs"

    id: Mapped[ObjectRef] = mapped_column(cogRefType, primary_key=True)


class Enigma(NamedLocalMixin, Base):
    __tablename__ = "enigmas"

    id: Mapped[ObjectRef] = mapped_column(enigmaRefType, primary_key=True)


class Pondle(NamedLocalMixin, Base):
    __tablename__ = "pondles"

    id: Mapped[ObjectRef] = mapped_column(pondleRefType, primary_key=True)


class Snug(NamedLocalMixin, Base):
    __tablename__ = "snugs"

    id: Mapped[ObjectRef] = mapped_column(snugRefType, primary_key=True)


class Om(NamedLocalMixin, Base):
    __tablename__ = "oms"

    id: Mapped[ObjectRef] = mapped_column(omRefType, primary_key=True)


class ObjectType(Base):
    __tablename__ = "objectTypes"
    __table_args__ = (Index("objectTypesTypeIdx", "typeRef", "objectRef"),)

    objectRef: Mapped[ObjectRef] = mapped_column(objectRefType, primary_key=True)
    typeRef: Mapped[ObjectRef] = mapped_column(typeRefType, primary_key=True)


class TypeParent(Base):
    __tablename__ = "typeParents"
    __table_args__ = (
        CheckConstraint('"childRef" <> "parentRef"', name="typeParentsDifferentCheck"),
        Index("typeParentsParentIdx", "parentRef", "childRef"),
    )

    childRef: Mapped[ObjectRef] = mapped_column(typeRefType, primary_key=True)
    parentRef: Mapped[ObjectRef] = mapped_column(typeRefType, primary_key=True)


class EventTarget(Base):
    __tablename__ = "eventTargets"
    __table_args__ = (
        CheckConstraint("position IS NULL OR position >= 0", name="eventTargetsPositionCheck"),
        Index("eventTargetsTargetIdx", "targetRef", "eventRef"),
    )

    eventRef: Mapped[ObjectRef] = mapped_column(eventRefType, primary_key=True)
    targetRef: Mapped[ObjectRef] = mapped_column(objectRefType, primary_key=True)
    role: Mapped[str | None] = mapped_column(slugType)
    position: Mapped[int | None] = mapped_column(Integer)


class ObjectRelationship(Base):
    __tablename__ = "objectRelationships"
    __table_args__ = (
        Index("objectRelationshipsTargetIdx", "targetRef", "relationshipType"),
    )

    sourceRef: Mapped[ObjectRef] = mapped_column(objectRefType, primary_key=True)
    targetRef: Mapped[ObjectRef] = mapped_column(objectRefType, primary_key=True)
    relationshipType: Mapped[str] = mapped_column(slugType, primary_key=True)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class ChronicleKind(StrEnum):
    ledger = "ledger"
    provenance = "provenance"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("jobsStatusCreatedIdx", "status", "createdAt"),)

    id: Mapped[UUID] = mapped_column(SAUUID(as_uuid=True), primary_key=True)
    handler: Mapped[str] = mapped_column(Text, nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[JobStatus] = mapped_column(
        ENUM(JobStatus, name="jobStatus", create_type=False), nullable=False
    )
    result: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    startedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ObjectChronicle(Base):
    __tablename__ = "objectChronicle"
    __table_args__ = (
        UniqueConstraint(
            "objectRef", "kind", "sequenceNumber"
        ),
        CheckConstraint(
            '"endedAt" IS NULL OR "endedAt" >= "begunAt"',
            name="objectChronicleTimeCheck",
        ),
        Index(
            "objectChronicleTimelineIdx", "objectRef", "kind", text('"begunAt" DESC')
        ),
    )

    entryId: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    objectRef: Mapped[ObjectRef] = mapped_column(objectRefType, nullable=False)
    eventRef: Mapped[ObjectRef | None] = mapped_column(eventRefType)
    kind: Mapped[ChronicleKind] = mapped_column(
        ENUM(ChronicleKind, name="chronicleKind", create_type=False)
    )
    begunAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sequenceNumber: Mapped[int] = mapped_column(BigInteger, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


modelByObjectTable = {
    "type": Type,
    "thing": Thing,
    "event": Event,
    "ember": Ember,
    "cog": Cog,
    "enigma": Enigma,
    "pondle": Pondle,
    "snug": Snug,
    "om": Om,
}
