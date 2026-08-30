"""Phase 3 - database BLOBs become indexed documents.

The two tests that matter most here are
``test_the_same_document_on_two_employees_does_not_collide`` and the leakage
audit. The first guards an identity chain that would otherwise silently
overwrite one employee's certificate with another's; the second guards the
invariant that raw bytes never become embedded text.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

from erp_pipeline.ai.attached_documents import (
    CONTENT_KIND_DOCUMENT_CHUNK,
    DocumentAttachment,
    attached_document_to_representations,
)
from erp_pipeline.ingestion.binary_assets import (
    BinaryAssetOptions,
    BinaryAssetOutcome,
    binary_field_names_for_entity,
    coerce_binary,
    extract_binary_asset,
)
from erp_pipeline.orchestration.multimodal import extract_record_assets, pair_records
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer


# ----------------------------------------------------------------------
# Fixtures - real bytes, built at test time
# ----------------------------------------------------------------------


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type="X",
        normalized_data_type=data_type,
        is_primary_key=primary,
        nullable=not primary,
    )


@pytest.fixture
def text_pdf() -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    document.new_page().insert_text(
        (72, 96), "BIRTH CERTIFICATE\nName: Nimal Silva\nDOB: 1997-03-20"
    )
    payload = document.tobytes()
    document.close()

    return payload


@pytest.fixture
def other_pdf() -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    document.new_page().insert_text((72, 96), "EMPLOYMENT CONTRACT\nRole: Accountant")
    payload = document.tobytes()
    document.close()

    return payload


@pytest.fixture
def jpeg() -> bytes:
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (300, 120), "white").save(buffer, "JPEG")

    return buffer.getvalue()


@pytest.fixture
def png() -> bytes:
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (300, 120), "white").save(buffer, "PNG")

    return buffer.getvalue()


@pytest.fixture
def zip_bytes() -> bytes:
    return b"PK\x03\x04" + b"\x00" * 200


@pytest.fixture
def employees_entity() -> SourceEntity:
    return SourceEntity(
        entity_id="hr.employees",
        source_name="employees",
        normalized_name="employees",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("employee_id",),
        fields=(
            _field("employee_id", FieldDataType.STRING, primary=True),
            _field("name", FieldDataType.STRING),
            _field("profile_photo", FieldDataType.BINARY),
            _field("birth_certificate", FieldDataType.BINARY),
            _field("employment_contract", FieldDataType.BINARY),
        ),
    )


def transform(rows, entity):
    return SourceNativeTransformer().transform_records(
        rows, entity, "legacy_hr", SourceType.POSTGRESQL
    ).records


# ======================================================================
# Detection - schema first, then bytes
# ======================================================================


def test_binary_fields_come_from_the_discovered_schema(employees_entity):
    assert binary_field_names_for_entity(employees_entity) == (
        "profile_photo",
        "birth_certificate",
        "employment_contract",
    )


def test_an_entity_with_no_binary_columns_reports_none():
    plain = SourceEntity(
        entity_id="s.invoices",
        source_name="invoices",
        normalized_name="invoices",
        entity_kind=EntityKind.TABLE,
        fields=(_field("inv_no", FieldDataType.STRING),),
    )

    assert binary_field_names_for_entity(plain) == ()


@pytest.mark.parametrize("wrapper", [bytes, bytearray, memoryview])
def test_driver_binary_types_are_all_accepted(text_pdf, wrapper):
    """psycopg gives memoryview, pyodbc gives bytearray, PyMySQL gives bytes."""
    assert coerce_binary(wrapper(text_pdf)) == text_pdf


def test_a_string_in_a_binary_column_is_not_treated_as_a_document():
    """A text value in a BLOB column is a schema disagreement, not base64."""
    assert coerce_binary("not actually bytes") is None


# ======================================================================
# TEST C / D - PDF
# ======================================================================


def test_a_text_pdf_blob_is_extracted_without_ocr(text_pdf):
    result = extract_binary_asset(text_pdf, "birth_certificate")

    assert result.outcome == BinaryAssetOutcome.EXTRACTED
    assert result.media_type == "application/pdf"
    assert result.ocr_used is False
    assert result.page_count == 1
    assert result.document_id


def test_the_document_id_is_content_addressed(text_pdf, other_pdf):
    first = extract_binary_asset(text_pdf, "c")
    same = extract_binary_asset(bytes(text_pdf), "c")
    different = extract_binary_asset(other_pdf, "c")

    assert first.document_id == same.document_id
    assert first.document_id != different.document_id


# ======================================================================
# TEST A / B - images
# ======================================================================


@pytest.mark.parametrize("fixture, media", [("jpeg", "image/jpeg"), ("png", "image/png")])
def test_an_image_blob_is_detected_by_its_bytes(request, fixture, media):
    payload = request.getfixturevalue(fixture)
    result = extract_binary_asset(payload, "profile_photo")

    assert result.outcome == BinaryAssetOutcome.EXTRACTED
    assert result.media_type == media


def test_the_column_name_never_decides_the_format(text_pdf):
    """A PDF stored in a column called profile_photo is still a PDF."""
    result = extract_binary_asset(text_pdf, "profile_photo")

    assert result.media_type == "application/pdf"


# ======================================================================
# TEST E - unsupported binary
# ======================================================================


def test_an_unsupported_blob_is_refused_not_guessed(zip_bytes):
    result = extract_binary_asset(zip_bytes, "archive")

    assert result.outcome == BinaryAssetOutcome.UNSUPPORTED
    assert result.document is None
    assert result.warnings


def test_a_corrupt_pdf_blob_degrades_rather_than_raising():
    result = extract_binary_asset(b"%PDF-1.7\n" + b"\x00" * 80, "contract")

    assert result.outcome in {
        BinaryAssetOutcome.UNREADABLE,
        BinaryAssetOutcome.UNSUPPORTED,
    }
    assert result.document is None


def test_an_oversized_blob_is_refused_before_extraction():
    result = extract_binary_asset(
        b"%PDF-1.7" + b"\x00" * 5000, "big", BinaryAssetOptions(max_bytes=100)
    )

    assert result.outcome == BinaryAssetOutcome.TOO_LARGE


def test_an_empty_binary_field_is_not_an_error():
    assert extract_binary_asset(None, "x").outcome == BinaryAssetOutcome.EMPTY
    assert extract_binary_asset(b"", "x").outcome == BinaryAssetOutcome.EMPTY


# ======================================================================
# TEST M - the result object never carries bytes
# ======================================================================


def test_an_asset_report_never_contains_bytes_or_base64(text_pdf, jpeg, zip_bytes):
    for payload in (text_pdf, jpeg, zip_bytes):
        report = json.dumps(extract_binary_asset(payload, "f").to_dict())

        assert base64.b64encode(payload).decode()[:24] not in report
        assert "%PDF" not in report
        assert "\\u00ff\\u00d8" not in report


# ======================================================================
# TEST G - THE COLLISION TEST
# ======================================================================


def test_the_same_document_on_two_employees_does_not_collide(text_pdf):
    """Identical certificate bytes issued to two employees.

    Content identity is shared - it IS the same document. Attachment identity
    must not be, or one employee's vector overwrites the other's and a search
    for EMP002 returns EMP003's record.
    """
    asset = extract_binary_asset(text_pdf, "birth_certificate")
    built = {}

    for employee in ("EMP002", "EMP003"):
        attachment = DocumentAttachment(
            parent_record_id=f"erp:legacy_hr:employees:{employee.lower()}",
            source_system_id="legacy_hr",
            source_entity="employees",
            source_field="birth_certificate",
            document_id=asset.document_id,
            business_key_name="employee_id",
            business_key_value=employee,
        )
        built[employee] = attached_document_to_representations(
            asset.document, attachment
        )

    first = built["EMP002"][0]
    second = built["EMP003"][0]

    # Distinct attachment identity, all the way to the vector.
    assert first.representation_id != second.representation_id
    assert first.vector_id != second.vector_id
    # Shared content identity, because it is the same document.
    assert first.metadata["document_id"] == second.metadata["document_id"]
    assert first.metadata["content_chunk_id"] == second.metadata["content_chunk_id"]
    # Each points at its own employee.
    assert first.source_record_ids == ("erp:legacy_hr:employees:emp002",)
    assert second.source_record_ids == ("erp:legacy_hr:employees:emp003",)


def test_ordinary_uploaded_chunk_identity_is_unchanged(text_pdf):
    """Phase 3 must not disturb how uploaded documents are identified."""
    from erp_pipeline.ai.chunking import chunk_document, chunk_to_representation

    asset = extract_binary_asset(text_pdf, "f")
    chunks = chunk_document(asset.document)
    representation = chunk_to_representation(chunks[0])

    assert representation.representation_id == chunks[0].chunk_id


# ======================================================================
# Parent linkage and metadata (Requirements 8-11)
# ======================================================================


def test_a_document_chunk_carries_its_erp_context(text_pdf):
    asset = extract_binary_asset(text_pdf, "birth_certificate")
    attachment = DocumentAttachment(
        parent_record_id="erp:legacy_hr:employees:emp002",
        source_system_id="legacy_hr",
        source_entity="employees",
        source_field="birth_certificate",
        document_id=asset.document_id,
        business_key_name="employee_id",
        business_key_value="EMP002",
        document_type="birth_certificate",
    )
    metadata = attached_document_to_representations(asset.document, attachment)[0].metadata

    assert metadata["content_kind"] == CONTENT_KIND_DOCUMENT_CHUNK
    assert metadata["parent_record_id"] == "erp:legacy_hr:employees:emp002"
    assert metadata["source_system_id"] == "legacy_hr"
    assert metadata["source_entity"] == "employees"
    assert metadata["source_field"] == "birth_certificate"
    assert metadata["document_type"] == "birth_certificate"
    assert metadata["business_key_name"] == "employee_id"
    assert metadata["business_key_value"] == "EMP002"
    assert metadata["document_id"] == asset.document_id
    assert metadata["page_start"] == 1
    assert metadata["chunk_index"] == 0


def test_document_type_defaults_to_the_source_field_name(text_pdf):
    """Deterministic ERP context, never a content-derived guess."""
    asset = extract_binary_asset(text_pdf, "employment_contract")
    attachment = DocumentAttachment(
        parent_record_id="erp:s:employees:e1",
        source_system_id="s",
        source_entity="employees",
        source_field="employment_contract",
        document_id=asset.document_id,
    )
    metadata = attached_document_to_representations(asset.document, attachment)[0].metadata

    assert metadata["document_type"] == "employment_contract"


# ======================================================================
# TEST F - multiple binary fields, independently processed
# ======================================================================


def test_every_binary_field_is_processed_independently(
    employees_entity, text_pdf, other_pdf, jpeg
):
    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "name": "Nimal Silva",
                "profile_photo": jpeg,
                "birth_certificate": text_pdf,
                "employment_contract": other_pdf,
            }
        )
    ]
    canonical = transform(rows, employees_entity)
    result = extract_record_assets(
        rows, canonical, employees_entity, binary_field_names_for_entity(employees_entity)
    )

    assert result.fields_seen == 3
    fields = {item.source_field for item in result.assets}
    assert fields == {"profile_photo", "birth_certificate", "employment_contract"}


def test_one_bad_blob_does_not_stop_the_others(
    employees_entity, text_pdf, zip_bytes
):
    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "name": "N",
                "profile_photo": zip_bytes,
                "birth_certificate": text_pdf,
            }
        )
    ]
    canonical = transform(rows, employees_entity)
    result = extract_record_assets(
        rows, canonical, employees_entity, binary_field_names_for_entity(employees_entity)
    )

    assert result.skipped >= 1
    assert any(
        r.metadata["source_field"] == "birth_certificate"
        for r in result.representations
    )


def test_the_scalar_record_survives_a_bad_blob(employees_entity, zip_bytes):
    rows = [
        SourceRecord.from_mapping(
            {"employee_id": "EMP002", "name": "Nimal Silva", "profile_photo": zip_bytes}
        )
    ]
    canonical = transform(rows, employees_entity)

    assert canonical[0].record_id == "erp:legacy_hr:employees:emp002"
    assert canonical[0].normalized_data["name"] == "Nimal Silva"


# ======================================================================
# Binary safety across the whole path (Requirement 2)
# ======================================================================


def test_no_binary_or_base64_reaches_any_representation(
    employees_entity, text_pdf, other_pdf, jpeg, zip_bytes
):
    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "name": "N",
                "profile_photo": jpeg,
                "birth_certificate": text_pdf,
                "employment_contract": other_pdf,
            }
        )
    ]
    canonical = transform(rows, employees_entity)
    result = extract_record_assets(
        rows, canonical, employees_entity, binary_field_names_for_entity(employees_entity)
    )

    everything = json.dumps(
        [r.to_dict() for r in result.representations]
        + [c.to_json_dict() for c in canonical]
        + [a.to_dict() for a in result.assets]
        + list(result.warnings),
        default=str,
    )

    for payload in (text_pdf, other_pdf, jpeg, zip_bytes):
        assert base64.b64encode(payload).decode()[:24] not in everything

    assert "JVBERi0x" not in everything     # base64 "%PDF"
    assert "/9j/4AAQ" not in everything     # base64 JPEG
    assert "iVBORw0KGgo" not in everything  # base64 PNG


def test_the_scalar_record_never_holds_the_blob(employees_entity, text_pdf):
    rows = [
        SourceRecord.from_mapping(
            {"employee_id": "EMP002", "name": "N", "birth_certificate": text_pdf}
        )
    ]

    assert "birth_certificate" not in transform(rows, employees_entity)[0].normalized_data


# ======================================================================
# Pairing safety
# ======================================================================


def test_records_pair_positionally_when_parallel(employees_entity, text_pdf):
    rows = [
        SourceRecord.from_mapping(
            {"employee_id": f"EMP00{n}", "name": "N", "birth_certificate": text_pdf}
        )
        for n in (2, 3)
    ]
    canonical = transform(rows, employees_entity)
    pairs = pair_records(rows, canonical)

    assert len(pairs) == 2
    assert pairs[0][1].record_id.endswith("emp002")
    assert pairs[1][1].record_id.endswith("emp003")


def test_a_rejected_row_does_not_shift_the_pairing(employees_entity, text_pdf):
    """The failure this guards against is silent and plausible: attaching
    EMP003's certificate to EMP002 because one earlier row was dropped."""
    rows = [
        SourceRecord.from_mapping({"name": "no key", "birth_certificate": text_pdf}),
        SourceRecord.from_mapping(
            {"employee_id": "EMP003", "name": "Amal", "birth_certificate": text_pdf}
        ),
    ]
    canonical = transform(rows, employees_entity)

    assert len(canonical) == 1

    pairs = pair_records(rows, canonical)

    assert len(pairs) == 1
    assert pairs[0][0].values["employee_id"] == "EMP003"
    assert pairs[0][1].record_id.endswith("emp003")


