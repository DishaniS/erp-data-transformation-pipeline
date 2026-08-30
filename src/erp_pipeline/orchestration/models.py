"""Job, stage and counter models.

WHY ENUMS AND NOT STRINGS
-------------------------
Job and stage status travel from the executor, through PostgreSQL, into a JSON
response, and a typo in any of those hops would produce a job that silently
never matches a filter. Enums make that a load-time error instead.

WHY COUNTERS ARE OPTIONAL
-------------------------
Every counter here is only ever set from a value an existing phase service
actually returned. A count that Phase 9 or Phase 11 did not report is left
absent rather than defaulted to zero, because a confident ``0`` is a claim and
``None`` is the truth.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

ORCHESTRATION_ENGINE_VERSION = "1.0"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    #: A process died mid-run. Never silently promoted to succeeded.
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.PARTIAL,
            JobStatus.INTERRUPTED,
        }


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Planned, but not run because an earlier stage failed.
    SKIPPED = "skipped"
    #: Never applicable to this pipeline shape (documents have no MAP stage).
    NOT_APPLICABLE = "not_applicable"


class PipelineStage(str, Enum):
    """Every stage any pipeline mode can contain."""

    # structured
    DISCOVER = "discover"
    MAP = "map"
    #: Decides whether a source-native job is ALLOWED to proceed. Distinct from
    #: MAP because it produces no profile - it produces an admission decision.
    SOURCE_NATIVE_GUARD = "source_native_guard"
    #: Opens the binary fields the source row carried. Placed AFTER AI_BUILD
    #: because that is the first point where BOTH the raw bytes and a stable
    #: parent record id exist at once, and because AI_BUILD assigns
    #: `representations` wholesale - running earlier would have it overwritten.
    MULTIMODAL_EXTRACT = "multimodal_extract"
    PERSIST_REPRESENTATIONS = "persist_representations"
    #: Phase 9. Runs LAST, after the new version is safely stored, so a failure
    #: anywhere earlier leaves the previous version current and searchable.
    LIFECYCLE_COMMIT = "lifecycle_commit"
    EXTRACT = "extract"
    TRANSFORM = "transform"
    VALIDATE = "validate"
    LOAD = "load"
    AI_BUILD = "ai_build"
    EMBED = "embed"
    TIER_ROUTE = "tier_route"

    # incremental
    DRIFT_CHECK = "drift_check"
    EXTRACT_CHANGED = "extract_changed"
    TIER_UPDATE = "tier_update"

    # document
    INGEST = "ingest"

    # api specification
    PARSE_SPEC = "parse_spec"
    SCHEMA = "schema"


class JobType(str, Enum):
    """What the caller asked for. Determines the stage graph."""

    STRUCTURED_PIPELINE = "structured_pipeline"
    DOCUMENT_PIPELINE = "document_pipeline"
    INCREMENTAL_SYNC = "incremental_sync"
    DRIFT_CHECK = "drift_check"
    API_SPEC_PREPARATION = "api_spec_preparation"
    #: An ERP entity the canonical model does not cover, indexed on its own
    #: terms. Its own job type rather than a flag on the structured pipeline,
    #: so a caller cannot arrive here by accident: choosing it is a statement
    #: that no canonical vocabulary applies, and the guard stage refuses the
    #: job outright if one does. That is what keeps it from becoming a way
    #: around an ambiguous mapping that a human is supposed to resolve.
    SOURCE_NATIVE_PIPELINE = "source_native_pipeline"
    #: Phase 7. Its input is a SourceSchema rather than rows or a document, so
    #: it is its own job type rather than an option on an existing one - but it
    #: runs the same PERSIST -> EMBED -> TIER_ROUTE tail as everything else.
    SCHEMA_PIPELINE = "schema_pipeline"


@dataclass(frozen=True)
class JobCounters:
    """Transparent per-job counts.

    ``None`` means the underlying service did not report the number. It is
    deliberately distinguishable from ``0``.
    """

    records_read: int | None = None
    records_transformed: int | None = None
    records_failed: int | None = None
    records_skipped: int | None = None
    representations_built: int | None = None
    embeddings_generated: int | None = None
    embeddings_skipped: int | None = None
    vectors_stored: int | None = None
    vectors_failed: int | None = None
    documents_ingested: int | None = None
    chunks_built: int | None = None
    #: Phase 3 multimodal counts. Optional like every other counter, so a job
    #: that never saw a BLOB reports None rather than a misleading zero.
    binary_fields_seen: int | None = None
    binary_assets_extracted: int | None = None
    binary_assets_skipped: int | None = None
    ocr_assets: int | None = None
    #: Phase 8: declared remote references this run attempted, permitted or not.
    remote_assets_attempted: int | None = None
    #: Phase 9 lifecycle outcomes.
    slots_promoted: int | None = None
    representations_superseded: int | None = None
    stale_vectors_removed: int | None = None
    stale_cleanup_deferred: int | None = None
    #: Phase 5: AI text written to durable storage, so a search hit can be
    #: resolved back to what it actually says.
    representations_persisted: int | None = None
    #: Phase 7: schema entities turned into searchable structure.
    schema_entities_indexed: int | None = None
    #: Representations removed because the entity shrank and no longer needs
    #: that many field groups.
    schema_representations_pruned: int | None = None
    operations_parsed: int | None = None

    def merged(self, **updates: int | None) -> "JobCounters":
        return replace(
            self, **{k: v for k, v in updates.items() if v is not None}
        )

    def to_dict(self) -> dict[str, int]:
        return {
            key: value
            for key, value in self.__dict__.items()
            if value is not None
        }


@dataclass(frozen=True)
class StageRun:
    """One stage's outcome."""

    stage: PipelineStage
    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    detail: str | None = None
    error_code: str | None = None
    #: Identifiers produced by this stage (schema_id, mapping_id, ...). Never
    #: business values.
    outputs: Mapping[str, Any] = field(default_factory=dict)
    warnings: Sequence[str] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "detail": self.detail,
            "error_code": self.error_code,
            "outputs": dict(self.outputs),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class JobRequest:
    """The validated, credential-free description of what to run.

    Nothing secret may live here: this object is persisted to PostgreSQL and
    echoed back through the API. Credentials are referenced by name through a
    ``SecretProvider`` and resolved at execution time only.
    """

    job_type: JobType
    source_id: str | None = None
    schema_id: str | None = None
    mapping_id: str | None = None
    upload_id: str | None = None
    entity: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """A stable hash of the request, used to police idempotency keys.

        Reusing a key with a different payload must be a conflict rather than a
        silent no-op, so the payload has to be comparable.
        """
        import hashlib
        import json

        payload = json.dumps(
            {
                "job_type": self.job_type.value,
                "source_id": self.source_id,
                "schema_id": self.schema_id,
                "mapping_id": self.mapping_id,
                "upload_id": self.upload_id,
                "entity": self.entity,
                "options": self.options,
            },
            sort_keys=True,
            default=str,
        )

        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_type": self.job_type.value,
            "source_id": self.source_id,
            "schema_id": self.schema_id,
            "mapping_id": self.mapping_id,
            "upload_id": self.upload_id,
            "entity": self.entity,
            "options": dict(self.options),
        }


