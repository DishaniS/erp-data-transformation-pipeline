"""Step 39: source values survive ingestion, but never leak into operations.

Phase 6 is the first part of this framework that deliberately RETAINS source
values - a mapping phase cannot transform data it never received. That makes
the privacy question sharper than in Phase 4/5, where the answer was simply
"no values anywhere":

    source values MUST be available in    iter_records(), page text
    source values MUST NOT appear in      schemas, warnings, exceptions,
                                          logs, summary serializations

Every test here plants a synthetic sentinel in real file content and checks
both halves of that rule.
"""

from __future__ import annotations

import json
import logging

import pytest

from erp_pipeline.ingestion import (
    CsvOptions,
    ImageOptions,
    IngestionOptions,
    MalformedCSVError,
    PdfOptions,
    ingest_file,
    probe_ocr,
)

from tests.erp_pipeline.ingestion.conftest import (
    SENTINEL_EMAIL,
    SENTINEL_IBAN,
    SENTINEL_INVOICE,
    SENTINELS,
)


def assert_no_sentinels(payload: str, context: str) -> None:
    for sentinel in SENTINELS:
        assert sentinel not in payload, f"{context} leaked {sentinel!r}"


# ============================================================
# CSV: values available where required
# ============================================================

def test_csv_source_values_are_available_for_later_mapping(csv_fixtures):
    """The positive half of the rule. Without this the phase is useless."""
    result = ingest_file(csv_fixtures / "sentinels.csv")
    rows = list(result.iter_records())

    assert rows[0].values["customer_email"] == SENTINEL_EMAIL
    assert rows[0].values["iban"] == SENTINEL_IBAN
    assert rows[0].values["invoice_ref"] == SENTINEL_INVOICE


def test_a_source_row_serializes_its_values_when_asked(csv_fixtures):
    row = next(ingest_file(csv_fixtures / "sentinels.csv").iter_records())

    assert SENTINEL_EMAIL in json.dumps(row.to_dict())
    assert SENTINEL_EMAIL not in json.dumps(row.to_dict(include_values=False))


# ============================================================
# CSV: values absent from everything operational
# ============================================================

def test_csv_values_never_reach_the_schema(csv_fixtures):
    schema = ingest_file(csv_fixtures / "sentinels.csv").schema

    assert_no_sentinels(json.dumps(schema.to_json_dict()), "SourceSchema")


def test_csv_values_never_reach_field_metadata(csv_fixtures):
    schema = ingest_file(csv_fixtures / "sentinels.csv").schema

    for field in schema.entities[0].fields:
        assert_no_sentinels(json.dumps(dict(field.metadata)), "field metadata")


def test_csv_values_never_reach_the_result_summary(csv_fixtures):
    result = ingest_file(csv_fixtures / "sentinels.csv")

    assert_no_sentinels(json.dumps(result.to_dict()), "result.to_dict()")


def test_column_names_are_reported_even_though_values_are_not(csv_fixtures):
    """The distinction the whole phase rests on: a column CALLED iban is
    structure and must be described; the IBAN itself must not be."""
    schema = ingest_file(csv_fixtures / "sentinels.csv").schema
    names = [field.source_name for field in schema.entities[0].fields]

    assert names == ["customer_email", "iban", "invoice_ref", "amount"]
    assert_no_sentinels(json.dumps(schema.to_json_dict()), "SourceSchema")


