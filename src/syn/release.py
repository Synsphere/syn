"""Read-only installed capabilities. No config, database, keys or network access."""
from importlib.metadata import version

from syn_client.releases import RuntimeContract


def runtimeContract() -> RuntimeContract:
    return RuntimeContract(
        runtime_version=version("syn"),
        client_version=version("syn-client"),
        python_versions=("3.12", "3.13"),
        runtime_platforms=("linux-x86_64",),
        client_platforms=("linux-x86_64", "windows-x86_64"),
        node_modes=("service",),
        api_profiles=(
            "syn.communication/1", "syn.consensus-policy/1",
            "syn.dag-hashgraph-finality/1", "syn.dag-hashgraph-finality/2",
            "syn.governed-policy-record/1", "syn.governed-policy-record/2",
        ),
        capabilities=(
            "http-workspace-messages", "offline-governance-verification",
            "offline-protected-dag-verification", "offline-type-policy-resolution",
        ),
        database_revision="20261024_0015",
    )
