"""File type detection, content identity, and path/size safety.

Everything here runs against real files on disk - no mocked filesystem, no
patched parsers.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ingestion import (
    FileAccessError,
    FileTooLargeError,
    FileType,
    FileTypeMismatchError,
    IngestionOptions,
    UnsupportedFileTypeError,
    describe_file,
    detect_file_type,
    hash_bytes,
    hash_file,
    ingest_file,
    make_file_id,
    parse_file_id,
)
from erp_pipeline.ingestion.detection import looks_like_text
from erp_pipeline.ingestion.safety import validate_file_size, validate_source_path
from erp_pipeline.schemas.enums import SourceType


# ============================================================
# Detection (Step 4)
# ============================================================

@pytest.mark.parametrize(
    "filename,expected",
    [
        ("normal.csv", FileType.CSV),
        ("semicolon.csv", FileType.CSV),
        ("utf8_bom.csv", FileType.CSV),
        ("empty.csv", FileType.CSV),
    ],
)
def test_csv_files_are_detected(csv_fixtures, filename, expected):
    assert detect_file_type(csv_fixtures / filename).file_type is expected


@pytest.mark.parametrize(
    "filename,expected_media",
    [
        ("text_single_page.pdf", "application/pdf"),
        ("handwritten.pdf", "application/pdf"),
        ("text.png", "image/png"),
        ("text.jpg", "image/jpeg"),
        ("small.webp", "image/webp"),
    ],
)
def test_binary_formats_are_detected_by_signature(binary_fixtures, filename,
                                                  expected_media):
    result = detect_file_type(binary_fixtures / filename)

    assert result.media_type == expected_media
    assert result.detected_by == "signature"


def test_detection_maps_onto_the_existing_phase_1_source_type():
    assert FileType.CSV.to_source_type() is SourceType.CSV
    assert FileType.PDF.to_source_type() is SourceType.PDF
    assert FileType.IMAGE.to_source_type() is SourceType.IMAGE


def test_file_type_knows_which_side_of_the_architecture_it_is_on():
    assert FileType.CSV.is_tabular and not FileType.CSV.is_document
    assert FileType.PDF.is_document and not FileType.PDF.is_tabular
    assert FileType.IMAGE.is_document and not FileType.IMAGE.is_tabular


def test_extension_and_content_conflict_fails_safely(binary_fixtures):
    """A PDF named .csv must not be fed to the CSV parser."""
    with pytest.raises(FileTypeMismatchError) as excinfo:
        detect_file_type(binary_fixtures / "actually_a_pdf.csv")

    assert excinfo.value.extension_type == "csv"
    assert excinfo.value.content_type == "pdf"


def test_type_mismatch_can_be_resolved_in_favour_of_content(binary_fixtures):
    result = detect_file_type(
        binary_fixtures / "actually_a_pdf.csv", allow_mismatch=True
    )

    assert result.file_type is FileType.PDF
    assert result.mismatch is True
    assert result.extension_type is FileType.CSV


def test_the_service_surfaces_a_trusted_mismatch_as_a_warning(binary_fixtures):
    result = ingest_file(
        binary_fixtures / "actually_a_pdf.csv",
        IngestionOptions(allow_type_mismatch=True),
    )

    assert result.is_document
    categories = [warning.category for warning in result.warnings]
    assert "file_type_mismatch" in categories


def test_an_image_extension_without_its_signature_is_rejected(binary_fixtures):
    with pytest.raises(FileTypeMismatchError):
        detect_file_type(binary_fixtures / "not_really.png")


def test_unknown_content_and_extension_is_rejected(binary_fixtures):
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(binary_fixtures / "mystery.bin")


def test_binary_content_is_never_treated_as_text():
    assert looks_like_text(b"invoice,amount\n1,2\n") is True
    assert looks_like_text(b"") is True
    assert looks_like_text(b"\x89PNG\r\n\x1a\n\x00\x00") is False
    assert looks_like_text(b"\x03\x00\x00\x00\x91\x02\x00binary") is False


def test_a_utf16_bom_declares_text_even_though_it_contains_nul_bytes():
    """So the caller gets the reader's actionable encoding message instead of
    a blanket "this looks binary" rejection."""
    assert looks_like_text("id,name\n".encode("utf-16")) is True


def test_utf8_multibyte_text_is_still_text():
    assert looks_like_text("référence,société\n".encode("utf-8")) is True


# ============================================================
# Content identity (Step 7)
# ============================================================

def test_identity_is_derived_from_content_not_the_filename(csv_fixtures, tmp_path):
    original = csv_fixtures / "normal.csv"
    renamed = tmp_path / "completely_different_name.csv"
    renamed.write_bytes(original.read_bytes())

    first = describe_file(original)
    second = describe_file(renamed)

    assert first.content_hash == second.content_hash
    assert first.file_id == second.file_id
    assert first.original_filename != second.original_filename


def test_different_bytes_produce_a_different_identity(csv_fixtures, tmp_path):
    edited = tmp_path / "normal.csv"
    edited.write_bytes(csv_fixtures / "normal.csv" and
                       (csv_fixtures / "normal.csv").read_bytes() + b"INV-1004,New,1,2026-04-01,true\n")

    assert describe_file(edited).content_hash != describe_file(
        csv_fixtures / "normal.csv"
    ).content_hash


def test_file_id_format_is_deterministic_and_reversible():
    digest = hash_bytes(b"stable content")
    file_id = make_file_id(digest)

    assert file_id == f"file.sha256.{digest}"
    assert parse_file_id(file_id) == digest


def test_file_id_is_a_valid_normalized_identifier_component():
    """It is embedded in composite ids, so it must survive Phase 1's rules."""
    from erp_pipeline.schemas.identity import is_normalized_identifier

    assert is_normalized_identifier(make_file_id(hash_bytes(b"x")))


