from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/om", tags=["om"])


@router.get("", response_model=PlaceholderResponse)
async def getOm() -> PlaceholderResponse:
    return placeholderResponse("om")
