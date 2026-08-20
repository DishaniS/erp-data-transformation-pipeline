"""Durable job state.

WHY THIS HAS TO BE PERSISTENT
-----------------------------
A job outlives the HTTP request that created it. If job state lived in process
memory, a restart would erase every record of what ran - and the honest answer
to "did that pipeline finish?" would be "nobody knows". That is the failure
this store exists to prevent.

WHAT IS NEVER STORED
--------------------
No credentials, no source rows, no document text, no vectors. A job row holds
identifiers, statuses, counts and timings. Everything persisted here is
something you could paste into a bug report.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from erp_pipeline.orchestration.errors import JobNotFoundError
from erp_pipeline.orchestration.models import (
    Job,
    JobCounters,
    JobRequest,
    JobStatus,
    JobType,
    PipelineStage,
    StageRun,
    StageStatus,
)

ORCHESTRATION_SCHEMA_NAME = "erp_orchestration"
JOBS_TABLE = "jobs"
STAGES_TABLE = "job_stages"


@runtime_checkable
class JobStore(Protocol):
    """The contract both implementations honour."""

    def create(self, job: Job) -> Job: ...

    def load(self, job_id: str) -> Job | None: ...

    def save(self, job: Job) -> Job: ...

    def list(
        self,
        status: JobStatus | None = None,
        job_type: JobType | None = None,
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Job, ...]: ...

    def find_by_idempotency_key(self, key: str) -> Job | None: ...

    def reap_interrupted(self) -> tuple[Job, ...]: ...


class InMemoryJobStore:
    """For unit tests and for running the API with no database configured."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self, job: Job) -> Job:
        self._jobs[job.job_id] = job
        return job

    def load(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def save(self, job: Job) -> Job:
        self._jobs[job.job_id] = job
        return job

    def list(
        self,
        status: JobStatus | None = None,
        job_type: JobType | None = None,
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Job, ...]:
        found = sorted(
            self._jobs.values(), key=lambda job: job.created_at, reverse=True
        )

        if status is not None:
            found = [job for job in found if job.status is status]

        if job_type is not None:
            found = [job for job in found if job.request.job_type is job_type]

        if source_id is not None:
            found = [job for job in found if job.request.source_id == source_id]

        return tuple(found[offset : offset + limit])

    def find_by_idempotency_key(self, key: str) -> Job | None:
        for job in self._jobs.values():
            if job.idempotency_key == key:
                return job

        return None

    def reap_interrupted(self) -> tuple[Job, ...]:
        """In-memory state dies with the process, so nothing can be stranded."""
        return ()


# ----------------------------------------------------------------------
# PostgreSQL
# ----------------------------------------------------------------------


def _validate_schema(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        raise ValueError(f"{schema!r} is not a valid PostgreSQL schema name")

    return schema


def create_jobs_sql(schema: str = ORCHESTRATION_SCHEMA_NAME) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{JOBS_TABLE} (
    job_id               TEXT        PRIMARY KEY,
    job_type             TEXT        NOT NULL,
    status               TEXT        NOT NULL,
    source_id            TEXT        NULL,
    schema_id            TEXT        NULL,
    mapping_id           TEXT        NULL,
    upload_id            TEXT        NULL,
    entity               TEXT        NULL,
    options_json         TEXT        NOT NULL DEFAULT '{{}}',
    counters_json        TEXT        NOT NULL DEFAULT '{{}}',
    outputs_json         TEXT        NOT NULL DEFAULT '{{}}',
    warnings_json        TEXT        NOT NULL DEFAULT '[]',
    error_code           TEXT        NULL,
    error_message        TEXT        NULL,
    idempotency_key      TEXT        NULL,
    request_fingerprint  TEXT        NULL,
    engine_version       TEXT        NOT NULL,
    job_version          INTEGER     NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL,
    started_at           TIMESTAMPTZ NULL,
    finished_at          TIMESTAMPTZ NULL
)
"""


def create_stages_sql(schema: str = ORCHESTRATION_SCHEMA_NAME) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{STAGES_TABLE} (
    job_id            TEXT        NOT NULL,
    stage             TEXT        NOT NULL,
    stage_order       INTEGER     NOT NULL,
    status            TEXT        NOT NULL,
    started_at        TIMESTAMPTZ NULL,
    finished_at       TIMESTAMPTZ NULL,
    duration_seconds  DOUBLE PRECISION NULL,
    detail            TEXT        NULL,
    error_code        TEXT        NULL,
    outputs_json      TEXT        NOT NULL DEFAULT '{{}}',
    warnings_json     TEXT        NOT NULL DEFAULT '[]',
    PRIMARY KEY (job_id, stage)
)
"""


def create_idempotency_index_sql(schema: str = ORCHESTRATION_SCHEMA_NAME) -> str:
    return (
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{JOBS_TABLE}_idempotency "
        f"ON {_validate_schema(schema)}.{JOBS_TABLE} (idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


def bootstrap_orchestration_schema(
    engine: Any, schema: str = ORCHESTRATION_SCHEMA_NAME
) -> None:
    """Create the namespace and its tables. Idempotent.

    ``schema`` is parameterised so a test can use a throwaway namespace instead
    of creating production tables as a side effect of being verified.
    """
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {_validate_schema(schema)}")
        )
        connection.execute(text(create_jobs_sql(schema)))
        connection.execute(text(create_stages_sql(schema)))
        connection.execute(text(create_idempotency_index_sql(schema)))


class PostgresJobStore:
    """Job state that survives a restart, because that is the whole point."""

    def __init__(
        self, engine: Any, schema: str = ORCHESTRATION_SCHEMA_NAME
    ) -> None:
        self._engine = engine
        self._schema = _validate_schema(schema)

    @property
    def schema(self) -> str:
        return self._schema

    # -- writes --

    def create(self, job: Job) -> Job:
        return self.save(job)

    def save(self, job: Job) -> Job:
        from sqlalchemy import text

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{JOBS_TABLE} (
                        job_id, job_type, status, source_id, schema_id,
                        mapping_id, upload_id, entity, options_json,
                        counters_json, outputs_json, warnings_json,
                        error_code, error_message, idempotency_key,
                        request_fingerprint, engine_version, job_version,
                        created_at, started_at, finished_at
                    ) VALUES (
                        :job_id, :job_type, :status, :source_id, :schema_id,
                        :mapping_id, :upload_id, :entity, :options_json,
                        :counters_json, :outputs_json, :warnings_json,
                        :error_code, :error_message, :idempotency_key,
                        :request_fingerprint, :engine_version, :job_version,
                        :created_at, :started_at, :finished_at
                    )
                    ON CONFLICT (job_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        schema_id = EXCLUDED.schema_id,
                        mapping_id = EXCLUDED.mapping_id,
                        counters_json = EXCLUDED.counters_json,
                        outputs_json = EXCLUDED.outputs_json,
                        warnings_json = EXCLUDED.warnings_json,
                        error_code = EXCLUDED.error_code,
                        error_message = EXCLUDED.error_message,
                        job_version = EXCLUDED.job_version,
                        started_at = EXCLUDED.started_at,
                        finished_at = EXCLUDED.finished_at
                    """
                ),
                {
                    "job_id": job.job_id,
                    "job_type": job.request.job_type.value,
                    "status": job.status.value,
                    "source_id": job.request.source_id,
                    "schema_id": job.request.schema_id,
                    "mapping_id": job.request.mapping_id,
                    "upload_id": job.request.upload_id,
                    "entity": job.request.entity,
                    "options_json": json.dumps(dict(job.request.options), default=str),
                    "counters_json": json.dumps(job.counters.to_dict()),
                    "outputs_json": json.dumps(dict(job.outputs), default=str),
                    "warnings_json": json.dumps(list(job.warnings)),
                    "error_code": job.error_code,
                    "error_message": job.error_message,
                    "idempotency_key": job.idempotency_key,
                    "request_fingerprint": job.request_fingerprint,
                    "engine_version": job.engine_version,
                    "job_version": job.version,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                },
            )

            for order, run in enumerate(job.stages):
                connection.execute(
                    text(
                        f"""
                        INSERT INTO {self._schema}.{STAGES_TABLE} (
                            job_id, stage, stage_order, status, started_at,
                            finished_at, duration_seconds, detail, error_code,
                            outputs_json, warnings_json
                        ) VALUES (
                            :job_id, :stage, :stage_order, :status, :started_at,
                            :finished_at, :duration_seconds, :detail, :error_code,
                            :outputs_json, :warnings_json
                        )
                        ON CONFLICT (job_id, stage) DO UPDATE SET
                            stage_order = EXCLUDED.stage_order,
                            status = EXCLUDED.status,
                            started_at = EXCLUDED.started_at,
                            finished_at = EXCLUDED.finished_at,
                            duration_seconds = EXCLUDED.duration_seconds,
                            detail = EXCLUDED.detail,
                            error_code = EXCLUDED.error_code,
                            outputs_json = EXCLUDED.outputs_json,
                            warnings_json = EXCLUDED.warnings_json
                        """
                    ),
                    {
                        "job_id": job.job_id,
                        "stage": run.stage.value,
                        "stage_order": order,
                        "status": run.status.value,
                        "started_at": run.started_at,
                        "finished_at": run.finished_at,
                        "duration_seconds": run.duration_seconds,
                        "detail": run.detail,
                        "error_code": run.error_code,
                        "outputs_json": json.dumps(dict(run.outputs), default=str),
                        "warnings_json": json.dumps(list(run.warnings)),
                    },
                )

        return job

    # -- reads --

    def load(self, job_id: str) -> Job | None:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{JOBS_TABLE} "
                    "WHERE job_id = :job_id"
                ),
                {"job_id": job_id},
            ).mappings().first()

            if row is None:
                return None

            stages = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{STAGES_TABLE} "
                    "WHERE job_id = :job_id ORDER BY stage_order"
                ),
                {"job_id": job_id},
            ).mappings().all()

        return _row_to_job(row, stages)

    def list(
        self,
        status: JobStatus | None = None,
        job_type: JobType | None = None,
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Job, ...]:
        from sqlalchemy import text

        clauses = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if status is not None:
            clauses.append("status = :status")
            params["status"] = status.value

        if job_type is not None:
            clauses.append("job_type = :job_type")
            params["job_type"] = job_type.value

        if source_id is not None:
            clauses.append("source_id = :source_id")
            params["source_id"] = source_id

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{JOBS_TABLE} {where} "
                    "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            ).mappings().all()

            jobs = []

            for row in rows:
                stages = connection.execute(
                    text(
                        f"SELECT * FROM {self._schema}.{STAGES_TABLE} "
                        "WHERE job_id = :job_id ORDER BY stage_order"
                    ),
                    {"job_id": row["job_id"]},
                ).mappings().all()
                jobs.append(_row_to_job(row, stages))

        return tuple(jobs)

    def find_by_idempotency_key(self, key: str) -> Job | None:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT job_id FROM {self._schema}.{JOBS_TABLE} "
                    "WHERE idempotency_key = :key"
                ),
                {"key": key},
            ).mappings().first()

        return self.load(row["job_id"]) if row else None

    def reap_interrupted(self) -> tuple[Job, ...]:
        """Mark jobs left RUNNING by a dead process as INTERRUPTED.

        A crashed job is not a successful job and it is not a fresh one. It is
        marked explicitly so an operator can see that it stopped mid-flight,
        and its RUNNING stage is marked FAILED rather than left mid-run.
        """
        from sqlalchemy import text

        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT job_id FROM {self._schema}.{JOBS_TABLE} "
                    "WHERE status IN ('running', 'pending')"
                )
            ).mappings().all()

        reaped = []

        for row in rows:
            job = self.load(row["job_id"])

            if job is None:
                continue

            stages = tuple(
                (
                    StageRun(
                        stage=run.stage,
                        status=StageStatus.FAILED,
                        started_at=run.started_at,
                        finished_at=datetime.now(timezone.utc),
                        detail="the worker process stopped before this stage finished",
                        error_code="INTERRUPTED",
                        outputs=run.outputs,
                        warnings=run.warnings,
                    )
                    if run.status is StageStatus.RUNNING
                    else run
                )
                for run in job.stages
            )

            from dataclasses import replace

            reaped.append(
                self.save(
                    replace(
                        job,
                        status=JobStatus.INTERRUPTED,
                        stages=stages,
                        finished_at=datetime.now(timezone.utc),
                        error_code="INTERRUPTED",
                        error_message=(
                            "the worker process stopped before this job "
                            "finished; its outcome is unknown and it was not "
                            "assumed successful"
                        ),
                        version=job.version + 1,
                    )
                )
            )

        return tuple(reaped)


def _row_to_job(row: Mapping[str, Any], stage_rows: Sequence[Mapping[str, Any]]) -> Job:
    request = JobRequest(
        job_type=JobType(row["job_type"]),
        source_id=row["source_id"],
        schema_id=row["schema_id"],
        mapping_id=row["mapping_id"],
        upload_id=row["upload_id"],
        entity=row["entity"],
        options=json.loads(row["options_json"] or "{}"),
    )

    stages = tuple(
        StageRun(
            stage=PipelineStage(stage["stage"]),
            status=StageStatus(stage["status"]),
            started_at=stage["started_at"],
            finished_at=stage["finished_at"],
            duration_seconds=stage["duration_seconds"],
            detail=stage["detail"],
            error_code=stage["error_code"],
            outputs=json.loads(stage["outputs_json"] or "{}"),
            warnings=tuple(json.loads(stage["warnings_json"] or "[]")),
        )
        for stage in stage_rows
    )

    return Job(
        job_id=row["job_id"],
        request=request,
        status=JobStatus(row["status"]),
        stages=stages,
        counters=JobCounters(**json.loads(row["counters_json"] or "{}")),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        warnings=tuple(json.loads(row["warnings_json"] or "[]")),
        outputs=json.loads(row["outputs_json"] or "{}"),
        idempotency_key=row["idempotency_key"],
        request_fingerprint=row["request_fingerprint"],
        engine_version=row["engine_version"],
        version=row["job_version"],
    )


__all__ = [
    "ORCHESTRATION_SCHEMA_NAME",
    "JOBS_TABLE",
    "STAGES_TABLE",
    "JobStore",
    "InMemoryJobStore",
    "PostgresJobStore",
    "bootstrap_orchestration_schema",
    "create_jobs_sql",
    "create_stages_sql",
]
