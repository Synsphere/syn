"""Qualify the complete public Potbot mesh adapter against installed Syn bytes.

This proves native identity-bound admission and real heartbeat transport. It does
not qualify a Cog image, cgroup/TASKSTATS backend, provider or production host.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import time
from uuid import UUID, uuid4

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.syn_release.agent import SynAgentDeployment
from potbot.syn_release.mesh import SynPrivateMesh
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation


def qualify(nodes, base, *, root, store, policy):
    certificates = [item[2]["status"]["group_certificate"] for item in nodes]
    body = certificates[0]["body"]
    participants = {}
    for session, first, second, outside in nodes:
        agent = UUID(session.host["agent_id"])
        deployment = SynAgentDeployment(store, policy, root, agent_id=agent, uid=session.host["uid"],
            expected_operator=body["operator_public_key_hash"], network_id=body["network_id"],
            group_id=UUID(body["group_id"]), group_epoch=body["group_epoch"],
            device_profile="syn.host-bound-virtual-device/1")
        participants[agent] = (deployment, session.api)

    class RecordedTransport(SynPrivateMesh):
        def call(self, agent_id, operation, payload=None):
            value = super().call(agent_id, operation, payload)
            if operation == "receive" and value["reply"]["result"]["messages"] and len(receptions) < 8:
                receptions.append(value)
            return value

    receptions = []
    workspace = uuid4()
    transport = RecordedTransport(base / "potbot-private-mesh", workspace_id=workspace, participants=participants)
    workers = ReleasedSynWorkerNodeMesh(transport, interval_seconds=1)
    actor = WorkspaceContext(workspace, uuid4())
    policy_pin = ExecutionPolicy("a" * 64, "b" * 64, "c" * 64, "native-admission-fixture")
    operation = SandboxedCogOperation(WorkBinding("tool", "syn://thing/undispatched-fixture",
        "syn://type/fixture-input", "syn://type/fixture-output", "d" * 64, "read_only", policy_pin),
        ("/usr/bin/never-dispatched",), "e" * 64)
    source = TypedCogValue("syn://type/fixture-input", {"fixture": True})
    context = CogRunContext("f" * 64, uuid4(), uuid4(), uuid4())
    first, second = transport.agents

    async def exercise():
        grant = workers.authorize(operation, source, context, actor, first)
        workers.consume(grant, operation, source, context, actor, first)
        try:
            workers.consume(grant, operation, source, context, actor, first)
        except ScopeDeniedError:
            pass
        else:
            raise AssertionError("Consumed native task grant was replayable")
        grant = workers.authorize(operation, source, context, actor, first)
        try:
            workers.consume(grant, operation, source, replace(context, task_revision_id=uuid4()), actor, first)
        except ScopeDeniedError:
            pass
        else:
            raise AssertionError("Native task grant accepted a changed task revision")
        # Rejection uses the real executor entry point and occurs before any
        # unqualified image/supervisor is touched. No positive launch is faked.
        executor = SandboxedCogExecutor.__new__(SandboxedCogExecutor)
        executor.syn_mesh = workers
        try:
            await executor.execute(operation, source, context, actor, first)
        except ScopeDeniedError:
            pass
        else:
            raise AssertionError("Executor dispatched without a task authorization")
        async with workers.node(workspace, first, uuid4()) as left:
            async with workers.node(workspace, second, uuid4()) as right:
                deadline = time.monotonic() + 20
                while not left.snapshot()["peers"] or not right.snapshot()["peers"]:
                    assert time.monotonic() < deadline, "Native Cog heartbeat reception did not complete"
                    await asyncio.sleep(0.05)
                assert receptions, "Signed native heartbeat reception evidence missing"
                assert transport._process is not None
                transport._process.terminate()
                transport._process.wait(timeout=5)
                while not left.fault_event.is_set() or not right.fault_event.is_set():
                    assert time.monotonic() < deadline, "Relay loss did not fence both Cog monitors"
                    await asyncio.sleep(0.05)
                try:
                    workers.probe(workspace, first)
                except StateMismatchError:
                    pass
                else:
                    raise AssertionError("Native admission continued after relay loss")

    with transport.running(), workers.running():
        asyncio.run(exercise())
    for session, *_ in nodes:
        assert session.process.wait(timeout=15) == 2, "Native Syn process did not fence after relay loss"
        assert not (root / UUID(session.host["agent_id"]).hex / "home/syn/agent.sock").exists()
    return {"profile": "syn.potbot-private-mesh-acceptance/1", "result": "passed", "scope": transport.scope,
        "native_adapter_verified": True, "heartbeat_receptions": receptions,
        "one_use_and_changed_task_grants_rejected": True,
        "executor_without_task_grant_rejected_before_dispatch": True,
        "relay_loss_fenced_both_native_nodes_and_cog_monitors": True,
        "cog_process_dispatched": False, "host_cog_kernel_qualification": "independent_required_gate"}
