from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/pondle", tags=["pondle"])


@router.get("", response_model=PlaceholderResponse)
async def getPondle() -> PlaceholderResponse:
    return placeholderResponse("pondle")
