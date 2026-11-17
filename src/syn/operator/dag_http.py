"""Read-only public proof endpoints for the local Operator's signed DAG.

Only public digests and signatures are exported; journal bodies remain private.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Path
from cryptography.exceptions import InvalidSignature

from . import dag_proofs
from .dag_evidence import latest, inclusion_bundle, consistency_bundle

log = logging.getLogger(__name__)


def build_router(sessions, operator):
    router = APIRouter(prefix="/v1/operator/dag", tags=["Operator DAG evidence"])

    def public_result(action, *args):
        try:
            return action(sessions, operator, *args)
        except (ValueError, LookupError, InvalidSignature) as error:
            # Never return private journal/config text in a public error.
            log.warning("Operator DAG proof unavailable: %s", type(error).__name__)
            raise HTTPException(409, "Signed Operator DAG evidence is not available or failed verification") from None

    @router.get("/checkpoint")
    def current_checkpoint():
        return public_result(latest)

    @router.get("/inclusion/{index}")
    def inclusion(index: int = Path(ge=0, lt=dag_proofs.MAX_LEAVES)):
        return public_result(inclusion_bundle, index)

    @router.get("/consistency/{old_size}")
    def consistency(old_size: int = Path(ge=1, le=dag_proofs.MAX_LEAVES)):
        return public_result(consistency_bundle, old_size)

    return router
