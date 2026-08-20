"""Discovery options and supplemental result models.

``SourceSchema`` (Phase 1) remains the authoritative structural output of
discovery. Everything in this module is either input configuration
(``DiscoveryOptions``) or *supplemental* evidence (the profiling models) that
deliberately lives outside ``SourceSchema`` rather than being forced into
Phase 1 fields never designed to hold it.

Privacy rule for every profiling and inference model here: aggregate
statistics only. No model in this module has a field capable of holding a
sample value, a row, or any column or document content. See
``discovery.profiling`` and ``discovery.mongodb_inference`` for the
enforcement, and ``tests/erp_pipeline/discovery/test_profiling.py`` /
``test_mongodb_privacy.py`` for the proof.

Two option objects live here, one per paradigm: ``DiscoveryOptions`` for
relational metadata discovery (Phase 4) and ``MongoInferenceOptions`` for
bounded document sampling (Phase 5). Both feed the SAME ``SourceSchema``
output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from erp_pipeline.schemas.enums import SourceType


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# System namespaces excluded by default (Step 4 - centralized)
# ============================================================
#
# Defined once, here, rather than scattered through the discovery algorithm.
# Keyed by SourceType so a new engine adds one entry instead of an `if` branch
# somewhere in the traversal code.

SYSTEM_NAMESPACES: Mapping[SourceType, frozenset[str]] = {
    SourceType.POSTGRESQL: frozenset(
        {"pg_catalog", "information_schema", "pg_toast"}
    ),
    SourceType.MYSQL: frozenset(
        {"information_schema", "mysql", "performance_schema", "sys"}
    ),
    SourceType.SQL_SERVER: frozenset(
        {"sys", "information_schema", "guest", "db_owner", "db_accessadmin",
         "db_securityadmin", "db_ddladmin", "db_backupoperator",
         "db_datareader", "db_datawriter", "db_denydatareader",
         "db_denydatawriter"}
    ),
}

#: Conservative default so a first discovery run against a large ERP cannot
#: accidentally launch thousands of aggregate queries.
DEFAULT_MAX_PROFILED_TABLES = 20
DEFAULT_MAX_PROFILING_QUERIES = 200
DEFAULT_QUERY_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class DiscoveryOptions:
    """Read-only, conservative-by-default discovery configuration.

    Every default here is chosen so that calling ``discover()`` with no
    options is safe against a production ERP: system namespaces excluded,
    views excluded, profiling off.
    """

    # --- scope selection ---
    include_schemas: Sequence[str] | None = None
    exclude_schemas: Sequence[str] = ()
    include_tables: Sequence[str] | None = None
    exclude_tables: Sequence[str] = ()
    include_views: bool = False
    include_system_schemas: bool = False

    # --- profiling (all off by default) ---
    profiling_enabled: bool = False
    profile_row_counts: bool = True
    profile_null_percentage: bool = True
    profile_distinct_count: bool = False
    profile_numeric_min_max: bool = True
    profile_length_stats: bool = False

    # --- profiling safety budget ---
    max_profiled_tables: int = DEFAULT_MAX_PROFILED_TABLES
    max_profiling_queries: int = DEFAULT_MAX_PROFILING_QUERIES
    strict_budget: bool = False

    # --- execution ---
    query_timeout_seconds: int = DEFAULT_QUERY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for name in ("max_profiled_tables", "max_profiling_queries", "query_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"DiscoveryOptions.{name} must be a positive integer, got {value!r}."
                )

    def excluded_namespaces(self, source_type: SourceType) -> frozenset[str]:
        """System namespaces plus caller exclusions, lower-cased for matching."""
        excluded = {name.lower() for name in self.exclude_schemas}

        if not self.include_system_schemas:
            excluded |= {
                name.lower() for name in SYSTEM_NAMESPACES.get(source_type, frozenset())
            }

        return frozenset(excluded)

    def wants_namespace(self, namespace: str, source_type: SourceType) -> bool:
        lowered = (namespace or "").lower()

        if lowered in self.excluded_namespaces(source_type):
            return False

        if self.include_schemas is not None:
            return lowered in {name.lower() for name in self.include_schemas}

        return True

    def wants_table(self, table_name: str) -> bool:
        lowered = table_name.lower()

        if lowered in {name.lower() for name in self.exclude_tables}:
            return False

        if self.include_tables is not None:
            return lowered in {name.lower() for name in self.include_tables}

        return True


# ============================================================
# Profiling results - aggregates only, never sample values
# ============================================================

@dataclass(frozen=True)
class ColumnProfile:
    """Aggregate statistics for one column.

    Every field is a COUNT, a percentage, a length, or a numeric bound.
    There is deliberately no field able to carry a value drawn from the
    column's data - not a sample, not a mode, not a "most common value".
    ``numeric_min``/``numeric_max`` are bounds of a numeric column only and
    are never populated for text, binary, or temporal columns.
    """

    column_name: str
    null_count: int | None = None
    null_percentage: float | None = None
    distinct_count: int | None = None
    numeric_min: float | None = None
    numeric_max: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    average_length: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TableProfile:
    """Aggregate statistics for one table and its profiled columns."""

    entity_name: str
    row_count: int | None = None
    columns: Sequence[ColumnProfile] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_name": self.entity_name,
            "row_count": self.row_count,
            "columns": [column.to_dict() for column in self.columns],
            "error": self.error,
        }


@dataclass(frozen=True)
class ProfilingSummary:
    """Outcome of a profiling pass, including whether the budget cut it short."""

    enabled: bool
    partial: bool = False
    tables_profiled: int = 0
    queries_executed: int = 0
    budget_exhausted: bool = False
    notes: Sequence[str] = ()
    tables: Sequence[TableProfile] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "partial": self.partial,
            "tables_profiled": self.tables_profiled,
            "queries_executed": self.queries_executed,
            "budget_exhausted": self.budget_exhausted,
            "notes": list(self.notes),
            "tables": [table.to_dict() for table in self.tables],
        }


# ============================================================
# MongoDB observed-schema inference (Phase 5)
# ============================================================
#
# TERMINOLOGY. Everything below describes what was OBSERVED in a bounded
# sample of documents. An ordinary MongoDB collection declares no schema, so
# none of these models - and nothing built from them - may be presented as an
# authoritative database schema. See docs/mongodb_schema_inference.md.

#: Collection-name prefixes MongoDB reserves for its own bookkeeping
#: (``system.views``, ``system.profile``, ``system.js``, ...). The document
#: analogue of ``SYSTEM_NAMESPACES``: declared once, here, rather than as an
#: ``if`` buried in the traversal.
SYSTEM_COLLECTION_PREFIXES: tuple[str, ...] = ("system.",)

#: Conservative sampling defaults. A first inference run against a production
#: document store must never turn into a full collection scan, so the caller
#: has to opt in to a larger sample explicitly.
DEFAULT_MAX_DOCUMENTS_PER_COLLECTION = 500
DEFAULT_MAX_TOTAL_DOCUMENTS = 5000
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_FIELDS_PER_COLLECTION = 500
DEFAULT_MAX_ARRAY_ELEMENTS_PER_DOCUMENT = 50


@dataclass(frozen=True)
class MongoInferenceOptions:
    """Read-only, conservative-by-default MongoDB inference configuration.

    Separate from ``DiscoveryOptions`` on purpose. Relational discovery reads
    declared metadata and its budget governs optional profiling queries;
    document inference reads *data* and its budget governs how many documents
    are examined at all. Sharing one options object would attach
    ``profile_numeric_min_max`` to MongoDB and ``max_array_elements_per_
    document`` to PostgreSQL, where neither means anything.
    """

    # --- scope selection ---
    include_collections: Sequence[str] | None = None
    exclude_collections: Sequence[str] = ()
    include_system_collections: bool = False
    include_views: bool = False

    # --- sampling budget ---
    max_documents_per_collection: int = DEFAULT_MAX_DOCUMENTS_PER_COLLECTION
    max_total_documents: int = DEFAULT_MAX_TOTAL_DOCUMENTS
    max_depth: int = DEFAULT_MAX_DEPTH
    max_fields_per_collection: int = DEFAULT_MAX_FIELDS_PER_COLLECTION
    max_array_elements_per_document: int = DEFAULT_MAX_ARRAY_ELEMENTS_PER_DOCUMENT

    # --- inference behaviour ---
    deterministic_sampling: bool = True
    include_validator_presence: bool = True
    track_type_distribution: bool = True
    track_presence: bool = True
    track_nulls: bool = True

    # --- execution ---
    query_timeout_seconds: int = DEFAULT_QUERY_TIMEOUT_SECONDS

    _POSITIVE_INT_FIELDS = (
        "max_documents_per_collection",
        "max_total_documents",
        "max_depth",
        "max_fields_per_collection",
        "max_array_elements_per_document",
        "query_timeout_seconds",
    )

    def __post_init__(self) -> None:
        for name in self._POSITIVE_INT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"MongoInferenceOptions.{name} must be a positive integer, "
                    f"got {value!r}."
                )

    def wants_collection(self, collection_name: str) -> bool:
        """Whether one collection is in scope.

        Matching is case-sensitive because MongoDB collection names are:
        ``Orders`` and ``orders`` can genuinely coexist in one database, so
        case-folding the filters would silently include or exclude the wrong
        collection.
        """
        if not self.include_system_collections and is_system_collection(collection_name):
            return False

        if collection_name in set(self.exclude_collections):
            return False

        if self.include_collections is not None:
            return collection_name in set(self.include_collections)

        return True

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot of the options an inference run was made with.

        Recorded in schema metadata so a stored observed schema always states
        the sample budget it was derived under - a presence ratio is
        meaningless without it.
        """
        return {
            "include_collections": (
                list(self.include_collections)
                if self.include_collections is not None
                else None
            ),
            "exclude_collections": list(self.exclude_collections),
            "include_system_collections": self.include_system_collections,
            "include_views": self.include_views,
            "max_documents_per_collection": self.max_documents_per_collection,
            "max_total_documents": self.max_total_documents,
            "max_depth": self.max_depth,
            "max_fields_per_collection": self.max_fields_per_collection,
            "max_array_elements_per_document": self.max_array_elements_per_document,
            "deterministic_sampling": self.deterministic_sampling,
            "include_validator_presence": self.include_validator_presence,
            "track_type_distribution": self.track_type_distribution,
            "track_presence": self.track_presence,
            "track_nulls": self.track_nulls,
        }