# ======================================================================
# TEST L - OCR unavailable
# ======================================================================


def test_ocr_unavailable_produces_no_fabricated_text(jpeg):
    from erp_pipeline.ingestion.ocr import probe_ocr

    result = extract_binary_asset(jpeg, "profile_photo")

    if not probe_ocr().available:
        assert result.ocr_used is False
        assert result.extraction_status == "ocr_unavailable"
        # Extraction "succeeded" structurally, but produced nothing to index -
        # and nothing was invented to fill the gap.
        document_text = "".join(p.text for p in result.document.pages)
        assert document_text.strip() == ""


def test_an_image_with_no_text_is_reported_rather_than_indexed_empty(
    employees_entity, jpeg
):
    rows = [
        SourceRecord.from_mapping(
            {"employee_id": "EMP002", "name": "N", "profile_photo": jpeg}
        )
    ]
    canonical = transform(rows, employees_entity)
    result = extract_record_assets(
        rows, canonical, employees_entity, ("profile_photo",)
    )

    if not result.representations:
        assert result.skipped >= 1
        assert any("no text" in w for w in result.warnings)


# ======================================================================
# TEST K - tables with no binary columns are unaffected
# ======================================================================


def test_a_table_without_binary_columns_does_no_document_work():
    invoices = SourceEntity(
        entity_id="s.invoices",
        source_name="invoices",
        normalized_name="invoices",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("inv_no",),
        fields=(
            _field("inv_no", FieldDataType.STRING, primary=True),
            _field("total_amt", FieldDataType.DECIMAL),
        ),
    )
    rows = [SourceRecord.from_mapping({"inv_no": "INV-1", "total_amt": "10.00"})]
    canonical = transform(rows, invoices)
    result = extract_record_assets(
        rows, canonical, invoices, binary_field_names_for_entity(invoices)
    )

    assert result.fields_seen == 0
    assert result.representations == ()


