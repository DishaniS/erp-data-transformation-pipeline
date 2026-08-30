"""CSV ingestion against real fixture files.

Covers encoding, delimiter detection, header handling, type inference, the
resulting Phase 1 ``SourceSchema``, streamed source rows, and every safety
budget. Nothing here is mocked: each test parses a real file from
``tests/fixtures/ingestion/``.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ingestion import (
    CsvOptions,
    ExtractionStatus,
    IngestionOptions,
    MalformedCSVError,
    TabularFileResult,
    detect_delimiter,
    detect_encoding,
    ingest_file,
)
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SchemaOrigin
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema


def ingest(path, **csv_kwargs):
    options = IngestionOptions(csv=CsvOptions(**csv_kwargs)) if csv_kwargs else None
    return ingest_file(path, options)


def fields_of(result) -> dict:
    return {field.normalized_name: field for field in result.schema.entities[0].fields}


# ============================================================
# Delimiters (Step 11)
# ============================================================

@pytest.mark.parametrize(
    "filename,expected",
    [
        ("normal.csv", ","),
        ("semicolon.csv", ";"),
        ("tab.csv", "\t"),
        ("pipe.csv", "|"),
    ],
)
def test_delimiters_are_detected(csv_fixtures, filename, expected):
    result = ingest(csv_fixtures / filename)

    assert result.provenance.delimiter == expected
    assert len(result.header) == 3 or len(result.header) == 5


def test_delimiter_detection_is_deterministic(csv_fixtures):
    path = csv_fixtures / "semicolon.csv"
    encoding = detect_encoding(path)

    first = detect_delimiter(path, encoding, CsvOptions())
    second = detect_delimiter(path, encoding, CsvOptions())

    assert first == second == (";", "detected")


def test_an_explicit_delimiter_overrides_detection(csv_fixtures):
    """Forcing the wrong delimiter proves the override is really applied."""
    result = ingest(csv_fixtures / "semicolon.csv", delimiter=",")

    assert result.provenance.delimiter == ","
    # One un-split column, because ';' was never treated as a separator.
    assert len(result.header) == 1


def test_delimiter_choice_is_recorded_in_metadata(csv_fixtures):
    result = ingest(csv_fixtures / "tab.csv")

    assert result.schema.entities[0].metadata["delimiter"] == "\t"


# ============================================================
# Encoding (Step 12)
# ============================================================

def test_plain_utf8_is_read(csv_fixtures):
    result = ingest(csv_fixtures / "normal.csv")

    assert result.provenance.encoding == "utf-8"


def test_utf8_bom_is_stripped_from_the_first_header(csv_fixtures):
    """A BOM left in place would corrupt the first column's name."""
    result = ingest(csv_fixtures / "utf8_bom.csv")

    assert result.provenance.encoding == "utf-8-sig"
    assert result.header[0] == "référence"
    assert not result.header[0].startswith("﻿")


def test_non_ascii_headers_and_values_survive(csv_fixtures):
    """The exact header is kept as ``source_name``; rows are keyed by the
    NORMALIZED name, since that is what the schema declares."""
    result = ingest(csv_fixtures / "utf8_bom.csv")

    assert result.header == ("référence", "société", "montant")
    fields = result.schema.entities[0].fields
    assert [field.source_name for field in fields] == [
        "référence", "société", "montant",
    ]
    assert [field.normalized_name for field in fields] == [
        "r_f_rence", "soci_t", "montant",
    ]

    values = [row.values for row in result.iter_records()]
    assert values[0]["soci_t"] == "Café Müller"
    assert values[1]["soci_t"] == "Ångström AB"


def test_an_explicit_encoding_is_honoured(csv_fixtures):
    result = ingest(csv_fixtures / "normal.csv", encoding="utf-8")

    assert result.provenance.encoding == "utf-8"


