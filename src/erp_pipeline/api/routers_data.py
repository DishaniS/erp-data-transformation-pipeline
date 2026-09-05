"""Upload, schema, mapping, job, search and record routes."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Mapping

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse

from erp_pipeline.api.schemas import (
    ApiSpecUploadResponse,
    AvailableSearchEntityResponse,
    AvailableSearchFieldResponse,
    CsvUploadResponse,
    DocumentUploadResponse,
    IdentityFilterFieldResponse,
    JobAcceptedResponse,
    JobCreateRequest,
    JobResponse,
    MappingResponse,
    MappingSuggestRequest,
    MappingUpdateRequest,
    MappingValidationResponse,
    RecordResponse,
    RepresentationResponse,
    SchemaResponse,
    SearchHitResponse,
    SearchMetadataResponse,
    SearchRequest,
    SearchResponse,
)
from erp_pipeline.api.routers import get_service, get_settings
from erp_pipeline.api.serialization import schema_response
from erp_pipeline.storage.filters import (
    FILTERABLE_FIELDS,
    InvalidFilterValueError,
    SearchFilters,
    UnknownFilterFieldError,
)
from erp_pipeline.schemas.search_fields import (
    RESERVED_PAYLOAD_FIELDS,
    available_search_catalog,
    render_filter_value,
    schema_filter_fields,
)
from erp_pipeline.ingestion.errors import (
    EncryptedPDFError,
    ImageDecodeError,
    IngestionError,
    MalformedCSVError,
    MalformedPDFError,
    UnsupportedFileTypeError,
)
from erp_pipeline.orchestration import (
    InvalidPipelineRequestError,
    JobRequest,
    JobStatus,
    JobType,
    MappingNotFoundError,
    OrchestrationService,
    RecordNotFoundError,
    RepresentationNotFoundError,
    SchemaNotFoundError,
    UnsupportedUploadError,
)
from erp_pipeline.orchestration.document_identity import DocumentIdentity

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


#: Ingestion failures that are the CLIENT'S file being wrong, not the server
#: being broken. Each maps onto the orchestration error whose HTTP status
#: already means the right thing, so no new status code is invented and
#: `error_body` keeps producing a typed `code` rather than INTERNAL_ERROR.
#:
#:     content is unreadable      -> InvalidPipelineRequestError -> 422
#:     the file is not that type  -> UnsupportedUploadError      -> 415
#:
#: A corrupt or password-protected PDF is an accepted TYPE carrying
#: unprocessable CONTENT, which is exactly what 422 means. A file whose bytes
#: contradict its extension is a TYPE problem, which is 415 - the status this
#: API already returns for a rejected suffix.
_CLIENT_INGESTION_ERRORS: tuple[type[IngestionError], ...] = (
    MalformedPDFError,
    EncryptedPDFError,
    ImageDecodeError,
    MalformedCSVError,
)


def _ingest_upload_or_refuse(service: OrchestrationService, upload_id: str) -> Any:
    """Ingest an uploaded file, converting client-file failures into 4xx.

    Without this, a corrupt or encrypted PDF escaped as a bare `IngestionError`,
    hit the catch-all handler and was reported as **500 INTERNAL_ERROR** - which
    tells a user their file is fine and our server is broken, when the opposite
    is true. It also buried a real fault: if everything is a 500, a genuine
    server fault is indistinguishable from a bad upload.

    Only the enumerated client-file failures are converted. Anything else -
    including a programming error inside an extractor - still propagates and
    still becomes a 500, because hiding those would be the opposite mistake.
    """
    try:
        return service.services.ingest_upload(upload_id)
    except UnsupportedFileTypeError as error:
        # Includes FileTypeMismatchError: the bytes disagree with the extension.
        raise UnsupportedUploadError(str(error)) from error
    except _CLIENT_INGESTION_ERRORS as error:
        raise InvalidPipelineRequestError(
            str(error), reason=type(error).__name__
        ) from error


def _selected_target(decision: Any) -> str | None:
    """The canonical target a decision actually selected, or None.

    Reads `decision.selected.qualified_target` - the engine's own record of its
    choice. Returns None only when nothing was selected, which is the honest
    answer for an AMBIGUOUS or UNMAPPED field.
    """
    selected = getattr(decision, "selected", None)

    if selected is None:
        return None

    target = getattr(selected, "qualified_target", None)

    return str(target) if target else None


def _confidence_label(decision: Any) -> str | None:
    """The confidence level as a plain value, or None when there is no choice.

    `str(None)` produced the literal string "None" in the JSON body, which a
    client cannot distinguish from a real level. An absent confidence is null.
    """
    confidence = getattr(decision, "confidence", None)

    if confidence is None:
        return None

    return getattr(confidence, "value", None) or str(confidence)


#: The per-page marker the PDF and image extractors set when OCR produced the
#: text. Declared once here so the API reads the extractor's own vocabulary
#: rather than inventing a second, independent notion of "OCR happened".
OCR_EXTRACTION_METHOD = "ocr"


def _ocr_was_used(document: Any) -> bool:
    """Whether OCR actually produced text on at least one page.

    ``ExtractedDocument`` has no ``ocr_used`` attribute - OCR state lives per
    page on ``ExtractedPage.extraction_method``, which the extractors set to
    ``"ocr"``, ``"text_layer"`` or ``"none"``. Reading a non-existent attribute
    made this flag permanently False, including for scans that were genuinely
    OCR'd.

    "Enabled" and "available" are deliberately NOT what this reports. A page
    whose status is ``OCR_UNAVAILABLE`` has ``extraction_method="none"`` and so
    is correctly excluded: OCR did not run, therefore it was not used.
    """
    return any(
        getattr(page, "extraction_method", None) == OCR_EXTRACTION_METHOD
        for page in (getattr(document, "pages", ()) or ())
    )


def _document_identity(document: Any, stored: Any) -> str | None:
    """The document's content-addressed identity.

    Uses the SAME rule ``ai.chunking.chunk_document`` already applies - the
    file's content hash - so the id returned by an upload is the id its chunks
    will carry once the document pipeline runs. Deriving it any other way would
    hand a caller an identifier that matches nothing downstream.

    Falls back to the stored upload's own hash when extraction produced no
    document object, which keeps the field populated for a file that arrived
    intact but could not be parsed.
    """
    file_source = getattr(document, "file", None)

    for candidate in (
        getattr(file_source, "content_hash", None),
        getattr(file_source, "file_id", None),
        getattr(stored, "content_hash", None),
    ):
        if candidate:
            return str(candidate)

    return None


def _sample_was_limited(service: OrchestrationService, result: Any) -> bool:
    """Whether schema inference stopped at its sample ceiling.

    When it did, the file holds AT LEAST ``rows_sampled`` rows and probably
    more, so a caller must not read the sample as a total. When it did not, the
    sample happened to cover the whole file - but that is still not a counted
    total, which is why ``rows_observed`` stays null either way.
    """
    sampled = int(getattr(result, "rows_sampled", 0) or 0)

    if not sampled:
        return False

    options = getattr(getattr(service.services, "ingestion", None), "options", None)
    limit = getattr(getattr(options, "csv", None), "max_rows_for_schema_inference", None)

    if not isinstance(limit, int) or limit <= 0:
        from erp_pipeline.ingestion.models import CsvOptions

        limit = CsvOptions().max_rows_for_schema_inference

    return sampled >= limit


def _warning_messages(result: Any, limit: int = 20) -> list[str]:
    """Warnings as API text, never as a Python repr.

    An ``ExtractionWarning`` is a dataclass, so ``str(w)`` yields
    ``ExtractionWarning(category='ocr_unavailable', message='...', row_number=None, ...)``
    - an implementation detail, complete with attribute names and ``None``
    padding, rendered into a field a user reads. The dataclass already carries a
    stable ``category`` and a human ``message``; this joins exactly those two and
    nothing else, so the internal class name never reaches a client.

    Anything that is already a plain string is passed through unchanged, which
    keeps the several call sites that append their own prose working.
    """
    messages: list[str] = []

    for warning in (getattr(result, "warnings", ()) or ()):
        if isinstance(warning, str):
            messages.append(warning)
            continue

        category = getattr(warning, "category", None)
        message = getattr(warning, "message", None)

        if message:
            messages.append(f"{category}: {message}" if category else str(message))
        else:
            # Not a shape this function understands. Falling back to str() would
            # leak a repr, so the category alone is reported instead.
            messages.append(str(category) if category else "unspecified warning")

    return messages[:limit]


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
    result = _ingest_upload_or_refuse(service, stored.upload_id)
    schema = getattr(result, "schema", None)
    published = False
    warnings = _warning_messages(result)

    if schema is not None and service.services.catalog is not None:
        published, problem = _publish_file_schema(service.services, schema)

        if problem is not None:
            warnings.append(problem)

    schema_job_id = schema_status = None

    if schema is not None:
        service.services.schema_cache[schema.schema_id] = schema
        # The STRUCTURE becomes searchable immediately. The ROWS do not: they
        # still require a mapping decision, and using schema indexing as a
        # backdoor around that review is exactly what this must not do.
        schema_job_id, schema_status, problem = service.index_schema(
            schema.schema_id
        )

        if problem:
            warnings.append(problem)

    return CsvUploadResponse(
        upload_id=stored.upload_id,
        filename=stored.display_name,
        content_hash=stored.content_hash,
        size_bytes=stored.size_bytes,
        source_system_id=getattr(schema, "source_system_id", None),
        schema_id=getattr(schema, "schema_id", None),
        columns=sum(len(e.fields) for e in getattr(schema, "entities", ()) or ()),
        # SAMPLED and COUNTED are different measurements and are reported as
        # such. `data_row_count` is None unless something walked the whole file,
        # and it is passed through as None rather than coerced to 0 - a zero
        # would read as "this file has no rows", which is a different and false
        # statement.
        rows_sampled=int(getattr(result, "rows_sampled", 0) or 0),
        sample_limited=_sample_was_limited(service, result),
        rows_observed=getattr(result, "data_row_count", None),
        published=published,
        schema_index_job_id=schema_job_id,
        schema_indexing_status=schema_status,
        warnings=warnings[:21],
    )


def _start_document_indexing(
    service: OrchestrationService,
    upload_id: str,
    identity: DocumentIdentity,
) -> tuple[str | None, str | None, str | None]:
    """Start the SAME document pipeline the manual job route starts.

    Calls the orchestration service directly rather than issuing an HTTP
    request to this application's own ``POST /v1/jobs``: a service that calls
    itself over the network adds a socket, a serialization round trip and a
    second failure mode to a call that is one function away.

    A scheduling failure does not fail the upload. The bytes are stored, the
    extraction succeeded, and both are still worth returning - the caller is
    told plainly that indexing did not start, and the manual job route remains
    available to start it.
    """
    try:
        job = service.submit(
            JobRequest(
                job_type=JobType.DOCUMENT_PIPELINE,
                upload_id=upload_id,
                options=identity.to_options(),
            )
        )
    except Exception as error:  # noqa: BLE001 - reported, never raised
        return (
            None,
            None,
            "the document was uploaded and extracted, but automatic indexing "
            f"could not be started ({type(error).__name__}). It can be started "
            "with POST /v1/jobs using job_type=document_pipeline and this "
            "upload_id.",
        )

    # ``submit`` returns the job as it was CREATED. Under an inline executor
    # the work has already finished by the time it returns, so reporting the
    # stale object would tell a caller "pending" about a job that succeeded -
    # or, worse, about one that failed. Re-reading costs one lookup and makes
    # the snapshot true for both executors.
    try:
        current = service.get(job.job_id)
    except Exception:  # noqa: BLE001 - the job exists; the snapshot is a bonus
        current = job

    return job.job_id, (current or job).status.value, None


@files_router.post(
    "/documents",
    response_model=DocumentUploadResponse,
    status_code=201,
    operation_id="uploadDocument",
)
def upload_document(
    file: UploadFile = File(...),
    source_system_id: str | None = Form(default=None),
    source_entity: str | None = Form(default=None),
    parent_record_id: str | None = Form(default=None),
    business_key_name: str | None = Form(default=None),
    business_key_value: str | None = Form(default=None),
    document_type: str | None = Form(default=None),
    sensitivity: str | None = Form(default=None),
    service: OrchestrationService = Depends(get_service),
) -> DocumentUploadResponse:
    """Upload a PDF or image, extract it, and start indexing it.

    Every identity field is OPTIONAL, so a client that posts only a file - which
    is what the existing frontend does - behaves exactly as it did before, plus
    the indexing that used to require a second call.

    Identity is DECLARED, never inferred. A file named
    ``EMP002_birth_certificate.jpg`` uploaded without metadata carries no
    business key: see ``orchestration.document_identity`` for why a guess in
    that field would be worse than an absence.

    Returns metadata only - never the extracted text, which is the document's
    entire content.
    """
    # Refused BEFORE the file is stored: a half-declared business key is a bad
    # request, and accepting the upload first would leave an orphan file behind
    # for a request that was never going to be honoured.
    identity = DocumentIdentity.declare(
        source_system_id=source_system_id,
        source_entity=source_entity,
        parent_record_id=parent_record_id,
        business_key_name=business_key_name,
        business_key_value=business_key_value,
        document_type=document_type,
        sensitivity=sensitivity,
    )

    stored = _store_upload(service, file, DOCUMENT_SUFFIXES)
    result = _ingest_upload_or_refuse(service, stored.upload_id)
    document = getattr(result, "document", None)
    warnings = _warning_messages(result)
    job_id, status, indexing_error = _start_document_indexing(
        service, stored.upload_id, identity
    )

    if indexing_error:
        warnings.append(indexing_error)

    return DocumentUploadResponse(
        upload_id=stored.upload_id,
        filename=stored.display_name,
        content_hash=stored.content_hash,
        size_bytes=stored.size_bytes,
        document_id=_document_identity(document, stored),
        file_type=str(getattr(getattr(result, "file", None), "file_type", "") or ""),
        page_count=len(getattr(document, "pages", ()) or ()),
        extraction_status=str(getattr(result, "status", "") or ""),
        ocr_used=_ocr_was_used(document),
        index_job_id=job_id,
        indexing_status=status,
        indexing_error=indexing_error,
        warnings=warnings,
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

    return schema_response(schema)


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
                # The engine records its choice on `selected`, a MappingCandidate
                # whose `qualified_target` is "entity.field". `FieldDecision` has
                # no `target_path` attribute, so the old lookup returned None for
                # every field - including AUTO_SELECTED ones with high
                # confidence, which made the response unable to say what a field
                # had actually been mapped to. Genuinely unselected decisions
                # (ambiguous, unmapped) still report null, which is correct:
                # nothing was chosen.
                "target_path": _selected_target(d),
                "confidence": _confidence_label(d),
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

#: Provenance returned with every hit, in the order a reader would want it:
#: what this is, where it came from, which ERP record it belongs to, and where
#: in the document it sits.
#:
#: Every one of these is an identity or a structural fact. None of them is
#: content: the extracted text of a certificate is NOT here, and resolving it
#: is Phase 5's job.
_HIT_METADATA_FIELDS: tuple[str, ...] = (
    "content_hash",
    "model_id",
    "content_kind",
    "source_system_id",
    "source_entity",
    "record_key",
    "source_field",
    "parent_record_id",
    "business_key_name",
    "business_key_value",
    "document_id",
    "document_type",
    "page_start",
    "page_end",
    "chunk_index",
    "schema_name",
    "entity_kind",
    "schema_id",
    "schema_version",
    "entity_id",
    "schema_chunk_index",
)


def _hit_metadata(metadata: Any) -> dict[str, Any]:
    """Flatten one storage-state row into a hit's provenance.

    Keys are always present, even when the value is ``None``. A structured
    record genuinely has no ``page_start``, and saying so explicitly is more
    useful to a caller than an absent key they have to guess the meaning of -
    the opposite of the vector payload, where absence is what makes a Qdrant
    match behave correctly.
    """
    payload: dict[str, Any] = {
        name: getattr(metadata, name, None) for name in _HIT_METADATA_FIELDS
    }
    payload["sensitivity"] = _enum_or_none(getattr(metadata, "sensitivity", None))
    if payload.get("record_key") is None:
        payload["record_key"] = payload.get("business_key_value")

    return payload


def _enum_or_none(value: object) -> str | None:
    """Render a stored enum by its wire value for a response payload."""
    if value is None:
        return None

    return str(getattr(value, "value", value))


search_router = APIRouter(prefix="/v1", tags=["search"])

_SEARCH_CONTROL_PARAMETERS = frozenset(
    {"q", "limit", "include_cold", "employee_id"}
)

#: Parameters that shape HOW retrieval runs but never name WHAT to retrieve.
#: Their presence alone must never switch ``GET /v1/search`` into search
#: mode - a bare ``?limit=5`` is still a request for the metadata catalog.
#: Everything else (``q``, any identity field, any dynamic filter - bare or
#: bracketed) does.
_MODE_NEUTRAL_PARAMETERS = frozenset({"limit", "include_cold"})


def _indexed_search_fields(
    service: OrchestrationService,
    *,
    refresh: bool = False,
) -> set[str] | None:
    """Fields indexed in every configured online Qdrant tier, when inspectable."""
    cached = getattr(service.services, "_search_index_field_cache", None)
    now = time.monotonic()
    if not refresh and cached and now - cached[0] < 30:
        return set(cached[1]) if cached[1] is not None else None

    storage = getattr(service.services, "storage", None)
    tiers = getattr(storage, "tiers", None)
    observed: list[set[str]] = []

    for tier in (
        getattr(tiers, "hot", None),
        getattr(tiers, "warm", None),
    ):
        client = getattr(tier, "client", None)
        collection = getattr(tier, "collection_name", None)
        if client is None or collection is None:
            continue
        try:
            info = client.get_collection(collection)
        except Exception:  # noqa: BLE001 - schema discovery stays available
            return None
        observed.append(set(getattr(info, "payload_schema", None) or {}))

    result = set.intersection(*observed) if observed else None
    service.services._search_index_field_cache = (now, result)
    return result


def _dynamic_filter_catalog(
    service: OrchestrationService,
    *,
    source_system_id: str | None = None,
    source_entity: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Current source-schema fields; no employee business key is hardcoded.

    Reads ``_known_schemas`` - in-memory cache plus the persisted catalog -
    so a dynamic field discovered before a restart is still an ACCEPTED
    filter afterward, not just one the metadata response still lists.
    """
    return schema_filter_fields(
        _known_schemas(service),
        source_system_id=source_system_id,
        source_entity=source_entity,
    )


