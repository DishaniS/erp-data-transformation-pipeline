"""Turn a raw ERP JSON response into canonical ERP data.

WHAT THIS MODULE DOES *NOT* DO
------------------------------
It does not match schemas, score field candidates, convert types, or validate
values. All four already exist and are the most heavily tested code in the
repository:

    api_specs.inference        JSON payload  -> observed SourceField structure
    mapping.MappingService     SourceSchema  -> MappingProfile (ERP-aware)
    transformation.Transform…  SourceRecord  -> CanonicalRecord

Writing a second mapping engine here - under time pressure, for one more input
format - would fork the ERP knowledge that makes the whole component
"ERP-aware" in the first place. The point of this phase is that an API
response is just another source the SAME engine can absorb.

WHAT IT ADDS
------------
Two things the existing chain genuinely cannot do:

1. **Envelope unwrapping.** ``{"result": {...}, "success": true}`` is not an
   ERP entity called "result". The business record is one level down, and
   which level is a STRUCTURAL question no schema declares.
2. **A synthetic schema per response.** A database source has a catalogued
   schema; a live API response does not. One is inferred from the payload
   itself, marked as observed, and thrown away afterwards.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from erp_pipeline.api_specs.inference import infer_structure_from_examples
from erp_pipeline.api_specs.models import ApiSpecOptions
from erp_pipeline.mapping.models import FieldOutcome
from erp_pipeline.mapping.service import MappingService
from erp_pipeline.response_adaptation.errors import (
    MalformedResponseError,
    MappingUnavailableError,
)
from erp_pipeline.schemas.enums import EntityKind, SchemaOrigin, SourceType
from erp_pipeline.schemas.identity import hash_json_payload, normalize_identifier
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.service import TransformationService
from erp_pipeline.transformation.transformer import TransformationContext

#: How deep envelope unwrapping will look before giving up. Three covers every
#: real wrapper shape seen (``data.result.item``); deeper nesting is business
#: structure, not an envelope.
MAX_UNWRAP_DEPTH = 3

#: Keys whose presence alongside a single nested object marks the parent as an
#: envelope rather than a record. Used as CORROBORATION only - the structural
#: rule below decides on its own, and this list only breaks ties. It is not a
#: closed list of wrapper names, because vendors invent their own.
_ENVELOPE_HINT_KEYS = frozenset(
    {
        "success", "status", "ok", "error", "errors", "message", "code",
        "timestamp", "server_time", "servertime", "request_id", "requestid",
        "took", "elapsed", "count", "total", "page", "page_size", "offset",
        "limit", "has_more", "next", "previous", "links", "meta", "_links",
    }
)


def _is_record(value: Any) -> bool:
    """A mapping that could itself be a business record."""
    return isinstance(value, Mapping) and bool(value)


def _is_record_list(value: Any) -> bool:
    """A non-empty sequence whose first element is a mapping."""
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and isinstance(value[0], Mapping)
    )


def unwrap_payload(
    body: Any, max_depth: int = MAX_UNWRAP_DEPTH
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Find the business records inside a response envelope.

    Returns ``(records, wrapper_path)``. The path is reported so provenance
    can state where in the response the data was found, rather than silently
    presenting a nested object as if it had been the whole body.

    THE RULE IS STRUCTURAL, NOT A LIST OF NAMES
    -------------------------------------------
    A mapping is treated as an envelope when exactly one of its values is a
    record (or a list of records) and every other value is a scalar. That
    catches ``{"result": {...}, "success": true}`` and
    ``{"data": [...], "count": 12}`` without knowing either word, and it
    refuses to unwrap ``{"invoice": {...}, "customer": {...}}``, where both
    values are records and neither is an envelope.

    A hint list exists only to break the tie when several candidates qualify.
    """
    path: list[str] = []
    current = body

    for _ in range(max_depth):
        if _is_record_list(current):
            return tuple(item for item in current if isinstance(item, Mapping)), tuple(path)

        if not isinstance(current, Mapping):
            break

        nested = [
            key
            for key, value in current.items()
            if _is_record(value) or _is_record_list(value)
        ]

        if len(nested) != 1:
            # Zero: this mapping is the record. More than one: two business
            # objects side by side, and picking one would be a guess.
            break

        key = nested[0]
        others = [k for k in current if k != key]

        # Every remaining sibling must be a scalar for this to be an envelope.
        if any(
            isinstance(current[other], (Mapping, list, tuple)) for other in others
        ):
            break

        # A lone nested object with no siblings is still an envelope
        # (``{"data": {...}}``); with siblings, they should look like
        # envelope metadata rather than business fields.
        if others and not any(
            str(other).lower() in _ENVELOPE_HINT_KEYS for other in others
        ):
            break

        path.append(str(key))
        current = current[key]

    if _is_record_list(current):
        return tuple(item for item in current if isinstance(item, Mapping)), tuple(path)

    if _is_record(current):
        return (current,), tuple(path)

    raise MalformedResponseError(
        "the response contains no object this engine can read as an ERP "
        "record",
        detail=f"root payload is {type(body).__name__}",
    )


