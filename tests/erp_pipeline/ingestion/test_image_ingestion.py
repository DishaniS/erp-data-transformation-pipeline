"""Image ingestion against real image files.

OCR-dependent assertions branch on the real capability probe rather than being
skipped wholesale, so the suite passes on a machine without Tesseract while
still proving real OCR where it is installed. The dedicated live-OCR
verification lives in ``test_live_ocr_verification.py``.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ingestion import (
    DocumentFileResult,
    ExtractionStatus,
    ImageDecodeError,
    ImageOptions,
    IngestionOptions,
    ingest_file,
    probe_ocr,
)
from erp_pipeline.ingestion.models import FileType

pytest.importorskip("PIL", reason="Pillow is not installed")

OCR_AVAILABLE = probe_ocr().available


def ingest(path, **image_kwargs):
    options = (
        IngestionOptions(image=ImageOptions(**image_kwargs)) if image_kwargs else None
    )
    return ingest_file(path, options)


# ============================================================
# Result shape and metadata (Step 25)
# ============================================================

@pytest.mark.parametrize("filename", ["text.png", "text.jpg", "small.webp"])
def test_images_produce_a_document_result(binary_fixtures, filename):
    result = ingest(binary_fixtures / filename)

    assert isinstance(result, DocumentFileResult)
    assert result.file.file_type is FileType.IMAGE
    assert result.document.page_count == 1


@pytest.mark.parametrize(
    "filename,expected_format,expected_media",
    [
        ("text.png", "PNG", "image/png"),
        ("text.jpg", "JPEG", "image/jpeg"),
        ("small.webp", "WEBP", "image/webp"),
    ],
)
def test_image_properties_are_recorded(binary_fixtures, filename,
                                       expected_format, expected_media):
    result = ingest(binary_fixtures / filename)
    properties = result.document.document_metadata

    assert properties["format"] == expected_format
    assert result.file.media_type == expected_media
    assert properties["width"] > 0
    assert properties["height"] > 0
    assert properties["frame_count"] >= 1


def test_dimensions_match_the_real_file(binary_fixtures):
    from PIL import Image

    with Image.open(binary_fixtures / "text.png") as image:
        expected = image.size

    properties = ingest(binary_fixtures / "text.png").document.document_metadata

    assert (properties["width"], properties["height"]) == expected


def test_the_content_hash_identifies_the_image(binary_fixtures):
    from erp_pipeline.ingestion import hash_file

    result = ingest(binary_fixtures / "text.png")

    assert result.file.content_hash == hash_file(binary_fixtures / "text.png")
    assert result.provenance.extractor == "pillow"


def test_an_image_is_treated_as_a_single_page_document(binary_fixtures):
    """Not a workaround - a scanned receipt IS a one-page document, and this
    keeps PDF and image results the same shape."""
    document = ingest(binary_fixtures / "text.png").document

    assert len(document.pages) == 1
    assert document.pages[0].page_number == 1


# ============================================================
# OCR (Steps 26, 27)
# ============================================================

@pytest.mark.skipif(not OCR_AVAILABLE, reason="Tesseract is not installed")
@pytest.mark.parametrize("filename", ["text.png", "text.jpg"])
def test_real_ocr_reads_text_from_an_image(binary_fixtures, filename):
    result = ingest(binary_fixtures / filename)

    assert result.status is ExtractionStatus.EXTRACTED
    assert result.document.pages[0].extraction_method == "ocr"
    assert "INVOICE" in result.document.document_text.upper()
    assert result.provenance.ocr_engine == "tesseract"
    assert result.provenance.ocr_engine_version


@pytest.mark.skipif(not OCR_AVAILABLE, reason="Tesseract is not installed")
def test_a_blank_image_reports_no_content_rather_than_failure(binary_fixtures):
    """OCR ran successfully; there was simply nothing to read."""
    result = ingest(binary_fixtures / "blank.png")

    assert result.status is ExtractionStatus.NO_CONTENT_DETECTED
    assert result.document.pages[0].extraction_method == "ocr"


def test_ocr_can_be_disabled_leaving_metadata_intact(binary_fixtures):
    result = ingest(binary_fixtures / "text.png", ocr_enabled=False)

    assert result.status is ExtractionStatus.OCR_UNAVAILABLE
    assert result.document.document_metadata["width"] > 0
    assert any(warning.category == "ocr_disabled" for warning in result.warnings)


def test_missing_ocr_is_reported_explicitly_not_as_empty_text(
    binary_fixtures, monkeypatch
):
    """"we could not read this" and "there was nothing to read" are different
    facts with different remedies."""
    from erp_pipeline.ingestion import image_ingestion
    from erp_pipeline.ingestion import ocr as ocr_module

    unavailable = ocr_module.OcrCapability(
        available=False, engine="tesseract", reason="simulated absence"
    )
    monkeypatch.setattr(image_ingestion, "probe_ocr", lambda *_: unavailable)

    result = ingest(binary_fixtures / "text.png")

    assert result.status is ExtractionStatus.OCR_UNAVAILABLE
    assert result.document.pages[0].text == ""
    assert result.provenance.ocr_engine is None
    warning = next(w for w in result.warnings if w.category == "ocr_unavailable")
    assert "simulated absence" in warning.message


def test_ocr_text_is_bounded_by_the_character_limit(binary_fixtures):
    if not OCR_AVAILABLE:
        pytest.skip("Tesseract is not installed")

    result = ingest(binary_fixtures / "text.png", max_text_chars=4)

    assert result.document.total_char_count <= 4
    assert result.document.pages[0].truncated is True
    assert result.status is ExtractionStatus.PARTIAL


def test_an_ocr_engine_failure_becomes_a_warning_not_a_crash(
    binary_fixtures, monkeypatch
):
    """An engine that is present but blows up is distinct from one that is
    absent, and must not be reported as OCR_UNAVAILABLE."""
    from erp_pipeline.ingestion import image_ingestion
    from erp_pipeline.ingestion import ocr as ocr_module

    available = ocr_module.OcrCapability(
        available=True, engine="tesseract", version="5.0.0", command="tesseract"
    )
    monkeypatch.setattr(image_ingestion, "probe_ocr", lambda *_: available)

    def explode(*_args, **_kwargs):
        raise RuntimeError("The OCR engine failed (SimulatedError).")

    monkeypatch.setattr(image_ingestion, "run_ocr", explode)

    result = ingest(binary_fixtures / "text.png")

    assert result.status is ExtractionStatus.FAILED
    assert any(warning.category == "ocr_failed" for warning in result.warnings)


# ============================================================
# Corrupt and oversized images (Steps 29, 31)
# ============================================================

def test_a_corrupt_image_produces_a_controlled_error(binary_fixtures):
    with pytest.raises(ImageDecodeError) as excinfo:
        ingest(binary_fixtures / "corrupt.png")

    assert "corrupt.png" in str(excinfo.value)


def test_an_implausibly_large_image_is_refused_before_decoding(binary_fixtures):
    """Decompression-bomb guard: the dimension check runs on the header, so a
    tiny file declaring enormous dimensions never gets decoded."""
    with pytest.raises(ImageDecodeError, match="exceeds the configured limit"):
        ingest(binary_fixtures / "text.png", max_pixels=10)


def test_a_valid_image_within_the_pixel_budget_is_accepted(binary_fixtures):
    result = ingest(binary_fixtures / "small.webp", max_pixels=1_000_000)

    assert result.document.document_metadata["format"] == "WEBP"


# ============================================================
# Determinism
# ============================================================

def test_repeated_ingestion_is_identical(binary_fixtures):
    first = ingest(binary_fixtures / "text.png")
    second = ingest(binary_fixtures / "text.png")

    assert first.file.file_id == second.file.file_id
    assert first.document.document_metadata == second.document.document_metadata
    assert first.document.document_text == second.document.document_text
