"""Transformation-run and data-quality contract tests."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from erp_pipeline.schemas import (
    DataQualityIssue,
    QualitySeverity,
    RunStatus,
    TransformationRun,
    ValidationError,
    summarize_value,
)
from erp_pipeline.schemas.validation import MAX_VALUE_SUMMARY_LENGTH
from erp_pipeline.version import RUN_MODEL_VERSION

STARTED = datetime(2026, 8, 10, 9, 0, 0, tzinfo=timezone.utc)
COMPLETED = datetime(2026, 8, 10, 9, 5, 30, tzinfo=timezone.utc)


def build_run(**overrides) -> TransformationRun:
    kwargs = dict(
        run_id="run_2026_08_10_0001",
        source_system_id="finance_erp_pg",
        mapping_id="finance_erp_pg_invoice_v1",
        status=RunStatus.SUCCEEDED,
        started_at=STARTED,
        completed_at=COMPLETED,
        records_read=1000,
        records_transformed=990,
        records_failed=4,
        records_skipped=6,
        warning_count=12,
        error_count=4,
    )
    kwargs.update(overrides)
    return TransformationRun(**kwargs)


# ============================================================
# TransformationRun
# ============================================================

def test_transformation_run_serializes_completely():
    run = build_run()
    payload = run.to_json_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["status"] == "succeeded"
    assert payload["started_at"] == "2026-08-10T09:00:00Z"
    assert payload["completed_at"] == "2026-08-10T09:05:30Z"
    assert payload["model_version"] == RUN_MODEL_VERSION


@pytest.mark.parametrize(
    "counter",
    [
        "records_read",
        "records_transformed",
        "records_failed",
        "records_skipped",
        "warning_count",
        "error_count",
    ],
)
def test_negative_counts_are_rejected(counter):
    with pytest.raises(ValidationError, match="must not be negative"):
        build_run(**{counter: -1})


@pytest.mark.parametrize(
    "counter",
    ["records_read", "records_transformed", "records_failed", "records_skipped"],
)
def test_non_integer_counts_are_rejected(counter):
    with pytest.raises(ValidationError, match="must be an integer"):
        build_run(**{counter: 12.5})


def test_zero_counts_are_valid():
    run = build_run(
        records_read=0,
        records_transformed=0,
        records_failed=0,
        records_skipped=0,
        warning_count=0,
        error_count=0,
    )
    assert run.records_read == 0


def test_completion_before_start_is_rejected():
    with pytest.raises(ValidationError, match="must not be earlier than"):
        build_run(started_at=COMPLETED, completed_at=STARTED)


def test_equal_start_and_completion_is_allowed():
    run = build_run(started_at=STARTED, completed_at=STARTED)
    assert run.duration_seconds == 0.0


def test_terminal_status_requires_a_completion_time():
    with pytest.raises(ValidationError, match="must record when it finished"):
        build_run(status=RunStatus.FAILED, completed_at=None)


def test_non_terminal_status_must_not_claim_completion():
    with pytest.raises(ValidationError, match="Only a finished run"):
        build_run(status=RunStatus.RUNNING, completed_at=COMPLETED)


def test_completion_without_start_is_rejected():
    with pytest.raises(ValidationError, match="without started_at"):
        build_run(started_at=None, completed_at=COMPLETED)


def test_pending_run_is_valid_with_no_timestamps():
    run = TransformationRun(
        run_id="run_pending", source_system_id="finance_erp_pg"
    )

    assert run.status is RunStatus.PENDING
    assert run.duration_seconds is None
    assert run.to_json_dict()["completed_at"] is None


def test_duration_and_cleanliness_are_derived():
    run = build_run()
    assert run.duration_seconds == 330.0
    assert run.is_clean is False

    clean = build_run(records_failed=0, error_count=0)
    assert clean.is_clean is True


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValidationError, match="timezone-aware"):
        build_run(started_at=datetime(2026, 8, 10, 9, 0, 0))


def test_run_id_must_be_a_normalized_identifier():
    with pytest.raises(ValidationError, match="normalized identifier"):
        build_run(run_id="Run #1")


def test_fan_out_counts_are_allowed():
    """One source record may produce several canonical records.

    The contract deliberately does not assert
    transformed + failed + skipped <= read, because that inequality is not
    universally true and encoding it would force later phases to misreport.
    """
    run = build_run(records_read=100, records_transformed=350)
    assert run.records_transformed > run.records_read


# ============================================================
# DataQualityIssue
# ============================================================

def build_issue(**overrides) -> DataQualityIssue:
    kwargs = dict(
        issue_id="issue_0001",
        run_id="run_2026_08_10_0001",
        record_id="erp:finance_erp_pg:invoice:inv-001",
        source_entity="fin_invoice",
        field_name="total_amount",
        severity=QualitySeverity.ERROR,
        code="NON_NUMERIC_AMOUNT",
        message="total_amount could not be parsed as a decimal.",
        expected="decimal(12,2)",
    )
    kwargs.update(overrides)
    return DataQualityIssue(**kwargs)


def test_data_quality_issue_serializes_completely():
    issue = build_issue(
        original_value_summary=summarize_value("25,000.00 LKR"),
        created_at=datetime(2026, 8, 10, 9, 3, tzinfo=timezone.utc),
    )
    payload = issue.to_json_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["severity"] == "error"
    assert payload["code"] == "NON_NUMERIC_AMOUNT"
    assert payload["created_at"] == "2026-08-10T09:03:00Z"
    assert payload["original_value_summary"] == "25,000.00 LKR"


@pytest.mark.parametrize(
    "severity, blocking",
    [
        (QualitySeverity.INFO, False),
        (QualitySeverity.WARNING, False),
        (QualitySeverity.ERROR, True),
        (QualitySeverity.CRITICAL, True),
    ],
)
def test_issue_blocking_classification(severity, blocking):
    assert build_issue(severity=severity).is_blocking is blocking


def test_invalid_severity_is_rejected():
    with pytest.raises(ValueError, match="not a valid QualitySeverity"):
        build_issue(severity="catastrophic")


def test_issue_code_must_be_token_like():
    with pytest.raises(ValidationError, match="must not contain whitespace"):
        build_issue(code="the amount was not numeric")


def test_original_value_summary_is_optional():
    assert build_issue().original_value_summary is None


def test_original_value_summary_is_length_bounded():
    """A quality report must not become a second copy of the source data."""
    with pytest.raises(ValidationError, match="bounded diagnostic excerpt"):
        build_issue(original_value_summary="x" * (MAX_VALUE_SUMMARY_LENGTH + 1))


def test_summarize_value_truncates_to_the_bound():
    summary = summarize_value("y" * 5000)

    assert len(summary) <= MAX_VALUE_SUMMARY_LENGTH
    assert summary.endswith("...[truncated]")
    # The truncated excerpt is accepted by the model.
    assert build_issue(original_value_summary=summary).original_value_summary == summary


def test_summarize_value_can_redact_content_entirely():
    """A finding on a restricted field should report shape, not content."""
    summary = summarize_value("123-45-6789", redact=True)

    assert "123-45-6789" not in summary
    assert "redacted" in summary
    assert "length=11" in summary


def test_issue_metadata_rejects_credentials():
    with pytest.raises(ValidationError, match="must not contain credentials"):
        build_issue(metadata={"db_password": "hunter2"})


def test_issue_can_reference_a_canonical_record_id():
    issue = build_issue()
    assert issue.record_id.startswith("erp:")


def test_issue_without_a_run_is_valid():
    """Validation can happen outside a run, for example during review."""
    issue = DataQualityIssue(
        issue_id="issue_adhoc",
        severity=QualitySeverity.WARNING,
        code="MISSING_MAPPING",
        message="No mapping proposed for source field discount_pct.",
    )

    assert issue.run_id is None
    assert issue.to_json_dict()["run_id"] is None
