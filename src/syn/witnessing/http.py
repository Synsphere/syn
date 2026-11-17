"""Read-only witness summaries and signed fork evidence; no peer URL disclosure."""
from fastapi import APIRouter, HTTPException

from .store import WitnessStore


def build_router(sessions, observer, node_ref, config):
    router = APIRouter(prefix="/v1/operator/dag/witnesses", tags=["Operator DAG witnesses"])
    store = WitnessStore(sessions, observer, node_ref)
    peers = {p.operator: p for p in config.peers}

    def state(peer_hash):
        peer = peers.get(peer_hash)
        if peer is None:
            raise HTTPException(404, "Peer is not configured")
        try:
            return store.snapshot(peer)
        except (ValueError, LookupError):
            raise HTTPException(409, "Retained witness evidence failed verification") from None

    @router.get("")
    def summaries():
        result = []
        for peer in config.peers:
            saved = state(peer.operator)
            checkpoint = saved["checkpoint"]
            result.append({"operator": peer.operator, "network": peer.network,
                "size": checkpoint["body"]["sequence"] if checkpoint else None,
                "quarantined": saved["conflicted"],
                "last_outcome": saved["last"]["outcome"] if saved["last"] else "not-observed"})
        return {"observer": observer, "peers": result, "consensus": False}

    @router.get("/{peer_hash}/checkpoint")
    def checkpoint(peer_hash: str):
        value = state(peer_hash)["checkpoint"]
        if value is None:
            raise HTTPException(409, "No verified peer checkpoint is retained")
        return value

    @router.get("/{peer_hash}/conflict")
    def conflict(peer_hash: str):
        saved = state(peer_hash)
        last = saved["last"]
        if not saved["conflicted"] or not last or last["outcome"] not in {"equivocation", "checkpoint-conflict"}:
            raise HTTPException(409, "No contradictory signed checkpoints are retained")
        return {"profile": "syn.operator-dag-conflict/1", "observer": observer,
                "outcome": last["outcome"], "first": last["previous_checkpoint"], "second": last["checkpoint"]}

    return router
