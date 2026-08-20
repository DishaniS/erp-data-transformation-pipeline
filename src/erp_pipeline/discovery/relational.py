"""Relational schema discovery - one algorithm, three engines.

Produces the Phase 1 ``SourceSchema`` contract from live PostgreSQL, MySQL
and SQL Server metadata. There is deliberately no ``PostgresTable`` /
``MySQLTable`` / ``SQLServerTable`` public model: all three engines converge
on ``SourceSchema`` -> ``SourceEntity`` -> ``SourceField`` /
``SourceRelationship`` exactly as Phase 1 defined them.

Common abstraction
------------------
``sqlalchemy.inspect(engine)`` is the single introspection layer. SQLAlchemy's
dialects already normalize ``get_schema_names`` / ``get_table_names`` /
``get_columns`` / ``get_pk_constraint`` / ``get_foreign_keys`` /
``get_unique_constraints`` / ``get_indexes`` across all three engines, so no
raw ``information_schema`` or ``sys.*`` SQL is written anywhere in this
module. The only engine-specific data is the system-namespace exclusion list,
centralized in ``discovery.models.SYSTEM_NAMESPACES``, and the namespace
semantics documented in ``_discover_namespaces``.

READ-ONLY
---------
This module issues no DDL and no DML. Every database interaction goes through
an Inspector method, which by construction only reads catalog metadata.
``tests/erp_pipeline/discovery/test_read_only_safety.py`` proves this by
scanning the module source for write SQL.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.connectors.base import BaseSourceConnector
from erp_pipeline.connectors.errors import redact_text
from erp_pipeline.discovery.errors import MetadataInspectionError, UnsupportedDiscoverySourceError
from erp_pipeline.discovery.models import DiscoveryOptions
from erp_pipeline.discovery.type_mapping import normalize_data_type, render_source_data_type
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, RelationshipType, SchemaOrigin, SourceType
from erp_pipeline.schemas.identity import normalize_identifier
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
)

#: The relational engines Phase 4 supports. MongoDB is Phase 5 and is
#: rejected explicitly rather than silently producing an empty schema.
SUPPORTED_SOURCE_TYPES: frozenset[SourceType] = frozenset(
    {SourceType.POSTGRESQL, SourceType.MYSQL, SourceType.SQL_SERVER}
)

#: Placeholder used while building the schema, before its content hash - and
#: therefore its final snapshot id - can be computed. See `_build_schema_id`.
_PROVISIONAL_SCHEMA_ID = "provisional.schema.id"


class RelationalSchemaDiscovery:
    """Discovers relational structure and returns a Phase 1 ``SourceSchema``."""

    def __init__(self, connector: BaseSourceConnector, options: DiscoveryOptions | None = None) -> None:
        self._connector = _require_relational_connector(connector)
        self._options = options or DiscoveryOptions()
        self._warnings: list[str] = []

    @property
    def warnings(self) -> tuple[str, ...]:
        """Non-fatal problems encountered - e.g. a table skipped mid-discovery."""
        return tuple(self._warnings)

    # ------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------

    def discover(self) -> SourceSchema:
        """Introspect the source and build the generic ``SourceSchema``."""
        inspector = self._create_inspector()
        source_type = self._connector.source_type
        database = self._connector._settings.database  # noqa: SLF001 - sanctioned seam

        namespaces = self._discover_namespaces(inspector, source_type)
        entities = self._discover_entities(inspector, namespaces, source_type)
        relationships = self._discover_relationships(inspector, namespaces, entities, source_type)

        schema_name = self._build_schema_name(source_type, database, namespaces)

        # Two-pass build: `compute_schema_hash()` excludes schema_id from its
        # projection, so a provisional id can be used to compute the hash and
        # the final, content-addressed id derived from it. See Step 15/16 and
        # `_build_schema_id` for why the id must incorporate the hash.
        provisional = self._assemble(
            schema_id=_PROVISIONAL_SCHEMA_ID,
            schema_name=schema_name,
            database=database,
            namespaces=namespaces,
            entities=entities,
            relationships=relationships,
            schema_hash=None,
        )
        structural_hash = provisional.compute_schema_hash()

        return self._assemble(
            schema_id=self._build_schema_id(database, schema_name, structural_hash),
            schema_name=schema_name,
            database=database,
            namespaces=namespaces,
            entities=entities,
            relationships=relationships,
            schema_hash=structural_hash,
        )

    # ------------------------------------------------------------
    # Inspector access (Phase 3 seam)
    # ------------------------------------------------------------

    def _create_inspector(self):
        try:
            return self._connector.create_inspector()
        except Exception as exc:
            raise MetadataInspectionError(
                f"Could not open a metadata inspector for source_system_id="
                f"{self._connector.source_system_id!r}: {redact_text(str(exc))}"
            ) from exc

    # ------------------------------------------------------------
    # Namespaces (Step 5)
    # ------------------------------------------------------------

    def _discover_namespaces(self, inspector, source_type: SourceType) -> tuple[str | None, ...]:
        """Resolve which namespaces to traverse.

        Engine semantics deliberately differ rather than being forced into one
        shape:

        PostgreSQL / SQL Server
            A database contains multiple schemas; each is a real namespace and
            becomes ``SourceEntity.namespace``.

        MySQL
            "schema" IS the database - there is no second namespace level
            inside it. A single ``None`` namespace is used so entities carry
            bare table names, matching how MySQL itself addresses them.
        """
        if source_type is SourceType.MYSQL:
            return (None,)

        try:
            available = inspector.get_schema_names()
        except Exception as exc:
            self._warnings.append(
                f"Could not list schemas; falling back to the default namespace: "
                f"{redact_text(str(exc))}"
            )
            return (None,)

        selected = [
            name for name in available if self._options.wants_namespace(name, source_type)
        ]

        if not selected:
            self._warnings.append(
                "No namespaces matched the discovery options; nothing to discover."
            )

        return tuple(sorted(selected))

    # ------------------------------------------------------------
    # Entities and fields (Steps 6, 7, 9, 11, 12, 13, 14)
    # ------------------------------------------------------------

    def _discover_entities(
        self, inspector, namespaces: Sequence[str | None], source_type: SourceType
    ) -> tuple[SourceEntity, ...]:
        entities: list[SourceEntity] = []

        for namespace in namespaces:
            for table_name, entity_kind in self._list_tables(inspector, namespace):
                if not self._options.wants_table(table_name):
                    continue

                try:
                    entities.append(
                        self._build_entity(inspector, namespace, table_name, entity_kind, source_type)
                    )
                except Exception as exc:
                    # A table dropped or made inaccessible mid-discovery must
                    # not abort the whole run.
                    self._warnings.append(
                        f"Skipped {_qualified(namespace, table_name)}: "
                        f"{redact_text(str(exc))}"
                    )

        return tuple(entities)

    def _list_tables(self, inspector, namespace: str | None) -> list[tuple[str, EntityKind]]:
        listed: list[tuple[str, EntityKind]] = []

        try:
            for name in inspector.get_table_names(schema=namespace):
                listed.append((name, EntityKind.TABLE))
        except Exception as exc:
            raise MetadataInspectionError(
                f"Could not list tables for namespace {namespace!r}: "
                f"{redact_text(str(exc))}"
            ) from exc

        if self._options.include_views:
            try:
                for name in inspector.get_view_names(schema=namespace):
                    listed.append((name, EntityKind.VIEW))
            except Exception as exc:
                self._warnings.append(
                    f"Could not list views for namespace {namespace!r}: "
                    f"{redact_text(str(exc))}"
                )

        return listed

    def _build_entity(
        self,
        inspector,
        namespace: str | None,
        table_name: str,
        entity_kind: EntityKind,
        source_type: SourceType,
    ) -> SourceEntity:
        columns = inspector.get_columns(table_name, schema=namespace)
        primary_key = self._safe_inspect(
            lambda: inspector.get_pk_constraint(table_name, schema=namespace),
            default={},
            what=f"primary key for {_qualified(namespace, table_name)}",
        )
        unique_constraints = self._safe_inspect(
            lambda: inspector.get_unique_constraints(table_name, schema=namespace),
            default=[],
            what=f"unique constraints for {_qualified(namespace, table_name)}",
        )
        indexes = self._safe_inspect(
            lambda: inspector.get_indexes(table_name, schema=namespace),
            default=[],
            what=f"indexes for {_qualified(namespace, table_name)}",
        )
        table_comment = self._safe_inspect(
            lambda: inspector.get_table_comment(table_name, schema=namespace),
            default={},
            what=f"table comment for {_qualified(namespace, table_name)}",
        )

        pk_columns: list[str] = list(primary_key.get("constrained_columns") or [])

        # A field is individually unique only when a single-column unique
        # constraint or single-column unique index covers it. A composite
        # constraint must NOT mark each participating column unique (Step 11).
        individually_unique = _single_column_unique_names(unique_constraints, indexes)

        dialect = getattr(inspector, "dialect", None)

        fields = tuple(
            self._build_field(
                column=column,
                ordinal=ordinal,
                pk_columns=pk_columns,
                individually_unique=individually_unique,
                dialect=dialect,
            )
            for ordinal, column in enumerate(columns)
        )

        normalized_pk = tuple(
            normalize_identifier(name) for name in pk_columns if name is not None
        )

        qualified = _qualified(namespace, table_name)

        metadata: dict[str, Any] = {
            "source_table_name": table_name,
            "discovered_entity_kind": entity_kind.value,
        }
        if namespace is not None:
            metadata["source_namespace"] = namespace
        if primary_key.get("name"):
            metadata["primary_key_constraint"] = str(primary_key["name"])

        composite_unique = _composite_unique_constraints(unique_constraints, indexes)
        if composite_unique:
            # Phase 1's SourceField.is_unique is per-field and cannot express
            # "these N columns are unique together". Rather than change a
            # frozen Phase 1 contract, composite uniqueness is preserved
            # losslessly here in structured entity metadata. Documented in
            # docs/relational_schema_discovery.md.
            metadata["composite_unique_constraints"] = composite_unique

        index_metadata = _index_metadata(indexes)
        if index_metadata:
            metadata["indexes"] = index_metadata

        description = None
        if isinstance(table_comment, Mapping) and table_comment.get("text"):
            description = str(table_comment["text"])

        return SourceEntity(
            entity_id=self._build_entity_id(qualified),
            source_name=table_name,
            normalized_name=normalize_identifier(qualified),
            entity_kind=entity_kind,
            namespace=namespace,
            fields=fields,
            primary_key_fields=normalized_pk,
            description=description,
            metadata=metadata,
        )

    def _build_field(
        self,
        column: Mapping[str, Any],
        ordinal: int,
        pk_columns: Sequence[str],
        individually_unique: frozenset[str],
        dialect: Any,
    ) -> SourceField:
        source_name = str(column["name"])
        sqlalchemy_type = column.get("type")

        rendered_type = render_source_data_type(sqlalchemy_type, dialect) if sqlalchemy_type is not None else None
        normalized_type = (
            normalize_data_type(sqlalchemy_type, rendered_type)
            if sqlalchemy_type is not None
            else FieldDataType.UNKNOWN
        )

        nullable = bool(column.get("nullable", True))
        is_primary_key = source_name in pk_columns

        # A primary-key column is never nullable, whatever the reflection
        # reported - and Phase 1 rejects a nullable PK outright.
        if is_primary_key:
            nullable = False

        metadata: dict[str, Any] = {"source_column_name": source_name}

        default_value = column.get("default")
        if default_value is not None:
            # Schema metadata only: stored verbatim as a string, never parsed,
            # never evaluated, never executed (Step 13).
            metadata["column_default"] = str(default_value)

        if column.get("autoincrement"):
            metadata["autoincrement"] = True

        computed = column.get("computed")
        if computed:
            metadata["computed"] = {key: str(value) for key, value in dict(computed).items()}

        identity = column.get("identity")
        if identity:
            metadata["identity"] = {key: str(value) for key, value in dict(identity).items()}

        return SourceField(
            source_name=source_name,
            normalized_name=normalize_identifier(source_name),
            source_data_type=rendered_type,
            normalized_data_type=normalized_type,
            nullable=nullable,
            # "required" here means the source itself requires a value: not
            # nullable and no default to fall back on.
            required=(not nullable) and default_value is None,
            is_primary_key=is_primary_key,
            is_unique=source_name in individually_unique,
            is_array=normalized_type is FieldDataType.ARRAY,
            nested_path=None,  # relational columns are flat
            # Phase 4 never infers business semantics - that is a later phase.
            semantic_type=None,
            description=(str(column["comment"]) if column.get("comment") else None),
            ordinal=ordinal,
            metadata=metadata,
        )

    # ------------------------------------------------------------
    # Relationships (Step 10)
    # ------------------------------------------------------------

    def _discover_relationships(
        self,
        inspector,
        namespaces: Sequence[str | None],
        entities: Sequence[SourceEntity],
        source_type: SourceType,
    ) -> tuple[SourceRelationship, ...]:
        known_entities = {entity.normalized_name for entity in entities}
        relationships: list[SourceRelationship] = []
        seen_ids: set[str] = set()

        for entity in entities:
            namespace = entity.namespace
            table_name = entity.source_name

            foreign_keys = self._safe_inspect(
                lambda: inspector.get_foreign_keys(table_name, schema=namespace),
                default=[],
                what=f"foreign keys for {_qualified(namespace, table_name)}",
            )

            for foreign_key in foreign_keys:
                relationship = self._build_relationship(
                    foreign_key=foreign_key,
                    from_entity=entity,
                    default_namespace=namespace,
                    known_entities=known_entities,
                    seen_ids=seen_ids,
                )
                if relationship is not None:
                    relationships.append(relationship)

        return tuple(relationships)

    def _build_relationship(
        self,
        foreign_key: Mapping[str, Any],
        from_entity: SourceEntity,
        default_namespace: str | None,
        known_entities: set[str],
        seen_ids: set[str],
    ) -> SourceRelationship | None:
        constrained = [str(name) for name in (foreign_key.get("constrained_columns") or [])]
        referred_columns = [str(name) for name in (foreign_key.get("referred_columns") or [])]
        referred_table = foreign_key.get("referred_table")

        if not constrained or not referred_table:
            return None

        # A cross-schema FK reports its own referred_schema; when absent the
        # reference is within the same namespace as the constrained table.
        referred_namespace = foreign_key.get("referred_schema", default_namespace)
        if referred_namespace is None and default_namespace is not None:
            referred_namespace = default_namespace

        to_entity_name = normalize_identifier(_qualified(referred_namespace, str(referred_table)))

        if to_entity_name not in known_entities:
            # The target is outside the discovered scope (e.g. an excluded
            # namespace). Phase 1's SourceSchema validates that every
            # relationship references a declared entity, so recording it would
            # make the schema unconstructable.
            self._warnings.append(
                f"Foreign key on {from_entity.normalized_name} references "
                f"{to_entity_name}, which is outside the discovered scope; "
                "relationship omitted."
            )
            return None

        from_fields = tuple(normalize_identifier(name) for name in constrained)
        to_fields = tuple(normalize_identifier(name) for name in referred_columns)

        constraint_name = foreign_key.get("name")
        relationship_id = self._build_relationship_id(
            from_entity=from_entity.normalized_name,
            to_entity=to_entity_name,
            from_fields=from_fields,
            constraint_name=str(constraint_name) if constraint_name else None,
            seen_ids=seen_ids,
        )

        metadata: dict[str, Any] = {
            "source_constraint_name": str(constraint_name) if constraint_name else None,
            "source_from_columns": constrained,
            "source_to_columns": referred_columns,
            "is_self_reference": from_entity.normalized_name == to_entity_name,
            "is_composite": len(from_fields) > 1,
        }
        if referred_namespace is not None:
            metadata["referred_namespace"] = referred_namespace

        options = foreign_key.get("options")
        if options:
            metadata["constraint_options"] = {
                key: str(value) for key, value in dict(options).items()
            }

        return SourceRelationship(
            relationship_id=relationship_id,
            relationship_type=RelationshipType.FOREIGN_KEY,
            from_entity=from_entity.normalized_name,
            to_entity=to_entity_name,
            from_fields=from_fields,
            to_fields=to_fields,
            # A declared database constraint is fact, not inference.
            confidence=1.0,
            description=None,
            metadata=metadata,
        )

    # ------------------------------------------------------------
    # Deterministic identity (Steps 6, 15)
    # ------------------------------------------------------------

    def _build_entity_id(self, qualified_name: str) -> str:
        return normalize_identifier(
            f"{self._connector.source_system_id}.{qualified_name}"
        )

    def _build_relationship_id(
        self,
        from_entity: str,
        to_entity: str,
        from_fields: Sequence[str],
        constraint_name: str | None,
        seen_ids: set[str],
    ) -> str:
        """Deterministic relationship identity.

        Prefers the database's own constraint name, qualified by the owning
        entity so two tables reusing a generic name (``fk_customer``) do not
        collide. Falls back to the column list when a dialect reports no name.
        A numeric suffix is appended only if a genuine duplicate still occurs,
        keeping ids unique without depending on discovery order for the common
        case.
        """
        discriminator = constraint_name or "_".join(from_fields)
        base = normalize_identifier(f"fk.{from_entity}.{discriminator}.{to_entity}")

        candidate = base
        suffix = 2
        while candidate in seen_ids:
            candidate = normalize_identifier(f"{base}.{suffix}")
            suffix += 1

        seen_ids.add(candidate)
        return candidate

    def _build_schema_name(
        self, source_type: SourceType, database: str, namespaces: Sequence[str | None]
    ) -> str:
        """The STABLE logical scope Phase 2 versions snapshots within.

        Must not change when the schema's *content* changes - otherwise every
        structural change would start a new version-1 history instead of
        incrementing the existing one.
        """
        if self._options.include_schemas is not None:
            return ",".join(sorted(str(name) for name in self._options.include_schemas))

        real_namespaces = [name for name in namespaces if name]
        if not real_namespaces:
            return database

        if len(real_namespaces) == 1:
            return real_namespaces[0]

        return ",".join(sorted(real_namespaces))

    def _build_schema_id(self, database: str, schema_name: str, structural_hash: str) -> str:
        """Deterministic SNAPSHOT identity, content-addressed.

        Phase 2 treats ``schema_id`` as an immutable snapshot identity: reusing
        one for different content raises ``SchemaIdentityConflictError``, while
        a new id with new content produces the next catalog version. Including
        a prefix of the structural hash therefore gives both required
        behaviours at once:

            unchanged schema -> identical id -> idempotent, still version 1
            changed schema   -> new id       -> catalog version N+1

        Nothing here depends on a timestamp, discovery order, or randomness.
        """
        return normalize_identifier(
            f"{self._connector.source_system_id}.{database}.{schema_name}.{structural_hash[:12]}"
        )

    # ------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------

    def _assemble(
        self,
        schema_id: str,
        schema_name: str,
        database: str,
        namespaces: Sequence[str | None],
        entities: Sequence[SourceEntity],
        relationships: Sequence[SourceRelationship],
        schema_hash: str | None,
    ) -> SourceSchema:
        metadata: dict[str, Any] = {
            "database": database,
            "discovered_namespaces": [name for name in namespaces if name],
            "engine": self._connector.source_type.value,
            "discovery_include_views": self._options.include_views,
        }
        if self._warnings:
            metadata["discovery_warnings"] = list(self._warnings)

        return SourceSchema(
            schema_id=schema_id,
            source_system_id=self._connector.source_system_id,
            schema_name=schema_name,
            origin=SchemaOrigin.DISCOVERED,
            entities=tuple(entities),
            relationships=tuple(relationships),
            schema_hash=schema_hash,
            metadata=metadata,
        )

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    def _safe_inspect(self, call, default, what: str):
        """Run one Inspector call, degrading to ``default`` with a warning.

        Optional metadata (comments, indexes, unique constraints) is not
        uniformly available across dialects and permission levels; failing to
        obtain it must never fail structural discovery (Step 14).
        """
        try:
            return call()
        except Exception as exc:
            self._warnings.append(f"Could not read {what}: {redact_text(str(exc))}")
            return default


# ============================================================
# Module-level helpers
# ============================================================

def _require_relational_connector(connector: Any) -> BaseSourceConnector:
    if not isinstance(connector, BaseSourceConnector):
        raise UnsupportedDiscoverySourceError(
            f"Relational discovery requires a source connector, got "
            f"{type(connector).__name__}."
        )

    if connector.source_type not in SUPPORTED_SOURCE_TYPES:
        supported = ", ".join(sorted(t.value for t in SUPPORTED_SOURCE_TYPES))
        raise UnsupportedDiscoverySourceError(
            f"Relational schema discovery supports {supported}, but "
            f"source_system_id={connector.source_system_id!r} is "
            f"{connector.source_type.value!r}. MongoDB schema inference is "
            "Phase 5 and is not implemented here."
        )

    if not hasattr(connector, "create_inspector"):
        raise UnsupportedDiscoverySourceError(
            f"{type(connector).__name__} does not expose create_inspector(); "
            "relational discovery needs a SQLAlchemy-backed connector."
        )

    return connector


def _qualified(namespace: str | None, table_name: str) -> str:
    """Namespace-qualified entity name.

    Qualifying is what keeps ``public.customer`` and ``sales.customer``
    distinct - the catalog enforces uniqueness of
    ``(schema_id, normalized_name)``, so unqualified names would collide
    across namespaces. MySQL, having no namespace level, yields the bare name.
    """
    return f"{namespace}.{table_name}" if namespace else table_name


def _single_column_unique_names(
    unique_constraints: Iterable[Mapping[str, Any]],
    indexes: Iterable[Mapping[str, Any]],
) -> frozenset[str]:
    """Columns that are individually unique.

    Composite constraints are deliberately excluded: marking each member
    column ``is_unique=True`` would assert something false about the data.
    """
    names: set[str] = set()

    for constraint in unique_constraints or []:
        columns = [c for c in (constraint.get("column_names") or []) if c]
        if len(columns) == 1:
            names.add(str(columns[0]))

    for index in indexes or []:
        if not index.get("unique"):
            continue
        columns = [c for c in (index.get("column_names") or []) if c]
        if len(columns) == 1:
            names.add(str(columns[0]))

    return frozenset(names)


def _composite_unique_constraints(
    unique_constraints: Iterable[Mapping[str, Any]],
    indexes: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Multi-column uniqueness, preserved losslessly as JSON-safe metadata.

    Declared constraints are recorded first and win on conflict: PostgreSQL
    (and SQL Server) implement a UNIQUE constraint with a backing unique
    index, so the same logical rule is reported by both Inspector calls.
    Deduplicating on the ordered column list keeps one authoritative entry
    per rule instead of an apparent duplicate.
    """
    preserved: list[dict[str, Any]] = []
    seen_column_sets: set[tuple[str, ...]] = set()

    def record(columns: list[str], name: Any, source: str) -> None:
        key = tuple(columns)
        if key in seen_column_sets:
            return
        seen_column_sets.add(key)
        preserved.append(
            {
                "name": str(name) if name else None,
                "columns": columns,
                "source": source,
            }
        )

    for constraint in unique_constraints or []:
        columns = [str(c) for c in (constraint.get("column_names") or []) if c]
        if len(columns) > 1:
            record(columns, constraint.get("name"), "unique_constraint")

    for index in indexes or []:
        if not index.get("unique"):
            continue
        columns = [str(c) for c in (index.get("column_names") or []) if c]
        if len(columns) > 1:
            record(columns, index.get("name"), "unique_index")

    return preserved


