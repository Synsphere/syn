from __future__ import annotations

from fastapi import APIRouter

from .common import PlaceholderResponse, placeholderResponse


router = APIRouter(prefix="/oms", tags=["oms"])


@router.get("", response_model=PlaceholderResponse)
async def getOms() -> PlaceholderResponse:
    return placeholderResponse("om")
