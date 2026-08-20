"""Job lifecycle: planning, stage order, failure halting, partial, idempotency."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InvalidPipelineRequestError,
    Job,
    JobConflictError,
    JobExecutor,
    JobRequest,
    JobStatus,
    JobType,
    OrchestrationService,
    PipelineContext,
    PipelinePlanner,
    PipelineRunner,
    PipelineServices,
    PipelineStage,
    RegisteredSource,
    RetryNotSupportedError,
    StageFailure,
    StageStatus,
    UnsupportedCapabilityError,
    UploadStore,
    sanitize_display_name,
    validate_identifier,
)
from erp_pipeline.orchestration.errors import UnsafeUploadNameError, UploadTooLargeError
from erp_pipeline.schemas.enums import SourceType

STRUCTURED_ORDER = [
    PipelineStage.DISCOVER,
    PipelineStage.MAP,
    PipelineStage.EXTRACT,
    PipelineStage.TRANSFORM,
    PipelineStage.VALIDATE,
    PipelineStage.LOAD,
    PipelineStage.AI_BUILD,
    PipelineStage.EMBED,
    PipelineStage.TIER_ROUTE,
]


def make_service(handlers=None, executor=None, services=None) -> OrchestrationService:
    service = OrchestrationService(
        services=services or PipelineServices(),
        job_store=InMemoryJobStore(),
        executor=executor or InlineJobExecutor(),
        handlers=handlers,
    )
    service.sources.register(
        RegisteredSource(
            source_id="erp_db",
            name="ERP DB",
            source_type=SourceType.POSTGRESQL,
            host="localhost",
        )
    )

    return service


# ----------------------------------------------------------------------
# Stage order - asserted from the execution trace, not from names
# ----------------------------------------------------------------------


def test_structured_stages_execute_in_the_required_order():
    """Names in a list prove nothing; this records what actually ran."""
    trace: list[PipelineStage] = []

    def recorder(stage: PipelineStage):
        def handler(context: PipelineContext):
            trace.append(stage)
            return {"ran": stage.value}

        return handler

    service = make_service(
        handlers={stage: recorder(stage) for stage in STRUCTURED_ORDER}
    )
    job = service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")
    )
    finished = service.get(job.job_id)

    assert trace == STRUCTURED_ORDER
    assert finished.status is JobStatus.SUCCEEDED

    # Each dependency, stated explicitly.
    for earlier, later in zip(STRUCTURED_ORDER, STRUCTURED_ORDER[1:]):
        assert trace.index(earlier) < trace.index(later)


def test_recorded_stage_history_matches_the_execution_order():
    service = make_service(
        handlers={stage: (lambda ctx: {}) for stage in STRUCTURED_ORDER}
    )
    job = service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")
    )

    recorded = [
        run.stage
        for run in service.get(job.job_id).stages
        if run.status is StageStatus.SUCCEEDED
    ]

    assert recorded == STRUCTURED_ORDER


# ----------------------------------------------------------------------
# Failure halts the pipeline
# ----------------------------------------------------------------------


def test_a_transform_failure_skips_every_later_stage():
    """Embedding records that never transformed would be worse than stopping."""
    ran: list[str] = []

    def ok(context: PipelineContext):
        ran.append("ok")
        return {}

    def boom(context: PipelineContext):
        raise StageFailure("transformation failed", code="TRANSFORM_FAILED")

    handlers = {stage: ok for stage in STRUCTURED_ORDER}
    handlers[PipelineStage.TRANSFORM] = boom

    service = make_service(handlers=handlers)
    job = service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")
    )
    finished = service.get(job.job_id)

    assert finished.status is JobStatus.FAILED
    assert finished.error_code == "TRANSFORM_FAILED"
    assert finished.stage(PipelineStage.TRANSFORM).status is StageStatus.FAILED

    for stage in (
        PipelineStage.VALIDATE,
        PipelineStage.LOAD,
        PipelineStage.AI_BUILD,
        PipelineStage.EMBED,
        PipelineStage.TIER_ROUTE,
    ):
        assert finished.stage(stage).status is StageStatus.SKIPPED

    # DISCOVER, MAP, EXTRACT ran; nothing after TRANSFORM did.
    assert len(ran) == 3


def test_a_failed_job_never_reports_stage_success():
    handlers = {stage: (lambda ctx: {}) for stage in STRUCTURED_ORDER}
    handlers[PipelineStage.EMBED] = lambda ctx: (_ for _ in ()).throw(
        StageFailure("model unavailable", code="MODEL_UNAVAILABLE")
    )

    service = make_service(handlers=handlers)
    job = service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")
    )
    finished = service.get(job.job_id)

    assert finished.status is JobStatus.FAILED
    assert finished.stage(PipelineStage.TIER_ROUTE).status is StageStatus.SKIPPED
    assert finished.stage(PipelineStage.TIER_ROUTE).status is not StageStatus.SUCCEEDED


def test_an_unexpected_exception_does_not_leak_its_message():
    """An arbitrary exception's text could contain a row value or a DSN."""

    def leaky(context: PipelineContext):
        raise RuntimeError("connection to postgresql://erp:hunter2@db/erp failed")

    handlers = {stage: (lambda ctx: {}) for stage in STRUCTURED_ORDER}
    handlers[PipelineStage.EXTRACT] = leaky

    service = make_service(handlers=handlers)
    job = service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")
    )
    finished = service.get(job.job_id)

    assert finished.status is JobStatus.FAILED
    assert "hunter2" not in str(finished.to_dict())
    assert "postgresql://" not in str(finished.to_dict())


