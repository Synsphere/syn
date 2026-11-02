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
        node_modes=("operator", "service"),
        api_profiles=(
            "syn.communication/1", "syn.consensus-policy/1",
            "syn.dag-hashgraph-finality/1", "syn.dag-hashgraph-finality/2",
            "syn.governed-policy-record/1", "syn.governed-policy-record/2",
            "syn.operator-local/1",
        ),
        capabilities=(
            "authenticated-operator-ipc", "http-workspace-messages", "native-os-operator-custody", "offline-governance-verification",
            "offline-protected-dag-verification", "offline-type-policy-resolution",
            "retained-operator-identity",
        ),
        database_revision="20261024_0015",
    )