def _allowed_dynamic_fields(
    service: OrchestrationService,
    *,
    source_system_id: str | None = None,
    source_entity: str | None = None,
) -> tuple[str, ...]:
    names = set(
        _dynamic_filter_catalog(
            service,
            source_system_id=source_system_id,
            source_entity=source_entity,
        )
    )
    indexed = _indexed_search_fields(service)
    if indexed is not None:
        names.update(indexed - RESERVED_PAYLOAD_FIELDS)
    return tuple(sorted(names))


_FILTER_BRACKET_RE = re.compile(r"^filters\[(?P<name>[^\[\]]+)\]$")


def _search_capable(service: OrchestrationService) -> bool:
    services = service.services

    return services.embedding is not None and services.storage is not None


def _identity_filter_fields(
    indexed: set[str] | None,
) -> list[IdentityFilterFieldResponse]:
    """The closed, entity-independent identity/provenance filters.

    Excludes a core field that Qdrant confirms is not currently indexed,
    exactly as the metadata surface this replaces did - a filter that cannot
    be served is not advertised as available.
    """
    return [
        IdentityFilterFieldResponse(
            name=name,
            description=f"Exact match on the {name} Qdrant payload field.",
        )
        for name in FILTERABLE_FIELDS
        if indexed is None or name in indexed
    ]


