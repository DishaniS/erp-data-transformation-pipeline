"""Phase 6 - an uploaded document indexes itself.

Before this phase, uploading a PDF extracted it and stopped; something had to
notice and post a second job. The headline test is
``test_uploading_emp002s_certificate_makes_it_searchable_with_no_second_call``:
one HTTP call in, filtered search and resolved text out.

The second thing these tests defend is that the convenience did not cost
anything. CSV still stops at schema inference, the manual job route still
works, a file-only upload still works, and no ERP identity is ever guessed from
a filename.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest

from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.ingestion import FileIngestionService
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemoryRepresentationStore,
    OrchestrationService,
    PipelineServices,
    UploadStore,
)
from erp_pipeline.orchestration.document_identity import DocumentIdentity
from erp_pipeline.orchestration.models import JobType, PipelineStage
from erp_pipeline.orchestration.planner import PipelinePlanner
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync import InMemoryCanonicalStore

from tests.erp_pipeline.api.test_search_resolution_and_filters import (  # noqa: E402
    DIMENSION,
    DeterministicTestModel,
    InProcessTier,
    PatchedStorage,
)


def _load_project_env() -> None:
    """Load ``.env`` before any OCR probe, as the ingestion conftest does."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")


_load_project_env()

CERTIFICATE_LINES = [
    "BIRTH CERTIFICATE",
    "Registrar General, Colombo",
    "Name: Nimal Silva",
]


def _pdf(lines) -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    page = document.new_page()

    for index, line in enumerate(lines):
        page.insert_text((56, 70 + index * 22), line, fontsize=11)

    payload = document.tobytes()
    document.close()

    return payload


def _image_of_text(text: str) -> bytes:
    """A PNG containing readable text - the OCR path."""
    fitz = pytest.importorskip("pymupdf")
    typed = fitz.open()
    typed.new_page(width=420, height=180).insert_text((28, 100), text, fontsize=26)
    bitmap = typed.load_page(0).get_pixmap(dpi=300).tobytes("png")
    typed.close()

    return bitmap


def _blank_png() -> bytes:
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (200, 100), "white").save(buffer, "PNG")

    return buffer.getvalue()


class Harness:
    """A full application with every service wired, and an inline executor."""

    def __init__(self, tmp_path):
        self.representations = InMemoryRepresentationStore()
        self.storage = PatchedStorage(
            hot=InProcessTier(), state_store=InMemoryTierStateStore()
        )
        self.services = PipelineServices(
            ingestion=FileIngestionService(),
            uploads=UploadStore(tmp_path / "uploads"),
            records=InMemoryCanonicalStore(),
            representations=self.representations,
            storage=self.storage,
            embedding=EmbeddingService(DeterministicTestModel(dimension=DIMENSION)),
        )
        self.orchestration = OrchestrationService(
            services=self.services,
            job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        )
        self.app = create_app(
            settings=ApiSettings(upload_dir=tmp_path / "uploads"),
            orchestration=self.orchestration,
        )


@pytest.fixture
def harness(tmp_path):
    pytest.importorskip("pymupdf")

    return Harness(tmp_path)


@pytest.fixture
def client(harness):
    from fastapi.testclient import TestClient

    with TestClient(harness.app) as test_client:
        yield test_client


def upload(client, payload: bytes, name="document.pdf", content_type="application/pdf",
           **identity):
    return client.post(
        "/v1/files/documents",
        files={"file": (name, payload, content_type)},
        data={key: value for key, value in identity.items() if value is not None},
    )


def search(client, **filters):
    return client.post(
        "/v1/search",
        json={"query": "birth certificate details", "top_k": 20, "filters": filters},
    ).json()["hits"]


def resolve(client, representation_id: str):
    return client.get(f"/v1/representations/{representation_id}")


# ======================================================================
# TEST O / A - the headline
# ======================================================================


