"""Extraction, assignment, type conversion, nulls, defaults, enums, normalization.

Steps 6-17. These are the mechanics that decide whether an ERP value survives
the journey intact, so the negative cases matter as much as the positive ones:
most of this file is about what the engine REFUSES to do.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from erp_pipeline.schemas.enums import (
    FieldDataType as T,
    MappingStatus,
    TransformationOperation,
)
from erp_pipeline.schemas.mapping_models import TransformationRule
from erp_pipeline.transformation import (
    BooleanPolicy,
    CaseNormalization,
    DatePolicy,
    ExtractionOutcome,
    IssueCode,
    NormalizationPolicy,
    NullPolicy,
    NumberPolicy,
    SourceRecord,
    StringPolicy,
    TransformationOptions,
    UnknownTypePolicy,
    assign_value,
    convert,
    extract_value,
    normalize_value,
    transform_record,
)

from tests.erp_pipeline.transformation.conftest import (
    customer_profile,
    invoice_profile,
    make_mapping,
    make_profile,
)

OPTIONS = TransformationOptions()


# ============================================================
# Extraction (Step 6)
# ============================================================

def test_a_direct_field_is_extracted():
    outcome, value = extract_value({"cust_no": "C001"}, "cust_no", OPTIONS)

    assert outcome is ExtractionOutcome.FOUND
    assert value == "C001"


def test_a_nested_path_is_extracted():
    values = {"customer": {"contact": {"email": "x@example.test"}}}

    outcome, value = extract_value(values, "customer.contact.email", OPTIONS)

    assert outcome is ExtractionOutcome.FOUND
    assert value == "x@example.test"


def test_a_missing_field_is_reported_as_missing_not_null():
    outcome, value = extract_value({"other": 1}, "cust_no", OPTIONS)

    assert outcome is ExtractionOutcome.MISSING
    assert value is None


def test_a_present_null_is_reported_as_null_not_missing():
    outcome, value = extract_value({"cust_no": None}, "cust_no", OPTIONS)

    assert outcome is ExtractionOutcome.NULL


def test_missing_and_null_are_different_outcomes():
    """The distinction is the whole point of Step 6."""
    missing, _ = extract_value({}, "x", OPTIONS)
    null, _ = extract_value({"x": None}, "x", OPTIONS)

    assert missing is not null


def test_a_broken_nested_path_does_not_raise():
    """A KeyError escaping would take down records that are perfectly fine."""
    outcome, _ = extract_value({"customer": "ABC"}, "customer.contact.email", OPTIONS)

    assert outcome is ExtractionOutcome.MISSING


def test_an_array_marker_path_is_not_silently_guessed():
    outcome, _ = extract_value(
        {"lines": [{"sku": "A"}]}, "lines[].sku", OPTIONS
    )

    assert outcome is ExtractionOutcome.MISSING


def test_a_missing_source_field_leaves_the_target_absent(pg_context):
    """So validation can say REQUIRED_FIELD_MISSING, not NULL_NOT_ALLOWED."""
    result = transform_record(
        SourceRecord.from_mapping({"cust_name": "Acme"}),
        customer_profile(),
        context=pg_context,
    )

    assert IssueCode.REQUIRED_FIELD_MISSING.value in result.issue_codes()
    assert IssueCode.SOURCE_FIELD_MISSING.value in result.issue_codes()


def test_a_null_source_field_is_reported_as_a_null_violation(pg_context):
    result = transform_record(
        SourceRecord.from_mapping({"cust_no": None, "cust_name": "Acme"}),
        customer_profile(),
        context=pg_context,
    )

    assert IssueCode.NULL_NOT_ALLOWED.value in result.issue_codes()


# ============================================================
# Null markers (Step 14)
# ============================================================

def test_null_markers_are_not_applied_by_default():
    outcome, value = extract_value({"x": "N/A"}, "x", OPTIONS)

    assert outcome is ExtractionOutcome.FOUND
    assert value == "N/A"


def test_a_configured_null_marker_is_honoured():
    options = TransformationOptions(
        null_policy=NullPolicy(null_markers=("N/A", "NULL"))
    )

    outcome, _ = extract_value({"x": "N/A"}, "x", options)

    assert outcome is ExtractionOutcome.NULL


def test_null_markers_are_case_insensitive_by_default():
    options = TransformationOptions(null_policy=NullPolicy(null_markers=("N/A",)))

    outcome, _ = extract_value({"x": "n/a"}, "x", options)

    assert outcome is ExtractionOutcome.NULL


def test_an_empty_string_is_not_null_by_default():
    outcome, value = extract_value({"x": ""}, "x", OPTIONS)

    assert outcome is ExtractionOutcome.FOUND
    assert value == ""


def test_an_empty_string_is_null_when_configured():
    options = TransformationOptions(
        null_policy=NullPolicy(empty_string_is_null=True)
    )

    outcome, _ = extract_value({"x": ""}, "x", options)

    assert outcome is ExtractionOutcome.NULL


# ============================================================
# Target assignment (Step 7)
# ============================================================

def test_a_flat_target_is_assigned():
    data: dict = {}

    assert assign_value(data, "customer_id", "C001") is None
    assert data == {"customer_id": "C001"}


def test_a_nested_target_builds_nested_objects():
    data: dict = {}

    assert assign_value(data, "contact.email", "x@example.test") is None
    assert data == {"contact": {"email": "x@example.test"}}


def test_a_scalar_is_never_silently_replaced_by_an_object():
    """Step 7's worked example."""
    data = {"customer": "ABC"}

    conflict = assign_value(data, "customer.email", "x@example.test")

    assert conflict is not None
    assert data == {"customer": "ABC"}