def _index_metadata(indexes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Index structure as JSON-safe metadata (Step 12).

    Records name, ordered column list and uniqueness. Expression bodies are
    NOT captured: a functional index expression can embed literal values, and
    this metadata is stored in a catalog that must stay free of data content.
    A ``None`` entry in SQLAlchemy's ``column_names`` marks such an expression
    and is reported only as a count.
    """
    collected: list[dict[str, Any]] = []

    for index in indexes or []:
        raw_columns = list(index.get("column_names") or [])
        named_columns = [str(c) for c in raw_columns if c is not None]
        expression_count = sum(1 for c in raw_columns if c is None)

        entry: dict[str, Any] = {
            "name": str(index.get("name")) if index.get("name") else None,
            "columns": named_columns,
            "unique": bool(index.get("unique")),
        }
        if expression_count:
            entry["expression_column_count"] = expression_count

        collected.append(entry)

    return collected


def discover_schema(
    connector: BaseSourceConnector, options: DiscoveryOptions | None = None
) -> SourceSchema:
    """Convenience wrapper: discover and return the ``SourceSchema``."""
    return RelationalSchemaDiscovery(connector, options).discover()


__all__ = [
    "SUPPORTED_SOURCE_TYPES",
    "RelationalSchemaDiscovery",
    "discover_schema",
]