def test_uploading_emp002s_certificate_makes_it_searchable_with_no_second_call(
    client, harness
):
    """One HTTP call in; filtered search and resolved text out."""
    response = upload(
        client,
        _pdf(CERTIFICATE_LINES),
        name="scan.pdf",
        source_system_id="legacy_hr",
        source_entity="employees",
        business_key_name="employee_id",
        business_key_value="EMP002",
        document_type="birth_certificate",
    )

    assert response.status_code == 201

    body = response.json()

    assert body["index_job_id"]
    assert body["indexing_status"] == "succeeded"
    assert body["indexing_error"] is None

    # Exactly one job, created by the upload - no manual POST /v1/jobs.
    jobs = client.get("/v1/jobs").json()

    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "document_pipeline"

    hits = search(
        client,
        business_key_name="employee_id",
        business_key_value="EMP002",
        document_type="birth_certificate",
        content_kind="document_chunk",
    )

    assert hits

    resolved = resolve(client, hits[0]["representation_id"]).json()

    assert "BIRTH CERTIFICATE" in resolved["text"]
    assert "Nimal Silva" in resolved["text"]
    assert resolved["business_key_value"] == "EMP002"
    assert resolved["document_type"] == "birth_certificate"
    assert resolved["content_kind"] == "document_chunk"


def test_a_plain_pdf_upload_indexes_itself(client):
    body = upload(client, _pdf(["COMPANY POLICY", "Travel and expenses"])).json()

    assert body["index_job_id"]
    assert body["indexing_status"] == "succeeded"

    hits = search(client, content_kind="document_chunk")

    assert hits
    assert resolve(client, hits[0]["representation_id"]).status_code == 200


# ======================================================================
# TEST B - image upload through OCR
# ======================================================================


def test_an_image_upload_is_ocred_and_indexed(client):
    from erp_pipeline.ingestion.ocr import probe_ocr

    if not probe_ocr().available:
        pytest.skip("OCR is unavailable on this machine")

    body = upload(
        client,
        _image_of_text("BIRTH CERTIFICATE"),
        name="scan.png",
        content_type="image/png",
        business_key_name="employee_id",
        business_key_value="EMP002",
        document_type="birth_certificate",
    ).json()

    assert body["ocr_used"] is True
    assert body["indexing_status"] == "succeeded"

    hits = search(client, business_key_value="EMP002")

    assert hits

    text = resolve(client, hits[0]["representation_id"]).json()["text"]

    assert "CERTIFICATE" in text.upper()


# ======================================================================
# TEST D - no identity is ever guessed
# ======================================================================


def test_a_filename_never_becomes_an_erp_identity(client, harness):
    """``EMP999_birth_certificate.pdf`` with no metadata declares nothing."""
    body = upload(client, _pdf(CERTIFICATE_LINES), name="EMP999_birth_certificate.pdf")

    assert body.status_code == 201

    stored = [
        harness.representations.get(key)
        for key in harness.representations.list_ids()
    ]

    assert stored

    for representation in stored:
        assert representation.metadata.get("business_key_value") is None
        assert representation.metadata.get("business_key_name") is None
        assert representation.metadata.get("document_type") is None

    # And the identity filter finds nothing, because nothing was declared.
    assert search(client, business_key_value="EMP999") == []


def test_ocr_text_never_becomes_an_erp_identity(client, harness):
    """The certificate says "Nimal Silva"; that does not make it an identity."""
    upload(client, _pdf(CERTIFICATE_LINES))

    for key in harness.representations.list_ids():
        metadata = harness.representations.get(key).metadata

        assert metadata.get("business_key_value") is None


# ======================================================================
# TEST E - the business key is one declaration in two fields
# ======================================================================


@pytest.mark.parametrize(
    "identity",
    [
        {"business_key_name": "employee_id"},
        {"business_key_value": "EMP002"},
    ],
)
def test_half_a_business_key_is_refused(client, identity):
    response = upload(client, _pdf(CERTIFICATE_LINES), **identity)

    assert response.status_code == 422
    assert "business_key" in response.text