# ======================================================================
# The BLOB path touches no filesystem
# ======================================================================


def test_extracting_a_blob_writes_nothing_to_disk(text_pdf, jpeg, zip_bytes):
    """A birth certificate must never land in the system temp directory.

    Spilling BLOBs to temp files would put ERP documents on disk in plaintext,
    outside every access control and encryption guarantee the storage tiers
    provide - and would break the ingestion package's standing read-only rule.
    """
    import os
    import tempfile

    temp_dir = tempfile.gettempdir()
    before = set(os.listdir(temp_dir))

    for payload in (text_pdf, jpeg, zip_bytes, b"%PDF-1.7\n" + b"\x00" * 80):
        extract_binary_asset(payload, "birth_certificate")

    assert set(os.listdir(temp_dir)) - before == set()


def test_in_memory_content_is_never_serialized_as_identity(text_pdf):
    """``payload`` is runtime state, exactly like ``local_path``."""
    from erp_pipeline.ingestion.models import FileSource, FileType

    source = FileSource(
        file_id="f",
        content_hash="h",
        original_filename="x.pdf",
        file_type=FileType.PDF,
        media_type="application/pdf",
        size_bytes=len(text_pdf),
        payload=text_pdf,
    )

    assert "payload" not in source.to_dict()
    assert "payload" not in source.to_dict(include_local_path=True)


