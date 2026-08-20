"""CSV typing rules, exercised directly with plain strings.

``csv_inference`` is I/O-free by design, so every rule here is tested without
touching a file. The file-level behaviour is covered in
``test_csv_ingestion.py``.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ingestion.csv_inference import (
    CATEGORY_BOOLEAN,
    CATEGORY_DATE,
    CATEGORY_DATETIME,
    CATEGORY_DECIMAL,
    CATEGORY_EMPTY,
    CATEGORY_INTEGER,
    CATEGORY_NULL_MARKER,
    CATEGORY_STRING,
    CsvStructureInference,
    build_source_fields,
    classify_value,
    deduplicate_normalized_name,
    render_source_data_type,
    resolve_field_type,
)
from erp_pipeline.ingestion.models import CsvOptions
from erp_pipeline.schemas.enums import FieldDataType


# ============================================================
# Value classification
# ============================================================

@pytest.mark.parametrize(
    "text,expected",
    [
        ("", CATEGORY_EMPTY),
        ("   ", CATEGORY_EMPTY),
        ("true", CATEGORY_BOOLEAN),
        ("FALSE", CATEGORY_BOOLEAN),
        ("0", CATEGORY_INTEGER),
        ("42", CATEGORY_INTEGER),
        ("-17", CATEGORY_INTEGER),
        ("+8", CATEGORY_INTEGER),
        ("3.14", CATEGORY_DECIMAL),
        ("-0.5", CATEGORY_DECIMAL),
        ("1e6", CATEGORY_DECIMAL),
        ("2026-01-15", CATEGORY_DATE),
        ("2026/01/15", CATEGORY_DATE),
        ("2026-01-15T09:30:00", CATEGORY_DATETIME),
        ("2026-01-15 09:30:00", CATEGORY_DATETIME),
        ("2026-01-15T09:30:00Z", CATEGORY_DATETIME),
        ("hello", CATEGORY_STRING),
        ("INV-1001", CATEGORY_STRING),
    ],
)
def test_values_are_classified(text, expected):
    assert classify_value(text) == expected


@pytest.mark.parametrize("text", ["007", "0012", "00"])
def test_zero_padded_numbers_are_strings_not_integers(text):
    """A leading zero is significant - an account code, a country prefix - and
    parsing it as an int destroys information irreversibly."""
    assert classify_value(text) == CATEGORY_STRING


@pytest.mark.parametrize("text", ["1", "0", "yes", "no", "Y", "N"])
def test_only_true_and_false_are_read_as_booleans(text):
    """1/0 are far more often quantities, and yes/no is ordinary vocabulary."""
    assert classify_value(text) != CATEGORY_BOOLEAN


@pytest.mark.parametrize("text", ["03/04/2026", "3/4/26", "04-03-2026"])
def test_ambiguous_locale_dates_stay_strings(text):
    """03/04/2026 is two different dates depending on locale, and sampling
    cannot settle which."""
    assert classify_value(text) == CATEGORY_STRING


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_not_decimals(text):
    assert classify_value(text) == CATEGORY_STRING


def test_null_tokens_are_recognized_only_when_configured():
    assert classify_value("N/A") == CATEGORY_STRING
    assert classify_value("N/A", frozenset({"n/a"})) == CATEGORY_NULL_MARKER


def test_null_token_matching_can_be_case_sensitive():
    assert classify_value("null", frozenset({"NULL"}), case_insensitive=False) == (
        CATEGORY_STRING
    )


# ============================================================
# Type resolution (Step 15)
# ============================================================

@pytest.mark.parametrize(
    "counts,expected",
    [
        ({}, FieldDataType.UNKNOWN),
        ({CATEGORY_EMPTY: 5}, FieldDataType.UNKNOWN),
        ({CATEGORY_NULL_MARKER: 3}, FieldDataType.UNKNOWN),
        ({CATEGORY_INTEGER: 4}, FieldDataType.INTEGER),
        ({CATEGORY_DECIMAL: 4}, FieldDataType.DECIMAL),
        ({CATEGORY_BOOLEAN: 4}, FieldDataType.BOOLEAN),
        ({CATEGORY_DATE: 4}, FieldDataType.DATE),
        ({CATEGORY_DATETIME: 4}, FieldDataType.DATETIME),
        ({CATEGORY_STRING: 4}, FieldDataType.STRING),
        # compatible widening
        ({CATEGORY_INTEGER: 3, CATEGORY_DECIMAL: 1}, FieldDataType.DECIMAL),
        ({CATEGORY_DATE: 3, CATEGORY_DATETIME: 1}, FieldDataType.DATETIME),
        # incompatible -> STRING, which is true of every CSV cell
        ({CATEGORY_INTEGER: 3, CATEGORY_STRING: 1}, FieldDataType.STRING),
        ({CATEGORY_BOOLEAN: 3, CATEGORY_INTEGER: 1}, FieldDataType.STRING),
        ({CATEGORY_DATE: 1, CATEGORY_INTEGER: 1}, FieldDataType.STRING),
    ],
)
def test_type_resolution_policy(counts, expected):
    assert resolve_field_type(counts) is expected


def test_empties_never_dilute_an_otherwise_single_type():
    assert resolve_field_type(
        {CATEGORY_INTEGER: 2, CATEGORY_EMPTY: 50, CATEGORY_NULL_MARKER: 10}
    ) is FieldDataType.INTEGER


def test_source_data_type_rendering_is_independent_of_counts():
    """It feeds the structural hash, so it must depend only on WHICH
    categories were seen."""
    assert render_source_data_type({CATEGORY_INTEGER: 1, CATEGORY_STRING: 99}) == (
        render_source_data_type({CATEGORY_INTEGER: 99, CATEGORY_STRING: 1})
    )
    assert render_source_data_type({CATEGORY_INTEGER: 1, CATEGORY_STRING: 1}) == (
        "mixed<integer|string>"
    )


def test_a_column_of_only_empties_renders_as_empty():
    assert render_source_data_type({CATEGORY_EMPTY: 4}) == CATEGORY_EMPTY


# ============================================================
# Column accumulation
# ============================================================

def test_observations_count_presence_empties_and_categories():
    inference = CsvStructureInference(["id", "note"])
    inference.observe_all([["1", "x"], ["2", ""], ["3", "y"]])

    observations = {item.source_name: item for item in inference.observations()}
    note = observations["note"]

    assert note.rows_sampled == 3
    assert note.present_count == 3
    assert note.empty_count == 1
    assert note.value_count == 2
    assert note.observed_always_populated is False
    assert observations["id"].observed_always_populated is True


def test_a_short_row_leaves_the_missing_column_absent_not_empty():
    """"the column had no cell" and "the cell was blank" are different facts."""
    inference = CsvStructureInference(["a", "b", "c"])
    inference.observe(["1", "2"])

    observations = {item.source_name: item for item in inference.observations()}

    assert observations["c"].present_count == 0
    assert observations["c"].missing_count == 1
    assert observations["c"].empty_count == 0


def test_extra_values_beyond_the_header_are_ignored_for_typing():
    inference = CsvStructureInference(["a"])
    inference.observe(["1", "surplus"])

    observations = inference.observations()

    assert len(observations) == 1
    assert observations[0].category_counts == {CATEGORY_INTEGER: 1}


def test_max_observed_length_is_a_measurement_not_a_sample():
    inference = CsvStructureInference(["note"])
    inference.observe(["a longer piece of text"])

    observation = inference.observations()[0]

    assert observation.max_observed_length == len("a longer piece of text")
    assert "longer" not in str(observation.to_dict())


def test_column_identity_is_positional_so_duplicates_stay_separate():
    inference = CsvStructureInference(["amount", "amount"])
    inference.observe(["1", "two"])

    observations = inference.observations()

    assert len(observations) == 2
    assert observations[0].category_counts == {CATEGORY_INTEGER: 1}
    assert observations[1].category_counts == {CATEGORY_STRING: 1}


# ============================================================
# Field construction
# ============================================================

def test_fields_are_built_in_file_order_with_ordinals():
    inference = CsvStructureInference(["c", "a", "b"])
    inference.observe(["1", "2", "3"])

    fields = build_source_fields(inference.observations()).fields

    assert [field.source_name for field in fields] == ["c", "a", "b"]
    assert [field.ordinal for field in fields] == [0, 1, 2]


def test_no_key_uniqueness_or_semantics_are_ever_inferred():
    """Step 42: cust_no is a name, not a canonical customer identifier."""
    inference = CsvStructureInference(["cust_no", "email_addr", "total_amt"])
    inference.observe(["C1", "a@example.invalid", "10.50"])

    fields = build_source_fields(inference.observations()).fields

    assert all(field.is_primary_key is False for field in fields)
    assert all(field.is_unique is False for field in fields)
    assert all(field.semantic_type is None for field in fields)
    assert all(field.nested_path is None for field in fields)
    assert all(field.is_array is False for field in fields)


def test_types_are_inferred_but_meanings_are_not():
    inference = CsvStructureInference(["cust_no", "email_addr", "total_amt"])
    inference.observe_all([["C1", "a@example.invalid", "10.50"]])

    fields = {f.normalized_name: f for f in build_source_fields(
        inference.observations()).fields}

    assert fields["cust_no"].normalized_data_type is FieldDataType.STRING
    assert fields["email_addr"].normalized_data_type is FieldDataType.STRING
    assert fields["total_amt"].normalized_data_type is FieldDataType.DECIMAL


def test_field_metadata_carries_counts_but_no_values():
    inference = CsvStructureInference(["secret_col"])
    inference.observe(["SECRET_VALUE_123"])

    field = build_source_fields(inference.observations()).fields[0]

    assert "SECRET_VALUE_123" not in str(field.metadata)
    assert field.metadata["observed"]["rows_sampled"] == 1
    assert field.metadata["schema_claim"] == "observed"


def test_field_metadata_survives_the_phase_1_credential_denylist():
    """Phase 1 rejects metadata keys that merely CONTAIN 'token', 'secret' and
    similar. Phase 6's own keys must not collide with that blunt rule."""
    inference = CsvStructureInference(["password", "api_key", "token"])
    inference.observe(["a", "b", "c"])

    fields = build_source_fields(inference.observations()).fields

    # The column NAMES are structure and are reported...
    assert [field.source_name for field in fields] == [
        "password", "api_key", "token",
    ]
    # ...while no metadata KEY looks like a credential.
    for field in fields:
        assert all(
            marker not in key.lower()
            for key in field.metadata
            for marker in ("password", "secret", "token", "api_key")
        )


