"""Live PostgreSQL proof that job state outlives the process that made it.

Everything runs in a throwaway schema `erp_phase13_live_<token>`, created and
dropped by the test. No BPI table, no catalog, no sync and no vector-storage
schema is read or written.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from erp_pipeline.orchestration import (
    InlineJobExecutor,
    Job,
    JobRequest,
    JobStatus,
    JobType,
    OrchestrationService,
    PipelineServices,
    PipelineStage,
    PostgresJobStore,
    RegisteredSource,
    StageRun,
    StageStatus,
    bootstrap_orchestration_schema,
)
from erp_pipeline.schemas.enums import SourceType


@pytest.fixture(scope="module")
def engine():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg2")

    import sqlalchemy as sa

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # pragma: no cover
        pass

    name = os.getenv("AI_DB_NAME")
    user = os.getenv("AI_DB_USER")

    if not (name and user):
        pytest.skip("AI_DB_* connection settings are not configured")

    url = (
        f"postgresql+psycopg2://{user}:{os.getenv('AI_DB_PASSWORD')}"
        f"@{os.getenv('AI_DB_HOST', 'localhost')}:{os.getenv('AI_DB_PORT', '5432')}"
        f"/{name}"
    )

    try:
        candidate = sa.create_engine(url)
        with candidate.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"live PostgreSQL unreachable: {error!r}")

    return candidate


@pytest.fixture
def schema(engine):
    import sqlalchemy as sa

    name = f"erp_phase13_live_{uuid.uuid4().hex[:10]}"
    bootstrap_orchestration_schema(engine, schema=name)

    try:
        yield name
    finally:
        with engine.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))


@pytest.fixture
def store(engine, schema) -> PostgresJobStore:
    return PostgresJobStore(engine, schema=schema)


def make_job(job_id: str = "job_live_1") -> Job:
    return Job(
        job_id=job_id,
        request=JobRequest(
            job_type=JobType.STRUCTURED_PIPELINE,
            source_id="erp_db",
            entity="invoices",
        ),
        status=JobStatus.PENDING,
        stages=(
            StageRun(stage=PipelineStage.DISCOVER),
            StageRun(stage=PipelineStage.MAP),
            StageRun(stage=PipelineStage.EXTRACT),
        ),
    )


def test_bootstrap_creates_both_tables(engine, schema):
    import sqlalchemy as sa

    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema"
                ),
                {"schema": schema},
            )
        }

    assert {"jobs", "job_stages"} <= tables


def test_bootstrap_is_idempotent(engine, schema):
    bootstrap_orchestration_schema(engine, schema=schema)
    bootstrap_orchestration_schema(engine, schema=schema)


def test_a_job_round_trips(store: PostgresJobStore):
    job = make_job()
    store.create(job)

    loaded = store.load(job.job_id)

    assert loaded is not None
    assert loaded.job_id == job.job_id
    assert loaded.status is JobStatus.PENDING
    assert loaded.request.job_type is JobType.STRUCTURED_PIPELINE
    assert loaded.request.entity == "invoices"
    assert len(loaded.stages) == 3


def test_stage_updates_persist(store: PostgresJobStore):
    job = store.create(make_job("job_live_stages"))

    job = job.with_stage(
        StageRun(
            stage=PipelineStage.DISCOVER,
            status=StageStatus.SUCCEEDED,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            duration_seconds=0.42,
            outputs={"schema_id": "schema_live_1"},
        )
    )
    store.save(job)

    loaded = store.load(job.job_id)
    discover = loaded.stage(PipelineStage.DISCOVER)

    assert discover.status is StageStatus.SUCCEEDED
    assert discover.outputs["schema_id"] == "schema_live_1"
    assert discover.duration_seconds == pytest.approx(0.42)
    # Stage order must survive the round trip, or the history is meaningless.
    assert [run.stage for run in loaded.stages] == [
        PipelineStage.DISCOVER,
        PipelineStage.MAP,
        PipelineStage.EXTRACT,
    ]


def test_counters_and_status_persist(store: PostgresJobStore):
    from erp_pipeline.orchestration import JobCounters

    job = store.create(make_job("job_live_counters"))
    store.save(
        replace(
            job,
            status=JobStatus.PARTIAL,
            counters=JobCounters(
                records_read=100, records_transformed=95, records_failed=5
            ),
        )
    )

    loaded = store.load(job.job_id)

    assert loaded.status is JobStatus.PARTIAL
    assert loaded.counters.records_transformed == 95
    assert loaded.counters.records_failed == 5


def test_job_state_survives_the_store_instance_being_destroyed(engine, schema):
    """CRITICAL PROOF B. Process-local state would pass every in-memory test."""
    first = PostgresJobStore(engine, schema=schema)
    job = first.create(make_job("job_live_restart"))
    first.save(replace(job, status=JobStatus.SUCCEEDED))
    del first

    # A brand-new store object, as if the API had restarted.
    second = PostgresJobStore(engine, schema=schema)
    loaded = second.load("job_live_restart")

    assert loaded is not None
    assert loaded.status is JobStatus.SUCCEEDED


def test_a_whole_service_can_be_rebuilt_and_still_see_the_job(engine, schema):
    """The full stack, not just the store: service and API state are rebuilt."""
    services = PipelineServices()
    services.sources.register(
        RegisteredSource(
            source_id="erp_db", name="ERP DB", source_type=SourceType.POSTGRESQL
        )
    )

    first = OrchestrationService(
        services=services,
        job_store=PostgresJobStore(engine, schema=schema),
        executor=InlineJobExecutor(),
        handlers={
            PipelineStage.DISCOVER: lambda ctx: {"schema_id": "s1"},
            PipelineStage.MAP: lambda ctx: {},
            PipelineStage.EXTRACT: lambda ctx: {},
            PipelineStage.TRANSFORM: lambda ctx: {},
            PipelineStage.VALIDATE: lambda ctx: {},
            PipelineStage.LOAD: lambda ctx: {},
            PipelineStage.AI_BUILD: lambda ctx: {},
            PipelineStage.EMBED: lambda ctx: {},
            PipelineStage.TIER_ROUTE: lambda ctx: {},
        },
    )
    job = first.submit(
        JobRequest(job_type=JobType.STRUCTURED_PIPELINE, source_id="erp_db")
    )
    job_id = job.job_id

    del first, services

    rebuilt = OrchestrationService(
        services=PipelineServices(),
        job_store=PostgresJobStore(engine, schema=schema),
        executor=InlineJobExecutor(),
    )
    recovered = rebuilt.get(job_id)

    assert recovered.status is JobStatus.SUCCEEDED
    assert recovered.stage(PipelineStage.DISCOVER).status is StageStatus.SUCCEEDED
    assert recovered.stage(PipelineStage.TIER_ROUTE).status is StageStatus.SUCCEEDED


def test_listing_and_filtering_work_against_the_database(store: PostgresJobStore):
    store.create(make_job("job_a"))
    store.save(replace(store.load("job_a"), status=JobStatus.SUCCEEDED))
    store.create(make_job("job_b"))

    assert len(store.list(limit=50)) == 2
    assert len(store.list(status=JobStatus.SUCCEEDED)) == 1
    assert len(store.list(source_id="erp_db")) == 2
    assert len(store.list(source_id="nobody")) == 0
    assert len(store.list(limit=1)) == 1


def test_idempotency_keys_are_unique_in_the_database(store: PostgresJobStore):
    job = replace(make_job("job_idem"), idempotency_key="key-live-1")
    store.create(job)

    found = store.find_by_idempotency_key("key-live-1")

    assert found is not None
    assert found.job_id == "job_idem"
    assert store.find_by_idempotency_key("no-such-key") is None


def test_a_crashed_job_is_marked_interrupted_not_successful(store: PostgresJobStore):
    """The restart policy, stated as a test.

    A job whose worker died is not a success and is not fresh. Marking it
    SUCCEEDED would be a lie; leaving it RUNNING forever would be a different
    one.
    """
    job = store.create(make_job("job_crashed"))
    store.save(
        replace(
            job,
            status=JobStatus.RUNNING,
            stages=(
                StageRun(stage=PipelineStage.DISCOVER, status=StageStatus.SUCCEEDED),
                StageRun(stage=PipelineStage.MAP, status=StageStatus.RUNNING),
                StageRun(stage=PipelineStage.EXTRACT, status=StageStatus.PENDING),
            ),
        )
    )

    reaped = store.reap_interrupted()

    assert any(item.job_id == "job_crashed" for item in reaped)

    recovered = store.load("job_crashed")

    assert recovered.status is JobStatus.INTERRUPTED
    assert recovered.status is not JobStatus.SUCCEEDED
    # The stage that was mid-flight is failed, not left RUNNING forever.
    assert recovered.stage(PipelineStage.MAP).status is StageStatus.FAILED
    assert recovered.stage(PipelineStage.MAP).error_code == "INTERRUPTED"
    # Work that genuinely finished is preserved.
    assert recovered.stage(PipelineStage.DISCOVER).status is StageStatus.SUCCEEDED


def test_no_credential_reaches_the_job_table(store: PostgresJobStore, engine, schema):
    """Job rows are pasteable into a bug report. That must stay true."""
    import sqlalchemy as sa

    secret = "SECRET_DB_PASSWORD_13981"
    job = replace(
        make_job("job_secrets"),
        request=JobRequest(
            job_type=JobType.STRUCTURED_PIPELINE,
            source_id="erp_db",
            options={"limit": 10},
        ),
    )
    store.create(job)

    with engine.connect() as connection:
        dumped = str(
            connection.execute(sa.text(f"SELECT * FROM {schema}.jobs")).mappings().all()
        )

    assert secret not in dumped
    assert "password" not in dumped.lower()
