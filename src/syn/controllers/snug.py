from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/snugs", tags=["snugs"])


@router.get("", response_model=PlaceholderResponse)
async def getSnugs() -> PlaceholderResponse:
    return placeholderResponse("snug")
