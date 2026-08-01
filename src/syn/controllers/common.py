from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy.orm import Session


class PlaceholderResponse(BaseModel):
    primitive: str
    status: Literal["placeholder"] = "placeholder"
    message: str


def placeholderResponse(primitive: str) -> PlaceholderResponse:
    return PlaceholderResponse(
        primitive=primitive,
        message=f"Temporary {primitive} controller response",
    )


def databaseSession(request: Request) -> Iterator[Session]:
    session = request.app.state.sessions()
    try:
        yield session
    finally:
        session.close()
