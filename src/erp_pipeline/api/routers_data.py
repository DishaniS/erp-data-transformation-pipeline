"""Upload, schema, mapping, job, search and record routes."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from erp_pipeline.api.schemas import (
    ApiSpecUploadResponse,
    CsvUploadResponse,
    DocumentUploadResponse,
    JobAcceptedResponse,
    JobCreateRequest,
    JobResponse,
    MappingResponse,
    MappingSuggestRequest,
    MappingUpdateRequest,
    MappingValidationResponse,
    RecordResponse,
    SchemaResponse,
    SearchHitResponse,
    SearchRequest,
    SearchResponse,
)
from erp_pipeline.api.routers import get_service, get_settings
from erp_pipeline.orchestration import (
    InvalidPipelineRequestError,
    JobRequest,
    JobStatus,
    JobType,
    MappingNotFoundError,
    OrchestrationService,
    RecordNotFoundError,
    SchemaNotFoundError,
    UnsupportedUploadError,
)

LOGGER = logging.getLogger("erp_pipeline.api.routers_data")

# ----------------------------------------------------------------------
# Uploads
# ----------------------------------------------------------------------

files_router = APIRouter(prefix="/v1/files", tags=["files"])


def _publish_file_schema(services: Any, schema: Any) -> tuple[bool, str | None]:
    """Register the uploaded file's source system, then publish its schema.

    WHY THE REGISTRATION STEP EXISTS
    --------------------------------
    ``schema_snapshots.source_system_id`` carries a foreign key to
    ``source_systems``. An uploaded file's schema is attributed to the logical
    system named by ``IngestionOptions.source_system_id`` (``file_source`` by
    default), which nothing else ever creates a row for. Publishing without
    registering it first therefore raised ``SourceSystemNotFoundError`` on
    every single upload.

    That failure used to be caught and discarded, so the API reported a
    successful upload while the catalog stayed empty. Registration is
    idempotent, so doing it here is safe on every call.

    Returns ``(published, problem)``. A problem is returned rather than raised
    because the upload itself genuinely succeeded - the bytes are stored and
    the schema was inferred. The caller surfaces it as a warning so the
    response can never again claim more than actually happened.
    """
    catalog = services.catalog
    ingestion = getattr(services, "ingestion", None)

    try:
        if ingestion is not None and hasattr(ingestion, "source_system"):
            catalog.register_source_system(ingestion.source_system())

        catalog.publish_schema(schema)

        return True, None
    except Exception as error:  # noqa: BLE001 - reported, never discarded
        # The exception type and message are logged for an operator. Neither is
        # echoed to the client verbatim, because a driver error can embed a
        # connection string.
        LOGGER.warning(
            "schema inferred but not published to the catalog",
            exc_info=True,
            extra={
                "schema_id": getattr(schema, "schema_id", None),
                "source_system_id": getattr(schema, "source_system_id", None),
                "error_type": type(error).__name__,
            },
        )

        return False, (
            "the schema was inferred but could not be published to the "
            f"catalog ({type(error).__name__}); it is available for this "
            "process only and will not survive a restart"
        )

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
DOCUMENT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


def _store_upload(service: OrchestrationService, upload: UploadFile, allowed: set[str]):
    """Stream to disk, then check the type. Never trusts the filename alone."""
    uploads = service.uploads

    if uploads is None:
        raise InvalidPipelineRequestError("this deployment has no upload store")

    stored = uploads.store_stream(
        upload.file, upload.filename, upload.content_type
    )

    if stored.suffix not in allowed:
        raise UnsupportedUploadError(
            f"{stored.suffix or 'this file type'} is not accepted by this "
            "endpoint",
            accepted=sorted(allowed),
        )

    return stored


@files_router.post(
    "/csv", response_model=CsvUploadResponse, status_code=201, operation_id="uploadCsv"
)
def upload_csv(
    file: UploadFile = File(...),
    service: OrchestrationService = Depends(get_service),
) -> CsvUploadResponse:
    """Upload a CSV and infer its schema through Phase 6.

    Returns metadata only. The rows themselves are not echoed: an ingestion
    endpoint that replayed business data would be an accidental data-export
    endpoint.
    """
    stored = _store_upload(service, file, CSV_SUFFIXES)
    result = service.services.ingest_upload(stored.upload_id)
    schema = getattr(result, "schema", None)
    published = False
    warnings = [str(w) for w in (getattr(result, "warnings", ()) or ())][:20]

    if schema is not None and service.services.catalog is not None:
        published, problem = _publish_file_schema(service.services, schema)

        if problem is not None:
            warnings.append(problem)

    if schema is not None:
        service.services.schema_cache[schema.schema_id] = schema

    return CsvUploadResponse(
        upload_id=stored.upload_id,
        filename=stored.display_name,
        content_hash=stored.content_hash,
        size_bytes=stored.size_bytes,
        source_system_id=getattr(schema, "source_system_id", None),
        schema_id=getattr(schema, "schema_id", None),
        columns=sum(len(e.fields) for e in getattr(schema, "entities", ()) or ()),
        rows_observed=getattr(result, "data_row_count", 0) or 0,
        published=published,
        warnings=warnings[:21],
    )


@files_router.post(
    "/documents",
    response_model=DocumentUploadResponse,
    status_code=201,
    operation_id="uploadDocument",
)
def upload_document(
    file: UploadFile = File(...),
    service: OrchestrationService = Depends(get_service),
) -> DocumentUploadResponse:
    """Upload a PDF or image and extract it through Phase 6.

    Returns metadata only - never the extracted text, which is the document's
    entire content.
    """
    stored = _store_upload(service, file, DOCUMENT_SUFFIXES)
    result = service.services.ingest_upload(stored.upload_id)
    document = getattr(result, "document", None)

    return DocumentUploadResponse(
        upload_id=stored.upload_id,
        filename=stored.display_name,
        content_hash=stored.content_hash,
        size_bytes=stored.size_bytes,
        document_id=getattr(document, "document_id", None),
        file_type=str(getattr(getattr(result, "file", None), "file_type", "") or ""),
        page_count=len(getattr(document, "pages", ()) or ()),
        extraction_status=str(getattr(result, "status", "") or ""),
        ocr_used=bool(getattr(document, "ocr_used", False)),
        warnings=[str(w) for w in (getattr(result, "warnings", ()) or ())][:20],
    )


# ----------------------------------------------------------------------
# API specifications
# ----------------------------------------------------------------------

specs_router = APIRouter(prefix="/v1/api-specs", tags=["api-specs"])

SPEC_SUFFIXES = {".json", ".yaml", ".yml"}


def _parse_spec(service: OrchestrationService, upload: UploadFile, label: str):
    stored = _store_upload(service, upload, SPEC_SUFFIXES)
    result = service.services.parse_api_spec(stored.upload_id)
    specification = getattr(result, "specification", None)
    schema = getattr(result, "schema", None)

    if schema is not None:
        service.services.schema_cache[schema.schema_id] = schema

    return ApiSpecUploadResponse(
        upload_id=stored.upload_id,
        filename=stored.display_name,
        content_hash=stored.content_hash,
        spec_id=getattr(specification, "spec_id", None),
        spec_format=str(getattr(specification, "spec_format", "") or label),
        schema_id=getattr(schema, "schema_id", None),
        # `operations` lives on the RESULT, not on the specification object.
        operations_count=len(getattr(result, "operations", ()) or ()),
        entities_count=len(getattr(schema, "entities", ()) or ()),
        endpoints_called=0,
        warnings=[str(w) for w in (getattr(result, "warnings", ()) or ())][:20],
    )


@specs_router.post(
    "/openapi",
    response_model=ApiSpecUploadResponse,
    status_code=201,
    operation_id="uploadOpenApiSpec",
)
def upload_openapi(
    file: UploadFile = File(...),
    service: OrchestrationService = Depends(get_service),
) -> ApiSpecUploadResponse:
    """Parse an OpenAPI or Swagger document as a CONTRACT.

    The documented endpoints are never called. ``endpoints_called`` is in the
    response so that boundary is visible rather than merely promised.
    """
    return _parse_spec(service, file, "openapi")


@specs_router.post(
    "/postman",
    response_model=ApiSpecUploadResponse,
    status_code=201,
    operation_id="uploadPostmanCollection",
)
def upload_postman(
    file: UploadFile = File(...),
    service: OrchestrationService = Depends(get_service),
) -> ApiSpecUploadResponse:
    """Parse a Postman collection structurally.

    Pre-request scripts, test scripts and the collection's requests are never
    executed - a collection is an untrusted document, not a program to run.
    """
    return _parse_spec(service, file, "postman")


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------

schemas_router = APIRouter(prefix="/v1/schemas", tags=["schemas"])


@schemas_router.get(
    "/{schema_id}", response_model=SchemaResponse, operation_id="getSchema"
)
def get_schema(
    schema_id: str,
    version: int | None = Query(default=None),
    service: OrchestrationService = Depends(get_service),
) -> SchemaResponse:
    schema = service.services.get_schema(schema_id)

    return SchemaResponse(
        schema_id=schema.schema_id,
        source_system_id=schema.source_system_id,
        schema_name=schema.schema_name,
        schema_version=getattr(schema, "schema_version", None),
        schema_hash=getattr(schema, "schema_hash", None),
        entities=[
            {
                "entity_id": entity.entity_id,
                "source_name": entity.source_name,
                "normalized_name": entity.normalized_name,
                "field_count": len(entity.fields),
                "primary_key_fields": list(entity.primary_key_fields),
                "fields": [
                    {
                        "source_name": field.source_name,
                        "data_type": str(getattr(field, "data_type", "")),
                        "nullable": getattr(field, "nullable", None),
                    }
                    for field in entity.fields
                ],
            }
            for entity in schema.entities
        ],
        relationship_count=len(getattr(schema, "relationships", ()) or ()),
    )


# ----------------------------------------------------------------------
# Mappings
# ----------------------------------------------------------------------

mappings_router = APIRouter(prefix="/v1/mappings", tags=["mappings"])


def _mapping_response(
    service: OrchestrationService,
    schema_id: str,
    result: Any,
    mapping_id: str | None = None,
):
    """Shape a Phase 8 result, keeping executable and draft mappings distinct.

    Phase 8 emits no profile while fields are ambiguous. The mapping is still
    given an id so a human can address and resolve it, but it is filed as a
    DRAFT - and `get_mapping_profile` refuses to hand a draft to a pipeline.
    """
    import uuid as _uuid

    coverage = result.coverage
    profile = result.profiles[0] if result.profiles else None
    ambiguous = getattr(coverage, "ambiguous_fields", 0)

    if profile is not None:
        mapping_id = getattr(profile, "mapping_id", None) or mapping_id
        if mapping_id:
            service.services.mapping_cache[mapping_id] = profile
            service.services.mapping_drafts.pop(mapping_id, None)
    else:
        mapping_id = mapping_id or f"mapdraft_{_uuid.uuid4().hex[:12]}"
        service.services.mapping_drafts[mapping_id] = {
            "schema_id": schema_id,
            "ambiguous_fields": ambiguous,
            "result": result,
        }

    return MappingResponse(
        mapping_id=mapping_id,
        schema_id=schema_id,
        status=str(getattr(profile, "status", "") or "generated"),
        total_fields=getattr(coverage, "total_fields", 0),
        mapped_fields=getattr(coverage, "mapped_fields", 0),
        ambiguous_fields=ambiguous,
        unmapped_fields=getattr(coverage, "unmapped_fields", 0),
        review_required_fields=getattr(coverage, "review_required_fields", 0),
        decisions=[
            {
                "source_field": getattr(d, "source_field_name", None)
                or str(getattr(d, "source_field", "")),
                "outcome": str(getattr(d, "outcome", "")),
                "target_path": getattr(d, "target_path", None),
                "confidence": str(getattr(d, "confidence", "")),
            }
            for d in list(getattr(result, "decisions", ()) or ())[:200]
        ],
        ambiguities=[
            {
                "source_field": str(getattr(a, "source_field", "")),
                # The candidates a human must choose between, with the scores
                # that tied. Without these the caller cannot resolve anything.
                "best_target": getattr(a, "best_target", None),
                "runner_up_target": getattr(a, "runner_up_target", None),
                "best_score": getattr(a, "best_score", None),
                "runner_up_score": getattr(a, "runner_up_score", None),
                "required_margin": getattr(a, "required_margin", None),
            }
            for a in list(getattr(result, "ambiguities", ()) or ())[:100]
        ],
        collisions=[
            {"target_path": str(getattr(c, "target_path", ""))}
            for c in list(getattr(result, "collisions", ()) or ())[:100]
        ],
        # Ambiguity is never auto-approved. Phase 8 decided a human is needed,
        # and an executable profile only exists once they have decided.
        auto_approved=bool(profile) and not ambiguous,
    )


@mappings_router.post(
    "/suggest", response_model=MappingResponse, operation_id="suggestMapping"
)
def suggest_mapping(
    payload: MappingSuggestRequest,
    service: OrchestrationService = Depends(get_service),
) -> MappingResponse:
    """Generate a mapping through Phase 8. Ambiguous fields stay unapproved."""
    schema = service.services.get_schema(payload.schema_id)
    mapping_service = service.services.mapping

    if mapping_service is None:
        raise InvalidPipelineRequestError("no mapping service is configured")

    result = mapping_service.generate(schema, strict=payload.strict)

    return _mapping_response(service, payload.schema_id, result)


@mappings_router.put(
    "/{mapping_id}", response_model=MappingResponse, operation_id="updateMapping"
)
def update_mapping(
    mapping_id: str,
    payload: MappingUpdateRequest,
    service: OrchestrationService = Depends(get_service),
) -> MappingResponse:
    """Apply human decisions by re-running Phase 8 with overrides.

    Overrides are fed back through the mapping engine rather than patched into
    the profile, so the engine's own validation still applies. A target path
    the canonical model does not define is refused there, not here.
    """
    from erp_pipeline.mapping import MappingOverride

    draft = service.services.mapping_drafts.get(mapping_id)

    if draft is not None:
        schema_id = draft["schema_id"]
    else:
        profile = service.services.get_mapping_profile(mapping_id)
        schema_id = getattr(profile, "source_schema_id", None) or getattr(
            profile, "schema_id", None
        )

    if not schema_id:
        raise MappingNotFoundError(
            f"mapping {mapping_id!r} does not reference a schema",
            mapping_id=mapping_id,
        )

    schema = service.services.get_schema(schema_id)
    overrides = []

    for item in payload.overrides:
        # Only an explicit target decides an ambiguity. "approve" without a
        # target would be a human waving through a choice they never made.
        if item.action in {"override", "approve"} and item.target_path:
            overrides.append(
                MappingOverride(
                    source_field=item.source_field,
                    target=item.target_path,
                    reason=item.reason or "manual decision via API",
                    decided_by="api",
                )
            )

    # The overrides go back through Phase 8, so its own validation still
    # applies. A target the canonical model does not define is refused there,
    # by the engine that owns the model - not by a check invented here.
    result = service.services.mapping.generate(schema, overrides=tuple(overrides))

    return _mapping_response(service, schema_id, result, mapping_id=mapping_id)


@mappings_router.post(
    "/{mapping_id}/validate",
    response_model=MappingValidationResponse,
    operation_id="validateMapping",
)
def validate_mapping(
    mapping_id: str, service: OrchestrationService = Depends(get_service)
) -> MappingValidationResponse:
    """Ask Phase 8 whether the mapping is executable. Moves no business data."""
    profile = service.services.get_mapping_profile(mapping_id)
    schema_id = getattr(profile, "source_schema_id", None) or getattr(
        profile, "schema_id", None
    )
    schema = service.services.get_schema(schema_id) if schema_id else None

    if schema is None:
        raise MappingNotFoundError(
            f"mapping {mapping_id!r} cannot be validated without its schema",
            mapping_id=mapping_id,
        )

    report = service.services.mapping.validate(profile, schema)
    issues = list(getattr(report, "issues", ()) or ())
    blocking = [i for i in issues if getattr(i, "blocking", False)]

    return MappingValidationResponse(
        mapping_id=mapping_id,
        valid=bool(getattr(report, "valid", not blocking)),
        blocking_issues=[{"message": str(i)} for i in blocking[:50]],
        review_required=bool(blocking),
    )


# ----------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------

jobs_router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


def _job_response(job: Any) -> JobResponse:
    payload = job.to_dict()
    payload["status_url"] = f"/v1/jobs/{job.job_id}"

    return JobResponse(**payload)


@jobs_router.post(
    "",
    response_model=JobAcceptedResponse,
    status_code=202,
    operation_id="createJob",
)
def create_job(
    payload: JobCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: OrchestrationService = Depends(get_service),
) -> JobAcceptedResponse:
    """Accept a job and return immediately.

    202, not 200: the pipeline runs on the executor afterwards. Blocking here
    until embeddings finished would tie a request socket to a model run.
    """
    request = JobRequest(
        job_type=payload.job_type,
        source_id=payload.source_id,
        schema_id=payload.schema_id,
        mapping_id=payload.mapping_id,
        upload_id=payload.upload_id,
        entity=payload.entity,
        options=dict(payload.options),
    )
    job = service.submit(request, idempotency_key=idempotency_key)

    return JobAcceptedResponse(
        job_id=job.job_id,
        status=job.status.value,
        job_type=job.request.job_type.value,
        created_at=job.created_at.isoformat(),
        status_url=f"/v1/jobs/{job.job_id}",
    )


@jobs_router.get("", response_model=list[JobResponse], operation_id="listJobs")
def list_jobs(
    status: JobStatus | None = Query(default=None),
    job_type: JobType | None = Query(default=None),
    source_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: OrchestrationService = Depends(get_service),
) -> list[JobResponse]:
    jobs = service.list(
        status=status,
        job_type=job_type,
        source_id=source_id,
        limit=limit,
        offset=offset,
    )

    return [_job_response(job) for job in jobs]


@jobs_router.get("/{job_id}", response_model=JobResponse, operation_id="getJob")
def get_job(
    job_id: str, service: OrchestrationService = Depends(get_service)
) -> JobResponse:
    return _job_response(service.get(job_id))


@jobs_router.post(
    "/{job_id}/retry", response_model=JobAcceptedResponse, status_code=202,
    operation_id="retryJob",
)
def retry_job(
    job_id: str, service: OrchestrationService = Depends(get_service)
) -> JobAcceptedResponse:
    job = service.retry(job_id)

    return JobAcceptedResponse(
        job_id=job.job_id,
        status=job.status.value,
        job_type=job.request.job_type.value,
        created_at=job.created_at.isoformat(),
        status_url=f"/v1/jobs/{job.job_id}",
    )


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------

search_router = APIRouter(prefix="/v1", tags=["search"])


@search_router.post("/search", response_model=SearchResponse, operation_id="search")
def search(
    payload: SearchRequest, service: OrchestrationService = Depends(get_service)
) -> SearchResponse:
    """Embed the query with Phase 11, retrieve with Phase 12.

    No LLM and no generated answer: this returns retrieved records, not prose
    about them. The response carries no vector - a search endpoint that
    returned embeddings would be an embedding-export endpoint.
    """
    services = service.services

    if services.embedding is None or services.storage is None:
        raise InvalidPipelineRequestError(
            "search needs both an embedding service and a vector store"
        )

    started = time.perf_counter()
    vector = services.embedding.model.encode([payload.query])[0]

    result = services.storage.search(
        vector, limit=payload.top_k, include_cold=payload.include_cold
    )

    hits = []

    for hit in result.hits:
        metadata = services.storage.state.load(hit.representation_id)
        hits.append(
            SearchHitResponse(
                representation_id=hit.representation_id,
                record_id=getattr(metadata, "representation_id", None),
                entity_type=getattr(metadata, "entity_type", None),
                score=round(float(hit.score), 6),
                tier=hit.tier.value,
                metadata={
                    "content_hash": getattr(metadata, "content_hash", None),
                    "model_id": getattr(metadata, "model_id", None),
                },
            )
        )

    tiers = [tier.value for tier in result.tiers_searched]
    deep = "cold" in tiers

    return SearchResponse(
        query_model=services.embedding.model_id,
        dimension=services.embedding.dimension,
        hits=hits,
        tiers_searched=tiers,
        include_cold=payload.include_cold,
        deep_search_used=deep,
        deep_search_note=(
            "archived vectors were rehydrated into a temporary index to answer "
            "this query; this costs materially more than a hot or warm search"
            if deep
            else None
        ),
        took_ms=round((time.perf_counter() - started) * 1000, 3),
    )


# ----------------------------------------------------------------------
# Records
# ----------------------------------------------------------------------

records_router = APIRouter(prefix="/v1/records", tags=["records"])


@records_router.get(
    "/{record_id:path}", response_model=RecordResponse, operation_id="getRecord"
)
def get_record(
    record_id: str, service: OrchestrationService = Depends(get_service)
) -> RecordResponse:
    """Return one canonical record. Never a vector, never a credential."""
    store = service.services.records

    if store is None:
        raise InvalidPipelineRequestError("no canonical record store is configured")

    record = store.get(record_id)

    if record is None:
        raise RecordNotFoundError(
            f"record {record_id!r} was not found", record_id=record_id
        )

    # The frozen Phase 1 contract serializes itself; Phase 13 does not invent a
    # second representation of a canonical record.
    if hasattr(record, "to_json_dict"):
        data = record.to_json_dict()
    elif hasattr(record, "to_dict"):
        data = record.to_dict()
    else:  # pragma: no cover - defensive
        data = dict(record)

    return RecordResponse(
        record_id=getattr(record, "record_id", None) or record_id,
        entity_type=getattr(record, "entity_type", None),
        source_system_id=getattr(record, "source_system_id", None),
        content_hash=getattr(record, "content_hash", None),
        # Business values only. Provenance and internal metadata stay out of the
        # payload body.
        data=data.get("normalized_data") or {},
    )


__all__ = [
    "files_router",
    "specs_router",
    "schemas_router",
    "mappings_router",
    "jobs_router",
    "search_router",
    "records_router",
]
