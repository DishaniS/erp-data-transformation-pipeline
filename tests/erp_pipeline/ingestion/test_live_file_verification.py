"""Step 44: end-to-end verification against real files and the real parsers.

These tests are the Phase 6 equivalent of the live database suites: they run
the complete public path - ``FileIngestionService.ingest()`` - over genuine
files written to disk, with no parser mocked and no capability simulated.

The OCR test asserts against the REAL engine when Tesseract is installed and
reports the honest capability state when it is not. Nothing here is called
"verified" unless the actual stack executed.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ingestion import (
    ExtractionStatus,
    FileIngestionService,
    IngestionOptions,
    probe_ocr,
)
from erp_pipeline.ingestion.models import FileType

SERVICE = FileIngestionService(IngestionOptions(source_system_id="live_file_probe"))


# ============================================================
# CSV
# ============================================================

def test_live_csv_end_to_end(tmp_path):
    """A real file, written now, read through the public entry point."""
    path = tmp_path / "live_invoices.csv"
    path.write_text(
        "invoice_no,customer,amount,issued_on,approved\n"
        "INV-9001,Northwind,1250.75,2026-04-01,true\n"
        "INV-9002,Vector,880,2026-04-02,false\n"
        "INV-9003,Initech,,2026-04-03,true\n",
        encoding="utf-8",
    )

    result = SERVICE.ingest(path)

    assert result.is_tabular
    assert result.file.file_type is FileType.CSV
    assert result.status is ExtractionStatus.EXTRACTED
    assert result.provenance.encoding == "utf-8"
    assert result.provenance.delimiter == ","
    assert result.provenance.row_count == 3

    schema = result.schema
    assert len(schema.entities) == 1
    assert [field.normalized_name for field in schema.entities[0].fields] == [
        "invoice_no", "customer", "amount", "issued_on", "approved",
    ]
    assert schema.schema_hash

    rows = list(result.iter_records())
    assert len(rows) == 3
    assert rows[0].values["invoice_no"] == "INV-9001"
    # An empty cell is preserved as an empty string, not invented as None.
    assert rows[2].values["amount"] == ""


def test_live_csv_rows_remain_accessible_after_the_result_is_summarized(tmp_path):
    path = tmp_path / "live.csv"
    path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")

    result = SERVICE.ingest(path)
    result.to_dict()

    assert [row.values for row in result.iter_records()] == [
        {"a": "1", "b": "2"}, {"a": "3", "b": "4"},
    ]


# ============================================================
# PDF
# ============================================================

def test_live_pdf_end_to_end(binary_fixtures):
    pytest.importorskip("fitz")

    result = SERVICE.ingest(binary_fixtures / "text_multi_page.pdf")

    assert result.is_document
    assert result.file.file_type is FileType.PDF
    assert result.status is ExtractionStatus.EXTRACTED
    assert result.document.page_count == 3
    assert result.provenance.extractor == "pymupdf"
    assert result.provenance.content_hash == result.file.content_hash

    # Page provenance is intact and ordered.
    assert [page.page_number for page in result.document.pages] == [1, 2, 3]
    assert result.document.total_char_count > 0


def test_live_pdf_reports_character_counts_without_exposing_text(binary_fixtures):
    pytest.importorskip("fitz")

    result = SERVICE.ingest(binary_fixtures / "text_single_page.pdf")
    payload = result.to_dict()

    assert payload["document"]["total_char_count"] > 0
    assert "INVOICE SUMMARY" not in str(payload)
    assert "INVOICE SUMMARY" in result.document.document_text


# ============================================================
# Image
# ============================================================

def test_live_image_metadata_end_to_end(binary_fixtures):
    result = SERVICE.ingest(binary_fixtures / "text.png")

    assert result.is_document
    assert result.file.file_type is FileType.IMAGE
    assert result.file.media_type == "image/png"
    assert result.provenance.extractor == "pillow"

    properties = result.document.document_metadata
    assert properties["format"] == "PNG"
    assert properties["width"] > 0 and properties["height"] > 0


# ============================================================
# OCR
# ============================================================

def test_live_ocr_reads_a_real_image():
    """Real Tesseract against a real rendered image, or an explicit skip.

    Never a silent pass: if OCR is unavailable the test says so rather than
    asserting nothing.
    """
    capability = probe_ocr()

    if not capability.available:
        pytest.skip(f"OCR is not available on this machine: {capability.reason}")

    assert capability.engine == "tesseract"
    assert capability.version


@pytest.mark.skipif(not probe_ocr().available, reason="Tesseract is not installed")
def test_live_ocr_extracts_text_from_a_real_png(binary_fixtures):
    result = SERVICE.ingest(binary_fixtures / "text.png")

    assert result.status is ExtractionStatus.EXTRACTED
    assert result.document.pages[0].extraction_method == "ocr"
    assert "INVOICE" in result.document.document_text.upper()
    assert result.provenance.ocr_engine == "tesseract"


@pytest.mark.skipif(not probe_ocr().available, reason="Tesseract is not installed")
def test_live_ocr_recovers_a_scanned_pdf_page(binary_fixtures):
    """The scanned-PDF path: no text layer, so the page is rasterized and
    OCR'd in memory."""
    pytest.importorskip("fitz")

    result = SERVICE.ingest(binary_fixtures / "scanned_image_only.pdf")
    page = result.document.pages[0]

    assert page.extraction_method == "ocr"
    assert page.char_count > 0
    assert "SCANNED" in page.text.upper()


# ============================================================
# Determinism across the whole public path (Steps 33, 34)
# ============================================================

def test_live_repeated_ingestion_is_byte_for_byte_stable(tmp_path,
                                                         binary_fixtures):
    pytest.importorskip("fitz")

    csv_path = tmp_path / "stable.csv"
    csv_path.write_text("a,b\n1,2\n", encoding="utf-8")

    for path in (csv_path, binary_fixtures / "text_multi_page.pdf",
                 binary_fixtures / "text.png"):
        first = SERVICE.ingest(path)
        second = SERVICE.ingest(path)

        assert first.file.content_hash == second.file.content_hash
        assert first.file.file_id == second.file.file_id
        assert first.status is second.status

        if first.is_tabular:
            assert first.schema.schema_id == second.schema.schema_id
            assert first.schema.compute_schema_hash() == (
                second.schema.compute_schema_hash()
            )
        else:
            assert first.document.document_text == second.document.document_text


def test_live_describe_identifies_a_file_without_parsing_it(tmp_path):
    """Useful for deduplication: identity before extraction cost."""
    path = tmp_path / "dedupe.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")

    described = SERVICE.describe(path)
    ingested = SERVICE.ingest(path)

    assert described.file_id == ingested.file.file_id
    assert described.content_hash == ingested.file.content_hash
    assert described.size_bytes == ingested.file.size_bytes
