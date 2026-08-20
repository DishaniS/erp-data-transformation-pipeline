"""Content hashing for AI-ready representations.

PHASE 10 OWNS THIS CONVENTION (Step 9)
--------------------------------------
Phase 10's incremental cascade already decides whether to re-embed by comparing
``representation_content_hash``. If Phase 11 introduced a second hash formula,
the two would disagree the moment either changed, and the symptom would be
either "everything re-embeds every run" or "nothing ever re-embeds" - both
silent.

So this module does not define a hash. It re-exports Phase 10's, and adds only
the chunk-identity helper that Phase 10 had no reason to have. Anything needing
a representation hash - including Phase 11 - goes through the same function.
"""

from __future__ import annotations

from erp_pipeline.schemas.identity import hash_json_payload, normalize_identifier
from erp_pipeline.sync.hashing import (
    VOLATILE_KEYS,
    representation_content_hash,
    strip_volatile,
    vector_id_for,
)


def chunk_content_hash(chunk_id: str, text: str) -> str:
    """Deterministic hash of one document chunk's text.

    Keyed by chunk id as well as text so two chunks that happen to contain the
    same boilerplate - a repeated header, an empty page - remain distinguishable
    rather than collapsing onto one identity.
    """
    return representation_content_hash(chunk_id, text_for_ai=text)


def make_chunk_id(document_id: str, chunk_index: int, config_fingerprint: str) -> str:
    """Stable chunk identity (Step 14).

    Includes the chunking CONFIGURATION, because chunk 3 of a document split at
    800 characters is not chunk 3 of the same document split at 400. Without it
    a configuration change would silently overwrite unrelated vectors.
    """
    suffix = hash_json_payload({"config": config_fingerprint})[:8]

    return normalize_identifier(f"{document_id}.c{chunk_index:05d}.{suffix}")


__all__ = [
    "VOLATILE_KEYS",
    "strip_volatile",
    "representation_content_hash",
    "vector_id_for",
    "chunk_content_hash",
    "make_chunk_id",
]
