from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Text, case, cast, or_, select
from sqlalchemy.orm import Session

from ..models import Type
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


def escapedLike(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/{id}", response_model=TypeResponse)
async def getType(id: TypeId, session: Session = Depends(databaseSession)) -> Type:
    reference = ObjectRef.local("type", id)
    record = session.get(Type, reference)
    if record is None:
        raise HTTPException(status_code=404, detail="Type not found")
    return record


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