def test_the_synthesised_filename_never_carries_the_column_name(text_pdf):
    """Extractor errors quote the filename; the ERP's vocabulary stays out."""
    asset = extract_binary_asset(text_pdf, "employee_birth_certificate")

    assert "birth_certificate" not in asset.document.file.original_filename
    assert asset.document.file.original_filename.startswith("blob_")


def test_an_uploaded_file_still_reads_from_its_path(tmp_path, text_pdf):
    """The in-memory branch is additive - the file branch is untouched."""
    from erp_pipeline.ingestion import ingest_file

    path = tmp_path / "certificate.pdf"
    path.write_bytes(text_pdf)
    result = ingest_file(path)

    assert "BIRTH CERTIFICATE" in "".join(
        page.text for page in result.document.pages
    )


# ======================================================================
# TEST D - a scanned PDF BLOB takes the OCR path
# ======================================================================


@pytest.fixture
def scanned_pdf() -> bytes:
    """A PDF whose only content is a PICTURE of text - no text layer at all.

    Built by rendering a real text page to a high-resolution bitmap and putting
    the bitmap into a fresh PDF. That is what a scanned certificate actually is,
    and unlike a blank image it gives OCR something to recognise, so the test
    can tell "OCR ran and read it" apart from "OCR ran and found nothing".
    """
    fitz = pytest.importorskip("pymupdf")

    typed = fitz.open()
    typed.new_page(width=400, height=200).insert_text(
        (40, 100), "BIRTH CERTIFICATE", fontsize=28
    )
    bitmap = typed.load_page(0).get_pixmap(dpi=300).tobytes("png")
    typed.close()

    scanned = fitz.open()
    page = scanned.new_page(width=400, height=200)
    page.insert_image(fitz.Rect(0, 0, 400, 200), stream=bitmap)
    payload = scanned.tobytes()
    scanned.close()

    return payload


