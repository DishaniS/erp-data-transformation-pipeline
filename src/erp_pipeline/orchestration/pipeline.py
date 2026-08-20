"""The stage runners. Each one calls an existing phase service and reports.

THE RULE THIS FILE OBEYS
------------------------
Every stage is a thin adapter: gather inputs, call the phase that owns the
work, record what happened. There is no discovery logic, no mapping scoring, no
transformation, no validator, no tokenizer, no tier decision here. If a stage
looks like it is computing something, that is a bug.

WHY STAGES STOP
---------------
When a stage fails, the remaining stages are marked SKIPPED rather than run.
A pipeline that carried on past a failed TRANSFORM would embed and index
records that were never successfully transformed - which is worse than
stopping, because it looks like it worked.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from erp_pipeline.orchestration.errors import (
    InvalidPipelineRequestError,
    MappingNotExecutableError,
    OrchestrationError,
)
from erp_pipeline.orchestration.models import (
    Job,
    JobCounters,
    JobStatus,
    PipelineStage,
    StageRun,
    StageStatus,
)
from erp_pipeline.orchestration.planner import PipelinePlan

LOGGER = logging.getLogger("erp_pipeline.orchestration.pipeline")


@dataclass
class PipelineContext:
    """Values passed between stages of one job.

    Deliberately holds live objects (schema, mapping profile, records) that are
    never persisted. Only identifiers and counts reach the job row.
    """

    job: Job
    plan: PipelinePlan
    services: Any
    schema: Any = None
    mapping_profile: Any = None
    mapping_result: Any = None
    source_records: tuple[Any, ...] = ()
    canonical_records: tuple[Any, ...] = ()
    representations: tuple[Any, ...] = ()
    embeddings: tuple[Any, ...] = ()
    document: Any = None
    #: Phase 10's SyncRunSummary, when this is an incremental job.
    sync_summary: Any = None
    counters: JobCounters = field(default_factory=JobCounters)
    outputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Set when a stage completes but the outcome is not a clean success.
    partial_reasons: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


StageHandler = Callable[[PipelineContext], Mapping[str, Any]]


class StageFailure(OrchestrationError):
    """Raised by a stage handler to fail its stage with a stable code."""

    code = "STAGE_FAILED"

    def __init__(self, message: str, code: str | None = None, **detail: Any) -> None:
        super().__init__(message, **detail)

        if code:
            self.code = code


class PipelineRunner:
    """Executes a plan's stages in order, halting the rest on failure."""

    def __init__(self, handlers: Mapping[PipelineStage, StageHandler]) -> None:
        self._handlers = dict(handlers)

    def initial_stages(self, plan: PipelinePlan) -> tuple[StageRun, ...]:
        """The planned stages, plus the ones that never apply to this shape.

        Recording NOT_APPLICABLE explicitly is what lets a reader tell "this
        pipeline has no MAP stage" apart from "MAP never ran".
        """
        planned = tuple(
            StageRun(stage=stage, status=StageStatus.PENDING) for stage in plan.stages
        )
        excluded = tuple(
            StageRun(
                stage=stage,
                status=StageStatus.NOT_APPLICABLE,
                detail=plan.rationale or None,
            )
            for stage in plan.not_applicable
        )

        return planned + excluded

    def run(self, context: PipelineContext) -> Job:
        job = replace(
            context.job,
            status=JobStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            stages=self.initial_stages(context.plan),
        )
        context.job = job
        failed = False
        failure_code: str | None = None
        failure_message: str | None = None

        for stage in context.plan.stages:
            if failed:
                job = job.with_stage(
                    StageRun(
                        stage=stage,
                        status=StageStatus.SKIPPED,
                        detail="an earlier stage failed, so this stage did not run",
                    )
                )
                context.job = job
                continue

            handler = self._handlers.get(stage)

            if handler is None:
                job = job.with_stage(
                    StageRun(
                        stage=stage,
                        status=StageStatus.NOT_APPLICABLE,
                        detail="no handler is configured for this stage",
                    )
                )
                context.job = job
                continue

            started = datetime.now(timezone.utc)
            clock = time.perf_counter()

            job = job.with_stage(
                StageRun(stage=stage, status=StageStatus.RUNNING, started_at=started)
            )
            context.job = job

            try:
                outputs = handler(context) or {}
            except Exception as error:  # noqa: BLE001 - converted to stage state
                failed = True
                failure_code = getattr(error, "code", "STAGE_FAILED")
                # The message is authored by our own typed errors. An unexpected
                # exception's text could contain anything, so it is not echoed.
                failure_message = (
                    error.message
                    if isinstance(error, OrchestrationError)
                    else f"{stage.value} failed with {type(error).__name__}"
                )
                LOGGER.warning(
                    "stage failed",
                    extra={
                        "job_id": job.job_id,
                        "stage": stage.value,
                        "error_code": failure_code,
                    },
                )
                job = job.with_stage(
                    StageRun(
                        stage=stage,
                        status=StageStatus.FAILED,
                        started_at=started,
                        finished_at=datetime.now(timezone.utc),
                        duration_seconds=round(time.perf_counter() - clock, 4),
                        detail=failure_message,
                        error_code=failure_code,
                    )
                )
                context.job = job
                continue

            job = job.with_stage(
                StageRun(
                    stage=stage,
                    status=StageStatus.SUCCEEDED,
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                    duration_seconds=round(time.perf_counter() - clock, 4),
                    outputs=dict(outputs),
                )
            )
            context.job = job

        status = self._final_status(failed, context)

        return replace(
            job,
            status=status,
            finished_at=datetime.now(timezone.utc),
            counters=context.counters,
            outputs=dict(context.outputs),
            warnings=tuple(context.warnings),
            error_code=failure_code,
            error_message=failure_message,
            version=job.version + 1,
        )

    def _final_status(self, failed: bool, context: PipelineContext) -> JobStatus:
        """FAILED beats PARTIAL beats SUCCEEDED.

        PARTIAL exists so a run that dropped records cannot be reported as a
        clean success. Reporting SUCCEEDED while five records were rejected
        would hide exactly the thing an operator needs to see.
        """
        if failed:
            return JobStatus.FAILED

        if context.partial_reasons:
            return JobStatus.PARTIAL

        return JobStatus.SUCCEEDED


__all__ = [
    "PipelineContext",
    "PipelineRunner",
    "StageHandler",
    "StageFailure",
]
