"""Normal, process-safe recurring job for passive local radio discovery."""
from __future__ import annotations

from .config import RadioConfig
from .discovery import RadioDiscovery
from .store import RadioStore
from .types import digest

RADIO_DISCOVER = "radio-discover"


def schedule_key(node_ref: str) -> str:
    return "radio-" + digest([node_ref, "discover"])


def run_discovery(database_url: str, configuration: dict, node_ref: str, **_arguments):
    from syn.positions.jobs import sessions_for
    settings = RadioConfig.from_dict(configuration)
    if not settings.enabled:
        return {"status": "disabled"}
    # Hardware discovery runs outside the database transaction. All recorded
    # results and canonical state share one transaction with the node hash ledger.
    results = RadioDiscovery(settings).scan()
    with sessions_for(database_url) as sessions:
        return RadioStore(sessions).record(node_ref, results)
