from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/cogs", tags=["cogs"])


@router.get("", response_model=PlaceholderResponse)
async def getCogs() -> PlaceholderResponse:
    return placeholderResponse("cog")
