"""Document pipeline, live Uvicorn loopback proof, and the OpenAPI artifact."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemorySecretProvider,
    JobStatus,
    OrchestrationService,
    PipelineServices,
    PipelineStage,
    RegisteredSource,
    StageStatus,
    UploadStore,
)
from erp_pipeline.schemas.enums import SourceType

REPO_ROOT = Path(__file__).resolve().parents[3]
ARTIFACT = REPO_ROOT / "artifacts" / "phase13_openapi.json"

MANDATORY_PATHS = {
    "/v1/sources",
    "/v1/sources/{source_id}",
    "/v1/sources/{source_id}/test",
    "/v1/sources/{source_id}/discover",
    "/v1/files/csv",
    "/v1/files/documents",
    "/v1/api-specs/openapi",
    "/v1/api-specs/postman",
    "/v1/schemas/{schema_id}",
    "/v1/mappings/suggest",
    "/v1/mappings/{mapping_id}",
    "/v1/mappings/{mapping_id}/validate",
    "/v1/jobs",
    "/v1/jobs/{job_id}",
    "/v1/search",
    "/v1/records/{record_id}",
    "/v1/health/live",
    "/v1/health/ready",
    "/v1/capabilities",
}


# ----------------------------------------------------------------------
# Document pipeline
# ----------------------------------------------------------------------


@pytest.fixture
def invoice_pdf(tmp_path: Path) -> bytes:
    """A real PDF with real ERP text, built with PyMuPDF."""
    fitz = pytest.importorskip("fitz", reason="pymupdf is not installed")

    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 96),
        "PURCHASE ORDER PO-88120\n"
        "Supplier: Northwind Components Ltd\n"
        "Total: 42,750.00 EUR\n"
        "Status: approved\n"
        "Raised on: 2025-04-11",
        fontsize=12,
    )
    path = tmp_path / "purchase_order.pdf"
    document.save(str(path))
    document.close()

    return path.read_bytes()


@pytest.fixture
def document_stack(qdrant_client, tmp_path: Path):
    from erp_pipeline.ai import EmbeddingService, SentenceTransformerModel
    from erp_pipeline.ingestion import FileIngestionService
    from erp_pipeline.storage import (
        ColdArchiveTier,
        QdrantHotTier,
        QdrantWarmTier,
        StaticKeyProvider,
        StorageService,
        generate_key,
    )

    token = uuid.uuid4().hex[:8]
    hot_name = f"erp_phase13_test_dochot_{token}"
    warm_name = f"erp_phase13_test_docwarm_{token}"
    hot = QdrantHotTier(qdrant_client, hot_name, 384)
    warm = QdrantWarmTier(qdrant_client, warm_name, 384)
    hot.ensure_collection(recreate=True)
    warm.ensure_collection(recreate=True)

    services = PipelineServices(
        ingestion=FileIngestionService(),
        embedding=EmbeddingService(SentenceTransformerModel()),
        storage=StorageService(
            hot=hot,
            warm=warm,
            cold=ColdArchiveTier(
                tmp_path / "cold", StaticKeyProvider(generate_key())
            ),
        ),
        uploads=UploadStore(tmp_path / "uploads"),
        secrets=InMemorySecretProvider(),
    )
    orchestration = OrchestrationService(
        services=services,
        job_store=InMemoryJobStore(),
        executor=InlineJobExecutor(),
    )
    orchestration.sources.register(
        RegisteredSource(
            source_id="erp_docs", name="ERP Documents", source_type=SourceType.PDF
        )
    )

    from fastapi.testclient import TestClient

    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=orchestration,
    )

    try:
        with TestClient(app) as client:
            yield client, orchestration, hot, warm
    finally:
        for name in (hot_name, warm_name):
            try:
                qdrant_client.delete_collection(name)
            except Exception:
                pass


def test_a_document_uploads_and_reports_safe_metadata_only(
    document_stack, invoice_pdf: bytes
):
    """Never the extracted text: that IS the document."""
    client, orchestration, hot, warm = document_stack

    response = client.post(
        "/v1/files/documents",
        files={"file": ("purchase_order.pdf", invoice_pdf, "application/pdf")},
    )

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["page_count"] >= 1
    assert body["content_hash"]
    assert "Northwind" not in response.text
    assert "PO-88120" not in response.text


def test_the_document_pipeline_needs_no_mapping_or_transformation(
    document_stack, invoice_pdf: bytes
):
    """A document has no columns, so MAP and TRANSFORM never apply."""
    client, orchestration, hot, warm = document_stack

    upload = client.post(
        "/v1/files/documents",
        files={"file": ("purchase_order.pdf", invoice_pdf, "application/pdf")},
    ).json()

    job = client.post(
        "/v1/jobs",
        json={
            "job_type": "document_pipeline",
            "source_id": "erp_docs",
            "upload_id": upload["upload_id"],
        },
    )

    assert job.status_code == 202
    finished = client.get(f"/v1/jobs/{job.json()['job_id']}").json()

    assert finished["status"] == JobStatus.SUCCEEDED.value, finished.get(
        "error_message"
    )

    stages = {run["stage"]: run["status"] for run in finished["stages"]}

    assert stages["ingest"] == StageStatus.SUCCEEDED.value
    assert stages["ai_build"] == StageStatus.SUCCEEDED.value
    assert stages["embed"] == StageStatus.SUCCEEDED.value
    assert stages["tier_route"] == StageStatus.SUCCEEDED.value

    # Not merely skipped - genuinely not applicable to this shape.
    assert stages["map"] == StageStatus.NOT_APPLICABLE.value
    assert stages["transform"] == StageStatus.NOT_APPLICABLE.value

    assert finished["counters"]["chunks_built"] >= 1
    assert finished["counters"]["vectors_stored"] >= 1
    assert hot.count() + warm.count() >= 1


def test_a_document_chunk_is_searchable(document_stack, invoice_pdf: bytes):
    client, orchestration, hot, warm = document_stack

    upload = client.post(
        "/v1/files/documents",
        files={"file": ("purchase_order.pdf", invoice_pdf, "application/pdf")},
    ).json()
    client.post(
        "/v1/jobs",
        json={
            "job_type": "document_pipeline",
            "source_id": "erp_docs",
            "upload_id": upload["upload_id"],
        },
    )

    hits = client.post(
        "/v1/search",
        json={"query": "approved purchase order from Northwind Components", "top_k": 5},
    ).json()

    assert hits["hits"]


# ----------------------------------------------------------------------
# CRITICAL PROOF F - real Uvicorn over loopback
# ----------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))

        return probe.getsockname()[1]


def test_a_real_uvicorn_server_serves_the_api_over_loopback(tmp_path: Path):
    """TestClient bypasses the network. This does not.

    Starts the actual ASGI app under Uvicorn on 127.0.0.1 and makes real HTTP
    requests, then shuts it down. Bound to loopback only - never exposed.
    """
    pytest.importorskip("uvicorn")
    httpx = pytest.importorskip("httpx")

    port = _free_port()
    script = tmp_path / "serve.py"
    script.write_text(
        "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})",
                "import uvicorn",
                "from erp_pipeline.api import create_app, ApiSettings",
                "from erp_pipeline.orchestration import (",
                "    OrchestrationService, PipelineServices, InMemoryJobStore,",
                "    InlineJobExecutor, RegisteredSource, UploadStore,",
                ")",
                "from erp_pipeline.schemas.enums import SourceType",
                "from erp_pipeline.orchestration import PipelineStage",
                f"uploads = UploadStore({str(tmp_path / 'up')!r})",
                "services = PipelineServices(uploads=uploads)",
                "orch = OrchestrationService(",
                "    services=services, job_store=InMemoryJobStore(),",
                "    executor=InlineJobExecutor(),",
                "    handlers={stage: (lambda ctx: {}) for stage in PipelineStage},",
                ")",
                "orch.sources.register(RegisteredSource(",
                "    source_id='erp_db', name='ERP DB',",
                "    source_type=SourceType.POSTGRESQL))",
                f"app = create_app(settings=ApiSettings(upload_dir={str(tmp_path / 'up')!r}),"
                " orchestration=orch)",
                f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')",
            ]
        ),
        encoding="utf-8",
    )

    server = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"

    try:
        deadline = time.time() + 60
        ready = False

        while time.time() < deadline:
            if server.poll() is not None:
                out, err = server.communicate()
                pytest.fail(f"server exited: {err.decode(errors='replace')[:800]}")

            try:
                if httpx.get(f"{base}/v1/health/live", timeout=2).status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.3)

        assert ready, "the uvicorn server never became reachable"

        live = httpx.get(f"{base}/v1/health/live", timeout=10)
        assert live.status_code == 200
        assert live.json()["status"] == "alive"
        assert live.headers["X-Request-ID"]

        capabilities = httpx.get(f"{base}/v1/capabilities", timeout=10)
        assert capabilities.status_code == 200
        assert capabilities.json()["job_types"]

        created = httpx.post(
            f"{base}/v1/jobs",
            json={"job_type": "structured_pipeline", "source_id": "erp_db"},
            timeout=30,
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        fetched = httpx.get(f"{base}/v1/jobs/{job_id}", timeout=10)
        assert fetched.status_code == 200
        assert fetched.json()["job_id"] == job_id

        missing = httpx.get(f"{base}/v1/jobs/job_nope", timeout=10)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "JOB_NOT_FOUND"

        assert httpx.get(f"{base}/openapi.json", timeout=10).status_code == 200
    finally:
        server.terminate()

        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()


# ----------------------------------------------------------------------
# The published OpenAPI artifact
# ----------------------------------------------------------------------


def test_the_openapi_artifact_is_generated_from_the_real_app(tmp_path: Path):
    """Exported from FastAPI, never hand-written - a second one would drift."""
    app = create_app(settings=ApiSettings(upload_dir=tmp_path))
    spec = app.openapi()

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(spec, indent=2, sort_keys=True), encoding="utf-8"
    )

    assert ARTIFACT.exists()

    published = json.loads(ARTIFACT.read_text(encoding="utf-8"))

    assert MANDATORY_PATHS <= set(published["paths"])
    assert published["info"]["title"]

    operations = [
        operation
        for path in published["paths"].values()
        for operation in path.values()
    ]
    ids = [operation["operationId"] for operation in operations]

    assert len(ids) == len(set(ids))
    assert len(operations) >= 20

    # The artifact must not carry a secret example either.
    rendered = json.dumps(published)
    for planted in (
        "SECRET_DB_PASSWORD_13981",
        "SECRET_API_KEY_88221",
        "SECRET_BEARER_99112",
    ):
        assert planted not in rendered