def test_assigning_the_same_target_twice_is_a_conflict():
    data: dict = {}
    assign_value(data, "customer_id", "C001")

    assert assign_value(data, "customer_id", "C002") is not None
    assert data["customer_id"] == "C001"


def test_a_target_path_conflict_reaches_the_result(pg_context):
    profile = make_profile(
        "conflict.profile",
        [
            make_mapping("a", "customer_id", T.STRING),
            make_mapping("b", "customer_id.nested", T.STRING),
            make_mapping("c", "name", T.STRING),
        ],
    )

    result = transform_record(
        SourceRecord.from_mapping({"a": "C001", "b": "x", "c": "Acme"}),
        profile,
        context=pg_context,
    )

    assert IssueCode.TARGET_PATH_CONFLICT.value in result.issue_codes()
    assert not result.is_transformed


# ============================================================
# STRING conversion (Step 9)
# ============================================================

def test_a_string_stays_a_string():
    assert convert("hello", T.STRING, OPTIONS).value == "hello"


def test_a_leading_zero_identifier_survives():
    """The single most common silent corruption in ERP migration."""
    result = convert("007", T.STRING, OPTIONS)

    assert result.ok
    assert result.value == "007"


def test_an_integer_becomes_a_string_when_allowed():
    assert convert(25, T.STRING, OPTIONS).value == "25"


def test_a_boolean_is_not_stringified_by_default():
    assert not convert(True, T.STRING, OPTIONS).ok


def test_an_object_is_not_stringified_by_default():
    assert not convert({"a": 1}, T.STRING, OPTIONS).ok


def test_an_object_can_be_stringified_when_explicitly_allowed():
    options = TransformationOptions(
        string_policy=StringPolicy(allow_structural_to_string=True)
    )

    assert convert({"a": 1}, T.STRING, options).ok


def test_a_float_becomes_its_decimal_reading_not_its_binary_one():
    assert convert(2500.50, T.STRING, OPTIONS).value == "2500.5"


# ============================================================
# INTEGER conversion (Step 10)
# ============================================================

def test_an_integer_string_converts():
    assert convert("25", T.INTEGER, OPTIONS).value == 25


def test_an_integral_float_converts_when_allowed():
    assert convert(25.0, T.INTEGER, OPTIONS).value == 25


def test_a_fractional_value_is_never_truncated():
    result = convert("25.9", T.INTEGER, OPTIONS)

    assert not result.ok
    assert "fractional" in (result.reason or "")


def test_non_numeric_text_is_refused():
    assert not convert("hello", T.INTEGER, OPTIONS).ok


def test_a_boolean_is_not_an_integer():
    """bool is an int subclass in Python; accepting it would be a silent bug."""
    assert not convert(True, T.INTEGER, OPTIONS).ok


