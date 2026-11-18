#!/usr/bin/env python3
"""Opt-in interoperability acceptance against an independently installed Kubo.

Run with the repository environment, for example:
  uv run python scripts/verify_amino_interop.py --kubo /path/to/ipfs

The temporary Kubo node has no bootstrap peers, delegated routers, automatic
configuration, mDNS, relay service, or reproviding. All identities are synthetic
and all advertised addresses are loopback. Nothing is published to public Amino.
This script needs a real Kubo executable; the normal pytest suite does not.
Kubo's WAN DHT filters loopback provider addresses. The script verifies the
stored provider PeerID on the wire, verifies that Syn rejects an undialable
candidate, and checks its signed record using the explicit local fixture
address. It does not qualify public reachability or public-address discovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.crypto.x25519 import create_new_key_pair as noise_key_pair
from libp2p.kad_dht.pb.kademlia_pb2 import Message as KadMessage
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.security.noise.transport import PROTOCOL_ID as NOISE, Transport
from libp2p.stream_muxer.yamux.yamux import PROTOCOL_ID as YAMUX, Yamux
from multiaddr import Multiaddr
import trio

from syn.amino import AminoDHTClient
from syn import peer_record
from syn.peer_record_transport import PeerRecordService, fetch_peer_record


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _host():
    keys = create_new_key_pair()
    host = new_host(key_pair=keys, bootstrap=[], enable_upnp=False,
        sec_opt={NOISE: Transport(keys, noise_privkey=noise_key_pair().private_key)},
        muxer_opt={YAMUX: Yamux})
    return host, keys


class LocalKubo:
    def __init__(self, binary: Path, root: Path):
        self.binary = binary
        self.root = root
        self.env = {**os.environ, "IPFS_PATH": str(root / "repo")}
        self.api_port = _port()
        self.swarm_port = _port()
        self.process = None
        self.log = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def command(self, *arguments: str) -> bytes:
        completed = subprocess.run([str(self.binary), *arguments], env=self.env,
            capture_output=True, timeout=30, check=True)
        return completed.stdout

    def start(self) -> None:
        self.command("init", "--profile=test")
        path = Path(self.env["IPFS_PATH"]) / "config"
        config = json.loads(path.read_text())
        config["Addresses"]["Swarm"] = [f"/ip4/127.0.0.1/tcp/{self.swarm_port}"]
        config["Addresses"]["Announce"] = config["Addresses"]["Swarm"]
        config["Addresses"]["API"] = f"/ip4/127.0.0.1/tcp/{self.api_port}"
        config["Addresses"]["Gateway"] = []
        config["Bootstrap"] = []
        config["Discovery"]["MDNS"]["Enabled"] = False
        config["Routing"]["Type"] = "dhtserver"
        config["Routing"]["DelegatedRouters"] = []
        config["Ipns"]["DelegatedPublishers"] = []
        config.setdefault("AutoConf", {})["Enabled"] = False
        config.setdefault("Provide", {})["Enabled"] = False
        config.setdefault("AutoTLS", {})["Enabled"] = False
        config["Swarm"]["RelayClient"]["Enabled"] = False
        config["Swarm"]["RelayService"]["Enabled"] = False
        config["Swarm"]["DisableNatPortMap"] = True
        config["AutoNAT"]["ServiceMode"] = "disabled"
        config.setdefault("Internal", {})["NonPublicAddrPublishing"] = True
        path.write_text(json.dumps(config, indent=2) + "\n")
        self.log = (self.root / "daemon.log").open("wb")
        self.process = subprocess.Popen([str(self.binary), "daemon"], env=self.env,
            stdout=self.log, stderr=subprocess.STDOUT)

    def rpc(self, command: str, *arguments: str, timeout: float = 15) -> bytes:
        query = urllib.parse.urlencode([("arg", arg) for arg in arguments] +
                                      [("timeout", f"{timeout:g}s")])
        url = f"http://127.0.0.1:{self.api_port}/api/v0/{command}?{query}"
        with self.opener.open(urllib.request.Request(url, data=b"", method="POST"),
                              timeout=timeout + 1) as response:
            return response.read()

    async def ready(self) -> dict:
        with trio.fail_after(20):
            while True:
                if self.process.poll() is not None:
                    raise RuntimeError("Kubo exited before API readiness")
                try:
                    return json.loads(await trio.to_thread.run_sync(
                        lambda: self.rpc("id", timeout=1)))
                except (OSError, ValueError):
                    await trio.sleep(.1)

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log is not None:
            self.log.close()


async def exercise(kubo: LocalKubo) -> dict:
    identity = await kubo.ready()
    bootstrap = f"/ip4/127.0.0.1/tcp/{kubo.swarm_port}/p2p/{identity['ID']}"
    provider, keys = _host()
    lookup_host, _ = _host()
    operator_key = Ed25519PrivateKey.generate()
    public = operator_key.public_key().public_bytes_raw()
    operator = hashlib.sha256(public).hexdigest()
    current = None
    PeerRecordService(provider, public, lambda: current).register()
    listeners = [Multiaddr("/ip4/127.0.0.1/tcp/0")]
    async with provider.run(listen_addrs=listeners), lookup_host.run(listen_addrs=listeners):
        now = int(time.time())
        body = {"profile": peer_record.PROFILE, "operator": operator,
            "network_id": "syn", "peer_id": str(provider.get_id()),
            "transport_public_key": keys.public_key.to_bytes().hex(),
            "addresses": sorted(str(addr) for addr in provider.get_addrs()),
            "issued_at": now, "expires_at": now + 300}
        current = {"body": body, "public_key": public.hex(),
            "signature": operator_key.sign(peer_record.DOMAIN + peer_record.canonical(body)).hex()}
        print("Connecting independent Kubo to synthetic Syn provider", file=sys.stderr, flush=True)
        await trio.to_thread.run_sync(lambda: kubo.rpc("swarm/connect", str(provider.get_addrs()[0])))
        cid = peer_record.provider_cid(operator)
        print("Retrieving raw public-key block with Kubo Bitswap", file=sys.stderr, flush=True)
        block = await trio.to_thread.run_sync(lambda: kubo.rpc("block/get", cid))
        if block != public or hashlib.sha256(block).hexdigest() != operator:
            raise AssertionError("Kubo fetched a different public-key block")
        dht = AminoDHTClient(provider, [bootstrap], timeout=5, allow_private=True)
        print("Bootstrapping Syn against Kubo /ipfs/kad/1.0.0", file=sys.stderr, flush=True)
        responsive = await dht.bootstrap()
        if responsive != 1:
            raise AssertionError(f"Kubo DHT bootstrap failed: {dht.status()}")
        sent = await dht.provide(peer_record.provider_key(operator), provider.get_addrs())
        if sent != 1:
            raise AssertionError(f"Kubo provider announcement failed: {dht.status()}")
        finder = AminoDHTClient(lookup_host, [bootstrap], timeout=5, allow_private=True)
        if await finder.bootstrap() != 1:
            raise AssertionError(f"Independent lookup bootstrap failed: {finder.status()}")
        print("Looking up the provider from a separate Syn host", file=sys.stderr, flush=True)
        candidates = await finder.find_providers(peer_record.provider_key(operator))
        # Inspect the independent implementation's actual wire response, before
        # the production adapter intentionally drops peers without dialable IPs.
        raw_response = await finder._request(info_from_p2p_addr(Multiaddr(bootstrap)),
            KadMessage(type=KadMessage.GET_PROVIDERS, key=peer_record.provider_key(operator)))
        remote_provider = next((entry for entry in raw_response.providerPeers
            if bytes(entry.id) == provider.get_id().to_bytes()), None)
        if raw_response.key != peer_record.provider_key(operator) or remote_provider is None:
            raise AssertionError("Kubo did not retain the exact provider key and PeerID")
        selected = next((peer for peer in candidates if peer.peer_id == provider.get_id()), None)
        loopback_filtered = not remote_provider.addrs
        address_source = "Kubo provider response"
        if selected is None:
            if not loopback_filtered:
                raise AssertionError("Syn discarded a Kubo provider with dialable test addresses")
            # WAN Kademlia intentionally strips loopback addresses. Do not invent
            # a public address or misreport this explicit fixture as discovery.
            selected = info_from_p2p_addr(provider.get_addrs()[0])
            address_source = "explicit loopback fixture; WAN DHT filtered addresses"
        print("Verifying the Operator-signed record over authenticated transport", file=sys.stderr, flush=True)
        record = await fetch_peer_record(lookup_host, selected,
            expected_operator=operator, expected_network="syn", timeout=5)
        if record != current:
            raise AssertionError("Discovered signed record differs from the served record")
        return {"passed": True, "scope": "isolated loopback; synthetic identities only",
            "kuboVersion": identity.get("AgentVersion"), "protocol": "/ipfs/kad/1.0.0",
            "bitswap": {"fetchedBy": "Kubo", "cid": cid, "bytes": len(block),
                        "sha256MatchesOperator": True},
            "bootstrapResponsive": responsive, "providerAnnouncementsSent": sent,
            "providerKeyAndPeerIdRoundTrip": True,
            "wanFilteredLoopbackAddresses": loopback_filtered,
            "independentLookupDialableProviders": len(candidates),
            "signedRecordVerified": True, "recordAddressSource": address_source,
            "publicReachabilityTested": False,
            "providerStatus": dht.status(), "lookupStatus": finder.status()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubo", type=Path, required=True, help="Official Kubo ipfs executable")
    parser.add_argument("--report", type=Path, help="Also write the JSON result to this file")
    arguments = parser.parse_args()
    binary = arguments.kubo.resolve(strict=True)
    logging.basicConfig(level=logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="syn-kubo-interop-") as directory:
        kubo = LocalKubo(binary, Path(directory))
        try:
            kubo.start()
            result = trio.run(exercise, kubo)
        except Exception as error:
            traceback.print_exception(error)
            result = {"passed": False, "error": f"{type(error).__name__}: {error}"}
            log = Path(directory) / "daemon.log"
            if log.exists():
                print(log.read_text(errors="replace")[-12000:], file=sys.stderr)
        finally:
            kubo.close()
    encoded = json.dumps(result, indent=2) + "\n"
    print(encoded, end="")
    if arguments.report is not None:
        arguments.report.write_text(encoded)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
