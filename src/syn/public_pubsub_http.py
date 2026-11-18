"""Local HTTP controls for open, URL-addressed GossipSub subscriptions."""
from __future__ import annotations

import json
import math
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from syn_client.public_pubsub import MAX_JSON_DEPTH, PublicMessageTooLarge
from .public_pubsub import (
    PublicCursorExpired, PublicGroupNotJoined, PublicPubsubBusy, PublicPubsubUnavailable,
)


MAX_CONTROL_BODY_BYTES = 128 * 1024
GROUP_HASH_PATTERN = r"^[0-9a-f]{64}$"
CURSOR_PATTERN = r"^[0-9a-f]{32}:(?:0|[1-9][0-9]{0,19})$"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _finite_float(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def _invalid_constant(_raw):
    raise ValueError("Invalid JSON constant")


class _BoundedRequest(Request):
    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            declared = self.headers.get("content-length")
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError:
                    raise HTTPException(400, "Invalid request content length") from None
                if length < 0:
                    raise HTTPException(400, "Invalid request content length")
                if length > MAX_CONTROL_BODY_BYTES:
                    raise HTTPException(413, "Public pubsub control request is too large")
            chunks, length = [], 0
            async for chunk in self.stream():
                length += len(chunk)
                if length > MAX_CONTROL_BODY_BYTES:
                    raise HTTPException(413, "Public pubsub control request is too large")
                chunks.append(chunk)
            self._body = b"".join(chunks)
        return self._body

    async def json(self):
        if not hasattr(self, "_json"):
            try:
                self._json = json.loads(await self.body(), object_pairs_hook=_unique_object,
                                        parse_float=_finite_float, parse_constant=_invalid_constant)
                pending = [(self._json, 0)]
                while pending:
                    value, depth = pending.pop()
                    if depth > MAX_JSON_DEPTH:
                        raise ValueError("Public control JSON exceeds its nesting limit")
                    if isinstance(value, dict):
                        for key in value:
                            key.encode("utf-8")
                        pending.extend((item, depth + 1) for item in value.values())
                    elif isinstance(value, list):
                        pending.extend((item, depth + 1) for item in value)
                    elif isinstance(value, str):
                        value.encode("utf-8")
            except (ValueError, UnicodeError, RecursionError):
                raise HTTPException(422, "Expected bounded JSON with unique fields and finite values") from None
        return self._json


class _BoundedRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            return await handler(_BoundedRequest(request.scope, request.receive))

        return bounded


class JoinPublicGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=1, max_length=320,
                     description="A Syn object URL; synny:// aliases canonicalize to syn://.")
    network_id: str | None = Field(default=None, min_length=1, max_length=64,
        pattern=r"^[a-z][a-z0-9-]{0,63}$",
        description="Defaults to the installed Operator's network, or syn for API-only nodes.")


class PublishPublicMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    data: Any = Field(description="Public JSON data; the complete application envelope is limited to 64 KiB.")


GroupHash = Annotated[str, Path(pattern=GROUP_HASH_PATTERN)]
MessageCursor = Annotated[str | None, Query(max_length=53, pattern=CURSOR_PATTERN)]
MessageLimit = Annotated[int, Query(ge=1, le=256)]


async def _invoke(request: Request, operation: str, *arguments):
    try:
        return await getattr(request.app.state.p2pNode, operation)(*arguments)
    except PublicMessageTooLarge:
        raise HTTPException(413, "Public pubsub message exceeds the protocol limit") from None
    except PublicCursorExpired:
        raise HTTPException(409, "Public group subscription changed; start with a fresh cursor") from None
    except PublicGroupNotJoined:
        raise HTTPException(404, "Public group is not joined on this node") from None
    except PublicPubsubBusy:
        raise HTTPException(429, "Public pubsub is at capacity") from None
    except PublicPubsubUnavailable:
        raise HTTPException(503, "Public pubsub is temporarily unavailable") from None
    except ValueError:
        raise HTTPException(422, "Invalid public group URL, network, message or cursor") from None


def build_router() -> APIRouter:
    router = APIRouter(prefix="/p2p/pubsub", tags=["Public GossipSub"], route_class=_BoundedRoute)

    @router.get("/groups")
    async def groups(request: Request) -> dict:
        """List this node's joined public groups and discovery/mesh state."""
        return await _invoke(request, "public_groups")

    @router.post("/groups")
    async def join(body: JoinPublicGroup, request: Request) -> dict:
        """Join any valid Syn URL without a roster or local database object."""
        return await _invoke(request, "join_public_group", body.url, body.network_id)

    @router.delete("/groups/{group_hash}")
    async def leave(group_hash: GroupHash, request: Request) -> dict:
        """Unsubscribe locally and stop refreshing this group's provider record."""
        return await _invoke(request, "leave_public_group", group_hash)

    @router.post("/groups/{group_hash}/messages")
    async def publish(group_hash: GroupHash, body: PublishPublicMessage, request: Request) -> dict:
        """Publish signed public JSON; acceptance does not prove remote reception."""
        return await _invoke(request, "publish_public", group_hash, body.data)

    @router.get("/groups/{group_hash}/messages")
    async def messages(group_hash: GroupHash, request: Request,
                       after: MessageCursor = None, limit: MessageLimit = 100) -> dict:
        """Read retained messages without consuming another reader's cursor."""
        return await _invoke(request, "read_public", group_hash, after, limit)

    return router