def is_system_collection(collection_name: str) -> bool:
    """True for a MongoDB-internal collection name."""
    return any(
        collection_name.startswith(prefix) for prefix in SYSTEM_COLLECTION_PREFIXES
    )


@dataclass(frozen=True)
class FieldObservation:
    """What a bounded document sample showed about ONE field path.

    Counts and ratios only. Exactly like ``ColumnProfile``, this model has no
    field capable of holding a value drawn from a document - not a sample, not
    a minimum, not a "most common value". ``type_counts`` maps a BSON type
    ALIAS (``"string"``, ``"objectId"``, ``"decimal"``) to how many values of
    that type were seen; the values themselves are counted and discarded.

    ``present_count`` counts DOCUMENTS in which the path occurred, while
    ``value_count`` counts VALUES observed. The two differ only for paths
    inside an array, where one document can contribute several values.
    """

    path: str
    segments: tuple[str, ...]
    documents_sampled: int
    present_count: int
    null_count: int
    value_count: int
    type_counts: Mapping[str, int] = field(default_factory=dict)
    element_type_counts: Mapping[str, int] = field(default_factory=dict)
    truncated_due_to_depth: bool = False
    array_elements_truncated: bool = False

    @property
    def missing_count(self) -> int:
        """Sampled documents in which this path did not occur at all."""
        return max(self.documents_sampled - self.present_count, 0)

    @property
    def presence_ratio(self) -> float:
        """Observed presence within the SAMPLE - never a database guarantee."""
        if self.documents_sampled <= 0:
            return 0.0
        return round(self.present_count / self.documents_sampled, 6)

    @property
    def null_ratio(self) -> float:
        """Share of observed values that were explicitly null."""
        if self.value_count <= 0:
            return 0.0
        return round(self.null_count / self.value_count, 6)

    @property
    def observed_always_present(self) -> bool:
        """Present in every sampled document, and never null."""
        return (
            self.documents_sampled > 0
            and self.present_count == self.documents_sampled
            and self.null_count == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "segments": list(self.segments),
            "documents_sampled": self.documents_sampled,
            "present_count": self.present_count,
            "missing_count": self.missing_count,
            "null_count": self.null_count,
            "value_count": self.value_count,
            "presence_ratio": self.presence_ratio,
            "null_ratio": self.null_ratio,
            "type_counts": dict(sorted(self.type_counts.items())),
            "element_type_counts": dict(sorted(self.element_type_counts.items())),
            "truncated_due_to_depth": self.truncated_due_to_depth,
            "array_elements_truncated": self.array_elements_truncated,
        }


