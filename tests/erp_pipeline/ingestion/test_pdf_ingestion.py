"""PDF ingestion against real PDF files.

Every test parses a genuine PDF from disk through PyMuPDF - including one
hand-written PDF that this project's own libraries did not author, so the
extraction path is not merely round-tripping its own output.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ingestion import (
    DocumentFileResult,
    EncryptedPDFError,
    ExtractionStatus,
    IngestionOptions,
    MalformedPDFError,
    PdfOptions,
    ingest_file,
    probe_ocr,
)
from erp_pipeline.ingestion.models import FileType

pytest.importorskip("fitz", reason="pymupdf is not installed")


def ingest(path, **pdf_kwargs):
    options = IngestionOptions(pdf=PdfOptions(**pdf_kwargs)) if pdf_kwargs else None
    return ingest_file(path, options)


# ============================================================
# Detection and result shape
# ============================================================

def test_a_pdf_produces_a_document_result_not_a_schema(binary_fixtures):
    """A PDF has no columns; inventing some would be a fabrication."""
    result = ingest(binary_fixtures / "text_single_page.pdf")

    assert isinstance(result, DocumentFileResult)
    assert result.is_document and not result.is_tabular
    assert result.file.file_type is FileType.PDF
    assert not hasattr(result, "schema")


def test_text_is_extracted_from_a_real_pdf(binary_fixtures):
    result = ingest(binary_fixtures / "text_single_page.pdf")

    assert result.status is ExtractionStatus.EXTRACTED
    assert "INVOICE SUMMARY" in result.document.document_text
    assert result.document.total_char_count > 0


def test_text_is_extracted_from_a_pdf_this_project_did_not_write(binary_fixtures):
    """The hand-written fixture is raw PDF bytes, not PyMuPDF output."""
    result = ingest(binary_fixtures / "handwritten.pdf")

    assert result.status is ExtractionStatus.EXTRACTED
    assert "HANDWRITTEN PDF" in result.document.document_text


# ============================================================
# Pages (Step 22)
# ============================================================

def test_page_boundaries_and_ordering_are_preserved(binary_fixtures):
    result = ingest(binary_fixtures / "text_multi_page.pdf")
    pages = result.document.pages

    assert len(pages) == 3
    assert [page.page_number for page in pages] == [1, 2, 3]
    for index, page in enumerate(pages, start=1):
        assert f"PAGE MARKER {index}" in page.text


def test_document_text_is_a_deterministic_ordered_join(binary_fixtures):
    document = ingest(binary_fixtures / "text_multi_page.pdf").document

    joined = document.document_text
    assert joined.index("PAGE MARKER 1") < joined.index("PAGE MARKER 2")
    assert joined.index("PAGE MARKER 2") < joined.index("PAGE MARKER 3")
    # Page boundaries stay recoverable from the joined string.
    assert joined.count("\f") == 2


def test_page_count_is_preserved(binary_fixtures):
    result = ingest(binary_fixtures / "text_multi_page.pdf")

    assert result.document.page_count == 3
    assert result.provenance.page_count == 3
    assert result.page_count == 3


def test_each_page_reports_how_its_text_was_obtained(binary_fixtures):
    pages = ingest(binary_fixtures / "text_multi_page.pdf").document.pages

    assert all(page.extraction_method == "text_layer" for page in pages)
    assert all(page.char_count > 0 for page in pages)


# ============================================================
# Metadata and provenance
# ============================================================

def test_document_metadata_is_captured_as_content(binary_fixtures):
    """A PDF title routinely contains a customer name, so it is treated as
    content and withheld from the operational summary."""
    result = ingest(binary_fixtures / "text_single_page.pdf")

    assert result.document.document_metadata["title"] == "Phase 6 Text PDF"
    assert "Phase 6 Text PDF" not in str(result.to_dict())
    assert "Phase 6 Text PDF" in str(result.to_dict(include_text=True))


def test_document_metadata_can_be_switched_off(binary_fixtures):
    result = ingest(
        binary_fixtures / "text_single_page.pdf", include_document_metadata=False
    )

    assert result.document.document_metadata == {}


def test_provenance_records_the_extractor_and_content_hash(binary_fixtures):
    result = ingest(binary_fixtures / "text_single_page.pdf")

    assert result.provenance.extractor == "pymupdf"
    assert result.provenance.content_hash == result.file.content_hash
    assert result.provenance.media_type == "application/pdf"


# ============================================================
# Empty, corrupt and encrypted (Steps 24, 31)
# ============================================================

def test_a_pdf_with_no_text_reports_no_content_not_failure(binary_fixtures):
    result = ingest(binary_fixtures / "blank.pdf", ocr_fallback=False)

    assert result.status is ExtractionStatus.NO_CONTENT_DETECTED
    assert result.document.page_count == 1
    assert result.document.has_text is False


def test_a_corrupt_pdf_produces_a_controlled_error(binary_fixtures):
    with pytest.raises(MalformedPDFError) as excinfo:
        ingest(binary_fixtures / "corrupt.pdf")

    assert "corrupt.pdf" in str(excinfo.value)


def test_an_encrypted_pdf_is_refused_without_attempting_to_decrypt(binary_fixtures):
    with pytest.raises(EncryptedPDFError) as excinfo:
        ingest(binary_fixtures / "encrypted.pdf")

    message = str(excinfo.value)
    assert "password-protected" in message
    # No password from the fixture leaks into the message.
    assert "user-secret" not in message
    assert "owner-secret" not in message


# ============================================================
# Safety budgets (Step 24)
# ============================================================

def test_the_page_limit_bounds_extraction(binary_fixtures):
    result = ingest(binary_fixtures / "text_multi_page.pdf", max_pages=2)

    assert len(result.document.pages) == 2
    # The real page count is still reported honestly.
    assert result.document.page_count == 3
    assert any(warning.category == "page_limit" for warning in result.warnings)


def test_the_per_page_character_limit_truncates_and_says_so(binary_fixtures):
    result = ingest(
        binary_fixtures / "text_multi_page.pdf", max_text_chars_per_page=5
    )

    assert all(page.char_count <= 5 for page in result.document.pages)
    assert all(page.truncated for page in result.document.pages)
    assert result.status is ExtractionStatus.PARTIAL
    assert any(
        warning.category == "page_text_truncated" for warning in result.warnings
    )


def test_the_document_wide_character_budget_stops_extraction(binary_fixtures):
    """Per-page limits alone are not enough: 500 pages just under the per-page
    cap would still produce an enormous total."""
    result = ingest(binary_fixtures / "text_multi_page.pdf", max_total_text_chars=20)

    assert result.document.total_char_count <= 20
    assert result.status is ExtractionStatus.PARTIAL
    assert any(warning.category == "text_budget" for warning in result.warnings)


# ============================================================
# OCR fallback (Step 23)
# ============================================================

def test_a_scanned_page_reports_its_state_explicitly(binary_fixtures):
    """Either OCR read it, or OCR was unavailable - never an empty string
    presented as a successful extraction."""
    result = ingest(binary_fixtures / "scanned_image_only.pdf")
    page = result.document.pages[0]

    if probe_ocr().available:
        assert page.status is ExtractionStatus.EXTRACTED
        assert page.extraction_method == "ocr"
        assert page.char_count > 0
    else:
        assert page.status is ExtractionStatus.OCR_UNAVAILABLE
        assert page.char_count == 0
        assert any(
            warning.category == "ocr_unavailable" for warning in result.warnings
        )


def test_ocr_fallback_can_be_disabled(binary_fixtures):
    result = ingest(binary_fixtures / "scanned_image_only.pdf", ocr_fallback=False)
    page = result.document.pages[0]

    assert page.extraction_method != "ocr"
    assert page.char_count == 0


def test_a_thin_text_layer_is_never_discarded_when_ocr_is_unavailable(
    binary_fixtures, monkeypatch
):
    """Regression: a page below the OCR threshold must keep the real text its
    text layer produced, rather than being reported as unreadable."""
    from erp_pipeline.ingestion import ocr as ocr_module
    from erp_pipeline.ingestion import pdf_ingestion

    unavailable = ocr_module.OcrCapability(
        available=False, engine="tesseract", reason="simulated absence"
    )
    monkeypatch.setattr(pdf_ingestion, "probe_ocr", lambda *_: unavailable)

    # "PAGE MARKER 1" is 13 characters - below the default 16-char threshold.
    result = ingest(binary_fixtures / "text_multi_page.pdf")

    assert result.document.pages[0].char_count > 0
    assert "PAGE MARKER 1" in result.document.pages[0].text
    assert result.document.pages[0].extraction_method == "text_layer"
    assert any(
        warning.category == "ocr_unavailable_low_text" for warning in result.warnings
    )


def test_a_page_with_no_text_at_all_is_ocr_unavailable_when_ocr_is_missing(
    binary_fixtures, monkeypatch
):
    from erp_pipeline.ingestion import ocr as ocr_module
    from erp_pipeline.ingestion import pdf_ingestion

    unavailable = ocr_module.OcrCapability(
        available=False, engine="tesseract", reason="simulated absence"
    )
    monkeypatch.setattr(pdf_ingestion, "probe_ocr", lambda *_: unavailable)

    result = ingest(binary_fixtures / "scanned_image_only.pdf")

    assert result.status is ExtractionStatus.OCR_UNAVAILABLE
    assert result.document.pages[0].status is ExtractionStatus.OCR_UNAVAILABLE


# ============================================================
# Determinism (Step 33)
# ============================================================

def test_repeated_extraction_is_identical(binary_fixtures):
    first = ingest(binary_fixtures / "text_multi_page.pdf")
    second = ingest(binary_fixtures / "text_multi_page.pdf")

    assert first.file.content_hash == second.file.content_hash
    assert first.file.file_id == second.file.file_id
    assert first.document.document_text == second.document.document_text
    assert [p.page_number for p in first.document.pages] == [
        p.page_number for p in second.document.pages
    ]


def test_the_extraction_timestamp_is_operational_only(binary_fixtures):
    """It may exist, but it must not reach identity or any hash."""
    first = ingest(binary_fixtures / "text_single_page.pdf")
    second = ingest(binary_fixtures / "text_single_page.pdf")

    assert first.file.file_id == second.file.file_id
    assert first.provenance.to_dict(include_timestamp=False) == (
        second.provenance.to_dict(include_timestamp=False)
    )