def _persisted_catalog_schemas(
    service: OrchestrationService,
    *,
    refresh: bool = False,
) -> tuple[Any, ...]:
    """Every schema the PERSISTED catalog currently knows about.

    ``schema_cache`` is a plain in-process dict - empty on a fresh worker, so
    ``GET /v1/search`` would report no searchable entities right after an
    App Service restart even though the vectors and the discovered structure
    both still exist. The catalog is Postgres-backed and does not have that
    problem, so it is read here as the authoritative source; this REBUILDS
    the same view from state that already exists, rather than storing a
    second copy of it anywhere.

    Cached for 30 seconds, matching ``_indexed_search_fields``: a metadata
    call is allowed to be briefly stale, not to cost a handful of catalog
    round trips on every request.
    """
    cached = getattr(service.services, "_catalog_schema_cache", None)
    now = time.monotonic()
    if not refresh and cached and now - cached[0] < 30:
        return cached[1]

    catalog = getattr(service.services, "catalog", None)
    if catalog is None:
        return ()

    schemas: list[Any] = []
    try:
        for source_system in catalog.repository.list_source_systems():
            records = catalog.repository.list_schema_snapshots(
                source_system.source_system_id
            )
            latest: dict[str, Any] = {}
            for record in records:
                current = latest.get(record.schema_name)
                if current is None or record.catalog_version > current.catalog_version:
                    latest[record.schema_name] = record
            for record in latest.values():
                schemas.append(catalog.repository.get_schema_snapshot(record.schema_id))
    except Exception:  # noqa: BLE001 - metadata discovery must stay available
        return ()

    result = tuple(schemas)
    service.services._catalog_schema_cache = (now, result)
    return result


