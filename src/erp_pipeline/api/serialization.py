"""Turn frozen contract objects into API response models.

WHY THIS MODULE EXISTS
----------------------
Two routers previously serialized a ``SourceSchema`` by hand, each with its own
``getattr(field, name, default)`` chain. That pattern converts a contract change
into a silently empty response field rather than a loud failure, and it is
exactly how ``field.data_type`` - an attribute ``SourceField`` has never had -
returned an empty string for every field's type in every schema response.

So the mapping lives here, once, and reads attributes EXPLICITLY. If the
contract changes, this breaks loudly and a test catches it, which is what
should happen.
"""

from __future__ import annotations

from typing import Any

from erp_pipeline.api.schemas import (
    SchemaEntityResponse,
    SchemaFieldResponse,
    SchemaRelationshipResponse,
    SchemaResponse,
)


def _enum_value(value: Any) -> str | None:
    """Render an enum by its wire value, leaving plain strings alone."""
    if value is None:
        return None

    return str(getattr(value, "value", value))


def field_response(field: Any) -> SchemaFieldResponse:
    """Serialize one ``SourceField``, keeping BOTH type views.

    ``source_data_type`` and ``normalized_data_type`` answer different
    questions - what the vendor declared, and how the value behaves once
    normalized - and a consumer generating typed ERP tooling needs the first
    while the mapping engine reasons about the second. Collapsing them loses
    precision that cannot be recovered.
    """
    return SchemaFieldResponse(
        source_name=field.source_name,
        normalized_name=field.normalized_name,
        source_data_type=field.source_data_type,
        normalized_data_type=_enum_value(field.normalized_data_type),
        nullable=field.nullable,
        required=field.required,
        is_primary_key=field.is_primary_key,
        is_unique=field.is_unique,
        is_array=field.is_array,
        nested_path=list(field.nested_path) if field.nested_path else None,
        semantic_type=field.semantic_type,
        description=field.description,
        ordinal=field.ordinal,
    )


def entity_response(entity: Any) -> SchemaEntityResponse:
    """Serialize one ``SourceEntity`` and its fields."""
    return SchemaEntityResponse(
        entity_id=entity.entity_id,
        source_name=entity.source_name,
        normalized_name=entity.normalized_name,
        entity_kind=_enum_value(entity.entity_kind),
        field_count=len(entity.fields),
        primary_key_fields=list(entity.primary_key_fields),
        fields=[field_response(field) for field in entity.fields],
    )


def relationship_response(relationship: Any) -> SchemaRelationshipResponse:
    """Serialize one ``SourceRelationship``.

    Previously only the relationship COUNT was exposed, which left a consumer
    able to see that an ERP had relationships but not what they were.
    """
    return SchemaRelationshipResponse(
        relationship_id=relationship.relationship_id,
        relationship_type=_enum_value(relationship.relationship_type),
        from_entity=relationship.from_entity,
        from_fields=list(relationship.from_fields),
        to_entity=relationship.to_entity,
        to_fields=list(relationship.to_fields),
        confidence=relationship.confidence,
    )


def schema_response(schema: Any) -> SchemaResponse:
    """Serialize a whole ``SourceSchema`` for the HTTP contract."""
    relationships = tuple(getattr(schema, "relationships", ()) or ())

    return SchemaResponse(
        schema_id=schema.schema_id,
        source_system_id=schema.source_system_id,
        schema_name=schema.schema_name,
        origin=_enum_value(getattr(schema, "origin", None)),
        schema_version=getattr(schema, "schema_version", None),
        schema_hash=getattr(schema, "schema_hash", None),
        entities=[entity_response(entity) for entity in schema.entities],
        relationships=[
            relationship_response(relationship) for relationship in relationships
        ],
        relationship_count=len(relationships),
    )


__all__ = [
    "field_response",
    "entity_response",
    "relationship_response",
    "schema_response",
]
