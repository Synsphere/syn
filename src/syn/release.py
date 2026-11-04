"""Read-only installed capabilities. No config, database, keys or network access."""
from importlib.metadata import version

from syn_client.releases import RuntimeContract


def runtimeContract() -> RuntimeContract:
    return RuntimeContract(
        runtime_version=version("syn"),
        client_version=version("syn-client"),
        python_versions=("3.12", "3.13"),
        runtime_platforms=("darwin-arm64", "darwin-x86_64", "linux-x86_64"),
        client_platforms=("darwin-arm64", "darwin-x86_64", "linux-x86_64", "windows-x86_64"),
        node_modes=("agent", "operator", "service"),
        api_profiles=(
            "syn.agent-group-rollover/1", "syn.agent-group/1", "syn.agent-local/1", "syn.communication/1", "syn.consensus-policy/1",
            "syn.dag-hashgraph-finality/1", "syn.dag-hashgraph-finality/2",
            "syn.governed-policy-record/1", "syn.governed-policy-record/2",
            "syn.lan-enrollment-request/1", "syn.legacy-state-archive/1", "syn.operator-local/1", "syn.operator-recovery/1",
            "syn.private-pubsub/1",
        ),
        capabilities=(
            "authenticated-agent-ipc", "authenticated-lan-enrollment", "authenticated-operator-ipc", "authenticated-private-pubsub", "confidential-selected-key-adoption",
            "group-member-revocation", "host-bound-virtual-device", "http-workspace-messages", "native-operator-recovery", "native-os-agent-custody", "native-os-group-custody", "native-os-operator-custody", "offline-governance-verification",
            "offline-protected-dag-verification", "offline-type-policy-resolution",
            "read-only-legacy-state-archive", "retained-agent-identity", "retained-operator-identity",
        ),
        database_revision="20261024_0015",
    )
