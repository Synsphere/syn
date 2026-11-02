"""Actual native-custody pub-sub checks inside the owned two-Agent fixture."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import select
import subprocess
import sys
import time
from uuid import uuid4

from syn_client.operator_protocol import canonical
from syn_client.pubsub_client import request


def qualify(nodes, base):
    # Every member was independently enrolled, restarted and observed by the
    # enclosing real native fixture. No synthetic signing/custody is injected.
    records = []
    for session, first, second, outside in nodes:
        body = second["status"]
        records.append({"node": hashlib.sha256(bytes.fromhex(body["selected_public_key"])).hexdigest(),
                        "socket": str(Path(session.host["home"]) / "syn/agent.sock"),
                        "owner_uid": session.host["uid"], "status": body})
    certificate = records[0]["status"]["group_certificate"]["body"]
    selected = {"profile": "syn.private-pubsub-scope/1", "workspace_id": str(uuid4()),
                "session_id": secrets.token_hex(16), "operator": certificate["operator_public_key_hash"],
                "network_id": certificate["network_id"], "group_id": certificate["group_id"],
                "group_epoch": certificate["group_epoch"],
                "members": [entry["status"]["group_certificate"] for entry in records]}
    config = base / "private-relay.json"
    config.write_bytes(canonical({"profile": "syn.private-relay-config/1", "scope": selected,
        "endpoints": [{key: entry[key] for key in ("node", "socket", "owner_uid")} for entry in records]}))
    config.chmod(0o600)
    relay = subprocess.Popen([sys.executable, "-I", "-B", "-m", "syn.mesh", "--config", str(config)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if not select.select([relay.stdout], [], [], 15)[0]:
            raise AssertionError("Real canonical Syn relay did not become ready")
        line = relay.stdout.readline(65537)
        if not line:
            raise AssertionError("Real canonical Syn relay refused native endpoints")
        ready = json.loads(line)
        assert ready["session_id"] == selected["session_id"] and ready["plaintext_access"] is False

        def call(entry, operation, payload=None):
            return request(socket_path=Path(entry["socket"]), owner_uid=entry["owner_uid"],
                expected_node=entry["node"], configuration_sha256=entry["status"]["configuration_sha256"],
                selected_scope=selected, operation=operation, payload=payload,
                expected_boot=entry["status"]["boot_id"])

        for entry in records:
            assert call(entry, "status")["reply"]["result"]["cog_execution_authorized"] is False
        payload = {"fixture": "native-encrypted-pubsub", "nonce": secrets.token_hex(32)}
        sent = call(records[0], "publish", {"topic": "synny://type/potbot-worker-heartbeat-v1", "value": payload})
        assert sent["reply"]["result"]["relay_accepted"] is True
        assert sent["reply"]["result"]["peer_reception_proved"] is False
        deadline = time.monotonic() + 5
        while True:
            received = call(records[1], "receive")
            messages = received["reply"]["result"]["messages"]
            if messages:
                break
            assert time.monotonic() < deadline
            time.sleep(0.05)
        delivery, = messages
        assert delivery["payload"] == payload
        assert delivery["envelope"]["header"]["node"] == records[0]["node"]
        assert records[0]["node"] != records[1]["node"]
        assert payload["nonce"].encode() not in canonical(delivery["envelope"])
        assert relay.poll() is None
        # Losing the actual Syn transport must fence both genuine Agent nodes.
        relay.terminate()
        relay.wait(timeout=5)
        for session, *_ in nodes:
            session.process.wait(timeout=12)
            assert session.process.returncode == 2
            assert not (Path(session.host["home"]) / "syn/agent.sock").exists()
        print("Real native encrypted Syn pub-sub, signed reception and relay-loss fencing passed", flush=True)
        return {"profile": "syn.private-pubsub-native-acceptance/1", "result": "passed",
                "scope": selected, "publication": sent, "reception": received,
                "relay_loss_fenced_both_native_nodes": True, "cog_execution_authorized": False}
    finally:
        if relay.poll() is None:
            relay.kill()
        relay.wait(timeout=5)
        if relay.stdout is not None:
            relay.stdout.close()
        if relay.stderr is not None:
            relay.stderr.close()
