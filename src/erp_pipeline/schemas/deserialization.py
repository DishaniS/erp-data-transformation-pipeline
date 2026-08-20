"""Pure JSON -> model reconstruction for the persisted contract types.

Phase 1 defined ``model -> JSON`` (``JsonModel.to_json_dict``) but deliberately
left the reverse direction unimplemented, since nothing in Phase 1 read
contracts back. Phase 2 persists contracts and must read them back exactly, so
this module closes that gap.

Design
------
Each ``from_*_dict`` function is an explicit, hand-written constructor call -
never a generic reflective loader, never ``eval``/``exec``/``pickle``, and
never a class name taken from the input. A malformed or hostile JSON payload
can only ever fail model construction (raising ``ValidationError`` from the
model's own ``__post_init__``); it can never cause arbitrary code to run or an
arbitrary class to be instantiated.

Every function accepts the *exact* shape ``to_json_dict()`` produces for that
model - enum members as their ``.value`` string, datetimes as RFC3339 ``Z``
strings, nested models as nested dicts, sequences as JSON arrays - so the round
trip

    model -> to_json_dict() -> from_*_dict() -> model

reconstructs a value equal to the original under ``to_json_dict()`` equality
(see the Phase 2 tests for the exact equality check used).

This module performs no I/O and imports nothing outside ``erp_pipeline`` and
the standard library, preserving the Phase 1 boundary that ``erp_pipeline.
schemas`` is a pure, dependency-light contract layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from erp_pipeline.schemas.canonical_models import (
    CanonicalDocument,
    CanonicalRecord,
    RecordProvenance,
    SourceReference,
)
from erp_pipeline.schemas.mapping_models import (
    FieldMapping,
    MappingProfile,
    TransformationRule,
)
from erp_pipeline.schemas.run_models import DataQualityIssue, TransformationRun
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
    SourceSystem,
)


class DeserializationError(ValueError):
    """Raised when a JSON payload cannot be reconstructed into a model.

    Wraps whatever the model's own validation raised, so the underlying
    ``ValidationError`` (or ``ValueError`` from an enum conversion) is always
    available via ``__cause__`` for debugging.
    """


def _parse_datetime(value: Any, field_name: str) -> datetime | None:
    """Parse an RFC3339 string produced by ``to_rfc3339`` back into a datetime.

    ``datetime.fromisoformat`` does not accept a trailing ``Z`` before Python
    3.11's relaxed parser; this project's minimum runtime already targets
    3.13, but the explicit replace keeps the function correct without relying
    on that.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    if not isinstance(value, str):
        raise DeserializationError(
            f"{field_name} must be an RFC3339 string, got {type(value).__name__}."
        )

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value

    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DeserializationError(
            f"{field_name} is not a valid RFC3339 datetime: {value!r}."
        ) from exc


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DeserializationError(
            f"{field_name} must be an object, got {type(value).__name__}."
        )
    return value


def _as_sequence(value: Any, field_name: str) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise DeserializationError(
            f"{field_name} must be an array, got {type(value).__name__}."
        )
    return value


def _wrap(model_name: str, payload: Mapping[str, Any], build):
    """Run one constructor and translate any failure into DeserializationError.

    ``KeyError`` is included because every constructor lambda below reads
    required fields via ``payload["..."]`` - a payload missing one of them
    must raise the same domain error as a payload with an invalid value, not
    an unrelated raw ``KeyError``.
    """
    try:
        return build()
    except DeserializationError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise DeserializationError(
            f"Could not reconstruct {model_name} from {sorted(payload)}: {exc}"
        ) from exc


# ============================================================
# Source-side models
# ============================================================

def source_system_from_dict(payload: Mapping[str, Any]) -> SourceSystem:
    """Reconstruct a ``SourceSystem`` from its ``to_json_dict()`` output."""
    return _wrap(
        "SourceSystem",
        payload,
        lambda: SourceSystem(
            source_system_id=payload["source_system_id"],
            name=payload["name"],
            source_type=payload["source_type"],
            description=payload.get("description"),
            environment=payload.get("environment"),
            schema_version=payload.get("schema_version", "1.0.0"),
            metadata=_as_mapping(payload.get("metadata"), "SourceSystem.metadata"),
            created_at=_parse_datetime(
                payload.get("created_at"), "SourceSystem.created_at"
            ),
            updated_at=_parse_datetime(
                payload.get("updated_at"), "SourceSystem.updated_at"
            ),
        ),
    )


