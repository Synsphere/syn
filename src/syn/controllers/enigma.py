from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/enigma", tags=["enigma"])


@router.get("", response_model=PlaceholderResponse)
async def getEnigma() -> PlaceholderResponse:
    return placeholderResponse("enigma")