# ----------------------------------------------------------------------
# PARTIAL
# ----------------------------------------------------------------------


def test_rejected_records_produce_partial_not_success():
    """Reporting SUCCEEDED while records were dropped would hide the problem."""

    def transform(context: PipelineContext):
        context.counters = context.counters.merged(
            records_transformed=95, records_failed=5
        )
        context.partial_reasons.append("5 record(s) were rejected")
        return {"records_rejected": 5}

    handlers = {stage: (lambda ctx: {}) for stage in STRUCTURED_ORDER}
    handlers[PipelineStage.TRANSFORM] = transform

    service = make_service(handlers=handlers)
    job = service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")
    )
    finished = service.get(job.job_id)

    assert finished.status is JobStatus.PARTIAL
    assert finished.counters.records_transformed == 95
    assert finished.counters.records_failed == 5


def test_counters_omit_numbers_no_service_reported():
    """An unreported count is absent, never a confident zero."""
    service = make_service(handlers={stage: (lambda ctx: {}) for stage in STRUCTURED_ORDER})
    job = service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")
    )

    assert service.get(job.job_id).counters.to_dict() == {}


# ----------------------------------------------------------------------
# Capability boundaries
# ----------------------------------------------------------------------


def test_asking_an_openapi_source_for_records_is_refused_before_any_work():
    service = make_service()
    service.sources.register(
        RegisteredSource(
            source_id="vendor_api",
            name="Vendor API",
            source_type=SourceType.OPENAPI,
        )
    )

    with pytest.raises(UnsupportedCapabilityError):
        service.submit(
            JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="vendor_api")
        )

    # Refused synchronously: no job row was created for an impossible request.
    assert service.list() == ()


def test_a_document_job_needs_no_mapping_stage():
    planner = PipelinePlanner()
    plan = planner.plan(
        JobRequest(job_type=JobType.DOCUMENT_PIPELINE, upload_id="upl_1"),
        SourceType.PDF,
    )

    assert PipelineStage.MAP not in plan.stages
    assert PipelineStage.TRANSFORM not in plan.stages
    assert PipelineStage.MAP in plan.not_applicable
    assert plan.stages[0] is PipelineStage.INGEST


def test_a_csv_pipeline_skips_discovery_because_there_is_nothing_to_discover():
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="csv"),
        SourceType.CSV,
    )

    assert PipelineStage.DISCOVER not in plan.stages
    assert PipelineStage.DISCOVER in plan.not_applicable
    assert plan.stages[0] is PipelineStage.MAP


