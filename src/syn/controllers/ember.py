from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/embers", tags=["embers"])


@router.get("", response_model=PlaceholderResponse)
async def getEmbers() -> PlaceholderResponse:
    return placeholderResponse("ember")
