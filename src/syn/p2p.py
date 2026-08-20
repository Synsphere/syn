from __future__ import annotations

import asyncio
import logging
from threading import Event, Lock, Thread
from typing import Any

import multiaddr
import trio
from libp2p import new_host
from libp2p.discovery.mdns.mdns import MDNSDiscovery

from .config import P2PConfig


logger = logging.getLogger(__name__)


class Libp2pNode:
    """Runs py-libp2p's Trio host alongside the asyncio web application."""

    def __init__(self, config: P2PConfig) -> None:
        self.config = config
        self._ready = Event()
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._error: BaseException | None = None
        self._peerId: str | None = None
        self._addresses: tuple[str, ...] = ()
        self._discoveredPeers: tuple[str, ...] = ()

    async def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._ready.clear()
        self._stop.clear()
        self._error = None
        self._thread = Thread(target=self._run, name="syn-libp2p", daemon=True)
        self._thread.start()
        ready = await asyncio.to_thread(
            self._ready.wait, self.config.startupTimeoutSeconds
        )
        if not ready:
            self._stop.set()
            raise TimeoutError("libp2p node did not start before its configured timeout")
        if self._error is not None:
            raise RuntimeError("libp2p node failed to start") from self._error
        logger.info("libp2p peer %s listening on %s", self._peerId, self._addresses)

    async def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        await asyncio.to_thread(thread.join, self.config.startupTimeoutSeconds)
        if thread.is_alive():
            raise TimeoutError("libp2p node did not stop before its configured timeout")
        self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "running": self._thread is not None and self._thread.is_alive(),
                "peerId": self._peerId,
                "listenAddresses": list(self._addresses),
                "bootstrapPeers": list(self.config.bootstrapPeers),
                "mdnsEnabled": self.config.mdnsEnabled,
                "discoveredPeers": list(self._discoveredPeers),
            }

    def _run(self) -> None:
        try:
            trio.run(self._serve)
        except BaseException as error:
            self._error = error
            logger.exception("libp2p node stopped with an error")
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        addresses = [multiaddr.Multiaddr(value) for value in self.config.listenAddresses]
        host = new_host(bootstrap=list(self.config.bootstrapPeers))
        async with host.run(listen_addrs=addresses):
            if self.config.mdnsEnabled:
                host.mDNS = MDNSDiscovery(
                    host.get_network(), port=_advertisedTcpPort(host.get_addrs())
                )
                host.mDNS.start()
            with self._lock:
                self._peerId = str(host.get_id())
                self._addresses = tuple(str(value) for value in host.get_addrs())
            self._ready.set()
            while not self._stop.is_set():
                peerId = str(host.get_id())
                discovered = tuple(
                    sorted(
                        str(value)
                        for value in host.get_peerstore().valid_peer_ids()
                        if str(value) != peerId
                    )
                )
                with self._lock:
                    self._discoveredPeers = discovered
                await trio.sleep(0.1)


def _advertisedTcpPort(addresses: list[multiaddr.Multiaddr]) -> int:
    for address in addresses:
        try:
            if address.value_for_protocol("ip4") is not None:
                return int(address.value_for_protocol("tcp"))
        except Exception:
            continue
    raise RuntimeError("mDNS requires at least one IPv4 TCP listen address")
