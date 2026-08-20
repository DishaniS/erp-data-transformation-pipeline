"""The FastAPI application factory.

WHY A FACTORY AND NOT A MODULE-LEVEL APP
----------------------------------------
Importing this module must not load a 90 MB sentence-transformer, open a
PostgreSQL pool or connect to Qdrant. If it did, every test run and every
``--help`` would pay for infrastructure it never uses, and the API would be
untestable without the full stack running.

So ``create_app`` takes its services as an argument. Nothing heavy is
constructed unless a caller explicitly asks for it via ``build_services``.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from erp_pipeline.api.config import API_PREFIX, API_TITLE, API_VERSION, ApiSettings
from erp_pipeline.api.responses import error_body, failure
from erp_pipeline.api.routers import (
    capabilities_router,
    health_router,
    sources_router,
)
from erp_pipeline.api.routers_data import (
    files_router,
    jobs_router,
    mappings_router,
    records_router,
    schemas_router,
    search_router,
    specs_router,
)
from erp_pipeline.api.security import API_KEY_HEADER, keys_match, requires_key
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    OrchestrationError,
    InMemoryJobStore,
    JobExecutor,
    OrchestrationService,
    PipelineServices,
    UploadStore,
)

LOGGER = logging.getLogger("erp_pipeline.api")

DESCRIPTION = """
Control plane for the ERP-Aware Data Transformation Pipeline.

This API **orchestrates** the pipeline's existing phases - schema discovery,
mapping, transformation, validation, incremental sync, embedding and hybrid
tiered vector storage. It does not reimplement any of them.

**Boundaries.** Uploaded OpenAPI and Postman documents are parsed as contracts;
the endpoints they describe are never called. No LLM is used and no generated
answers are produced.
"""


def create_app(
    settings: ApiSettings | None = None,
    services: PipelineServices | None = None,
    orchestration: OrchestrationService | None = None,
) -> FastAPI:
    """Build the application. Everything heavy is injected, never imported."""
    resolved = settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A job left RUNNING by a dead process is marked INTERRUPTED here -
        # never silently treated as successful.
        try:
            reaped = app.state.orchestration.recover_interrupted_jobs()

            if reaped:
                LOGGER.warning(
                    "marked %d job(s) interrupted after an unclean shutdown",
                    len(reaped),
                )
        except Exception:  # noqa: BLE001 - startup must not die on recovery
            LOGGER.exception("interrupted-job recovery failed")

        yield

        executor = getattr(app.state.orchestration, "executor", None)

        if executor is not None and hasattr(executor, "shutdown"):
            executor.shutdown(wait=False)

    app = FastAPI(
        title=API_TITLE,
        description=DESCRIPTION,
        version=API_VERSION,
        lifespan=lifespan,
        docs_url="/docs" if resolved.docs_enabled else None,
        redoc_url="/redoc" if resolved.docs_enabled else None,
        openapi_url="/openapi.json" if resolved.docs_enabled else None,
    )

    app.state.settings = resolved
    app.state.orchestration = orchestration or OrchestrationService(
        services=services or PipelineServices(),
        job_store=InMemoryJobStore(),
        executor=InlineJobExecutor(),
    )

    # -- CORS: closed unless explicitly configured ----------------------
    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_origins),
            # Never a wildcard with credentials; that combination lets any
            # origin act as the user.
            allow_credentials=resolved.cors_allow_credentials,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Content-Type", API_KEY_HEADER, "Idempotency-Key"],
        )

    # -- request id + auth ---------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Attach an operational request id and enforce the API key.

        The id is a random UUID because it identifies a REQUEST, not an ERP
        record. Domain identity elsewhere in this pipeline is deterministic and
        must never be derived from this value.
        """
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id

        if resolved.auth_enabled and requires_key(
            request.method, request.url.path, resolved.protect_reads
        ):
            supplied = request.headers.get(API_KEY_HEADER)

            if not keys_match(supplied, resolved.api_key):
                # The supplied key is never echoed or logged.
                LOGGER.warning(
                    "rejected unauthenticated request",
                    extra={
                        "request_id": request_id,
                        "path": request.url.path,
                        "method": request.method,
                    },
                )

                return JSONResponse(
                    status_code=401,
                    content=failure(
                        "UNAUTHORIZED",
                        "A valid API key is required for this endpoint.",
                        request_id,
                    ),
                    headers={"X-Request-ID": request_id},
                )

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id

        return response

    # -- error handling -------------------------------------------------
    # Registered against the DOMAIN base class, not `Exception`. A handler on
    # `Exception` is served by Starlette's server-error middleware, which
    # re-raises under a test client and would turn every typed 404 into a test
    # crash rather than a response.
    @app.exception_handler(OrchestrationError)
    async def handle_domain_error(
        request: Request, exc: OrchestrationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        status, body = error_body(exc, request_id)

        return JSONResponse(status_code=status, content=body)

    @app.exception_handler(Exception)
    async def handle_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        status, body = error_body(exc, request_id)

        if status >= 500:
            LOGGER.exception(
                "unhandled error", extra={"request_id": request_id}
            )

        return JSONResponse(status_code=status, content=body)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)

        return JSONResponse(
            status_code=422,
            content=failure(
                "VALIDATION_ERROR",
                "The request payload is not valid.",
                request_id,
                {
                    "fields": [
                        {
                            "location": ".".join(str(p) for p in err.get("loc", ())),
                            "problem": err.get("msg"),
                        }
                        for err in exc.errors()[:20]
                    ]
                },
            ),
        )

    for router in (
        health_router,
        capabilities_router,
        sources_router,
        files_router,
        specs_router,
        schemas_router,
        mappings_router,
        jobs_router,
        search_router,
        records_router,
    ):
        app.include_router(router)

    return app


def build_services(
    settings: ApiSettings | None = None,
    with_embedding: bool = True,
    with_storage: bool = True,
    engine: Any = None,
) -> PipelineServices:
    """Construct the real phase services. Explicit, never on import.

    This is the one function that loads a model and opens connections, so a
    caller always knows when they are paying that cost.
    """
    resolved = settings or ApiSettings()
    services = PipelineServices(uploads=UploadStore(resolved.upload_dir))

    from erp_pipeline.ingestion import FileIngestionService
    from erp_pipeline.api_specs import ApiSpecificationService
    from erp_pipeline.mapping import MappingService
    from erp_pipeline.transformation import TransformationService

    services.ingestion = FileIngestionService()
    services.api_specs = ApiSpecificationService()
    services.mapping = MappingService()
    services.transformation = TransformationService()

    if with_embedding:
        from erp_pipeline.ai import EmbeddingService, SentenceTransformerModel

        services.embedding = EmbeddingService(SentenceTransformerModel())

    if with_storage:
        from erp_pipeline.storage import StorageService

        services.storage = StorageService()

    if engine is not None:
        from erp_pipeline.orchestration import PostgresCanonicalRecordStore

        services.records = PostgresCanonicalRecordStore(engine)

    return services


__all__ = ["create_app", "build_services", "ApiSettings"]
