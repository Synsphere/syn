from __future__ import annotations

from typing import Any

from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import UserDefinedType

from .refs import ObjectRef


class ObjectRefType(UserDefinedType[ObjectRef]):
    """SQLAlchemy mapping for a quoted PostgreSQL ObjectRef domain."""

    cache_ok = True

    def __init__(
        self,
        domainName: str = "objectRef",
        *,
        localKind: str | None = None,
        localOnly: bool = False,
    ) -> None:
        self.domainName = domainName
        self.localKind = localKind
        self.localOnly = localOnly

    def get_col_spec(self, **_: Any) -> str:
        return f'"{self.domainName}"'

    def bind_processor(self, dialect: Dialect):
        def process(value: str | ObjectRef | None) -> str | None:
            if value is None:
                return None
            reference = ObjectRef(str(value))
            if self.localOnly and not reference.isLocal:
                raise ValueError(f"{self.domainName} must be a local syn:// reference")
            if self.localKind is not None and reference.objectTable != self.localKind:
                raise ValueError(
                    f"{self.domainName} must reference syn://{self.localKind}/, got {reference}"
                )
            return str(reference)

        return process

    def result_processor(self, dialect: Dialect, coltype: object):
        def process(value: str | None) -> ObjectRef | None:
            return ObjectRef(value) if value is not None else None

        return process


class SlugType(UserDefinedType[str]):
    cache_ok = True

    def get_col_spec(self, **_: Any) -> str:
        return "slug"


slugType = SlugType()
objectRefType = ObjectRefType()
typeRefType = ObjectRefType("typeRef", localKind="type", localOnly=True)
thingRefType = ObjectRefType("thingRef", localKind="thing", localOnly=True)
eventRefType = ObjectRefType("eventRef", localKind="event", localOnly=True)
emberRefType = ObjectRefType("emberRef", localKind="ember", localOnly=True)
cogRefType = ObjectRefType("cogRef", localKind="cog", localOnly=True)
enigmaRefType = ObjectRefType("enigmaRef", localKind="enigma", localOnly=True)
pondleRefType = ObjectRefType("pondleRef", localKind="pondle", localOnly=True)
snugRefType = ObjectRefType("snugRef", localKind="snug", localOnly=True)
omRefType = ObjectRefType("omRef", localKind="om", localOnly=True)
