"""Runs, counters, thresholds, rejection handling and streaming.

Steps 34-45, 52, 60, 68-72. The central invariant under test:

    records_read == records_transformed + records_failed + records_skipped

and the central policy under test: a run that breached a threshold is never
reported as having succeeded.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterator

import pytest

from erp_pipeline.schemas.enums import (
    FieldDataType as T,
    MappingStatus,
    QualitySeverity,
    RunStatus,
)
from erp_pipeline.schemas.run_models import TransformationRun
from erp_pipeline.transformation import (
    FailurePolicy,
    IssueCode,
    QualityThresholds,
    RecordOutcome,
    SourceRecord,
    TransformationOptions,
    TransformationService,
    transform_record,
    transform_records,
)
from erp_pipeline.transformation.errors import TransformationConfigurationError

from tests.erp_pipeline.transformation.conftest import (
    customer_profile,
    invoice_profile,
    make_mapping,
    make_profile,
)


def good(index: int) -> SourceRecord:
    return SourceRecord.from_mapping(
        {"inv_no": f"INV-{index}", "cust_no": "C001", "total_amt": "10.00"},
        ordinal=index,
    )


def bad(index: int) -> SourceRecord:
    return SourceRecord.from_mapping(
        {"inv_no": f"INV-{index}", "cust_no": "C001", "total_amt": "hello"},
        ordinal=index,
    )


def batch(good_count: int, bad_count: int) -> list[SourceRecord]:
    return [good(i) for i in range(1, good_count + 1)] + [
        bad(i) for i in range(good_count + 1, good_count + bad_count + 1)
    ]


# ============================================================
# The mandatory bad-value proof (Step 52)
# ============================================================

def test_amount_hello_produces_a_quality_issue_and_a_rejection(pg_context):
    summary = transform_records([bad(1)], invoice_profile(), pg_context)

    assert summary.records_read == 1
    assert summary.records_transformed == 0
    assert summary.records_failed == 1
    assert IssueCode.TYPE_CONVERSION_FAILED.value in summary.issue_codes()


def test_amount_hello_never_becomes_zero(pg_context):
    summary = transform_records([bad(1)], invoice_profile(), pg_context)

    assert summary.successful_records == ()


def test_amount_hello_never_becomes_null_or_the_raw_string(pg_context):
    result = transform_record(bad(1), invoice_profile(), context=pg_context)

    assert result.record is None


def test_the_rejection_names_the_field_and_the_target(pg_context):
    result = transform_record(bad(1), invoice_profile(), context=pg_context)

    conversion = [
        issue
        for issue in result.issues
        if issue.code == IssueCode.TYPE_CONVERSION_FAILED.value
    ][0]

    assert conversion.field_name == "total_amt"
    assert "amount" in conversion.message


# ============================================================
# Record outcomes (Step 41)
# ============================================================

def test_every_record_has_exactly_one_outcome(pg_context):
    summary = transform_records(batch(3, 2), invoice_profile(), pg_context)

    assert (
        summary.records_transformed
        + summary.records_failed
        + summary.records_skipped
        == summary.records_read
    )


def test_the_counter_invariant_holds(pg_context):
    summary = transform_records(batch(7, 3), invoice_profile(), pg_context)

    assert summary.counters_balance


def test_the_counter_invariant_holds_with_duplicates(pg_context):
    from erp_pipeline.transformation import DuplicatePolicy, ValidationProfile

    options = TransformationOptions(
        duplicate_policy=DuplicatePolicy.SKIP,
        validation=ValidationProfile(duplicate_key_fields=("invoice_id",)),
    )
    records = [good(1), good(1), good(2), bad(3)]

    summary = TransformationService(options=options).transform_records(
        records, invoice_profile(), pg_context
    )

    assert summary.counters_balance
    assert summary.records_read == 4


def test_a_skipped_record_is_not_counted_as_transformed(pg_context):
    from erp_pipeline.transformation import DuplicatePolicy, ValidationProfile

    options = TransformationOptions(
        duplicate_policy=DuplicatePolicy.SKIP,
        validation=ValidationProfile(duplicate_key_fields=("invoice_id",)),
    )

    summary = TransformationService(options=options).transform_records(
        [good(1), good(1)], invoice_profile(), pg_context
    )

    assert summary.records_transformed == 1
    assert summary.records_skipped == 1


# ============================================================
# Rejected records (Steps 34, 42)
# ============================================================

def test_every_rejected_record_has_at_least_one_reason(pg_context):
    summary = transform_records(batch(2, 3), invoice_profile(), pg_context)

    for rejected in summary.rejected_records:
        assert rejected.reasons


def test_a_rejection_reason_is_a_stable_code(pg_context):
    summary = transform_records([bad(1)], invoice_profile(), pg_context)

    assert IssueCode.TYPE_CONVERSION_FAILED.value in (
        summary.rejected_records[0].reasons
    )


def test_a_rejection_records_its_provenance(pg_context):
    summary = transform_records([bad(4)], invoice_profile(), pg_context)
    rejected = summary.rejected_records[0]

    assert rejected.ordinal == 4
    assert rejected.source_entity == "fin_invoice"
    assert rejected.mapping_id == invoice_profile().mapping_id


def test_a_rejection_serializes_without_source_values_by_default(pg_context):
    summary = transform_records([bad(1)], invoice_profile(), pg_context)

    payload = summary.rejected_records[0].to_dict()

    assert "source_values" not in payload


def test_a_rejection_can_expose_values_on_explicit_request(pg_context):
    """A remediation tool may ask; the default report never does."""
    summary = transform_records([bad(1)], invoice_profile(), pg_context)

    payload = summary.rejected_records[0].to_dict(include_source_values=True)

    assert "source_values" in payload


def test_a_rejected_record_cannot_be_built_without_reasons():
    from erp_pipeline.transformation import RejectedRecord
    from erp_pipeline.transformation.errors import TransformationError

    with pytest.raises(TransformationError):
        RejectedRecord(record_reference="key=X", reasons=())


# ============================================================
# Empty batch (Step 70)
# ============================================================

def test_an_empty_batch_reads_nothing(pg_context):
    summary = transform_records([], invoice_profile(), pg_context)

    assert summary.records_read == 0
    assert summary.records_transformed == 0
    assert summary.records_failed == 0


def test_an_empty_batch_reports_safe_ratios(pg_context):
    summary = transform_records([], invoice_profile(), pg_context)

    assert summary.success_ratio == 0.0
    assert summary.failure_ratio == 0.0
    assert summary.skip_ratio == 0.0


def test_an_empty_batch_succeeds(pg_context):
    """Nothing was asked of it and nothing went wrong."""
    summary = transform_records([], invoice_profile(), pg_context)

    assert summary.run.status is RunStatus.SUCCEEDED
    assert not summary.threshold_exceeded


def test_an_empty_batch_still_records_a_duration(pg_context):
    summary = transform_records([], invoice_profile(), pg_context)

    assert summary.duration_seconds >= 0.0
    assert summary.run.duration_seconds is not None


# ============================================================
# Metrics (Step 68)
# ============================================================

def test_ratios_are_computed_against_records_read(pg_context):
    summary = transform_records(batch(8, 2), invoice_profile(), pg_context)

    assert summary.success_ratio == 0.8
    assert summary.failure_ratio == 0.2
    assert summary.skip_ratio == 0.0


def test_issue_counts_are_reported(pg_context):
    summary = transform_records(batch(1, 1), invoice_profile(), pg_context)

    assert summary.quality_issue_count > 0
    assert summary.error_count > 0
    assert summary.critical_count == 0


def test_a_run_reports_a_duration(pg_context):
    summary = transform_records(batch(2, 0), invoice_profile(), pg_context)

    assert summary.duration_seconds >= 0.0


def test_the_summary_serializes_every_metric(pg_context):
    payload = transform_records(
        batch(3, 1), invoice_profile(), pg_context
    ).to_dict()

    for key in (
        "records_read",
        "records_transformed",
        "records_failed",
        "records_skipped",
        "success_ratio",
        "failure_ratio",
        "skip_ratio",
        "warning_count",
        "error_count",
        "critical_count",
        "quality_issue_count",
        "duration_seconds",
        "threshold_exceeded",
        "counters_balance",
    ):
        assert key in payload


# ============================================================
# TransformationRun (Step 39)
# ============================================================

def test_the_run_is_the_frozen_phase_1_contract(pg_context):
    summary = transform_records(batch(2, 0), invoice_profile(), pg_context)

    assert isinstance(summary.run, TransformationRun)


def test_the_run_reports_the_mandatory_counters(pg_context):
    run = transform_records(batch(3, 1), invoice_profile(), pg_context).run

    assert run.records_read == 4
    assert run.records_transformed == 3
    assert run.records_failed == 1
    assert run.records_skipped == 0
    assert run.warning_count >= 0
    assert run.duration_seconds is not None


def test_the_run_records_the_mapping_it_executed(pg_context):
    run = transform_records(batch(1, 0), invoice_profile(), pg_context).run

    assert run.mapping_id == invoice_profile().mapping_id


def test_the_run_records_the_engine_and_config_versions(pg_context):
    """Step 74: reproducibility."""
    run = transform_records(batch(1, 0), invoice_profile(), pg_context).run

    assert run.metadata["transformation_engine_version"]
    assert run.metadata["transformation_config"]
    assert run.metadata["validation_profile_version"]


def test_a_clean_run_succeeds(pg_context):
    summary = transform_records(batch(3, 0), invoice_profile(), pg_context)

    assert summary.run.status is RunStatus.SUCCEEDED


def test_a_run_with_failures_is_partial_not_succeeded(pg_context):
    summary = transform_records(batch(3, 1), invoice_profile(), pg_context)

    assert summary.run.status is RunStatus.PARTIAL


# ============================================================
# Thresholds (Steps 36, 37, 60)
# ============================================================

def _threshold_options(**kwargs) -> TransformationOptions:
    return TransformationOptions(thresholds=QualityThresholds(**kwargs))


def test_no_numeric_threshold_is_enforced_by_default():
    """No invented universal '5% is acceptable'."""
    thresholds = QualityThresholds()

    assert thresholds.max_failure_ratio is None
    assert thresholds.max_failed_records is None
    assert not thresholds.is_enabled


def test_zero_failures_breaches_nothing(pg_context):
    summary = TransformationService(
        options=_threshold_options(max_failure_ratio=0.05)
    ).transform_records(batch(10, 0), invoice_profile(), pg_context)

    assert not summary.threshold_exceeded
    assert summary.run.status is RunStatus.SUCCEEDED


def test_below_the_threshold_passes(pg_context):
    summary = TransformationService(
        options=_threshold_options(max_failure_ratio=0.25)
    ).transform_records(batch(9, 1), invoice_profile(), pg_context)

    assert not summary.threshold_exceeded


def test_exactly_at_the_threshold_passes(pg_context):
    """The limit is a maximum, so equality is within it."""
    summary = TransformationService(
        options=_threshold_options(max_failure_ratio=0.2)
    ).transform_records(batch(8, 2), invoice_profile(), pg_context)

    assert summary.failure_ratio == 0.2
    assert not summary.threshold_exceeded


def test_above_the_threshold_fails_the_run(pg_context):
    """Step 37's worked example: 90/100 with a 5% limit is not success."""
    summary = TransformationService(
        options=_threshold_options(max_failure_ratio=0.05)
    ).transform_records(batch(90, 10), invoice_profile(), pg_context)

    assert summary.records_read == 100
    assert summary.records_transformed == 90
    assert summary.records_failed == 10
    assert summary.failure_ratio == 0.1
    assert summary.threshold_exceeded
    assert summary.run.status is RunStatus.FAILED


