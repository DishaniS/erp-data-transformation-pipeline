"""The orchestration facade. Callable without FastAPI.

This is the object the API layer talks to, and it deliberately knows nothing
about HTTP. Keeping it framework-free means the whole pipeline can be driven
from a test or a script, and it stops request handling from leaking into
pipeline logic.

Every phase service is injected. Nothing here constructs a model, a database
engine or a vector client on import - see ``build_default_services`` for the
one place that does, called explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.orchestration.errors import (
    InvalidPipelineRequestError,
    MappingNotExecutableError,
    JobConflictError,
    JobNotFoundError,
    MappingNotFoundError,
    RecordNotFoundError,
    RetryNotSupportedError,
    SchemaNotFoundError,
    UnsupportedCapabilityError,
)
from erp_pipeline.orchestration.executor import InlineJobExecutor, JobExecutor
from erp_pipeline.orchestration.extraction import (
    ExtractionRequest,
    RelationalSnapshotExtractor,
    CsvSnapshotExtractor,
)
from erp_pipeline.orchestration.job_store import InMemoryJobStore, JobStore
from erp_pipeline.orchestration.models import (
    Job,
    JobRequest,
    JobStatus,
    JobType,
    PipelineStage,
    StageStatus,
    new_job_id,
)
from erp_pipeline.orchestration.pipeline import PipelineContext, PipelineRunner
from erp_pipeline.orchestration.planner import PipelinePlanner
from erp_pipeline.orchestration.secrets import NullSecretProvider, SecretProvider
from erp_pipeline.orchestration.sources import RegisteredSource, SourceRegistry
from erp_pipeline.orchestration.stages import DEFAULT_HANDLERS, INCREMENTAL_HANDLERS
from erp_pipeline.orchestration.upload_store import StoredUpload, UploadStore
from erp_pipeline.schemas.enums import SourceType

LOGGER = logging.getLogger("erp_pipeline.orchestration.service")


@dataclass
class PipelineServices:
    """The phase services orchestration calls. All optional, all injectable.

    A missing service is not an error at construction: a deployment with no
    Qdrant should still be able to register sources and parse specs. It becomes
    an error only when a stage actually needs it.
    """

    catalog: Any = None
    mapping: Any = None
    transformation: Any = None
    ingestion: Any = None
    api_specs: Any = None
    sync: Any = None
    embedding: Any = None
    storage: Any = None
    records: Any = None
    sources: SourceRegistry = field(default_factory=SourceRegistry)
    uploads: UploadStore | None = None
    secrets: SecretProvider = field(default_factory=NullSecretProvider)
    connection_factory: Any = None
    #: Set by the service so stages can reach schemas without a catalog.
    schema_cache: dict[str, Any] = field(default_factory=dict)
    mapping_cache: dict[str, Any] = field(default_factory=dict)
    upload_results: dict[str, Any] = field(default_factory=dict)
    #: Mappings that are NOT yet executable because Phase 8 found ambiguity.
    #: They are addressable so a human can resolve them, but a draft can never
    #: be handed to a transformation - see `get_mapping_profile`.
    mapping_drafts: dict[str, Any] = field(default_factory=dict)

    # -- helpers the stage handlers call --

    @property
    def embedding_model_id(self) -> str:
        return getattr(self.embedding, "model_id", "unavailable")

    def _require(self, service: Any, name: str) -> Any:
        if service is None:
            raise UnsupportedCapabilityError(
                f"this deployment has no {name} configured, so that stage "
                "cannot run"
            )

        return service

    def get_schema(self, schema_id: str) -> Any:
        if schema_id in self.schema_cache:
            return self.schema_cache[schema_id]

        if self.catalog is not None:
            try:
                schema = self.catalog.get_snapshot(schema_id)
            except Exception as error:  # noqa: BLE001 - normalised below
                raise SchemaNotFoundError(
                    f"schema {schema_id!r} was not found", schema_id=schema_id
                ) from error

            self.schema_cache[schema_id] = schema

            return schema

        raise SchemaNotFoundError(
            f"schema {schema_id!r} was not found", schema_id=schema_id
        )

    def get_mapping_profile(self, mapping_id: str) -> Any:
        if mapping_id in self.mapping_cache:
            return self.mapping_cache[mapping_id]

        # A draft exists but is not executable. Refusing here is the whole
        # point of Phase 8's ambiguity detection: a pipeline must not run
        # through a mapping nobody approved.
        if mapping_id in self.mapping_drafts:
            draft = self.mapping_drafts[mapping_id]

            raise MappingNotExecutableError(
                f"mapping {mapping_id!r} still has "
                f"{draft['ambiguous_fields']} ambiguous field(s) awaiting "
                "review; approve them before running data through it",
                mapping_id=mapping_id,
                ambiguous_fields=draft["ambiguous_fields"],
            )

        if self.catalog is not None:
            try:
                profile = self.catalog.get_mapping_profile(mapping_id)
            except Exception as error:  # noqa: BLE001
                raise MappingNotFoundError(
                    f"mapping {mapping_id!r} was not found", mapping_id=mapping_id
                ) from error

            self.mapping_cache[mapping_id] = profile

            return profile

        raise MappingNotFoundError(
            f"mapping {mapping_id!r} was not found", mapping_id=mapping_id
        )

    def discover_schema(self, source: RegisteredSource) -> Any:
        """Delegates to Phase 4 or Phase 5 - never re-implements discovery."""
        settings = source.connection_settings(self.secrets)

        if source.source_type is SourceType.MONGODB:
            from erp_pipeline.discovery import MongoDBInferenceService

            result = MongoDBInferenceService().infer(settings)
            schema = getattr(result, "schema", result)
        else:
            from erp_pipeline.discovery import RelationalDiscoveryService

            result = RelationalDiscoveryService().discover(settings)
            schema = getattr(result, "schema", result)

        self.schema_cache[schema.schema_id] = schema

        if self.catalog is not None:
            try:
                self.catalog.publish_schema(schema)
            except Exception:  # noqa: BLE001 - publishing is best effort
                LOGGER.warning("schema discovered but not published to the catalog")

        return schema

    def extract_snapshot(
        self, source: RegisteredSource, request: ExtractionRequest
    ) -> tuple[Any, ...]:
        if self.connection_factory is not None:
            factory = lambda: self.connection_factory(source)  # noqa: E731
        else:
            factory = self._sqlalchemy_factory(source)

        return RelationalSnapshotExtractor().extract(request, factory)

    def _sqlalchemy_factory(self, source: RegisteredSource):
        import sqlalchemy as sa

        password = (
            self.secrets.resolve(source.credential_ref)
            if source.credential_ref
            else ""
        )
        url = sa.engine.URL.create(
            drivername="postgresql+psycopg2",
            username=source.username,
            password=password,
            host=source.host,
            port=source.port,
            database=source.database,
        )
        engine = sa.create_engine(url)

        return engine.connect

    def extract_csv_records(
        self, upload_id: str, entity: Any, limit: int
    ) -> tuple[Any, ...]:
        result = self.upload_results.get(upload_id)

        if result is None:
            result = self.ingest_upload(upload_id)

        rows = _csv_rows(result)

        return CsvSnapshotExtractor().extract_rows(rows, entity, limit)

    def transform(
        self,
        records: Sequence[Any],
        profile: Any,
        schema: Any,
        source_type: Any = None,
    ) -> Any:
        from erp_pipeline.transformation import TransformationContext

        service = self._require(self.transformation, "transformation service")

        # `schema.origin` is a SchemaOrigin (how the schema was obtained), NOT
        # a SourceType (what kind of system it came from). Passing one where
        # the other is expected makes every record fail provenance
        # construction, so the source type is taken from the plan instead.
        context = TransformationContext(
            source_type=source_type,
            schema_id=getattr(schema, "schema_id", None),
            schema_version=getattr(schema, "schema_version", None),
        )

        return service.transform_records(records, profile, context)

    def build_representations(self, records: Iterable[Any]) -> tuple[Any, ...]:
        from erp_pipeline.ai import canonical_record_to_representation

        return tuple(canonical_record_to_representation(r) for r in records)

    def build_document_representations(self, result: Any) -> tuple[Any, ...]:
        from erp_pipeline.ai import document_to_representations

        return tuple(document_to_representations(result))

    def embed(self, representations: Sequence[Any]) -> Any:
        service = self._require(self.embedding, "embedding service")

        return service.embed_many(representations)

    def store_vector(self, record: Any) -> Any:
        service = self._require(self.storage, "storage service")

        return service.store(record)

    def ingest_upload(self, upload_id: str) -> Any:
        service = self._require(self.ingestion, "file ingestion service")
        uploads = self._require(self.uploads, "upload store")
        result = service.ingest(uploads.path_for(upload_id))
        self.upload_results[upload_id] = result

        return result

    def parse_api_spec(self, upload_id: str) -> Any:
        service = self._require(self.api_specs, "API specification service")
        uploads = self._require(self.uploads, "upload store")

        return service.parse(uploads.path_for(upload_id))

    # ------------------------------------------------------------------
    # Phase 10 delegation
    # ------------------------------------------------------------------

    #: Source types whose changes can be polled with a watermark. A PDF or an
    #: OpenAPI document has no cursor and no change stream, so pretending it
    #: supports CDC would be a lie the planner then has to live with.
    INCREMENTAL_SOURCES = frozenset(
        {
            SourceType.POSTGRESQL,
            SourceType.MYSQL,
            SourceType.SQL_SERVER,
            SourceType.MONGODB,
        }
    )

    def build_sync_target(self, request: JobRequest, schema: Any) -> Any:
        """Assemble Phase 10's ``SyncTarget`` from the job request."""
        from erp_pipeline.sync import SyncTarget

        profile = (
            self.get_mapping_profile(request.mapping_id)
            if request.mapping_id
            else None
        )
        source = self.sources.get(request.source_id)

        from erp_pipeline.orchestration.extraction import resolve_entity

        entity = resolve_entity(schema, request.entity)

        return SyncTarget(
            source_system_id=source.source_id,
            source_entity=entity.source_name,
            source_type=source.source_type,
            mapping_profile=profile,
            schema_id=getattr(schema, "schema_id", None),
            schema_hash=getattr(schema, "schema_hash", None),
        )

    def build_extraction_config(self, request: JobRequest, schema: Any) -> Any:
        """Validate the incremental cursor against the DISCOVERED schema.

        A watermark field is an identifier that reaches SQL, so it is checked
        against the schema rather than trusted. Accepting an arbitrary string
        here would undo the extraction guarantees the rest of the system keeps.
        """
        from erp_pipeline.orchestration.extraction import resolve_entity
        from erp_pipeline.sync import ExtractionConfig, WatermarkStrategy

        entity = resolve_entity(schema, request.entity)
        known = {field.source_name for field in entity.fields}
        options = request.options

        def checked(name: str, label: str) -> str | None:
            value = options.get(name)

            if value is None:
                return None

            if value not in known:
                raise InvalidPipelineRequestError(
                    f"{label} {value!r} is not a field of "
                    f"{entity.source_name!r}; incremental extraction only "
                    "accepts fields present in the discovered schema"
                )

            return value

        watermark_field = checked("watermark_field", "watermark field")

        if watermark_field is None:
            raise InvalidPipelineRequestError(
                "an incremental sync needs options.watermark_field naming a "
                f"monotonic column of {entity.source_name!r} (for example an "
                "updated_at timestamp)"
            )

        key_field = checked("key_field", "key field") or (
            entity.primary_key_fields[0] if entity.primary_key_fields else None
        )

        if key_field is None:
            raise InvalidPipelineRequestError(
                f"{entity.source_name!r} has no primary key and no "
                "options.key_field was supplied"
            )

        strategy_name = str(options.get("strategy", "composite")).lower()

        try:
            strategy = WatermarkStrategy(strategy_name)
        except ValueError as error:
            raise InvalidPipelineRequestError(
                f"{strategy_name!r} is not a supported watermark strategy"
            ) from error

        return ExtractionConfig(
            source_entity=entity.source_name,
            strategy=strategy,
            key_field=key_field,
            watermark_field=watermark_field,
            # Ties on the watermark are broken by the key, which is what makes
            # equal timestamps safe rather than a source of skipped rows.
            tie_break_field=checked("tie_break_field", "tie-break field") or key_field,
            namespace=entity.namespace,
            deleted_flag_field=checked("deleted_flag_field", "soft-delete field"),
        )

    def _incremental_engine(self, source: RegisteredSource) -> Any:
        """The SQLAlchemy engine Phase 10's extractor reads changes from."""
        if self.connection_factory is not None:
            return self.connection_factory(source)

        import sqlalchemy as sa

        password = (
            self.secrets.resolve(source.credential_ref)
            if source.credential_ref
            else ""
        )
        url = sa.engine.URL.create(
            drivername="postgresql+psycopg2",
            username=source.username,
            password=password,
            host=source.host,
            port=source.port,
            database=source.database,
        )

        return sa.create_engine(url, pool_pre_ping=True)

    def check_drift(self, request: JobRequest) -> Any:
        """Really invoke Phase 10's drift computation.

        This previously returned a cached attribute, which meant the API could
        report "no drift" for a schema nobody had compared. It now discovers
        the CURRENT schema, loads the PREVIOUS one from the catalog, and hands
        both to Phase 10 - which owns the diff and the mapping-impact rules.
        """
        service = self._require(self.sync, "sync service")
        source = self.sources.get(request.source_id)

        new_schema = self.discover_schema(source)
        previous_schema = None

        if self.catalog is not None:
            try:
                history = self.catalog.history(
                    source.source_id, getattr(new_schema, "schema_name", None)
                )
                # history[0] is the snapshot just published, so the comparison
                # baseline is the one before it.
                for record in list(history)[1:]:
                    candidate = self.catalog.get_snapshot(record.schema_id)

                    if candidate.schema_id != new_schema.schema_id:
                        previous_schema = candidate
                        break
            except Exception:  # noqa: BLE001 - absence is a valid answer
                previous_schema = None

        target = self.build_sync_target(request, new_schema)

        return service.check_drift(
            target=target,
            new_schema=new_schema,
            previous_schema=previous_schema,
        )

    def run_incremental(self, request: JobRequest) -> Any:
        """Really invoke Phase 10's incremental run.

        Phase 13 supplies configuration and collaborators; every watermark,
        checkpoint, content-hash and at-least-once decision stays inside
        Phase 10.
        """
        service = self._require(self.sync, "sync service")
        source = self.sources.get(request.source_id)

        if source.source_type not in self.INCREMENTAL_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source.source_type.value} has no incremental cursor, so "
                "change data capture is not available for it",
                code_hint="UNSUPPORTED_INCREMENTAL_STRATEGY",
                source_type=source.source_type.value,
            )

        if source.source_type is SourceType.MONGODB:
            raise UnsupportedCapabilityError(
                "incremental sync for MongoDB needs a change-stream extractor "
                "that is not configured in this deployment",
                source_type=source.source_type.value,
            )

        schema = (
            self.get_schema(request.schema_id)
            if request.schema_id
            else self.discover_schema(source)
        )

        from erp_pipeline.sync import RelationalIncrementalExtractor, SyncOptions

        config = self.build_extraction_config(request, schema)
        extractor = RelationalIncrementalExtractor(
            self._incremental_engine(source), config
        )
        target = self.build_sync_target(request, schema)

        options = SyncOptions(
            batch_size=int(request.options.get("batch_size", 100)),
            process_deletes=bool(request.options.get("process_deletes", False)),
        )

        return service.run_incremental(
            target=target,
            extractor=extractor,
            options=options,
            strategy=config.strategy,
            watermark_field=config.watermark_field,
            tie_break_field=config.tie_break_field,
        )


