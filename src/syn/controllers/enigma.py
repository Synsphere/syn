from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/enigmas", tags=["enigmas"])


@router.get("", response_model=PlaceholderResponse)
async def getEnigmas() -> PlaceholderResponse:
    return placeholderResponse("enigma")
