"""Deterministic content hashing for AI-ready representations.

WHY THIS MATTERS MORE THAN IT LOOKS
-----------------------------------
The content hash is the gate that decides whether an embedding is regenerated.
Get it wrong in one direction and every sync re-embeds everything, which is the
expensive full rebuild this phase exists to avoid. Get it wrong in the other
and a genuinely changed case keeps a stale vector for ever, which is worse:
the search index quietly lies.

So the hash covers exactly the SEMANTIC CONTENT sent for embedding, and
nothing else. Excluded by construction (Step 21):

    run ids, timestamps, durations, random UUIDs, sync bookkeeping,
    row counters, operational status columns

Two representations with the same hash carry the same meaning for an embedding
model, whatever else changed around them.

REUSE
-----
Built on the frozen ``schemas.identity.compute_content_hash`` rather than a
second SHA-256 implementation, so the ordering, ``None``-stripping and encoding
rules are provably the same ones the canonical layer already uses.
"""

from __future__ import annotations

from typing import Any, Mapping

from erp_pipeline.schemas.identity import (
    compute_content_hash,
    make_deterministic_uuid,
)

#: Keys never included in a content hash, whatever a builder puts in metadata.
#: A representation whose hash moved because a timestamp ticked would re-embed
#: on every run - the exact failure this module prevents.
VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "run_id",
        "sync_run_id",
        "created_at",
        "updated_at",
        "synced_at",
        "last_synced_at",
        "extracted_at",
        "processed_at",
        "duration",
        "duration_seconds",
        "embedding_status",
        "watermark",
        "version",
        "revision",
    }
)


def strip_volatile(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Remove operational bookkeeping before hashing, at any depth."""
    if not payload:
        return {}

    result: dict[str, Any] = {}

    for key, value in payload.items():
        if str(key).lower() in VOLATILE_KEYS:
            continue
        if isinstance(value, Mapping):
            result[key] = strip_volatile(value)
        else:
            result[key] = value

    return result


def representation_content_hash(
    representation_id: str,
    text_for_ai: str | None = None,
    content: Mapping[str, Any] | None = None,
) -> str:
    """The deterministic hash of one AI-ready representation.

    Identical inputs always produce an identical digest, and any change to the
    embedded text or the structured payload changes it.
    """
    return compute_content_hash(
        record_id=representation_id,
        content=strip_volatile(content),
        text_for_ai=text_for_ai,
    )


def vector_id_for(representation_id: str) -> str:
    """The stable vector identity for a representation (Step 25).

    Derived from the representation's own id, so updating content reuses the
    SAME vector record rather than accumulating a new point per sync run.
    Uses the frozen UUIDv5 derivation, which is what the existing BPI vector
    identity convention already relies on.
    """
    return make_deterministic_uuid(representation_id)


__all__ = [
    "VOLATILE_KEYS",
    "strip_volatile",
    "representation_content_hash",
    "vector_id_for",
]
