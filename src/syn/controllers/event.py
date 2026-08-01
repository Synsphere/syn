from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=PlaceholderResponse)
async def getEvents() -> PlaceholderResponse:
    return placeholderResponse("event")
