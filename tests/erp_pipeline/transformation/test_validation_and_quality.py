"""Validation, data-quality issues, duplicates and references.

Steps 23-33, 53, 58, 59. The recurring theme: an invalid value is REPORTED,
never repaired, replaced or quietly dropped.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from erp_pipeline.schemas.enums import (
    FieldDataType as T,
    MappingStatus,
    QualitySeverity,
)
from erp_pipeline.transformation import (
    DuplicatePolicy,
    FieldConstraint,
    InMemoryReferenceResolver,
    IssueCode,
    RecordOutcome,
    SourceRecord,
    TransformationOptions,
    TransformationService,
    ValidationProfile,
    default_severity,
    make_issue,
    transform_record,
)
from erp_pipeline.transformation.errors import TransformationConfigurationError

from tests.erp_pipeline.transformation.conftest import (
    customer_profile,
    invoice_profile,
    make_mapping,
    make_profile,
)


def _customer(cust_no="C001", name="Acme", **extra):
    values = {"cust_no": cust_no, "cust_name": name}
    values.update(extra)
    return SourceRecord.from_mapping(values)


# ============================================================
# Required fields (Steps 23, 53)
# ============================================================

def test_a_missing_required_target_is_reported(pg_context):
    profile = make_profile(
        "req.profile", [make_mapping("cust_name", "name", T.STRING)]
    )

    result = transform_record(_customer(), profile, context=pg_context)

    assert IssueCode.REQUIRED_FIELD_MISSING.value in result.issue_codes()


def test_a_record_missing_a_required_target_is_rejected(pg_context):
    profile = make_profile(
        "req.profile", [make_mapping("cust_name", "name", T.STRING)]
    )

    result = transform_record(_customer(), profile, context=pg_context)

    assert result.outcome is RecordOutcome.REJECTED
    assert result.record is None


def test_a_required_field_missing_error_is_blocking(pg_context):
    profile = make_profile(
        "req.profile", [make_mapping("cust_name", "name", T.STRING)]
    )

    result = transform_record(_customer(), profile, context=pg_context)

    assert any(
        issue.is_blocking
        for issue in result.issues
        if issue.code == IssueCode.REQUIRED_FIELD_MISSING.value
    )


def test_an_optional_target_may_be_absent(pg_context):
    """customer.email and customer.phone are optional."""
    result = transform_record(_customer(), customer_profile(), context=pg_context)

    assert result.is_transformed
    assert "email" not in result.record.normalized_data


# ============================================================
# Datatype validation (Step 24)
# ============================================================

def test_a_converted_decimal_really_is_a_decimal(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "2500.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert isinstance(result.record.normalized_data["amount"], Decimal)


def test_a_mapping_target_type_never_overrides_the_canonical_model(pg_context):
    """The model is the authority; a stale profile belief does not win."""
    profile = invoice_profile(
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            # Profile wrongly claims STRING; canonical invoice.amount is DECIMAL.
            ("total_amt", "amount", T.STRING),
        )
    )

    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "2500.00"}
        ),
        profile,
        context=pg_context,
    )

    assert isinstance(result.record.normalized_data["amount"], Decimal)


def test_datatype_validation_confirms_the_transformers_work(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "2500.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert IssueCode.DATATYPE_MISMATCH.value not in result.issue_codes()


# ============================================================
# Nullability (Step 25)
# ============================================================

def test_a_declared_non_nullable_field_rejects_null(pg_context):
    profile = customer_profile(
        fields=(
            ("cust_no", "customer_id", T.STRING),
            ("cust_name", "name", T.STRING),
            ("mail", "email", T.STRING),
        )
    )
    options = TransformationOptions(
        validation=ValidationProfile(
            constraints=(FieldConstraint(target_field="email", nullable=False),)
        )
    )

    result = transform_record(
        _customer(mail=None), profile, options=options, context=pg_context
    )

    assert IssueCode.NULL_NOT_ALLOWED.value in result.issue_codes()


def test_an_optional_nullable_field_accepts_null(pg_context):
    profile = customer_profile(
        fields=(
            ("cust_no", "customer_id", T.STRING),
            ("cust_name", "name", T.STRING),
            ("mail", "email", T.STRING),
        )
    )

    result = transform_record(_customer(mail=None), profile, context=pg_context)

    assert result.is_transformed
    assert result.record.normalized_data["email"] is None


# ============================================================
# Allowed values (Step 26)
# ============================================================

def _status_profile():
    return invoice_profile(
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
            ("st", "status", T.STRING),
        )
    )


def _status_options():
    return TransformationOptions(
        validation=ValidationProfile(
            constraints=(
                FieldConstraint(
                    target_field="status",
                    allowed_values=("PENDING", "APPROVED", "REJECTED"),
                ),
            )
        )
    )


def test_a_declared_allowed_value_passes(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00", "st": "APPROVED"}
        ),
        _status_profile(),
        options=_status_options(),
        context=pg_context,
    )

    assert result.is_transformed


def test_a_value_outside_the_vocabulary_is_reported(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {
                "inv_no": "I",
                "cust_no": "C",
                "total_amt": "1.00",
                "st": "UNKNOWN_STATUS",
            }
        ),
        _status_profile(),
        options=_status_options(),
        context=pg_context,
    )

    assert IssueCode.INVALID_ALLOWED_VALUE.value in result.issue_codes()
    assert not result.is_transformed


def test_an_invalid_value_is_never_silently_replaced(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00", "st": "BOGUS"}
        ),
        _status_profile(),
        options=_status_options(),
        context=pg_context,
    )

    assert result.record is None


def test_allowed_values_are_not_checked_when_undeclared(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00", "st": "ANYTHING"}
        ),
        _status_profile(),
        context=pg_context,
    )

    assert result.is_transformed


# ============================================================
# Ranges (Step 27)
# ============================================================

def _amount_range_options(minimum=Decimal("0")):
    return TransformationOptions(
        validation=ValidationProfile(
            constraints=(
                FieldConstraint(target_field="amount", min_value=minimum),
            )
        )
    )


def test_a_value_within_range_passes(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "10.00"}
        ),
        invoice_profile(),
        options=_amount_range_options(),
        context=pg_context,
    )

    assert result.is_transformed


def test_a_value_below_the_minimum_is_reported(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "-5.00"}
        ),
        invoice_profile(),
        options=_amount_range_options(),
        context=pg_context,
    )

    assert IssueCode.OUT_OF_RANGE.value in result.issue_codes()


def test_no_range_is_assumed_without_a_declaration(pg_context):
    """The engine never decides on its own that an amount must be positive."""
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "-5.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert result.is_transformed


def test_an_impossible_range_is_refused_at_configuration_time():
    with pytest.raises(TransformationConfigurationError):
        FieldConstraint(target_field="amount", min_value=10, max_value=1)


def test_an_uncomparable_bound_is_reported_not_crashed(pg_context):
    from datetime import date

    options = TransformationOptions(
        validation=ValidationProfile(
            constraints=(
                FieldConstraint(target_field="amount", min_value=date(2026, 1, 1)),
            )
        )
    )

    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "10.00"}
        ),
        invoice_profile(),
        options=options,
        context=pg_context,
    )

    assert IssueCode.OUT_OF_RANGE.value in result.issue_codes()


# ============================================================
# Business identifiers (Step 28)
# ============================================================

def test_a_declared_pattern_is_enforced(pg_context):
    options = TransformationOptions(
        validation=ValidationProfile(
            constraints=(
                FieldConstraint(target_field="customer_id", pattern=r"C\d{3}"),
            )
        )
    )

    good = transform_record(
        _customer("C001"), customer_profile(), options=options, context=pg_context
    )
    bad = transform_record(
        _customer("XX"), customer_profile(), options=options, context=pg_context
    )

    assert good.is_transformed
    assert IssueCode.INVALID_IDENTIFIER.value in bad.issue_codes()


def test_a_declared_length_is_enforced(pg_context):
    options = TransformationOptions(
        validation=ValidationProfile(
            constraints=(
                FieldConstraint(target_field="customer_id", min_length=4),
            )
        )
    )

    result = transform_record(
        _customer("C1"), customer_profile(), options=options, context=pg_context
    )

    assert IssueCode.INVALID_IDENTIFIER.value in result.issue_codes()


def test_no_identifier_format_is_assumed(pg_context):
    """No country, tax or industry format is hard-coded anywhere."""
    result = transform_record(
        _customer("anything at all"), customer_profile(), context=pg_context
    )

    assert result.is_transformed


def test_an_invalid_pattern_is_refused_at_configuration_time():
    with pytest.raises(TransformationConfigurationError):
        FieldConstraint(target_field="x", pattern="[unclosed")


# ============================================================
# Duplicates (Steps 29, 58)
# ============================================================

def _dup_options(policy: DuplicatePolicy) -> TransformationOptions:
    return TransformationOptions(
        duplicate_policy=policy,
        validation=ValidationProfile(duplicate_key_fields=("customer_id",)),
    )


def _dup_batch():
    return [_customer("C001"), _customer("C001"), _customer("C002")]


def test_duplicate_detection_is_off_unless_keys_are_declared(pg_context):
    summary = TransformationService().transform_records(
        _dup_batch(), customer_profile(), pg_context
    )

    assert summary.records_transformed == 3
    assert IssueCode.DUPLICATE_RECORD.value not in summary.issue_codes()


def test_a_duplicate_is_rejected_under_the_reject_policy(pg_context):
    summary = TransformationService(
        options=_dup_options(DuplicatePolicy.REJECT)
    ).transform_records(_dup_batch(), customer_profile(), pg_context)

    assert summary.records_transformed == 2
    assert summary.records_failed == 1
    assert summary.records_skipped == 0
    assert summary.counters_balance


def test_a_duplicate_is_skipped_under_the_skip_policy(pg_context):
    summary = TransformationService(
        options=_dup_options(DuplicatePolicy.SKIP)
    ).transform_records(_dup_batch(), customer_profile(), pg_context)

    assert summary.records_transformed == 2
    assert summary.records_skipped == 1
    assert summary.records_failed == 0
    assert summary.counters_balance


def test_a_skipped_duplicate_states_its_reason(pg_context):
    from erp_pipeline.transformation import SkipReason

    summary = TransformationService(
        options=_dup_options(DuplicatePolicy.SKIP)
    ).transform_records(_dup_batch(), customer_profile(), pg_context)

    assert summary.skipped_records[0].reason is SkipReason.DUPLICATE
    assert summary.skipped_records[0].detail


def test_a_duplicate_is_kept_but_flagged_under_the_warn_policy(pg_context):
    summary = TransformationService(
        options=_dup_options(DuplicatePolicy.WARN)
    ).transform_records(_dup_batch(), customer_profile(), pg_context)

    assert summary.records_transformed == 3
    assert IssueCode.DUPLICATE_RECORD.value in summary.issue_codes()


def test_a_duplicate_is_never_deduplicated_silently(pg_context):
    """Even ALLOW leaves a trace."""
    summary = TransformationService(
        options=_dup_options(DuplicatePolicy.ALLOW)
    ).transform_records(_dup_batch(), customer_profile(), pg_context)

    assert summary.records_transformed == 3
    assert IssueCode.DUPLICATE_RECORD.value in summary.issue_codes()


def test_duplicate_detection_uses_the_declared_key_only(pg_context):
    """Same name, different id - not a duplicate."""
    summary = TransformationService(
        options=_dup_options(DuplicatePolicy.REJECT)
    ).transform_records(
        [_customer("C001", "Acme"), _customer("C002", "Acme")],
        customer_profile(),
        pg_context,
    )

    assert summary.records_transformed == 2


# ============================================================
# References (Steps 30, 59)
# ============================================================

def _reference_options() -> TransformationOptions:
    return TransformationOptions(
        validation=ValidationProfile(
            constraints=(
                FieldConstraint(
                    target_field="customer_id", reference_set="customers"
                ),
            )
        )
    )


def _resolver():
    return InMemoryReferenceResolver.of(customers={"C001", "C002"})


def test_a_known_reference_validates(pg_context):
    service = TransformationService(
        options=_reference_options(), resolver=_resolver()
    )

    result = service.transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C001", "total_amt": "1.00"}
        ),
        invoice_profile(),
        pg_context,
    )

    assert result.is_transformed


def test_an_unknown_reference_is_reported(pg_context):
    service = TransformationService(
        options=_reference_options(), resolver=_resolver()
    )

    result = service.transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C999", "total_amt": "1.00"}
        ),
        invoice_profile(),
        pg_context,
    )

    assert IssueCode.REFERENCE_NOT_FOUND.value in result.issue_codes()
    assert not result.is_transformed


def test_no_resolver_means_not_checked_never_valid(pg_context):
    """Step 30: an unverified reference must not be reported as valid."""
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C999", "total_amt": "1.00"}
        ),
        invoice_profile(),
        options=_reference_options(),
        context=pg_context,
    )

    assert IssueCode.REFERENCE_NOT_CHECKED.value in result.issue_codes()


def test_not_checked_is_a_warning_not_an_error(pg_context):
    """It says nothing about the data, so it must not reject the record."""
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C999", "total_amt": "1.00"}
        ),
        invoice_profile(),
        options=_reference_options(),
        context=pg_context,
    )

    assert result.is_transformed
    assert default_severity(IssueCode.REFERENCE_NOT_CHECKED) is (
        QualitySeverity.WARNING
    )


def test_an_unknown_reference_set_is_not_checked(pg_context):
    service = TransformationService(
        options=_reference_options(),
        resolver=InMemoryReferenceResolver.of(suppliers={"S1"}),
    )

    result = service.transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C001", "total_amt": "1.00"}
        ),
        invoice_profile(),
        pg_context,
    )

    assert IssueCode.REFERENCE_NOT_CHECKED.value in result.issue_codes()


def test_the_validator_carries_no_database_coupling():
    """No connection, engine or driver reaches validator.py."""
    from pathlib import Path

    source = Path("src/erp_pipeline/transformation/validator.py").read_text(
        encoding="utf-8"
    )

    for forbidden in ("sqlalchemy", "psycopg", "pymongo", "connect(", "cursor"):
        assert forbidden not in source


# ============================================================
# DataQualityIssue construction (Steps 31, 32, 33)
# ============================================================

def test_an_issue_uses_the_frozen_phase_1_contract():
    from erp_pipeline.schemas.run_models import DataQualityIssue

    issue = make_issue(IssueCode.TYPE_CONVERSION_FAILED, "message")

    assert isinstance(issue, DataQualityIssue)


def test_an_issue_carries_a_stable_code():
    issue = make_issue(IssueCode.TYPE_CONVERSION_FAILED, "message")

    assert issue.code == "TYPE_CONVERSION_FAILED"


def test_issue_codes_are_never_derived_from_exception_names():
    """Step 76: codes are an interface, pinned to the enum."""
    for code in IssueCode:
        assert code.value == code.value.upper()
        assert " " not in code.value


def test_an_issue_id_is_deterministic():
    first = make_issue(IssueCode.OUT_OF_RANGE, "m", field_name="amount")
    second = make_issue(IssueCode.OUT_OF_RANGE, "m", field_name="amount")

    assert first.issue_id == second.issue_id


def test_severity_defaults_are_declared_per_code():
    assert default_severity(IssueCode.TYPE_CONVERSION_FAILED) is (
        QualitySeverity.ERROR
    )
    assert default_severity(IssueCode.SOURCE_VALUE_NULL) is QualitySeverity.INFO
    assert default_severity(IssueCode.INTERNAL_TRANSFORMATION_ERROR) is (
        QualitySeverity.CRITICAL
    )


def test_not_everything_is_a_warning():
    """Step 33: a finding that corrupts a record is not a warning."""
    from erp_pipeline.transformation import DEFAULT_SEVERITIES

    blocking = {
        code
        for code, severity in DEFAULT_SEVERITIES.items()
        if severity in (QualitySeverity.ERROR, QualitySeverity.CRITICAL)
    }

    assert IssueCode.TYPE_CONVERSION_FAILED in blocking
    assert IssueCode.REQUIRED_FIELD_MISSING in blocking
    assert IssueCode.DATATYPE_MISMATCH in blocking


def test_every_issue_code_has_a_declared_severity():
    from erp_pipeline.transformation import DEFAULT_SEVERITIES

    assert set(DEFAULT_SEVERITIES) == set(IssueCode)


def test_blocking_severities_use_the_contracts_own_definition():
    error = make_issue(IssueCode.TYPE_CONVERSION_FAILED, "m")
    info = make_issue(IssueCode.SOURCE_VALUE_NULL, "m")

    assert error.is_blocking
    assert not info.is_blocking


def test_a_duplicate_constraint_declaration_is_refused():
    with pytest.raises(TransformationConfigurationError):
        ValidationProfile(
            constraints=(
                FieldConstraint(target_field="amount", min_value=0),
                FieldConstraint(target_field="amount", max_value=10),
            )
        )
