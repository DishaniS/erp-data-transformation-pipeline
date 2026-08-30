"""Schema-driven employee attributes that are safe to use as exact filters.

Business field names are never enumerated here. They come from the discovered
``SourceSchema`` and from each canonical record's normalized data, so adding a
column such as ``cost_center`` requires re-ingestion but no backend code change.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from typing import Any


# Technical payload keys are owned by the pipeline and cannot be overwritten by
# a source column with the same name. This is a collision boundary, not a list
# of employee business fields.
RESERVED_PAYLOAD_FIELDS = frozenset(
    {
        "representation_id",
        "embedding_id",
        "vector_id",
        "content_hash",
        "model_id",
        "dimension",
        "entity_type",
        "sensitivity",
        "canonical_record_id",
        "source_system_id",
        "source_entity",
        "record_key",
        "document_id",
        "content_kind",
        "parent_record_id",
        "source_field",
        "business_key_name",
        "business_key_value",
        "document_type",
        "schema_name",
        "entity_kind",
        "schema_id",
        "schema_version",
        "entity_id",
        "schema_chunk_index",
        "logical_key",
        "page_start",
        "page_end",
        "chunk_index",
        "filter_attributes",
    }
)


def render_filter_value(value: Any) -> str:
    """The ONE normalization a filter value goes through - ingestion or search.

    Used identically by ingestion (deciding what a dynamic field's value
    "is", before it is tokenized into a Qdrant payload) and by
    ``GET /v1/search`` (deciding what a caller's filter value "is", before
    it is tokenized to compare). Two separate renderings here - as this
    module and ``storage.filters`` briefly each had their own - would let a
    boolean or an enum normalize one way at ingestion and another at search,
    silently breaking every dynamic filter on that field. ``storage.filters``
    imports this function rather than keeping its own copy.

    Enums compare by their wire value; a boolean renders as the lowercase
    spelling a caller actually types in a query string (``true``/``false``),
    not Python's ``str(bool)``; everything else is ``str().strip()``.
    """
    if value is None:
        return ""

    inner = getattr(value, "value", value)

    if isinstance(inner, bool):
        return "true" if inner else "false"

    return str(inner).strip()


def filter_value_token(
    secret: str,
    *,
    source_system_id: str,
    source_entity: str,
    field_name: str,
    value: Any,
) -> str:
    """A deterministic, KEYED token standing in for one dynamic filter value.

    HMAC-SHA256, never an unkeyed hash: an unkeyed digest of ``"Finance"`` is
    crackable by anyone who can enumerate plausible department names (a
    dictionary attack), which would defeat the entire point of not storing
    the value itself. Keying it with a secret nobody outside this process
    holds is what makes the token one-way in practice, not merely in
    appearance.

    Scoped by ``(source_system_id, source_entity, field_name)``: the SAME
    business value in a different system, entity or field produces a
    DIFFERENT token, so two payloads sharing a token can only mean they
    share the SAME value in the SAME scope - never a coincidental collision
    across unrelated fields, and never something a caller could exploit to
    correlate two records across scopes.

    Called identically at ingestion (tokenizing what actually gets written
    into the Qdrant payload) and at search time (tokenizing a caller's
    filter value before it is compared) - both go through
    :func:`render_filter_value` first, so the same value always normalizes
    to the same token regardless of which side computed it.
    """
    material = "\x1f".join(
        (
            render_filter_value(source_system_id),
            render_filter_value(source_entity),
            render_filter_value(field_name),
            render_filter_value(value),
        )
    )

    return hmac.new(
        secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def normalize_filter_attributes(
    values: Mapping[str, Any] | None,
    *,
    excluded_fields: Sequence[str] = (),
) -> dict[str, Any]:
    """Flatten arbitrary normalized record data into Qdrant keyword values.

    Values are represented as strings (or arrays of strings), matching the
    keyword indexes and URL query-parameter semantics. Binary/object values are
    omitted rather than serialized into vector metadata.
    """
    normalized: dict[str, Any] = {}
    excluded = set(excluded_fields)

    def visit(prefix: str, value: Any) -> None:
        root = prefix.split(".", 1)[0]
        if (
            not prefix
            or root in excluded
            or root in RESERVED_PAYLOAD_FIELDS
            or value is None
        ):
            return
        if isinstance(value, (bytes, bytearray, memoryview)):
            return
        if isinstance(value, Mapping):
            for child, child_value in value.items():
                name = f"{prefix}.{child}" if prefix else str(child)
                visit(name, child_value)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            scalar = [
                _keyword(item)
                for item in value
                if item is not None
                and not isinstance(item, Mapping)
                and not (
                    isinstance(item, Sequence)
                    and not isinstance(item, (str, bytes, bytearray))
                )
            ]
            if scalar:
                normalized[prefix] = scalar
            return

        normalized[prefix] = _keyword(value)

    for key, value in (values or {}).items():
        visit(str(key), value)

    return normalized


def schema_filter_fields(
    schemas: Sequence[Any],
    *,
    source_system_id: str | None = None,
    source_entity: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Describe filterable fields from current discovered schema objects."""
    catalog: dict[str, dict[str, Any]] = {}

    for schema in schemas:
        system = getattr(schema, "source_system_id", None)
        if source_system_id and system != source_system_id:
            continue

        for entity in getattr(schema, "entities", ()) or ():
            qualified = _qualified_entity(entity)
            aliases = {
                qualified,
                getattr(entity, "source_name", None),
                getattr(entity, "normalized_name", None),
                getattr(entity, "entity_id", None),
            }
            if source_entity and source_entity not in aliases:
                continue

            for field in getattr(entity, "fields", ()) or ():
                name = str(getattr(field, "normalized_name", "") or "")
                if not name or name in RESERVED_PAYLOAD_FIELDS:
                    continue
                metadata = dict(getattr(field, "metadata", None) or {})
                if metadata.get("filterable") is False:
                    continue
                data_type = getattr(getattr(field, "normalized_data_type", None), "value", None)
                if data_type == "binary":
                    continue

                item = catalog.setdefault(
                    name,
                    {
                        "name": name,
                        "description": getattr(field, "description", None)
                        or f"Exact match on the current {name} employee attribute.",
                        "data_type": data_type or "string",
                        "dynamic": True,
                        "source_system_ids": set(),
                        "source_entities": set(),
                    },
                )
                if system:
                    item["source_system_ids"].add(system)
                item["source_entities"].add(qualified)

    for item in catalog.values():
        item["source_system_ids"] = sorted(item["source_system_ids"])
        item["source_entities"] = sorted(item["source_entities"])

    return catalog