def source_field_from_dict(payload: Mapping[str, Any]) -> SourceField:
    """Reconstruct a ``SourceField`` from its ``to_json_dict()`` output."""
    return _wrap(
        "SourceField",
        payload,
        lambda: SourceField(
            source_name=payload["source_name"],
            normalized_name=payload["normalized_name"],
            source_data_type=payload.get("source_data_type"),
            normalized_data_type=payload.get("normalized_data_type", "unknown"),
            nullable=payload.get("nullable", True),
            required=payload.get("required", False),
            is_primary_key=payload.get("is_primary_key", False),
            is_unique=payload.get("is_unique", False),
            is_array=payload.get("is_array", False),
            nested_path=_nested_path_from_json(payload.get("nested_path")),
            semantic_type=payload.get("semantic_type"),
            description=payload.get("description"),
            ordinal=payload.get("ordinal"),
            metadata=_as_mapping(payload.get("metadata"), "SourceField.metadata"),
        ),
    )


def _nested_path_from_json(value: Any) -> tuple[str, ...] | None:
    """``to_json_dict()`` renders ``nested_path`` as a JSON array or null."""
    if value is None:
        return None
    return tuple(_as_sequence(value, "SourceField.nested_path"))


def source_entity_from_dict(payload: Mapping[str, Any]) -> SourceEntity:
    """Reconstruct a ``SourceEntity``, including its nested fields."""
    return _wrap(
        "SourceEntity",
        payload,
        lambda: SourceEntity(
            entity_id=payload["entity_id"],
            source_name=payload["source_name"],
            normalized_name=payload["normalized_name"],
            entity_kind=payload.get("entity_kind", "table"),
            namespace=payload.get("namespace"),
            fields=tuple(
                source_field_from_dict(item)
                for item in _as_sequence(payload.get("fields"), "SourceEntity.fields")
            ),
            primary_key_fields=tuple(
                _as_sequence(
                    payload.get("primary_key_fields"), "SourceEntity.primary_key_fields"
                )
            ),
            description=payload.get("description"),
            metadata=_as_mapping(payload.get("metadata"), "SourceEntity.metadata"),
        ),
    )


def source_relationship_from_dict(payload: Mapping[str, Any]) -> SourceRelationship:
    """Reconstruct a ``SourceRelationship`` from its ``to_json_dict()`` output."""
    return _wrap(
        "SourceRelationship",
        payload,
        lambda: SourceRelationship(
            relationship_id=payload["relationship_id"],
            relationship_type=payload["relationship_type"],
            from_entity=payload["from_entity"],
            to_entity=payload["to_entity"],
            from_fields=tuple(
                _as_sequence(
                    payload.get("from_fields"), "SourceRelationship.from_fields"
                )
            ),
            to_fields=tuple(
                _as_sequence(payload.get("to_fields"), "SourceRelationship.to_fields")
            ),
            confidence=payload.get("confidence", 1.0),
            description=payload.get("description"),
            metadata=_as_mapping(
                payload.get("metadata"), "SourceRelationship.metadata"
            ),
        ),
    )


def source_schema_from_dict(payload: Mapping[str, Any]) -> SourceSchema:
    """Reconstruct a full ``SourceSchema``: entities, fields and relationships.

    This is the primary Phase 2 round-trip target: everything a catalog
    snapshot must reproduce flows through this one function.
    """
    return _wrap(
        "SourceSchema",
        payload,
        lambda: SourceSchema(
            schema_id=payload["schema_id"],
            source_system_id=payload["source_system_id"],
            schema_name=payload["schema_name"],
            origin=payload["origin"],
            schema_version=payload.get("schema_version", "1"),
            entities=tuple(
                source_entity_from_dict(item)
                for item in _as_sequence(payload.get("entities"), "SourceSchema.entities")
            ),
            relationships=tuple(
                source_relationship_from_dict(item)
                for item in _as_sequence(
                    payload.get("relationships"), "SourceSchema.relationships"
                )
            ),
            schema_hash=payload.get("schema_hash"),
            model_version=payload.get("model_version", "1.0.0"),
            discovered_at=_parse_datetime(
                payload.get("discovered_at"), "SourceSchema.discovered_at"
            ),
            created_at=_parse_datetime(
                payload.get("created_at"), "SourceSchema.created_at"
            ),
            metadata=_as_mapping(payload.get("metadata"), "SourceSchema.metadata"),
        ),
    )


