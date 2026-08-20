"""Request and response models.

CREDENTIALS IN REQUESTS
-----------------------
A caller may supply a password once, on registration, using ``SecretStr``. It
is moved straight into the ``SecretProvider`` and is never stored on the
source, never persisted, never returned and never rendered into the OpenAPI
document - ``SecretStr`` also keeps it out of any accidental ``repr``.

The preferred path is ``credential_ref``: name a secret that already exists and
send no password at all.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from erp_pipeline.orchestration.models import JobStatus, JobType
from erp_pipeline.schemas.enums import SourceType


class SourceCreate(BaseModel):
    """Register a source. Structural metadata only."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "ERP Finance (PostgreSQL)",
                "source_type": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "erp_finance",
                "username": "erp_reader",
                "credential_ref": "erp_finance_password",
            }
        }
    )

    name: str = Field(min_length=1, max_length=200)
    source_type: SourceType
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=255)
    auth_database: str | None = Field(default=None, max_length=255)
    ssl_enabled: bool = False
    description: str | None = Field(default=None, max_length=1000)

    #: The name of a secret, resolved at connect time. Not a secret itself.
    credential_ref: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Name of a credential held by the configured secret provider. "
            "The password itself is never stored on the source."
        ),
    )

    #: Accepted once, handed to the secret provider, then dropped. Never
    #: persisted on the source and never returned by any endpoint.
    password: SecretStr | None = Field(
        default=None,
        description=(
            "Optional. If supplied it is stored in the configured secret "
            "provider under credential_ref and is never persisted with the "
            "source or returned by any endpoint."
        ),
    )

    metadata: Mapping[str, Any] = Field(default_factory=dict)


class SourceResponse(BaseModel):
    """A registered source. There is no password field to omit."""

    source_id: str
    name: str
    source_type: str
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    credential_ref: str | None = None
    ssl_enabled: bool = False
    description: str | None = None
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    registered_at: str


class ConnectionTestResponse(BaseModel):
    source_id: str
    source_type: str
    success: bool
    message: str | None = None
    capabilities: Sequence[str] = ()
    server_version: str | None = None
    duration_ms: float | None = None


class DiscoveryResponse(BaseModel):
    source_id: str
    schema_id: str
    schema_hash: str | None = None
    schema_version: int | None = None
    entity_count: int
    field_count: int
    relationship_count: int = 0
    published: bool = False
    warnings: Sequence[str] = ()


class CsvUploadResponse(BaseModel):
    upload_id: str
    filename: str
    content_hash: str
    size_bytes: int
    source_system_id: str | None = None
    schema_id: str | None = None
    columns: int = 0
    rows_observed: int = 0
    published: bool = False
    warnings: Sequence[str] = ()


class DocumentUploadResponse(BaseModel):
    upload_id: str
    filename: str
    content_hash: str
    size_bytes: int
    document_id: str | None = None
    file_type: str | None = None
    page_count: int = 0
    extraction_status: str | None = None
    ocr_used: bool = False
    warnings: Sequence[str] = ()


class ApiSpecUploadResponse(BaseModel):
    upload_id: str
    filename: str
    content_hash: str
    spec_id: str | None = None
    spec_format: str | None = None
    schema_id: str | None = None
    operations_count: int = 0
    entities_count: int = 0
    #: Always zero. Stated in the response so the boundary is visible.
    endpoints_called: int = 0
    warnings: Sequence[str] = ()


class SchemaResponse(BaseModel):
    schema_id: str
    source_system_id: str
    schema_name: str
    schema_version: int | None = None
    schema_hash: str | None = None
    entities: Sequence[Mapping[str, Any]] = ()
    relationship_count: int = 0


class MappingSuggestRequest(BaseModel):
    schema_id: str
    strict: bool = False


class MappingResponse(BaseModel):
    mapping_id: str | None = None
    schema_id: str
    status: str
    total_fields: int = 0
    mapped_fields: int = 0
    ambiguous_fields: int = 0
    unmapped_fields: int = 0
    review_required_fields: int = 0
    #: Per-field explainability from Phase 8.
    decisions: Sequence[Mapping[str, Any]] = ()
    ambiguities: Sequence[Mapping[str, Any]] = ()
    collisions: Sequence[Mapping[str, Any]] = ()
    auto_approved: bool = False


