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
from erp_pipeline.schemas.enums import SensitivityLevel, SourceType


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
    #: Phase 7. The job indexing this schema as searchable STRUCTURE, and its
    #: status as the response was written. ``None`` for both means indexing did
    #: not start; the reason is in ``warnings``.
    #:
    #: Indexing the schema does NOT index the source's rows - those still go
    #: through mapping review or the source-native pipeline.
    schema_index_job_id: str | None = None
    schema_indexing_status: str | None = None
    warnings: Sequence[str] = ()


class CsvUploadResponse(BaseModel):
    """The result of a CSV upload.

    ROW COUNTS ARE TWO DIFFERENT FACTS, AND THE API SAYS WHICH IS WHICH
    Schema inference reads a BOUNDED SAMPLE of the file
    (``CsvOptions.max_rows_for_schema_inference``); it does not walk the whole
    file, because inferring a column's type does not require doing so. Reporting
    that sample as a total row count would be a measurement the pipeline never
    took, so the two are separate fields and ``rows_observed`` stays ``null``
    until something genuinely counts every row.
    """

    upload_id: str
    filename: str
    content_hash: str
    size_bytes: int
    source_system_id: str | None = None
    schema_id: str | None = None
    columns: int = 0
    #: Data rows actually inspected during schema inference. Always known.
    rows_sampled: int = 0
    #: True when the sample stopped at the configured limit, so the file
    #: contains AT LEAST ``rows_sampled`` rows and probably more.
    sample_limited: bool = False
    #: Total data rows in the file. ``null`` means NOT COUNTED - the ingestion
    #: path that produced this response samples rather than counts. It is never
    #: filled in from ``rows_sampled``, which would assert a total nobody
    #: measured.
    rows_observed: int | None = None
    published: bool = False
    #: Phase 7. The job indexing this CSV's inferred STRUCTURE. The rows
    #: themselves are untouched: they still require a mapping decision before
    #: anything about them is indexed.
    schema_index_job_id: str | None = None
    schema_indexing_status: str | None = None
    warnings: Sequence[str] = ()


class DocumentUploadResponse(BaseModel):
    """The result of a PDF or image upload.

    ``document_id`` is CONTENT-ADDRESSED, matching the identity
    ``ai.chunking.chunk_document`` already derives, so the id returned here is
    the same id a later chunk will carry. Uploading identical bytes twice
    therefore yields the same document id, and editing the file yields a new
    one - which is the property that makes a chunk traceable to its document.
    """

    upload_id: str
    filename: str
    content_hash: str
    size_bytes: int
    document_id: str | None = None
    file_type: str | None = None
    page_count: int = 0
    extraction_status: str | None = None
    #: Phase 6. The indexing job this upload started, and that job's CURRENT
    #: status - not a promise about its outcome.
    #:
    #: ``None`` for both means no job was started, and ``indexing_error`` says
    #: why. A caller polls ``GET /v1/jobs/{index_job_id}`` for the authoritative
    #: lifecycle; this field is a snapshot taken as the upload returned.
    #:
    #: Deliberately NOT a ``searchable`` flag. A document is searchable only
    #: once its representation is persisted, embedded, and routed to a
    #: searchable tier, and none of that has happened when this response is
    #: written.
    index_job_id: str | None = None
    indexing_status: str | None = None
    #: Set only when the upload succeeded and indexing could not be STARTED.
    #: A job that starts and later fails reports through the job, not here.
    indexing_error: str | None = None
    #: True when OCR actually produced text on at least one page. Derived from
    #: the extractor's own per-page ``extraction_method``, never from whether
    #: OCR was merely enabled or available.
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


