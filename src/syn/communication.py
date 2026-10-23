"""Authenticated durable workspace communication with a serialized hash chain."""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pgInsert
from sqlalchemy.dialects.sqlite import insert as sqliteInsert
from sqlalchemy.orm import Session, sessionmaker
from syn_client.protocol import (
    GENESIS_HASH,
    receipt_hash,
    uuid_text,
    validate_message,
)

metadata = MetaData()
heads = Table(
    "syn_communication_heads",
    metadata,
    Column("workspace_id", String(36), primary_key=True),
    Column("sequence", BigInteger, nullable=False),
    Column("hash", String(64), nullable=False),
)
messages = Table(
    "syn_communication_messages",
    metadata,
    Column(
        "workspace_id", String(36), ForeignKey(heads.c.workspace_id), primary_key=True
    ),
    Column("sequence", BigInteger, primary_key=True),
    Column("message_id", String(36), nullable=False),
    Column("channel", String(128), nullable=False),
    Column("sender", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("recorded_at", String(40), nullable=False),
    Column("previous_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
    UniqueConstraint("workspace_id", "message_id"),
)


class MessageConflict(ValueError):
    pass


class MessageStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def append(self, workspace: str, body: dict[str, Any]) -> dict[str, Any]:
        uuid_text(workspace)
        message = validate_message(body)
        with self.sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            insert = pgInsert if dialect == "postgresql" else sqliteInsert
            session.execute(
                insert(heads)
                .values(workspace_id=workspace, sequence=0, hash=GENESIS_HASH)
                .on_conflict_do_nothing(index_elements=["workspace_id"])
            )
            head = (
                session.execute(
                    select(heads)
                    .where(heads.c.workspace_id == workspace)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            existing = (
                session.execute(
                    select(messages).where(
                        messages.c.workspace_id == workspace,
                        messages.c.message_id == message["message_id"],
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if canonical({key: existing[key] for key in message}) != canonical(message):
                    raise MessageConflict("Message ID already has different contents")
                return {"schema_version": 1, **dict(existing)}
            receipt = {
                "schema_version": 1,
                "workspace_id": workspace,
                "sequence": head["sequence"] + 1,
                **message,
                "recorded_at": datetime.now(UTC).isoformat(),
                "previous_hash": head["hash"],
            }
            receipt["hash"] = receipt_hash(receipt)
            session.execute(
                messages.insert().values(
                    **{
                        key: value
                        for key, value in receipt.items()
                        if key != "schema_version"
                    }
                )
            )
            session.execute(
                heads.update()
                .where(heads.c.workspace_id == workspace)
                .values(sequence=receipt["sequence"], hash=receipt["hash"])
            )
            return receipt

    def head(self, workspace: str) -> dict[str, Any]:
        uuid_text(workspace)
        with self.sessions() as session:
            head = (
                session.execute(select(heads).where(heads.c.workspace_id == workspace))
                .mappings()
                .first()
            )
            return (
                dict(head)
                if head
                else {"workspace_id": workspace, "sequence": 0, "hash": GENESIS_HASH}
            )

    def page(self, workspace: str, after: int, limit: int) -> dict[str, Any]:
        uuid_text(workspace)
        if (
            type(after) is not int
            or not 0 <= after <= 9_007_199_254_740_991
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid page bounds")
        with self.sessions() as session:
            rows = (
                session.execute(
                    select(messages)
                    .where(
                        messages.c.workspace_id == workspace,
                        messages.c.sequence > after,
                    )
                    .order_by(messages.c.sequence)
                    .limit(limit + 1)
                )
                .mappings()
                .all()
            )
            items = [{"schema_version": 1, **dict(row)} for row in rows[:limit]]
        return {
            "items": items,
            "next_cursor": items[-1]["sequence"] if items else after,
            "has_more": len(rows) > limit,
        }


def communicationTokens() -> dict[str, str]:
    raw = os.environ.get("SYN_COMMUNICATION_TOKENS", "{}")
    try:
        return validateTokens(json.loads(raw))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "SYN_COMMUNICATION_TOKENS must map canonical workspace UUIDs to unique 32..4096 character bearer tokens"
        ) from None


def validateTokens(tokens: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(tokens, Mapping):
        raise ValueError("Invalid Syn communication credentials")
    result = {}
    for workspace, token in tokens.items():
        uuid_text(workspace)
        if (
            not isinstance(token, str)
            or not 32 <= len(token) <= 4096
            or any(not 33 <= ord(c) <= 126 for c in token)
            or token in result.values()
        ):
            raise ValueError("Invalid Syn communication credentials")
        result[workspace] = token
    return result


def communicationRouter(
    sessions: sessionmaker[Session], tokens: Mapping[str, str]
) -> APIRouter:
    credentials = validateTokens(tokens)
    router = APIRouter(prefix="/v1/communication")
    store = MessageStore(sessions)

    def authorize(request: Request, workspace: str) -> None:
        expected = credentials.get(workspace)
        supplied = request.headers.get("authorization", "")
        if expected is None or not hmac.compare_digest(
            supplied.encode(), ("Bearer " + expected).encode()
        ):
            raise HTTPException(403, "Syn workspace access denied")

    @router.get("/workspaces/{workspace}/head")
    def head(workspace: str, request: Request) -> dict[str, Any]:
        authorize(request, workspace)
        return store.head(workspace)

    @router.get("/workspaces/{workspace}/messages")
    def page(
        workspace: str, request: Request, after: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        authorize(request, workspace)
        if len(request.query_params.multi_items()) != len(
            set(request.query_params)
        ) or set(request.query_params) - {"after", "limit"}:
            raise HTTPException(422, "Invalid page query")
        try:
            return store.page(workspace, after, limit)
        except ValueError:
            raise HTTPException(422, "Invalid page bounds") from None

    @router.post("/workspaces/{workspace}/messages", status_code=201)
    async def append(workspace: str, request: Request) -> dict[str, Any]:
        authorize(request, workspace)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 131_072:
                raise HTTPException(413, "Message body exceeds 131072 bytes")
        try:
            body = json.loads(raw, object_pairs_hook=_uniquePairs)
            # PostgreSQL access is synchronous; never block the asyncio server loop.
            from starlette.concurrency import run_in_threadpool

            return await run_in_threadpool(store.append, workspace, body)
        except MessageConflict:
            raise HTTPException(
                409, "Message ID already has different contents"
            ) from None
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise HTTPException(422, "Invalid Syn message") from None

    return router


def _uniquePairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result
