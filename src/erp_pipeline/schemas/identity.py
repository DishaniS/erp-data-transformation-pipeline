"""Deterministic identity rules for the canonical ERP model.

The rules, and why each one exists
----------------------------------
    deterministic          the same logical record always yields the same id
    source-independent     no engine-specific concept appears in the id
    no database SERIALs    identity never comes from an auto-increment column
    safely normalized      ids are safe in files, URLs and payload filters
    reproducible           reruns and rebuilds produce identical ids

The third rule is the one learned the hard way. Identity taken from a
PostgreSQL ``SERIAL`` changes whenever its table is rebuilt, which silently
re-identifies every derived record and orphans every stored vector. The symptom
appears long after the cause, so ``looks_like_surrogate_key`` and
``require_business_key`` refuse it at the point of construction instead.

``normalize_identifier`` is pinned byte-for-byte by a frozen corpus in
``tests/erp_pipeline/test_identity_and_serialization.py``. Changing it
re-identifies every stored record, so it is a MAJOR contract change and must
never happen by accident.

Canonical record id format
--------------------------
    erp:{source_system_id}:{entity_type}:{stable_source_key}

Example:
    erp:finance_erp_pg:invoice:inv-001

The ``source_system_id`` component is what keeps two ERP systems apart. Invoice
``1001`` in ERP A and invoice ``1001`` in ERP B produce different canonical
ids, because a business key is only unique *within* the system that issued it.

Every component is normalized, and normalization removes ``:``, so the four
components can always be recovered unambiguously (see ``parse_canonical_id``).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Mapping

# Leading token of every generic canonical identifier. Distinct from the
# Phase 0 prefixes so the two id spaces cannot overlap.
CANONICAL_ID_PREFIX = "erp"

# Component separator. Normalization guarantees no component can contain it.
CANONICAL_ID_SEPARATOR = ":"

# Conventional entity_type slot used by CanonicalDocument, so structured
# records and documents share one id grammar.
DOCUMENT_ENTITY_TYPE = "document"

# Namespace prefix for derived UUIDv5 values handed to external systems.
# Two deployments that shared a vector store would collide on the same record
# id without it, so the prefix is part of the derivation rather than cosmetic.
UUID_NAMESPACE_PREFIX = "erp_pipeline"

_UNSAFE_CHARS = re.compile(r"[^a-z0-9_.\-]")
_REPEATED_UNDERSCORE = re.compile(r"_+")
_WHITESPACE = re.compile(r"\s+")

# A value that is nothing but digits. This is the ONE shape that is
# unambiguously a database surrogate key rather than a business key, so it is
# the only shape flagged. Anything broader produces false positives on real ERP
# keys ("INV-001", "declaration 100000"), which would make the guard unusable.
_SURROGATE_KEY = re.compile(r"^\d+$")


class IdentityError(ValueError):
    """Raised when a deterministic identifier cannot be built or parsed."""


def normalize_identifier(value: Any) -> str:
    """Normalize one component of a deterministic identifier.

    Lower-cases, collapses whitespace to ``_``, replaces every character
    outside ``[a-z0-9_.-]`` with ``_``, collapses repeated underscores and
    trims leading/trailing underscores.

    Examples:
        ``"Invoice Number"``  -> ``"invoice_number"``
        ``"INV-001"``          -> ``"inv-001"``
        ``"_id"``              -> ``"id"``
        ``"fin.invoice"``      -> ``"fin.invoice"``

    Byte-for-byte identical to Phase 0's ``normalize_key_component``. Changing
    this function re-identifies every stored record, so it is a MAJOR contract
    change (see ``erp_pipeline.version``).
    """
    if value is None:
        raise IdentityError("Cannot build an identifier component from None.")

    if isinstance(value, bool):
        # bool is an int subclass; "true"/"false" is almost never the intent
        # and silently accepting it hides a mapping bug.
        raise IdentityError(
            "Cannot build an identifier component from a boolean value."
        )

    text = str(value).strip().lower()

    if not text:
        raise IdentityError("Cannot build an identifier component from an empty value.")

    text = _WHITESPACE.sub("_", text)
    text = _UNSAFE_CHARS.sub("_", text)
    text = _REPEATED_UNDERSCORE.sub("_", text)
    text = text.strip("_")

    if not text:
        raise IdentityError(
            f"Value {value!r} normalized to an empty identifier component."
        )

    return text


def is_normalized_identifier(value: object) -> bool:
    """True when ``value`` is already a fully normalized component.

    Defined as a fixed point of ``normalize_identifier``: a value is normalized
    exactly when normalizing it changes nothing. That makes the validator and
    the generator provably consistent - there is no second pattern to keep in
    sync with the normalization rule.
    """
    if not isinstance(value, str) or not value:
        return False

    try:
        return normalize_identifier(value) == value
    except IdentityError:
        return False


def looks_like_surrogate_key(value: object) -> bool:
    """True when ``value`` is a bare integer, and therefore not a business key.

    A value consisting only of digits (``4471``, ``"4471"``) is the classic
    symptom of identity taken from a PostgreSQL ``SERIAL`` or a row offset. Such
    a value changes whenever the source table is rebuilt, which silently
    re-identifies every derived record and orphans every stored vector.

    DELIBERATELY NARROW. Only the digits-only shape is flagged, because it is
    the only shape that is unambiguous. Real ERP business keys frequently
    contain digits (``"INV-001"``, ``"declaration 100000"``, ``"cus-44"``) and
    flagging those would make the guard unusable on real data.

    Composed identifiers such as the legacy ``case_4471`` are detected by
    applying this check to the *stable-key component* of a parsed canonical id
    rather than by widening the pattern here - see
    ``erp_pipeline.verification.record_integrity.check_record_identity``.
    """
    if isinstance(value, bool):
        return False

    if isinstance(value, int):
        return True

    if not isinstance(value, str):
        return False

    return bool(_SURROGATE_KEY.match(value.strip()))


def require_business_key(value: Any, context: str = "identifier") -> Any:
    """Return ``value``, or refuse it when it looks like a surrogate key.

    The invariant this enforces was learned the hard way in the BPI prototype:
    vector identity derived from a ``SERIAL`` silently re-identified every
    record whenever the source table was rebuilt, orphaning every stored
    vector. Refusing loudly at the point of construction is far cheaper than
    discovering it after an embedding run.

    Call this wherever an identifier crosses a layer boundary - into a
    canonical id, a representation id, or a vector point id.
    """
    if looks_like_surrogate_key(value):
        raise IdentityError(
            f"{context} {value!r} is a bare integer, which is a database "
            "surrogate key rather than a business key. Identity that comes "
            "from an auto-increment column changes whenever the source is "
            "reloaded, which orphans every derived record and vector. Use the "
            "record's own business key instead."
        )

    return value


def make_canonical_record_id(
    source_system_id: str,
    entity_type: str,
    stable_source_key: Any,
) -> str:
    """Build the canonical id for one normalized ERP record.

    ``stable_source_key`` must be the record's key *in the source system* - a
    business key such as an invoice number, or a natural key the source
    guarantees is stable. It must never be a PostgreSQL SERIAL, a row offset,
    or any value that changes when the source is reloaded.
    """
    return CANONICAL_ID_SEPARATOR.join(
        (
            CANONICAL_ID_PREFIX,
            normalize_identifier(source_system_id),
            normalize_identifier(entity_type),
            normalize_identifier(stable_source_key),
        )
    )


def make_canonical_document_id(source_system_id: str, document_id: Any) -> str:
    """Build the canonical id for one unstructured document.

    Uses the same four-component grammar with ``document`` in the entity-type
    slot. ``document_id`` should itself be content-derived (for example a
    SHA-256 over the file bytes) so that the same file always yields the same
    canonical id and an edited file yields a new one.
    """
    return make_canonical_record_id(
        source_system_id=source_system_id,
        entity_type=DOCUMENT_ENTITY_TYPE,
        stable_source_key=document_id,
    )


def parse_canonical_id(record_id: str) -> tuple[str, str, str]:
    """Recover ``(source_system_id, entity_type, stable_source_key)``.

    Proves the id grammar is unambiguous: because every component is
    normalized and normalization removes ``:``, splitting can never be
    confused by a component that happens to contain the separator.
    """
    if not isinstance(record_id, str) or not record_id:
        raise IdentityError("Cannot parse an empty canonical id.")

    parts = record_id.split(CANONICAL_ID_SEPARATOR)

    if len(parts) != 4 or parts[0] != CANONICAL_ID_PREFIX:
        raise IdentityError(
            f"{record_id!r} is not a canonical id. Expected "
            f"'{CANONICAL_ID_PREFIX}:{{source_system_id}}:{{entity_type}}:"
            "{stable_source_key}'."
        )

    if not all(parts[1:]):
        raise IdentityError(f"{record_id!r} has an empty identifier component.")

    return parts[1], parts[2], parts[3]


def make_deterministic_uuid(
    record_id: str,
    namespace_prefix: str = UUID_NAMESPACE_PREFIX,
) -> str:
    """Fold a canonical id into a deterministic UUIDv5.

    Some external systems - vector stores in particular - accept only integers
    or UUIDs as point identifiers. This provides one without introducing
    randomness:

        same record   -> same UUID (an upsert updates in place)
        changed content -> same UUID, new payload
        different record -> different UUID

    The human-readable ``record_id`` remains the authoritative identity. This
    value is a derived projection for a specific consumer, and nothing should
    resolve a record by it alone.
    """
    if not isinstance(record_id, str) or not record_id.strip():
        raise IdentityError("Cannot derive a UUID from an empty record id.")

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace_prefix}/{record_id}"))


def compute_content_hash(
    record_id: str,
    content: Mapping[str, Any] | None = None,
    text_for_ai: str | None = None,
) -> str:
    """Compute a deterministic SHA-256 over a record's meaningful content.

    Covers identity, the normalized business content, and the text handed to an
    embedding model. Two records with the same hash carry the same AI-relevant
    content, which is what future incremental re-processing will rely on.

    Callers must exclude volatile bookkeeping (timestamps, run ids, processing
    status). Those change without the content changing and would defeat change
    detection.

    Properties are identical to Phase 0's ``compute_content_hash``: sorted
    keys, compact separators, ``None`` values dropped so absent and null hash
    alike, SHA-256 hex output. The digests are not interchangeable between the
    two packages, and are not meant to be - they hash different envelopes at
    different layers.
    """
    payload = {
        "record_id": record_id,
        "text_for_ai": text_for_ai or "",
        "content": _strip_none(content or {}),
    }

    return hash_json_payload(payload)


def hash_json_payload(payload: Any) -> str:
    """SHA-256 of a JSON-serializable payload, in a canonical encoding.

    Key order, whitespace and non-ASCII handling are all pinned so the digest
    depends only on content, never on how the mapping was constructed.
    """
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def _strip_none(value: Any) -> Any:
    """Recursively drop ``None`` values so absent and null hash identically."""
    if isinstance(value, Mapping):
        return {
            key: _strip_none(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if item is not None
        }

    if isinstance(value, (list, tuple)):
        return [_strip_none(item) for item in value]

    return value


__all__ = [
    "CANONICAL_ID_PREFIX",
    "CANONICAL_ID_SEPARATOR",
    "DOCUMENT_ENTITY_TYPE",
    "UUID_NAMESPACE_PREFIX",
    "IdentityError",
    "normalize_identifier",
    "is_normalized_identifier",
    "looks_like_surrogate_key",
    "require_business_key",
    "make_canonical_record_id",
    "make_canonical_document_id",
    "parse_canonical_id",
    "make_deterministic_uuid",
    "compute_content_hash",
    "hash_json_payload",
]