def test_undecodable_bytes_produce_a_controlled_error(tmp_path):
    """Never a silent replacement-character decode presented as success."""
    path = tmp_path / "latin1.csv"
    path.write_bytes("id,name\n1,Mull\xe9r\n".encode("latin-1"))

    with pytest.raises(MalformedCSVError) as excinfo:
        ingest(path)

    assert excinfo.value.byte_offset is not None
    assert "utf-8" in str(excinfo.value)


def test_a_utf16_bom_is_reported_rather_than_guessed(tmp_path):
    """UTF-16 is a text file, so it passes detection - and is then refused by
    the reader with an actionable message rather than being decoded blindly."""
    path = tmp_path / "utf16.csv"
    path.write_bytes("id,name\n1,x\n".encode("utf-16"))

    with pytest.raises(MalformedCSVError, match="UTF-16"):
        ingest(path)


def test_utf16_can_be_read_once_the_caller_states_the_encoding(tmp_path):
    path = tmp_path / "utf16.csv"
    path.write_bytes("id,name\n1,Ångström\n".encode("utf-16"))

    result = ingest(path, encoding="utf-16")

    assert result.header == ("id", "name")
    assert [row.values["name"] for row in result.iter_records()] == ["Ångström"]


# ============================================================
# Headers (Step 10)
# ============================================================

def test_quoted_headers_and_embedded_separators_are_preserved(csv_fixtures):
    result = ingest(csv_fixtures / "quoted.csv")

    assert result.header == ("invoice no", "customer, legal name", "notes")
    names = [field.source_name for field in result.schema.entities[0].fields]
    assert "customer, legal name" in names


def test_quoted_values_containing_newlines_and_quotes_are_read(csv_fixtures):
    rows = list(ingest(csv_fixtures / "quoted.csv").iter_records())

    assert len(rows) == 2
    assert "line one\nline two" == rows[0].values["notes"]
    assert 'says "urgent" twice' == rows[1].values["notes"]


def test_duplicate_headers_are_all_kept_and_deterministically_named(csv_fixtures):
    """Losing a duplicate column would silently drop a column of data."""
    result = ingest(csv_fixtures / "duplicate_headers.csv")
    fields = result.schema.entities[0].fields

    assert len(fields) == 4
    assert [field.source_name for field in fields] == [
        "amount", "Amount", "amount", "total",
    ]
    normalized = [field.normalized_name for field in fields]
    assert normalized == ["amount", "amount.2", "amount.3", "total"]
    assert len(set(normalized)) == 4


def test_duplicate_header_naming_is_stable_across_runs(csv_fixtures):
    first = ingest(csv_fixtures / "duplicate_headers.csv")
    second = ingest(csv_fixtures / "duplicate_headers.csv")

    assert [f.normalized_name for f in first.schema.entities[0].fields] == [
        f.normalized_name for f in second.schema.entities[0].fields
    ]


def test_each_duplicate_column_keeps_its_own_values(csv_fixtures):
    rows = list(ingest(csv_fixtures / "duplicate_headers.csv").iter_records())

    assert rows[0].values == {
        "amount": "1", "amount.2": "2", "amount.3": "3", "total": "6",
    }


def test_a_blank_header_cell_becomes_a_positional_name(csv_fixtures):
    result = ingest(csv_fixtures / "blank_header.csv")
    fields = result.schema.entities[0].fields

    assert [field.normalized_name for field in fields] == ["id", "column_2", "total"]
    assert any(w.category == "header_normalization" for w in result.warnings)


def test_headers_with_spaces_and_punctuation_normalize_without_loss(tmp_path):
    path = tmp_path / "messy.csv"
    path.write_text("Invoice No.,Customer Name,Total (USD)\n1,x,2\n", encoding="utf-8")

    fields = ingest(path).schema.entities[0].fields

    assert [f.source_name for f in fields] == [
        "Invoice No.", "Customer Name", "Total (USD)",
    ]
    assert [f.normalized_name for f in fields] == [
        "invoice_no.", "customer_name", "total_usd",
    ]


