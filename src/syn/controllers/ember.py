from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/ember", tags=["ember"])


@router.get("", response_model=PlaceholderResponse)
async def getEmber() -> PlaceholderResponse:
    return placeholderResponse("ember")