def test_a_refused_upload_starts_no_job(client):
    upload(client, _pdf(CERTIFICATE_LINES), business_key_name="employee_id")

    assert client.get("/v1/jobs").json() == []


def test_both_halves_together_are_accepted(client):
    response = upload(
        client,
        _pdf(CERTIFICATE_LINES),
        business_key_name="employee_id",
        business_key_value="EMP002",
    )

    assert response.status_code == 201


def test_identity_fields_reject_credentials():
    """The declaration carries ERP identifiers, not connection strings."""
    from erp_pipeline.orchestration.errors import InvalidPipelineRequestError

    for value in (
        "postgresql://user:pw@host/db",
        "Authorization: Bearer abc",
        "password=hunter2",
    ):
        with pytest.raises(InvalidPipelineRequestError):
            DocumentIdentity.declare(source_system_id=value)


def test_an_over_long_identity_value_is_refused():
    from erp_pipeline.orchestration.errors import InvalidPipelineRequestError

    with pytest.raises(InvalidPipelineRequestError):
        DocumentIdentity.declare(document_type="x" * 500)


# ======================================================================
# TEST F - same file, same association
# ======================================================================


def test_reuploading_the_same_document_creates_no_duplicate_corpus_entry(
    client, harness
):
    payload = _pdf(CERTIFICATE_LINES)
    identity = {
        "business_key_name": "employee_id",
        "business_key_value": "EMP002",
        "document_type": "birth_certificate",
    }

    first = upload(client, payload, **identity).json()
    after_first = harness.representations.count()
    second = upload(client, payload, **identity).json()

    assert first["document_id"] == second["document_id"]
    # Two uploads, two jobs - jobs are execution history.
    assert len(client.get("/v1/jobs").json()) == 2
    # But one document, one representation.
    assert harness.representations.count() == after_first

    hits = search(client, business_key_value="EMP002")

    assert len(hits) == after_first


def test_a_reupload_without_identity_is_also_idempotent(client, harness):
    payload = _pdf(["COMPANY POLICY"])

    upload(client, payload)
    after_first = harness.representations.count()
    upload(client, payload)

    assert harness.representations.count() == after_first


# ======================================================================
# TEST G - same file, different employee
# ======================================================================


def test_one_certificate_uploaded_for_two_employees_stays_separate(
    client, harness
):
    """The Phase 3 collision, reachable through the upload path.

    Identical bytes, so identical content identity - and the attachment
    identity must still keep them apart, or EMP003's upload silently overwrites
    EMP002's vector.
    """
    payload = _pdf(CERTIFICATE_LINES)
    resolved = {}

    for employee in ("EMP002", "EMP003"):
        body = upload(
            client,
            payload,
            business_key_name="employee_id",
            business_key_value=employee,
            document_type="birth_certificate",
        ).json()
        resolved[employee] = body

    # Content identity is shared, because it IS the same document.
    assert resolved["EMP002"]["document_id"] == resolved["EMP003"]["document_id"]
    # Attachment identity is not.
    assert harness.representations.count() == 2

    for employee in ("EMP002", "EMP003"):
        hits = search(client, business_key_value=employee)

        assert len(hits) == 1

        body = resolve(client, hits[0]["representation_id"]).json()

        assert body["business_key_value"] == employee

    seen = {
        harness.representations.get(key).representation_id
        for key in harness.representations.list_ids()
    }

    assert len(seen) == 2


def test_the_emp002_filter_never_returns_emp003s_upload(client):
    payload = _pdf(CERTIFICATE_LINES)

    for employee in ("EMP002", "EMP003"):
        upload(
            client,
            payload,
            business_key_name="employee_id",
            business_key_value=employee,
            document_type="birth_certificate",
        )

    for employee in ("EMP002", "EMP003"):
        for hit in search(client, business_key_value=employee):
            body = resolve(client, hit["representation_id"]).json()

            assert body["business_key_value"] == employee


