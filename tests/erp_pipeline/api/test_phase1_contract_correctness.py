"""Phase 1 contract-correctness regression tests.

Each test here pins one defect that shipped a FALSE STATEMENT to a client: a row
count that was never counted, an identifier that was always null, an OCR flag
that could never be true, a mapping target the engine had chosen but did not
report, a Python repr rendered into a user-facing message, and a client's broken
file reported as a server fault.

They are written as contract tests rather than unit tests on purpose. Every one
of these defects was invisible from inside its own module and only became
apparent in the response body, which is the thing a caller actually reads.
"""

from __future__ import annotations

import io

import pytest

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def invoice_csv() -> bytes:
    return (
        b"inv_no,cust_ref,total_amt,curr,approval_status,row_version\n"
        b"INV-204,CUS-17,45000.00,LKR,A,7\n"
        b"INV-205,CUS-22,12500.50,USD,P,8\n"
    )


@pytest.fixture
def text_pdf() -> bytes:
    fitz = pytest.importorskip("pymupdf", reason="pymupdf is not installed")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 96), "BIRTH CERTIFICATE\nName: Nimal Silva\nDOB: 1997-03-20")
    payload = document.tobytes()
    document.close()

    return payload


@pytest.fixture
def png_bytes() -> bytes:
    pillow = pytest.importorskip("PIL.Image", reason="Pillow is not installed")
    buffer = io.BytesIO()
    pillow.new("RGB", (160, 60), "white").save(buffer, "PNG")

    return buffer.getvalue()


def upload_csv(client, name: str, data: bytes):
    return client.post("/v1/files/csv", files={"file": (name, data, "text/csv")})


def upload_doc(client, name: str, data: bytes, content_type: str):
    return client.post(
        "/v1/files/documents", files={"file": (name, data, content_type)}
    )


# ======================================================================
# FIX 1 - CSV row-count semantics
# ======================================================================


def test_a_sampled_row_count_is_never_reported_as_a_total(client, invoice_csv):
    """The defect: `rows_observed` said 0 for a file that plainly had rows.

    The deeper problem was that it claimed to be a TOTAL while the pipeline only
    ever inspects a bounded sample. Reporting the sample under that name would
    have replaced one false statement with another.
    """
    body = upload_csv(client, "invoices.csv", invoice_csv).json()

    assert body["rows_sampled"] == 2
    # Nothing counted the whole file, so the total is unknown - not zero.
    assert body["rows_observed"] is None


def test_an_empty_csv_reports_zero_sampled_rows(client):
    body = upload_csv(client, "empty.csv", b"a,b\n").json()

    assert body["rows_sampled"] == 0
    assert body["rows_observed"] is None


def test_a_single_row_csv_reports_one_sampled_row(client):
    body = upload_csv(client, "one.csv", b"a,b\n1,2\n").json()

    assert body["rows_sampled"] == 1


def test_a_file_larger_than_the_sample_limit_says_the_sample_was_limited(client):
    """The case that makes the distinction matter.

    With more rows than the inference ceiling, `rows_sampled` is BY DEFINITION
    not the file's row count, and the response has to say so.
    """
    from erp_pipeline.ingestion.models import CsvOptions

    limit = CsvOptions().max_rows_for_schema_inference
    rows = b"".join(f"{n},{n}\n".encode() for n in range(limit + 50))
    body = upload_csv(client, "big.csv", b"a,b\n" + rows).json()

    assert body["sample_limited"] is True
    assert body["rows_sampled"] == limit
    assert body["rows_observed"] is None
    # The file really does have more rows than were sampled.
    assert limit < limit + 50


def test_a_small_file_is_not_marked_sample_limited(client, invoice_csv):
    body = upload_csv(client, "invoices.csv", invoice_csv).json()

    assert body["sample_limited"] is False


def test_a_tsv_upload_reports_the_same_row_semantics(client):
    body = upload_csv(client, "orders.tsv", b"a\tb\n1\t2\n").json()

    assert body["rows_sampled"] >= 0
    assert body["rows_observed"] is None


# ======================================================================
# FIX 2 - document identity
# ======================================================================


def test_a_pdf_upload_returns_a_document_id(client, text_pdf):
    body = upload_doc(client, "cert.pdf", text_pdf, "application/pdf").json()

    assert body["document_id"]


def test_identical_bytes_produce_the_same_document_id(client, text_pdf):
    """Content-addressed identity, matching what `chunk_document` already uses.

    This is what lets a chunk indexed later be traced back to the document an
    upload returned.
    """
    first = upload_doc(client, "cert.pdf", text_pdf, "application/pdf").json()
    second = upload_doc(client, "renamed.pdf", text_pdf, "application/pdf").json()

    assert first["document_id"] == second["document_id"]