class MappingOverrideRequest(BaseModel):
    source_field: str
    target_path: str | None = None
    action: str = Field(
        default="approve",
        description="approve, reject, or override",
        pattern="^(approve|reject|override)$",
    )
    reason: str | None = Field(default=None, max_length=500)


class MappingUpdateRequest(BaseModel):
    overrides: Sequence[MappingOverrideRequest] = ()
    expected_version: int | None = None


class MappingValidationResponse(BaseModel):
    mapping_id: str
    valid: bool
    blocking_issues: Sequence[Mapping[str, Any]] = ()
    type_conflicts: Sequence[Mapping[str, Any]] = ()
    collisions: Sequence[Mapping[str, Any]] = ()
    review_required: bool = False
    required_target_coverage: float | None = None


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_type": "structured_pipeline",
                "source_id": "erp_finance",
                "schema_id": "schema_abc123",
                "mapping_id": "map_abc123",
                "options": {"limit": 100},
            }
        }
    )

    job_type: JobType
    source_id: str | None = None
    schema_id: str | None = None
    mapping_id: str | None = None
    upload_id: str | None = None
    entity: str | None = None
    options: Mapping[str, Any] = Field(default_factory=dict)


class StageResponse(BaseModel):
    stage: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    detail: str | None = None
    error_code: str | None = None
    outputs: Mapping[str, Any] = Field(default_factory=dict)
    warnings: Sequence[str] = ()


class JobResponse(BaseModel):
    job_id: str
    status: str
    job_type: str
    current_stage: str | None = None
    stages: Sequence[StageResponse] = ()
    counters: Mapping[str, int] = Field(default_factory=dict)
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    warnings: Sequence[str] = ()
    outputs: Mapping[str, Any] = Field(default_factory=dict)
    status_url: str | None = None


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: str
    job_type: str
    created_at: str
    status_url: str


class SearchRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "query": "unpaid supplier invoice in euros",
                "top_k": 10,
                "include_cold": False,
            }
        }
    )

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=100)
    #: Off by default: cold search rehydrates archives and is expensive.
    include_cold: bool = False
    filters: Mapping[str, Any] = Field(default_factory=dict)


class SearchHitResponse(BaseModel):
    """One result. Deliberately has no vector field."""

    representation_id: str
    record_id: str | None = None
    entity_type: str | None = None
    score: float
    tier: str
    metadata: Mapping[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query_model: str
    dimension: int
    hits: Sequence[SearchHitResponse] = ()
    tiers_searched: Sequence[str] = ()
    include_cold: bool = False
    #: True when archives were rehydrated to answer this query.
    deep_search_used: bool = False
    deep_search_note: str | None = None
    took_ms: float | None = None


class RecordResponse(BaseModel):
    record_id: str
    entity_type: str | None = None
    source_system_id: str | None = None
    content_hash: str | None = None
    data: Mapping[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    api_version: str


class DependencyHealth(BaseModel):
    name: str
    configured: bool
    ready: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: str
    ready: bool
    dependencies: Sequence[DependencyHealth] = ()


class CapabilitiesResponse(BaseModel):
    api_version: str
    engine_version: str
    source_types: Sequence[str] = ()
    file_types: Sequence[str] = ()
    api_spec_formats: Sequence[str] = ()
    job_types: Sequence[str] = ()
    storage_tiers: Sequence[str] = ()
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    incremental_sync_supported: bool = False
    #: Honest statements about what this component does NOT do.
    limitations: Sequence[str] = ()


__all__ = [
    "SourceCreate",
    "SourceResponse",
    "ConnectionTestResponse",
    "DiscoveryResponse",
    "CsvUploadResponse",
    "DocumentUploadResponse",
    "ApiSpecUploadResponse",
    "SchemaResponse",
    "MappingSuggestRequest",
    "MappingResponse",
    "MappingOverrideRequest",
    "MappingUpdateRequest",
    "MappingValidationResponse",
    "JobCreateRequest",
    "StageResponse",
    "JobResponse",
    "JobAcceptedResponse",
    "SearchRequest",
    "SearchHitResponse",
    "SearchResponse",
    "RecordResponse",
    "HealthResponse",
    "DependencyHealth",
    "ReadinessResponse",
    "CapabilitiesResponse",
]