def test_the_api_spec_plan_never_includes_extraction():
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.API_SPEC_PREPARATION, upload_id="u"),
        SourceType.OPENAPI,
    )

    assert list(plan.stages) == [
        PipelineStage.PARSE_SPEC,
        PipelineStage.SCHEMA,
        PipelineStage.MAP,
    ]
    assert PipelineStage.EXTRACT in plan.not_applicable


def test_the_incremental_plan_uses_phase_10_stages():
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.INCREMENTAL_SYNC, source_id="db"),
        SourceType.POSTGRESQL,
    )

    assert plan.stages[0] is PipelineStage.DRIFT_CHECK
    assert PipelineStage.EXTRACT_CHANGED in plan.stages
    assert PipelineStage.TIER_UPDATE in plan.stages


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


def test_the_same_key_and_payload_returns_the_same_job():
    service = make_service(handlers={stage: (lambda ctx: {}) for stage in STRUCTURED_ORDER})
    request = JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")

    first = service.submit(request, idempotency_key="key-1")
    second = service.submit(request, idempotency_key="key-1")

    assert first.job_id == second.job_id
    assert len(service.list()) == 1


def test_the_same_key_with_a_different_payload_is_a_conflict():
    service = make_service(handlers={stage: (lambda ctx: {}) for stage in STRUCTURED_ORDER})

    service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db"),
        idempotency_key="key-2",
    )

    with pytest.raises(JobConflictError):
        service.submit(
            JobRequest(
                job_type=JobType.STRUCTURED_PIPELINE,
                source_id="erp_db",
                entity="different_table",
            ),
            idempotency_key="key-2",
        )


def test_an_idempotency_key_is_not_record_identity():
    """It scopes a submission; it must never become a domain id."""
    service = make_service(handlers={stage: (lambda ctx: {}) for stage in STRUCTURED_ORDER})
    job = service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db"),
        idempotency_key="key-3",
    )

    assert "key-3" not in job.job_id
    assert job.job_id.startswith("job_")


# ----------------------------------------------------------------------
# Retry
# ----------------------------------------------------------------------


def test_only_failed_jobs_may_be_retried():
    service = make_service(handlers={stage: (lambda ctx: {}) for stage in STRUCTURED_ORDER})
    job = service.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")
    )

    with pytest.raises(RetryNotSupportedError):
        service.retry(job.job_id)


def test_incremental_sync_is_not_generically_retryable():
    """Replaying a partial sync could reprocess or skip changes."""
    service = make_service(handlers={})
    store = service.jobs
    failed = Job(
        job_id="job_x",
        request=JobRequest(job_type=JobType.INCREMENTAL_SYNC, source_id="erp_db"),
        status=JobStatus.FAILED,
    )
    store.create(failed)

    with pytest.raises(RetryNotSupportedError):
        service.retry("job_x")


# ----------------------------------------------------------------------
# Executor bound
# ----------------------------------------------------------------------


def test_the_executor_never_exceeds_its_worker_bound():
    """Unbounded workers would let a few callers exhaust the host."""
    executor = JobExecutor(max_workers=2)
    barrier = threading.Event()

    def slow():
        time.sleep(0.05)
        return True

    try:
        futures = [executor.submit(slow) for _ in range(8)]

        for future in futures:
            future.result(timeout=10)

        assert executor.peak_active <= 2
        assert executor.submitted == 8
        assert executor.completed == 8
    finally:
        executor.shutdown()


def test_a_failing_job_does_not_kill_its_worker():
    executor = JobExecutor(max_workers=1)

    try:
        bad = executor.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        with pytest.raises(RuntimeError):
            bad.result(timeout=10)

        assert executor.submit(lambda: "fine").result(timeout=10) == "fine"
    finally:
        executor.shutdown()