def test_an_upload_without_a_parent_reports_no_parent(client, harness):
    """No parent record is invented from a business key."""
    upload(
        client,
        _pdf(CERTIFICATE_LINES),
        business_key_name="employee_id",
        business_key_value="EMP002",
    )
    hits = search(client, business_key_value="EMP002")
    body = resolve(client, hits[0]["representation_id"]).json()

    assert body["parent_record_id"] is None
    assert body["business_key_value"] == "EMP002"


def test_a_declared_parent_record_is_preserved_exactly(client):
    upload(
        client,
        _pdf(CERTIFICATE_LINES),
        parent_record_id="erp:legacy_hr:employees:emp002",
        business_key_name="employee_id",
        business_key_value="EMP002",
    )
    hits = search(client, parent_record_id="erp:legacy_hr:employees:emp002")

    assert hits

    body = resolve(client, hits[0]["representation_id"]).json()

    assert body["parent_record_id"] == "erp:legacy_hr:employees:emp002"


# ======================================================================
# TEST H - uploads with no ERP identity at all
# ======================================================================


def test_a_document_with_no_erp_identity_still_indexes_and_searches(client):
    body = upload(
        client, _pdf(["COMPANY POLICY", "Travel and expenses"]), name="policy.pdf"
    ).json()

    assert body["indexing_status"] == "succeeded"

    hits = search(client, content_kind="document_chunk")

    assert hits

    resolved = resolve(client, hits[0]["representation_id"]).json()

    assert "COMPANY POLICY" in resolved["text"]
    assert resolved["business_key_value"] is None
    assert resolved["document_type"] is None
    assert resolved["content_kind"] == "document_chunk"


def test_an_uploaded_chunk_is_a_document_chunk_not_a_new_kind(client, harness):
    """Arrival mechanism and content kind are different concepts."""
    upload(client, _pdf(["ANYTHING"]))

    kinds = {
        harness.representations.get(key).metadata.get("content_kind")
        for key in harness.representations.list_ids()
    }

    assert kinds == {"document_chunk"}


# ======================================================================
# TEST I / J - things that must not be indexed
# ======================================================================


def test_a_corrupt_pdf_is_refused_and_indexes_nothing(client, harness):
    response = upload(client, b"%PDF-1.7\n" + b"\x00" * 200, name="broken.pdf")

    assert 400 <= response.status_code < 500
    assert harness.representations.count() == 0
    assert client.get("/v1/jobs").json() == []


def test_an_unreadable_image_indexes_no_fabricated_text(client, harness):
    """A blank image produces no chunk, so there is nothing to index."""
    response = upload(client, _blank_png(), name="blank.png", content_type="image/png")

    assert response.status_code == 201

    for key in harness.representations.list_ids():
        assert (harness.representations.get(key).text_for_ai or "").strip()


# ======================================================================
# TEST K - indexing failure is reported, never hidden
# ======================================================================


def test_an_upload_survives_a_scheduling_failure_and_says_so(tmp_path):
    """The bytes are stored and extracted; only indexing did not start."""
    from fastapi.testclient import TestClient

    built = Harness(tmp_path)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("executor unavailable")

    built.orchestration.submit = refuse

    with TestClient(built.app) as client:
        response = upload(client, _pdf(CERTIFICATE_LINES))

    assert response.status_code == 201

    body = response.json()

    assert body["upload_id"]
    assert body["document_id"]
    assert body["index_job_id"] is None
    assert body["indexing_status"] is None
    assert "could not be started" in body["indexing_error"]
    assert any("POST /v1/jobs" in warning for warning in body["warnings"])