def test_nan_and_infinity_are_refused_for_integers():
    assert not convert(float("nan"), T.INTEGER, OPTIONS).ok
    assert not convert(float("inf"), T.INTEGER, OPTIONS).ok


def test_an_empty_string_is_not_zero():
    assert not convert("", T.INTEGER, OPTIONS).ok


# ============================================================
# DECIMAL conversion (Step 11)
# ============================================================

def test_a_decimal_string_becomes_a_decimal():
    result = convert("2500.50", T.DECIMAL, OPTIONS)

    assert result.ok
    assert result.value == Decimal("2500.50")
    assert isinstance(result.value, Decimal)


def test_an_integer_becomes_a_decimal():
    assert convert(2500, T.DECIMAL, OPTIONS).value == Decimal("2500")


def test_a_float_keeps_its_printed_precision():
    """Decimal(2500.50) would capture the binary approximation instead."""
    assert convert(2500.50, T.DECIMAL, OPTIONS).value == Decimal("2500.50")


def test_money_is_never_a_float():
    assert not isinstance(convert("2500.50", T.DECIMAL, OPTIONS).value, float)


def test_non_numeric_text_is_not_a_decimal():
    result = convert("hello", T.DECIMAL, OPTIONS)

    assert not result.ok
    assert result.code is IssueCode.TYPE_CONVERSION_FAILED


def test_nan_and_infinity_are_refused_for_decimals():
    assert not convert(Decimal("NaN"), T.DECIMAL, OPTIONS).ok
    assert not convert(Decimal("Infinity"), T.DECIMAL, OPTIONS).ok


def test_a_thousands_separator_is_ambiguous_by_default():
    assert not convert("1,234", T.DECIMAL, OPTIONS).ok


def test_a_thousands_separator_converts_when_declared():
    options = TransformationOptions(
        number_policy=NumberPolicy(allow_thousands_separator=True)
    )

    assert convert("1,234", T.DECIMAL, options).value == Decimal("1234")


# ============================================================
# BOOLEAN conversion (Step 12)
# ============================================================

def test_a_real_boolean_converts():
    assert convert(True, T.BOOLEAN, OPTIONS).value is True


def test_the_configured_literals_convert():
    assert convert("true", T.BOOLEAN, OPTIONS).value is True
    assert convert("false", T.BOOLEAN, OPTIONS).value is False


def test_a_non_empty_string_is_not_true():
    """Step 12's worked example: 'approved' must not become True."""
    assert not convert("approved", T.BOOLEAN, OPTIONS).ok


def test_integers_are_not_booleans_by_default():
    assert not convert(1, T.BOOLEAN, OPTIONS).ok


def test_integers_are_booleans_when_configured():
    options = TransformationOptions(
        boolean_policy=BooleanPolicy(allow_integer_forms=True)
    )

    assert convert(1, T.BOOLEAN, options).value is True


def test_yes_no_converts_only_when_configured():
    options = TransformationOptions(
        boolean_policy=BooleanPolicy(
            true_values=("true", "yes"), false_values=("false", "no")
        )
    )

    assert not convert("yes", T.BOOLEAN, OPTIONS).ok
    assert convert("yes", T.BOOLEAN, options).value is True


def test_a_literal_cannot_mean_both_true_and_false():
    from erp_pipeline.transformation.errors import TransformationConfigurationError

    with pytest.raises(TransformationConfigurationError):
        BooleanPolicy(true_values=("y",), false_values=("Y",))


# ============================================================
# DATE / DATETIME conversion (Step 13)
# ============================================================

def test_an_iso_date_converts():
    assert convert("2026-08-14", T.DATE, OPTIONS).value == date(2026, 8, 14)


def test_a_date_object_converts():
    assert convert(date(2026, 8, 14), T.DATE, OPTIONS).value == date(2026, 8, 14)


def test_an_ambiguous_date_is_refused():
    """03/04/2026 is 3 April or 4 March depending on where you are."""
    assert not convert("03/04/2026", T.DATE, OPTIONS).ok


def test_an_ambiguous_date_converts_with_a_declared_format():
    options = TransformationOptions(
        date_policy=DatePolicy(date_formats=("%d/%m/%Y",))
    )

    assert convert("03/04/2026", T.DATE, options).value == date(2026, 4, 3)