@dataclass(frozen=True)
class CollectionInferenceSummary:
    """Outcome of inferring one collection's observed structure.

    ``estimated_document_count`` comes from MongoDB's cheap metadata estimate
    and is ``None`` when it could not be obtained. It is reported next to
    ``documents_sampled`` precisely so a reader can see that a sample was
    taken - this framework never converts the pair into a "coverage
    percentage", which would imply a completeness claim a sample cannot make.
    """

    collection_name: str
    documents_sampled: int
    field_path_count: int
    estimated_document_count: int | None = None
    collection_type: str = "collection"
    deterministic_sampling: bool = True
    partial: bool = False
    field_limit_reached: bool = False
    depth_limit_reached: bool = False
    sample_budget_exhausted: bool = False
    validator_present: bool | None = None
    validation_level: str | None = None
    validation_action: str | None = None
    notes: Sequence[str] = ()
    observations: Sequence[FieldObservation] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_name": self.collection_name,
            "documents_sampled": self.documents_sampled,
            "field_path_count": self.field_path_count,
            "estimated_document_count": self.estimated_document_count,
            "collection_type": self.collection_type,
            "deterministic_sampling": self.deterministic_sampling,
            "partial": self.partial,
            "field_limit_reached": self.field_limit_reached,
            "depth_limit_reached": self.depth_limit_reached,
            "sample_budget_exhausted": self.sample_budget_exhausted,
            "validator_present": self.validator_present,
            "validation_level": self.validation_level,
            "validation_action": self.validation_action,
            "notes": list(self.notes),
            "observations": [observation.to_dict() for observation in self.observations],
        }


