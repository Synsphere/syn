from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/event", tags=["event"])


@router.get("", response_model=PlaceholderResponse)
async def getEvent() -> PlaceholderResponse:
    return placeholderResponse("event")