class SchemaFieldResponse(BaseModel):
    """One source field, with BOTH of its type views.

    The two type fields are not redundant and must not be collapsed:

    ``source_data_type``      the vendor's own declaration, verbatim -
                              ``NUMERIC(12,2)``, ``NVARCHAR``, ``ObjectId``.
                              Precision and vendor spelling are unrecoverable
                              once discarded, and a consumer generating typed
                              tooling needs them.
    ``normalized_data_type``  the coarse cross-source classification the
                              mapping engine reasons about.
    """

    source_name: str
    normalized_name: str
    source_data_type: str | None = None
    normalized_data_type: str | None = None
    nullable: bool | None = None
    required: bool | None = None
    is_primary_key: bool | None = None
    is_unique: bool | None = None
    is_array: bool | None = None
    #: Dotted path for a nested field (MongoDB, OpenAPI); ``None`` when flat.
    nested_path: Sequence[str] | None = None
    #: Declared field meaning (email, iban, tax_id, ...). Currently always
    #: ``None`` - the contract slot exists but no producer populates it yet.
    semantic_type: str | None = None
    description: str | None = None
    ordinal: int | None = None


class SchemaEntityResponse(BaseModel):
    """One source entity and its fields."""

    entity_id: str
    source_name: str
    normalized_name: str
    entity_kind: str | None = None
    field_count: int = 0
    primary_key_fields: Sequence[str] = ()
    fields: Sequence[SchemaFieldResponse] = ()


class SchemaRelationshipResponse(BaseModel):
    """One relationship between two source entities.

    Field names mirror the ``SourceRelationship`` contract exactly
    (``from_*`` / ``to_*``) rather than being renamed in transit, so a
    consumer reading the contract and a consumer reading this response are
    looking at the same thing.
    """

    relationship_id: str
    relationship_type: str | None = None
    from_entity: str
    from_fields: Sequence[str] = ()
    to_entity: str
    to_fields: Sequence[str] = ()
    #: 1.0 for a declared constraint; lower for an inferred relationship.
    confidence: float = 1.0


class SchemaResponse(BaseModel):
    schema_id: str
    source_system_id: str
    schema_name: str
    #: How this schema was obtained. ``discovered`` means the source DECLARED it
    #: - a relational catalog was read. ``inferred`` means it was OBSERVED from a
    #: bounded sample of documents, as MongoDB requires, and is therefore true of
    #: what was sampled rather than guaranteed of the whole collection.
    #:
    #: Exposed because a consumer that cannot tell these apart would treat an
    #: observation as a guarantee.
    origin: str | None = None
    schema_version: int | None = None
    schema_hash: str | None = None
    entities: Sequence[SchemaEntityResponse] = ()
    #: The declared foreign-key graph. Previously only its size was exposed,
    #: which left a consumer unable to reconstruct ERP entity relationships.
    relationships: Sequence[SchemaRelationshipResponse] = ()
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
                "filters": {"entity_type": "invoice"},
            }
        }
    )

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=100)
    #: Off by default: cold search rehydrates archives and is expensive.
    include_cold: bool = False
    #: Equality constraints over a CLOSED set of identity fields:
    #: ``entity_type``, canonical ``record_key``, source provenance,
    #: sensitivity and document identity.
    #:
    #: An unsupported field name is REFUSED with 422 rather than ignored. A
    #: silently dropped filter returns a plausible-looking unfiltered result,
    #: which is the worst possible answer for a caller about to hand those
    #: results to a governance model.
    filters: Mapping[str, Any] = Field(
        default_factory=dict,
        description=(
            "Equality filters over identity/provenance fields plus fields from "
            "the current discovered source schema. See GET /v1/search (no "
            "query parameters) for the live, discoverable set. An unsupported "
            "field is rejected, never ignored."
        ),
    )