def test_a_breached_run_is_never_reported_as_succeeded(pg_context):
    summary = TransformationService(
        options=_threshold_options(max_failed_records=0)
    ).transform_records(batch(5, 1), invoice_profile(), pg_context)

    assert summary.run.status is not RunStatus.SUCCEEDED


def test_a_breach_raises_a_threshold_issue(pg_context):
    summary = TransformationService(
        options=_threshold_options(max_failure_ratio=0.05)
    ).transform_records(batch(9, 1), invoice_profile(), pg_context)

    assert IssueCode.QUALITY_THRESHOLD_EXCEEDED.value in summary.issue_codes()


def test_a_breach_explains_itself(pg_context):
    summary = TransformationService(
        options=_threshold_options(max_failure_ratio=0.05)
    ).transform_records(batch(9, 1), invoice_profile(), pg_context)

    assert summary.threshold_reasons
    assert "failure ratio" in summary.threshold_reasons[0]


def test_the_max_failed_records_threshold_works(pg_context):
    summary = TransformationService(
        options=_threshold_options(max_failed_records=2)
    ).transform_records(batch(5, 3), invoice_profile(), pg_context)

    assert summary.threshold_exceeded


def test_the_max_error_issues_threshold_works(pg_context):
    summary = TransformationService(
        options=_threshold_options(max_error_issues=1)
    ).transform_records(batch(2, 2), invoice_profile(), pg_context)

    assert summary.threshold_exceeded


