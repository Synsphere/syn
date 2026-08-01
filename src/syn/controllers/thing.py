from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/things", tags=["things"])


@router.get("", response_model=PlaceholderResponse)
async def getThings() -> PlaceholderResponse:
    return placeholderResponse("thing")
