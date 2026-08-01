from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/cog", tags=["cog"])


@router.get("", response_model=PlaceholderResponse)
async def getCog() -> PlaceholderResponse:
    return placeholderResponse("cog")