def test_the_minimum_success_ratio_threshold_works(pg_context):
    summary = TransformationService(
        options=_threshold_options(minimum_success_ratio=0.95)
    ).transform_records(batch(9, 1), invoice_profile(), pg_context)

    assert summary.threshold_exceeded


def test_an_invalid_ratio_is_refused_at_configuration_time():
    with pytest.raises(TransformationConfigurationError):
        QualityThresholds(max_failure_ratio=1.5)


def test_a_negative_count_threshold_is_refused():
    with pytest.raises(TransformationConfigurationError):
        QualityThresholds(max_failed_records=-1)


# ============================================================
# Fail-fast vs continue (Step 38)
# ============================================================

def test_continue_is_the_default():
    assert TransformationOptions().failure_policy is FailurePolicy.CONTINUE


def test_continue_processes_the_whole_batch(pg_context):
    summary = TransformationService(
        options=_threshold_options(max_failed_records=0)
    ).transform_records(batch(5, 5), invoice_profile(), pg_context)

    assert summary.records_read == 10
    assert not summary.stopped_early


def test_fail_fast_stops_at_the_breach(pg_context):
    options = TransformationOptions(
        failure_policy=FailurePolicy.FAIL_FAST,
        thresholds=QualityThresholds(max_failed_records=1),
    )
    records = [good(1), bad(2), bad(3), good(4), good(5)]

    summary = TransformationService(options=options).transform_records(
        records, invoice_profile(), pg_context
    )

    assert summary.stopped_early
    assert summary.records_read < 5