def _known_schemas(service: OrchestrationService) -> tuple[Any, ...]:
    """Every schema this process can currently describe, from any source.

    The union of the in-process cache (immediate, for a schema just
    discovered or uploaded this request) and the persisted catalog
    (authoritative across restarts). ``available_search_catalog`` merges
    fields by ``(source_system_id, source_entity)`` regardless of how many
    ``SourceSchema`` objects mention the same entity, so overlap between the
    two here is harmless - never a duplicated entity in the response.
    """
    in_memory = tuple(service.services.schema_cache.values())
    persisted = _persisted_catalog_schemas(service)

    seen_ids = {getattr(schema, "schema_id", None) for schema in in_memory}
    return in_memory + tuple(
        schema for schema in persisted if schema.schema_id not in seen_ids
    )


def _search_metadata_response(
    service: OrchestrationService,
    *,
    source_system_id: str | None,
    source_entity: str | None,
) -> SearchMetadataResponse:
    """What ``GET /v1/search`` returns when called with no ``q``.

    Built from the SAME facts the search path itself validates against - the
    discovered schema catalog (in-memory AND persisted, so this survives a
    restart) and the live Qdrant payload index - so this can never advertise
    a filter the search call would then refuse.
    """
    indexed = _indexed_search_fields(service, refresh=True)
    catalog = available_search_catalog(
        _known_schemas(service),
        source_system_id=source_system_id,
        source_entity=source_entity,
        indexed_fields=indexed,
        search_capable=_search_capable(service),
    )

    return SearchMetadataResponse(
        available_search=[
            AvailableSearchEntityResponse(
                source_system_id=item["source_system_id"],
                source_entity=item["source_entity"],
                entity_kind=item["entity_kind"],
                description=item["description"],
                searchable=item["searchable"],
                fields=[
                    AvailableSearchFieldResponse(**field)
                    for field in item["fields"]
                ],
            )
            for item in catalog
        ],
        identity_filters=_identity_filter_fields(indexed),
        qdrant_indexes_verified=indexed is not None,
    )