def test_headerless_files_get_positional_columns(csv_fixtures):
    result = ingest(csv_fixtures / "normal.csv", has_header=False)
    fields = result.schema.entities[0].fields

    assert [field.normalized_name for field in fields] == [
        "column_1", "column_2", "column_3", "column_4", "column_5",
    ]
    # The first physical line is data, not a header.
    assert result.rows_sampled == 4


# ============================================================
# Type inference (Steps 14, 15)
# ============================================================

def test_common_types_are_inferred(csv_fixtures):
    fields = fields_of(ingest(csv_fixtures / "normal.csv"))

    assert fields["invoice_no"].normalized_data_type is FieldDataType.STRING
    assert fields["customer"].normalized_data_type is FieldDataType.STRING
    assert fields["amount"].normalized_data_type is FieldDataType.DECIMAL
    assert fields["issued_on"].normalized_data_type is FieldDataType.DATE
    assert fields["approved"].normalized_data_type is FieldDataType.BOOLEAN


def test_integer_and_decimal_widen_to_decimal(csv_fixtures):
    """1500 and 2750.50 in one column: DECIMAL is true of both."""
    amount = fields_of(ingest(csv_fixtures / "normal.csv"))["amount"]

    assert amount.normalized_data_type is FieldDataType.DECIMAL
    assert amount.source_data_type == "mixed<decimal|integer>"


def test_incompatible_types_fall_back_to_string(csv_fixtures):
    """100 / 200.25 / N/A - STRING is the only claim true of every value."""
    amount = fields_of(ingest(csv_fixtures / "mixed_types.csv"))["amount"]

    assert amount.normalized_data_type is FieldDataType.STRING
    assert amount.source_data_type == "mixed<decimal|integer|string>"
    assert amount.metadata["mixed_types"] is True


def test_zero_padded_codes_stay_strings(csv_fixtures):
    """007 is an account code, not the number seven."""
    code = fields_of(ingest(csv_fixtures / "mixed_types.csv"))["code"]

    assert code.normalized_data_type is FieldDataType.STRING


def test_date_and_datetime_widen_to_datetime(csv_fixtures):
    when = fields_of(ingest(csv_fixtures / "mixed_types.csv"))["when"]

    assert when.normalized_data_type is FieldDataType.DATETIME
    assert when.source_data_type == "mixed<date|datetime>"


def test_a_pure_integer_column_stays_integer(csv_fixtures):
    quantity = fields_of(ingest(csv_fixtures / "mixed_types.csv"))["quantity"]

    assert quantity.normalized_data_type is FieldDataType.INTEGER
    assert quantity.source_data_type == "integer"


def test_the_category_distribution_is_preserved(csv_fixtures):
    amount = fields_of(ingest(csv_fixtures / "mixed_types.csv"))["amount"]

    assert amount.metadata["value_category_distribution"] == {
        "decimal": 1, "integer": 1, "string": 1,
    }


# ============================================================
# Empty values and null tokens (Step 16)
# ============================================================

def test_empty_values_are_counted_and_make_a_column_nullable(csv_fixtures):
    note = fields_of(ingest(csv_fixtures / "nulls.csv"))["note"]

    assert note.metadata["observed"]["empty_count"] == 1
    assert note.nullable is True
    assert note.required is False


def test_textual_null_tokens_are_not_assumed_by_default(csv_fixtures):
    """"NULL" and "n/a" are ordinary strings unless configured otherwise."""
    note = fields_of(ingest(csv_fixtures / "nulls.csv"))["note"]

    assert note.metadata["observed"]["null_marker_count"] == 0
    assert note.normalized_data_type is FieldDataType.STRING
    assert note.metadata["value_category_distribution"] == {"string": 2}


