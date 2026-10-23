"""Export bounded Type authoring state; export is not trusted policy adoption."""

from __future__ import annotations

from collections.abc import Sequence

from syn_client.consensus import CanonicalTypeDefinition, ConsensusPolicy, canonical_bytes

from .models import Type, TypeProperty


def exportTypeDefinition(
    record: Type,
    *,
    revision: str,
    parents: Sequence[str],
    properties: Sequence[TypeProperty],
) -> CanonicalTypeDefinition:
    """Freeze the supplied authoring revision; governance must adopt its digest.

    No statistics, credentials or arbitrary knowledge are implicitly exported.
    Semantic schema changes still require a new explicitly selected revision.
    """
    from syn_client.consensus import type_ref

    policy = record.consensusPolicy
    parentRefs = sorted(type_ref(parent) for parent in parents)
    if len(parentRefs) != len(set(parentRefs)) or str(record.id) in parentRefs:
        raise ValueError("Invalid canonical Type parents")
    fields = sorted(
        (
            {
                "name": p.name,
                "type_ref": type_ref(str(p.propertyTypeRef)),
                "minimum": p.min,
                "maximum": p.max,
            }
            for p in properties
        ),
        key=lambda field: field["name"],
    )
    if len({p["name"] for p in fields}) != len(fields):
        raise ValueError("Duplicate canonical Type property")
    body = {"name": record.name, "parents": parentRefs, "properties": fields}
    return CanonicalTypeDefinition(
        str(record.id),
        revision,
        canonical_bytes(body).decode("utf-8"),
        ConsensusPolicy.from_dict(policy) if policy is not None else None,
    )
