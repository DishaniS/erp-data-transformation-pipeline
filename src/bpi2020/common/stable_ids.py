"""Deterministic cross-layer record identifiers for the BPI 2020 pipeline.

Phase 0 identity rule
---------------------
PostgreSQL ``SERIAL`` values are internal table primary keys ONLY.

Any record that crosses a pipeline boundary - a JSON/JSONL file, a Qdrant
point, or a foreign reference held by another table - must be identified by a
deterministic identifier built here.

Why this module exists
----------------------
The case and document tables are rebuilt by the transformation scripts. Before
Phase 0 the rebuild deleted and re-inserted rows, so the ``SERIAL`` sequence
kept advancing and every logical record received a *different* numeric id on
every run. Files written by an earlier run still contained the old numbers, so
the embedding stage looked up rows that no longer existed, updated nothing, and
still reported success. PostgreSQL and Qdrant diverged silently.

The identifiers below are derived only from *business* identity, so they
survive table rebuilds, sequence drift, and full re-imports.

Formats
-------
event     ``event:{source_system}:{source_entity}:{source_record_key}``
case      ``case:{process_type}:{normalized_case_id}``
document  ``document:{document_id}``
qdrant    ``uuid5(NAMESPACE_URL, "bpi2020/{record_id}")``
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Mapping

# Logical system that produced the records. Part of the event identifier so a
# later phase can add a second source system without colliding with BPI data.
SOURCE_SYSTEM = "bpi2020"

# Bumped when the *shape* of a unified record changes in a way consumers must
# notice. It is deliberately not part of content_hash: re-versioning the schema
# should not by itself invalidate every stored embedding.
UNIFIED_SCHEMA_VERSION = "1.0.0"

# Namespace prefix for Qdrant point IDs. Kept identical to the pre-Phase-0
# uploader so the derivation rule itself is unchanged; only the *input* moves
# from a SERIAL to a stable record_id.
_QDRANT_ID_NAMESPACE_PREFIX = "bpi2020"

_UNSAFE_CHARS = re.compile(r"[^a-z0-9_.\-]")
_REPEATED_UNDERSCORE = re.compile(r"_+")
_WHITESPACE = re.compile(r"\s+")


class StableIdError(ValueError):
    """Raised when a stable identifier cannot be built from the given input."""


def normalize_key_component(value: Any) -> str:
    """Normalize one component of a stable identifier.

    Lower-cases, collapses whitespace to ``_`` and replaces any character that
    is not ``[a-z0-9_.-]``. BPI case identifiers look like
    ``"declaration 100000"`` or ``"travel permit 76455"``, which become
    ``declaration_100000`` and ``travel_permit_76455``.

    The result is safe to embed in a colon-delimited identifier, a filename, a
    URL, and a Qdrant payload filter.
    """
    if value is None:
        raise StableIdError("Cannot build a stable identifier from None.")

    text = str(value).strip().lower()

    if not text:
        raise StableIdError("Cannot build a stable identifier from an empty value.")

    text = _WHITESPACE.sub("_", text)
    text = _UNSAFE_CHARS.sub("_", text)
    text = _REPEATED_UNDERSCORE.sub("_", text)
    text = text.strip("_")

    if not text:
        raise StableIdError(
            f"Value {value!r} normalized to an empty stable-identifier component."
        )

    return text


def make_event_record_id(
    source_entity: str,
    source_record_key: Any,
    source_system: str = SOURCE_SYSTEM,
) -> str:
    """Build the stable identifier for one cleaned event row.

    ``source_entity`` is the legacy ERP table the row came from, e.g.
    ``domestic_declarations_raw``. ``source_record_key`` is that table's
    ``source_row_id``, which the CSV importer assigns deterministically from
    the CSV row order, so it is reproducible across re-imports.

    ``(source_entity, source_record_key)`` was verified unique across all
    270,211 cleaned events.
    """
    return ":".join(
        (
            "event",
            normalize_key_component(source_system),
            normalize_key_component(source_entity),
            normalize_key_component(source_record_key),
        )
    )


def make_case_record_id(process_type: Any, case_id: Any) -> str:
    """Build the stable identifier for one AI-ready ERP case.

    ``(process_type, case_id)`` is the natural key of a BPI case and was
    verified unique across all 32,999 cases with zero normalization
    collisions.
    """
    return ":".join(
        (
            "case",
            normalize_key_component(process_type),
            normalize_key_component(case_id),
        )
    )


def make_document_record_id(document_id: Any) -> str:
    """Build the stable identifier for one AI-ready document.

    ``document_id`` is already content-derived (SHA-256 over the file name plus
    the file bytes), so the same file always yields the same identifier and a
    changed file yields a new one.
    """
    return f"document:{normalize_key_component(document_id)}"


def make_qdrant_point_id(record_id: str) -> str:
    """Derive the deterministic Qdrant point ID for a stable record_id.

    Qdrant accepts unsigned integers or UUIDs as point IDs, so a stable string
    key is folded into a UUIDv5. The mapping is pure:

    * same logical record   -> same point ID (an upsert updates in place)
    * changed content       -> same point ID, new vector and payload
    * different record      -> different point ID

    Never pass a PostgreSQL SERIAL to this function.
    """
    if not record_id or not str(record_id).strip():
        raise StableIdError("Cannot derive a Qdrant point ID from an empty record_id.")

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{_QDRANT_ID_NAMESPACE_PREFIX}/{record_id}",
        )
    )


def compute_content_hash(
    record_id: str,
    text_for_ai: str,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Compute a deterministic SHA-256 hash of the AI-facing content.

    The hash covers record identity, the exact text handed to the embedding
    model, and the normalized metadata that ends up in the Qdrant payload.

    Callers must NOT include volatile pipeline bookkeeping (``embedding_status``,
    ``qdrant_point_id``, ``created_at``) in ``metadata`` - those change without
    the AI content changing and would defeat change detection.

    Phase 0 only establishes the hash. Incremental re-embedding driven by it is
    a later phase.
    """
    payload = {
        "record_id": record_id,
        "text_for_ai": text_for_ai or "",
        "metadata": _normalize_for_hash(metadata or {}),
    }

    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def _normalize_for_hash(value: Any) -> Any:
    """Recursively drop None values so absent and null metadata hash alike."""
    if isinstance(value, Mapping):
        return {
            key: _normalize_for_hash(item)
            for key, item in sorted(value.items())
            if item is not None
        }

    if isinstance(value, (list, tuple)):
        return [_normalize_for_hash(item) for item in value]

    return value
