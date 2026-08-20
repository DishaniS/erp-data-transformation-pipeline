"""Source-side contracts: how a heterogeneous ERP source is described.

These models describe what a source *looks like*, never how to connect to it.
One vocabulary has to cover:

    PostgreSQL / MySQL / SQL Server   table, column, foreign key
    MongoDB                           collection, nested document, embedding
    CSV                               dataset, header column
    OpenAPI / Postman                 object schema, property, ``$ref``
    PDF / image                       document set

The models are therefore structural rather than relational: a primary key is
optional, a field may live at a nested path, and a relationship may be an
embedding rather than a foreign key.

Nothing here discovers a schema. Introspection, MongoDB sampling, CSV type
inference and specification parsing all belong to later phases; Phase 1 only
fixes the shape those phases must produce.

All models are keyword-only, so adding a field later can never silently change
the meaning of an existing positional call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    RelationshipType,
    SchemaOrigin,
    SourceType,
)
from erp_pipeline.schemas.identity import hash_json_payload
from erp_pipeline.schemas.serialization import JsonModel, utc_now
from erp_pipeline.schemas.validation import (
    ValidationError,
    optional_identifier,
    optional_text,
    require_aware_datetime,
    require_confidence,
    require_identifier,
    require_metadata,
    require_text,
)
from erp_pipeline.version import SOURCE_MODEL_VERSION


@dataclass(kw_only=True)
class SourceSystem(JsonModel):
    """One ERP source system, described without any means of reaching it.

    STRICT RULE: this model never carries a password, API secret, token or a
    connection string containing credentials. ``metadata`` is validated against
    a credential-key denylist, so a serialized ``SourceSystem`` is always safe
    to log, diff, publish or store in a catalog.

    Connection configuration belongs to a future connector/secrets layer that
    references a system by ``source_system_id``.
    """

    source_system_id: str
    name: str
    source_type: SourceType
    description: str | None = None
    environment: str | None = None
    schema_version: str = SOURCE_MODEL_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.source_system_id = require_identifier(
            self.source_system_id, "SourceSystem.source_system_id"
        )
        self.name = require_text(self.name, "SourceSystem.name")
        self.source_type = SourceType.from_value(self.source_type)
        self.description = optional_text(self.description, "SourceSystem.description")
        self.environment = optional_identifier(
            self.environment, "SourceSystem.environment"
        )
        self.schema_version = require_text(
            self.schema_version, "SourceSystem.schema_version"
        )
        self.metadata = require_metadata(self.metadata, "SourceSystem.metadata")
        self.created_at = require_aware_datetime(
            self.created_at, "SourceSystem.created_at"
        )
        self.updated_at = require_aware_datetime(
            self.updated_at, "SourceSystem.updated_at"
        )


@dataclass(kw_only=True)
class SourceField(JsonModel):
    """One field of a source entity.

    The two type fields are the heart of this model and must not be collapsed:

    ``source_data_type``
        The vendor's own declaration, preserved verbatim - ``VARCHAR(100)``,
        ``DECIMAL(12,2)``, ``NVARCHAR``, ``ObjectId``, ``array<object>``,
        ``string($date-time)``. Precision, vendor spelling and parameters are
        exactly what a later phase needs for faithful type conversion and for
        detecting schema drift, and they are unrecoverable once discarded.

    ``normalized_data_type``
        The coarse cross-source classification a mapping reasons about.

    ``nested_path`` supports non-flat sources. A MongoDB field at
    ``financial.total`` or an OpenAPI property inside a nested object is stored
    as a tuple of segments, which stays unambiguous even when a segment itself
    contains a dot.
    """

    source_name: str
    normalized_name: str
    source_data_type: str | None = None
    normalized_data_type: FieldDataType = FieldDataType.UNKNOWN
    nullable: bool = True
    required: bool = False
    is_primary_key: bool = False
    is_unique: bool = False
    is_array: bool = False
    nested_path: tuple[str, ...] | None = None
    semantic_type: str | None = None
    description: str | None = None
    ordinal: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source_name = require_text(self.source_name, "SourceField.source_name")
        self.normalized_name = require_identifier(
            self.normalized_name, "SourceField.normalized_name"
        )
        self.source_data_type = optional_text(
            self.source_data_type, "SourceField.source_data_type"
        )
        self.normalized_data_type = FieldDataType.from_value(self.normalized_data_type)
        self.semantic_type = optional_identifier(
            self.semantic_type, "SourceField.semantic_type"
        )
        self.description = optional_text(self.description, "SourceField.description")
        self.nested_path = _coerce_path(self.nested_path, "SourceField.nested_path")
        self.metadata = require_metadata(self.metadata, "SourceField.metadata")

        for flag_name in ("nullable", "required", "is_primary_key", "is_unique", "is_array"):
            value = getattr(self, flag_name)
            if not isinstance(value, bool):
                raise ValidationError(
                    f"SourceField.{flag_name} must be a boolean, got "
                    f"{type(value).__name__}."
                )

        if self.ordinal is not None:
            if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
                raise ValidationError("SourceField.ordinal must be an integer.")
            if self.ordinal < 0:
                raise ValidationError("SourceField.ordinal must not be negative.")

        if self.is_primary_key and self.nullable:
            # A nullable primary key is contradictory in every source
            # technology this framework targets.
            raise ValidationError(
                f"SourceField {self.normalized_name!r} is marked as a primary key "
                "but is nullable. A primary-key field cannot be nullable."
            )

    @property
    def access_path(self) -> tuple[str, ...]:
        """Full path to this field within its entity, root fields included."""
        return (self.nested_path or ()) + (self.source_name,)


@dataclass(kw_only=True)
class SourceEntity(JsonModel):
    """One addressable structure inside a source system.

    Represents a relational table or view, a MongoDB collection, a CSV
    dataset, an OpenAPI/Postman object schema, or a set of documents.

    A primary key is NOT assumed. MongoDB collections, CSV files, API payload
    schemas and document sets frequently have none, and forcing one would make
    the model unusable for most non-relational sources.
    """

    entity_id: str
    source_name: str
    normalized_name: str
    entity_kind: EntityKind = EntityKind.TABLE
    namespace: str | None = None
    fields: Sequence[SourceField] = field(default_factory=tuple)
    primary_key_fields: Sequence[str] = field(default_factory=tuple)
    description: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.entity_id = require_identifier(self.entity_id, "SourceEntity.entity_id")
        self.source_name = require_text(self.source_name, "SourceEntity.source_name")
        self.normalized_name = require_identifier(
            self.normalized_name, "SourceEntity.normalized_name"
        )
        self.entity_kind = EntityKind.from_value(self.entity_kind)
        self.namespace = optional_text(self.namespace, "SourceEntity.namespace")
        self.description = optional_text(self.description, "SourceEntity.description")
        self.metadata = require_metadata(self.metadata, "SourceEntity.metadata")

        self.fields = _require_models(
            self.fields, SourceField, "SourceEntity.fields"
        )

        duplicate = _first_duplicate(f.normalized_name for f in self.fields)
        if duplicate is not None:
            raise ValidationError(
                f"SourceEntity {self.normalized_name!r} declares field "
                f"{duplicate!r} more than once. Normalized field names must be "
                "unique within an entity."
            )

        self.primary_key_fields = tuple(
            require_identifier(name, "SourceEntity.primary_key_fields[]")
            for name in _require_sequence(
                self.primary_key_fields, "SourceEntity.primary_key_fields"
            )
        )

        duplicate_key = _first_duplicate(self.primary_key_fields)
        if duplicate_key is not None:
            raise ValidationError(
                f"SourceEntity {self.normalized_name!r} lists primary-key field "
                f"{duplicate_key!r} more than once."
            )

        if self.fields:
            known = {f.normalized_name for f in self.fields}
            unknown = [name for name in self.primary_key_fields if name not in known]
            if unknown:
                raise ValidationError(
                    f"SourceEntity {self.normalized_name!r} names primary-key "
                    f"field(s) {unknown} that are not declared in fields."
                )

    @property
    def has_primary_key(self) -> bool:
        return bool(self.primary_key_fields)

    def field_by_normalized_name(self, normalized_name: str) -> SourceField | None:
        """Look up a declared field, or ``None`` when it is not present."""
        for item in self.fields:
            if item.normalized_name == normalized_name:
                return item
        return None


@dataclass(kw_only=True)
class SourceRelationship(JsonModel):
    """A relationship between two source entities.

    Not restricted to relational foreign keys. A MongoDB embedded document, a
    parent/child nesting in an OpenAPI schema and an inferred join candidate
    are all expressible.

    ``confidence`` records how certain the relationship is: 1.0 for a
    relationship read directly from a declared constraint, lower for one that a
    future inference step proposes. Phase 1 defines the field; nothing infers
    anything yet.
    """

    relationship_id: str
    relationship_type: RelationshipType
    from_entity: str
    to_entity: str
    from_fields: Sequence[str] = field(default_factory=tuple)
    to_fields: Sequence[str] = field(default_factory=tuple)
    confidence: float = 1.0
    description: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.relationship_id = require_identifier(
            self.relationship_id, "SourceRelationship.relationship_id"
        )
        self.relationship_type = RelationshipType.from_value(self.relationship_type)
        self.from_entity = require_identifier(
            self.from_entity, "SourceRelationship.from_entity"
        )
        self.to_entity = require_identifier(
            self.to_entity, "SourceRelationship.to_entity"
        )
        self.confidence = require_confidence(
            self.confidence, "SourceRelationship.confidence"
        )
        self.description = optional_text(
            self.description, "SourceRelationship.description"
        )
        self.metadata = require_metadata(self.metadata, "SourceRelationship.metadata")

        self.from_fields = tuple(
            require_identifier(name, "SourceRelationship.from_fields[]")
            for name in _require_sequence(
                self.from_fields, "SourceRelationship.from_fields"
            )
        )
        self.to_fields = tuple(
            require_identifier(name, "SourceRelationship.to_fields[]")
            for name in _require_sequence(
                self.to_fields, "SourceRelationship.to_fields"
            )
        )

        if not self.from_fields:
            raise ValidationError(
                f"SourceRelationship {self.relationship_id!r} must name at least "
                "one field on the source side."
            )

        # Key-based relationships pair fields positionally, so a composite key
        # must line up on both sides. Embedded and parent/child relationships
        # legitimately have no target field list at all.
        key_based = self.relationship_type in (
            RelationshipType.FOREIGN_KEY,
            RelationshipType.REFERENCE,
        )
        if key_based and len(self.from_fields) != len(self.to_fields):
            raise ValidationError(
                f"SourceRelationship {self.relationship_id!r} is "
                f"{self.relationship_type.value} but pairs "
                f"{len(self.from_fields)} source field(s) with "
                f"{len(self.to_fields)} target field(s). Key-based relationships "
                "must pair fields one to one."
            )


@dataclass(kw_only=True)
class SourceSchema(JsonModel):
    """A versioned description of one source system's structure.

    Equally able to hold a relational catalog, a set of inferred MongoDB
    collection shapes, CSV dataset headers, or the object schemas extracted
    from an OpenAPI document or a Postman collection - ``origin`` records which
    of those it is.

    ``schema_version`` is the version of *this description* of the source, not
    of the contract model. It lets a later phase keep successive snapshots of
    the same source and compare them; comparing them is Phase 8's problem, not
    Phase 1's.
    """

    schema_id: str
    source_system_id: str
    schema_name: str
    origin: SchemaOrigin
    schema_version: str = "1"
    entities: Sequence[SourceEntity] = field(default_factory=tuple)
    relationships: Sequence[SourceRelationship] = field(default_factory=tuple)
    schema_hash: str | None = None
    model_version: str = SOURCE_MODEL_VERSION
    discovered_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.schema_id = require_identifier(self.schema_id, "SourceSchema.schema_id")
        self.source_system_id = require_identifier(
            self.source_system_id, "SourceSchema.source_system_id"
        )
        self.schema_name = require_text(self.schema_name, "SourceSchema.schema_name")
        self.origin = SchemaOrigin.from_value(self.origin)
        self.schema_version = require_text(
            self.schema_version, "SourceSchema.schema_version"
        )
        self.model_version = require_text(
            self.model_version, "SourceSchema.model_version"
        )
        self.schema_hash = optional_text(self.schema_hash, "SourceSchema.schema_hash")
        self.metadata = require_metadata(self.metadata, "SourceSchema.metadata")
        self.created_at = require_aware_datetime(
            self.created_at, "SourceSchema.created_at"
        )
        if self.discovered_at is not None:
            self.discovered_at = require_aware_datetime(
                self.discovered_at, "SourceSchema.discovered_at"
            )

        self.entities = _require_models(
            self.entities, SourceEntity, "SourceSchema.entities"
        )
        self.relationships = _require_models(
            self.relationships, SourceRelationship, "SourceSchema.relationships"
        )

        duplicate = _first_duplicate(e.normalized_name for e in self.entities)
        if duplicate is not None:
            raise ValidationError(
                f"SourceSchema {self.schema_id!r} declares entity {duplicate!r} "
                "more than once. Normalized entity names must be unique."
            )

        # Relationships may only reference entities this schema declares.
        # Skipped when no entities are listed, so a relationship-only fragment
        # stays representable.
        if self.entities:
            known = {e.normalized_name for e in self.entities}
            for relationship in self.relationships:
                missing = [
                    name
                    for name in (relationship.from_entity, relationship.to_entity)
                    if name not in known
                ]
                if missing:
                    raise ValidationError(
                        f"SourceRelationship {relationship.relationship_id!r} "
                        f"references entit(ies) {missing} that are not declared "
                        f"in schema {self.schema_id!r}."
                    )

    def entity_by_normalized_name(self, normalized_name: str) -> SourceEntity | None:
        """Look up a declared entity, or ``None`` when it is not present."""
        for entity in self.entities:
            if entity.normalized_name == normalized_name:
                return entity
        return None

    def compute_schema_hash(self) -> str:
        """Deterministic hash of this schema's structure.

        Covers entities, fields and relationships. Deliberately excludes
        timestamps, the stored hash itself and free-form metadata, so the same
        source structure hashes identically no matter when it was described.

        The per-field attributes hashed here must stay in sync with
        ``erp_pipeline.catalog.versioning._COMPARED_FIELD_ATTRIBUTES``, which
        is what ``compare_schemas()`` treats as structural. A field attribute
        present in one list but not the other lets a real structural change
        either go undetected by the hash (silently skipped by catalog
        versioning) or be reported by the diff without ever producing a new
        catalog version. ``semantic_type`` was added here for exactly this
        reason.

        Phase 1 only provides the value. ``erp_pipeline.catalog.versioning.
        compare_schemas()`` (Phase 2) is what compares two hashes' underlying
        schemas to detect drift.
        """
        projection = {
            "source_system_id": self.source_system_id,
            "entities": [
                {
                    "normalized_name": entity.normalized_name,
                    "entity_kind": entity.entity_kind.value,
                    "namespace": entity.namespace,
                    "primary_key_fields": list(entity.primary_key_fields),
                    "fields": sorted(
                        (
                            {
                                "normalized_name": item.normalized_name,
                                "source_name": item.source_name,
                                "source_data_type": item.source_data_type,
                                "normalized_data_type": item.normalized_data_type.value,
                                "nullable": item.nullable,
                                "required": item.required,
                                "is_primary_key": item.is_primary_key,
                                "is_unique": item.is_unique,
                                "is_array": item.is_array,
                                "nested_path": list(item.nested_path or ()),
                                "semantic_type": item.semantic_type,
                            }
                            for item in entity.fields
                        ),
                        key=lambda payload: payload["normalized_name"],
                    ),
                }
                for entity in sorted(
                    self.entities, key=lambda entity: entity.normalized_name
                )
            ],
            "relationships": sorted(
                (
                    {
                        "relationship_type": relationship.relationship_type.value,
                        "from_entity": relationship.from_entity,
                        "from_fields": list(relationship.from_fields),
                        "to_entity": relationship.to_entity,
                        "to_fields": list(relationship.to_fields),
                    }
                    for relationship in self.relationships
                ),
                key=lambda payload: (
                    payload["from_entity"],
                    payload["to_entity"],
                    payload["relationship_type"],
                ),
            ),
        }

        return hash_json_payload(projection)


# ============================================================
# Internal helpers
# ============================================================

def _coerce_path(value: Any, field_name: str) -> tuple[str, ...] | None:
    """Accept ``None``, a dotted string or a sequence; return path segments."""
    if value is None:
        return None

    if isinstance(value, str):
        segments: Sequence[str] = [part for part in value.split(".") if part]
    elif isinstance(value, (list, tuple)):
        segments = list(value)
    else:
        raise ValidationError(
            f"{field_name} must be a dotted string or a sequence of segments, "
            f"got {type(value).__name__}."
        )

    cleaned = tuple(require_text(segment, f"{field_name}[]") for segment in segments)

    return cleaned or None


def _require_sequence(value: Any, field_name: str) -> Sequence[Any]:
    """Reject a bare string where a sequence of names is expected."""
    if value is None:
        return ()

    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValidationError(
            f"{field_name} must be a list or tuple, got {type(value).__name__}."
        )

    return value


def _require_models(value: Any, expected: type, field_name: str) -> tuple[Any, ...]:
    """Require a sequence whose members are all of the expected model type."""
    items = _require_sequence(value, field_name)

    for index, item in enumerate(items):
        if not isinstance(item, expected):
            raise ValidationError(
                f"{field_name}[{index}] must be a {expected.__name__}, got "
                f"{type(item).__name__}."
            )

    return tuple(items)


def _first_duplicate(values: Iterable[str]) -> str | None:
    """Return the first repeated value, or ``None`` when all are distinct."""
    seen: set[str] = set()

    for value in values:
        if value in seen:
            return value
        seen.add(value)

    return None


__all__ = [
    "SourceSystem",
    "SourceSchema",
    "SourceEntity",
    "SourceField",
    "SourceRelationship",
]
