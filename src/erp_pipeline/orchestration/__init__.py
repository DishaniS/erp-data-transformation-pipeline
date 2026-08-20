"""Orchestration for the ERP transformation pipeline (Phase 13 core).

              JobRequest
                  |
                  v
            PipelinePlanner        capability-aware stage graph
                  |
                  v
            OrchestrationService   persist -> enqueue -> return
                  |
                  v
              JobExecutor          bounded worker pool
                  |
                  v
            PipelineRunner         stage by stage, halting on failure
                  |
        +---------+---------+
        v                   v
   phase services       JobStore    durable status and stage history

WHAT THIS PACKAGE IS
--------------------
A control plane. It decides WHICH existing phase runs, in WHAT order, and
records WHAT happened. It contains no discovery, mapping, transformation,
validation, chunking, embedding or tier-routing logic of its own - each of
those belongs to the phase that owns it, and is called rather than copied.

It is deliberately free of any web framework, so the whole pipeline can be
driven from a script or a test without starting an HTTP server.
"""

from __future__ import annotations

from erp_pipeline.orchestration.errors import (
    DependencyUnavailableError,
    InvalidPipelineRequestError,
    JobConflictError,
    JobNotFoundError,
    MappingNotExecutableError,
    MappingNotFoundError,
    OrchestrationError,
    RecordNotFoundError,
    RetryNotSupportedError,
    SchemaNotFoundError,
    SecretUnavailableError,
    SourceNotFoundError,
    UnsafeUploadNameError,
    UnsupportedCapabilityError,
    UnsupportedUploadError,
    UploadNotFoundError,
    UploadTooLargeError,
)
from erp_pipeline.orchestration.executor import (
    DEFAULT_MAX_WORKERS,
    InlineJobExecutor,
    JobExecutor,
)
from erp_pipeline.orchestration.extraction import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    CsvSnapshotExtractor,
    ExtractionRequest,
    MongoSnapshotExtractor,
    RelationalSnapshotExtractor,
    extractor_for,
    resolve_entity,
    validate_identifier,
)
from erp_pipeline.orchestration.job_store import (
    JOBS_TABLE,
    ORCHESTRATION_SCHEMA_NAME,
    STAGES_TABLE,
    InMemoryJobStore,
    JobStore,
    PostgresJobStore,
    bootstrap_orchestration_schema,
)
from erp_pipeline.orchestration.models import (
    ORCHESTRATION_ENGINE_VERSION,
    Job,
    JobCounters,
    JobRequest,
    JobStatus,
    JobType,
    PipelineStage,
    StageRun,
    StageStatus,
    new_job_id,
)
from erp_pipeline.orchestration.pipeline import (
    PipelineContext,
    PipelineRunner,
    StageFailure,
)
from erp_pipeline.orchestration.planner import (
    DISCOVERABLE_SOURCES,
    DOCUMENT_SOURCES,
    RECORD_BEARING_SOURCES,
    SPEC_SOURCES,
    PipelinePlan,
    PipelinePlanner,
)
from erp_pipeline.orchestration.record_store import (
    RECORD_SCHEMA_NAME,
    PostgresCanonicalRecordStore,
    bootstrap_record_schema,
)
from erp_pipeline.orchestration.secrets import (
    EnvironmentSecretProvider,
    InMemorySecretProvider,
    NullSecretProvider,
    SecretProvider,
)
from erp_pipeline.orchestration.service import (
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.orchestration.sources import (
    RegisteredSource,
    SourceRegistry,
    normalize_source_id,
    scrub_metadata,
)
from erp_pipeline.orchestration.stages import DEFAULT_HANDLERS
from erp_pipeline.orchestration.upload_store import (
    DEFAULT_MAX_UPLOAD_BYTES,
    StoredUpload,
    UploadStore,
    sanitize_display_name,
)

__all__ = [
    # errors
    "OrchestrationError",
    "JobNotFoundError",
    "SourceNotFoundError",
    "SchemaNotFoundError",
    "MappingNotFoundError",
    "RecordNotFoundError",
    "UploadNotFoundError",
    "UnsupportedCapabilityError",
    "InvalidPipelineRequestError",
    "MappingNotExecutableError",
    "UploadTooLargeError",
    "UnsupportedUploadError",
    "UnsafeUploadNameError",
    "DependencyUnavailableError",
    "JobConflictError",
    "SecretUnavailableError",
    "RetryNotSupportedError",
    # models
    "ORCHESTRATION_ENGINE_VERSION",
    "JobStatus",
    "StageStatus",
    "PipelineStage",
    "JobType",
    "JobCounters",
    "StageRun",
    "JobRequest",
    "Job",
    "new_job_id",
    # planning and execution
    "PipelinePlan",
    "PipelinePlanner",
    "RECORD_BEARING_SOURCES",
    "DISCOVERABLE_SOURCES",
    "DOCUMENT_SOURCES",
    "SPEC_SOURCES",
    "PipelineContext",
    "PipelineRunner",
    "StageFailure",
    "DEFAULT_HANDLERS",
    "JobExecutor",
    "InlineJobExecutor",
    "DEFAULT_MAX_WORKERS",
    # persistence
    "JobStore",
    "InMemoryJobStore",
    "PostgresJobStore",
    "bootstrap_orchestration_schema",
    "ORCHESTRATION_SCHEMA_NAME",
    "JOBS_TABLE",
    "STAGES_TABLE",
    "PostgresCanonicalRecordStore",
    "bootstrap_record_schema",
    "RECORD_SCHEMA_NAME",
    # sources, secrets, uploads
    "RegisteredSource",
    "SourceRegistry",
    "normalize_source_id",
    "scrub_metadata",
    "SecretProvider",
    "EnvironmentSecretProvider",
    "InMemorySecretProvider",
    "NullSecretProvider",
    "UploadStore",
    "StoredUpload",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "sanitize_display_name",
    # extraction
    "ExtractionRequest",
    "RelationalSnapshotExtractor",
    "MongoSnapshotExtractor",
    "CsvSnapshotExtractor",
    "extractor_for",
    "resolve_entity",
    "validate_identifier",
    "DEFAULT_BATCH_SIZE",
    "MAX_BATCH_SIZE",
    # facade
    "OrchestrationService",
    "PipelineServices",
]