def test_a_timestamp_is_not_silently_truncated_to_a_date():
    assert not convert("2026-08-14T09:30:00", T.DATE, OPTIONS).ok


def test_a_datetime_object_is_not_silently_truncated_to_a_date():
    assert not convert(datetime(2026, 8, 14, 9, 30), T.DATE, OPTIONS).ok


def test_an_iso_datetime_converts_to_utc_aware():
    result = convert("2026-08-14T09:30:00Z", T.DATETIME, OPTIONS)

    assert result.ok
    assert result.value == datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)
    assert result.value.tzinfo is not None


def test_an_offset_datetime_keeps_its_instant():
    result = convert("2026-08-14T11:30:00+02:00", T.DATETIME, OPTIONS)

    assert result.value == datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)


def test_a_naive_datetime_is_made_utc_aware_by_default():
    """The canonical model cannot hold a naive datetime at all."""
    result = convert("2026-08-14T09:30:00", T.DATETIME, OPTIONS)

    assert result.value.tzinfo is timezone.utc


def test_a_naive_datetime_is_refused_when_configured():
    options = TransformationOptions(
        date_policy=DatePolicy(assume_utc_when_naive=False)
    )

    assert not convert(datetime(2026, 8, 14, 9, 30), T.DATETIME, options).ok


def test_a_date_promotes_to_midnight_utc():
    result = convert(date(2026, 8, 14), T.DATETIME, OPTIONS)

    assert result.value == datetime(2026, 8, 14, tzinfo=timezone.utc)


# ============================================================
# BINARY / OBJECT / ARRAY / UNKNOWN
# ============================================================

def test_bytes_become_base64_because_the_contract_cannot_hold_raw_bytes():
    result = convert(b"hello", T.BINARY, OPTIONS)

    assert result.ok
    assert result.value == "aGVsbG8="


def test_non_base64_text_is_not_binary():
    assert not convert("not base64!!", T.BINARY, OPTIONS).ok


def test_an_object_target_accepts_a_mapping():
    assert convert({"a": 1}, T.OBJECT, OPTIONS).value == {"a": 1}


def test_an_object_target_refuses_a_scalar():
    assert not convert("abc", T.OBJECT, OPTIONS).ok


def test_an_array_target_accepts_a_list():
    assert convert([1, 2], T.ARRAY, OPTIONS).value == [1, 2]


def test_an_array_target_refuses_a_string():
    """A string is iterable; a character-by-character array is a classic bug."""
    assert not convert("abc", T.ARRAY, OPTIONS).ok


def test_an_unknown_target_type_passes_through_by_default():
    assert convert("anything", T.UNKNOWN, OPTIONS).value == "anything"


def test_an_unknown_target_type_can_be_refused():
    options = TransformationOptions(
        unknown_type_policy=UnknownTypePolicy.REJECT
    )

    result = convert("anything", T.UNKNOWN, options)

    assert not result.ok
    assert result.code is IssueCode.UNSUPPORTED_DATA_TYPE


def test_null_passes_through_every_type():
    for data_type in (T.STRING, T.INTEGER, T.DECIMAL, T.DATE, T.DATETIME):
        assert convert(None, data_type, OPTIONS).value is None


# ============================================================
# Defaults (Steps 15, 54)
# ============================================================

def test_a_default_fills_a_missing_field(pg_context):
    profile = invoice_profile(
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
            ("ccy", "currency", T.STRING),
        )
    )
    options = TransformationOptions(defaults={"currency": "USD"})

    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "INV-1", "cust_no": "C001", "total_amt": "10.00"}
        ),
        profile,
        options=options,
        context=pg_context,
    )

    assert result.is_transformed
    assert result.record.normalized_data["currency"] == "USD"


def test_a_default_fills_a_null_field(pg_context):
    profile = invoice_profile(
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
            ("ccy", "currency", T.STRING),
        )
    )
    options = TransformationOptions(defaults={"currency": "USD"})

    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00", "ccy": None}
        ),
        profile,
        options=options,
        context=pg_context,
    )

    assert result.record.normalized_data["currency"] == "USD"