@dataclass(frozen=True)
class MongoInferenceSummary:
    """Supplemental, aggregate-only evidence for one whole inference run."""

    database: str
    collections_discovered: int = 0
    collections_inferred: int = 0
    total_documents_sampled: int = 0
    partial: bool = False
    budget_exhausted: bool = False
    notes: Sequence[str] = ()
    collections: Sequence[CollectionInferenceSummary] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "collections_discovered": self.collections_discovered,
            "collections_inferred": self.collections_inferred,
            "total_documents_sampled": self.total_documents_sampled,
            "partial": self.partial,
            "budget_exhausted": self.budget_exhausted,
            "notes": list(self.notes),
            "collections": [summary.to_dict() for summary in self.collections],
        }


@dataclass(frozen=True)
class MongoDiscoveryResult:
    """What one MongoDB inference run produced.

    ``schema`` is the authoritative Phase 1 ``SourceSchema`` - the same
    contract PostgreSQL, MySQL and SQL Server discovery produce. ``inference``
    is supplemental and deliberately NOT embedded in it, for the same reason
    profiling is kept out of ``DiscoveryResult.schema``: sample statistics
    change with the sample, and must not perturb the structural hash.
    """

    schema: Any  # SourceSchema - typed loosely to keep this module import-light
    inference: MongoInferenceSummary
    discovered_at: datetime = field(default_factory=utc_now)
    warnings: Sequence[str] = ()

    @property
    def schema_hash(self) -> str:
        return self.schema.compute_schema_hash()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema.to_json_dict(),
            "inference": self.inference.to_dict(),
            "discovered_at": self.discovered_at.isoformat(),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DiscoveryResult:
    """What one discovery run produced.

    ``schema`` is the authoritative Phase 1 ``SourceSchema``. ``profiling``
    is supplemental and is NOT embedded inside the schema - keeping them
    separate is what lets the structural hash stay stable whether or not
    profiling ran.
    """

    schema: Any  # SourceSchema - typed loosely to keep this module import-light
    profiling: ProfilingSummary
    discovered_at: datetime = field(default_factory=utc_now)
    warnings: Sequence[str] = ()

    @property
    def schema_hash(self) -> str:
        return self.schema.compute_schema_hash()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema.to_json_dict(),
            "profiling": self.profiling.to_dict(),
            "discovered_at": self.discovered_at.isoformat(),
            "warnings": list(self.warnings),
        }


__all__ = [
    "SYSTEM_NAMESPACES",
    "DEFAULT_MAX_PROFILED_TABLES",
    "DEFAULT_MAX_PROFILING_QUERIES",
    "DEFAULT_QUERY_TIMEOUT_SECONDS",
    "DiscoveryOptions",
    "ColumnProfile",
    "TableProfile",
    "ProfilingSummary",
    "DiscoveryResult",
    # MongoDB observed-schema inference (Phase 5)
    "SYSTEM_COLLECTION_PREFIXES",
    "DEFAULT_MAX_DOCUMENTS_PER_COLLECTION",
    "DEFAULT_MAX_TOTAL_DOCUMENTS",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FIELDS_PER_COLLECTION",
    "DEFAULT_MAX_ARRAY_ELEMENTS_PER_DOCUMENT",
    "MongoInferenceOptions",
    "is_system_collection",
    "FieldObservation",
    "CollectionInferenceSummary",
    "MongoInferenceSummary",
    "MongoDiscoveryResult",
    "utc_now",
]
