"""Mapping contracts: how a future engine will be told to convert a source.

NO MAPPING ENGINE EXISTS. Nothing in this module transforms a value, suggests
a mapping, or scores a match. These are the definitions such an engine will
consume once it is built in a later phase.

Mappings are DATA
-----------------
A mapping profile is a serializable document. It is authored, reviewed,
approved, stored and versioned like any other record, and it can be diffed and
audited. That is the whole point: transformation logic that lives in Python
functions cannot be reviewed by a domain expert, cannot be stored per source
system, and cannot be changed without a deployment.

Security: a transformation is an operation NAME plus a JSON configuration -
never a code string. There is no field anywhere in this module that a future
engine could pass to ``eval``, ``exec``, ``compile`` or a shell. An engine must
dispatch on ``TransformationOperation`` members it implements, and reject any
operation it does not recognize.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from erp_pipeline.schemas.enums import (
    FieldDataType,
    MappingStatus,
    TransformationOperation,
)
from erp_pipeline.schemas.serialization import JsonModel, utc_now
from erp_pipeline.schemas.validation import (
    ValidationError,
    optional_aware_datetime,
    optional_text,
    require_aware_datetime,
    require_confidence,
    require_identifier,
    require_json_object,
    require_metadata,
    require_text,
)
from erp_pipeline.version import MAPPING_MODEL_VERSION


@dataclass(kw_only=True)
class TransformationRule(JsonModel):
    """A declarative, data-only transformation step.

    ``operation`` names what to do; ``config`` parameterizes it. Examples of
    the shape a future engine would receive:

        {"operation": "cast",      "config": {"to": "decimal"}}
        {"operation": "enum_map",  "config": {"values": {"Y": "approved"}}}
        {"operation": "nested_path", "config": {"path": ["financial", "total"]}}
        {"operation": "date_parse", "config": {"format": "%d/%m/%Y"}}

    ``config`` is validated as a JSON object, which by construction excludes
    callables, lambdas, classes and arbitrary Python objects - so a mapping
    author cannot smuggle executable behaviour through it.
    """

    operation: TransformationOperation
    config: Mapping[str, Any] = field(default_factory=dict)
    description: str | None = None

    def __post_init__(self) -> None:
        self.operation = TransformationOperation.from_value(self.operation)
        self.config = require_json_object(self.config, "TransformationRule.config")
        self.description = optional_text(
            self.description, "TransformationRule.description"
        )


@dataclass(kw_only=True)
class FieldMapping(JsonModel):
    """How one source field becomes one canonical field.

    ``confidence`` and ``status`` exist so an automated proposal and a
    human-approved decision are the same kind of object at different points in
    a review workflow. Phase 1 defines the workflow's vocabulary; it implements
    none of it.
    """

    source_field: str
    target_field: str
    source_type: FieldDataType | None = None
    target_type: FieldDataType | None = None
    transformations: Sequence[TransformationRule] = field(default_factory=tuple)
    confidence: float = 1.0
    status: MappingStatus = MappingStatus.SUGGESTED
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source_field = require_text(
            self.source_field, "FieldMapping.source_field"
        )
        self.target_field = require_identifier(
            self.target_field, "FieldMapping.target_field"
        )
        self.confidence = require_confidence(
            self.confidence, "FieldMapping.confidence"
        )
        self.status = MappingStatus.from_value(self.status)
        self.reason = optional_text(self.reason, "FieldMapping.reason")
        self.metadata = require_metadata(self.metadata, "FieldMapping.metadata")

        if self.source_type is not None:
            self.source_type = FieldDataType.from_value(self.source_type)
        if self.target_type is not None:
            self.target_type = FieldDataType.from_value(self.target_type)

        if isinstance(self.transformations, (str, bytes)) or not isinstance(
            self.transformations, (list, tuple)
        ):
            raise ValidationError(
                "FieldMapping.transformations must be a list or tuple of "
                f"TransformationRule, got {type(self.transformations).__name__}."
            )

        for index, rule in enumerate(self.transformations):
            if not isinstance(rule, TransformationRule):
                raise ValidationError(
                    f"FieldMapping.transformations[{index}] must be a "
                    f"TransformationRule, got {type(rule).__name__}. A "
                    "transformation is declarative data, never a callable or a "
                    "code string."
                )

        self.transformations = tuple(self.transformations)


@dataclass(kw_only=True)
class MappingProfile(JsonModel):
    """A reviewable set of field mappings from one source entity to one
    canonical entity type.

    Scoped to a specific ``source_system_id`` + ``source_entity``, because the
    same logical entity type is shaped differently in every ERP: an invoice in
    a MySQL system and an invoice in a MongoDB system need different profiles
    producing the same canonical shape.
    """

    mapping_id: str
    source_system_id: str
    source_entity: str
    target_entity_type: str
    source_schema_id: str | None = None
    schema_version: str = "1"
    model_version: str = MAPPING_MODEL_VERSION
    field_mappings: Sequence[FieldMapping] = field(default_factory=tuple)
    status: MappingStatus = MappingStatus.SUGGESTED
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.mapping_id = require_identifier(
            self.mapping_id, "MappingProfile.mapping_id"
        )
        self.source_system_id = require_identifier(
            self.source_system_id, "MappingProfile.source_system_id"
        )
        self.source_entity = require_text(
            self.source_entity, "MappingProfile.source_entity"
        )
        self.target_entity_type = require_identifier(
            self.target_entity_type, "MappingProfile.target_entity_type"
        )
        self.source_schema_id = (
            require_identifier(self.source_schema_id, "MappingProfile.source_schema_id")
            if self.source_schema_id is not None
            else None
        )
        self.schema_version = require_text(
            self.schema_version, "MappingProfile.schema_version"
        )
        self.model_version = require_text(
            self.model_version, "MappingProfile.model_version"
        )
        self.status = MappingStatus.from_value(self.status)
        self.approved_by = optional_text(self.approved_by, "MappingProfile.approved_by")
        self.approved_at = optional_aware_datetime(
            self.approved_at, "MappingProfile.approved_at"
        )
        self.created_at = require_aware_datetime(
            self.created_at, "MappingProfile.created_at"
        )
        self.updated_at = require_aware_datetime(
            self.updated_at, "MappingProfile.updated_at"
        )
        self.metadata = require_metadata(self.metadata, "MappingProfile.metadata")

        if isinstance(self.field_mappings, (str, bytes)) or not isinstance(
            self.field_mappings, (list, tuple)
        ):
            raise ValidationError(
                "MappingProfile.field_mappings must be a list or tuple, got "
                f"{type(self.field_mappings).__name__}."
            )

        for index, mapping in enumerate(self.field_mappings):
            if not isinstance(mapping, FieldMapping):
                raise ValidationError(
                    f"MappingProfile.field_mappings[{index}] must be a "
                    f"FieldMapping, got {type(mapping).__name__}."
                )

        self.field_mappings = tuple(self.field_mappings)

        # The same (source_field, target_field) pair twice is a duplicate
        # instruction. Two different source fields feeding one target is left
        # legal, because a future concat/coalesce operation needs exactly that.
        seen: set[tuple[str, str]] = set()
        for mapping in self.field_mappings:
            pair = (mapping.source_field, mapping.target_field)
            if pair in seen:
                raise ValidationError(
                    f"MappingProfile {self.mapping_id!r} maps "
                    f"{mapping.source_field!r} to {mapping.target_field!r} more "
                    "than once."
                )
            seen.add(pair)

        # An approval must say who approved it; an unapproved profile must not
        # claim an approver.
        if self.status is MappingStatus.APPROVED and not self.approved_by:
            raise ValidationError(
                f"MappingProfile {self.mapping_id!r} has status 'approved' but "
                "no approved_by. An approval must record who granted it."
            )

    @property
    def target_fields(self) -> tuple[str, ...]:
        """Canonical field names this profile produces, in declaration order."""
        result: list[str] = []
        for mapping in self.field_mappings:
            if mapping.target_field not in result:
                result.append(mapping.target_field)
        return tuple(result)

    def mappings_requiring_review(self) -> tuple[FieldMapping, ...]:
        """Field mappings a human still has to decide on."""
        return tuple(
            mapping
            for mapping in self.field_mappings
            if mapping.status
            in (MappingStatus.SUGGESTED, MappingStatus.REVIEW_REQUIRED)
        )


__all__ = [
    "TransformationRule",
    "FieldMapping",
    "MappingProfile",
]
