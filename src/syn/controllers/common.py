from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class PlaceholderResponse(BaseModel):
    primitive: str
    status: Literal["placeholder"] = "placeholder"
    message: str


def placeholderResponse(primitive: str) -> PlaceholderResponse:
    return PlaceholderResponse(
        primitive=primitive,
        message=f"Temporary {primitive} controller response",
    )
