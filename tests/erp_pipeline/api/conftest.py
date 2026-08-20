"""Fixtures for the Phase 13 API tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemorySecretProvider,
    OrchestrationService,
    PipelineServices,
    UploadStore,
)

#: Planted wherever a credential could travel. If any of these appear in a
#: response, a job row, a log line or the OpenAPI document, the redaction
#: guarantee is broken.
SECRET_DB_PASSWORD = "SECRET_DB_PASSWORD_13981"
SECRET_API_KEY = "SECRET_API_KEY_88221"
SECRET_BEARER = "SECRET_BEARER_99112"

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def upload_dir(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


@pytest.fixture
def services(upload_dir: Path) -> PipelineServices:
    """Lightweight services: no model, no vector store, no database."""
    from erp_pipeline.ingestion import FileIngestionService
    from erp_pipeline.api_specs import ApiSpecificationService
    from erp_pipeline.mapping import MappingService
    from erp_pipeline.transformation import TransformationService
    from erp_pipeline.sync import InMemoryCanonicalStore

    return PipelineServices(
        ingestion=FileIngestionService(),
        api_specs=ApiSpecificationService(),
        mapping=MappingService(),
        transformation=TransformationService(),
        records=InMemoryCanonicalStore(),
        uploads=UploadStore(upload_dir),
        secrets=InMemorySecretProvider(),
    )


@pytest.fixture
def orchestration(services: PipelineServices) -> OrchestrationService:
    return OrchestrationService(
        services=services,
        job_store=InMemoryJobStore(),
        executor=InlineJobExecutor(),
    )


@pytest.fixture
def settings(upload_dir: Path) -> ApiSettings:
    return ApiSettings(upload_dir=upload_dir)


@pytest.fixture
def app(settings: ApiSettings, orchestration: OrchestrationService):
    return create_app(settings=settings, orchestration=orchestration)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def csv_bytes() -> bytes:
    """A small ERP-shaped CSV: invoices with a key, amounts and dates."""
    rows = [
        "invoice_id,customer_id,customer_name,amount,currency,status,issued_on",
        "INV-1001,CUS-01,Acme Trading,15400.50,LKR,approved,2025-01-15",
        "INV-1002,CUS-02,Beta Supplies,8200.00,USD,pending,2025-02-03",
        "INV-1003,CUS-03,Gamma Logistics,45300.75,EUR,approved,2025-02-19",
        "INV-1004,CUS-01,Acme Trading,2750.25,LKR,rejected,2025-03-07",
        "INV-1005,CUS-04,Delta Foods,19800.00,GBP,settled,2025-03-22",
    ]

    return ("\n".join(rows) + "\n").encode("utf-8")


@pytest.fixture
def qdrant_client():
    """Live Qdrant, or a skip naming what was unreachable."""
    pytest.importorskip("qdrant_client", reason="qdrant-client is not installed")

    from qdrant_client import QdrantClient

    host = os.environ.get("QDRANT_HOST", "localhost")
    port = int(os.environ.get("QDRANT_PORT", "6333"))

    try:
        client = QdrantClient(host=host, port=port, timeout=30)
        client.get_collections()
    except Exception as error:  # pragma: no cover - environment dependent
        pytest.skip(f"live Qdrant unreachable at {host}:{port}: {error!r}")

    return client


@pytest.fixture(scope="session")
def pg_engine():
    """Live PostgreSQL, or a skip naming what was unreachable."""
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg2")

    import sqlalchemy as sa

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # pragma: no cover
        pass

    name = os.getenv("AI_DB_NAME")
    user = os.getenv("AI_DB_USER")

    if not (name and user):
        pytest.skip("AI_DB_* connection settings are not configured")

    url = (
        f"postgresql+psycopg2://{user}:{os.getenv('AI_DB_PASSWORD')}"
        f"@{os.getenv('AI_DB_HOST', 'localhost')}:{os.getenv('AI_DB_PORT', '5432')}"
        f"/{name}"
    )

    try:
        engine = sa.create_engine(url)
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"live PostgreSQL unreachable: {error!r}")

    return engine