def test_the_scanned_fixture_really_has_no_text_layer(scanned_pdf):
    """Guards the fixture itself: if it grew a text layer, TEST D proves nothing."""
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open(stream=scanned_pdf, filetype="pdf")

    try:
        assert document.load_page(0).get_text().strip() == ""
    finally:
        document.close()


def test_a_scanned_pdf_blob_reaches_the_ocr_fallback(scanned_pdf):
    """The text layer is empty, so extraction must fall through to OCR.

    Asserted against the OCR capability rather than skipped outright, because
    the interesting behaviour on THIS machine - honest degradation when
    Tesseract is missing - is exactly what a scanned certificate would hit in a
    deployment that forgot to install it.
    """
    from erp_pipeline.ingestion.ocr import probe_ocr

    result = extract_binary_asset(scanned_pdf, "scanned_certificate")

    assert result.outcome == BinaryAssetOutcome.EXTRACTED
    assert result.media_type == "application/pdf"

    if probe_ocr().available:
        assert result.ocr_used is True
        assert "CERTIFICATE" in "".join(p.text for p in result.document.pages).upper()
    else:
        # Nothing was invented to cover the gap.
        assert result.ocr_used is False
        assert "".join(p.text for p in result.document.pages).strip() == ""
        assert any("ocr" in w.lower() for w in result.warnings)