def _csv_rows(result: Any) -> Iterable[Any]:
    """Phase 6 exposes rows through a reader; find it without assuming shape."""
    for attribute in ("iter_rows", "rows", "read_rows"):
        candidate = getattr(result, attribute, None)

        if callable(candidate):
            return candidate()

        if candidate is not None:
            return candidate

    reader = getattr(result, "_row_reader", None)

    if callable(reader):
        return reader()

    raise InvalidPipelineRequestError("the CSV result exposes no row reader")


class OrchestrationService:
    """Creates jobs, runs pipelines, and answers questions about them."""

    def __init__(
        self,
        services: PipelineServices | None = None,
        job_store: JobStore | None = None,
        executor: Any = None,
        planner: PipelinePlanner | None = None,
        handlers: Mapping[PipelineStage, Any] | None = None,
    ) -> None:
        self.services = services or PipelineServices()
        self.jobs = job_store or InMemoryJobStore()
        self.executor = executor or InlineJobExecutor()
        self.planner = planner or PipelinePlanner()
        self._handlers = dict(handlers or DEFAULT_HANDLERS)
        self.runner = PipelineRunner(self._handlers)

        # An incremental job reuses stage NAMES that mean something different:
        # Phase 10 does the whole propagation in one call, so those stages
        # report rather than re-execute. Using the standard handlers would
        # transform and store every change a second time.
        self._incremental_runner = PipelineRunner(
            {**self._handlers, **INCREMENTAL_HANDLERS} if handlers is None
            else self._handlers
        )

    # -- sources --

    @property
    def sources(self) -> SourceRegistry:
        return self.services.sources

    @property
    def uploads(self) -> UploadStore | None:
        return self.services.uploads

    # -- lifecycle --

    def recover_interrupted_jobs(self) -> tuple[Job, ...]:
        """Called at startup. Never promotes an unfinished job to success."""
        return self.jobs.reap_interrupted()

    # -- jobs --

    def submit(
        self, request: JobRequest, idempotency_key: str | None = None
    ) -> Job:
        """Validate, plan, persist, enqueue. Returns before the work is done."""
        fingerprint = request.fingerprint()

        if idempotency_key:
            existing = self.jobs.find_by_idempotency_key(idempotency_key)

            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise JobConflictError(
                        "this idempotency key was already used with a "
                        "different request payload",
                        idempotency_key=idempotency_key,
                        existing_job_id=existing.job_id,
                    )

                return existing

        source_type = None

        if request.source_id:
            source_type = self.sources.get(request.source_id).source_type

        # Planning happens before persistence so an impossible request is
        # refused synchronously rather than becoming a failed job.
        plan = self.planner.plan(request, source_type)

        job = Job(
            job_id=new_job_id(),
            request=request,
            status=JobStatus.PENDING,
            stages=self.runner.initial_stages(plan),
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        self.jobs.create(job)

        self.executor.submit(lambda: self._execute(job.job_id, plan))

        return job

    def _execute(self, job_id: str, plan: Any) -> Job:
        job = self.jobs.load(job_id)

        if job is None:  # pragma: no cover - only on external deletion
            raise JobNotFoundError(f"job {job_id!r} disappeared before execution")

        context = PipelineContext(job=job, plan=plan, services=self.services)
        runner = (
            self._incremental_runner
            if job.request.job_type is JobType.INCREMENTAL_SYNC
            else self.runner
        )

        try:
            finished = runner.run(context)
        except Exception as error:  # noqa: BLE001 - a job must always settle
            LOGGER.exception("pipeline raised outside a stage")
            finished = replace(
                job,
                status=JobStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                error_code=getattr(error, "code", "PIPELINE_ERROR"),
                error_message="the pipeline failed before completing",
                version=job.version + 1,
            )

        return self.jobs.save(finished)

    def get(self, job_id: str) -> Job:
        job = self.jobs.load(job_id)

        if job is None:
            raise JobNotFoundError(f"job {job_id!r} was not found", job_id=job_id)

        return job

    def list(self, **filters: Any) -> tuple[Job, ...]:
        return self.jobs.list(**filters)

    def retry(self, job_id: str) -> Job:
        """Re-submit a failed job as a NEW job.

        Deliberately not an in-place resume. Phase 10's incremental state moves
        forward as it runs, so replaying a half-finished sync could reprocess
        or skip changes. Only job types that are safe to run from the start are
        allowed.
        """
        job = self.get(job_id)

        if job.status not in {JobStatus.FAILED, JobStatus.INTERRUPTED}:
            raise RetryNotSupportedError(
                f"only failed or interrupted jobs may be retried; this job is "
                f"{job.status.value}",
                job_id=job_id,
                status=job.status.value,
            )

        if job.request.job_type in {JobType.INCREMENTAL_SYNC}:
            raise RetryNotSupportedError(
                "incremental sync advances watermark state as it runs, so a "
                "generic replay could reprocess or skip changes; re-run it "
                "explicitly instead",
                job_id=job_id,
                job_type=job.request.job_type.value,
            )

        return self.submit(job.request)


__all__ = ["PipelineServices", "OrchestrationService"]