def test_a_default_never_rescues_a_conversion_failure(pg_context):
    """Step 15's worked example: amount='hello' must not become 0."""
    options = TransformationOptions(defaults={"amount": Decimal("0")})

    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "hello"}
        ),
        invoice_profile(),
        options=options,
        context=pg_context,
    )

    assert not result.is_transformed
    assert IssueCode.TYPE_CONVERSION_FAILED.value in result.issue_codes()


def test_a_present_valid_value_is_not_overwritten_by_a_default(pg_context):
    profile = invoice_profile(
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
            ("ccy", "currency", T.STRING),
        )
    )
    options = TransformationOptions(defaults={"currency": "USD"})

    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00", "ccy": "LKR"}
        ),
        profile,
        options=options,
        context=pg_context,
    )

    assert result.record.normalized_data["currency"] == "LKR"


# ============================================================
# Enum mapping (Steps 16, 55)
# ============================================================

def _enum_profile() -> object:
    return invoice_profile(
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
        )
    )


def _status_profile():
    return make_profile(
        "enum.profile",
        [
            make_mapping("inv_no", "invoice_id", T.STRING),
            make_mapping("cust_no", "customer_id", T.STRING),
            make_mapping("total_amt", "amount", T.DECIMAL),
            make_mapping(
                "st",
                "status",
                T.STRING,
                transformations=(
                    TransformationRule(
                        operation=TransformationOperation.ENUM_MAP,
                        config={"values": {"P": "PENDING", "C": "COMPLETED"}},
                    ),
                ),
            ),
        ],
        source_entity="fin_invoice",
        target_entity_type="invoice",
    )


def test_a_declared_enum_code_translates(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00", "st": "P"}
        ),
        _status_profile(),
        context=pg_context,
    )

    assert result.is_transformed
    assert result.record.normalized_data["status"] == "PENDING"


def test_an_undeclared_enum_code_is_an_issue_not_a_guess(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00", "st": "UNKNOWN"}
        ),
        _status_profile(),
        context=pg_context,
    )

    assert not result.is_transformed
    assert IssueCode.UNKNOWN_ENUM_VALUE.value in result.issue_codes()


def test_enum_mapping_is_exact_not_fuzzy(pg_context):
    """Lower-case 'p' is not 'P'. An enum table means what it says."""
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00", "st": "p"}
        ),
        _status_profile(),
        context=pg_context,
    )

    assert IssueCode.UNKNOWN_ENUM_VALUE.value in result.issue_codes()


# ============================================================
# Normalization (Step 17)
# ============================================================

def test_normalization_does_nothing_by_default():
    policy = NormalizationPolicy()

    assert normalize_value("  AB-001  ", "customer_id", policy) == "  AB-001  "


def test_trim_applies_when_declared():
    policy = NormalizationPolicy(trim_strings=True)

    assert normalize_value("  AB-001  ", "customer_id", policy) == "AB-001"


def test_case_normalization_applies_when_declared():
    assert normalize_value(
        "AB-001", "x", NormalizationPolicy(case=CaseNormalization.LOWER)
    ) == "ab-001"


def test_business_identifiers_are_not_lowercased_globally():
    """The default must never mutate a primary key."""
    assert normalize_value(
        "AB-001", "customer_id", NormalizationPolicy()
    ) == "AB-001"


def test_normalization_can_be_scoped_to_named_fields():
    policy = NormalizationPolicy(
        trim_strings=True, apply_to_fields=("name",)
    )

    assert normalize_value("  x  ", "name", policy) == "x"
    assert normalize_value("  x  ", "customer_id", policy) == "  x  "


def test_internal_whitespace_collapses_only_when_declared():
    policy = NormalizationPolicy(collapse_internal_whitespace=True)

    assert normalize_value("a   b", "x", policy) == "a b"


def test_unicode_normalization_applies_when_declared():
    policy = NormalizationPolicy(unicode_form="NFC")
    decomposed = "é"

    assert normalize_value(decomposed, "x", policy) == "é"


def test_an_invalid_unicode_form_is_refused():
    from erp_pipeline.transformation.errors import TransformationConfigurationError

    with pytest.raises(TransformationConfigurationError):
        NormalizationPolicy(unicode_form="NFZ")


def test_normalization_leaves_non_strings_untouched():
    policy = NormalizationPolicy(trim_strings=True, case=CaseNormalization.UPPER)

    assert normalize_value(Decimal("1.50"), "amount", policy) == Decimal("1.50")