class SearchHitResponse(BaseModel):
    """One result. Deliberately has no vector field."""

    representation_id: str
    #: The canonical record this hit resolves to, suitable for
    #: ``GET /v1/records/{record_id}`` exactly as returned.
    #:
    #: ``None`` means the stored vector genuinely carries no canonical
    #: reference - it predates the field, or derives from no canonical record.
    #: It is never reconstructed by parsing ``representation_id``: that id is
    #: normalized (``:`` becomes ``_``) and the original cannot be recovered.
    canonical_record_id: str | None = None
    #: Retained for backward compatibility. Mirrors ``canonical_record_id``.
    record_id: str | None = None
    source_system_id: str | None = None
    source_entity: str | None = None
    record_key: str | None = None
    content_kind: str | None = None
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
    #: The filters that were actually applied, echoed back so a caller can
    #: confirm the query it thinks it ran is the query that ran.
    filters_applied: Mapping[str, str] = Field(default_factory=dict)
    #: True when archives were rehydrated to answer this query.
    deep_search_used: bool = False
    deep_search_note: str | None = None
    took_ms: float | None = None


class AvailableSearchFieldResponse(BaseModel):
    """One field of one discovered entity, as ``GET /v1/search`` reports it
    when called with no query.
    """

    name: str
    #: The coarse normalized type - ``STRING``, ``INTEGER``, ``DECIMAL``, ...
    type: str
    #: True when this field is the entity's primary key (or part of a
    #: composite one) - the canonical ``record_key`` for this entity.
    business_key: bool = False
    #: Whether an equality filter on this field is currently accepted by
    #: ``GET /v1/search``. False (rather than omitted) when the field is a
    #: genuine discovered column that is not yet indexed in Qdrant.
    filterable: bool = True
    description: str | None = None
    #: A safe example, or ``None``. Populated only from a schema-declared
    #: enum constraint - never from sampled business data. Absence is the
    #: honest answer for almost every field; it is not an omission.
    example_value: str | None = None


class AvailableSearchEntityResponse(BaseModel):
    """One discovered ``(source_system_id, source_entity)`` and its fields."""

    source_system_id: str | None = None
    source_entity: str
    entity_kind: str | None = None
    description: str | None = None
    #: Whether this entity currently has an operational search path - the
    #: embedding and storage services are configured and at least one
    #: filterable field was discovered. It does NOT assert that indexed
    #: vectors exist yet; a query for a genuinely empty entity still returns
    #: zero hits rather than an error.
    searchable: bool = True
    fields: Sequence[AvailableSearchFieldResponse] = ()


class IdentityFilterFieldResponse(BaseModel):
    """One entry of the closed, entity-independent identity/provenance
    filter set - ``source_system_id``, ``record_key``, ``sensitivity``, and
    so on. These mean the same thing for every entity, so they are listed
    once rather than repeated under each one.
    """

    name: str
    description: str
    filterable: bool = True


class SearchMetadataResponse(BaseModel):
    """What ``GET /v1/search`` returns when called with no search parameters.

    The single source of search discoverability: which systems and entities
    are known, which of their fields can be filtered, and the fixed
    identity/provenance filters that apply everywhere. There is no separate
    metadata or options endpoint - this shape IS ``GET /v1/search``.

    ``extra="forbid"``: the route's response model is
    ``SearchMetadataResponse | SearchResponse``, and the two share no field
    names. Forbidding extras here means a ``SearchResponse``-shaped payload
    can never be mistaken for an all-defaults-empty metadata response -
    Pydantic is forced to match it against ``SearchResponse`` instead.
    """

    model_config = ConfigDict(extra="forbid")

    available_search: Sequence[AvailableSearchEntityResponse] = ()
    identity_filters: Sequence[IdentityFilterFieldResponse] = ()
    control_parameters: Sequence[str] = (
        "q",
        "limit",
        "include_cold",
        "employee_id",
    )
    qdrant_indexes_verified: bool = False


