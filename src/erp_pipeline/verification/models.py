"""Contracts for cross-store integrity verification.

WHY A DEDICATED VOCABULARY
--------------------------
Integrity findings are an interface, not log output. A dashboard groups by
them, a test pins them, and a report from one run must be comparable with a
report from another. So the codes are declared once, here, and never derived
from an exception class name or a message string - the same rule the
transformation layer's ``IssueCode`` follows, for the same reason.

WHAT A FINDING MAY CONTAIN
--------------------------
Identity, a code, and a bounded diagnostic detail. Never a business value, and
never a vector: a report that quoted the data it was checking would become a
second, unprotected copy of the corpus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

#: Version of the verification contracts.
VERIFICATION_ENGINE_VERSION = "1.0"


class IntegrityCode(str, Enum):
    """Stable, machine-readable integrity findings."""

    # --- identity ---
    MALFORMED_RECORD_ID = "MALFORMED_RECORD_ID"
    SURROGATE_KEY_IDENTITY = "SURROGATE_KEY_IDENTITY"
    DUPLICATE_RECORD_ID = "DUPLICATE_RECORD_ID"

    # --- presence ---
    CANONICAL_RECORD_MISSING = "CANONICAL_RECORD_MISSING"
    REPRESENTATION_MISSING = "REPRESENTATION_MISSING"
    EMBEDDING_MISSING = "EMBEDDING_MISSING"
    VECTOR_MISSING = "VECTOR_MISSING"

    # --- agreement between layers ---
    CONTENT_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"
    CANONICAL_REFERENCE_MISMATCH = "CANONICAL_REFERENCE_MISMATCH"
    MODEL_ID_MISMATCH = "MODEL_ID_MISMATCH"
    DIMENSION_MISMATCH = "DIMENSION_MISMATCH"
    VECTOR_ID_MISMATCH = "VECTOR_ID_MISMATCH"
    TIER_METADATA_MISMATCH = "TIER_METADATA_MISMATCH"
    ENTITY_TYPE_MISMATCH = "ENTITY_TYPE_MISMATCH"

    # --- orphans ---
    ORPHANED_VECTOR = "ORPHANED_VECTOR"
    ORPHANED_TIER_STATE = "ORPHANED_TIER_STATE"

    # --- embedding state ---
    EMBEDDING_NOT_GENERATED = "EMBEDDING_NOT_GENERATED"
    EMBEDDING_STALE = "EMBEDDING_STALE"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class IntegritySeverity(str, Enum):
    """How much a finding matters.

    ``FAILURE`` means the stores genuinely disagree and a consumer would get a
    wrong answer. ``WARNING`` means something is worth investigating but has a
    legitimate explanation - a record embedded but not yet stored, for example,
    is normal mid-run.
    """

    FAILURE = "failure"
    WARNING = "warning"
    INFO = "info"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Default severity per code. A caller may override per run, but the defaults
#: encode the distinction above rather than treating every finding as fatal.
DEFAULT_SEVERITY: Mapping[IntegrityCode, IntegritySeverity] = {
    IntegrityCode.MALFORMED_RECORD_ID: IntegritySeverity.FAILURE,
    IntegrityCode.SURROGATE_KEY_IDENTITY: IntegritySeverity.FAILURE,
    IntegrityCode.DUPLICATE_RECORD_ID: IntegritySeverity.FAILURE,
    IntegrityCode.CANONICAL_RECORD_MISSING: IntegritySeverity.FAILURE,
    IntegrityCode.REPRESENTATION_MISSING: IntegritySeverity.FAILURE,
    IntegrityCode.EMBEDDING_MISSING: IntegritySeverity.WARNING,
    IntegrityCode.VECTOR_MISSING: IntegritySeverity.FAILURE,
    IntegrityCode.CONTENT_HASH_MISMATCH: IntegritySeverity.FAILURE,
    IntegrityCode.CANONICAL_REFERENCE_MISMATCH: IntegritySeverity.FAILURE,
    IntegrityCode.MODEL_ID_MISMATCH: IntegritySeverity.FAILURE,
    IntegrityCode.DIMENSION_MISMATCH: IntegritySeverity.FAILURE,
    IntegrityCode.VECTOR_ID_MISMATCH: IntegritySeverity.FAILURE,
    IntegrityCode.TIER_METADATA_MISMATCH: IntegritySeverity.FAILURE,
    IntegrityCode.ENTITY_TYPE_MISMATCH: IntegritySeverity.WARNING,
    IntegrityCode.ORPHANED_VECTOR: IntegritySeverity.FAILURE,
    IntegrityCode.ORPHANED_TIER_STATE: IntegritySeverity.WARNING,
    IntegrityCode.EMBEDDING_NOT_GENERATED: IntegritySeverity.WARNING,
    IntegrityCode.EMBEDDING_STALE: IntegritySeverity.FAILURE,
}

#: Hard cap on a finding's diagnostic text, so a report can never grow into a
#: copy of the data it is checking.
MAX_DETAIL_LENGTH = 300


@dataclass(frozen=True)
class IntegrityIssue:
    """One disagreement between two layers."""

    code: IntegrityCode
    subject_id: str
    detail: str
    severity: IntegritySeverity = IntegritySeverity.FAILURE
    #: Structural context only - layer names, hashes, dimensions, tiers.
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.detail) > MAX_DETAIL_LENGTH:
            object.__setattr__(
                self, "detail", self.detail[: MAX_DETAIL_LENGTH - 3] + "..."
            )

    @property
    def is_failure(self) -> bool:
        return self.severity is IntegritySeverity.FAILURE

    def describe(self) -> str:
        return f"[{self.severity.value}] {self.code.value} {self.subject_id}: {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "subject_id": self.subject_id,
            "detail": self.detail,
            "context": dict(self.context),
        }


def make_issue(
    code: IntegrityCode,
    subject_id: str,
    detail: str,
    severity: IntegritySeverity | None = None,
    **context: Any,
) -> IntegrityIssue:
    """Build a finding with the code's default severity unless overridden."""
    return IntegrityIssue(
        code=code,
        subject_id=subject_id,
        detail=detail,
        severity=severity or DEFAULT_SEVERITY.get(code, IntegritySeverity.FAILURE),
        context=context,
    )


