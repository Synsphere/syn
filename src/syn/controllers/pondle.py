from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/pondles", tags=["pondles"])


@router.get("", response_model=PlaceholderResponse)
async def getPondles() -> PlaceholderResponse:
    return placeholderResponse("pondle")
