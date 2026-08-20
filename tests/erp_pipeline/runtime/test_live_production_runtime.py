"""Live proof that the production composition root is durable.

Everything here uses the REAL composition root - not a hand-assembled fixture -
against live PostgreSQL. That is the whole point of this pass: before it, every
proof depended on a test wiring the system by hand.

Schemas are created in the real database because they are the ones the
application owns and are `CREATE ... IF NOT EXISTS`. No data is dropped and no
existing schema is modified.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from erp_pipeline.api.config import ApiSettings
from erp_pipeline.runtime import (
    OWNED_SCHEMAS,
    ColdSettings,
    DatabaseSettings,
    QdrantSettings,
    RuntimeSettings,
    bootstrap_all,
    build_pipeline_engine,
    verify_all,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

CSV_BYTES = b"""invoice_id,customer_id,customer_name,amount,currency,status,issued_on
INV-7001,CUS-71,Meridian Tools,3120.00,LKR,approved,2025-06-01
INV-7002,CUS-72,Atlas Chemicals,9840.50,USD,pending,2025-06-08
"""


@pytest.fixture(scope="module")
def live_database() -> DatabaseSettings:
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg2")

    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except Exception:  # pragma: no cover
        pass

    settings = DatabaseSettings.from_environment()

    if not settings.configured:
        pytest.skip("PIPELINE_DB_*/AI_DB_* settings are not configured")

    try:
        import sqlalchemy as sa

        engine = sa.create_engine(settings.url())
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"live PostgreSQL unreachable: {type(error).__name__}")

    return settings


@pytest.fixture(scope="module")
def engine(live_database: DatabaseSettings):
    built = build_pipeline_engine(live_database)
    bootstrap_all(built)

    from erp_pipeline.runtime.persistence import bootstrap_runtime_persistence

    bootstrap_runtime_persistence(built)

    return built


@pytest.fixture
def settings(live_database: DatabaseSettings, tmp_path: Path, monkeypatch):
    """A production configuration pointed at throwaway directories."""
    monkeypatch.setenv(
        "ERP_COLD_ARCHIVE_KEY", base64.b64encode(os.urandom(32)).decode()
    )

    return RuntimeSettings(
        api=ApiSettings(upload_dir=tmp_path / "uploads"),
        database=live_database,
        # Qdrant availability varies; the vector path has its own proofs.
        qdrant=QdrantSettings(enabled=False),
        cold=ColdSettings(enabled=True, directory=tmp_path / "cold"),
        bootstrap_on_startup=True,
    )


@pytest.fixture
def build_app(settings: RuntimeSettings, engine):
    """A factory, so a test can throw the whole application away and rebuild."""
    from erp_pipeline.runtime.application import create_production_app

    def factory():
        return create_production_app(settings, engine)

    return factory


# ----------------------------------------------------------------------
# Bootstrap
# ----------------------------------------------------------------------


def test_bootstrap_creates_every_owned_schema(engine):
    assert verify_all(engine) == ()


def test_bootstrap_is_idempotent(engine):
    """Running it twice must not fail, duplicate or destroy anything."""
    first = bootstrap_all(engine)
    second = bootstrap_all(engine)

    assert first.ok and second.ok
    # By the second run nothing is new.
    assert second.created == ()
    assert set(second.after) >= set(OWNED_SCHEMAS)


# ----------------------------------------------------------------------
# The composition root selects durable implementations
# ----------------------------------------------------------------------


def test_production_composition_uses_no_in_memory_store(build_app):
    """The headline of this pass, asserted directly."""
    app = build_app()
    orchestration = app.state.orchestration
    services = orchestration.services

    selected = {
        "job_store": type(orchestration.jobs).__name__,
        "records": type(services.records).__name__,
        "sources": type(services.sources).__name__,
        "uploads": type(services.uploads).__name__,
        "drafts": type(services.mapping_drafts).__name__,
        "secrets": type(services.secrets).__name__,
    }

    assert selected["job_store"] == "PostgresJobStore"
    assert selected["records"] == "PostgresCanonicalRecordStore"
    assert selected["sources"] == "PostgresSourceRegistry"
    assert selected["uploads"] == "PostgresUploadStore"
    assert selected["drafts"] == "PostgresMappingDraftStore"
    # Credentialed sources must not resolve through NullSecretProvider.
    assert selected["secrets"] == "EnvironmentSecretProvider"

    for name in selected.values():
        assert not name.startswith("InMemory"), f"{name} is not durable"
        assert name != "NullSecretProvider"


def test_the_tier_state_store_is_durable(settings, engine):
    """Even with Qdrant disabled, tier state must not be in-memory."""
    from erp_pipeline.runtime.services import build_storage_service

    storage = build_storage_service(settings, engine)

    assert storage is not None
    assert type(storage.state).__name__ == "PostgresTierStateStore"


def test_the_bounded_executor_is_used_not_the_inline_one(build_app):
    orchestration = build_app().state.orchestration

    assert type(orchestration.executor).__name__ == "JobExecutor"
    assert orchestration.executor.max_workers >= 1


def test_phase_10_is_wired_with_durable_sync_state(build_app):
    services = build_app().state.orchestration.services

    assert services.sync is not None
    assert type(services.sync.state_store).__name__ == "PostgresSyncStateStore"


def test_the_embedding_model_is_not_loaded_during_assembly(build_app):
    """Assembling the app must not download or load MiniLM."""
    services = build_app().state.orchestration.services

    assert services.embedding is not None
    assert services.embedding.loaded is False
    # Reported without loading - the id is configuration, not a weight.
    assert "MiniLM" in services.embedding.model_id


# ----------------------------------------------------------------------
# Restart proofs
# ----------------------------------------------------------------------


def test_a_registered_source_survives_a_restart(build_app):
    from fastapi.testclient import TestClient

    source_id = f"restartsrc{uuid.uuid4().hex[:8]}"

    app = build_app()
    with TestClient(app) as client:
        created = client.post(
            "/v1/sources",
            json={
                "name": source_id,
                "source_type": "postgresql",
                "host": "db.internal",
                "port": 5432,
                "database": "erp",
                "username": "erp_reader",
                "credential_ref": "erp_reader_pw",
            },
        )
        assert created.status_code == 201, created.text
        registered_id = created.json()["source_id"]

    # Throw the entire application away.
    del app, client

    rebuilt = build_app()
    with TestClient(rebuilt) as client2:
        fetched = client2.get(f"/v1/sources/{registered_id}")

        assert fetched.status_code == 200
        body = fetched.json()
        assert body["database"] == "erp"
        # The reference survives; the secret never entered the database.
        assert body["credential_ref"] == "erp_reader_pw"


def test_no_password_column_exists_for_sources(engine):
    """The strongest form of "passwords are never persisted"."""
    import sqlalchemy as sa

    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'erp_runtime' "
                    "AND table_name = 'registered_sources'"
                )
            )
        }

    assert columns
    for forbidden in ("password", "passwd", "secret", "token"):
        assert not any(forbidden in column for column in columns)


def test_an_upload_survives_a_restart(build_app):
    from fastapi.testclient import TestClient

    app = build_app()
    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/files/csv", files={"file": ("invoice.csv", CSV_BYTES, "text/csv")}
        )
        assert uploaded.status_code == 201, uploaded.text
        payload = uploaded.json()

    del app, client

    rebuilt = build_app()
    uploads = rebuilt.state.orchestration.services.uploads
    resolved = uploads.get(payload["upload_id"])

    assert resolved.content_hash == payload["content_hash"]
    # The file itself is still where the metadata says it is.
    assert uploads.path_for(payload["upload_id"]).exists()


def test_a_mapping_draft_survives_a_restart(build_app):
    """An ambiguous mapping awaiting human review must not vanish."""
    from fastapi.testclient import TestClient

    # 'invoices.csv' produces genuine ambiguity where 'invoice.csv' does not.
    ambiguous_csv = CSV_BYTES

    app = build_app()
    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/files/csv",
            files={"file": ("invoices.csv", ambiguous_csv, "text/csv")},
        ).json()
        suggested = client.post(
            "/v1/mappings/suggest", json={"schema_id": uploaded["schema_id"]}
        ).json()

    mapping_id = suggested["mapping_id"]
    assert mapping_id

    if suggested["auto_approved"]:
        pytest.skip("this corpus produced no ambiguity, so there is no draft")

    del app, client

    rebuilt = build_app()
    drafts = rebuilt.state.orchestration.services.mapping_drafts

    assert mapping_id in drafts
    assert drafts.get(mapping_id)["schema_id"] == uploaded["schema_id"]


def test_a_job_and_its_canonical_records_survive_a_restart(build_app, engine):
    """The central persistence proof, through the production composition."""
    import sqlalchemy as sa
    from fastapi.testclient import TestClient

    from erp_pipeline.orchestration import RegisteredSource
    from erp_pipeline.schemas.enums import SourceType

    source_id = f"prodcsv{uuid.uuid4().hex[:8]}"

    app = build_app()
    app.state.orchestration.services.sources.register(
        RegisteredSource(
            source_id=source_id, name=source_id, source_type=SourceType.CSV
        )
    )

    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/files/csv", files={"file": ("invoice.csv", CSV_BYTES, "text/csv")}
        ).json()
        mapped = client.post(
            "/v1/mappings/suggest", json={"schema_id": uploaded["schema_id"]}
        ).json()

        if not mapped["mapping_id"] or not mapped["auto_approved"]:
            pytest.skip("the mapping needs review; covered by the draft test")

        accepted = client.post(
            "/v1/jobs",
            json={
                "job_type": "structured_pipeline",
                "source_id": source_id,
                "schema_id": uploaded["schema_id"],
                "mapping_id": mapped["mapping_id"],
                "upload_id": uploaded["upload_id"],
            },
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]

        # The executor is a real pool, so the job finishes after the response.
        deadline = time.time() + 240
        status = "pending"

        while time.time() < deadline:
            status = client.get(f"/v1/jobs/{job_id}").json()["status"]

            if status not in {"pending", "running"}:
                break

            time.sleep(0.5)

        final = client.get(f"/v1/jobs/{job_id}").json()

    assert status not in {"pending", "running"}, "the job never settled"
    assert final["counters"]["records_transformed"] == 2
    assert final["counters"]["records_read"] == 2

    load_stage = next(s for s in final["stages"] if s["stage"] == "load")
    assert load_stage["outputs"]["records_loaded"] == 2

    # The records really are in PostgreSQL.
    with engine.connect() as connection:
        stored = connection.execute(
            sa.text(
                "SELECT canonical_id FROM erp_runtime.canonical_records "
                "WHERE canonical_id LIKE :pattern"
            ),
            {"pattern": "%inv-700%"},
        ).all()

    assert stored, "no canonical record reached PostgreSQL"
    record_id = stored[0][0]

    del app, client

    rebuilt = build_app()
    with TestClient(rebuilt) as client2:
        recovered = client2.get(f"/v1/jobs/{job_id}")

        assert recovered.status_code == 200
        assert recovered.json()["counters"]["records_transformed"] == 2

        record = client2.get(f"/v1/records/{record_id}")
        assert record.status_code == 200
        assert record.json()["record_id"] == record_id
        # A record endpoint must never return a vector.
        assert "vector" not in record.text.lower()


def test_readiness_reports_the_real_dependencies(build_app):
    from fastapi.testclient import TestClient

    with TestClient(build_app()) as client:
        body = client.get("/v1/health/ready").json()

    names = {check["name"]: check for check in body["dependencies"]}

    assert names["postgresql"]["ready"] is True
    assert "Postgres" in names["job_store"]["detail"]
    assert "NOT DURABLE" not in names["job_store"]["detail"]
    # Readiness must not have loaded the model.
    assert "loads on first use" in (names["embedding_model"]["detail"] or "")


# ----------------------------------------------------------------------
# The documented startup command
# ----------------------------------------------------------------------


def test_the_bootstrap_command_runs_and_is_idempotent():
    """`python -m erp_pipeline.runtime.bootstrap`, as documented."""
    first = subprocess.run(
        [sys.executable, "-m", "erp_pipeline.runtime.bootstrap"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    second = subprocess.run(
        [sys.executable, "-m", "erp_pipeline.runtime.bootstrap", "--verify-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if first.returncode == 2:
        pytest.skip("PostgreSQL is not configured for the subprocess")

    assert first.returncode == 0, first.stdout + first.stderr
    assert "bootstrap complete" in first.stdout
    assert second.returncode == 0
    assert "all owned schemas are present" in second.stdout

    # The command prints configuration, never a value.
    assert "[REDACTED]" in first.stdout


def test_a_real_uvicorn_server_starts_from_the_documented_command(
    settings: RuntimeSettings, tmp_path: Path
):
    """CRITICAL: `python -m erp_pipeline.api` with no test-specific assembly."""
    import socket

    httpx = pytest.importorskip("httpx")
    pytest.importorskip("uvicorn")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    environment = dict(os.environ)
    environment.update(
        {
            "ERP_API_HOST": "127.0.0.1",
            "ERP_API_PORT": str(port),
            "ERP_API_UPLOAD_DIR": str(tmp_path / "uploads"),
            "ERP_COLD_ARCHIVE_DIR": str(tmp_path / "cold"),
            "ERP_COLD_ARCHIVE_KEY": base64.b64encode(os.urandom(32)).decode(),
            "ERP_QDRANT_ENABLED": "false",
            "ERP_BOOTSTRAP_ON_STARTUP": "true",
        }
    )

    server = subprocess.Popen(
        [sys.executable, "-m", "erp_pipeline.api"],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"

    try:
        deadline = time.time() + 120
        ready = False

        while time.time() < deadline:
            if server.poll() is not None:
                out, err = server.communicate()
                pytest.fail(
                    "the server exited: "
                    + (err.decode(errors="replace") or out.decode(errors="replace"))[:900]
                )

            try:
                if httpx.get(f"{base}/v1/health/live", timeout=2).status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.4)

        assert ready, "the documented startup command never became reachable"

        live = httpx.get(f"{base}/v1/health/live", timeout=10)
        assert live.status_code == 200
        assert live.json()["status"] == "alive"

        readiness = httpx.get(f"{base}/v1/health/ready", timeout=20)
        assert readiness.status_code == 200
        dependencies = {
            check["name"]: check for check in readiness.json()["dependencies"]
        }
        assert dependencies["postgresql"]["ready"] is True
        assert "Postgres" in dependencies["job_store"]["detail"]

        capabilities = httpx.get(f"{base}/v1/capabilities", timeout=10)
        assert capabilities.status_code == 200
        assert capabilities.json()["job_types"]

        uploaded = httpx.post(
            f"{base}/v1/files/csv",
            files={"file": ("invoice.csv", CSV_BYTES, "text/csv")},
            timeout=60,
        )
        assert uploaded.status_code == 201, uploaded.text

        missing = httpx.get(f"{base}/v1/jobs/job_does_not_exist", timeout=10)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "JOB_NOT_FOUND"

        assert httpx.get(f"{base}/openapi.json", timeout=10).status_code == 200
    finally:
        server.terminate()

        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()