@search_router.get(
    "/search",
    response_model=SearchMetadataResponse | SearchResponse,
    operation_id="searchGet",
)
def search_get(
    request: Request,
    q: str | None = Query(default=None, min_length=1, max_length=2000),
    source_system_id: str | None = Query(default=None, min_length=1),
    source_entity: str | None = Query(default=None, min_length=1),
    employee_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=10, ge=1, le=100),
    include_cold: bool = Query(default=False),
    service: OrchestrationService = Depends(get_service),
) -> SearchMetadataResponse | SearchResponse:
    """The one search endpoint: metadata when called bare, search otherwise.

    Mode is decided by what was actually supplied, not by ``q`` alone:

    * No parameters (or only ``limit``/``include_cold``, which shape
      retrieval but do not name anything to retrieve) -> the live,
      schema-driven metadata catalog. This is the ONLY metadata surface
      this API has; there is deliberately no separate options or schema
      endpoint left to drift out of sync with it.
    * Any identity or dynamic filter - ``source_system_id``,
      ``source_entity``, ``record_key``, ``employee_id``, or a schema field,
      bare or bracketed - switches to search mode, WITH or WITHOUT ``q``:

        - with ``q``: semantic ranking inside the filtered scope.
        - without ``q``: exact identity/metadata retrieval, no ranking.
          Supplying the full canonical identity - ``source_system_id`` +
          ``source_entity`` + ``record_key``, whatever those are in YOUR
          catalog - therefore returns that one record directly, with no
          query text required. Call this endpoint bare to discover the
          systems, entities and fields this deployment actually holds.

    Either way, filters are pushed into Qdrant as a server-side constraint
    BEFORE anything else runs - a ``Filter`` for semantic ranking
    (``HybridVectorStore.search``), a ``scroll`` for exact retrieval
    (``HybridVectorStore.fetch``) - so an exact-scoped call narrows the
    candidate set itself. Neither path scans the whole collection.
    """
    meaningful_params = [
        name for name in request.query_params if name not in _MODE_NEUTRAL_PARAMETERS
    ]

    if not meaningful_params:
        return _search_metadata_response(
            service,
            source_system_id=source_system_id,
            source_entity=source_entity,
        )

    filters: dict[str, str] = {}

    # q/limit/include_cold/employee_id control retrieval. Every other
    # parameter - bare (``department=Finance``) or bracketed
    # (``filters[department]=Finance``) - is a payload criterion validated
    # from the current schema; no employee field such as department, role or
    # a future custom attribute is hardcoded. Both forms are accepted for the
    # SAME field so an existing bare-parameter caller keeps working exactly
    # as before, while a caller that prefers the unambiguous bracket form -
    # the safest GET representation for a dynamic, open-ended field name -
    # gets identical behaviour.
    for name in request.query_params:
        if name in _SEARCH_CONTROL_PARAMETERS:
            continue

        bracket = _FILTER_BRACKET_RE.match(name)
        field_name = bracket.group("name") if bracket else name
        values = request.query_params.getlist(name)

        if len(values) != 1:
            raise InvalidPipelineRequestError(
                f"search filter {field_name!r} must be supplied exactly once"
            )

        if field_name in filters and filters[field_name] != values[0]:
            raise InvalidPipelineRequestError(
                f"conflicting values supplied for filter {field_name!r}"
            )

        filters[field_name] = values[0]

    record_key = filters.get("record_key")
    if employee_id and record_key and employee_id != record_key:
        raise InvalidPipelineRequestError(
            "employee_id and record_key identify the same business key and "
            "must match when both are supplied"
        )

    exact_key = employee_id or record_key
    if employee_id:
        filters["record_key"] = employee_id
    source_system_id = filters.get("source_system_id")

    # EMP002 is not globally unique. Refusing an unscoped exact lookup is safer
    # than returning a plausible EMP002 from the wrong ERP system.
    if exact_key and not source_system_id:
        raise InvalidPipelineRequestError(
            "source_system_id is required when employee_id or record_key is supplied"
        )

    allowed_fields = _allowed_dynamic_fields(
        service,
        source_system_id=source_system_id,
        source_entity=filters.get("source_entity"),
    )

    if q is None:
        return _execute_filter_only_search(
            filters, service, allowed_fields=allowed_fields, limit=limit
        )

    return _execute_search(
        SearchRequest(
            query=q,
            top_k=limit,
            include_cold=include_cold,
            filters=filters,
        ),
        service,
        allowed_fields=allowed_fields,
    )