def new_job_id() -> str:
    """Operational identity only.

    A random UUID is right here precisely because a job is an operational
    event, not an ERP record. Domain identity everywhere else in this pipeline
    is deterministic; this deliberately is not, and must never be used as a
    record key.
    """
    return f"job_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Job:
    """A job and its stage history."""

    job_id: str
    request: JobRequest
    status: JobStatus = JobStatus.PENDING
    stages: tuple[StageRun, ...] = ()
    counters: JobCounters = field(default_factory=JobCounters)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    warnings: Sequence[str] = ()
    outputs: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    request_fingerprint: str | None = None
    engine_version: str = ORCHESTRATION_ENGINE_VERSION
    version: int = 0

    @property
    def current_stage(self) -> PipelineStage | None:
        for run in self.stages:
            if run.status is StageStatus.RUNNING:
                return run.stage

        return None

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None

        end = self.finished_at or datetime.now(timezone.utc)

        return round((end - self.started_at).total_seconds(), 4)

    def with_stage(self, run: StageRun) -> "Job":
        """Replace one stage's record, preserving planned order."""
        stages = tuple(
            run if existing.stage is run.stage else existing
            for existing in self.stages
        )

        return replace(self, stages=stages, version=self.version + 1)

    def stage(self, stage: PipelineStage) -> StageRun | None:
        for run in self.stages:
            if run.stage is stage:
                return run

        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "job_type": self.request.job_type.value,
            "request": self.request.to_dict(),
            "current_stage": (
                self.current_stage.value if self.current_stage else None
            ),
            "stages": [run.to_dict() for run in self.stages],
            "counters": self.counters.to_dict(),
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
            "duration_seconds": self.duration_seconds,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "warnings": list(self.warnings),
            "outputs": dict(self.outputs),
            "engine_version": self.engine_version,
            "version": self.version,
        }


__all__ = [
    "ORCHESTRATION_ENGINE_VERSION",
    "JobStatus",
    "StageStatus",
    "PipelineStage",
    "JobType",
    "JobCounters",
    "StageRun",
    "JobRequest",
    "Job",
    "new_job_id",
]
