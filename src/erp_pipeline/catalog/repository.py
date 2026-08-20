"""SQLAlchemy Core persistence for the schema catalog.

Implementation choice: SQLAlchemy Core, not the ORM (Task 27)
---------------------------------------------------------------
The Phase 1 dataclasses (``SourceSystem``, ``SourceSchema``, ``MappingProfile``,
...) already ARE the domain model - validated, serializable, tested. An ORM
declarative layer would define a *second*, parallel set of classes mapped to
these same tables, and every change to a Phase 1 contract would then require
updating two model hierarchies in lockstep or watching them drift apart.

Core gives everything persistence actually needs - parameterized statements,
transactions, connection pooling, JSONB support - without that duplication.
Every method in this module does the same two things: turn a Phase 1 model
into column values on the way in, and turn column values back into a Phase 1
model (via its ordinary constructor - the model's own validation runs again on
the way out, which is a feature, not overhead) on the way out. Database rows
are a persistence *representation* of the domain model, never a second model.

Transactions
------------
Every method that writes uses exactly one ``engine.begin()`` block. SQLAlchemy
commits on normal exit and rolls back automatically on any exception raised
inside - including this module's own domain errors - so a snapshot save can
never leave a partial ``schema_snapshots`` row with no matching entities, or
entities with no fields (Task 13).

No delete API
-------------
This module exposes no delete method for any table. Historical schema
snapshots are immutable once written (Task 6); the absence of a delete path is
enforced structurally, not just by convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from erp_pipeline.catalog import schema as catalog_schema
from erp_pipeline.catalog.exceptions import (
    CatalogConnectionError,
    CatalogIntegrityError,
    MappingProfileNotFoundError,
    SchemaIdentityConflictError,
    SchemaSnapshotNotFoundError,
    SourceSystemIdentityConflictError,
    SourceSystemNotFoundError,
)
from erp_pipeline.catalog.versioning import is_identical_content, next_catalog_version
from erp_pipeline.schemas.mapping_models import FieldMapping, MappingProfile, TransformationRule
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
    SourceSystem,
)


def _translate(exc: Exception, context: str) -> Exception:
    """Map a driver/SQLAlchemy exception to a domain CatalogError.

    ``__cause__`` is preserved by the caller's ``raise ... from exc``, so the
    original exception is always available for debugging even though the
    public API only ever raises catalog-domain errors.
    """
    if isinstance(exc, IntegrityError):
        return CatalogIntegrityError(
            f"{context}: a catalog integrity constraint was violated ({exc.orig!r})."
        )
    if isinstance(exc, OperationalError):
        return CatalogConnectionError(
            f"{context}: could not reach the catalog database ({exc.orig!r})."
        )
    return exc


@dataclass(frozen=True)
class SchemaSnapshotRecord:
    """Catalog-level metadata about one snapshot, without its full content.

    Cheap to fetch and list. Use ``CatalogRepository.get_schema_snapshot`` to
    reconstruct the full ``SourceSchema`` when the content itself is needed.
    """

    schema_id: str
    source_system_id: str
    schema_name: str
    source_schema_version: str
    catalog_version: int
    origin: str
    schema_hash: str
    model_version: str
    discovered_at: datetime | None
    created_at: datetime
    captured_at: datetime
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class SchemaSnapshotResult:
    """The outcome of ``save_schema_snapshot``.

    ``created`` is the idempotency signal Task 6 requires callers be able to
    observe: ``False`` means the save was a no-op because an equivalent
    snapshot already existed (whatever ``schema_id`` the caller supplied is
    reflected in ``requested_schema_id``, while ``record`` describes the
    snapshot actually on file).
    """

    record: SchemaSnapshotRecord
    created: bool
    requested_schema_id: str


class CatalogRepository:
    """Persistence for the schema catalog. No raw SQL crosses this boundary."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ============================================================
    # Source systems (Task 3, 4)
    # ============================================================

    def save_source_system(self, source_system: SourceSystem) -> SourceSystem:
        """UPSERT a source system, idempotently.

        Registering the same ``source_system_id`` again updates only
        descriptive fields (``name``, ``description``, ``environment``,
        ``schema_version``, ``metadata``, ``updated_at``); ``created_at`` and
        ``source_type`` are never touched by an update.

        Raises ``SourceSystemIdentityConflictError`` if the stored
        ``source_type`` would change - ``source_system_id`` identifies one
        source technology for its lifetime, and every schema snapshot filed
        under it assumes that technology never changed underneath it.
        """
        try:
            with self._engine.begin() as connection:
                existing = connection.execute(
                    select(catalog_schema.source_systems).where(
                        catalog_schema.source_systems.c.source_system_id
                        == source_system.source_system_id
                    )
                ).mappings().first()

                if existing is not None:
                    if existing["source_type"] != source_system.source_type.value:
                        raise SourceSystemIdentityConflictError(
                            f"SourceSystem {source_system.source_system_id!r} is "
                            f"already registered with source_type="
                            f"{existing['source_type']!r}; refusing to change it to "
                            f"{source_system.source_type.value!r}. Register a "
                            "different source_system_id for the new technology."
                        )

                    connection.execute(
                        update(catalog_schema.source_systems)
                        .where(
                            catalog_schema.source_systems.c.source_system_id
                            == source_system.source_system_id
                        )
                        .values(
                            name=source_system.name,
                            description=source_system.description,
                            environment=source_system.environment,
                            schema_version=source_system.schema_version,
                            metadata=dict(source_system.metadata),
                            updated_at=source_system.updated_at,
                        )
                    )
                else:
                    connection.execute(
                        insert(catalog_schema.source_systems).values(
                            source_system_id=source_system.source_system_id,
                            name=source_system.name,
                            source_type=source_system.source_type.value,
                            description=source_system.description,
                            environment=source_system.environment,
                            schema_version=source_system.schema_version,
                            metadata=dict(source_system.metadata),
                            created_at=source_system.created_at,
                            updated_at=source_system.updated_at,
                        )
                    )
        except (IntegrityError, OperationalError) as exc:
            raise _translate(exc, "save_source_system") from exc

        return self.get_source_system(source_system.source_system_id)

    def get_source_system(self, source_system_id: str) -> SourceSystem:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    select(catalog_schema.source_systems).where(
                        catalog_schema.source_systems.c.source_system_id
                        == source_system_id
                    )
                ).mappings().first()
        except OperationalError as exc:
            raise _translate(exc, "get_source_system") from exc

        if row is None:
            raise SourceSystemNotFoundError(
                f"No source system is registered with id {source_system_id!r}."
            )

        return self._source_system_from_row(row)

    def list_source_systems(
        self, source_type: str | None = None
    ) -> tuple[SourceSystem, ...]:
        statement = select(catalog_schema.source_systems).order_by(
            catalog_schema.source_systems.c.source_system_id
        )
        if source_type is not None:
            value = getattr(source_type, "value", source_type)
            statement = statement.where(
                catalog_schema.source_systems.c.source_type == value
            )

        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except OperationalError as exc:
            raise _translate(exc, "list_source_systems") from exc

        return tuple(self._source_system_from_row(row) for row in rows)

    @staticmethod
    def _source_system_from_row(row: Mapping[str, Any]) -> SourceSystem:
        return SourceSystem(
            source_system_id=row["source_system_id"],
            name=row["name"],
            source_type=row["source_type"],
            description=row["description"],
            environment=row["environment"],
            schema_version=row["schema_version"],
            metadata=row["metadata"] or {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ============================================================
    # Schema snapshots (Task 5, 6, 7, 8, 9, 10, 11, 13)
    # ============================================================

    def save_schema_snapshot(self, source_schema: SourceSchema) -> SchemaSnapshotResult:
        """Persist one immutable schema snapshot, atomically.

        Algorithm (Task 6):

        1. Recompute the structural hash server-side - a caller-supplied
           ``schema_hash`` on the object is never trusted (Task 7).
        2. If this exact ``schema_id`` already has a row: its stored hash must
           match the recomputed one, or ``SchemaIdentityConflictError`` is
           raised (a ``schema_id`` may never be reused for different
           content). If it matches, this is a pure re-save of the same
           object - return the existing snapshot, no new version.
        3. Otherwise, compare against the latest snapshot in the
           ``(source_system_id, schema_name)`` scope. If its hash matches the
           incoming one, the caller is presenting structurally identical
           content under a fresh ``schema_id`` - deduplicate: return the
           existing latest snapshot, discard the new id, no new version.
        4. Otherwise this is genuinely new content: assign
           ``catalog_version = latest + 1`` (or ``1`` if the scope is empty)
           and insert the snapshot together with every entity, field and
           relationship in one transaction.
        """
        computed_hash = source_schema.compute_schema_hash()

        try:
            with self._engine.begin() as connection:
                system_exists = connection.execute(
                    select(catalog_schema.source_systems.c.source_system_id).where(
                        catalog_schema.source_systems.c.source_system_id
                        == source_schema.source_system_id
                    )
                ).first()
                if system_exists is None:
                    raise SourceSystemNotFoundError(
                        f"Cannot save schema {source_schema.schema_id!r}: source "
                        f"system {source_schema.source_system_id!r} is not "
                        "registered. Call save_source_system() first."
                    )

                existing_row = connection.execute(
                    select(catalog_schema.schema_snapshots).where(
                        catalog_schema.schema_snapshots.c.schema_id
                        == source_schema.schema_id
                    )
                ).mappings().first()

                if existing_row is not None:
                    if existing_row["schema_hash"] != computed_hash:
                        raise SchemaIdentityConflictError(
                            f"schema_id {source_schema.schema_id!r} is already "
                            f"recorded with structural hash "
                            f"{existing_row['schema_hash']!r}, but the schema being "
                            f"saved now hashes to {computed_hash!r}. A schema_id is "
                            "an immutable identity - give changed content a new "
                            "schema_id instead of reusing this one."
                        )
                    return SchemaSnapshotResult(
                        record=self._snapshot_record_from_row(existing_row),
                        created=False,
                        requested_schema_id=source_schema.schema_id,
                    )

                latest_row = connection.execute(
                    select(catalog_schema.schema_snapshots)
                    .where(
                        catalog_schema.schema_snapshots.c.source_system_id
                        == source_schema.source_system_id,
                        catalog_schema.schema_snapshots.c.schema_name
                        == source_schema.schema_name,
                    )
                    .order_by(catalog_schema.schema_snapshots.c.catalog_version.desc())
                    .limit(1)
                ).mappings().first()

                if latest_row is not None and is_identical_content(
                    latest_row["schema_hash"], computed_hash
                ):
                    return SchemaSnapshotResult(
                        record=self._snapshot_record_from_row(latest_row),
                        created=False,
                        requested_schema_id=source_schema.schema_id,
                    )

                new_version = next_catalog_version(
                    latest_row["catalog_version"] if latest_row is not None else None
                )

                connection.execute(
                    insert(catalog_schema.schema_snapshots).values(
                        schema_id=source_schema.schema_id,
                        source_system_id=source_schema.source_system_id,
                        schema_name=source_schema.schema_name,
                        source_schema_version=source_schema.schema_version,
                        catalog_version=new_version,
                        origin=source_schema.origin.value,
                        schema_hash=computed_hash,
                        model_version=source_schema.model_version,
                        discovered_at=source_schema.discovered_at,
                        created_at=source_schema.created_at,
                        metadata=dict(source_schema.metadata),
                    )
                )

                self._insert_entities_and_fields(connection, source_schema)
                self._insert_relationships(connection, source_schema)

                new_row = connection.execute(
                    select(catalog_schema.schema_snapshots).where(
                        catalog_schema.schema_snapshots.c.schema_id
                        == source_schema.schema_id
                    )
                ).mappings().first()

                return SchemaSnapshotResult(
                    record=self._snapshot_record_from_row(new_row),
                    created=True,
                    requested_schema_id=source_schema.schema_id,
                )
        except (SourceSystemNotFoundError, SchemaIdentityConflictError):
            raise
        except (IntegrityError, OperationalError) as exc:
            raise _translate(exc, "save_schema_snapshot") from exc

    @staticmethod
    def _insert_entities_and_fields(connection, source_schema: SourceSchema) -> None:
        entity_rows = []
        field_rows = []

        for entity_position, entity in enumerate(source_schema.entities):
            entity_rows.append(
                dict(
                    schema_id=source_schema.schema_id,
                    entity_id=entity.entity_id,
                    entity_position=entity_position,
                    source_name=entity.source_name,
                    normalized_name=entity.normalized_name,
                    entity_kind=entity.entity_kind.value,
                    namespace=entity.namespace,
                    primary_key_fields=list(entity.primary_key_fields),
                    description=entity.description,
                    metadata=dict(entity.metadata),
                )
            )

            for field_position, source_field in enumerate(entity.fields):
                field_rows.append(
                    dict(
                        schema_id=source_schema.schema_id,
                        entity_id=entity.entity_id,
                        normalized_name=source_field.normalized_name,
                        field_position=field_position,
                        source_name=source_field.source_name,
                        source_data_type=source_field.source_data_type,
                        normalized_data_type=source_field.normalized_data_type.value,
                        nullable=source_field.nullable,
                        required=source_field.required,
                        is_primary_key=source_field.is_primary_key,
                        is_unique=source_field.is_unique,
                        is_array=source_field.is_array,
                        nested_path=(
                            list(source_field.nested_path)
                            if source_field.nested_path is not None
                            else None
                        ),
                        semantic_type=source_field.semantic_type,
                        description=source_field.description,
                        field_ordinal=source_field.ordinal,
                        metadata=dict(source_field.metadata),
                    )
                )

        if entity_rows:
            connection.execute(insert(catalog_schema.source_entities), entity_rows)
        if field_rows:
            connection.execute(insert(catalog_schema.source_fields), field_rows)

    @staticmethod
    def _insert_relationships(connection, source_schema: SourceSchema) -> None:
        relationship_rows = [
            dict(
                schema_id=source_schema.schema_id,
                relationship_id=relationship.relationship_id,
                relationship_position=position,
                relationship_type=relationship.relationship_type.value,
                from_entity=relationship.from_entity,
                to_entity=relationship.to_entity,
                from_fields=list(relationship.from_fields),
                to_fields=list(relationship.to_fields),
                confidence=relationship.confidence,
                description=relationship.description,
                metadata=dict(relationship.metadata),
            )
            for position, relationship in enumerate(source_schema.relationships)
        ]

        if relationship_rows:
            connection.execute(insert(catalog_schema.source_relationships), relationship_rows)

    def get_schema_snapshot(self, schema_id: str) -> SourceSchema:
        """Fully reconstruct a ``SourceSchema`` - the lossless round-trip target."""
        try:
            with self._engine.connect() as connection:
                snapshot_row = connection.execute(
                    select(catalog_schema.schema_snapshots).where(
                        catalog_schema.schema_snapshots.c.schema_id == schema_id
                    )
                ).mappings().first()

                if snapshot_row is None:
                    raise SchemaSnapshotNotFoundError(
                        f"No schema snapshot found with schema_id {schema_id!r}."
                    )

                entity_rows = connection.execute(
                    select(catalog_schema.source_entities)
                    .where(catalog_schema.source_entities.c.schema_id == schema_id)
                    .order_by(catalog_schema.source_entities.c.entity_position)
                ).mappings().all()

                field_rows = connection.execute(
                    select(catalog_schema.source_fields)
                    .where(catalog_schema.source_fields.c.schema_id == schema_id)
                    .order_by(
                        catalog_schema.source_fields.c.entity_id,
                        catalog_schema.source_fields.c.field_position,
                    )
                ).mappings().all()

                relationship_rows = connection.execute(
                    select(catalog_schema.source_relationships)
                    .where(catalog_schema.source_relationships.c.schema_id == schema_id)
                    .order_by(catalog_schema.source_relationships.c.relationship_position)
                ).mappings().all()
        except SchemaSnapshotNotFoundError:
            raise
        except OperationalError as exc:
            raise _translate(exc, "get_schema_snapshot") from exc

        fields_by_entity: dict[str, list[SourceField]] = {}
        for row in field_rows:
            fields_by_entity.setdefault(row["entity_id"], []).append(
                SourceField(
                    source_name=row["source_name"],
                    normalized_name=row["normalized_name"],
                    source_data_type=row["source_data_type"],
                    normalized_data_type=row["normalized_data_type"],
                    nullable=row["nullable"],
                    required=row["required"],
                    is_primary_key=row["is_primary_key"],
                    is_unique=row["is_unique"],
                    is_array=row["is_array"],
                    nested_path=(
                        tuple(row["nested_path"]) if row["nested_path"] else None
                    ),
                    semantic_type=row["semantic_type"],
                    description=row["description"],
                    ordinal=row["field_ordinal"],
                    metadata=row["metadata"] or {},
                )
            )

        entities = tuple(
            SourceEntity(
                entity_id=row["entity_id"],
                source_name=row["source_name"],
                normalized_name=row["normalized_name"],
                entity_kind=row["entity_kind"],
                namespace=row["namespace"],
                fields=tuple(fields_by_entity.get(row["entity_id"], ())),
                primary_key_fields=tuple(row["primary_key_fields"] or ()),
                description=row["description"],
                metadata=row["metadata"] or {},
            )
            for row in entity_rows
        )

        relationships = tuple(
            SourceRelationship(
                relationship_id=row["relationship_id"],
                relationship_type=row["relationship_type"],
                from_entity=row["from_entity"],
                to_entity=row["to_entity"],
                from_fields=tuple(row["from_fields"] or ()),
                to_fields=tuple(row["to_fields"] or ()),
                confidence=row["confidence"],
                description=row["description"],
                metadata=row["metadata"] or {},
            )
            for row in relationship_rows
        )

        return SourceSchema(
            schema_id=snapshot_row["schema_id"],
            source_system_id=snapshot_row["source_system_id"],
            schema_name=snapshot_row["schema_name"],
            origin=snapshot_row["origin"],
            schema_version=snapshot_row["source_schema_version"],
            entities=entities,
            relationships=relationships,
            schema_hash=snapshot_row["schema_hash"],
            model_version=snapshot_row["model_version"],
            discovered_at=snapshot_row["discovered_at"],
            created_at=snapshot_row["created_at"],
            metadata=snapshot_row["metadata"] or {},
        )

    def get_schema_snapshot_record(self, schema_id: str) -> SchemaSnapshotRecord:
        """Cheap catalog-level metadata for one snapshot, no full reconstruction."""
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    select(catalog_schema.schema_snapshots).where(
                        catalog_schema.schema_snapshots.c.schema_id == schema_id
                    )
                ).mappings().first()
        except OperationalError as exc:
            raise _translate(exc, "get_schema_snapshot_record") from exc

        if row is None:
            raise SchemaSnapshotNotFoundError(
                f"No schema snapshot found with schema_id {schema_id!r}."
            )

        return self._snapshot_record_from_row(row)

    def get_latest_schema_record(
        self, source_system_id: str, schema_name: str
    ) -> SchemaSnapshotRecord:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    select(catalog_schema.schema_snapshots)
                    .where(
                        catalog_schema.schema_snapshots.c.source_system_id
                        == source_system_id,
                        catalog_schema.schema_snapshots.c.schema_name == schema_name,
                    )
                    .order_by(catalog_schema.schema_snapshots.c.catalog_version.desc())
                    .limit(1)
                ).mappings().first()
        except OperationalError as exc:
            raise _translate(exc, "get_latest_schema_record") from exc

        if row is None:
            raise SchemaSnapshotNotFoundError(
                f"No schema snapshot found for source_system_id="
                f"{source_system_id!r}, schema_name={schema_name!r}."
            )

        return self._snapshot_record_from_row(row)

    def get_latest_schema(self, source_system_id: str, schema_name: str) -> SourceSchema:
        """Retrieve and fully reconstruct the latest snapshot in a scope."""
        record = self.get_latest_schema_record(source_system_id, schema_name)
        return self.get_schema_snapshot(record.schema_id)

    def list_schema_snapshots(
        self, source_system_id: str, schema_name: str | None = None
    ) -> tuple[SchemaSnapshotRecord, ...]:
        """List snapshot history for a source system, oldest first.

        Returns cheap catalog-level records, not full ``SourceSchema``
        reconstructions - call ``get_schema_snapshot`` for the ones actually
        needed.
        """
        statement = (
            select(catalog_schema.schema_snapshots)
            .where(
                catalog_schema.schema_snapshots.c.source_system_id
                == source_system_id
            )
            .order_by(catalog_schema.schema_snapshots.c.catalog_version)
        )
        if schema_name is not None:
            statement = statement.where(
                catalog_schema.schema_snapshots.c.schema_name == schema_name
            )

        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except OperationalError as exc:
            raise _translate(exc, "list_schema_snapshots") from exc

        return tuple(self._snapshot_record_from_row(row) for row in rows)

    @staticmethod
    def _snapshot_record_from_row(row: Mapping[str, Any]) -> SchemaSnapshotRecord:
        return SchemaSnapshotRecord(
            schema_id=row["schema_id"],
            source_system_id=row["source_system_id"],
            schema_name=row["schema_name"],
            source_schema_version=row["source_schema_version"],
            catalog_version=row["catalog_version"],
            origin=row["origin"],
            schema_hash=row["schema_hash"],
            model_version=row["model_version"],
            discovered_at=row["discovered_at"],
            created_at=row["created_at"],
            captured_at=row["captured_at"],
            metadata=row["metadata"] or {},
        )

    # ============================================================
    # Mapping profiles (Task 16, 17)
    # ============================================================

    def save_mapping_profile(self, profile: MappingProfile) -> MappingProfile:
        """UPSERT a mapping profile and replace its field mappings.

        Unlike schema snapshots, a mapping profile is not modelled as an
        immutable history in Phase 1 - it is a single reviewable document
        identified by ``mapping_id`` that a reviewer edits in place, so saving
        it again with the same ``mapping_id`` replaces its content rather than
        creating a new version. Its own ``schema_version`` field records the
        MappingProfile *contract* version, not a snapshot history.

        Bound to a specific schema snapshot via the Phase 1
        ``source_schema_id`` field exactly as the contract defines it - no
        repository-level binding metadata is added (Task 17).
        """
        try:
            with self._engine.begin() as connection:
                system_exists = connection.execute(
                    select(catalog_schema.source_systems.c.source_system_id).where(
                        catalog_schema.source_systems.c.source_system_id
                        == profile.source_system_id
                    )
                ).first()
                if system_exists is None:
                    raise SourceSystemNotFoundError(
                        f"Cannot save mapping profile {profile.mapping_id!r}: "
                        f"source system {profile.source_system_id!r} is not "
                        "registered. Call save_source_system() first."
                    )

                if profile.source_schema_id is not None:
                    schema_exists = connection.execute(
                        select(catalog_schema.schema_snapshots.c.schema_id).where(
                            catalog_schema.schema_snapshots.c.schema_id
                            == profile.source_schema_id
                        )
                    ).first()
                    if schema_exists is None:
                        raise SchemaSnapshotNotFoundError(
                            f"Cannot save mapping profile {profile.mapping_id!r}: "
                            f"source_schema_id {profile.source_schema_id!r} has no "
                            "catalog snapshot. Save the schema snapshot first, or "
                            "leave source_schema_id unset."
                        )

                values = dict(
                    source_system_id=profile.source_system_id,
                    source_schema_id=profile.source_schema_id,
                    source_entity=profile.source_entity,
                    target_entity_type=profile.target_entity_type,
                    schema_version=profile.schema_version,
                    model_version=profile.model_version,
                    status=profile.status.value,
                    approved_by=profile.approved_by,
                    approved_at=profile.approved_at,
                    created_at=profile.created_at,
                    updated_at=profile.updated_at,
                    metadata=dict(profile.metadata),
                )

                existing = connection.execute(
                    select(catalog_schema.mapping_profiles.c.mapping_id).where(
                        catalog_schema.mapping_profiles.c.mapping_id
                        == profile.mapping_id
                    )
                ).first()

                if existing is not None:
                    connection.execute(
                        update(catalog_schema.mapping_profiles)
                        .where(
                            catalog_schema.mapping_profiles.c.mapping_id
                            == profile.mapping_id
                        )
                        .values(**values)
                    )
                    connection.execute(
                        delete(catalog_schema.field_mappings).where(
                            catalog_schema.field_mappings.c.mapping_id
                            == profile.mapping_id
                        )
                    )
                else:
                    connection.execute(
                        insert(catalog_schema.mapping_profiles).values(
                            mapping_id=profile.mapping_id, **values
                        )
                    )

                field_mapping_rows = [
                    dict(
                        mapping_id=profile.mapping_id,
                        mapping_position=position,
                        source_field=field_mapping.source_field,
                        target_field=field_mapping.target_field,
                        source_type=(
                            field_mapping.source_type.value
                            if field_mapping.source_type
                            else None
                        ),
                        target_type=(
                            field_mapping.target_type.value
                            if field_mapping.target_type
                            else None
                        ),
                        transformations=[
                            {
                                "operation": rule.operation.value,
                                "config": dict(rule.config),
                                "description": rule.description,
                            }
                            for rule in field_mapping.transformations
                        ],
                        confidence=field_mapping.confidence,
                        status=field_mapping.status.value,
                        reason=field_mapping.reason,
                        metadata=dict(field_mapping.metadata),
                    )
                    for position, field_mapping in enumerate(profile.field_mappings)
                ]

                if field_mapping_rows:
                    connection.execute(
                        insert(catalog_schema.field_mappings), field_mapping_rows
                    )
        except (SourceSystemNotFoundError, SchemaSnapshotNotFoundError):
            raise
        except (IntegrityError, OperationalError) as exc:
            raise _translate(exc, "save_mapping_profile") from exc

        return self.get_mapping_profile(profile.mapping_id)

    def get_mapping_profile(self, mapping_id: str) -> MappingProfile:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    select(catalog_schema.mapping_profiles).where(
                        catalog_schema.mapping_profiles.c.mapping_id == mapping_id
                    )
                ).mappings().first()

                if row is None:
                    raise MappingProfileNotFoundError(
                        f"No mapping profile found with mapping_id {mapping_id!r}."
                    )

                field_mapping_rows = connection.execute(
                    select(catalog_schema.field_mappings)
                    .where(catalog_schema.field_mappings.c.mapping_id == mapping_id)
                    .order_by(catalog_schema.field_mappings.c.mapping_position)
                ).mappings().all()
        except MappingProfileNotFoundError:
            raise
        except OperationalError as exc:
            raise _translate(exc, "get_mapping_profile") from exc

        field_mappings_out = tuple(
            FieldMapping(
                source_field=fm_row["source_field"],
                target_field=fm_row["target_field"],
                source_type=fm_row["source_type"],
                target_type=fm_row["target_type"],
                transformations=tuple(
                    TransformationRule(
                        operation=rule["operation"],
                        config=rule.get("config") or {},
                        description=rule.get("description"),
                    )
                    for rule in (fm_row["transformations"] or [])
                ),
                confidence=fm_row["confidence"],
                status=fm_row["status"],
                reason=fm_row["reason"],
                metadata=fm_row["metadata"] or {},
            )
            for fm_row in field_mapping_rows
        )

        return MappingProfile(
            mapping_id=row["mapping_id"],
            source_system_id=row["source_system_id"],
            source_entity=row["source_entity"],
            target_entity_type=row["target_entity_type"],
            source_schema_id=row["source_schema_id"],
            schema_version=row["schema_version"],
            model_version=row["model_version"],
            field_mappings=field_mappings_out,
            status=row["status"],
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=row["metadata"] or {},
        )

    def list_mapping_profiles(
        self, source_system_id: str | None = None
    ) -> tuple[MappingProfile, ...]:
        statement = select(catalog_schema.mapping_profiles.c.mapping_id).order_by(
            catalog_schema.mapping_profiles.c.mapping_id
        )
        if source_system_id is not None:
            statement = statement.where(
                catalog_schema.mapping_profiles.c.source_system_id
                == source_system_id
            )

        try:
            with self._engine.connect() as connection:
                mapping_ids = [
                    row[0] for row in connection.execute(statement).all()
                ]
        except OperationalError as exc:
            raise _translate(exc, "list_mapping_profiles") from exc

        return tuple(self.get_mapping_profile(mapping_id) for mapping_id in mapping_ids)


__all__ = [
    "CatalogRepository",
    "SchemaSnapshotRecord",
    "SchemaSnapshotResult",
]
