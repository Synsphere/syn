from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/thing", tags=["thing"])


@router.get("", response_model=PlaceholderResponse)
async def getThing() -> PlaceholderResponse:
    return placeholderResponse("thing")