def test_different_content_produces_a_different_document_id(client, text_pdf):
    fitz = pytest.importorskip("pymupdf")
    other = fitz.open()
    other.new_page().insert_text((72, 96), "A COMPLETELY DIFFERENT DOCUMENT")
    payload = other.tobytes()
    other.close()

    first = upload_doc(client, "a.pdf", text_pdf, "application/pdf").json()
    second = upload_doc(client, "b.pdf", payload, "application/pdf").json()

    assert first["document_id"] != second["document_id"]


def test_an_image_upload_returns_a_document_id(client, png_bytes):
    body = upload_doc(client, "scan.png", png_bytes, "image/png").json()

    assert body["document_id"]


def test_a_jpeg_upload_returns_a_document_id(client):
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (80, 40), "white").save(buffer, "JPEG")
    body = upload_doc(client, "s.jpg", buffer.getvalue(), "image/jpeg").json()

    assert body["document_id"]


def test_the_document_id_matches_the_identity_chunking_would_derive(
    client, text_pdf
):
    """The id must be the one `ai.chunking` uses, or it references nothing."""
    body = upload_doc(client, "cert.pdf", text_pdf, "application/pdf").json()

    assert body["document_id"] == body["content_hash"]


# ======================================================================
# FIX 3 - OCR flag
# ======================================================================


def test_a_text_pdf_reports_that_ocr_was_not_used(client, text_pdf):
    body = upload_doc(client, "cert.pdf", text_pdf, "application/pdf").json()

    assert body["ocr_used"] is False
    assert body["extraction_status"]


def test_ocr_used_is_derived_from_the_extractors_own_page_marker():
    """The flag must read `ExtractedPage.extraction_method`, not a second
    invented notion of OCR state - which is why it was permanently False."""
    from erp_pipeline.api.routers_data import OCR_EXTRACTION_METHOD, _ocr_was_used

    class Page:
        def __init__(self, method):
            self.extraction_method = method

    class Doc:
        def __init__(self, methods):
            self.pages = [Page(m) for m in methods]

    assert _ocr_was_used(Doc(["text_layer"])) is False
    assert _ocr_was_used(Doc(["none"])) is False
    assert _ocr_was_used(Doc([])) is False
    assert _ocr_was_used(Doc([OCR_EXTRACTION_METHOD])) is True
    # A mixed PDF: one scanned page among text pages still counts.
    assert _ocr_was_used(Doc(["text_layer", "ocr", "text_layer"])) is True


def test_ocr_unavailable_is_not_reported_as_ocr_used(client, png_bytes):
    """OCR that could not run was not used. "Available" is a different fact."""
    body = upload_doc(client, "scan.png", png_bytes, "image/png").json()

    if body["extraction_status"] == "ocr_unavailable":
        assert body["ocr_used"] is False


# ======================================================================
# FIX 5 - warning serialization
# ======================================================================


def test_warnings_never_leak_a_python_repr(client, png_bytes):
    """`str()` on a dataclass renders its class name, attribute names and None
    padding into a field a user reads."""
    body = upload_doc(client, "scan.png", png_bytes, "image/png").json()

    for warning in body["warnings"]:
        assert isinstance(warning, str)
        assert "ExtractionWarning(" not in warning
        assert "row_number=" not in warning
        assert "column_index=" not in warning
        assert "object at 0x" not in warning
        assert not warning.startswith("<")


def test_a_warning_keeps_its_category_and_message(client, png_bytes):
    body = upload_doc(client, "scan.png", png_bytes, "image/png").json()

    if body["warnings"]:
        assert ":" in body["warnings"][0]


def test_plain_string_warnings_are_passed_through_unchanged():
    from erp_pipeline.api.routers_data import _warning_messages

    class Result:
        warnings = ("already a plain sentence",)

    assert _warning_messages(Result()) == ["already a plain sentence"]


# ======================================================================
# FIX 6 - malformed / encrypted documents are client errors
# ======================================================================


def test_a_valid_pdf_is_accepted(client, text_pdf):
    assert upload_doc(client, "ok.pdf", text_pdf, "application/pdf").status_code == 201


def test_a_corrupt_pdf_is_a_client_error_not_a_server_fault(client):
    response = upload_doc(
        client, "corrupt.pdf", b"%PDF-1.7\n" + b"\x00" * 60, "application/pdf"
    )

    assert 400 <= response.status_code < 500
    assert response.status_code != 500
    assert response.json()["error"]["code"] != "INTERNAL_ERROR"


def test_a_file_whose_bytes_contradict_its_extension_is_refused_as_a_type_error(
    client,
):
    response = upload_doc(client, "fake.pdf", b"not a pdf at all", "application/pdf")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_UPLOAD"