@search_router.post(
    "/search",
    response_model=SearchResponse,
    operation_id="search",
    deprecated=True,
)
def search_post(
    payload: SearchRequest, service: OrchestrationService = Depends(get_service)
) -> SearchResponse:
    """Compatibility route for existing clients; prefer ``GET /v1/search``."""
    return _execute_search(payload, service)


def _tokenize_dynamic_filters(
    filters_raw: Mapping[str, Any],
    service: OrchestrationService,
    *,
    dynamic_field_names: tuple[str, ...],
) -> dict[str, Any]:
    """Replace each dynamic (catalog-driven) filter's value with its token.

    Closed identity/provenance fields - ``record_key``, ``source_system_id``,
    ``sensitivity`` and the rest of ``FILTERABLE_FIELDS`` - pass through
    completely unchanged. Only fields the discovered schema declared
    (``department_name``, ``shift_code``, ...) are ERP business content, and
    only those get tokenized - using the exact function ingestion used to
    write their token into the Qdrant payload
    (``EmbeddingService.tokenize_filter_value``), so the same value always
    produces the same token on both sides.

    A dynamic field's token is scoped by BOTH source system and entity - the
    same field name can exist in two different entities of the same system
    with unrelated meanings - so using one now REQUIRES both
    ``source_system_id`` and ``source_entity`` in the same request; there is
    no token to compute otherwise. This is stricter than before
    tokenization, when an unscoped dynamic filter matched across every
    source (and entity) that happened to declare the field; it is also the
    only way the token can mean anything at all.
    """
    dynamic_in_use = sorted(
        name for name in filters_raw if name in dynamic_field_names
    )

    if not dynamic_in_use:
        return dict(filters_raw)

    services = service.services
    source_system_id = filters_raw.get("source_system_id")
    source_entity = filters_raw.get("source_entity")

    if not source_system_id or not source_entity:
        raise InvalidPipelineRequestError(
            "source_system_id and source_entity are both required to filter "
            f"on a dynamic schema field ({', '.join(dynamic_in_use)})"
        )

    if services.embedding is None:
        raise InvalidPipelineRequestError(
            "dynamic schema filters require the embedding/token service to "
            "be configured"
        )

    tokenized = dict(filters_raw)

    for field_name in dynamic_in_use:
        token = services.embedding.tokenize_filter_value(
            source_system_id=str(source_system_id),
            source_entity=str(source_entity),
            field_name=field_name,
            value=filters_raw[field_name],
        )

        if token is None:
            raise InvalidPipelineRequestError(
                f"dynamic filter {field_name!r} is unavailable: no "
                "filter-token secret is configured for this deployment"
            )

        tokenized[field_name] = token

    return tokenized