def test_ocr_can_be_switched_off_for_the_blob_path(scanned_pdf):
    result = extract_binary_asset(
        scanned_pdf, "scanned_certificate", BinaryAssetOptions(ocr_enabled=False)
    )

    assert result.ocr_used is False
    assert "".join(p.text for p in result.document.pages).strip() == ""


def test_a_scanned_page_never_yields_a_fabricated_representation(
    employees_entity, scanned_pdf
):
    """An unreadable scan must produce no vector at all, not an empty one."""
    rows = [
        SourceRecord.from_mapping(
            {"employee_id": "EMP002", "name": "N", "birth_certificate": scanned_pdf}
        )
    ]
    canonical = transform(rows, employees_entity)
    result = extract_record_assets(
        rows, canonical, employees_entity, ("birth_certificate",)
    )

    for representation in result.representations:
        assert representation.text_for_ai.strip()


def test_ocr_is_counted_only_for_assets_that_produced_a_vector(
    employees_entity, scanned_pdf, jpeg
):
    """A blank image OCR found nothing in is not an "OCR asset".

    Both go down the OCR path, but only one yields text. Counting the other
    would tell an operator that two documents were read by OCR when one
    produced nothing at all.
    """
    from erp_pipeline.ingestion.ocr import probe_ocr

    if not probe_ocr().available:
        pytest.skip("OCR is unavailable on this machine")

    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP004",
                "name": "K",
                "birth_certificate": scanned_pdf,
                "profile_photo": jpeg,
            }
        )
    ]
    canonical = transform(rows, employees_entity)
    result = extract_record_assets(
        rows, canonical, employees_entity, ("birth_certificate", "profile_photo")
    )

    assert result.fields_seen == 2
    assert result.ocr_assets == 1
    assert len(result.representations) == 1
    assert result.skipped == 1
