"""Idempotent PostgreSQL DDL for the ``erp_catalog`` schema namespace.

Table design decisions
-----------------------
**Application-assigned identity, never SERIAL.** Every primary key here is a
string the Phase 1 contract already produces (``source_system_id``,
``schema_id``, ``entity_id``, ``normalized_name``, ``relationship_id``,
``mapping_id``) or a small composite of them. A PostgreSQL ``SERIAL`` would
reintroduce exactly the identity instability this project spent an entire
phase eliminating - see ``erp_pipeline.schemas.identity``, and in particular
``require_business_key``.

**Relational structure, not one JSON blob.** ``source_entities``,
``source_fields`` and ``source_relationships`` are proper tables, each row
mirroring one dataclass field-for-field. This is what lets a future phase run
``WHERE normalized_name = ...`` instead of scanning JSON documents, while
``metadata`` (free-form, not queried by name) is JSONB.

**Entity uniqueness is scoped to its snapshot.** ``source_entities`` has
``PRIMARY KEY (schema_id, entity_id)`` and
``UNIQUE (schema_id, normalized_name)`` - never a bare unique on
``entity_id``/``normalized_name`` alone. The same table name in snapshot V1
and snapshot V2 of the same source is two different rows, which is exactly
what makes historical snapshots independently readable.

**Position columns preserve list order.** The Phase 1 models hold
``entities``, ``fields`` and ``relationships`` as ordered sequences, and Task 9
requires that order to survive persistence. ``entity_position`` /
``field_position`` / ``relationship_position`` are catalog bookkeeping only -
distinct from ``SourceField.ordinal``, which is a source-supplied value (e.g. a
CSV column index) that may be absent.

**No ON DELETE CASCADE anywhere.** Historical snapshots must not vanish as a
side effect of deleting something else. The repository in this package
exposes no delete method at all (Task 14); the absence of CASCADE is defense
in depth against a future addition doing the wrong thing by default.

**JSONB usage (Task 28).** Used for ``metadata`` everywhere, and for the small
structural arrays that have no independent query value of their own:
``nested_path``, ``primary_key_fields``, relationship ``from_fields``/
``to_fields``, and field-mapping ``transformations``. Everything a future
phase is likely to filter or join on - entity and field names, types, PK/
unique/array flags, relationship endpoints - is its own column.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    Boolean,
    Column,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.engine import Engine

from erp_pipeline.catalog.config import CATALOG_SCHEMA_NAME

CATALOG_METADATA = MetaData(schema=CATALOG_SCHEMA_NAME)

_JSONB_EMPTY_OBJECT = text("'{}'::jsonb")
_JSONB_EMPTY_ARRAY = text("'[]'::jsonb")
_AWARE_TIMESTAMP = TIMESTAMP(timezone=True)


def _fk(table_column: str) -> ForeignKey:
    """Build a ForeignKey qualified with the catalog schema name."""
    return ForeignKey(f"{CATALOG_SCHEMA_NAME}.{table_column}")


# ============================================================
# source_systems
# ============================================================

source_systems = Table(
    "source_systems",
    CATALOG_METADATA,
    Column("source_system_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("source_type", Text, nullable=False),
    Column("description", Text, nullable=True),
    Column("environment", Text, nullable=True),
    Column("schema_version", Text, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=_JSONB_EMPTY_OBJECT),
    Column("created_at", _AWARE_TIMESTAMP, nullable=False),
    Column("updated_at", _AWARE_TIMESTAMP, nullable=False),
)


# ============================================================
# schema_snapshots - immutable historical record
# ============================================================

schema_snapshots = Table(
    "schema_snapshots",
    CATALOG_METADATA,
    Column("schema_id", Text, primary_key=True),
    Column(
        "source_system_id",
        Text,
        _fk("source_systems.source_system_id"),
        nullable=False,
    ),
    Column("schema_name", Text, nullable=False),
    # Version supplied by / discovered from the source model, if any
    # (SourceSchema.schema_version). NOT the catalog's own version.
    Column("source_schema_version", Text, nullable=False),
    # Monotonically increasing integer this catalog assigns within
    # (source_system_id, schema_name). See versioning.next_catalog_version.
    Column("catalog_version", Integer, nullable=False),
    Column("origin", Text, nullable=False),
    # Recomputed server-side from the persisted structure at save time -
    # never trusted from the caller. See repository.save_schema_snapshot.
    Column("schema_hash", Text, nullable=False),
    Column("model_version", Text, nullable=False),
    Column("discovered_at", _AWARE_TIMESTAMP, nullable=True),
    # SourceSchema.created_at - the timestamp on the object itself.
    Column("created_at", _AWARE_TIMESTAMP, nullable=False),
    # When this catalog row was written. Distinct from created_at: a schema
    # object built yesterday can be published to the catalog today.
    Column(
        "captured_at",
        _AWARE_TIMESTAMP,
        nullable=False,
        server_default=func.now(),
    ),
    Column("metadata", JSONB, nullable=False, server_default=_JSONB_EMPTY_OBJECT),
    UniqueConstraint(
        "source_system_id",
        "schema_name",
        "catalog_version",
        name="uq_schema_snapshots_scope_version",
    ),
    # Latest-snapshot resolution: WHERE source_system_id=... AND schema_name=...
    # ORDER BY catalog_version DESC LIMIT 1.
    Index(
        "ix_schema_snapshots_scope_version",
        "source_system_id",
        "schema_name",
        "catalog_version",
    ),
    # Idempotency check: is this hash already the latest in scope?
    Index(
        "ix_schema_snapshots_scope_hash",
        "source_system_id",
        "schema_name",
        "schema_hash",
    ),
)


# ============================================================
# source_entities - scoped to one schema snapshot
# ============================================================

source_entities = Table(
    "source_entities",
    CATALOG_METADATA,
    Column(
        "schema_id", Text, _fk("schema_snapshots.schema_id"), primary_key=True
    ),
    Column("entity_id", Text, primary_key=True),
    Column("entity_position", Integer, nullable=False),
    Column("source_name", Text, nullable=False),
    Column("normalized_name", Text, nullable=False),
    Column("entity_kind", Text, nullable=False),
    Column("namespace", Text, nullable=True),
    Column(
        "primary_key_fields", JSONB, nullable=False, server_default=_JSONB_EMPTY_ARRAY
    ),
    Column("description", Text, nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=_JSONB_EMPTY_OBJECT),
    # Task 8: the same logical entity name may exist in two different
    # snapshots. Uniqueness is scoped to schema_id, never global.
    UniqueConstraint(
        "schema_id", "normalized_name", name="uq_source_entities_schema_normalized"
    ),
    Index("ix_source_entities_normalized_name", "normalized_name"),
)


# ============================================================
# source_fields - scoped to one entity within one snapshot
# ============================================================

source_fields = Table(
    "source_fields",
    CATALOG_METADATA,
    Column("schema_id", Text, primary_key=True),
    Column("entity_id", Text, primary_key=True),
    Column("normalized_name", Text, primary_key=True),
    Column("field_position", Integer, nullable=False),
    Column("source_name", Text, nullable=False),
    Column("source_data_type", Text, nullable=True),
    Column("normalized_data_type", Text, nullable=False),
    Column("nullable", Boolean, nullable=False),
    Column("required", Boolean, nullable=False),
    Column("is_primary_key", Boolean, nullable=False),
    Column("is_unique", Boolean, nullable=False),
    Column("is_array", Boolean, nullable=False),
    Column("nested_path", JSONB, nullable=True),
    Column("semantic_type", Text, nullable=True),
    Column("description", Text, nullable=True),
    # The model's own optional SourceField.ordinal (e.g. CSV column index).
    # Distinct from field_position, which is catalog list-order bookkeeping.
    Column("field_ordinal", Integer, nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=_JSONB_EMPTY_OBJECT),
    ForeignKeyConstraint(
        ["schema_id", "entity_id"],
        [
            f"{CATALOG_SCHEMA_NAME}.source_entities.schema_id",
            f"{CATALOG_SCHEMA_NAME}.source_entities.entity_id",
        ],
    ),
    Index("ix_source_fields_normalized_name", "normalized_name"),
    Index(
        "ix_source_fields_entity_position", "schema_id", "entity_id", "field_position"
    ),
)


# ============================================================
# source_relationships - scoped to one schema snapshot
# ============================================================

source_relationships = Table(
    "source_relationships",
    CATALOG_METADATA,
    Column(
        "schema_id", Text, _fk("schema_snapshots.schema_id"), primary_key=True
    ),
    Column("relationship_id", Text, primary_key=True),
    Column("relationship_position", Integer, nullable=False),
    Column("relationship_type", Text, nullable=False),
    Column("from_entity", Text, nullable=False),
    Column("to_entity", Text, nullable=False),
    Column("from_fields", JSONB, nullable=False, server_default=_JSONB_EMPTY_ARRAY),
    Column("to_fields", JSONB, nullable=False, server_default=_JSONB_EMPTY_ARRAY),
    Column("confidence", Double, nullable=False),
    Column("description", Text, nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=_JSONB_EMPTY_OBJECT),
    Index("ix_source_relationships_from_entity", "schema_id", "from_entity"),
    Index("ix_source_relationships_to_entity", "schema_id", "to_entity"),
)


# ============================================================
# mapping_profiles / field_mappings
# ============================================================

mapping_profiles = Table(
    "mapping_profiles",
    CATALOG_METADATA,
    Column("mapping_id", Text, primary_key=True),
    Column(
        "source_system_id",
        Text,
        _fk("source_systems.source_system_id"),
        nullable=False,
    ),
    # Task 17: binds a profile to one known schema snapshot. Nullable because
    # the Phase 1 MappingProfile.source_schema_id is itself optional - Phase 2
    # persists that field as-is rather than forcing a binding the contract
    # does not require.
    Column(
        "source_schema_id",
        Text,
        _fk("schema_snapshots.schema_id"),
        nullable=True,
    ),
    Column("source_entity", Text, nullable=False),
    Column("target_entity_type", Text, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("model_version", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("approved_by", Text, nullable=True),
    Column("approved_at", _AWARE_TIMESTAMP, nullable=True),
    Column("created_at", _AWARE_TIMESTAMP, nullable=False),
    Column("updated_at", _AWARE_TIMESTAMP, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=_JSONB_EMPTY_OBJECT),
    Index("ix_mapping_profiles_source", "source_system_id", "source_entity"),
)


field_mappings = Table(
    "field_mappings",
    CATALOG_METADATA,
    Column(
        "mapping_id", Text, _fk("mapping_profiles.mapping_id"), primary_key=True
    ),
    Column("mapping_position", Integer, primary_key=True),
    Column("source_field", Text, nullable=False),
    Column("target_field", Text, nullable=False),
    Column("source_type", Text, nullable=True),
    Column("target_type", Text, nullable=True),
    # A TransformationRule list, stored as JSONB (Task 28 explicitly allows
    # this for transformation rule configs). Each element is
    # {"operation": ..., "config": {...}, "description": ...}; never executed,
    # only ever round-tripped.
    Column(
        "transformations", JSONB, nullable=False, server_default=_JSONB_EMPTY_ARRAY
    ),
    Column("confidence", Double, nullable=False),
    Column("status", Text, nullable=False),
    Column("reason", Text, nullable=True),
    Column("metadata", JSONB, nullable=False, server_default=_JSONB_EMPTY_OBJECT),
    Index("ix_field_mappings_source_field", "mapping_id", "source_field"),
)


ALL_TABLE_NAMES: tuple[str, ...] = (
    "source_systems",
    "schema_snapshots",
    "source_entities",
    "source_fields",
    "source_relationships",
    "mapping_profiles",
    "field_mappings",
)


@dataclass(frozen=True)
class BootstrapReport:
    """What ``bootstrap_catalog`` actually verified, not an assumption."""

    schema_name: str
    tables_present: tuple[str, ...]
    tables_missing: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.tables_missing

    def render(self) -> str:
        lines = [f"Catalog schema: {self.schema_name}"]
        lines.append(f"Tables present ({len(self.tables_present)}):")
        for name in self.tables_present:
            lines.append(f"  - {name}")
        if self.tables_missing:
            lines.append(f"Tables MISSING ({len(self.tables_missing)}):")
            for name in self.tables_missing:
                lines.append(f"  - {name}")
        return "\n".join(lines)


def bootstrap_catalog(engine: Engine) -> BootstrapReport:
    """Create the ``erp_catalog`` schema and every table, idempotently.

    Safe to run any number of times: existing tables and their data are left
    untouched (``create_all`` reflects what already exists and only creates
    what is missing; nothing is ever dropped or recreated). Running this twice
    in a row produces the same report both times.
    """
    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {CATALOG_SCHEMA_NAME}"))

    CATALOG_METADATA.create_all(engine, checkfirst=True)

    inspector = inspect(engine)
    existing = set(inspector.get_table_names(schema=CATALOG_SCHEMA_NAME))

    return BootstrapReport(
        schema_name=CATALOG_SCHEMA_NAME,
        tables_present=tuple(name for name in ALL_TABLE_NAMES if name in existing),
        tables_missing=tuple(name for name in ALL_TABLE_NAMES if name not in existing),
    )


def main() -> int:
    """``python -m erp_pipeline.catalog.schema`` - Task 25 bootstrap entry point."""
    from erp_pipeline.catalog.config import CatalogDatabaseSettings

    settings = CatalogDatabaseSettings.from_env()
    print(f"Bootstrapping catalog schema on {settings.safe_target} ...")

    engine = settings.create_engine()
    try:
        report = bootstrap_catalog(engine)
    finally:
        engine.dispose()

    print(report.render())
    print("PASS" if report.is_complete else "FAIL: tables missing after bootstrap")
    return 0 if report.is_complete else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())


__all__ = [
    "CATALOG_METADATA",
    "ALL_TABLE_NAMES",
    "BootstrapReport",
    "bootstrap_catalog",
    "source_systems",
    "schema_snapshots",
    "source_entities",
    "source_fields",
    "source_relationships",
    "mapping_profiles",
    "field_mappings",
]