def test_configured_null_tokens_are_honoured(csv_fixtures):
    result = ingest(csv_fixtures / "nulls.csv", null_tokens=["NULL", "n/a"])
    note = fields_of(result)["note"]

    assert note.metadata["observed"]["null_marker_count"] == 2
    assert note.metadata["observed"]["empty_count"] == 1
    # No non-null values were ever observed, so no type can be claimed.
    assert note.normalized_data_type is FieldDataType.UNKNOWN


def test_null_token_matching_can_be_case_sensitive(csv_fixtures):
    result = ingest(
        csv_fixtures / "nulls.csv",
        null_tokens=["NULL"],
        case_insensitive_null_tokens=False,
    )

    assert fields_of(result)["note"].metadata["observed"]["null_marker_count"] == 1


def test_a_fully_populated_column_is_required(csv_fixtures):
    fields = fields_of(ingest(csv_fixtures / "normal.csv"))

    assert fields["invoice_no"].required is True
    assert fields["invoice_no"].nullable is False


# ============================================================
# Malformed rows and budgets (Step 13)
# ============================================================

def test_malformed_rows_are_reported_but_do_not_abort(csv_fixtures):
    result = ingest(csv_fixtures / "malformed.csv")

    categories = [warning.category for warning in result.warnings]
    assert categories.count("row_width_mismatch") == 2
    rows = [w.row_number for w in result.warnings if w.category == "row_width_mismatch"]
    assert rows == [2, 3]
    # The good rows still produced a schema.
    assert len(result.schema.entities[0].fields) == 3


def test_short_and_long_rows_are_visible_on_the_source_row(csv_fixtures):
    rows = list(ingest(csv_fixtures / "malformed.csv").iter_records())

    assert rows[1].missing_fields == ("c",)
    assert rows[1].is_complete is False
    assert rows[2].extra_value_count == 1
    assert rows[0].is_complete is True