def test_a_failing_index_job_reports_failure_and_indexes_nothing(tmp_path):
    from fastapi.testclient import TestClient

    built = Harness(tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("embedding model unavailable")

    built.services.embedding.embed_many = explode

    with TestClient(built.app) as client:
        body = upload(client, _pdf(CERTIFICATE_LINES)).json()

        assert body["index_job_id"]
        assert body["indexing_status"] == "failed"

        job = client.get(f"/v1/jobs/{body['index_job_id']}").json()

        assert job["status"] == "failed"
        # Nothing became searchable.
        assert search(client, content_kind="document_chunk") == []
        # And the manual route remains available to retry.
        retry = client.post(f"/v1/jobs/{body['index_job_id']}/retry")

        assert retry.status_code in (202, 409, 422)


# ======================================================================
# TEST L - CSV is untouched
# ======================================================================


def test_a_csv_upload_never_indexes_its_rows(client, harness):
    """Phase 7 made the CSV's STRUCTURE searchable. Its ROWS are not.

    That distinction is the whole point of the mapping review: a caller must
    not be able to learn that ``INV-1`` exists, or what it is worth, by
    uploading a file and searching. Structure is metadata; rows are data, and
    data still requires a mapping decision before anything about it is indexed.
    """
    csv_bytes = b"""invoice_id,customer_id,amount,currency
INV-1,CUS-1,100.00,LKR
"""
    response = client.post(
        "/v1/files/csv", files={"file": ("ledger.csv", csv_bytes, "text/csv")}
    )

    assert response.status_code == 201

    body = response.json()

    assert body["schema_id"]
    # A DOCUMENT indexing job is still not something a CSV starts.
    assert "index_job_id" not in body

    kinds = {
        (harness.representations.get(key).metadata or {}).get("content_kind")
        for key in harness.representations.list_ids()
    }

    # Structure, yes. Rows, never.
    assert "structured_record" not in kinds
    assert kinds <= {"schema"}

    # And no row value is reachable through anything that was indexed.
    for key in harness.representations.list_ids():
        text = harness.representations.get(key).text_for_ai or ""

        for value in ("INV-1", "CUS-1", "100.00", "LKR"):
            assert value not in text


def test_a_csv_upload_does_index_its_schema(client, harness):
    """The Phase 7 addition, asserted explicitly rather than implied."""
    csv_bytes = b"""invoice_id,customer_id,amount
INV-1,CUS-1,100.00
"""
    body = client.post(
        "/v1/files/csv", files={"file": ("ledger.csv", csv_bytes, "text/csv")}
    ).json()

    assert body["schema_index_job_id"]
    assert body["schema_indexing_status"] == "succeeded"
    assert harness.representations.count() >= 1


# ======================================================================
# TEST M - the manual route is preserved
# ======================================================================


def test_the_manual_document_job_route_still_works(client, harness):
    """Uploads now self-index, so the manual job re-indexes the same upload."""
    body = upload(client, _pdf(CERTIFICATE_LINES)).json()
    before = harness.representations.count()

    response = client.post(
        "/v1/jobs",
        json={"job_type": "document_pipeline", "upload_id": body["upload_id"]},
    )

    assert response.status_code == 202

    manual = client.get(f"/v1/jobs/{response.json()['job_id']}").json()

    assert manual["status"] == "succeeded"
    # Deterministic ids: re-indexing replaces, never accumulates.
    assert harness.representations.count() == before


def test_the_manual_route_accepts_a_historical_upload(client):
    body = upload(client, _pdf(["OLD DOCUMENT"])).json()
    response = client.post(
        "/v1/jobs",
        json={"job_type": "document_pipeline", "upload_id": body["upload_id"]},
    )

    assert response.status_code == 202


# ======================================================================
# TEST N - Phase 5 ordering survives the automatic path
# ======================================================================


def test_the_document_plan_persists_before_it_embeds():
    from erp_pipeline.orchestration.models import JobRequest
    from erp_pipeline.schemas.enums import SourceType

    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.DOCUMENT_PIPELINE, upload_id="up_1"),
        SourceType.PDF,
    )
    stages = list(plan.stages)

    assert stages.index(PipelineStage.PERSIST_REPRESENTATIONS) < stages.index(
        PipelineStage.EMBED
    )
    assert stages.index(PipelineStage.EMBED) < stages.index(
        PipelineStage.TIER_ROUTE
    )