def available_search_catalog(
    schemas: Sequence[Any],
    *,
    source_system_id: str | None = None,
    source_entity: str | None = None,
    indexed_fields: set[str] | None = None,
    search_capable: bool = True,
) -> list[dict[str, Any]]:
    """One entry per discovered ``(source_system_id, source_entity)``.

    This is what ``GET /v1/search`` returns when called with no query - the
    live, schema-driven answer to "what can I search and filter on right
    now". Field selection mirrors :func:`schema_filter_fields` exactly (same
    exclusions: reserved payload keys, ``metadata["filterable"] is False``,
    binary columns) so the two never drift into disagreeing about what is
    filterable.

    ``indexed_fields`` gates each field's ``filterable`` flag the same way
    ``get_search_schema`` used to: when the live Qdrant payload schema is
    inspectable, a field not yet indexed is still listed (it is a real,
    discovered column) but reported as not currently filterable.

    ``example_value`` is populated ONLY from a field's declared enum
    constants (a contract fact, not sampled data - the same boundary
    ``schema_conversion._field_metadata`` already draws for OpenAPI
    ``example``/``examples``, which are recorded as a presence flag and
    never as a value). No business value from an actual record is ever
    surfaced here.
    """
    groups: dict[tuple[str | None, str], dict[str, Any]] = {}

    for schema in schemas:
        system = getattr(schema, "source_system_id", None)
        if source_system_id and system != source_system_id:
            continue

        for entity in getattr(schema, "entities", ()) or ():
            qualified = _qualified_entity(entity)
            aliases = {
                qualified,
                getattr(entity, "source_name", None),
                getattr(entity, "normalized_name", None),
                getattr(entity, "entity_id", None),
            }
            if source_entity and source_entity not in aliases:
                continue

            key = (system, qualified)
            group = groups.setdefault(
                key,
                {
                    "source_system_id": system,
                    "source_entity": qualified,
                    "entity_kind": getattr(
                        getattr(entity, "entity_kind", None), "value", None
                    ),
                    "description": getattr(entity, "description", None),
                    "fields": {},
                },
            )

            primary_keys = set(getattr(entity, "primary_key_fields", ()) or ())

            for field in getattr(entity, "fields", ()) or ():
                name = str(getattr(field, "normalized_name", "") or "")
                if not name or name in RESERVED_PAYLOAD_FIELDS:
                    continue
                metadata = dict(getattr(field, "metadata", None) or {})
                if metadata.get("filterable") is False:
                    continue
                data_type = getattr(
                    getattr(field, "normalized_data_type", None), "value", None
                )
                if data_type == "binary":
                    continue

                group["fields"][name] = {
                    "name": name,
                    "type": (data_type or "unknown").upper(),
                    "business_key": bool(
                        getattr(field, "is_primary_key", False)
                        or name in primary_keys
                    ),
                    "filterable": (
                        indexed_fields is None or name in indexed_fields
                    ),
                    "description": getattr(field, "description", None)
                    or f"Exact match on the current {name} attribute.",
                    "example_value": _declared_example(metadata),
                }

    catalog = []

    for (system, qualified), group in groups.items():
        fields = sorted(group["fields"].values(), key=lambda item: item["name"])
        catalog.append(
            {
                "source_system_id": system,
                "source_entity": qualified,
                "entity_kind": group["entity_kind"],
                "description": group["description"],
                "searchable": bool(search_capable and fields),
                "fields": fields,
            }
        )

    catalog.sort(key=lambda item: (item["source_system_id"] or "", item["source_entity"]))

    return catalog


def _declared_example(metadata: Mapping[str, Any]) -> str | None:
    """A safe example value, or ``None``.

    Sourced ONLY from a closed enum a schema declares as a constraint - never
    from sampled business data. Every other field is honestly ``None`` rather
    than fabricated: nothing in this pipeline's discovered-schema metadata
    retains an actual field value (Mongo inference keeps only type/null
    statistics; OpenAPI parsing records an ``example`` as a presence flag,
    never the value itself), so there is no safe source to draw one from.
    """
    values = metadata.get("enum")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and values:
        return _keyword(values[0])

    return None


def _qualified_entity(entity: Any) -> str:
    namespace = getattr(entity, "namespace", None)
    source_name = str(getattr(entity, "source_name", "") or "")
    return f"{namespace}.{source_name}" if namespace else source_name


#: Kept as a local name so existing call sites in this module are unchanged;
#: identical to ``render_filter_value`` except it never sees ``None`` (its
#: callers already guard for that), so the two behave the same either way.
_keyword = render_filter_value


__all__ = [
    "RESERVED_PAYLOAD_FIELDS",
    "normalize_filter_attributes",
    "schema_filter_fields",
    "available_search_catalog",
]
