"""Qualify the complete public Potbot mesh adapter against installed Syn bytes.

This proves native identity-bound admission and real heartbeat transport.
An optional reviewed consumer probe can exercise a disposable monitored Cog
while the official release cohort is still live.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.syn_release.agent import SynAgentDeployment
from potbot.syn_release.mesh import SynPrivateMesh
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from syn_client.agent import agent_status


def qualify(nodes, base, *, root, store=None, policy=None, source_runtime=None,
            source_contract=None, monitored_worker_probe=None):
    if monitored_worker_probe is not None and store is None:
        raise ValueError("Monitored worker authorization requires the installed official release")
    certificates = [item[2]["status"]["group_certificate"] for item in nodes]
    body = certificates[0]["body"]
    participants = {}

    class SourceWheelDeployment(SynAgentDeployment):
        """Only package/assignment registry is substituted in the SOURCE fixture.

        No identity, signature, native custody or process check is substituted.
        Official acceptance always uses the actual installer/deployment class.
        """
        def _installation(self):
            return {"observed": {"runtime": source_contract}}, source_runtime / "bin/python"

        def attach(self):
            known = self.source_proof["status"]
            return agent_status(socket_path=self.root / self.agent_id.hex / "home/syn/agent.sock",
                owner_uid=self.uid, expected_hash=hashlib.sha256(bytes.fromhex(known["selected_public_key"])).hexdigest(),
                expected_operator=self.expected_operator, expected_version=source_contract["runtime_version"],
                configuration_sha256=known["configuration_sha256"], assignment_sha256=known["assignment_sha256"],
                expected_group=str(self.group_id), expected_epoch=self.group_epoch)

    for session, first, second, outside in nodes:
        agent = UUID(session.host["agent_id"])
        if store is not None:
            deployment = SynAgentDeployment(store, policy, root, agent_id=agent, uid=session.host["uid"],
                expected_operator=body["operator_public_key_hash"], network_id=body["network_id"],
                group_id=UUID(body["group_id"]), group_epoch=body["group_epoch"],
                device_profile="syn.host-bound-virtual-device/1")
        else:
            assert source_runtime is not None and source_contract is not None
            deployment = SourceWheelDeployment.__new__(SourceWheelDeployment)
            deployment.root, deployment.agent_id, deployment.uid = root, agent, session.host["uid"]
            deployment.expected_operator, deployment.network_id = body["operator_public_key_hash"], body["network_id"]
            deployment.group_id, deployment.group_epoch = UUID(body["group_id"]), body["group_epoch"]
            deployment.policy = SimpleNamespace(manifest_sha256="0" * 64, fingerprint="0" * 64,
                value={"source_commit": "0" * 40, "version": source_contract["runtime_version"]})
            deployment.source_proof = second
        participants[agent] = (deployment, session.api)

    class RecordedTransport(SynPrivateMesh):
        def call(self, agent_id, operation, payload=None):
            try:
                value = super().call(agent_id, operation, payload)
            except StateMismatchError as error:
                if monitored_worker_probe is not None:
                    public = str(error)
                    if public in {
                        "Native Syn command capacity deadline exceeded",
                        "Upstream Syn operation exceeded its deadline",
                        "Upstream Syn refused; reconcile native keystore and state",
                    }:
                        print("Official native operation failed:", operation, public, flush=True)
                raise
            if operation == "receive" and value["reply"]["result"]["messages"] and len(receptions) < 8:
                receptions.append(value)
            return value

    receptions = []
    workspace = uuid4()
    transport = RecordedTransport(base / "potbot-private-mesh", workspace_id=workspace, participants=participants)
    workers = (
        ReleasedSynWorkerNodeMesh(transport) if monitored_worker_probe is not None
        else ReleasedSynWorkerNodeMesh(transport, interval_seconds=1)
    )
    actor = WorkspaceContext(workspace, uuid4())
    policy_pin = ExecutionPolicy("a" * 64, "b" * 64, "c" * 64, "native-admission-fixture")
    operation = SandboxedCogOperation(WorkBinding("tool", "syn://thing/undispatched-fixture",
        "syn://type/fixture-input", "syn://type/fixture-output", "d" * 64, "read_only", policy_pin),
        ("/usr/bin/never-dispatched",), "e" * 64)
    source = TypedCogValue("syn://type/fixture-input", {"fixture": True})
    context = CogRunContext("f" * 64, uuid4(), uuid4(), uuid4())
    first, second = transport.agents
    monitored_evidence = None

    async def exercise():
        nonlocal monitored_evidence
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
        if monitored_worker_probe is not None:
            monitored_evidence = await monitored_worker_probe(workers, base)
            assert monitored_evidence.get("result") == "passed"
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
    print("Real Potbot public mesh adapter, signed native heartbeats, task-grant rejection and relay-loss fencing passed", flush=True)
    evidence = {"profile": "syn.potbot-private-mesh-acceptance/1", "result": "passed", "scope": transport.scope,
        "native_adapter_verified": True, "heartbeat_receptions": receptions,
        "official_release_installation_verified": store is not None,
        "source_fixture_substitutions": ([] if store is not None else ["package installation registry", "retained assignment registry"]),
        "one_use_and_changed_task_grants_rejected": True,
        "executor_without_task_grant_rejected_before_dispatch": True,
        "relay_loss_fenced_both_native_nodes_and_cog_monitors": True,
        "cog_process_dispatched": monitored_evidence is not None,
        "host_cog_kernel_qualification": (
            "disposable_monitored_worker" if monitored_evidence else "independent_required_gate"
        )}
    if monitored_evidence is not None:
        evidence["monitored_worker_authorization"] = monitored_evidence
    return evidence
