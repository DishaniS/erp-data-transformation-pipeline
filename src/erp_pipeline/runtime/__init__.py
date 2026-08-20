"""Production runtime wiring.

              RuntimeSettings          one place reads the environment
                     |
                     v
            build_pipeline_engine      one engine, one AI-native database
                     |
        +------------+------------+
        v                         v
   bootstrap_all           build_production_services
   five owned schemas      durable stores, never in-memory
                                  |
                                  v
                        create_production_app
                                  |
                                  v
                    uvicorn erp_pipeline.runtime.application:app

WHY THIS PACKAGE EXISTS
-----------------------
Phases 0-13 built the capabilities. Every one of them was verified by a test
that assembled the system by hand, which meant the shipped defaults stayed
in-memory and the application had no way to start. This package is the
composition root: the single place where "which implementation?" is answered,
and answered with the durable one.

It contains no pipeline logic. If a research algorithm appears here, it is in
the wrong package.
"""

from __future__ import annotations

from erp_pipeline.runtime.bootstrap import (
    BootstrapResult,
    SchemaResult,
    bootstrap_all,
    verify_all,
)
from erp_pipeline.runtime.database import (
    OWNED_SCHEMAS,
    build_pipeline_engine,
    check_connection,
    existing_schemas,
)
from erp_pipeline.runtime.persistence import (
    DRAFTS_TABLE,
    RUNTIME_SCHEMA,
    SOURCES_TABLE,
    UPLOADS_TABLE,
    PostgresMappingDraftStore,
    PostgresSourceRegistry,
    PostgresUploadStore,
    bootstrap_runtime_persistence,
)
from erp_pipeline.runtime.services import (
    build_production_services,
    build_qdrant_client,
    build_storage_service,
    build_sync_service,
)
from erp_pipeline.runtime.settings import (
    COLD_KEY_VARIABLE,
    ColdSettings,
    ConfigurationError,
    DatabaseSettings,
    QdrantSettings,
    RuntimeSettings,
)

__all__ = [
    # settings
    "RuntimeSettings",
    "DatabaseSettings",
    "QdrantSettings",
    "ColdSettings",
    "ConfigurationError",
    "COLD_KEY_VARIABLE",
    # database
    "build_pipeline_engine",
    "check_connection",
    "existing_schemas",
    "OWNED_SCHEMAS",
    # bootstrap
    "bootstrap_all",
    "verify_all",
    "BootstrapResult",
    "SchemaResult",
    "bootstrap_runtime_persistence",
    # persistence
    "PostgresSourceRegistry",
    "PostgresUploadStore",
    "PostgresMappingDraftStore",
    "RUNTIME_SCHEMA",
    "SOURCES_TABLE",
    "UPLOADS_TABLE",
    "DRAFTS_TABLE",
    # composition
    "build_production_services",
    "build_storage_service",
    "build_sync_service",
    "build_qdrant_client",
]