@dataclass(frozen=True)
class VerificationReport:
    """The result of one verification run.

    The verdict is DERIVED from the findings, never set by a caller. That is
    what stops a report being declared green while carrying failures.
    """

    checks_run: int = 0
    issues: tuple[IntegrityIssue, ...] = ()
    counts: Mapping[str, Any] = field(default_factory=dict)
    subjects_examined: int = 0
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    engine_version: str = VERIFICATION_ENGINE_VERSION

    @property
    def failures(self) -> tuple[IntegrityIssue, ...]:
        return tuple(issue for issue in self.issues if issue.is_failure)

    @property
    def warnings(self) -> tuple[IntegrityIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity is IntegritySeverity.WARNING
        )

    @property
    def passed(self) -> bool:
        return not self.failures

    def issues_with(self, code: IntegrityCode) -> tuple[IntegrityIssue, ...]:
        return tuple(issue for issue in self.issues if issue.code is code)

    def count_by_code(self) -> dict[str, int]:
        tally: dict[str, int] = {}

        for issue in self.issues:
            tally[issue.code.value] = tally.get(issue.code.value, 0) + 1

        return dict(sorted(tally.items()))

    def merged(self, other: "VerificationReport") -> "VerificationReport":
        """Combine two reports. Used to run several checks as one verdict."""
        return VerificationReport(
            checks_run=self.checks_run + other.checks_run,
            issues=self.issues + other.issues,
            counts={**dict(self.counts), **dict(other.counts)},
            subjects_examined=max(self.subjects_examined, other.subjects_examined),
            generated_at=self.generated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "generated_at": self.generated_at.isoformat(),
            "passed": self.passed,
            "checks_run": self.checks_run,
            "subjects_examined": self.subjects_examined,
            "failure_count": len(self.failures),
            "warning_count": len(self.warnings),
            "count_by_code": self.count_by_code(),
            "counts": dict(self.counts),
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def render(self, max_issues: int = 25) -> str:
        """A short human-readable verdict for a console or a log."""
        lines = [
            f"Cross-store integrity: {'PASS' if self.passed else 'FAIL'}",
            f"  checks run          : {self.checks_run}",
            f"  subjects examined   : {self.subjects_examined}",
            f"  failures            : {len(self.failures)}",
            f"  warnings            : {len(self.warnings)}",
        ]

        for label, value in sorted(self.counts.items()):
            lines.append(f"  {label:<20}: {value}")

        if self.issues:
            lines.append("  findings:")
            for issue in self.issues[:max_issues]:
                lines.append(f"    - {issue.describe()}")

            if len(self.issues) > max_issues:
                lines.append(
                    f"    ... and {len(self.issues) - max_issues} more"
                )

        return "\n".join(lines)


def build_report(
    issues: Sequence[IntegrityIssue],
    checks_run: int,
    subjects_examined: int = 0,
    counts: Mapping[str, Any] | None = None,
) -> VerificationReport:
    return VerificationReport(
        checks_run=checks_run,
        issues=tuple(issues),
        counts=dict(counts or {}),
        subjects_examined=subjects_examined,
    )


__all__ = [
    "VERIFICATION_ENGINE_VERSION",
    "MAX_DETAIL_LENGTH",
    "IntegrityCode",
    "IntegritySeverity",
    "DEFAULT_SEVERITY",
    "IntegrityIssue",
    "VerificationReport",
    "make_issue",
    "build_report",
]
