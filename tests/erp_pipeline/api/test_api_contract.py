"""Every mandatory endpoint: success, invalid payload, not found, error shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from erp_pipeline.api import API_VERSION, create_app
from erp_pipeline.api.config import ApiSettings

from .conftest import SECRET_API_KEY, SECRET_BEARER, SECRET_DB_PASSWORD

MANDATORY_ROUTES = {
    ("post", "/v1/sources"),
    ("get", "/v1/sources"),
    ("get", "/v1/sources/{source_id}"),
    ("post", "/v1/sources/{source_id}/test"),
    ("post", "/v1/sources/{source_id}/discover"),
    ("post", "/v1/files/csv"),
    ("post", "/v1/files/documents"),
    ("post", "/v1/api-specs/openapi"),
    ("post", "/v1/api-specs/postman"),
    ("get", "/v1/schemas/{schema_id}"),
    ("post", "/v1/mappings/suggest"),
    ("put", "/v1/mappings/{mapping_id}"),
    ("post", "/v1/mappings/{mapping_id}/validate"),
    ("post", "/v1/jobs"),
    ("get", "/v1/jobs"),
    ("get", "/v1/jobs/{job_id}"),
    ("post", "/v1/search"),
    ("get", "/v1/records/{record_id}"),
    ("get", "/v1/health/live"),
    ("get", "/v1/health/ready"),
    ("get", "/v1/capabilities"),
}


# ----------------------------------------------------------------------
# Application shape
# ----------------------------------------------------------------------


def test_importing_the_api_does_not_load_heavy_services():
    """A test run must not pay for a model it never uses.

    If importing the module loaded sentence-transformers or opened a vector
    connection, the API would be untestable without the whole stack running.
    """
    import subprocess
    import sys

    probe = (
        "import sys; sys.path.insert(0, 'src');"
        "import erp_pipeline.api.main;"
        "print('sentence_transformers' in sys.modules,"
        "      'qdrant_client' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"


def test_health_live_is_up(client):
    response = client.get("/v1/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"
    assert response.json()["api_version"] == API_VERSION


def test_every_response_carries_a_request_id(client):
    response = client.get("/v1/health/live")

    assert response.headers["X-Request-ID"]


def test_a_supplied_request_id_is_echoed(client):
    response = client.get("/v1/health/live", headers={"X-Request-ID": "abc123"})

    assert response.headers["X-Request-ID"] == "abc123"


def test_health_ready_reports_dependencies_honestly(client):
    """No embedding model or vector store is configured in this fixture."""
    body = client.get("/v1/health/ready").json()

    assert body["status"] in {"ready", "degraded"}
    names = {dep["name"] for dep in body["dependencies"]}
    assert "vector_storage" in names


def test_liveness_survives_a_broken_vector_store(services, settings):
    """Liveness must not depend on Qdrant, or an outage restarts the API."""

    class ExplodingStorage:
        def health(self):
            raise RuntimeError("qdrant is down")

    from fastapi.testclient import TestClient

    from erp_pipeline.orchestration import (
        InlineJobExecutor,
        InMemoryJobStore,
        OrchestrationService,
    )

    services.storage = ExplodingStorage()
    app = create_app(
        settings=settings,
        orchestration=OrchestrationService(
            services=services,
            job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        ),
    )

    with TestClient(app) as client:
        assert client.get("/v1/health/live").status_code == 200

        ready = client.get("/v1/health/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is False


def test_capabilities_states_its_boundaries(client):
    body = client.get("/v1/capabilities").json()

    assert body["api_version"] == API_VERSION
    assert "postgresql" in body["source_types"]
    assert body["job_types"]

    limitations = " ".join(body["limitations"]).lower()
    assert "never calls" in limitations or "out of scope" in limitations
    assert "no llm" in limitations
    assert "sql server" in limitations  # deferred status stated honestly


# ----------------------------------------------------------------------
# Error contract
# ----------------------------------------------------------------------


def test_missing_resource_is_404_with_a_stable_code(client):
    response = client.get("/v1/sources/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "SOURCE_NOT_FOUND"
    assert body["error"]["request_id"]


def test_missing_job_is_404(client):
    response = client.get("/v1/jobs/job_missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_missing_schema_is_404(client):
    response = client.get("/v1/schemas/schema_missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCHEMA_NOT_FOUND"


def test_missing_record_is_404(client):
    response = client.get("/v1/records/ai:invoice:nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RECORD_NOT_FOUND"


def test_invalid_payload_is_422_with_field_detail(client):
    response = client.post("/v1/sources", json={"name": "", "source_type": "nope"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["detail"]["fields"]


def test_an_unsupported_capability_is_not_a_500(client):
    """Asking an OpenAPI source for records is a boundary, not a crash."""
    client.post(
        "/v1/sources",
        json={"name": "Vendor Spec", "source_type": "openapi"},
    )

    response = client.post(
        "/v1/jobs",
        json={"job_type": "structured_pipeline", "source_id": "vendor_spec"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_errors_never_leak_internals(client):
    """No traceback, no DSN, no file path in an error body."""
    body = client.get("/v1/sources/missing").text.lower()

    for leak in ("traceback", "postgresql://", "psycopg2", "c:\\", "/src/"):
        assert leak not in body


# ----------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------


def test_create_and_read_a_source(client):
    created = client.post(
        "/v1/sources",
        json={
            "name": "ERP Finance",
            "source_type": "postgresql",
            "host": "localhost",
            "port": 5432,
            "database": "erp_finance",
            "username": "erp_reader",
            "credential_ref": "erp_finance_pw",
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["source_id"] == "erp_finance"
    assert body["source_type"] == "postgresql"

    fetched = client.get(f"/v1/sources/{body['source_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["database"] == "erp_finance"

    listed = client.get("/v1/sources")
    assert listed.status_code == 200
    assert any(item["source_id"] == "erp_finance" for item in listed.json())


def test_a_password_is_never_returned_or_persisted(client, orchestration):
    """The single most important guarantee in the source API."""
    response = client.post(
        "/v1/sources",
        json={
            "name": "Secret Source",
            "source_type": "postgresql",
            "host": "localhost",
            "username": "erp",
            "password": SECRET_DB_PASSWORD,
        },
    )

    assert response.status_code == 201
    assert SECRET_DB_PASSWORD not in response.text

    source_id = response.json()["source_id"]

    # Not on the stored object ...
    stored = orchestration.sources.get(source_id)
    assert SECRET_DB_PASSWORD not in repr(stored)
    assert SECRET_DB_PASSWORD not in json.dumps(stored.to_dict())

    # ... and not on any later read.
    assert SECRET_DB_PASSWORD not in client.get(f"/v1/sources/{source_id}").text
    assert SECRET_DB_PASSWORD not in client.get("/v1/sources").text

    # It went to the secret provider instead, under a reference.
    assert stored.credential_ref
    assert orchestration.services.secrets.has(stored.credential_ref)


def test_the_secret_provider_redacts_itself(orchestration):
    """A provider repr in a log must not spill every credential it holds."""
    provider = orchestration.services.secrets
    provider.put("some_ref", SECRET_DB_PASSWORD)

    assert SECRET_DB_PASSWORD not in repr(provider)
    assert "redacted" in repr(provider).lower()


def test_credential_shaped_metadata_is_dropped(client):
    """Metadata is an open dictionary, so it is filtered on the way in."""
    response = client.post(
        "/v1/sources",
        json={
            "name": "Metadata Source",
            "source_type": "postgresql",
            "metadata": {
                "region": "eu-west",
                "password": SECRET_DB_PASSWORD,
                "api_key": SECRET_API_KEY,
                "bearer_token": SECRET_BEARER,
            },
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["metadata"] == {"region": "eu-west"}
    assert SECRET_DB_PASSWORD not in response.text
    assert SECRET_API_KEY not in response.text
    assert SECRET_BEARER not in response.text


def test_connection_test_reports_failure_without_leaking(client):
    """A failed connection is a result, not a 500 - and it says nothing extra."""
    client.post(
        "/v1/sources",
        json={
            "name": "Unreachable DB",
            "source_type": "postgresql",
            "host": "127.0.0.1",
            "port": 1,
            "database": "nope",
            "username": "erp",
        },
    )

    response = client.post("/v1/sources/unreachable_db/test")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "postgresql://" not in response.text
    assert "password" not in response.text.lower()


# ----------------------------------------------------------------------
# OpenAPI document
# ----------------------------------------------------------------------


def test_openapi_document_contains_every_mandatory_route(client):
    spec = client.get("/openapi.json").json()

    present = {
        (method, path)
        for path, operations in spec["paths"].items()
        for method in operations
    }

    assert MANDATORY_ROUTES <= present


def test_operation_ids_are_unique_and_deterministic(client):
    spec = client.get("/openapi.json").json()
    ids = [
        operation["operationId"]
        for operations in spec["paths"].values()
        for operation in operations.values()
    ]

    assert len(ids) == len(set(ids))
    assert all(ids)


def test_openapi_embeds_no_secret_example(client):
    """A credential field may exist; a credential VALUE may not."""
    rendered = client.get("/openapi.json").text

    for planted in (SECRET_DB_PASSWORD, SECRET_API_KEY, SECRET_BEARER):
        assert planted not in rendered


def test_the_password_field_is_declared_but_never_exampled(client):
    spec = client.get("/openapi.json").json()
    source_create = spec["components"]["schemas"]["SourceCreate"]

    assert "password" in source_create["properties"]

    example = source_create.get("example", {})
    assert "password" not in example

    # The description must say what happens to it.
    described = source_create["properties"]["password"]["description"].lower()
    assert "never" in described


def test_docs_are_served(client):
    assert client.get("/docs").status_code == 200
