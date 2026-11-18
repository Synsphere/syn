"""Opt-in Windows-host TCP/UDP + UPnP broker for a pinned WSL2 NAT sandbox.

This process owns ONLY its in-memory leases. It cannot replace an existing
Windows portproxy, Win firewall rule, or foreign IGD mapping. Run separately
from Syn custody, with an independent secret, scoped private firewall policy,
and an administrator-approved LAN/WSL address binding.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
import selectors
import socket
import socketserver
import sys
import threading
import time

from syn_wsl_bridge_common import (BridgeError, BridgePolicy, MAX_CONTROL_BYTES,
    ReplayGuard, canonical, secret_file, sign_reply, valid_ipv4, valid_port)

LOG = logging.getLogger("syn.wsl.host-bridge")
MAX_UDP_PACKET = 4096
MAX_UDP_PEERS = 256
UDP_PEER_IDLE = 90.0
MAX_TCP_SESSIONS = 64


class RouterError(BridgeError):
    pass


class MiniUpnpGateway:
    """Readback-checked IGD operations, restricted to Windows LAN endpoints."""

    def __init__(self, lan_address: str, *, multicast_interface=None, upnp=None):
        self.lan_address = valid_ipv4(lan_address)
        self._lock = threading.Lock()
        if upnp is None:
            import miniupnpc  # Windows-host optional dependency, no WSL install
            upnp = (miniupnpc.UPnP(multicastif=multicast_interface)
                    if multicast_interface else miniupnpc.UPnP())
            upnp.discoverdelay = 2000
        self.upnp = upnp
        count = upnp.discover()
        if type(count) is not int or count <= 0:
            raise RouterError("Windows host could not discover a UPnP gateway")
        upnp.selectigd()
        if str(upnp.lanaddr) != self.lan_address:
            raise RouterError("Router selected a different Windows LAN interface")

    def external_ip(self) -> str:
        with self._lock:
            return str(self.upnp.externalipaddress())

    def get(self, external: int, protocol: str) -> dict | None:
        with self._lock:
            try:
                result = self.upnp.getspecificportmapping(external, protocol)
            except Exception as error:
                if error.args and type(error.args[0]) is int and error.args[0] == 714:
                    return None
                raise RouterError("Router mapping readback failed") from None
        if result is None:
            return None
        if type(result) not in (tuple, list) or len(result) < 2:
            raise RouterError("Invalid router mapping readback")
        try:
            return {"client": valid_ipv4(str(result[0])), "port": int(result[1]),
                    "description": str(result[2]) if len(result) > 2 else ""}
        except (ValueError, TypeError):
            raise RouterError("Invalid router mapping readback") from None

    def add(self, external: int, protocol: str, description: str, lease: int) -> None:
        with self._lock:
            try:
                answer = self.upnp.addportmapping(external, protocol, self.lan_address,
                                                  external, description, "", lease)
            except Exception:
                raise RouterError("Router rejected UPnP mapping") from None
        if answer is False:
            raise RouterError("Router rejected UPnP mapping")
        if self.get(external, protocol) != {"client": self.lan_address,
                                          "port": external, "description": description}:
            raise RouterError("Router UPnP readback did not match host-owned mapping")

    def remove(self, external: int, protocol: str, description: str) -> None:
        expected = {"client": self.lan_address, "port": external, "description": description}
        current = self.get(external, protocol)
        if current is None:
            return
        if current != expected:
            raise RouterError("Router mapping ownership changed; refusing deletion")
        with self._lock:
            try:
                answer = self.upnp.deleteportmapping(external, protocol)
            except Exception:
                raise RouterError("Router refused removal of owned mapping") from None
        if answer is False or self.get(external, protocol) is not None:
            raise RouterError("Router did not confirm mapping removal")


class TcpIngress:
    """An in-process forwarding socket. No persistent netsh portproxy changes."""

    def __init__(self, bind_ip: str, external: int, target_ip: str, internal: int):
        self.bind_ip, self.external, self.target_ip, self.internal = bind_ip, external, target_ip, internal
        self._stop = threading.Event()
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_TCP_SESSIONS)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            self._sock.bind((bind_ip, external))
            self._sock.listen(32)
            self._sock.settimeout(0.4)
        except BaseException:
            self._sock.close()
            raise
        self._thread = threading.Thread(target=self._accept, daemon=True,
                                        name=f"syn-wsl-tcp-{external}")
        self._thread.start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self._relay, args=(client,), daemon=True).start()

    def _relay(self, client: socket.socket):
        upstream = None
        try:
            upstream = socket.create_connection((self.target_ip, self.internal), timeout=3)
            for s in (client, upstream):
                s.settimeout(2)
            with self._lock:
                self._clients.update((client, upstream))
            # Honour TCP half-close: receiving EOF shuts down only the
            # opposite write direction. The other direction drains its reply.
            activity = [time.monotonic()]
            stopped = threading.Event()
            def pump(src, dst):
                try:
                    while not stopped.is_set() and not self._stop.is_set():
                        try:
                            data = src.recv(32768)
                        except socket.timeout:
                            if time.monotonic() - activity[0] > 120:
                                return
                            continue
                        if not data:
                            return
                        activity[0] = time.monotonic()
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    try: dst.shutdown(socket.SHUT_WR)
                    except OSError: pass
            send = threading.Thread(target=pump, args=(client, upstream), daemon=True)
            send.start()
            pump(upstream, client)
            stopped.set()
            send.join(timeout=2)
        except OSError:
            pass
        finally:
            with self._lock:
                self._clients.discard(client)
                if upstream: self._clients.discard(upstream)
            client.close()
            if upstream: upstream.close()
            self._slots.release()

    def stop(self):
        self._stop.set()
        self._sock.close()
        with self._lock:
            for s in self._clients:
                try: s.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                s.close()
        self._thread.join(timeout=2)


class UdpIngress:
    """Per-remote-source UDP NAT sessions for QUIC 4-tuple isolation.

    A distinct host-to-WSL ephemeral source port is allocated per Internet peer.
    Replies return on that ephemeral socket and are sent from the public UDP
    listener, so concurrent QUIC clients do not collide on a single 4-tuple.
    """

    def __init__(self, bind_ip: str, external: int, wsl_host_ip: str,
                 target_ip: str, internal: int, *, max_peers=MAX_UDP_PEERS):
        self.bind_ip, self.external, self.wsl_host_ip = bind_ip, external, wsl_host_ip
        self.target_ip, self.internal, self.max_peers = target_ip, internal, max_peers
        self._stop = threading.Event()
        self._sessions: dict[tuple[str, int], tuple[socket.socket, float]] = {}
        self._selector = selectors.DefaultSelector()
        self._public = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._public.bind((bind_ip, external))
            self._public.setblocking(False)
            self._selector.register(self._public, selectors.EVENT_READ, None)
        except BaseException:
            self._selector.close()
            self._public.close()
            raise
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name=f"syn-wsl-udp-{external}")
        self._thread.start()

    def _remove(self, peer):
        entry = self._sessions.pop(peer, None)
        if entry:
            sock, _ = entry
            try: self._selector.unregister(sock)
            except (KeyError, ValueError): pass
            sock.close()

    def _serve(self):
        latest_cleanup = time.monotonic()
        while not self._stop.is_set():
            try: events = self._selector.select(timeout=0.25)
            except (OSError, ValueError): break
            for key, _ in events:
                sock, peer = key.fileobj, key.data
                try:
                    data, source = sock.recvfrom(MAX_UDP_PACKET + 1)
                except (OSError, ValueError):
                    continue
                if not data or len(data) > MAX_UDP_PACKET:
                    continue
                if peer is None:
                    remote = source
                    if remote == (self.target_ip, self.internal):
                        continue
                    entry = self._sessions.get(remote)
                    if entry is None:
                        if len(self._sessions) >= self.max_peers:
                            continue  # bounded; old sessions expire below
                        private_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        try:
                            private_sock.bind((self.wsl_host_ip, 0))
                            private_sock.setblocking(False)
                            self._selector.register(private_sock, selectors.EVENT_READ, remote)
                        except OSError:
                            private_sock.close()
                            continue
                        self._sessions[remote] = (private_sock, time.monotonic())
                        entry = self._sessions[remote]
                    self._sessions[remote] = (entry[0], time.monotonic())
                    try: entry[0].sendto(data, (self.target_ip, self.internal))
                    except OSError: self._remove(remote)
                else:
                    if source != (self.target_ip, self.internal):
                        continue
                    self._sessions[peer] = (sock, time.monotonic())
                    try: self._public.sendto(data, peer)
                    except OSError: self._remove(peer)
            if time.monotonic() - latest_cleanup >= 2:
                latest_cleanup = time.monotonic()
                for peer, (_, tick) in list(self._sessions.items()):
                    if latest_cleanup - tick >= UDP_PEER_IDLE:
                        self._remove(peer)
        for peer in list(self._sessions): self._remove(peer)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._public.close()
        self._selector.close()

    def status(self):
        return {"sessions": len(self._sessions), "maxSessions": self.max_peers}


@dataclass
class Mapping:
    external: int
    internal: int
    protocol: str
    description: str
    lease_expiry: float
    ingress: object
    local_active: bool = True


class BridgeMappings:
    """Own only exact, temporary host sockets and readback-confirmed IGD rules."""

    def __init__(self, policy: BridgePolicy, router, *, tcp_factory=TcpIngress,
                 udp_factory=UdpIngress, now=time.monotonic):
        self.policy, self.router = policy, router
        self.tcp_factory, self.udp_factory, self._now = tcp_factory, udp_factory, now
        self._lock = threading.RLock()
        self._mappings: dict[tuple[str, int], Mapping] = {}
        if getattr(router, "lan_address", None) != policy.windows_lan:
            raise BridgeError("Router LAN interface differs from the Windows bridge policy")

    def apply(self, protocol: str, external: int, internal: int, lease: int) -> dict:
        self.policy.protocol(protocol)
        self.policy.port(external)
        valid_port(internal)
        if type(lease) is not int or not 0 <= lease <= 7200:
            raise BridgeError("Invalid requested lease")
        actual_lease = min(lease or self.policy.max_lease_seconds, self.policy.max_lease_seconds)
        key = (protocol, external)
        tag = f"Syn WSL sandbox {protocol} {external}"
        with self._lock:
            owned = self._mappings.get(key)
            if owned and owned.internal != internal:
                raise BridgeError("Owned external port already targets another sandbox port")
            if owned is None and len(self._mappings) >= self.policy.max_mappings:
                raise BridgeError("Sandbox mapping capacity exhausted")
            old = self.router.get(external, protocol)
            expected = {"client": self.policy.windows_lan, "port": external,
                        "description": tag}
            if old is not None and (old != expected or owned is None):
                raise BridgeError("External router mapping is already owned by another client")
            ingress = None
            if owned is None or not owned.local_active:
                try:
                    if protocol == "TCP":
                        ingress = self.tcp_factory(self.policy.windows_lan, external,
                                                   self.policy.sandbox_ip, internal)
                    else:
                        ingress = self.udp_factory(self.policy.windows_lan, external,
                            self.policy.windows_wsl, self.policy.sandbox_ip, internal)
                except OSError:
                    raise BridgeError("Windows TCP/UDP listener is already occupied or unavailable") from None
            try:
                self.router.add(external, protocol, tag, actual_lease)
            except Exception:
                if ingress: ingress.stop()
                # A gateway may install a mapping then fail its readback.
                # Attempt exact-owner rollback rather than orphaning the rule.
                if owned is None:
                    try: self.router.remove(external, protocol, tag)
                    except BridgeError:
                        LOG.warning("Could not verify rollback of %s %d", protocol, external)
                raise
            if owned is None:
                owned = Mapping(external, internal, protocol, tag, 0, ingress)
                self._mappings[key] = owned
            elif ingress is not None:
                owned.ingress, owned.local_active = ingress, True
            owned.lease_expiry = self._now() + actual_lease
            return {"protocol": protocol, "externalPort": external, "internalPort": internal,
                    "internalClient": self.policy.sandbox_ip, "leaseSeconds": actual_lease,
                    "externalIp": self.router.external_ip(), "mappingVerified": True,
                    "publicReachability": "unverified"}

    def query(self, protocol: str, external: int) -> dict:
        self.policy.protocol(protocol)
        self.policy.port(external)
        with self._lock:
            row = self._mappings.get((protocol, external))
            if row is None:
                raise BridgeError("Mapping is not owned by this sandbox")
            if self.router.get(external, protocol) != {"client": self.policy.windows_lan,
                                "port": external, "description": row.description}:
                raise BridgeError("Router mapping no longer matches ownership receipt")
            return {"protocol": protocol, "externalPort": external,
                    "internalPort": row.internal, "internalClient": self.policy.sandbox_ip,
                    "description": row.description,
                    "leaseSeconds": max(0, int(row.lease_expiry - self._now()))}

    def remove(self, protocol: str, external: int) -> dict:
        self.policy.protocol(protocol)
        self.policy.port(external)
        with self._lock:
            key = (protocol, external)
            owned = self._mappings.get(key)
            if owned is None:
                raise BridgeError("Cannot delete a mapping not owned by this sandbox")
            try:
                self.router.remove(external, protocol, owned.description)
            except BridgeError:
                # Never continue forwarding after lease expiry or a failed
                # deletion; retry exact-owner router cleanup on the next sweep.
                if owned.local_active:
                    owned.ingress.stop()
                    owned.local_active = False
                raise
            if owned.local_active:
                owned.ingress.stop()
            self._mappings.pop(key, None)
            return {"removed": True, "protocol": protocol, "externalPort": external}

    def sweep(self):
        with self._lock:
            for protocol, external in list(self._mappings):
                if self._mappings[(protocol, external)].lease_expiry <= self._now():
                    try: self.remove(protocol, external)
                    except BridgeError:
                        LOG.warning("Owned bridge mapping cleanup failed for %s %d", protocol, external)

    def close(self):
        with self._lock:
            for protocol, external in list(self._mappings):
                try: self.remove(protocol, external)
                except BridgeError:
                    # Stop local ingress even when the router will not confirm
                    # cleanup. The short router lease then expires on its own.
                    if self._mappings[(protocol, external)].local_active:
                        self._mappings[(protocol, external)].ingress.stop()
                    LOG.warning("Could not confirm router cleanup for %s %d", protocol, external)
            self._mappings.clear()

    def execute(self, operation: dict) -> dict:
        if type(operation) is not dict or "action" not in operation:
            raise BridgeError("Invalid bridge operation")
        action = operation["action"]
        if action == "external-ip" and set(operation) == {"action"}:
            return {"externalIp": self.router.external_ip(), "mappingVerified": False,
                    "publicReachability": "unverified"}
        if action in ("map", "delete", "query"):
            required = ({"action", "protocol", "externalPort", "internalPort", "leaseSeconds"}
                        if action == "map" else {"action", "protocol", "externalPort"})
            if set(operation) != required:
                raise BridgeError("Bridge operation has unexpected fields")
            if action == "map":
                return self.apply(operation["protocol"], operation["externalPort"],
                                  operation["internalPort"], operation["leaseSeconds"])
            if action == "delete":
                return self.remove(operation["protocol"], operation["externalPort"])
            return self.query(operation["protocol"], operation["externalPort"])
        raise BridgeError("Unsupported bridge operation")

    def status(self) -> dict:
        with self._lock:
            return {"mappings": [{"protocol": m.protocol, "externalPort": m.external,
                     "internalPort": m.internal,
                     "remainingSeconds": max(0, int(m.lease_expiry - self._now()))}
                     for m in self._mappings.values()],
                    "maxMappings": self.policy.max_mappings}


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def verify_request(self, request, client_address):
        # Bound the number of threads BEFORE a remote client is dispatched.
        if client_address[0] != self.bridge.policy.sandbox_ip:
            return False
        return self._slots.acquire(blocking=False)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def __init__(self, bridge: BridgeMappings, secret: bytes, port: int):
        self.bridge, self.secret, self.replay = bridge, secret, ReplayGuard()
        self._slots = threading.BoundedSemaphore(16)
        super().__init__((bridge.policy.windows_wsl, port), BridgeHTTPHandler)


class BridgeHTTPHandler(BaseHTTPRequestHandler):
    server_version = "SynWSLBridge/1"
    sys_version = ""

    def log_message(self, format, *args):
        # No remote body, headers or hostile router text in permanent logs.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def do_POST(self):
        server = self.server
        if self.path != "/v1/control":
            self.send_error(403)
            return
        try:
            try:
                size = int(self.headers.get("Content-Length", "-1"))
                if not 0 < size <= MAX_CONTROL_BYTES:
                    raise BridgeError("Bridge control packet too large")
                body = self.rfile.read(size)
                if len(body) != size:
                    raise BridgeError("Truncated bridge request")
                operation = server.replay.verify(server.secret, body, self.headers.get("X-Syn-MAC", ""))
                request_nonce = json.loads(body)["nonce"]
                value = server.bridge.execute(operation)
                packet, status = {"ok": True, "value": value, "requestNonce": request_nonce}, 200
            except (BridgeError, ValueError, TypeError, OSError):
                # An unverified request never gets an acknowledged nonce.
                packet, status = {"ok": False, "value": {"error": "denied"},
                                  "requestNonce": locals().get("request_nonce")}, 403
            wire = canonical(packet)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.send_header("X-Syn-MAC", sign_reply(server.secret, wire))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(wire)
        finally:
            pass

    def do_GET(self):
        self.send_error(405)


def cleanup_owned(policy: BridgePolicy):
    """Explicit, offline recovery of exact bridge-tagged IGD rules only.

    No portproxy/firewall changes exist to recover: this bridge uses live
    in-process sockets. Never delete another program's UPnP mapping.
    """
    router = MiniUpnpGateway(policy.windows_lan)
    removed = []
    for port in range(policy.first_port, policy.last_port + 1):
        for protocol in ("TCP", "UDP"):
            current = router.get(port, protocol)
            tag = f"Syn WSL sandbox {protocol} {port}"
            if current == {"client": policy.windows_lan, "port": port, "description": tag}:
                router.remove(port, protocol, tag)
                removed.append((protocol, port))
    return removed


def serve(*, policy: BridgePolicy, control_port: int, secret: bytes,
          multicast_groups=(), multicast_port=48778):
    from syn_wsl_multicast_relay import MulticastRelay
    router = MiniUpnpGateway(policy.windows_lan)
    mappings = BridgeMappings(policy, router)
    server = multicast = multicast_thread = maintenance = None
    stop = threading.Event()
    try:
        server = BridgeHTTPServer(mappings, secret, control_port)
        multicast = (MulticastRelay(interface=policy.windows_lan,
                     tunnel_bind=policy.windows_wsl, tunnel_port=multicast_port,
                     peer_ip=policy.sandbox_ip, peer_port=multicast_port,
                     secret=secret, groups=multicast_groups) if multicast_groups else None)
        if multicast:
            multicast_thread = threading.Thread(target=multicast.run, daemon=True,
                                                 name="syn-wsl-host-multicast")
            multicast_thread.start()
        def housekeeping():
            while not stop.wait(2): mappings.sweep()
        maintenance = threading.Thread(target=housekeeping, daemon=True)
        maintenance.start()
        LOG.info("Windows bridge active: LAN=%s WSL=%s control=%d permitted=%d-%d",
                 policy.windows_lan, policy.windows_wsl, control_port,
                 policy.first_port, policy.last_port)
        server.serve_forever(poll_interval=.2)
    finally:
        stop.set()
        if server: server.server_close()
        mappings.close()
        if multicast:
            multicast.stop()
            if multicast_thread: multicast_thread.join(timeout=2)
        if maintenance: maintenance.join(timeout=3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-lan", required=True)
    parser.add_argument("--windows-wsl", required=True)
    parser.add_argument("--sandbox-ip", required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--control-port", type=int, default=48777)
    parser.add_argument("--port-min", type=int, default=4001)
    parser.add_argument("--port-max", type=int, default=4099)
    parser.add_argument("--max-mappings", type=int, default=16)
    parser.add_argument("--max-lease", type=int, default=1800)
    parser.add_argument("--no-tcp", action="store_true")
    parser.add_argument("--cleanup-owned", action="store_true",
                        help="Offline, explicitly approved exact-owner router cleanup")
    parser.add_argument("--no-udp", action="store_true")
    parser.add_argument("--mdns", action="store_true", help="Opt-in mDNS reflector")
    parser.add_argument("--multicast-group", action="append", default=[],
                        help="Extra explicitly approved IPv4 multicast GROUP:PORT")
    parser.add_argument("--multicast-port", type=int, default=48778)
    arguments = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("The host bridge runs on Windows; Linux may only run tests")
    from syn_wsl_multicast_relay import channels
    policy = BridgePolicy(arguments.windows_lan, arguments.windows_wsl,
        arguments.sandbox_ip, arguments.port_min, arguments.port_max,
        arguments.max_mappings, arguments.max_lease,
        not arguments.no_tcp, not arguments.no_udp)
    multicast_groups = channels(mdns=arguments.mdns, groups=arguments.multicast_group)
    try:
        if arguments.cleanup_owned:
            # Do not run while a broker is live on the same control socket.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                if probe.connect_ex((policy.windows_wsl, arguments.control_port)) == 0:
                    raise BridgeError("Stop the active bridge before orphan cleanup")
            removed = cleanup_owned(policy)
            print(json.dumps({"cleaned": [{"protocol": item[0], "port": item[1]}
                                          for item in removed]}))
            return 0
        serve(policy=policy, control_port=valid_port(arguments.control_port),
              secret=secret_file(arguments.secret),
              multicast_groups=multicast_groups,
              multicast_port=valid_port(arguments.multicast_port))
    except (BridgeError, OSError) as error:
        LOG.error("Host bridge startup or ownership check failed: %s", type(error).__name__)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
