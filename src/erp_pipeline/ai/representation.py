"""Projecting canonical records into AI-ready representations.

    CanonicalRecord ──┬──> deterministic text  ──┐
                      └──> structured payload ───┴──> AIRepresentation
                                                          │
                                                    content_hash

WHY A PROJECTION AND NOT A DUMP
-------------------------------
``str(record)`` or ``json.dumps(normalized_data)`` would both "work" and both
would be wrong. A Python repr embeds type names and memory-layout artefacts; a
raw JSON dump embeds operational bookkeeping alongside business facts. Either
makes the vector depend on things that carry no meaning, so an engine-version
bump would look like a content change and re-embed the corpus.

This module builds text from BUSINESS content only, in a fixed order, with
stable formatting - and keeps the structured payload beside it rather than
flattening everything into one string (Step 7).

DETERMINISM
-----------
Field order is ALPHABETICAL, not insertion order. Two canonical records with
identical content but built by different code paths would otherwise produce
different text and therefore different hashes. Sorted order is less pretty and
completely reproducible, which is the correct trade here.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.ai.hashing import representation_content_hash
from erp_pipeline.ai.models import (
    RepresentationConfig,
    make_representation_id,
)
from erp_pipeline.schemas.canonical_models import CanonicalRecord
from erp_pipeline.schemas.enums import ContentKind
from erp_pipeline.transformation.source_native import (
    BUSINESS_KEY_NAME,
    BUSINESS_KEY_VALUE,
)
from erp_pipeline.sync.propagation import AIRepresentation


def humanize(name: str) -> str:
    """``invoice_id`` -> ``Invoice Id``, ``contact.email`` -> ``Contact Email``."""
    parts = str(name).replace(".", " ").replace("_", " ").split()

    return " ".join(part[:1].upper() + part[1:] for part in parts if part)


def format_value(value: Any) -> str | None:
    """Render one value deterministically, or ``None`` to omit it.

    ``Decimal`` prints exactly - never through ``float`` - so an amount in the
    embedding text reads the same as the amount in the record. Datetimes are
    normalized to UTC ISO form so the same instant always renders identically.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, datetime):
        from datetime import timezone

        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, str):
        text = value.strip()
        return text or None

    if isinstance(value, (list, tuple)):
        rendered = [format_value(item) for item in value]
        kept = [item for item in rendered if item]
        return ", ".join(kept) if kept else None

    if isinstance(value, Mapping):
        # Handled by flattening before this point; reaching here means a
        # mapping nested inside a list, which is rendered compactly.
        parts = [
            f"{humanize(key)}: {format_value(item)}"
            for key, item in sorted(value.items())
            if format_value(item) is not None
        ]
        return "; ".join(parts) if parts else None

    return str(value)


def flatten(
    payload: Mapping[str, Any],
    config: RepresentationConfig,
    prefix: str = "",
) -> list[tuple[str, Any]]:
    """Flatten nested business content into ``(dotted_path, value)`` pairs.

    Sorted at every level, so nesting order cannot vary between two records
    with the same content. Operational keys are dropped here rather than later,
    so they influence neither the text nor the hash.
    """
    flattened: list[tuple[str, Any]] = []

    for key in sorted(payload):
        if str(key).lower() in config.operational_keys:
            continue

        value = payload[key]
        path = f"{prefix}{key}"

        if isinstance(value, Mapping):
            flattened.extend(flatten(value, config, prefix=f"{path}."))
        else:
            flattened.append((path, value))

    return flattened


def build_text(
    entity_type: str,
    payload: Mapping[str, Any],
    config: RepresentationConfig | None = None,
    source_context: Mapping[str, Any] | None = None,
) -> str:
    """Build the deterministic ERP-oriented text for one record (Step 29).

    Generic over the canonical model: nothing here knows what an invoice is.
    Whatever fields the record carries become labelled lines, which is why the
    same builder serves invoices, customers, purchase orders and anything a
    later mapping profile introduces.
    """
    config = config or RepresentationConfig()
    lines: list[str] = []

    if config.include_entity_header:
        lines.append(f"Entity: {humanize(entity_type)}")

    if config.include_source_context and source_context:
        for key in sorted(source_context):
            rendered = format_value(source_context[key])
            if rendered:
                lines.append(f"{humanize(key)}: {rendered}")

    for path, value in flatten(payload, config):
        rendered = format_value(value)
        if rendered is None:
            # A null field adds no meaning and would make two records differ
            # only by absence-versus-null, which is not a semantic difference.
            continue

        label = humanize(path) if config.humanize_field_names else path
        lines.append(f"{label}: {rendered}")

    text = "\n".join(lines)

    if len(text) > config.max_characters:
        # Bounded explicitly rather than left to the model's silent truncation
        # (Step 28). The marker makes the bounding visible in the text itself.
        marker = "\n[content truncated]"
        text = text[: config.max_characters - len(marker)] + marker

    return text


