"""Two native Agent sessions exercise an operator-approved group cutoff."""
from __future__ import annotations

import hashlib
from pathlib import Path
import secrets
from unittest.mock import patch
from uuid import uuid4

from potbot.domain.errors import StateMismatchError
from syn.mesh import PrivateRelay
from syn.operator import adoption_cli
from syn_client.operator_protocol import SynProtocolError, canonical, receive, send
from syn_client.pubsub_client import private_connect, request


def qualify_rollover(stack, session_factory, nodes, config):
    survivor, revoked = (entry[0] for entry in nodes)
    old_status, revoked_status = (entry[2]["status"] for entry in nodes)
    old_cert, revoked_cert = (status["group_certificate"] for status in (old_status, revoked_status))
    operator = old_status["operator_public_key_hash"]
    group = old_cert["body"]["group_id"]
    retained_node = old_cert["body"]["node_public_key_hash"]
    revoked_node = revoked_cert["body"]["node_public_key_hash"]

    def reopen(node, epoch):
        session = session_factory(stack, node.host, operator, existing=True, password=node.password, epoch=epoch)
        assert session.answered == {0}
        assert session.initial["configuration_sha256"] == node.initial["configuration_sha256"]
        return session

    def maintenance(session, operation, payload=None):
        # The pinned Potbot adapter predates these two maintenance operations.
        raw = canonical({"operation": operation, "payload": payload}) + b"\n"
        session.process.stdin.write(raw)
        session.process.stdin.flush()
        return session.api.read(timeout=30)

    requests = {}
    for node, selected in ((survivor, retained_node), (revoked, revoked_node)):
        session = reopen(node, 1)
        requests[selected] = maintenance(session, "prepare-group-rollover")
        session.close()
        assert session.process.returncode == 0

    payload = {"group_id": group, "expected_epoch": 1, "revoke": [revoked_node],
               "requests": {retained_node: requests[retained_node]}}
    with patch.object(adoption_cli, "approve_rollover", return_value=True) as approval:
        # A retained leaf cannot have its encrypted delivery redirected to another key.
        invalid = {**payload, "requests": {retained_node: requests[revoked_node]}}
        try:
            adoption_cli.operate(config, "agent-group-rollover", invalid)
        except SynProtocolError:
            approval.assert_not_called()
        else:
            raise AssertionError("Rollover accepted a substituted adoption request")
        update = adoption_cli.operate(config, "agent-group-rollover", payload)
        assert adoption_cli.operate(config, "agent-group-rollover", payload) == update
        approval.assert_called_once()
    transition = update["transition"]
    assert transition["body"]["retained"] == [retained_node]
    assert transition["body"]["revoked"] == [revoked_node]
    assert list(update["transfers"]) == [retained_node]
    assert transition["body"]["epoch"] == 2

    for node, selected in ((survivor, retained_node), (revoked, revoked_node)):
        with_update = reopen(node, 2)
        applied = maintenance(with_update, "apply-group-rollover", {
            "transition": transition, "transfer": update["transfers"].get(selected),
        })
        assert applied["node"] == selected and applied["group_epoch"] == 2
        assert applied["retained"] is (selected == retained_node)
        assert applied["transition_sha256"] == hashlib.sha256(canonical(transition)).hexdigest()
        with_update.close()
        assert with_update.process.returncode == 0

    for epoch in (1, 2):
        excluded = reopen(revoked, epoch)
        try:
            excluded.command("serve")
        except StateMismatchError:
            assert excluded.process.wait(timeout=10) == 2
            assert not (Path(revoked.host["home"]) / "syn/agent.sock").exists()
        else:
            raise AssertionError("Revoked Agent started after a complete native restart")
        finally:
            excluded.close()

    admitted = reopen(survivor, 2)
    ready = admitted.command("serve")["status"]
    certificate = ready["group_certificate"]
    assert ready["selected_public_key"] == old_status["selected_public_key"]
    assert ready["assignment_sha256"] == old_status["assignment_sha256"]
    assert ready["boot_id"] != old_status["boot_id"]
    assert certificate["body"]["group_epoch"] == 2
    assert certificate["body"]["group_key_commitment"] != old_cert["body"]["group_key_commitment"]
    path = Path(survivor.host["home"]) / "syn/agent.sock"
    owner = survivor.host["uid"]
    scope = {"profile": "syn.private-pubsub-scope/1", "workspace_id": str(uuid4()),
             "session_id": secrets.token_hex(16), "operator": operator, "network_id": ready["network_id"],
             "group_id": group, "group_epoch": 1, "members": [old_cert, revoked_cert]}
    with private_connect(path, owner) as conn:
        send(conn, {"profile": "syn.private-pubsub-link/1", "scope": scope})
        try:
            receive(conn)
        except SynProtocolError:
            pass
        else:
            raise AssertionError("Retained Agent admitted the prior cohort after restart")

    scope.update(session_id=secrets.token_hex(16), group_epoch=2, members=[certificate])
    relay = PrivateRelay({"profile": "syn.private-relay-config/1", "scope": scope,
                          "endpoints": [{"node": retained_node, "socket": str(path), "owner_uid": owner}]})
    try:
        relay.connect()
        status = request(socket_path=path, owner_uid=owner, expected_node=retained_node,
                         configuration_sha256=ready["configuration_sha256"], selected_scope=scope,
                         operation="status", expected_boot=ready["boot_id"])
        assert status["reply"]["result"]["peers"] == [retained_node]
    finally:
        relay.close()
    assert admitted.process.wait(timeout=15) == 2
    admitted.close()
    print("Group rollover passed: retained Agent joined epoch 2; revoked Agent refused both epoch pins after restart",
          flush=True)
    return {"profile": "syn.agent-group-rollover-native-acceptance/1", "result": "passed",
            "transition": transition, "retained_status": status,
            "revoked_restart_refused_epochs": [1, 2], "prior_cohort_refused_after_restart": True,
            "retained_identity_and_assignment_preserved": True, "new_group_key_commitment": True}