def test_csv_values_never_reach_warnings(tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text(
        f"a,b,c\n{SENTINEL_EMAIL},{SENTINEL_IBAN}\n"
        f"{SENTINEL_INVOICE},2,3,4\n",
        encoding="utf-8",
    )

    result = ingest_file(path)

    assert result.warnings
    assert_no_sentinels(
        json.dumps([w.to_dict() for w in result.warnings]), "warnings"
    )
    # The position is what identifies the problem, and a position is not
    # sensitive.
    assert {w.row_number for w in result.warnings} == {1, 2}


def test_csv_values_never_reach_an_exception_message(tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text(
        "a,b,c\n" + "".join(f"{SENTINEL_IBAN},{i}\n" for i in range(20)),
        encoding="utf-8",
    )

    with pytest.raises(MalformedCSVError) as excinfo:
        ingest_file(path, IngestionOptions(csv=CsvOptions(max_errors=3)))

    assert_no_sentinels(str(excinfo.value), "MalformedCSVError")
    assert_no_sentinels(repr(excinfo.value), "MalformedCSVError repr")


def test_an_undecodable_file_error_reports_an_offset_not_content(tmp_path):
    path = tmp_path / "latin1.csv"
    path.write_bytes(
        f"id,name\n1,{SENTINEL_EMAIL}\xe9\n".encode("latin-1")
    )

    with pytest.raises(MalformedCSVError) as excinfo:
        ingest_file(path)

    assert_no_sentinels(str(excinfo.value), "decode error")
    assert excinfo.value.byte_offset is not None


def test_nothing_is_logged_during_csv_ingestion(csv_fixtures, caplog):
    """Never log the first N characters of anything - that is exactly the
    'helpful' log line that leaks a customer record."""
    with caplog.at_level(logging.DEBUG):
        result = ingest_file(csv_fixtures / "sentinels.csv")
        list(result.iter_records())

    assert_no_sentinels(caplog.text, "log output")


# ============================================================
# PDF
# ============================================================

def test_pdf_text_is_available_but_absent_from_the_summary(binary_fixtures):
    pytest.importorskip("fitz")

    result = ingest_file(binary_fixtures / "sentinels.pdf")

    # Available as content...
    assert SENTINEL_EMAIL in result.document.document_text
    assert SENTINEL_IBAN in result.document.pages[0].text

    # ...and withheld from the operational payload.
    assert_no_sentinels(json.dumps(result.to_dict()), "DocumentFileResult.to_dict()")
    assert_no_sentinels(
        json.dumps(result.document.to_dict()), "ExtractedDocument.to_dict()"
    )
    assert_no_sentinels(json.dumps(result.provenance.to_dict()), "provenance")


def test_pdf_text_is_serialized_only_on_explicit_request(binary_fixtures):
    pytest.importorskip("fitz")

    result = ingest_file(binary_fixtures / "sentinels.pdf")

    payload = json.dumps(result.to_dict(include_text=True))
    assert SENTINEL_EMAIL in payload


def test_a_pdf_title_is_treated_as_content_not_metadata(binary_fixtures):
    """A document title routinely carries a customer or project name."""
    pytest.importorskip("fitz")

    result = ingest_file(binary_fixtures / "sentinels.pdf")

    assert result.document.document_metadata["title"] == SENTINEL_INVOICE
    assert_no_sentinels(json.dumps(result.to_dict()), "document metadata in summary")


def test_pdf_warnings_carry_no_extracted_text(binary_fixtures):
    pytest.importorskip("fitz")

    result = ingest_file(
        binary_fixtures / "sentinels.pdf",
        IngestionOptions(pdf=PdfOptions(max_text_chars_per_page=5)),
    )

    assert result.warnings
    assert_no_sentinels(
        json.dumps([w.to_dict() for w in result.warnings]), "PDF warnings"
    )


def test_nothing_is_logged_during_pdf_ingestion(binary_fixtures, caplog):
    pytest.importorskip("fitz")

    with caplog.at_level(logging.DEBUG):
        ingest_file(binary_fixtures / "sentinels.pdf")

    assert_no_sentinels(caplog.text, "log output")


# ============================================================
# Image OCR (Step 28)
# ============================================================

@pytest.mark.skipif(not probe_ocr().available, reason="Tesseract is not installed")
def test_ocr_text_is_available_but_never_leaks(binary_fixtures, caplog):
    with caplog.at_level(logging.DEBUG):
        result = ingest_file(binary_fixtures / "sentinels.png")

    recognized = result.document.document_text

    # OCR of a rendered sentinel is imperfect, so match on a stable prefix
    # rather than the whole string.
    assert "SECRET" in recognized.upper()

    assert_no_sentinels(json.dumps(result.to_dict()), "image result summary")
    assert_no_sentinels(caplog.text, "log output")
    assert "SECRET" not in caplog.text.upper()


def test_ocr_failure_messages_name_the_engine_not_the_content(
    binary_fixtures, monkeypatch
):
    from erp_pipeline.ingestion import image_ingestion
    from erp_pipeline.ingestion import ocr as ocr_module

    monkeypatch.setattr(
        image_ingestion,
        "probe_ocr",
        lambda *_: ocr_module.OcrCapability(
            available=True, engine="tesseract", version="5.0.0"
        ),
    )
    monkeypatch.setattr(
        image_ingestion,
        "run_ocr",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("The OCR engine failed (SimulatedError).")
        ),
    )

    result = ingest_file(binary_fixtures / "sentinels.png")

    assert_no_sentinels(
        json.dumps([w.to_dict() for w in result.warnings]), "OCR failure warning"
    )


def test_the_ocr_capability_report_hides_the_local_executable_path():
    """The resolved command is a workstation path, so only its presence is
    reported in the portable payload."""
    payload = probe_ocr().to_dict()

    assert "command" not in payload
    assert isinstance(payload["command_configured"], bool)


# ============================================================
# Structural guarantees
# ============================================================

def test_the_observation_model_cannot_hold_a_value(csv_fixtures):
    """Not just an assertion about one run: every FieldObservation attribute
    is a count, a ratio, a column name or an index."""
    from erp_pipeline.ingestion.models import FieldObservation

    result = ingest_file(csv_fixtures / "sentinels.csv")

    for observation in result.observations:
        assert isinstance(observation, FieldObservation)
        for name, value in vars(observation).items():
            if name == "source_name":
                continue  # a column name IS the structure
            if name == "category_counts":
                assert all(isinstance(count, int) for count in value.values())
                continue
            assert isinstance(value, (int, bool)), f"{name} can hold {type(value)}"


def test_image_metadata_is_dimensions_not_pixels(binary_fixtures):
    result = ingest_file(
        binary_fixtures / "sentinels.png",
        IngestionOptions(image=ImageOptions(ocr_enabled=False)),
    )

    properties = result.document.document_metadata
    assert set(properties) == {"format", "mode", "width", "height", "frame_count"}