class RepresentationResponse(BaseModel):
    """One stored representation, with the text a model would actually see.

    This is the only place in the API that returns representation TEXT.
    ``GET /v1/search`` deliberately does not: a caller picks which hit is worth
    expanding, so ranked results stay small and extracted document content is
    exposed once rather than N times per query.

    Never carries a vector, and never carries the bytes the text came from.
    """

    representation_id: str
    entity_type: str | None = None
    content_kind: str | None = None
    #: The AI-ready text, exactly as embedded. ``None`` when the representation
    #: genuinely has none - an image OCR could read nothing from still has an
    #: identity worth resolving, and saying so beats an empty string that looks
    #: like successfully-extracted emptiness.
    text: str | None = None
    content_hash: str | None = None
    canonical_record_id: str | None = None
    parent_record_id: str | None = None
    source_system_id: str | None = None
    source_entity: str | None = None
    record_key: str | None = None
    source_field: str | None = None
    business_key_name: str | None = None
    business_key_value: str | None = None
    document_id: str | None = None
    document_type: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    chunk_index: int | None = None
    #: Phase 7 schema provenance. Set only for ``content_kind = schema``.
    #: ``schema_id`` is the snapshot this structure came from - it changes when
    #: the schema does, which is what makes the catalog versionable.
    schema_name: str | None = None
    entity_kind: str | None = None
    schema_id: str | None = None
    schema_version: str | None = None
    #: Stable across schema versions, unlike ``schema_id``.
    entity_id: str | None = None
    schema_chunk_index: int | None = None
    sensitivity: str | None = None
    #: The canonical records this representation was built from.
    source_record_ids: Sequence[str] = ()


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


class CapabilityStatus(BaseModel):
    """One capability reported as three separate facts (Phase 11).

    ``supported`` and ``enabled`` are deliberately not the same question.
    ``supported`` says this build contains the implementation; ``enabled`` says
    THIS deployment has it wired up. Collapsing them into a single boolean is
    how an integration contract starts lying: remote asset fetching is
    implemented and ships disabled, and a consumer told only "true" would plan
    around a feature that refuses every call it makes.

    A capability advertised here must have a contract test that exercises it.
    """

    supported: bool
    enabled: bool
    #: Why it is disabled, or what it is bounded by. Never a marketing claim.
    detail: str | None = None


class CapabilitiesResponse(BaseModel):
    api_version: str
    engine_version: str
    source_types: Sequence[str] = ()
    file_types: Sequence[str] = ()
    api_spec_formats: Sequence[str] = ()
    job_types: Sequence[str] = ()
    #: Phase 7. The ``content_kind`` values ``GET /v1/search`` accepts, so a
    #: downstream consumer can discover that schema retrieval exists without
    #: probing for it. Derived from the enum, never hand-written: a capability
    #: list that could drift from the filter contract would advertise
    #: something the system refuses.
    content_kinds: Sequence[str] = ()
    storage_tiers: Sequence[str] = ()
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    incremental_sync_supported: bool = False
    #: Phase 11. What Members 2 and 3 need to discover before integrating,
    #: keyed by a stable capability name. The pre-existing scalar fields above
    #: are left exactly as they were: a consumer written against Phase 13 keeps
    #: working, and this is additional information rather than a replacement.
    integration_capabilities: Mapping[str, CapabilityStatus] = Field(
        default_factory=dict
    )
    #: Honest statements about what this component does NOT do.
    limitations: Sequence[str] = ()


# ----------------------------------------------------------------------
# Response adaptation (Phase 14)
# ----------------------------------------------------------------------


class ResponseAdaptOptions(BaseModel):
    """Per-request budgets. Every field is optional and defaults to the
    engine's configured value, so a caller who does not care about budgets
    sends nothing and still gets a bounded result."""

    minimum_relevance_score: float | None = Field(default=None, ge=0.0, le=1.0)
    max_fields: int | None = Field(default=None, ge=1, le=200)
    max_output_characters: int | None = Field(default=None, ge=100, le=200_000)
    max_value_characters: int | None = Field(default=None, ge=10, le=50_000)
    #: The ablation switch. Off means every field is kept, which is the
    #: GENERIC baseline rather than a degraded mode.
    enable_relevance_selection: bool = True
    #: Off makes the engine skip canonical mapping entirely and pass source
    #: field names through - exposed so the ERP-awareness contribution can be
    #: measured against its own absence.
    enable_erp_mapping: bool = True


