"""Pure catalog-version arithmetic, schema comparison and summarization.

Nothing in this module touches PostgreSQL or does any other I/O. It operates
entirely on already-loaded ``SourceSystem`` / ``SourceSchema`` objects, which
is what makes it fully unit-testable without a database (Task 30).

Scope
-----
``compare_schemas`` answers "what changed structurally between two schema
snapshots?" using only the two ``SourceSchema`` objects. It never consults
timestamps - two schemas differing only in ``created_at`` compare as
identical, matching ``SourceSchema.compute_schema_hash()``'s own exclusion of
timestamps.

Rename detection is deliberately conservative (Task 20). A generic schema
system watching arbitrary, unfamiliar sources cannot know that a removed field
and an added field are "the same field renamed" - that is a claim about
intent, which only a human or a source-specific heuristic can make reliably.
This module only ever reports a *possible_rename_candidate*, never a
confirmed rename, and only when the evidence is unambiguous: exactly one field
removed and exactly one field added *in the same entity*, with matching
normalized type and flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema
from erp_pipeline.schemas.source_models import SourceSystem


# ============================================================
# Catalog version arithmetic
# ============================================================

def next_catalog_version(current_latest: int | None) -> int:
    """The catalog version to assign to a new, structurally different snapshot.

    ``current_latest`` is ``None`` when the (source_system_id, schema_name)
    scope has no snapshot yet, which starts the sequence at 1.
    """
    if current_latest is None:
        return 1

    if current_latest < 1:
        raise ValueError(
            f"current_latest catalog version must be a positive integer, got "
            f"{current_latest}."
        )

    return current_latest + 1


def is_identical_content(existing_hash: str, candidate_hash: str) -> bool:
    """True when two structural hashes represent the same schema content.

    Trivial by itself, but named so the idempotency decision in
    ``repository.save_schema_snapshot`` reads as intent rather than a bare
    ``==``.
    """
    return existing_hash == candidate_hash


# ============================================================
# Schema comparison
# ============================================================

# Field attributes compared for structural change, and whether a change to
# each one is treated as breaking. Deliberately excludes `description` and
# `metadata`, which are documentation, not structure.
_COMPARED_FIELD_ATTRIBUTES: tuple[str, ...] = (
    "source_data_type",
    "normalized_data_type",
    "nullable",
    "required",
    "is_primary_key",
    "is_unique",
    "is_array",
    "nested_path",
    "semantic_type",
)


def _field_attribute_value(source_field: SourceField, attribute: str) -> Any:
    value = getattr(source_field, attribute)
    # Enum members and tuples must compare and print predictably regardless
    # of which SourceSchema instance produced them.
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    return value


def _relationship_key(relationship) -> tuple[Any, ...]:
    """Structural identity of a relationship, ignoring its assigned id.

    Mirrors ``SourceSchema.compute_schema_hash()``, which also hashes
    relationships by structure rather than by ``relationship_id`` - a fresh
    discovery run may mint a new id for the same real-world relationship, and
    that must not register as "removed one, added another."
    """
    return (
        relationship.relationship_type.value,
        relationship.from_entity,
        tuple(relationship.from_fields),
        relationship.to_entity,
        tuple(relationship.to_fields),
    )


class BreakingLevel:
    """String constants for ``SchemaDiff.breaking_level``.

    Plain string constants rather than an ``erp_pipeline.schemas`` enum: this
    classification is a catalog-layer *analysis result*, not a source-of-truth
    contract value persisted on a model, so it does not belong in the Phase 1
    vocabulary that ``schemas/enums.py`` owns.
    """

    NON_BREAKING = "non_breaking"
    POTENTIALLY_BREAKING = "potentially_breaking"
    BREAKING = "breaking"

    _ORDER = {NON_BREAKING: 0, POTENTIALLY_BREAKING: 1, BREAKING: 2}

    @classmethod
    def max(cls, a: str, b: str) -> str:
        return a if cls._ORDER[a] >= cls._ORDER[b] else b


@dataclass(frozen=True)
class FieldChange:
    """One structural attribute that differs for a field present in both schemas."""

    entity: str
    field: str
    attribute: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True)
class RenameCandidate:
    """A conservative, unconfirmed guess that a field was renamed in place.

    ``reason`` states exactly what evidence produced the guess, since the
    evidence is the whole justification for treating this as a candidate
    rather than as an unrelated add + remove.
    """

    entity: str
    removed_field: str
    added_field: str
    reason: str


@dataclass(frozen=True)
class SchemaDiff:
    """Structural difference between two ``SourceSchema`` snapshots."""

    old_schema_id: str
    new_schema_id: str
    added_entities: tuple[str, ...] = field(default_factory=tuple)
    removed_entities: tuple[str, ...] = field(default_factory=tuple)
    added_fields: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    removed_fields: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    changed_fields: tuple[FieldChange, ...] = field(default_factory=tuple)
    added_relationships: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    removed_relationships: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    possible_rename_candidates: tuple[RenameCandidate, ...] = field(default_factory=tuple)
    breaking_level: str = BreakingLevel.NON_BREAKING
    breaking_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_structural_changes(self) -> bool:
        return bool(
            self.added_entities
            or self.removed_entities
            or self.added_fields
            or self.removed_fields
            or self.changed_fields
            or self.added_relationships
            or self.removed_relationships
        )

    def summary(self) -> str:
        lines = [f"Schema diff: {self.old_schema_id} -> {self.new_schema_id}"]
        lines.append(f"Breaking level: {self.breaking_level}")
        lines.append(f"Added entities      : {len(self.added_entities)} {list(self.added_entities)}")
        lines.append(f"Removed entities    : {len(self.removed_entities)} {list(self.removed_entities)}")
        lines.append(f"Added fields        : {len(self.added_fields)} {list(self.added_fields)}")
        lines.append(f"Removed fields      : {len(self.removed_fields)} {list(self.removed_fields)}")
        lines.append(f"Changed fields      : {len(self.changed_fields)}")
        for change in self.changed_fields:
            lines.append(
                f"  {change.entity}.{change.field}.{change.attribute}: "
                f"{change.old_value!r} -> {change.new_value!r}"
            )
        lines.append(
            f"Added relationships : {len(self.added_relationships)}"
        )
        lines.append(
            f"Removed relationships: {len(self.removed_relationships)}"
        )
        lines.append(
            f"Possible rename candidates: {len(self.possible_rename_candidates)}"
        )
        for candidate in self.possible_rename_candidates:
            lines.append(
                f"  {candidate.entity}: {candidate.removed_field!r} -> "
                f"{candidate.added_field!r} ({candidate.reason})"
            )
        if self.breaking_reasons:
            lines.append("Breaking reasons:")
            for reason in self.breaking_reasons:
                lines.append(f"  - {reason}")
        return "\n".join(lines)


def compare_schemas(old: SourceSchema, new: SourceSchema) -> SchemaDiff:
    """Compute the structural difference between two schema snapshots.

    Comparison keys are the same *meaningful* identities the schemas
    themselves use elsewhere: entities and fields by ``normalized_name``,
    relationships by structure. Nothing here reads either schema's timestamp.
    """
    old_entities = {entity.normalized_name: entity for entity in old.entities}
    new_entities = {entity.normalized_name: entity for entity in new.entities}

    added_entity_names = sorted(set(new_entities) - set(old_entities))
    removed_entity_names = sorted(set(old_entities) - set(new_entities))
    common_entity_names = sorted(set(old_entities) & set(new_entities))

    added_fields: list[tuple[str, str]] = []
    removed_fields: list[tuple[str, str]] = []
    changed_fields: list[FieldChange] = []
    rename_candidates: list[RenameCandidate] = []
    breaking_reasons: list[str] = []
    breaking_level = BreakingLevel.NON_BREAKING

    if removed_entity_names:
        breaking_level = BreakingLevel.max(breaking_level, BreakingLevel.BREAKING)
        breaking_reasons.append(
            f"{len(removed_entity_names)} entity(ies) removed: {removed_entity_names}"
        )

    for entity_name in common_entity_names:
        old_entity = old_entities[entity_name]
        new_entity = new_entities[entity_name]

        old_fields = {f.normalized_name: f for f in old_entity.fields}
        new_fields = {f.normalized_name: f for f in new_entity.fields}

        entity_added = sorted(set(new_fields) - set(old_fields))
        entity_removed = sorted(set(old_fields) - set(new_fields))
        entity_common = sorted(set(old_fields) & set(new_fields))

        added_fields.extend((entity_name, name) for name in entity_added)
        removed_fields.extend((entity_name, name) for name in entity_removed)

        for field_name in entity_common:
            old_field = old_fields[field_name]
            new_field = new_fields[field_name]

            for attribute in _COMPARED_FIELD_ATTRIBUTES:
                old_value = _field_attribute_value(old_field, attribute)
                new_value = _field_attribute_value(new_field, attribute)

                if old_value != new_value:
                    changed_fields.append(
                        FieldChange(
                            entity=entity_name,
                            field=field_name,
                            attribute=attribute,
                            old_value=old_value,
                            new_value=new_value,
                        )
                    )

        # Task 20: report a rename candidate only under unambiguous evidence -
        # exactly one field removed and exactly one added in the SAME entity,
        # agreeing on normalized type and array-ness. Anything less certain is
        # left as plain added + removed.
        if len(entity_removed) == 1 and len(entity_added) == 1:
            removed_field = old_fields[entity_removed[0]]
            added_field = new_fields[entity_added[0]]

            if (
                removed_field.normalized_data_type == added_field.normalized_data_type
                and removed_field.is_array == added_field.is_array
            ):
                rename_candidates.append(
                    RenameCandidate(
                        entity=entity_name,
                        removed_field=removed_field.normalized_name,
                        added_field=added_field.normalized_name,
                        reason=(
                            "exactly one field removed and one added in this "
                            f"entity, both {removed_field.normalized_data_type.value}"
                            f"{' array' if removed_field.is_array else ''}"
                        ),
                    )
                )

        if entity_removed:
            breaking_level = BreakingLevel.max(breaking_level, BreakingLevel.BREAKING)
            breaking_reasons.append(
                f"{entity_name}: {len(entity_removed)} field(s) removed: {entity_removed}"
            )

        for name in entity_added:
            added_field = new_fields[name]
            if added_field.required and not added_field.nullable:
                breaking_level = BreakingLevel.max(
                    breaking_level, BreakingLevel.POTENTIALLY_BREAKING
                )
                breaking_reasons.append(
                    f"{entity_name}.{name}: new required, non-nullable field added "
                    "with no default known to the catalog"
                )

        for change in changed_fields:
            if change.entity != entity_name:
                continue

            if change.attribute == "normalized_data_type":
                breaking_level = BreakingLevel.max(breaking_level, BreakingLevel.BREAKING)
                breaking_reasons.append(
                    f"{entity_name}.{change.field}: normalized_data_type changed "
                    f"{change.old_value!r} -> {change.new_value!r}"
                )
            elif change.attribute == "is_primary_key":
                breaking_level = BreakingLevel.max(breaking_level, BreakingLevel.BREAKING)
                breaking_reasons.append(
                    f"{entity_name}.{change.field}: primary-key status changed "
                    f"{change.old_value!r} -> {change.new_value!r}"
                )
            elif change.attribute == "nullable" and change.old_value and not change.new_value:
                breaking_level = BreakingLevel.max(
                    breaking_level, BreakingLevel.POTENTIALLY_BREAKING
                )
                breaking_reasons.append(
                    f"{entity_name}.{change.field}: became non-nullable; existing "
                    "data may violate this"
                )

    old_relationships = {_relationship_key(r): r for r in old.relationships}
    new_relationships = {_relationship_key(r): r for r in new.relationships}

    added_relationship_keys = sorted(
        set(new_relationships) - set(old_relationships), key=str
    )
    removed_relationship_keys = sorted(
        set(old_relationships) - set(new_relationships), key=str
    )

    added_relationships = tuple(
        (new_relationships[key].from_entity, new_relationships[key].to_entity)
        for key in added_relationship_keys
    )
    removed_relationships = tuple(
        (old_relationships[key].from_entity, old_relationships[key].to_entity)
        for key in removed_relationship_keys
    )
    # A removed relationship narrows what a consumer can join on; treated as
    # potentially breaking, not automatically breaking, since a relationship
    # by itself carries no data a removed field or entity would.
    if removed_relationship_keys:
        breaking_level = BreakingLevel.max(
            breaking_level, BreakingLevel.POTENTIALLY_BREAKING
        )
        breaking_reasons.append(
            f"{len(removed_relationship_keys)} relationship(s) removed"
        )

    return SchemaDiff(
        old_schema_id=old.schema_id,
        new_schema_id=new.schema_id,
        added_entities=tuple(added_entity_names),
        removed_entities=tuple(removed_entity_names),
        added_fields=tuple(added_fields),
        removed_fields=tuple(removed_fields),
        changed_fields=tuple(changed_fields),
        added_relationships=added_relationships,
        removed_relationships=removed_relationships,
        possible_rename_candidates=tuple(rename_candidates),
        breaking_level=breaking_level,
        breaking_reasons=tuple(breaking_reasons),
    )


# ============================================================
# Snapshot summary (Task 22) - calculated, never hardcoded
# ============================================================

@dataclass(frozen=True)
class SchemaSnapshotSummary:
    """A human-readable summary computed from actual schema content."""

    source_system_id: str
    source_system_name: str
    source_type: str
    schema_name: str
    catalog_version: int
    entity_count: int
    field_count: int
    relationship_count: int
    schema_hash: str

    def render(self) -> str:
        return (
            f"Source System:\n{self.source_system_name}\n\n"
            f"Source Type:\n{self.source_type}\n\n"
            f"Schema:\n{self.schema_name}\n\n"
            f"Catalog Version:\n{self.catalog_version}\n\n"
            f"Entities:\n{self.entity_count}\n\n"
            f"Fields:\n{self.field_count}\n\n"
            f"Relationships:\n{self.relationship_count}\n\n"
            f"Schema Hash:\n{self.schema_hash}\n"
        )


def summarize_schema(
    source_system: SourceSystem,
    schema: SourceSchema,
    catalog_version: int,
) -> SchemaSnapshotSummary:
    """Build a snapshot summary from real schema content.

    ``entity_count`` / ``field_count`` / ``relationship_count`` are counted
    directly from ``schema.entities`` / their ``fields`` / ``schema.
    relationships`` - never a stored or assumed number. Calling this against a
    ``SourceSchema`` freshly returned by ``CatalogRepository.get_schema_
    snapshot`` proves the counts reflect what was actually persisted.
    """
    field_count = sum(len(entity.fields) for entity in schema.entities)

    return SchemaSnapshotSummary(
        source_system_id=source_system.source_system_id,
        source_system_name=source_system.name,
        source_type=source_system.source_type.value,
        schema_name=schema.schema_name,
        catalog_version=catalog_version,
        entity_count=len(schema.entities),
        field_count=field_count,
        relationship_count=len(schema.relationships),
        schema_hash=schema.schema_hash or schema.compute_schema_hash(),
    )


__all__ = [
    "next_catalog_version",
    "is_identical_content",
    "BreakingLevel",
    "FieldChange",
    "RenameCandidate",
    "SchemaDiff",
    "compare_schemas",
    "SchemaSnapshotSummary",
    "summarize_schema",
]