# ============================================================
# Canonical models
# ============================================================

def source_reference_from_dict(payload: Mapping[str, Any]) -> SourceReference:
    return _wrap(
        "SourceReference",
        payload,
        lambda: SourceReference(
            source_system_id=payload["source_system_id"],
            source_type=payload["source_type"],
            source_entity=payload.get("source_entity"),
            source_record_key=payload.get("source_record_key"),
        ),
    )


def record_provenance_from_dict(payload: Mapping[str, Any] | None) -> RecordProvenance | None:
    if payload is None:
        return None

    return _wrap(
        "RecordProvenance",
        payload,
        lambda: RecordProvenance(
            schema_id=payload.get("schema_id"),
            schema_version=payload.get("schema_version"),
            ingestion_method=payload.get("ingestion_method"),
            original_record_id=payload.get("original_record_id"),
            source_file_path=payload.get("source_file_path"),
            page_number=payload.get("page_number"),
            api_operation=payload.get("api_operation"),
            extracted_at=_parse_datetime(
                payload.get("extracted_at"), "RecordProvenance.extracted_at"
            ),
            metadata=_as_mapping(payload.get("metadata"), "RecordProvenance.metadata"),
        ),
    )


def canonical_record_from_dict(payload: Mapping[str, Any]) -> CanonicalRecord:
    """Reconstruct a ``CanonicalRecord``, bypassing ``from_source`` id derivation.

    Uses the direct constructor rather than ``CanonicalRecord.from_source``
    because the persisted ``record_id`` and ``content_hash`` are already final
    and must be preserved exactly, not recomputed.
    """
    return _wrap(
        "CanonicalRecord",
        payload,
        lambda: CanonicalRecord(
            record_id=payload["record_id"],
            record_type=payload.get("record_type", "structured_record"),
            source=source_reference_from_dict(payload["source"]),
            entity_type=payload["entity_type"],
            normalized_data=_as_mapping(
                payload.get("normalized_data"), "CanonicalRecord.normalized_data"
            ),
            text_for_ai=payload.get("text_for_ai"),
            schema_version=payload.get("schema_version", "1.0.0"),
            content_hash=payload.get("content_hash"),
            sensitivity=payload.get("sensitivity", "internal"),
            provenance=record_provenance_from_dict(payload.get("provenance")),
            created_at=_parse_datetime(
                payload.get("created_at"), "CanonicalRecord.created_at"
            ),
            updated_at=_parse_datetime(
                payload.get("updated_at"), "CanonicalRecord.updated_at"
            ),
            metadata=_as_mapping(payload.get("metadata"), "CanonicalRecord.metadata"),
        ),
    )


def canonical_document_from_dict(payload: Mapping[str, Any]) -> CanonicalDocument:
    return _wrap(
        "CanonicalDocument",
        payload,
        lambda: CanonicalDocument(
            record_id=payload["record_id"],
            source=source_reference_from_dict(payload["source"]),
            document_id=payload["document_id"],
            title=payload.get("title"),
            document_type=payload.get("document_type"),
            mime_type=payload.get("mime_type"),
            text=payload.get("text"),
            page_count=payload.get("page_count"),
            language=payload.get("language"),
            record_type=payload.get("record_type", "document"),
            schema_version=payload.get("schema_version", "1.0.0"),
            content_hash=payload.get("content_hash"),
            sensitivity=payload.get("sensitivity", "internal"),
            provenance=record_provenance_from_dict(payload.get("provenance")),
            created_at=_parse_datetime(
                payload.get("created_at"), "CanonicalDocument.created_at"
            ),
            updated_at=_parse_datetime(
                payload.get("updated_at"), "CanonicalDocument.updated_at"
            ),
            metadata=_as_mapping(
                payload.get("metadata"), "CanonicalDocument.metadata"
            ),
        ),
    )


# ============================================================
# Mapping contracts
# ============================================================

def transformation_rule_from_dict(payload: Mapping[str, Any]) -> TransformationRule:
    return _wrap(
        "TransformationRule",
        payload,
        lambda: TransformationRule(
            operation=payload["operation"],
            config=_as_mapping(payload.get("config"), "TransformationRule.config"),
            description=payload.get("description"),
        ),
    )


