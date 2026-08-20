"""Construction of ``DataQualityIssue`` findings - the one place that does it.

Every issue in Phase 9 is built here. Centralizing it is what makes the privacy
and stability guarantees checkable: there is a single function to audit, rather
than fifty scattered constructor calls that each might quote a value.

PRIVACY (Steps 31, 61)
----------------------
An issue never carries a business value.

``message`` is composed from field names, type names, codes and bounds. The
offending value is not interpolated into it, and there is no code path that
does so.

``original_value_summary`` stays ``None`` unless a caller sets
``include_value_diagnostics``. Even then it is produced by
``summarize_value(..., redact=True)``, which reports only the value's TYPE and
LENGTH - ``<redacted str length=5>``. So the opt-in buys shape information for
debugging and still cannot leak content. There is deliberately no option that
emits the raw value into an issue.

STABILITY (Step 76)
-------------------
``code`` always comes from the ``IssueCode`` enum, never from an exception
class name or a formatted message, so downstream grouping is stable across
refactors.
"""

from __future__ import annotations

from typing import Any, Mapping

from erp_pipeline.schemas.enums import QualitySeverity
from erp_pipeline.schemas.identity import normalize_identifier
from erp_pipeline.schemas.run_models import DataQualityIssue
from erp_pipeline.schemas.validation import summarize_value
from erp_pipeline.transformation.models import (
    IssueCode,
    TransformationOptions,
    deterministic_suffix,
)

#: Default severity of each code. A code's severity is a property of what the
#: finding MEANS, so it lives with the vocabulary rather than at each call site
#: where it would inevitably drift.
#:
#: Nothing here is INFO-by-default except genuinely informational findings, and
#: nothing that corrupts a record is a mere warning (Step 33).
DEFAULT_SEVERITIES: Mapping[IssueCode, QualitySeverity] = {
    # A field the mapping expected but the source did not send. A warning on
    # its own: whether it matters depends on whether the target is required,
    # which the required-field check decides separately.
    IssueCode.SOURCE_FIELD_MISSING: QualitySeverity.WARNING,
    IssueCode.SOURCE_VALUE_NULL: QualitySeverity.INFO,
    IssueCode.TARGET_PATH_CONFLICT: QualitySeverity.ERROR,

    # Conversion problems are errors: the alternative to failing is writing a
    # wrong number into an ERP record.
    IssueCode.TYPE_CONVERSION_FAILED: QualitySeverity.ERROR,
    IssueCode.UNKNOWN_ENUM_VALUE: QualitySeverity.ERROR,
    IssueCode.RULE_EXECUTION_FAILED: QualitySeverity.ERROR,
    IssueCode.UNSUPPORTED_DATA_TYPE: QualitySeverity.ERROR,

    IssueCode.COMPUTED_FIELD_DEPENDENCY_CYCLE: QualitySeverity.CRITICAL,
    IssueCode.COMPUTED_FIELD_INPUT_MISSING: QualitySeverity.ERROR,

    IssueCode.REQUIRED_FIELD_MISSING: QualitySeverity.ERROR,
    IssueCode.NULL_NOT_ALLOWED: QualitySeverity.ERROR,
    IssueCode.DATATYPE_MISMATCH: QualitySeverity.ERROR,
    IssueCode.INVALID_ALLOWED_VALUE: QualitySeverity.ERROR,
    IssueCode.OUT_OF_RANGE: QualitySeverity.ERROR,
    IssueCode.INVALID_IDENTIFIER: QualitySeverity.ERROR,

    IssueCode.DUPLICATE_RECORD: QualitySeverity.ERROR,
    IssueCode.REFERENCE_NOT_FOUND: QualitySeverity.ERROR,
    # Not an error: an unchecked reference is an honest statement that no
    # resolver was supplied. It must never be reported as "valid" (Step 30),
    # but it is not evidence that the data is wrong either.
    IssueCode.REFERENCE_NOT_CHECKED: QualitySeverity.WARNING,
    IssueCode.RECORD_IDENTITY_MISSING: QualitySeverity.ERROR,
    IssueCode.NO_FIELDS_MAPPED: QualitySeverity.ERROR,

    IssueCode.QUALITY_THRESHOLD_EXCEEDED: QualitySeverity.CRITICAL,
    # The engine itself misbehaved. Critical because it says nothing reliable
    # about the data and everything about the pipeline.
    IssueCode.INTERNAL_TRANSFORMATION_ERROR: QualitySeverity.CRITICAL,
}


def default_severity(code: IssueCode) -> QualitySeverity:
    return DEFAULT_SEVERITIES.get(code, QualitySeverity.ERROR)


def make_issue(
    code: IssueCode,
    message: str,
    *,
    severity: QualitySeverity | None = None,
    run_id: str | None = None,
    record_reference: str | None = None,
    source_entity: str | None = None,
    field_name: str | None = None,
    expected: str | None = None,
    value: Any = None,
    options: TransformationOptions | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DataQualityIssue:
    """Build one ``DataQualityIssue`` - the frozen Phase 1 contract.

    ``value`` is accepted so the caller does not have to decide the privacy
    question at every call site. It is used ONLY to derive a redacted shape
    description, and only when the caller opted in.

    ``issue_id`` is derived deterministically from the finding's content, so
    transforming the same record twice produces the same issue id and a
    determinism test can compare whole results (Step 66).
    """
    resolved_severity = severity or default_severity(code)

    summary: str | None = None
    if options is not None and options.include_value_diagnostics and value is not None:
        # redact=True: type and length only, never content.
        summary = summarize_value(value, redact=True)

    issue_id = normalize_identifier(
        "issue."
        + deterministic_suffix(
            {
                "code": code.value,
                "message": message,
                "record": record_reference,
                "entity": source_entity,
                "field": field_name,
                "expected": expected,
            }
        )
    )

    return DataQualityIssue(
        issue_id=issue_id,
        severity=resolved_severity,
        code=code.value,
        message=message,
        run_id=run_id,
        record_id=record_reference,
        source_entity=source_entity,
        field_name=field_name,
        original_value_summary=summary,
        expected=expected,
        metadata=dict(metadata or {}),
    )


def count_by_severity(issues: tuple[DataQualityIssue, ...]) -> dict[str, int]:
    """Severity histogram for a run summary."""
    counts = {severity.value: 0 for severity in QualitySeverity}
    for issue in issues:
        counts[issue.severity.value] += 1
    return counts


def has_blocking(issues: tuple[DataQualityIssue, ...]) -> bool:
    """Whether any issue should stop a record being published.

    Uses the contract's own ``is_blocking`` (ERROR or CRITICAL) rather than a
    second, divergent definition.
    """
    return any(issue.is_blocking for issue in issues)


__all__ = [
    "DEFAULT_SEVERITIES",
    "default_severity",
    "make_issue",
    "count_by_severity",
    "has_blocking",
]
