from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/credential", tags=["credential"])


@router.get("", response_model=PlaceholderResponse)
async def getCredential() -> PlaceholderResponse:
    return placeholderResponse("credential")
