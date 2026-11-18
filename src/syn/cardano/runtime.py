"""Small independent trusted-peer followers on the node's asyncio lifecycle."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import logging

from sqlalchemy.exc import SQLAlchemyError

from .config import CardanoConfig, peer_address
from .protocol import (
    CardanoClient, CardanoProtocolError, Point, RollBackward, RollForward,
    UnsupportedEra, _protocol_error,
)


logger = logging.getLogger(__name__)


def _point(point: Point) -> dict | None:
    return None if point.slot is None else {"slot": point.slot, "hash": point.hash}


def _error_details(error, operation):
    """Public status/logs contain bounded causes, never SQL or peer wire text."""
    stage = getattr(error, "stage", None)
    if stage not in {"connect", "handshake", "mux", "keepalive", "chain-sync", "block-fetch"}:
        stage = operation
    if isinstance(error, SQLAlchemyError):
        detail = "Database operation failed"
    elif isinstance(error, (CardanoProtocolError, OSError, TimeoutError)):
        detail = str(_protocol_error(error, stage))
    else:
        detail = "Stored observation integrity check failed"
    detail = " ".join("".join(char if 32 <= ord(char) < 127 else " "
                              for char in detail[:512]).split())[:240]
    return stage, f"{type(error).__name__}: {detail}"


class CardanoRuntime:
    """Observe each configured peer independently; never select global consensus.

    A database operation and its dual ledger append finish before requesting the
    next chain update. Cancellation waits for an in-flight transaction so no
    observation can commit after this service has finished stopping.
    """

    def __init__(self, sessions, node_ref: str | None, config: CardanoConfig | None = None,
                 *, store=None, connect=None):
        self.config = config if config is not None else CardanoConfig()
        if self.config.enabled:
            if node_ref is None:
                raise ValueError("Cardano observation requires a prepared node identity")
            from syn.positions.types import local_ref
            local_ref(node_ref, "thing")
        self.node_ref = node_ref
        self.store = store
        if self.config.enabled and self.store is None:
            from .store import CardanoStore
            self.store = CardanoStore(sessions)
        self._connect = connect or CardanoClient.connect
        self._lifecycle = asyncio.Lock()
        self._database = asyncio.Semaphore(1)
        self._stop = asyncio.Event()
        self._tasks = []
        self._running = False
        self.fault = None
        self._persisted = {"peers": []}
        self._peers = {peer: {"peer": peer, "state": "stopped" if self.config.enabled else "disabled",
                             "connected": False, "attempts": 0, "blocks_received": 0,
                             "rollbacks_received": 0, "last_error": None, "tip": None,
                             "last_error_stage": None, "retry_delay_seconds": 0,
                             "protocol_version": None}
                       for peer in self.config.peers}

    async def _db(self, method, *args):
        async with self._database:
            operation = asyncio.create_task(asyncio.to_thread(method, *args))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # A Python worker thread cannot be forcibly cancelled. Join the
                # transaction before releasing our gate or native node lifetime.
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not operation.cancelled():
                    operation.exception()
                raise

    async def _refresh(self):
        self._persisted = await self._db(self.store.status, self.node_ref, self.config.networkMagic)

    async def start(self):
        async with self._lifecycle:
            if self._running or not self.config.enabled:
                return
            self._stop.clear()
            self.fault = None
            await self._db(self.store.prepare, self.node_ref, self.config.networkMagic,
                           self.config.peers)
            await self._refresh()
            self._running = True
            self._tasks = [asyncio.create_task(self._run_peer(peer), name="syn-cardano-" + str(index))
                           for index, peer in enumerate(self.config.peers)]

    async def stop(self):
        async with self._lifecycle:
            self._stop.set()
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            self._running = False
            for peer in self._peers.values():
                peer.update(state="stopped" if self.config.enabled else "disabled", connected=False,
                            retry_delay_seconds=0)

    def check(self):
        if self.fault is not None:
            raise RuntimeError("Cardano observation service failed") from self.fault

    def status(self):
        persisted = {item["peer"]: item for item in self._persisted.get("peers", [])}
        return {"enabled": self.config.enabled, "running": self._running,
                "node_ref": self.node_ref, "network_magic": self.config.networkMagic,
                "blockchain_ref": "syn://thing/cardano", "mode": "trusted-peer-observations",
                "peers": [deepcopy({**persisted.get(peer, {}), **state})
                          for peer, state in self._peers.items()]}

    async def _intersect(self, client, peer):
        saved = await self._db(self.store.intersection_points, self.node_ref,
                               self.config.networkMagic, peer)
        if saved:
            points = [Point() if point is None else Point(point["slot"], point["hash"])
                      for point in saved]
            result = await client.find_intersect(points)
            if result.point is None or result.point not in points:
                raise CardanoProtocolError("Trusted peer has no retained intersection; checkpoint preserved")
        elif self.config.startSlot is not None:
            point = Point(self.config.startSlot, self.config.startHash)
            result = await client.find_intersect([point])
            if result.point != point:
                raise CardanoProtocolError("Trusted peer does not contain the configured starting point")
        else:
            result = await client.intersect_tip()
            if result.point is None:
                raise CardanoProtocolError("Trusted peer tip intersection failed")
        await self._db(self.store.intersect, self.node_ref, self.config.networkMagic,
                       peer, _point(result.point))
        self._peers[peer]["tip"] = asdict(result.tip)
        await self._refresh()

    async def _run_peer(self, peer):
        from .store import CardanoStoreError
        state = self._peers[peer]
        delay = self.config.reconnectDelaySeconds
        while not self._stop.is_set():
            client = None
            operation = "connect"
            try:
                state.update(state="connecting", connected=False, attempts=state["attempts"] + 1,
                             retry_delay_seconds=0, protocol_version=None)
                host, port = peer_address(peer)
                client = await self._connect(host, port, self.config.networkMagic,
                    timeout=self.config.timeoutSeconds,
                    keepalive_interval=self.config.keepaliveIntervalSeconds)
                state.update(state="intersecting", connected=True,
                             protocol_version=getattr(client, "version", None))
                operation = "intersection"
                await self._intersect(client, peer)
                if state["last_error"] is not None:
                    logger.info("Cardano observation resumed for %s", peer)
                state.update(state="following", last_error=None, last_error_stage=None)
                while not self._stop.is_set():
                    operation = "chain-sync"
                    update = await client.next_update()
                    if isinstance(update, RollForward):
                        operation = "block-fetch"
                        raw = await client.fetch_block(update.header)
                        operation = "record-block"
                        receipt = await self._db(self.store.apply, self.node_ref,
                            self.config.networkMagic, peer, asdict(update.header), raw)
                        state["blocks_received"] += 1
                        state["last_event_ref"] = receipt["event_ref"]
                    elif isinstance(update, RollBackward):
                        operation = "record-rollback"
                        await self._db(self.store.rollback, self.node_ref,
                            self.config.networkMagic, peer, _point(update.point))
                        state["rollbacks_received"] += 1
                    else:
                        raise CardanoProtocolError("Unexpected chain update")
                    state["tip"] = asdict(update.tip)
                    operation = "database-status"
                    await self._refresh()
                    delay = self.config.reconnectDelaySeconds
            except asyncio.CancelledError:
                raise
            except (UnsupportedEra, CardanoStoreError) as error:
                stage, detail = _error_details(error, operation)
                state.update(state="blocked", last_error=detail, last_error_stage=stage,
                             retry_delay_seconds=0)
                logger.warning("Cardano observation blocked for %s during %s: %s", peer, stage, detail)
                return
            except (CardanoProtocolError, OSError, TimeoutError, SQLAlchemyError) as error:
                stage, detail = _error_details(error, operation)
                state.update(state="retrying", last_error=detail, last_error_stage=stage,
                             retry_delay_seconds=delay)
                logger.warning("Cardano observation for %s failed during %s: %s; reconnecting in %.1fs",
                               peer, stage, detail, delay)
            except Exception as error:
                self.fault = error
                state.update(state="failed", last_error=type(error).__name__)
                logger.exception("Cardano observation service failed")
                return
            finally:
                state["connected"] = False
                if client is not None:
                    await client.close()
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except TimeoutError:
                pass
            delay = min(self.config.maxReconnectDelaySeconds, delay * 2)