# ----------------------------------------------------------------------
# Safe extraction
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "users; DROP TABLE invoices",
        "users--",
        'users" OR "1"="1',
        "../etc/passwd",
        "",
        "1invalid",
    ],
)
def test_extraction_refuses_identifiers_it_cannot_prove_safe(hostile: str):
    """Refused rather than escaped: refusal is easier to prove correct."""
    with pytest.raises(InvalidPipelineRequestError):
        validate_identifier(hostile, "table")


def test_extraction_builds_a_bounded_ordered_select():
    from erp_pipeline.orchestration.extraction import (
        ExtractionRequest,
        RelationalSnapshotExtractor,
    )
    from erp_pipeline.schemas.source_models import SourceEntity, SourceField
    from erp_pipeline.schemas.enums import FieldDataType

    entity = SourceEntity(
        entity_id="e1",
        source_name="invoices",
        normalized_name="invoices",
        fields=(
            SourceField(
                source_name="invoice_id",
                normalized_name="invoice_id",
                source_data_type="text",
                normalized_data_type=FieldDataType.STRING,
                nullable=False,
                is_primary_key=True,
            ),
            SourceField(
                source_name="amount",
                normalized_name="amount",
                source_data_type="numeric",
                normalized_data_type=FieldDataType.DECIMAL,
                nullable=True,
            ),
        ),
        primary_key_fields=("invoice_id",),
    )

    statement = RelationalSnapshotExtractor().build_statement(
        ExtractionRequest(schema=None, entity=entity, limit=10)
    )

    assert statement.startswith("SELECT ")
    assert "ORDER BY" in statement
    assert "LIMIT 10" in statement
    # Read-only. Nothing that writes may ever be generated here.
    for forbidden in ("DROP", "DELETE", "UPDATE", "INSERT", ";"):
        assert forbidden not in statement.upper()


def test_the_extraction_limit_is_capped():
    from erp_pipeline.orchestration.extraction import ExtractionRequest, MAX_BATCH_SIZE

    request = ExtractionRequest(schema=None, entity=None, limit=10_000_000)

    assert request.bounded_limit == MAX_BATCH_SIZE


# ----------------------------------------------------------------------
# Upload safety
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\cmd.exe",
        "/absolute/path.csv",
        "C:\\Windows\\system.ini",
    ],
)
def test_upload_names_cannot_traverse(hostile: str, tmp_path: Path):
    """The stored path comes from a generated id; the name is only displayed."""
    store = UploadStore(tmp_path)
    stored = store.store_bytes(b"a,b\n1,2\n", filename=hostile)

    assert ".." not in stored.display_name
    assert "/" not in stored.display_name
    assert "\\" not in stored.display_name
    # The file is contained whatever the caller called it: the sanitized name
    # is kept (downstream schema inference reads meaning from it), but it
    # always sits inside a per-upload directory the caller cannot influence.
    resolved = stored.path.resolve()

    assert str(resolved).startswith(str(tmp_path.resolve()))
    assert stored.path.parent.name == stored.upload_id
    assert store.path_for(stored.upload_id) == resolved


def test_an_oversized_upload_is_refused_and_leaves_no_file(tmp_path: Path):
    store = UploadStore(tmp_path, max_bytes=64)

    with pytest.raises(UploadTooLargeError):
        store.store_bytes(b"x" * 5000, filename="big.csv")

    assert list(tmp_path.rglob("*.csv")) == []


def test_a_stored_upload_never_exposes_its_absolute_path(tmp_path: Path):
    store = UploadStore(tmp_path)
    stored = store.store_bytes(b"a,b\n1,2\n", filename="fine.csv")

    assert "path" not in stored.to_dict()
    assert str(tmp_path) not in str(stored.to_dict())


def test_upload_content_is_hashed(tmp_path: Path):
    store = UploadStore(tmp_path)

    first = store.store_bytes(b"same", filename="a.csv")
    second = store.store_bytes(b"same", filename="b.csv")

    assert first.content_hash == second.content_hash
    assert first.upload_id != second.upload_id