def test_an_empty_document_upload_is_a_client_error(client):
    response = upload_doc(client, "empty.pdf", b"", "application/pdf")

    assert 400 <= response.status_code < 500


def test_the_client_error_map_covers_the_document_failures():
    """Pins the taxonomy so a future extractor error is a deliberate decision."""
    from erp_pipeline.api.routers_data import _CLIENT_INGESTION_ERRORS
    from erp_pipeline.ingestion.errors import (
        EncryptedPDFError,
        ImageDecodeError,
        MalformedCSVError,
        MalformedPDFError,
    )

    for error in (
        MalformedPDFError,
        EncryptedPDFError,
        ImageDecodeError,
        MalformedCSVError,
    ):
        assert error in _CLIENT_INGESTION_ERRORS


def test_an_unexpected_internal_error_still_surfaces_as_500(app, monkeypatch):
    """The conversion must not become a blanket exception swallow.

    Uses its own client with ``raise_server_exceptions=False``: by default the
    test client re-raises a server fault instead of returning the response the
    application actually produced, so the 500 would never be observable.
    """
    from fastapi.testclient import TestClient

    from erp_pipeline.api import routers_data

    def explode(service, upload_id):
        raise RuntimeError("a genuine programming fault")

    monkeypatch.setattr(routers_data, "_ingest_upload_or_refuse", explode)

    strict = TestClient(app, raise_server_exceptions=False)
    response = upload_csv(strict, "x.csv", b"a,b\n1,2\n")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


# ======================================================================
# FIX 7 - CSV logical entity name
# ======================================================================


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("invoices.csv", "invoices"),
        ("employees.csv", "employees"),
        ("purchase_orders.tsv", "purchase_orders"),
        ("my.data.csv", "my.data"),
    ],
)
def test_the_logical_entity_name_drops_the_file_extension(
    client, filename, expected
):
    """Phase 8 matches `source_name` against the canonical model, so a trailing
    ".csv" weakened the entity signal on every uploaded file."""
    body = upload_csv(client, filename, b"a,b\n1,2\n").json()
    schema = client.get(f"/v1/schemas/{body['schema_id']}").json()
    entity = schema["entities"][0]

    assert entity["source_name"] == expected


def test_the_original_filename_is_still_preserved_as_provenance(
    client, invoice_csv
):
    """Only the SEMANTIC name changed. Identity and provenance must not move."""
    body = upload_csv(client, "invoices.csv", invoice_csv).json()

    assert body["filename"] == "invoices.csv"
    assert body["content_hash"]


# ======================================================================
# FIX 4 - mapping target path
# ======================================================================


def test_a_selected_mapping_reports_the_target_it_selected(client, invoice_csv):
    """The engine recorded its choice on `selected.qualified_target`; the
    response read a `target_path` attribute that does not exist, so every
    decision reported null - including auto-selected, high-confidence ones."""
    upload = upload_csv(client, "invoices.csv", invoice_csv).json()
    result = client.post(
        "/v1/mappings/suggest", json={"schema_id": upload["schema_id"]}
    ).json()

    selected = [d for d in result["decisions"] if d["outcome"] == "auto_selected"]

    assert selected, "expected at least one auto-selected decision"

    for decision in selected:
        assert decision["target_path"], (
            f"{decision['source_field']} was auto-selected but reported no target"
        )
        assert "." in decision["target_path"]


def test_an_unselected_decision_still_reports_no_target(client, invoice_csv):
    """Null is the correct answer when nothing was chosen."""
    upload = upload_csv(client, "invoices.csv", invoice_csv).json()
    result = client.post(
        "/v1/mappings/suggest", json={"schema_id": upload["schema_id"]}
    ).json()

    for decision in result["decisions"]:
        if decision["outcome"] in {"unmapped", "ambiguous"}:
            assert decision["target_path"] is None


def test_confidence_is_null_rather_than_the_string_none(client, invoice_csv):
    """`str(None)` produced the literal "None", indistinguishable from a level."""
    upload = upload_csv(client, "invoices.csv", invoice_csv).json()
    result = client.post(
        "/v1/mappings/suggest", json={"schema_id": upload["schema_id"]}
    ).json()

    for decision in result["decisions"]:
        assert decision["confidence"] != "None"


def test_the_target_helper_reads_the_engines_own_record():
    from erp_pipeline.api.routers_data import _selected_target

    class Candidate:
        qualified_target = "invoice.amount"

    class Decision:
        selected = Candidate()

    class Undecided:
        selected = None

    assert _selected_target(Decision()) == "invoice.amount"
    assert _selected_target(Undecided()) is None