def test_parse_file_id_rejects_a_bare_hash():
    with pytest.raises(ValueError):
        parse_file_id(hash_bytes(b"x"))


def test_streamed_and_in_memory_hashes_agree(csv_fixtures):
    path = csv_fixtures / "normal.csv"

    assert hash_file(path) == hash_bytes(path.read_bytes())


def test_identity_does_not_change_between_runs(csv_fixtures):
    first = describe_file(csv_fixtures / "normal.csv")
    second = describe_file(csv_fixtures / "normal.csv")

    assert first == second


# ============================================================
# Provenance and path handling (Steps 8, 30)
# ============================================================

def test_the_local_path_is_never_part_of_the_portable_payload(csv_fixtures):
    """A developer workstation path is not identity and must not be published."""
    described = describe_file(csv_fixtures / "normal.csv")
    payload = described.to_dict()

    assert "local_path" not in payload
    assert "runtime_local_path" not in payload
    assert str(csv_fixtures) not in str(payload)
    assert payload["original_filename"] == "normal.csv"


def test_the_local_path_is_available_when_explicitly_requested(csv_fixtures):
    described = describe_file(csv_fixtures / "normal.csv")

    payload = described.to_dict(include_local_path=True)

    assert payload["runtime_local_path"].endswith("normal.csv")


def test_full_result_serialization_carries_no_absolute_path(csv_fixtures):
    result = ingest_file(csv_fixtures / "normal.csv")

    assert str(csv_fixtures) not in str(result.to_dict())


def test_provenance_records_the_extractor_and_its_version(csv_fixtures):
    result = ingest_file(csv_fixtures / "normal.csv")

    assert result.provenance.extractor == "python-csv"
    assert result.provenance.extractor_version
    assert result.provenance.content_hash == result.file.content_hash
    assert result.provenance.size_bytes > 0


def test_a_directory_is_rejected(csv_fixtures):
    with pytest.raises(FileAccessError, match="directory"):
        validate_source_path(csv_fixtures)


def test_a_missing_file_is_rejected(tmp_path):
    with pytest.raises(FileAccessError):
        validate_source_path(tmp_path / "nope.csv")


def test_a_none_path_is_rejected():
    with pytest.raises(FileAccessError):
        validate_source_path(None)


# ============================================================
# Size limit (Step 29)
# ============================================================

def test_an_oversized_file_is_rejected(csv_fixtures):
    path = csv_fixtures / "normal.csv"

    with pytest.raises(FileTooLargeError) as excinfo:
        validate_file_size(path, max_bytes=10)

    assert excinfo.value.limit_bytes == 10
    assert excinfo.value.size_bytes > 10


def test_the_size_limit_is_enforced_before_any_parsing(csv_fixtures):
    with pytest.raises(FileTooLargeError):
        ingest_file(
            csv_fixtures / "normal.csv", IngestionOptions(max_file_size_bytes=16)
        )


def test_the_size_limit_default_is_not_absurdly_small():
    """A realistic ERP export must ingest without reconfiguration."""
    assert IngestionOptions().max_file_size_bytes >= 64 * 1024 * 1024


def test_a_file_within_the_limit_is_accepted(csv_fixtures):
    result = ingest_file(
        csv_fixtures / "normal.csv",
        IngestionOptions(max_file_size_bytes=1024 * 1024),
    )

    assert result.file.size_bytes < 1024 * 1024