# ============================================================
# Name deduplication
# ============================================================

def test_deduplication_is_deterministic_and_order_driven():
    used: dict[str, int] = {}

    assert deduplicate_normalized_name("amount", used) == "amount"
    assert deduplicate_normalized_name("amount", used) == "amount.2"
    assert deduplicate_normalized_name("amount", used) == "amount.3"
    assert deduplicate_normalized_name("total", used) == "total"


def test_deduplication_avoids_colliding_with_an_existing_suffixed_name():
    used: dict[str, int] = {}

    assert deduplicate_normalized_name("amount.2", used) == "amount.2"
    assert deduplicate_normalized_name("amount", used) == "amount"
    assert deduplicate_normalized_name("amount", used) == "amount.3"


def test_an_unnameable_header_gets_a_deterministic_fallback():
    first = build_source_fields(
        CsvStructureInference(["###"]).observations()
    ).fields[0]
    second = build_source_fields(
        CsvStructureInference(["###"]).observations()
    ).fields[0]

    assert first.normalized_name == second.normalized_name
    assert first.normalized_name.startswith("column.")
    assert first.source_name == "###"


def test_the_options_object_rejects_a_nonsensical_budget():
    with pytest.raises(ValueError):
        CsvOptions(max_rows_for_schema_inference=0)

    with pytest.raises(ValueError):
        CsvOptions(delimiter=";;")