def test_the_automatic_job_runs_the_stages_in_that_order(client):
    body = upload(client, _pdf(CERTIFICATE_LINES)).json()
    job = client.get(f"/v1/jobs/{body['index_job_id']}").json()
    succeeded = [
        stage["stage"] for stage in job["stages"] if stage["status"] == "succeeded"
    ]

    assert succeeded.index("persist_representations") < succeeded.index("embed")
    assert succeeded.index("embed") < succeeded.index("tier_route")


# ======================================================================
# Extraction happens once
# ======================================================================


def test_the_document_is_extracted_once_not_twice(client, harness, monkeypatch):
    """The upload extracts it; the job must not OCR the same file again."""
    calls = {"count": 0}
    original = harness.services.ingestion.ingest

    def counting(path):
        calls["count"] += 1
        return original(path)

    monkeypatch.setattr(harness.services.ingestion, "ingest", counting)
    upload(client, _pdf(CERTIFICATE_LINES))

    assert calls["count"] == 1


# ======================================================================
# Content safety
# ======================================================================


def test_no_raw_bytes_reach_the_search_or_resolution_surface(client):
    payload = _pdf(CERTIFICATE_LINES)
    upload(client, payload, business_key_name="employee_id", business_key_value="EMP002")

    hits = search(client, content_kind="document_chunk")
    surface = json.dumps(
        [resolve(client, hit["representation_id"]).json() for hit in hits]
        + [dict(hit) for hit in hits]
    )

    assert base64.b64encode(payload).decode()[:24] not in surface
    assert "JVBERi0x" not in surface
    assert "%PDF-" not in surface


def test_no_text_reaches_the_vector_payload(client, harness):
    from erp_pipeline.storage.migration import _payload_for

    upload(client, _pdf(CERTIFICATE_LINES))
    surface = json.dumps(
        [_payload_for(state) for state in harness.storage.state.list_all()],
        default=str,
    )

    assert "BIRTH CERTIFICATE" not in surface
    assert "text_for_ai" not in surface


def test_the_upload_response_never_contains_the_extracted_text(client):
    body = upload(client, _pdf(CERTIFICATE_LINES)).json()

    assert "text" not in body
    assert "BIRTH CERTIFICATE" not in json.dumps(body)


# ======================================================================
# Backward compatibility
# ======================================================================


def test_a_file_only_upload_still_works(client):
    """What the existing frontend sends today."""
    response = client.post(
        "/v1/files/documents",
        files={"file": ("policy.pdf", _pdf(["POLICY"]), "application/pdf")},
    )

    assert response.status_code == 201

    body = response.json()

    for field in (
        "upload_id", "filename", "content_hash", "size_bytes", "document_id",
        "file_type", "page_count", "extraction_status", "ocr_used", "warnings",
    ):
        assert field in body, field


def test_the_new_response_fields_are_published(harness):
    schema = harness.app.openapi()["components"]["schemas"][
        "DocumentUploadResponse"
    ]

    for field in ("index_job_id", "indexing_status", "indexing_error"):
        assert field in schema["properties"], field


def test_the_identity_form_fields_are_published_and_optional(harness):
    spec = harness.app.openapi()
    body = spec["paths"]["/v1/files/documents"]["post"]["requestBody"]
    reference = list(body["content"].values())[0]["schema"]["$ref"].split("/")[-1]
    schema = spec["components"]["schemas"][reference]

    for field in (
        "source_system_id", "source_entity", "parent_record_id",
        "business_key_name", "business_key_value", "document_type",
    ):
        assert field in schema["properties"], field

    # Only the file is required.
    assert schema.get("required", []) == ["file"]


def test_the_upload_reports_no_searchable_flag(client):
    """Searchability is a job outcome, not an upload outcome."""
    body = upload(client, _pdf(CERTIFICATE_LINES)).json()

    assert "searchable" not in body
    assert "indexed" not in body
