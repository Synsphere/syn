"""Canonical public-only local Syn relay. No keystore or execution authority.

Connects only operator-selected private Agent endpoints and fans out signed
ciphertext within one frozen group/workspace/session. No IP listener is opened.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import signal
import socket
import sys
import threading

from syn.operator.custody import private_read
from syn_client.operator_protocol import SynProtocolError, canonical, exact, parse, receive, send
from syn_client.pubsub import scope, verify_hello, verify_message


class PrivateRelay:
    def __init__(self, config):
        config = exact(config, {"profile", "scope", "endpoints"}, "private relay configuration")
        if config["profile"] != "syn.private-relay-config/1":
            raise SynProtocolError("Unknown private relay profile")
        self.selected = scope(config["scope"])
        self.endpoints = config["endpoints"]
        members = {entry["body"]["node_public_key_hash"] for entry in self.selected["members"]}
        if not isinstance(self.endpoints, list) or len(self.endpoints) != len(members):
            raise SynProtocolError("Relay endpoints differ from selected membership")
        identities = set()
        for entry in self.endpoints:
            exact(entry, {"node", "socket", "owner_uid"}, "private relay endpoint")
            if entry["node"] not in members or entry["node"] in identities:
                raise SynProtocolError("Relay endpoint identity is duplicate or outside its roster")
            identities.add(entry["node"])
        self.connections = {}
        self.hellos = {}
        self.sequences = {}
        self.writes = {}
        self.threads = []
        self.stopped = threading.Event()
        self.fault = threading.Event()

    def connect(self):
        from syn_client.pubsub_client import private_connect
        try:
            for entry in self.endpoints:
                conn = private_connect(Path(entry["socket"]), entry["owner_uid"])
                self.connections[entry["node"]] = conn
                self.writes[entry["node"]] = threading.Lock()
                send(conn, {"profile": "syn.private-pubsub-link/1", "scope": self.selected})
                hello = receive(conn)
                verify_hello(hello, self.selected, expected_node=entry["node"])
                self.hellos[entry["node"]] = hello
            # No message is accepted until all selected real nodes have signed
            # this fresh session and their own live boot incarnation.
            for conn in self.connections.values():
                send(conn, {"kind": "ready", "hellos": list(self.hellos.values())})
            for node, conn in self.connections.items():
                thread = threading.Thread(target=self._reader, args=(node, conn), daemon=True)
                self.threads.append(thread)
                thread.start()
        except BaseException:
            self.fault.set()
            self.close()
            raise

    def _send(self, node, packet):
        with self.writes[node]:
            send(self.connections[node], packet)

    def _reader(self, node, conn):
        import select
        try:
            while not self.stopped.is_set():
                if not select.select([conn], [], [], 0.2)[0]:
                    continue
                packet = exact(receive(conn), {"kind", "envelope"}, "relay publication")
                if packet["kind"] != "message":
                    raise SynProtocolError("Relay accepts typed ciphertext publications only")
                header = verify_message(packet["envelope"], self.selected, expected_node=node,
                                        expected_boot=self.hellos[node]["body"]["boot_id"])
                if header["sequence"] <= self.sequences.get(node, 0):
                    raise SynProtocolError("Relay publication is replayed or reordered")
                self.sequences[node] = header["sequence"]
                for peer in self.connections:
                    if peer != node:
                        self._send(peer, packet)
                digest = hashlib.sha256(canonical(packet["envelope"])).hexdigest()
                self._send(node, {"kind": "ack", "message_sha256": digest})
        except BaseException:
            if not self.stopped.is_set():
                self.fault.set()
                self.stopped.set()

    def close(self):
        self.stopped.set()
        for conn in self.connections.values():
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.name != "posix" or os.getuid() != 0:
        raise SystemExit("Private native relay requires the privileged local deployment controller")
    relay = PrivateRelay(parse(private_read(args.config)))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: relay.stopped.set())
    try:
        relay.connect()
        value = {"profile": "syn.private-relay-ready/1", "session_id": relay.selected["session_id"],
            "workspace_id": relay.selected["workspace_id"], "nodes": sorted(relay.connections),
            "plaintext_access": False, "cog_execution_authorized": False}
        sys.stdout.buffer.write(canonical(value) + b"\n")
        sys.stdout.buffer.flush()
        while not relay.stopped.wait(0.1):
            pass
        return 2 if relay.fault.is_set() else 0
    finally:
        relay.close()


if __name__ == "__main__":
    raise SystemExit(main())