def _display_filters(filters_raw: Mapping[str, Any]) -> dict[str, str]:
    """Filters as a caller would recognize them - never a generated token.

    Built from the ORIGINAL, pre-tokenization values so ``filters_applied``
    always shows "Finance", never a hex digest - a caller must be able to
    confirm this is the query they think they ran (see ``SearchResponse``),
    and a token cannot serve that purpose.
    """
    return {
        str(key): render_filter_value(value)
        for key, value in sorted(filters_raw.items())
    }


def _execute_search(
    payload: SearchRequest,
    service: OrchestrationService,
    *,
    allowed_fields: tuple[str, ...] | None = None,
) -> SearchResponse:
    """Embed the query with Phase 11, then retrieve inside validated filters.

    No LLM and no generated answer: this returns retrieved records, not prose
    about them. The response carries no vector - a search endpoint that
    returned embeddings would be an embedding-export endpoint.
    """
    services = service.services

    if services.embedding is None or services.storage is None:
        raise InvalidPipelineRequestError(
            "search needs both an embedding service and a vector store"
        )

    # The compatibility POST route must obey the same identity boundary as the
    # GET route. A business record key is only meaningful inside its source
    # system; accepting it alone could return the same key from another ERP.
    if payload.filters.get("record_key") and not payload.filters.get(
        "source_system_id"
    ):
        raise InvalidPipelineRequestError(
            "source_system_id is required when record_key is supplied"
        )

    if allowed_fields is None:
        allowed_fields = _allowed_dynamic_fields(
            service,
            source_system_id=payload.filters.get("source_system_id"),
            source_entity=payload.filters.get("source_entity"),
        )

    filters_raw = dict(payload.filters)

    # Refuse an unsupported filter rather than ignoring it. A silently dropped
    # filter returns a plausible-looking unfiltered result, which is the worst
    # possible answer for a caller about to act on these hits.
    try:
        tokenized_raw = _tokenize_dynamic_filters(
            filters_raw, service, dynamic_field_names=allowed_fields
        )
        filters = SearchFilters.from_mapping(
            tokenized_raw,
            allowed_fields=allowed_fields,
        )
    except (UnknownFilterFieldError, InvalidFilterValueError) as error:
        raise InvalidPipelineRequestError(
            str(error),
            supported_filters=list(
                getattr(error, "supported", FILTERABLE_FIELDS)
            ),
        ) from error

    started = time.perf_counter()
    vector = services.embedding.model.encode([payload.query])[0]

    result = services.storage.search(
        vector,
        limit=payload.top_k,
        include_cold=payload.include_cold,
        filters=filters,
    )

    tiers = [tier.value for tier in result.tiers_searched]
    deep = "cold" in tiers

    return SearchResponse(
        query_model=services.embedding.model_id,
        dimension=services.embedding.dimension,
        hits=_build_hit_responses(result, services),
        tiers_searched=tiers,
        include_cold=payload.include_cold,
        # From the ORIGINAL values, never from ``filters.to_dict()``: a
        # dynamic field's criterion is a token by this point, and a token
        # must never be echoed back through the API (see
        # _tokenize_dynamic_filters).
        filters_applied=_display_filters(filters_raw),
        deep_search_used=deep,
        deep_search_note=(
            "archived vectors were rehydrated into a temporary index to answer "
            "this query; this costs materially more than a hot or warm search"
            if deep
            else None
        ),
        took_ms=round((time.perf_counter() - started) * 1000, 3),
    )


def _build_hit_responses(result: Any, services: Any) -> list[SearchHitResponse]:
    """Shape a storage-layer ``SearchResult`` into wire hits.

    Shared by semantic search (``_execute_search``) and exact filter-only
    retrieval (``_execute_filter_only_search``): both return the same
    ``SearchHit`` shape from the storage layer, so both are rendered the
    same way. Only how ``hit.score`` was produced differs between them.
    """
    hits = []

    for hit in result.hits:
        # The store already batch-loaded this row while merging tiers and
        # re-checking filters, so it is read from the hit rather than fetched
        # again. The fallback is for a hit that arrived without one.
        metadata = hit.state or services.storage.state.load(hit.representation_id)

        # Carried forward from storage state, never reconstructed: a
        # representation id is normalized, so the canonical id cannot be
        # recovered from it by parsing.
        canonical_record_id = hit.canonical_record_id or getattr(
            metadata, "canonical_record_id", None
        )

        hits.append(
            SearchHitResponse(
                representation_id=hit.representation_id,
                canonical_record_id=canonical_record_id,
                # Mirrors canonical_record_id. Kept so an existing consumer of
                # `record_id` is not broken, and now actually resolvable.
                record_id=canonical_record_id,
                source_system_id=getattr(metadata, "source_system_id", None),
                source_entity=getattr(metadata, "source_entity", None),
                record_key=(
                    getattr(metadata, "record_key", None)
                    or getattr(metadata, "business_key_value", None)
                ),
                content_kind=getattr(metadata, "content_kind", None),
                entity_type=hit.entity_type
                or getattr(metadata, "entity_type", None),
                score=round(float(hit.score), 6),
                tier=hit.tier.value,
                metadata=_hit_metadata(metadata),
            )
        )

    return hits