def canonical_record_to_representation(
    record: CanonicalRecord,
    config: RepresentationConfig | None = None,
) -> AIRepresentation:
    """Project a Phase 1 ``CanonicalRecord`` into an AI-ready representation.

    Reuses Phase 10's ``AIRepresentation`` rather than defining a competing
    model, so the result drops straight into the incremental cascade and shares
    its content-hash semantics.

    Identity is derived from the canonical record id, so the same record always
    projects to the same representation - and the projection carries the
    canonical id forward for traceability.
    """
    config = config or RepresentationConfig()

    source_context: dict[str, Any] = {}
    if config.include_source_context:
        source_context = {
            "source_system": record.source.source_system_id,
            "source_entity": record.source.source_entity,
        }

    text = build_text(
        record.entity_type,
        record.normalized_data,
        config,
        source_context=source_context,
    )

    structured = {
        key: value
        for key, value in record.normalized_data.items()
        if str(key).lower() not in config.operational_keys
    }

    representation_id = make_representation_id(
        record.entity_type, record.record_id
    )
    # Dynamic filter attributes are schema/catalog-driven, never a blind dump
    # of a record's fields: only a caller that actually knows the discovered
    # schema (``SourceNativeTransformer``, which excludes anything the
    # schema marked non-filterable) is in a position to declare them. A
    # record built any other way - like a plain ``CanonicalRecord.from_source``
    # with no schema behind it - carries none, rather than this function
    # guessing which of its normalized_data fields are safe to expose as
    # filters. Guessing is exactly how an amount or any other arbitrary
    # business value used to reach the Qdrant payload.
    declared_filters = (record.metadata or {}).get("filter_attributes")
    filter_attributes = (
        dict(declared_filters) if isinstance(declared_filters, Mapping) else {}
    )

    return AIRepresentation(
        representation_id=representation_id,
        entity_type=record.entity_type,
        text_for_ai=text,
        content=structured,
        source_record_ids=(record.record_id,),
        metadata={
            # Structural provenance only - enough for Phase 12 to route on and
            # for a reader to trace a vector back, with no business values.
            "content_kind": ContentKind.STRUCTURED_RECORD.value,
            "canonical_record_id": record.record_id,
            "source_system_id": record.source.source_system_id,
            "source_type": record.source.source_type.value,
            "source_entity": record.source.source_entity,
            "record_key": record.source.source_record_key,
            "filter_attributes": filter_attributes,
            "sensitivity": record.sensitivity.value,
            "representation_config": config.fingerprint(),
            **_business_identity(record),
        },
        content_hash=representation_content_hash(
            representation_id, text_for_ai=text, content=structured
        ),
    )


def _business_identity(record: CanonicalRecord) -> dict[str, Any]:
    """The generic business key a record already carries, if it carries one.

    Phase 2 records this for source-native entities: ``employee_id`` / ``EMP002``,
    or a composite ``warehouse_id|product_id`` / ``WH-1|P-77``. Phase 4 only has
    to stop discarding it.

    Records from the CANONICAL mapping path have no such metadata, and none is
    invented for them. Their business key would have to be guessed from which
    canonical field looks key-like, and a filter matching a guessed identity is
    worse than a filter that returns nothing - the caller cannot tell the
    difference between "no match" and "wrong match".
    """
    metadata = getattr(record, "metadata", None) or {}

    return {
        key: metadata[key]
        for key in (BUSINESS_KEY_NAME, BUSINESS_KEY_VALUE)
        if metadata.get(key) is not None
    }


def canonical_records_to_representations(
    records: Iterable[CanonicalRecord],
    config: RepresentationConfig | None = None,
) -> Iterable[AIRepresentation]:
    """Lazily project many records, so a large corpus is never materialized."""
    for record in records:
        yield canonical_record_to_representation(record, config)


__all__ = [
    "humanize",
    "format_value",
    "flatten",
    "build_text",
    "canonical_record_to_representation",
    "canonical_records_to_representations",
]