def test_too_many_malformed_rows_stops_ingestion(tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text("a,b,c\n" + "1,2\n" * 20, encoding="utf-8")

    with pytest.raises(MalformedCSVError, match="malformed rows"):
        ingest(path, max_errors=5)


def test_too_many_columns_is_rejected(tmp_path):
    path = tmp_path / "wide.csv"
    path.write_text(",".join(f"c{i}" for i in range(50)) + "\n", encoding="utf-8")

    with pytest.raises(MalformedCSVError, match="columns"):
        ingest(path, max_columns=10)


def test_an_oversized_field_is_bounded_during_inference(tmp_path):
    """Inference reads a sample, so one unreadable row is warned about and
    skipped rather than failing the whole file."""
    path = tmp_path / "huge_field.csv"
    path.write_text("a,b\n1,2\n" + "x" * 5000 + ",2\n", encoding="utf-8")

    result = ingest(path, max_field_length=100)

    assert any(warning.category == "malformed_row" for warning in result.warnings)
    assert result.schema.entities[0].fields  # the good row still described it


def test_an_oversized_field_raises_when_streaming_the_data(tmp_path):
    """The data handoff must never silently drop a row: losing business data
    without telling anyone is worse than failing."""
    path = tmp_path / "huge_field.csv"
    path.write_text("a,b\n1,2\n" + "x" * 5000 + ",2\n", encoding="utf-8")

    result = ingest(path, max_field_length=100)

    with pytest.raises(MalformedCSVError) as excinfo:
        list(result.iter_records())

    assert excinfo.value.row_number == 2
    # The position is named; the offending content is not.
    assert "x" * 50 not in str(excinfo.value)


def test_schema_inference_is_bounded_by_the_row_sample(tmp_path):
    path = tmp_path / "many.csv"
    path.write_text(
        "id,amount\n" + "".join(f"{i},{i}\n" for i in range(5000)), encoding="utf-8"
    )

    result = ingest(path, max_rows_for_schema_inference=50)

    assert result.rows_sampled == 50
    assert result.schema.entities[0].metadata["sample"]["full_scan"] is False
    # The full file is still readable.
    assert sum(1 for _ in result.iter_records()) == 5000


# ============================================================
# Empty and header-only files
# ============================================================

def test_an_empty_file_is_reported_not_guessed_at(csv_fixtures):
    result = ingest(csv_fixtures / "empty.csv")

    assert result.status is ExtractionStatus.NO_CONTENT_DETECTED
    assert result.schema.entities[0].fields == ()
    assert any(warning.category == "empty_file" for warning in result.warnings)


def test_a_header_only_file_yields_columns_but_no_rows(csv_fixtures):
    result = ingest(csv_fixtures / "header_only.csv")

    assert result.status is ExtractionStatus.NO_CONTENT_DETECTED
    assert [f.normalized_name for f in result.schema.entities[0].fields] == [
        "col_a", "col_b", "col_c",
    ]
    assert result.rows_sampled == 0
    assert list(result.iter_records()) == []


def test_columns_with_no_observed_values_are_unknown_not_guessed(csv_fixtures):
    fields = fields_of(ingest(csv_fixtures / "header_only.csv"))

    assert all(
        field.normalized_data_type is FieldDataType.UNKNOWN
        for field in fields.values()
    )


# ============================================================
# SourceSchema output (Steps 17, 18)
# ============================================================

def test_a_csv_produces_the_phase_1_contract(csv_fixtures):
    result = ingest(csv_fixtures / "normal.csv")

    assert isinstance(result, TabularFileResult)
    assert isinstance(result.schema, SourceSchema)
    assert all(isinstance(e, SourceEntity) for e in result.schema.entities)
    assert all(
        isinstance(f, SourceField)
        for e in result.schema.entities
        for f in e.fields
    )


def test_the_schema_is_marked_inferred_not_discovered(csv_fixtures):
    """A CSV has no catalog to discover; its structure is read off the rows."""
    schema = ingest(csv_fixtures / "normal.csv").schema

    assert schema.origin is SchemaOrigin.INFERRED
    assert schema.metadata["schema_claim"] == "observed"


def test_one_csv_becomes_one_dataset_entity(csv_fixtures):
    """The entity is named for the DATASET, not for the file that carried it.

    ``source_name`` previously kept the ".csv" suffix, which Phase 8's entity
    evidence then matched against the canonical model - so "invoices.csv" failed
    to match "invoice" cleanly and pushed mappable fields into AMBIGUOUS. The
    suffix is provenance, not semantics, and is asserted below to still be
    preserved on the provenance record.
    """
    result = ingest(csv_fixtures / "normal.csv")
    schema = result.schema

    assert len(schema.entities) == 1
    entity = schema.entities[0]
    assert entity.entity_kind is EntityKind.DATASET
    assert entity.normalized_name == "normal"
    assert entity.source_name == "normal"

    # The filename itself is not lost - it remains the file's provenance.
    assert result.provenance.original_filename == "normal.csv"
    assert entity.metadata["source_filename"] == "normal.csv"


def test_no_primary_key_is_invented(csv_fixtures):
    """A CSV declares no key, and a distinct-looking column is not one."""
    entity = ingest(csv_fixtures / "normal.csv").schema.entities[0]

    assert entity.primary_key_fields == ()
    assert not any(field.is_primary_key for field in entity.fields)
    assert not any(field.is_unique for field in entity.fields)


def test_no_relationships_are_invented(csv_fixtures):
    assert ingest(csv_fixtures / "normal.csv").schema.relationships == ()


def test_field_order_matches_the_file(csv_fixtures):
    fields = ingest(csv_fixtures / "normal.csv").schema.entities[0].fields

    assert [field.ordinal for field in fields] == [0, 1, 2, 3, 4]
    assert [field.normalized_name for field in fields] == [
        "invoice_no", "customer", "amount", "issued_on", "approved",
    ]


def test_the_schema_hash_is_deterministic(csv_fixtures):
    first = ingest(csv_fixtures / "normal.csv").schema
    second = ingest(csv_fixtures / "normal.csv").schema

    assert first.compute_schema_hash() == second.compute_schema_hash()
    assert first.schema_id == second.schema_id


def test_a_new_column_changes_the_schema_hash(csv_fixtures, tmp_path):
    baseline = ingest(csv_fixtures / "normal.csv").schema

    extended = tmp_path / "normal.csv"
    extended.write_text(
        "invoice_no,customer,amount,issued_on,approved,currency\n"
        "INV-1001,Acme Supplies,1500,2026-01-15,true,USD\n",
        encoding="utf-8",
    )
    changed = ingest(extended).schema

    assert changed.compute_schema_hash() != baseline.compute_schema_hash()
    assert changed.schema_id != baseline.schema_id
    # Same logical scope, so the catalog can version them together.
    assert changed.schema_name == baseline.schema_name


def test_a_type_change_changes_the_schema_hash(tmp_path):
    numeric = tmp_path / "d.csv"
    numeric.write_text("id,amount\n1,100\n2,200\n", encoding="utf-8")
    first = ingest(numeric).schema.compute_schema_hash()

    numeric.write_text("id,amount\n1,one hundred\n2,two hundred\n", encoding="utf-8")
    second = ingest(numeric).schema.compute_schema_hash()

    assert first != second


def test_the_sample_size_alone_does_not_change_the_hash(tmp_path):
    """Row counts live in unhashed metadata, so widening the sample over a
    uniform file must not look like a schema change."""
    path = tmp_path / "uniform.csv"
    path.write_text(
        "id,amount\n" + "".join(f"{i},{i}.5\n" for i in range(1, 200)),
        encoding="utf-8",
    )

    small = ingest(path, max_rows_for_schema_inference=10).schema
    large = ingest(path, max_rows_for_schema_inference=150).schema

    assert small.compute_schema_hash() == large.compute_schema_hash()


def test_the_filename_scopes_the_schema_but_content_identifies_the_file(
    csv_fixtures, tmp_path
):
    """The two answer different questions and must not be conflated."""
    renamed = tmp_path / "renamed.csv"
    renamed.write_bytes((csv_fixtures / "normal.csv").read_bytes())

    original = ingest(csv_fixtures / "normal.csv")
    copy = ingest(renamed)

    # Same bytes -> same file identity.
    assert original.file.content_hash == copy.file.content_hash
    # Different dataset name -> different schema scope.
    assert original.schema.schema_name != copy.schema.schema_name


# ============================================================
# Source rows (Step 20)
# ============================================================

def test_source_rows_preserve_raw_values_unconverted(csv_fixtures):
    rows = list(ingest(csv_fixtures / "normal.csv").iter_records())

    assert len(rows) == 3
    assert rows[0].values["amount"] == "1500"          # still a string
    assert rows[1].values["amount"] == "2750.50"       # precision intact
    assert rows[0].values["approved"] == "true"


def test_source_rows_carry_their_position_and_file_identity(csv_fixtures):
    result = ingest(csv_fixtures / "normal.csv")
    rows = list(result.iter_records())

    assert [row.row_number for row in rows] == [1, 2, 3]
    assert all(row.file_id == result.file.file_id for row in rows)


def test_iteration_is_repeatable_and_lazy(csv_fixtures):
    result = ingest(csv_fixtures / "normal.csv")

    first = [row.values for row in result.iter_records()]
    second = [row.values for row in result.iter_records()]

    assert first == second
    import types
    assert isinstance(result.iter_records(), types.GeneratorType)


def test_rows_are_not_held_on_the_result_object(csv_fixtures):
    """Streaming is the point: the result must not be a second copy."""
    result = ingest(csv_fixtures / "normal.csv")

    assert "1500" not in str(result.to_dict())
    assert "Acme Supplies" not in str(result.to_dict())
