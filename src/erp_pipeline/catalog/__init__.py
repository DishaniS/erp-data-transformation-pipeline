"""Persistent, versioned storage for the schema catalog.

Where ``erp_pipeline.schemas`` defines the pure, I/O-free contracts, this
package persists them: PostgreSQL connection handling, transactions and SQL
all live here and nowhere in ``schemas/``.

Logical model vs physical storage
----------------------------------
The catalog's logical model is the Phase 1 contracts. PostgreSQL is only the
physical engine chosen to store them - see ``docs/schema_catalog.md``.

Public surface:

    config       PIPELINE_DB_* configuration (AI_DB_* deprecated fallback)
    schema       idempotent DDL for the erp_catalog namespace
    repository   CatalogRepository - CRUD over Phase 1 contract objects
    versioning   pure schema comparison, version arithmetic, summaries
    service      SchemaCatalogService - repository + versioning orchestration
    exceptions   the catalog's domain error hierarchy
    verify       python -m erp_pipeline.catalog.verify

This package depends on SQLAlchemy and psycopg2, which ``erp_pipeline.
schemas`` deliberately does not. It never imports ``bpi2020``.
"""

from __future__ import annotations

from erp_pipeline.catalog.config import CATALOG_SCHEMA_NAME, CatalogDatabaseSettings
from erp_pipeline.catalog.exceptions import (
    CatalogConfigurationError,
    CatalogConnectionError,
    CatalogError,
    CatalogIntegrityError,
    MappingProfileNotFoundError,
    SchemaIdentityConflictError,
    SchemaSnapshotNotFoundError,
    SourceSystemIdentityConflictError,
    SourceSystemNotFoundError,
)
from erp_pipeline.catalog.repository import (
    CatalogRepository,
    SchemaSnapshotRecord,
    SchemaSnapshotResult,
)
from erp_pipeline.catalog.schema import ALL_TABLE_NAMES, BootstrapReport, bootstrap_catalog
from erp_pipeline.catalog.service import SchemaCatalogService
from erp_pipeline.catalog.versioning import (
    BreakingLevel,
    FieldChange,
    RenameCandidate,
    SchemaDiff,
    SchemaSnapshotSummary,
    compare_schemas,
    next_catalog_version,
    summarize_schema,
)

__all__ = [
    "CATALOG_SCHEMA_NAME",
    "CatalogDatabaseSettings",
    "CatalogError",
    "CatalogConfigurationError",
    "CatalogConnectionError",
    "CatalogIntegrityError",
    "SourceSystemNotFoundError",
    "SourceSystemIdentityConflictError",
    "SchemaSnapshotNotFoundError",
    "SchemaIdentityConflictError",
    "MappingProfileNotFoundError",
    "CatalogRepository",
    "SchemaSnapshotRecord",
    "SchemaSnapshotResult",
    "ALL_TABLE_NAMES",
    "BootstrapReport",
    "bootstrap_catalog",
    "SchemaCatalogService",
    "BreakingLevel",
    "FieldChange",
    "RenameCandidate",
    "SchemaDiff",
    "SchemaSnapshotSummary",
    "compare_schemas",
    "next_catalog_version",
    "summarize_schema",
]
