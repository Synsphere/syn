from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Integer, Text, case, cast, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased
from syn_client.consensus import ConsensusPolicy

from ..models import InvalidTypeParentsError, Type, TypeParent, TypeProperty
from ..refs import ObjectRef
from .common import databaseSession


router = APIRouter(prefix="/type", tags=["type"])

TypeId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=255,
        pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$",
        description="The slug portion of a local syn://type/ reference.",
    ),
]
TypeQuery = Annotated[str, Query(min_length=1, max_length=255)]
PropertyName = Annotated[str, Path(min_length=1, max_length=1024)]
TreeDepth = Annotated[
    int,
    Query(
        ge=0,
        le=100,
        description="Maximum number of child edges to include below the starting Type.",
    ),
]


class TypeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    wikipediaRef: str | None
    locality: int | None
    stats: dict[str, Any] | None
    statsUpdated: datetime | None
    knowledge: dict[str, Any] | None
    knowledgeUpdated: datetime | None
    confidence: Decimal | None
    confidenceUpdated: datetime | None
    consensusPolicy: dict[str, Any] | None


class TypeTreeResponse(TypeResponse):
    parents: list[TypeTreeResponse] = Field(default_factory=list)


class TypeDescendantTreeResponse(BaseModel):
    typeRef: str
    name: str
    children: list[TypeDescendantTreeResponse] = Field(default_factory=list)


class TypeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    wikipediaRef: str | None = None
    locality: int | None = None
    stats: dict[str, Any] | None = None
    knowledge: dict[str, Any] | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    parents: list[str] | None = None
    consensusPolicy: dict[str, Any] | None = None

    @field_validator("consensusPolicy")
    @classmethod
    def validateConsensusPolicy(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return ConsensusPolicy.from_dict(value).to_dict() if value is not None else None

    @field_validator("name")
    @classmethod
    def nameMustNotBeBlank(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name

    @field_validator("parents")
    @classmethod
    def parentsMustBeLocalTypeRefs(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        for value in values:
            try:
                reference = ObjectRef(value)
            except (TypeError, ValueError) as error:
                raise ValueError(str(error)) from error
            if not reference.isLocal or reference.objectTable != "type":
                raise ValueError("parents must contain only syn://type/ references")
        return values


class TypePropertyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: str
    name: str
    propertyTypeRef: str
    min: int
    max: int


class TypePropertyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=1024)
    propertyTypeRef: str
    min: int = Field(default=1, ge=0)
    max: int = Field(default=1, ge=0)

    @field_validator("name")
    @classmethod
    def nameMustNotBeBlank(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name

    @field_validator("propertyTypeRef")
    @classmethod
    def propertyTypeMustBeLocalTypeRef(cls, value: str) -> str:
        try:
            reference = ObjectRef(value)
        except (TypeError, ValueError) as error:
            raise ValueError(str(error)) from error
        if not reference.isLocal or reference.objectTable != "type":
            raise ValueError("propertyTypeRef must be a syn://type/ reference")
        return str(reference)

    @model_validator(mode="after")
    def maximumMustNotBeBelowMinimum(self) -> TypePropertyRequest:
        if self.max < self.min:
            raise ValueError("max must be greater than or equal to min")
        return self


def escapedLike(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def typeParentTree(record: Type, session: Session) -> TypeTreeResponse:
    parentEdge = aliased(TypeParent)
    ancestorEdges = (
        select(TypeParent.childRef, TypeParent.parentRef)
        .where(TypeParent.childRef == record.id)
        .cte("ancestorEdges", recursive=True)
    )
    ancestorEdges = ancestorEdges.union(
        select(parentEdge.childRef, parentEdge.parentRef).where(
            parentEdge.childRef == ancestorEdges.c.parentRef
        )
    )
    statement = select(Type, ancestorEdges.c.childRef).join(
        ancestorEdges, Type.id == ancestorEdges.c.parentRef
    )
    parentsByChild: dict[str, list[Type]] = {}
    for parent, childRef in session.execute(statement).all():
        parentsByChild.setdefault(str(childRef), []).append(parent)

    def build(node: Type, path: frozenset[str]) -> TypeTreeResponse:
        parentRecords = sorted(
            parentsByChild.get(str(node.id), []),
            key=lambda parent: (parent.name.casefold(), str(parent.id)),
        )
        parents = [
            build(parent, path | {str(parent.id)})
            for parent in parentRecords
            if str(parent.id) not in path
        ]
        values = TypeResponse.model_validate(node).model_dump()
        return TypeTreeResponse(**values, parents=parents)

    return build(record, frozenset({str(record.id)}))


def typeChildTree(
    record: Type,
    session: Session,
    depth: int,
) -> TypeDescendantTreeResponse:
    childrenByParent: dict[str, dict[str, Type]] = {}
    if depth > 0:
        childEdge = aliased(TypeParent)
        descendantEdges = (
            select(
                TypeParent.parentRef.label("parentRef"),
                TypeParent.childRef.label("childRef"),
                literal(1, Integer).label("depth"),
            )
            .where(TypeParent.parentRef == record.id)
            .cte("descendantEdges", recursive=True)
        )
        descendantEdges = descendantEdges.union(
            select(
                childEdge.parentRef,
                childEdge.childRef,
                (descendantEdges.c.depth + 1).label("depth"),
            ).where(
                childEdge.parentRef == descendantEdges.c.childRef,
                descendantEdges.c.depth < depth,
            )
        )
        statement = (
            select(Type, descendantEdges.c.parentRef)
            .join(descendantEdges, Type.id == descendantEdges.c.childRef)
            .order_by(descendantEdges.c.depth, Type.name, Type.id)
        )
        for child, parentRef in session.execute(statement).all():
            childrenByParent.setdefault(str(parentRef), {})[str(child.id)] = child

    def build(
        node: Type,
        remainingDepth: int,
        path: frozenset[str],
    ) -> TypeDescendantTreeResponse:
        childRecords = sorted(
            childrenByParent.get(str(node.id), {}).values(),
            key=lambda child: (child.name.casefold(), str(child.id)),
        )
        children = (
            [
                build(child, remainingDepth - 1, path | {str(child.id)})
                for child in childRecords
                if str(child.id) not in path
            ]
            if remainingDepth > 0
            else []
        )
        return TypeDescendantTreeResponse(
            typeRef=str(node.id),
            name=node.name,
            children=children,
        )

    return build(record, depth, frozenset({str(record.id)}))


@router.get("/{id}", response_model=TypeTreeResponse)
async def getType(
    id: TypeId, session: Session = Depends(databaseSession)
) -> TypeTreeResponse:
    reference = ObjectRef.local("type", id)
    record = session.get(Type, reference)
    if record is None:
        raise HTTPException(status_code=404, detail="Type not found")
    return typeParentTree(record, session)


@router.get("/{id}/tree", response_model=TypeDescendantTreeResponse)
async def getTypeTree(
    id: TypeId,
    depth: TreeDepth = 10,
    session: Session = Depends(databaseSession),
) -> TypeDescendantTreeResponse:
    reference = ObjectRef.local("type", id)
    record = session.get(Type, reference)
    if record is None:
        raise HTTPException(status_code=404, detail="Type not found")
    return typeChildTree(record, session, depth)


@router.get("/{id}/consensus-definition")
async def getConsensusDefinition(
    id: TypeId,
    revision: Annotated[str, Query(pattern=r"^[1-9][0-9]{0,15}$")],
    session: Session = Depends(databaseSession),
) -> dict[str, Any]:
    """Export a draft projection; no response asserts adoption or consensus."""
    from ..type_consensus import exportTypeDefinition

    reference, record = requireType(id, session)
    parents = session.scalars(
        select(TypeParent.parentRef).where(TypeParent.childRef == reference)
    ).all()
    properties = session.scalars(
        select(TypeProperty).where(TypeProperty.type == reference)
    ).all()
    definition = exportTypeDefinition(
        record, revision=revision, parents=[str(p) for p in parents], properties=properties,
    )
    return {"status": "draft", "definition": definition.to_dict(), "digest": definition.digest}


@router.put("/{id}", response_model=TypeResponse)
async def updateType(
    id: TypeId,
    body: TypeUpdateRequest,
    session: Session = Depends(databaseSession),
) -> Type:
    reference = ObjectRef.local("type", id)
    record = session.get(Type, reference)
    if record is None:
        raise HTTPException(status_code=404, detail="Type not found")

    try:
        if body.parents is not None:
            record.replaceParents(session, body.parents)

        updatedAt = datetime.now(UTC)
        if record.stats != body.stats:
            record.stats = body.stats
            record.statsUpdated = updatedAt
        if record.knowledge != body.knowledge:
            record.knowledge = body.knowledge
            record.knowledgeUpdated = updatedAt
        if record.confidence != body.confidence:
            record.confidence = body.confidence
            record.confidenceUpdated = updatedAt

        record.name = body.name
        record.wikipediaRef = body.wikipediaRef
        record.locality = body.locality
        # Old clients omit this field; omission must not erase an authored policy.
        if "consensusPolicy" in body.model_fields_set:
            record.consensusPolicy = body.consensusPolicy
        session.commit()
    except InvalidTypeParentsError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Unable to save type") from error
    return record


def requireType(id: str, session: Session) -> tuple[ObjectRef, Type]:
    reference = ObjectRef.local("type", id)
    record = session.get(Type, reference)
    if record is None:
        raise HTTPException(status_code=404, detail="Type not found")
    return reference, record


@router.get("/{id}/properties", response_model=list[TypePropertyResponse])
async def getTypeProperties(
    id: TypeId,
    session: Session = Depends(databaseSession),
) -> list[TypeProperty]:
    reference, _record = requireType(id, session)
    statement = (
        select(TypeProperty)
        .where(TypeProperty.type == reference)
        .order_by(TypeProperty.name, TypeProperty.propertyTypeRef)
    )
    return list(session.scalars(statement).all())


@router.post(
    "/{id}/properties",
    response_model=TypePropertyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def createTypeProperty(
    id: TypeId,
    body: TypePropertyRequest,
    session: Session = Depends(databaseSession),
) -> TypeProperty:
    reference, _record = requireType(id, session)
    if session.get(TypeProperty, (reference, body.name)) is not None:
        raise HTTPException(status_code=409, detail="Type property already exists")

    propertyRecord = TypeProperty(
        type=reference,
        name=body.name,
        propertyTypeRef=ObjectRef(body.propertyTypeRef),
        min=body.min,
        max=body.max,
    )
    session.add(propertyRecord)
    try:
        session.commit()
    except SQLAlchemyError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Unable to create type property") from error
    return propertyRecord


@router.put("/{id}/property/{name:path}", response_model=TypePropertyResponse)
async def updateTypeProperty(
    id: TypeId,
    name: PropertyName,
    body: TypePropertyRequest,
    session: Session = Depends(databaseSession),
) -> TypeProperty:
    reference, _record = requireType(id, session)
    propertyRecord = session.get(TypeProperty, (reference, name))
    if propertyRecord is None:
        raise HTTPException(status_code=404, detail="Type property not found")
    if body.name != name and session.get(TypeProperty, (reference, body.name)) is not None:
        raise HTTPException(status_code=409, detail="Type property already exists")

    propertyRecord.name = body.name
    propertyRecord.propertyTypeRef = ObjectRef(body.propertyTypeRef)
    propertyRecord.min = body.min
    propertyRecord.max = body.max
    try:
        session.commit()
    except SQLAlchemyError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Unable to update type property") from error
    return propertyRecord


@router.delete("/{id}/property/{name:path}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteTypeProperty(
    id: TypeId,
    name: PropertyName,
    session: Session = Depends(databaseSession),
) -> Response:
    reference, _record = requireType(id, session)
    propertyRecord = session.get(TypeProperty, (reference, name))
    if propertyRecord is None:
        raise HTTPException(status_code=404, detail="Type property not found")

    session.delete(propertyRecord)
    try:
        session.commit()
    except SQLAlchemyError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Unable to delete type property") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("", response_model=list[TypeResponse])
async def searchTypes(
    query: TypeQuery,
    session: Session = Depends(databaseSession),
) -> list[Type]:
    search = query.strip()
    if not search:
        raise HTTPException(status_code=422, detail="Search query must not be blank")

    escaped = escapedLike(search)
    contains = f"%{escaped}%"
    prefix = f"{escaped}%"
    idText = cast(Type.id, Text)
    nameExact = Type.name.ilike(escaped, escape="\\")
    idExact = idText.ilike(escaped, escape="\\")
    localIdExact = idText.ilike(f"syn://type/{escaped}", escape="\\")
    namePrefix = Type.name.ilike(prefix, escape="\\")
    idPrefix = or_(
        idText.ilike(prefix, escape="\\"),
        idText.ilike(f"syn://type/{prefix}", escape="\\"),
    )
    matches = or_(
        Type.name.ilike(contains, escape="\\"),
        idText.ilike(contains, escape="\\"),
    )
    relevance = case(
        (or_(nameExact, idExact, localIdExact), 0),
        (or_(namePrefix, idPrefix), 1),
        else_=2,
    )
    statement = (
        select(Type)
        .where(matches)
        .order_by(relevance, Type.name, idText)
        .limit(10)
    )
    return list(session.scalars(statement).all())[:10]