def count_leaf_fields(payload: Any, _depth: int = 0) -> int:
    """Count the leaf values in a payload.

    The honest denominator for a reduction ratio: a caller's context cost is
    driven by leaves, not by the number of top-level keys. A list of records
    counts its first element only, because that is the record shape being
    described rather than the number of rows.
    """
    if _depth > 12:
        return 1

    if isinstance(payload, Mapping):
        return sum(count_leaf_fields(value, _depth + 1) for value in payload.values()) or 1

    if isinstance(payload, (list, tuple)):
        if not payload:
            return 1

        return count_leaf_fields(payload[0], _depth + 1)

    return 1


def infer_response_schema(
    records: Sequence[Mapping[str, Any]],
    source_system_id: str,
    entity_hint: str | None = None,
    endpoint: str | None = None,
) -> SourceSchema:
    """Build a throwaway ``SourceSchema`` describing this response.

    A live API response has no catalogued schema, so one is observed from the
    payload with the SAME engine that describes a Postman example or a MongoDB
    document. Marked ``SchemaOrigin.INFERRED`` so nothing downstream mistakes
    it for a declared contract, and never published to the catalog: it
    describes one response, not a source system.
    """
    if not records:
        raise MalformedResponseError("no records to infer a schema from")

    structure = infer_structure_from_examples(
        list(records), ApiSpecOptions(), entity_hint=entity_hint or ""
    )

    if not structure.fields:
        raise MalformedResponseError(
            "the response object carries no readable fields",
            detail="structure inference produced zero fields",
        )

    entity_name = normalize_identifier(
        entity_hint or _entity_name_from_endpoint(endpoint) or "response"
    )
    structural_hash = hash_json_payload(
        [
            [field.source_name, field.normalized_data_type.value]
            for field in structure.fields
        ]
    )

    entity = SourceEntity(
        entity_id=f"{source_system_id}.{entity_name}",
        source_name=entity_name,
        normalized_name=entity_name,
        entity_kind=EntityKind.API_SCHEMA,
        fields=structure.fields,
    )

    return SourceSchema(
        schema_id=f"{source_system_id}.response.{structural_hash[:16]}",
        source_system_id=source_system_id,
        schema_name=f"{entity_name}_response",
        origin=SchemaOrigin.INFERRED,
        entities=(entity,),
    )


def _entity_name_from_endpoint(endpoint: str | None) -> str | None:
    """A plausible entity name from an endpoint path.

    ``/api/invoices/INV-204`` -> ``invoices``. A HINT for entity matching
    only; the mapping engine still decides what the payload is, and a wrong
    guess here costs nothing because the engine scores the fields themselves.
    """
    if not endpoint:
        return None

    segments = [
        segment
        for segment in endpoint.strip("/").split("/")
        if segment and not segment.startswith("{")
    ]

    for segment in reversed(segments):
        # Skip identifier-looking segments: the resource name is the one
        # before them.
        if any(character.isdigit() for character in segment):
            continue

        if segment.lower() in {"api", "v1", "v2", "v3", "rest", "services"}:
            continue

        return segment

    return None


