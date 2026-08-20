"""Execution and data-quality contracts.

Describes what a transformation run reports and what a quality finding looks
like. No orchestrator, scheduler, retry policy or execution engine exists in
Phase 1 - these are the records such a component will emit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from erp_pipeline.schemas.enums import QualitySeverity, RunStatus
from erp_pipeline.schemas.serialization import JsonModel, utc_now
from erp_pipeline.schemas.validation import (
    ValidationError,
    optional_aware_datetime,
    optional_text,
    require_aware_datetime,
    require_identifier,
    require_metadata,
    require_non_negative_int,
    require_ordered_times,
    require_text,
    require_value_summary,
)
from erp_pipeline.version import RUN_MODEL_VERSION


@dataclass(kw_only=True)
class TransformationRun(JsonModel):
    """One execution of a mapping against a source system.

    Count validation
    ----------------
    Every count is required to be a non-negative integer. Cross-count
    arithmetic is deliberately NOT enforced - specifically, nothing here
    asserts ``transformed + failed + skipped <= read``. A single source record
    can legitimately produce several canonical records (an order with line
    items, a document with several sections), so that inequality is not
    universally true. Encoding an unsound invariant into a foundational
    contract would force later phases to lie about their own counters, which is
    worse than checking less.

    Time validation is enforced: a completion cannot precede a start, a
    finished run must record when it finished, and an unfinished run must not
    claim to have finished.
    """

    run_id: str
    source_system_id: str
    status: RunStatus = RunStatus.PENDING
    mapping_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    records_read: int = 0
    records_transformed: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    warning_count: int = 0
    error_count: int = 0
    model_version: str = RUN_MODEL_VERSION
    message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.run_id = require_identifier(self.run_id, "TransformationRun.run_id")
        self.source_system_id = require_identifier(
            self.source_system_id, "TransformationRun.source_system_id"
        )
        self.status = RunStatus.from_value(self.status)
        self.mapping_id = (
            require_identifier(self.mapping_id, "TransformationRun.mapping_id")
            if self.mapping_id is not None
            else None
        )
        self.model_version = require_text(
            self.model_version, "TransformationRun.model_version"
        )
        self.message = optional_text(self.message, "TransformationRun.message")
        self.metadata = require_metadata(self.metadata, "TransformationRun.metadata")

        self.started_at = optional_aware_datetime(
            self.started_at, "TransformationRun.started_at"
        )
        self.completed_at = optional_aware_datetime(
            self.completed_at, "TransformationRun.completed_at"
        )
        self.created_at = require_aware_datetime(
            self.created_at, "TransformationRun.created_at"
        )

        for counter in (
            "records_read",
            "records_transformed",
            "records_failed",
            "records_skipped",
            "warning_count",
            "error_count",
        ):
            setattr(
                self,
                counter,
                require_non_negative_int(
                    getattr(self, counter), f"TransformationRun.{counter}"
                ),
            )

        require_ordered_times(self.started_at, self.completed_at)

        if self.status.is_terminal and self.completed_at is None:
            raise ValidationError(
                f"TransformationRun {self.run_id!r} has terminal status "
                f"{self.status.value!r} but no completed_at. A finished run must "
                "record when it finished."
            )

        if not self.status.is_terminal and self.completed_at is not None:
            raise ValidationError(
                f"TransformationRun {self.run_id!r} has non-terminal status "
                f"{self.status.value!r} but sets completed_at. Only a finished "
                "run may record a completion time."
            )

        if self.completed_at is not None and self.started_at is None:
            raise ValidationError(
                f"TransformationRun {self.run_id!r} records completed_at without "
                "started_at."
            )

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock duration, or ``None`` while the run is unfinished."""
        if self.started_at is None or self.completed_at is None:
            return None

        return (self.completed_at - self.started_at).total_seconds()

    @property
    def is_clean(self) -> bool:
        """True when the run succeeded with no failed records and no errors."""
        return (
            self.status is RunStatus.SUCCEEDED
            and self.records_failed == 0
            and self.error_count == 0
        )


@dataclass(kw_only=True)
class DataQualityIssue(JsonModel):
    """One data-quality finding raised during a transformation run.

    Safety of ``original_value_summary``
    ------------------------------------
    A quality report must never become a second, less-protected copy of the
    source data. The excerpt is therefore optional and length-bounded, and
    ``validation.summarize_value(value, redact=True)`` produces a shape-only
    description - type and length, no content - which is what a finding on a
    confidential or restricted field should carry.
    """

    issue_id: str
    severity: QualitySeverity
    code: str
    message: str
    run_id: str | None = None
    record_id: str | None = None
    source_entity: str | None = None
    field_name: str | None = None
    original_value_summary: str | None = None
    expected: str | None = None
    model_version: str = RUN_MODEL_VERSION
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.issue_id = require_identifier(self.issue_id, "DataQualityIssue.issue_id")
        self.severity = QualitySeverity.from_value(self.severity)
        self.code = _require_issue_code(self.code)
        self.message = require_text(self.message, "DataQualityIssue.message")
        self.run_id = (
            require_identifier(self.run_id, "DataQualityIssue.run_id")
            if self.run_id is not None
            else None
        )
        self.record_id = optional_text(self.record_id, "DataQualityIssue.record_id")
        self.source_entity = optional_text(
            self.source_entity, "DataQualityIssue.source_entity"
        )
        self.field_name = optional_text(self.field_name, "DataQualityIssue.field_name")
        self.expected = optional_text(self.expected, "DataQualityIssue.expected")
        self.model_version = require_text(
            self.model_version, "DataQualityIssue.model_version"
        )
        self.created_at = require_aware_datetime(
            self.created_at, "DataQualityIssue.created_at"
        )
        self.metadata = require_metadata(self.metadata, "DataQualityIssue.metadata")

        self.original_value_summary = require_value_summary(
            self.original_value_summary, "DataQualityIssue.original_value_summary"
        )

    @property
    def is_blocking(self) -> bool:
        """True for severities that should stop a record being published."""
        return self.severity in (QualitySeverity.ERROR, QualitySeverity.CRITICAL)


def _require_issue_code(value: Any) -> str:
    """Require a stable, machine-readable issue code.

    Codes are grouped and filtered by downstream tooling, so they must be
    token-like: ``NULL_IN_REQUIRED_FIELD``, not a free sentence. The human
    explanation belongs in ``message``.
    """
    code = require_text(value, "DataQualityIssue.code")

    if any(character.isspace() for character in code):
        raise ValidationError(
            f"DataQualityIssue.code must not contain whitespace, got {code!r}. "
            "Use a token such as 'NULL_IN_REQUIRED_FIELD' and put the human "
            "explanation in message."
        )

    return code


__all__ = [
    "TransformationRun",
    "DataQualityIssue",
]