def test_fail_fast_counts_only_what_it_read(pg_context):
    options = TransformationOptions(
        failure_policy=FailurePolicy.FAIL_FAST,
        thresholds=QualityThresholds(max_failed_records=0),
    )

    summary = TransformationService(options=options).transform_records(
        [good(1), bad(2), good(3), good(4)], invoice_profile(), pg_context
    )

    assert summary.records_read == 2
    assert summary.counters_balance


def test_fail_fast_marks_the_run_failed(pg_context):
    options = TransformationOptions(
        failure_policy=FailurePolicy.FAIL_FAST,
        thresholds=QualityThresholds(max_failed_records=0),
    )

    summary = TransformationService(options=options).transform_records(
        [bad(1), good(2)], invoice_profile(), pg_context
    )

    assert summary.run.status is RunStatus.FAILED


def test_stop_on_critical_issue_is_enabled_by_default():
    assert QualityThresholds().stop_on_critical_issue is True


# ============================================================
# Unexpected errors (Step 43)
# ============================================================

def test_one_malformed_record_does_not_crash_the_batch(pg_context, monkeypatch):
    """A per-record barrier keeps the other records processable."""
    from erp_pipeline.transformation import transformer as transformer_module

    original = transformer_module.RecordTransformer.transform
    calls = {"n": 0}

    def flaky(self, source_record, mapping_profile, context, run_id=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("synthetic internal failure")
        return original(self, source_record, mapping_profile, context, run_id)

    monkeypatch.setattr(transformer_module.RecordTransformer, "transform", flaky)

    summary = TransformationService(
        options=TransformationOptions(
            thresholds=QualityThresholds(stop_on_critical_issue=False)
        )
    ).transform_records(batch(3, 0), invoice_profile(), pg_context)

    assert summary.records_read == 3
    assert summary.records_transformed == 2
    assert summary.records_failed == 1
    assert IssueCode.INTERNAL_TRANSFORMATION_ERROR.value in summary.issue_codes()


def test_an_internal_error_still_produces_a_reason(pg_context, monkeypatch):
    from erp_pipeline.transformation import transformer as transformer_module

    def always_fail(self, source_record, mapping_profile, context, run_id=None):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(
        transformer_module.RecordTransformer, "transform", always_fail
    )

    summary = TransformationService(
        options=TransformationOptions(
            thresholds=QualityThresholds(stop_on_critical_issue=False)
        )
    ).transform_records([good(1)], invoice_profile(), pg_context)

    assert summary.rejected_records[0].reasons == (
        IssueCode.INTERNAL_TRANSFORMATION_ERROR.value,
    )


def test_an_internal_error_message_names_no_value(pg_context, monkeypatch):
    from erp_pipeline.transformation import transformer as transformer_module

    def always_fail(self, source_record, mapping_profile, context, run_id=None):
        raise RuntimeError("SECRET_LEAK_12345")

    monkeypatch.setattr(
        transformer_module.RecordTransformer, "transform", always_fail
    )

    summary = TransformationService(
        options=TransformationOptions(
            thresholds=QualityThresholds(stop_on_critical_issue=False)
        )
    ).transform_records([good(1)], invoice_profile(), pg_context)

    assert "SECRET_LEAK_12345" not in summary.issues[0].message


def test_keyboard_interrupt_is_not_swallowed(pg_context, monkeypatch):
    """BaseException must pass straight through a per-record barrier."""
    from erp_pipeline.transformation import transformer as transformer_module

    def interrupt(self, source_record, mapping_profile, context, run_id=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        transformer_module.RecordTransformer, "transform", interrupt
    )

    with pytest.raises(KeyboardInterrupt):
        TransformationService().transform_records(
            [good(1)], invoice_profile(), pg_context
        )


def test_a_configuration_defect_is_not_turned_into_a_record_issue(pg_context):
    """An unimplemented operation would repeat on every record."""
    from erp_pipeline.schemas.mapping_models import TransformationRule
    from erp_pipeline.schemas.enums import TransformationOperation
    from erp_pipeline.transformation import rules as rule_module
    from erp_pipeline.transformation.errors import UnsupportedOperationError

    profile = make_profile(
        "unsupported.profile",
        [
            make_mapping("inv_no", "invoice_id", T.STRING),
            make_mapping("cust_no", "customer_id", T.STRING),
            make_mapping(
                "total_amt",
                "amount",
                T.DECIMAL,
                transformations=(
                    TransformationRule(operation=TransformationOperation.TRIM),
                ),
            ),
        ],
        source_entity="fin_invoice",
        target_entity_type="invoice",
    )

    registry = dict(rule_module._REGISTRY)
    registry.pop(TransformationOperation.TRIM)
    original = rule_module._REGISTRY
    rule_module._REGISTRY = registry

    try:
        with pytest.raises(UnsupportedOperationError):
            transform_records([good(1)], profile, pg_context)
    finally:
        rule_module._REGISTRY = original


# ============================================================
# Ordering and streaming (Steps 67, 71, 72)
# ============================================================

def test_input_order_is_preserved(pg_context):
    records = [good(i) for i in range(1, 6)]

    summary = transform_records(records, invoice_profile(), pg_context)

    keys = [
        record.normalized_data["invoice_id"]
        for record in summary.successful_records
    ]

    assert keys == [f"INV-{i}" for i in range(1, 6)]


def test_rejections_keep_input_order(pg_context):
    records = [bad(1), good(2), bad(3)]

    summary = transform_records(records, invoice_profile(), pg_context)

    assert [item.ordinal for item in summary.rejected_records] == [1, 3]


def test_a_generator_is_accepted(pg_context):
    def stream() -> Iterator[SourceRecord]:
        for i in range(1, 4):
            yield good(i)

    summary = transform_records(stream(), invoice_profile(), pg_context)

    assert summary.records_transformed == 3


def test_the_source_is_not_drained_up_front(pg_context):
    """Proof that processing is incremental, not list()-then-transform."""
    pulled: list[int] = []

    def stream() -> Iterator[SourceRecord]:
        for i in range(1, 1001):
            pulled.append(i)
            yield bad(i)

    options = TransformationOptions(
        failure_policy=FailurePolicy.FAIL_FAST,
        thresholds=QualityThresholds(max_failed_records=0),
    )

    summary = TransformationService(options=options).transform_records(
        stream(), invoice_profile(), pg_context
    )

    assert len(pulled) == 1
    assert summary.records_read == 1


def test_fail_fast_stops_a_stream_cleanly(pg_context):
    def stream() -> Iterator[SourceRecord]:
        yield good(1)
        yield bad(2)
        yield good(3)
        raise AssertionError("the stream should never be advanced this far")

    options = TransformationOptions(
        failure_policy=FailurePolicy.FAIL_FAST,
        thresholds=QualityThresholds(max_failed_records=0),
    )

    summary = TransformationService(options=options).transform_records(
        stream(), invoice_profile(), pg_context
    )

    assert summary.records_read == 2
    assert summary.stopped_early