class StructuredResponseAdapter:
    """Maps one structured ERP response onto canonical ERP fields.

    Holds the shared mapping and transformation services, because both are
    stateless with respect to a single response and constructing them per
    request would rebuild the alias index every time.
    """

    def __init__(
        self,
        mapping: MappingService | None = None,
        transformation: TransformationService | None = None,
    ) -> None:
        self._mapping = mapping or MappingService()
        self._transformation = transformation or TransformationService()

    @property
    def mapping(self) -> MappingService:
        return self._mapping

    @property
    def transformation(self) -> TransformationService:
        return self._transformation

    def adapt(
        self,
        record: Mapping[str, Any],
        schema: SourceSchema,
        source_system_id: str,
        endpoint: str | None = None,
    ) -> "StructuredAdaptation":
        """Map and transform one record.

        Returns the canonical data alongside the per-field mapping decisions,
        so the caller can explain what became what without re-deriving it.
        """
        result = self._mapping.generate(schema, validate=False)

        if not result.profiles:
            raise MappingUnavailableError(
                "no canonical entity matched this response, so its fields "
                "cannot be expressed in ERP terms",
                entity_hint=schema.entities[0].source_name if schema.entities else None,
            )

        profile = result.profiles[0]

        transformed = self._transformation.transform_record(
            SourceRecord.from_mapping(
                dict(record), source_entity=schema.entities[0].source_name
            ),
            profile,
            TransformationContext(
                source_type=SourceType.OPENAPI,
                schema_id=schema.schema_id,
                ingestion_method="api_response_adaptation",
                source_file_path=endpoint,
            ),
        )

        canonical = transformed.record

        return StructuredAdaptation(
            entity_type=profile.target_entity_type,
            canonical_data=dict(canonical.normalized_data) if canonical else {},
            canonical_record_id=canonical.record_id if canonical else None,
            decisions=result.decisions,
            entity_confidence=_entity_confidence(result),
            mapping_id=profile.mapping_id,
            issues=tuple(
                issue.code for issue in (transformed.issues or ())
            ),
        )


def _entity_confidence(result: Any) -> float | None:
    """Mean score of the auto-selected field decisions.

    A stand-in for "how sure is the engine that this is an invoice": the
    entity match itself is not exposed as a number on the result, but the
    strength of the field evidence that produced it is a faithful proxy and
    is already computed.
    """
    scores = [
        decision.selected.score.total
        for decision in getattr(result, "decisions", ())
        if decision.outcome is FieldOutcome.AUTO_SELECTED and decision.selected
    ]

    if not scores:
        return None

    return round(sum(scores) / len(scores), 6)


class StructuredAdaptation:
    """The result of mapping one response record onto canonical ERP fields."""

    __slots__ = (
        "entity_type",
        "canonical_data",
        "canonical_record_id",
        "decisions",
        "entity_confidence",
        "mapping_id",
        "issues",
    )

    def __init__(
        self,
        entity_type: str,
        canonical_data: Mapping[str, Any],
        canonical_record_id: str | None,
        decisions: Sequence[Any],
        entity_confidence: float | None,
        mapping_id: str | None,
        issues: Sequence[str] = (),
    ) -> None:
        self.entity_type = entity_type
        self.canonical_data = dict(canonical_data)
        self.canonical_record_id = canonical_record_id
        self.decisions = tuple(decisions)
        self.entity_confidence = entity_confidence
        self.mapping_id = mapping_id
        self.issues = tuple(issues)


def flatten_record(
    payload: Any, prefix: str = "", _depth: int = 0
) -> dict[str, Any]:
    """Flatten a nested record into dotted paths.

    Used by the passthrough path, where no canonical mapping is applied and
    the fields must still be addressable and countable. Matches the dotted
    spelling the mapping engine uses for nested source fields, so a flattened
    key and a mapping decision refer to the same thing.
    """
    flat: dict[str, Any] = {}

    if _depth > 12:
        return {prefix or "value": payload}

    if isinstance(payload, Mapping):
        for key in sorted(payload, key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            flat.update(flatten_record(payload[key], child, _depth + 1))

        return flat or {prefix or "value": {}}

    if isinstance(payload, (list, tuple)):
        if payload and isinstance(payload[0], Mapping):
            # Describe the element shape, not every element: a 500-row list
            # would otherwise produce 500x the fields.
            return flatten_record(payload[0], f"{prefix}[]", _depth + 1)

        return {prefix or "value": list(payload)}

    return {prefix or "value": payload}


__all__ = [
    "MAX_UNWRAP_DEPTH",
    "unwrap_payload",
    "count_leaf_fields",
    "infer_response_schema",
    "flatten_record",
    "StructuredResponseAdapter",
    "StructuredAdaptation",
]
