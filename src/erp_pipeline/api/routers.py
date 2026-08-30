"""All /v1 routers.

Each handler does four things and no more: validate, call the orchestration
service, shape the response, return. Any pipeline logic appearing here would be
a duplicate of a phase service.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from erp_pipeline.api.config import API_VERSION
from erp_pipeline.api.schemas import (
    ApiSpecUploadResponse,
    CapabilitiesResponse,
    CapabilityStatus,
    ConnectionTestResponse,
    CsvUploadResponse,
    DependencyHealth,
    DiscoveryResponse,
    DocumentUploadResponse,
    HealthResponse,
    JobAcceptedResponse,
    JobCreateRequest,
    JobResponse,
    MappingResponse,
    MappingSuggestRequest,
    MappingUpdateRequest,
    MappingValidationResponse,
    ReadinessResponse,
    RecordResponse,
    SchemaResponse,
    SearchRequest,
    SearchResponse,
    SourceCreate,
    SourceResponse,
)
from erp_pipeline.orchestration import (
    JobRequest,
    JobStatus,
    JobType,
    OrchestrationService,
    RecordNotFoundError,
    RegisteredSource,
    SchemaNotFoundError,
    UnsupportedUploadError,
    normalize_source_id,
)
from erp_pipeline.orchestration.models import ORCHESTRATION_ENGINE_VERSION
from erp_pipeline.schemas.enums import ContentKind, SourceType


def get_service(request: Request) -> OrchestrationService:
    """The single seam through which routes reach orchestration."""
    return request.app.state.orchestration


def get_settings(request: Request) -> Any:
    return request.app.state.settings


# ----------------------------------------------------------------------
# Health and capabilities
# ----------------------------------------------------------------------

health_router = APIRouter(prefix="/v1/health", tags=["health"])


@health_router.get("/live", response_model=HealthResponse, operation_id="healthLive")
def health_live() -> HealthResponse:
    """Is the process alive?

    Deliberately checks nothing external. If liveness depended on Qdrant, an
    outage in a vector database would get the API process killed and restarted
    - which fixes nothing and loses in-flight jobs.
    """
    return HealthResponse(status="alive", api_version=API_VERSION)


@health_router.get(
    "/ready", response_model=ReadinessResponse, operation_id="healthReady"
)
def health_ready(
    request: Request,
    service: OrchestrationService = Depends(get_service),
) -> ReadinessResponse:
    """Are the CONFIGURED dependencies usable?

    Only configured dependencies count. A deployment with no vector store is
    not unready - it simply cannot serve search, and says so.
    """
    services = service.services
    checks: list[DependencyHealth] = []

    # PostgreSQL: a real round trip, but only SELECT 1 - readiness must be
    # cheap enough to poll every few seconds.
    engine = getattr(request.app.state, "engine", None) if request else None

    if engine is not None:
        from erp_pipeline.runtime.database import check_connection

        ok, detail = check_connection(engine)
        checks.append(
            DependencyHealth(
                name="postgresql",
                configured=True,
                ready=ok,
                detail=None if ok else f"unreachable ({detail})",
            )
        )

    store_name = type(service.jobs).__name__
    checks.append(
        DependencyHealth(
            name="job_store",
            configured=True,
            ready=True,
            # Surfaced so an operator can see at a glance whether this
            # instance is durable or running on in-memory stores.
            detail=f"{store_name}"
            + ("" if store_name.startswith("Postgres") else " (NOT DURABLE)"),
        )
    )

    # Deliberately does NOT load the model: readiness must never trigger a
    # download or a multi-second import.
    embedding = services.embedding
    checks.append(
        DependencyHealth(
            name="embedding_model",
            configured=embedding is not None,
            ready=embedding is not None,
            detail=(
                None
                if embedding is None
                else (
                    "loaded"
                    if getattr(embedding, "loaded", True)
                    else "configured; loads on first use"
                )
            ),
        )
    )

    cold = getattr(
        getattr(request.app.state, "runtime_settings", None), "cold", None
    ) if request else None

    if cold is not None and cold.enabled:
        checks.append(
            DependencyHealth(
                name="cold_archive",
                configured=True,
                ready=cold.key_present,
                detail=None if cold.key_present else "encryption key is not set",
            )
        )

    storage = services.storage
    if storage is None:
        checks.append(
            DependencyHealth(
                name="vector_storage",
                configured=False,
                ready=True,
                detail="not configured; search is unavailable",
            )
        )
    else:
        try:
            health = storage.health()
            ready = any(tier.get("available") for tier in health.values())
            checks.append(
                DependencyHealth(
                    name="vector_storage",
                    configured=True,
                    ready=bool(ready),
                    detail=", ".join(
                        f"{name}={'up' if tier.get('available') else 'down'}"
                        for name, tier in health.items()
                    ),
                )
            )
        except Exception as error:  # noqa: BLE001 - readiness must not raise
            checks.append(
                DependencyHealth(
                    name="vector_storage",
                    configured=True,
                    ready=False,
                    detail=f"probe failed: {type(error).__name__}",
                )
            )

    ready = all(check.ready for check in checks)

    return ReadinessResponse(
        status="ready" if ready else "degraded", ready=ready, dependencies=checks
    )


def _probe(name: str, configured: bool, target: Any) -> DependencyHealth:
    return DependencyHealth(
        name=name,
        configured=configured,
        ready=configured,
        detail=None if configured else "not configured",
    )


capabilities_router = APIRouter(prefix="/v1", tags=["capabilities"])


def _integration_capabilities(services: Any) -> dict[str, CapabilityStatus]:
    """What Members 2 and 3 can rely on, measured from the wiring (Phase 11).

    Every ``enabled`` below is derived from a service actually being present,
    never from a constant. A capability whose dependencies are missing reports
    ``enabled=False`` with the reason, because an integration partner planning
    against this document needs to know the difference between "this build
    cannot" and "this deployment did not configure it".
    """
    search_ready = services.embedding is not None and services.storage is not None
    ingest_ready = services.ingestion is not None

    def status(enabled: bool, detail: str | None = None) -> CapabilityStatus:
        return CapabilityStatus(supported=True, enabled=enabled, detail=detail)

    missing_index = (
        None
        if search_ready
        else "no embedding model or vector store is configured in this deployment"
    )

    return {
        "csv_ingestion": status(
            ingest_ready,
            "infers and catalogs a schema; business rows are NOT indexed by "
            "this call and still require a mapping or source-native job",
        ),
        "document_ingestion": status(
            ingest_ready, "PDF and image upload with OCR fallback"
        ),
        "automatic_document_indexing": status(
            ingest_ready and search_ready,
            missing_index
            or "an uploaded document submits its own document_pipeline job",
        ),
        "schema_discovery": status(
            services.connection_factory is not None or services.catalog is not None,
            "requires a registered source with a reachable connection",
        ),
        "schema_vector_retrieval": status(
            search_ready, missing_index or "GET /v1/search?content_kind=schema"
        ),
        "structured_transformation": status(services.transformation is not None),
        "semantic_search": status(
            search_ready,
            missing_index
            or "returns identity and provenance; document text is resolved "
            "separately through GET /v1/representations/{id}",
        ),
        "representation_resolution": status(
            services.representations is not None,
            None
            if services.representations is not None
            else "no representation store is configured; search hits cannot be "
            "resolved back to their text",
        ),
        "response_adaptation": status(
            True,
            "POST /v1/responses/adapt needs no model, database or vector store. "
            "A collection response adapts its FIRST record only and warns.",
        ),
        "remote_asset_fetching": status(
            services.remote_asset_fetcher is not None
            and services.remote_asset_policy is not None,
            "ships disabled: no HTTP client is bundled, and a deployment must "
            "supply both a policy and a fetcher. Static declared assets only - "
            "never an ERP business API.",
        ),
        "scheduled_sync": status(
            services.sync is not None,
            "polling on a configured interval. Not CDC and not database "
            "replication; freshness is bounded by the poll interval.",
        ),
        "sensitivity_metadata": status(
            True,
            "classification is DECLARED by the caller and reported on search "
            "hits and resolved representations. This component makes no user "
            "authorization decision.",
        ),
    }


@capabilities_router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    operation_id="getCapabilities",
)
def capabilities(
    service: OrchestrationService = Depends(get_service),
    settings: Any = Depends(get_settings),
) -> CapabilitiesResponse:
    services = service.services
    limitations = [
        "This component parses API specifications but never calls the "
        "documented endpoints; runtime REST and SOAP ERP execution is out of "
        "scope.",
        "No LLM is used and no generated answers are produced; search returns "
        "retrieved records only.",
        "Cold-tier search requires archive rehydration and is off by default.",
    ]

    if not settings.sql_server_live_verified:
        limitations.append(
            "SQL Server support is implemented but live verification remains "
            "deferred."
        )

    return CapabilitiesResponse(
        api_version=API_VERSION,
        engine_version=ORCHESTRATION_ENGINE_VERSION,
        source_types=[source.value for source in SourceType],
        file_types=["csv", "pdf", "png", "jpg", "jpeg", "tiff"],
        api_spec_formats=["openapi_3", "swagger_2", "postman_collection"],
        job_types=[job.value for job in JobType],
        content_kinds=[kind.value for kind in ContentKind],
        storage_tiers=["hot", "warm", "cold"] if services.storage else [],
        embedding_model=getattr(services.embedding, "model_id", None),
        embedding_dimension=getattr(services.embedding, "dimension", None),
        incremental_sync_supported=services.sync is not None,
        integration_capabilities=_integration_capabilities(services),
        limitations=limitations,
    )


# ----------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------

sources_router = APIRouter(prefix="/v1/sources", tags=["sources"])


@sources_router.post(
    "", response_model=SourceResponse, status_code=201, operation_id="createSource"
)
def create_source(
    payload: SourceCreate, service: OrchestrationService = Depends(get_service)
) -> SourceResponse:
    """Register a source. The password, if supplied, never reaches storage."""
    source_id = normalize_source_id(payload.name)
    credential_ref = payload.credential_ref

    # A supplied password is moved into the secret provider immediately and
    # then dropped. The RegisteredSource has nowhere to put it even if we
    # wanted to, which is the design.
    if payload.password is not None:
        credential_ref = credential_ref or f"{source_id}_password"
        provider = service.services.secrets

        if hasattr(provider, "put"):
            provider.put(credential_ref, payload.password.get_secret_value())

    registered = service.sources.register(
        RegisteredSource(
            source_id=source_id,
            name=payload.name,
            source_type=payload.source_type,
            host=payload.host,
            port=payload.port,
            database=payload.database,
            username=payload.username,
            credential_ref=credential_ref,
            auth_database=payload.auth_database,
            ssl_enabled=payload.ssl_enabled,
            description=payload.description,
            metadata=payload.metadata,
        )
    )

    return SourceResponse(**registered.to_dict())


@sources_router.get("", response_model=list[SourceResponse], operation_id="listSources")
def list_sources(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: OrchestrationService = Depends(get_service),
) -> list[SourceResponse]:
    return [
        SourceResponse(**source.to_dict())
        for source in service.sources.list(limit=limit, offset=offset)
    ]


@sources_router.get(
    "/{source_id}", response_model=SourceResponse, operation_id="getSource"
)
def get_source(
    source_id: str, service: OrchestrationService = Depends(get_service)
) -> SourceResponse:
    return SourceResponse(**service.sources.get(source_id).to_dict())


@sources_router.post(
    "/{source_id}/test",
    response_model=ConnectionTestResponse,
    operation_id="testSourceConnection",
)
def test_source(
    source_id: str, service: OrchestrationService = Depends(get_service)
) -> ConnectionTestResponse:
    """Open a connection through the Phase 3 connector and report safely."""
    from erp_pipeline.connectors import ConnectorRegistry

    source = service.sources.get(source_id)
    started = time.perf_counter()
    settings = source.connection_settings(service.services.secrets)

    try:
        connector = ConnectorRegistry.create(settings)
        result = connector.test_connection()
        capabilities = connector.get_capabilities()
        metadata = None

        try:
            metadata = connector.get_source_metadata()
        except Exception:  # noqa: BLE001 - metadata is a bonus, not the test
            metadata = None

        connector.close()

        return ConnectionTestResponse(
            source_id=source_id,
            source_type=source.source_type.value,
            success=bool(getattr(result, "success", False)),
            message=getattr(result, "message", None),
            capabilities=[
                name
                for name in dir(capabilities)
                if name.startswith("supports_") and getattr(capabilities, name)
            ],
            server_version=getattr(metadata, "server_version", None),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    except Exception as error:  # noqa: BLE001 - a failed test is a result
        # The exception text may embed a DSN, so only the type is reported.
        return ConnectionTestResponse(
            source_id=source_id,
            source_type=source.source_type.value,
            success=False,
            message=f"connection failed ({type(error).__name__})",
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )


@sources_router.post(
    "/{source_id}/discover",
    response_model=DiscoveryResponse,
    operation_id="discoverSourceSchema",
)
def discover_source(
    source_id: str, service: OrchestrationService = Depends(get_service)
) -> DiscoveryResponse:
    """Delegates to Phase 4 or Phase 5. No discovery logic lives here."""
    source = service.sources.get(source_id)
    schema = service.services.discover_schema(source)
    warnings: list[str] = []
    # Structure discovered here becomes semantically searchable without a
    # second call. Row data is untouched.
    schema_job_id, schema_status, problem = service.index_schema(schema.schema_id)

    if problem:
        warnings.append(problem)

    return DiscoveryResponse(
        source_id=source_id,
        schema_id=schema.schema_id,
        schema_hash=getattr(schema, "schema_hash", None),
        schema_version=getattr(schema, "schema_version", None),
        entity_count=len(schema.entities),
        field_count=sum(len(entity.fields) for entity in schema.entities),
        relationship_count=len(getattr(schema, "relationships", ()) or ()),
        published=service.services.catalog is not None,
        schema_index_job_id=schema_job_id,
        schema_indexing_status=schema_status,
        warnings=warnings,
    )


__all__ = [
    "health_router",
    "capabilities_router",
    "sources_router",
    "get_service",
    "get_settings",
]