def _execute_filter_only_search(
    filters_raw: dict[str, str],
    service: OrchestrationService,
    *,
    allowed_fields: tuple[str, ...],
    limit: int,
) -> SearchResponse:
    """Exact identity/metadata retrieval with no query text.

    No embedding is computed and no ANN ranking runs: the filter alone
    determines the result, through ``HybridVectorStore.fetch`` - the same
    ``SearchFilters.to_qdrant_filter()`` the semantic path pushes into
    Qdrant, applied via ``scroll`` rather than a vector query. Every hit
    carries ``score=1.0`` because none was computed; this is an identity
    lookup, not a ranking.
    """
    services = service.services

    if services.storage is None:
        raise InvalidPipelineRequestError("search needs a vector store")

    try:
        tokenized_raw = _tokenize_dynamic_filters(
            filters_raw, service, dynamic_field_names=allowed_fields
        )
        filters = SearchFilters.from_mapping(tokenized_raw, allowed_fields=allowed_fields)
    except (UnknownFilterFieldError, InvalidFilterValueError) as error:
        raise InvalidPipelineRequestError(
            str(error),
            supported_filters=list(getattr(error, "supported", FILTERABLE_FIELDS)),
        ) from error

    started = time.perf_counter()
    result = services.storage.fetch(filters, limit=limit)
    tiers = [tier.value for tier in result.tiers_searched]

    return SearchResponse(
        query_model=getattr(services.embedding, "model_id", None) or "none",
        dimension=getattr(services.embedding, "dimension", 0) or 0,
        hits=_build_hit_responses(result, services),
        tiers_searched=tiers,
        include_cold=False,
        # From the ORIGINAL values - never the token. See _execute_search.
        filters_applied=_display_filters(filters_raw),
        deep_search_used=False,
        deep_search_note=None,
        took_ms=round((time.perf_counter() - started) * 1000, 3),
    )


# ----------------------------------------------------------------------
# Representations (Phase 5)
# ----------------------------------------------------------------------

representations_router = APIRouter(
    prefix="/v1/representations", tags=["representations"]
)


@representations_router.get(
    "/{representation_id:path}",
    response_model=RepresentationResponse,
    operation_id="getRepresentation",
)
def get_representation(
    representation_id: str, service: OrchestrationService = Depends(get_service)
) -> RepresentationResponse:
    """Resolve a search hit into the AI text it stands for.

    The ``:path`` converter is required, not decorative: representation ids
    contain colons and dots (``ai:document:erp_legacy_hr_...``), and the
    default converter stops at the first slash.

    Looks up by ``representation_id`` rather than ``document_id``, and that
    choice is load-bearing. Two employees issued the same certificate share a
    ``document_id``; resolving by it would collapse their association and hand
    back a row that belongs to whichever was written last.
    """
    store = getattr(service.services, "representations", None)

    if store is None:
        raise InvalidPipelineRequestError(
            "this deployment has no representation store configured, so AI "
            "text cannot be resolved"
        )

    representation = store.get(representation_id)

    if representation is None:
        raise RepresentationNotFoundError(
            f"representation {representation_id!r} was not found",
            representation_id=representation_id,
        )

    metadata = dict(getattr(representation, "metadata", None) or {})

    def provenance(name: str) -> Any:
        return metadata.get(name)

    return RepresentationResponse(
        representation_id=representation.representation_id,
        entity_type=representation.entity_type,
        content_kind=provenance("content_kind"),
        # The one endpoint that returns text. Already bounded upstream by the
        # chunker and RepresentationConfig, and returned unmodified so it is
        # byte-for-byte what was embedded.
        text=representation.text_for_ai,
        content_hash=representation.resolved_hash(),
        canonical_record_id=provenance("canonical_record_id"),
        parent_record_id=provenance("parent_record_id"),
        source_system_id=provenance("source_system_id"),
        source_entity=provenance("source_entity"),
        record_key=provenance("record_key") or provenance("business_key_value"),
        source_field=provenance("source_field"),
        business_key_name=provenance("business_key_name"),
        business_key_value=provenance("business_key_value"),
        document_id=provenance("document_id"),
        document_type=provenance("document_type"),
        page_start=provenance("page_start"),
        page_end=provenance("page_end"),
        chunk_index=provenance("chunk_index"),
        schema_name=provenance("schema_name"),
        entity_kind=provenance("entity_kind"),
        schema_id=provenance("schema_id"),
        schema_version=provenance("schema_version"),
        entity_id=provenance("entity_id"),
        schema_chunk_index=provenance("schema_chunk_index"),
        sensitivity=_enum_or_none(provenance("sensitivity")),
        source_record_ids=list(representation.source_record_ids or ()),
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
    "representations_router",
    "specs_router",
    "schemas_router",
    "mappings_router",
    "jobs_router",
    "search_router",
    "records_router",
]