def field_mapping_from_dict(payload: Mapping[str, Any]) -> FieldMapping:
    return _wrap(
        "FieldMapping",
        payload,
        lambda: FieldMapping(
            source_field=payload["source_field"],
            target_field=payload["target_field"],
            source_type=payload.get("source_type"),
            target_type=payload.get("target_type"),
            transformations=tuple(
                transformation_rule_from_dict(item)
                for item in _as_sequence(
                    payload.get("transformations"), "FieldMapping.transformations"
                )
            ),
            confidence=payload.get("confidence", 1.0),
            status=payload.get("status", "suggested"),
            reason=payload.get("reason"),
            metadata=_as_mapping(payload.get("metadata"), "FieldMapping.metadata"),
        ),
    )


def mapping_profile_from_dict(payload: Mapping[str, Any]) -> MappingProfile:
    """Reconstruct a full ``MappingProfile``, including its field mappings."""
    return _wrap(
        "MappingProfile",
        payload,
        lambda: MappingProfile(
            mapping_id=payload["mapping_id"],
            source_system_id=payload["source_system_id"],
            source_entity=payload["source_entity"],
            target_entity_type=payload["target_entity_type"],
            source_schema_id=payload.get("source_schema_id"),
            schema_version=payload.get("schema_version", "1"),
            model_version=payload.get("model_version", "1.0.0"),
            field_mappings=tuple(
                field_mapping_from_dict(item)
                for item in _as_sequence(
                    payload.get("field_mappings"), "MappingProfile.field_mappings"
                )
            ),
            status=payload.get("status", "suggested"),
            approved_by=payload.get("approved_by"),
            approved_at=_parse_datetime(
                payload.get("approved_at"), "MappingProfile.approved_at"
            ),
            created_at=_parse_datetime(
                payload.get("created_at"), "MappingProfile.created_at"
            ),
            updated_at=_parse_datetime(
                payload.get("updated_at"), "MappingProfile.updated_at"
            ),
            metadata=_as_mapping(payload.get("metadata"), "MappingProfile.metadata"),
        ),
    )


# ============================================================
# Run / quality contracts
# ============================================================

def transformation_run_from_dict(payload: Mapping[str, Any]) -> TransformationRun:
    return _wrap(
        "TransformationRun",
        payload,
        lambda: TransformationRun(
            run_id=payload["run_id"],
            source_system_id=payload["source_system_id"],
            status=payload.get("status", "pending"),
            mapping_id=payload.get("mapping_id"),
            started_at=_parse_datetime(
                payload.get("started_at"), "TransformationRun.started_at"
            ),
            completed_at=_parse_datetime(
                payload.get("completed_at"), "TransformationRun.completed_at"
            ),
            records_read=payload.get("records_read", 0),
            records_transformed=payload.get("records_transformed", 0),
            records_failed=payload.get("records_failed", 0),
            records_skipped=payload.get("records_skipped", 0),
            warning_count=payload.get("warning_count", 0),
            error_count=payload.get("error_count", 0),
            model_version=payload.get("model_version", "1.0.0"),
            message=payload.get("message"),
            created_at=_parse_datetime(
                payload.get("created_at"), "TransformationRun.created_at"
            ),
            metadata=_as_mapping(payload.get("metadata"), "TransformationRun.metadata"),
        ),
    )


def data_quality_issue_from_dict(payload: Mapping[str, Any]) -> DataQualityIssue:
    return _wrap(
        "DataQualityIssue",
        payload,
        lambda: DataQualityIssue(
            issue_id=payload["issue_id"],
            severity=payload["severity"],
            code=payload["code"],
            message=payload["message"],
            run_id=payload.get("run_id"),
            record_id=payload.get("record_id"),
            source_entity=payload.get("source_entity"),
            field_name=payload.get("field_name"),
            original_value_summary=payload.get("original_value_summary"),
            expected=payload.get("expected"),
            model_version=payload.get("model_version", "1.0.0"),
            created_at=_parse_datetime(
                payload.get("created_at"), "DataQualityIssue.created_at"
            ),
            metadata=_as_mapping(
                payload.get("metadata"), "DataQualityIssue.metadata"
            ),
        ),
    )


__all__ = [
    "DeserializationError",
    "source_system_from_dict",
    "source_field_from_dict",
    "source_entity_from_dict",
    "source_relationship_from_dict",
    "source_schema_from_dict",
    "source_reference_from_dict",
    "record_provenance_from_dict",
    "canonical_record_from_dict",
    "canonical_document_from_dict",
    "transformation_rule_from_dict",
    "field_mapping_from_dict",
    "mapping_profile_from_dict",
    "transformation_run_from_dict",
    "data_quality_issue_from_dict",
]