class ResponseAssetReference(BaseModel):
    """A URL the ERP response referenced.

    Fetching is refused unless the deployment explicitly enables it AND the URL
    passes the SSRF policy. A caller cannot turn fetching on from the request
    body - that would let a remote ERP response choose what this service
    connects to.
    """

    url: str = Field(min_length=1, max_length=2048)
    declared_content_type: str | None = Field(default=None, max_length=200)
    label: str | None = Field(default=None, max_length=200)


class ResponseAdaptRequest(BaseModel):
    """One already-executed ERP API response, plus the question it answers.

    This endpoint does NOT call an ERP system. The caller has already made the
    call; what arrives here is what came back.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "query": "How much is invoice INV-204 for?",
                "source_system_id": "finance_erp",
                "endpoint": "/api/invoices/INV-204",
                "http_status": 200,
                "content_type": "application/json",
                "body": {
                    "result": {
                        "inv_no": "INV-204",
                        "cust_ref": "CUS-17",
                        "total_amt": "45000.00",
                        "curr": "LKR",
                        "approval_status": "A",
                    },
                    "success": True,
                },
            }
        }
    )

    query: str | None = Field(default=None, max_length=2000)
    source_system_id: str = Field(default="unknown_erp", max_length=200)
    endpoint: str | None = Field(default=None, max_length=2048)
    http_status: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = Field(default=None, max_length=200)
    #: The decoded JSON body, when the caller already parsed it.
    body: Any = None
    #: Base64 bytes, for an image or PDF response. Present because this is the
    #: ONE adaptation endpoint and a binary response has to reach it somehow.
    body_base64: str | None = Field(default=None, max_length=20_000_000)
    headers: Mapping[str, str] = Field(default_factory=dict)
    asset_urls: Sequence[ResponseAssetReference] = ()
    entity_hint: str | None = Field(default=None, max_length=200)
    #: CONSUMED, never inferred. The caller states the classification the data
    #: already carries; this endpoint does not examine values to guess one.
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    options: ResponseAdaptOptions = Field(default_factory=ResponseAdaptOptions)


class ResponseAdaptResponse(BaseModel):
    """The adapted response.

    ``llm_ready`` is the payload to put in front of a model. Everything else is
    evidence: what was detected, what was selected, what was dropped and why,
    and what the transformation measurably cost and saved.
    """

    response_type: str
    entity_type: str | None = None
    llm_ready: Mapping[str, Any] = Field(default_factory=dict)
    assets: Sequence[Mapping[str, Any]] = ()
    provenance: Mapping[str, Any] | None = None
    transformation: Mapping[str, Any] = Field(default_factory=dict)
    report: Mapping[str, Any] | None = None
    warnings: Sequence[str] = ()
    success: bool = True
    #: Succeeded, but something inside it did not - a refused asset URL, a
    #: budget truncation. A caller that treats this as a plain success is
    #: reading a partial answer as a complete one.
    partial: bool = False


__all__ = [
    "SourceCreate",
    "SourceResponse",
    "ConnectionTestResponse",
    "DiscoveryResponse",
    "CsvUploadResponse",
    "DocumentUploadResponse",
    "ApiSpecUploadResponse",
    "SchemaResponse",
    "SchemaEntityResponse",
    "SchemaFieldResponse",
    "SchemaRelationshipResponse",
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
    "AvailableSearchFieldResponse",
    "AvailableSearchEntityResponse",
    "IdentityFilterFieldResponse",
    "SearchMetadataResponse",
    "SearchHitResponse",
    "SearchResponse",
    "RecordResponse",
    "HealthResponse",
    "DependencyHealth",
    "ReadinessResponse",
    "CapabilitiesResponse",
    "CapabilityStatus",
    "ResponseAdaptOptions",
    "ResponseAssetReference",
    "ResponseAdaptRequest",
    "ResponseAdaptResponse",
]
