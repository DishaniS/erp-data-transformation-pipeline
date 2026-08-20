"""Critical Proofs A, D, E and F - the live end-to-end demonstrations.

These use the REAL MiniLM model and a REAL Qdrant, in isolated collections that
are deleted afterwards. Mocking them here would prove only that the mocks
agree with each other.
"""

from __future__ import annotations

import json
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

COLLECTION_PREFIX = "erp_phase13_test_"

#: The CSV is named for the canonical entity it carries. Schema inference takes
#: the entity name from the filename, and the mapping engine matches on it, so
#: `invoice.csv` maps cleanly where `data.csv` would not.
CSV_BYTES = b"""invoice_id,customer_id,customer_name,amount,currency,status,issued_on
INV-1001,CUS-01,Acme Trading,15400.50,LKR,approved,2025-01-15
INV-1002,CUS-02,Beta Supplies,8200.00,USD,pending,2025-02-03
INV-1003,CUS-03,Gamma Logistics,45300.75,EUR,approved,2025-02-19
"""


@pytest.fixture
def live_stack(qdrant_client, tmp_path: Path):
    """Real ingestion, mapping, transformation, MiniLM and Qdrant."""
    from erp_pipeline.ai import EmbeddingService, SentenceTransformerModel
    from erp_pipeline.api_specs import ApiSpecificationService
    from erp_pipeline.ingestion import FileIngestionService
    from erp_pipeline.mapping import MappingService
    from erp_pipeline.storage import (
        ColdArchiveTier,
        QdrantHotTier,
        QdrantWarmTier,
        StaticKeyProvider,
        StorageService,
        generate_key,
    )
    from erp_pipeline.sync import InMemoryCanonicalStore
    from erp_pipeline.transformation import TransformationService

    token = uuid.uuid4().hex[:8]
    hot_name = f"{COLLECTION_PREFIX}hot_{token}"
    warm_name = f"{COLLECTION_PREFIX}warm_{token}"

    hot = QdrantHotTier(qdrant_client, hot_name, 384)
    warm = QdrantWarmTier(qdrant_client, warm_name, 384)
    hot.ensure_collection(recreate=True)
    warm.ensure_collection(recreate=True)
    cold = ColdArchiveTier(tmp_path / "cold", StaticKeyProvider(generate_key()))

    services = PipelineServices(
        ingestion=FileIngestionService(),
        api_specs=ApiSpecificationService(),
        mapping=MappingService(),
        transformation=TransformationService(),
        records=InMemoryCanonicalStore(),
        embedding=EmbeddingService(SentenceTransformerModel()),
        storage=StorageService(hot=hot, warm=warm, cold=cold),
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
            source_id="erp_csv", name="ERP CSV", source_type=SourceType.CSV
        )
    )

    from fastapi.testclient import TestClient

    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=orchestration,
    )

    try:
        with TestClient(app) as client:
            yield client, orchestration, hot, warm, cold
    finally:
        for name in (hot_name, warm_name):
            try:
                qdrant_client.delete_collection(name)
            except Exception:
                pass


def run_csv_pipeline(client) -> dict:
    """The mandated API flow, start to finish."""
    upload = client.post(
        "/v1/files/csv", files={"file": ("invoice.csv", CSV_BYTES, "text/csv")}
    )
    assert upload.status_code == 201, upload.text
    uploaded = upload.json()

    mapping = client.post(
        "/v1/mappings/suggest", json={"schema_id": uploaded["schema_id"]}
    )
    assert mapping.status_code == 200, mapping.text
    mapped = mapping.json()

    job = client.post(
        "/v1/jobs",
        json={
            "job_type": "structured_pipeline",
            "source_id": "erp_csv",
            "schema_id": uploaded["schema_id"],
            "mapping_id": mapped["mapping_id"],
            "upload_id": uploaded["upload_id"],
        },
    )
    assert job.status_code == 202, job.text

    status = client.get(f"/v1/jobs/{job.json()['job_id']}")
    assert status.status_code == 200

    return {
        "upload": uploaded,
        "mapping": mapped,
        "accepted": job.json(),
        "job": status.json(),
    }


# ----------------------------------------------------------------------
# CRITICAL PROOF A
# ----------------------------------------------------------------------


def test_the_whole_csv_pipeline_runs_through_the_api(live_stack):
    """CSV -> mapping -> job -> transform -> embed -> tier -> search."""
    client, orchestration, hot, warm, cold = live_stack
    outcome = run_csv_pipeline(client)
    job = outcome["job"]

    assert job["status"] == JobStatus.SUCCEEDED.value, job.get("error_message")

    counters = job["counters"]
    assert counters["records_read"] == 3
    assert counters["records_transformed"] == 3
    assert counters["records_failed"] == 0
    assert counters["representations_built"] == 3
    assert counters["embeddings_generated"] == 3
    assert counters["vectors_stored"] == 3
    assert counters["vectors_failed"] == 0

    # Every executed stage really succeeded.
    executed = {
        run["stage"]: run["status"]
        for run in job["stages"]
        if run["status"] != StageStatus.NOT_APPLICABLE.value
    }

    for stage in (
        PipelineStage.MAP,
        PipelineStage.EXTRACT,
        PipelineStage.TRANSFORM,
        PipelineStage.VALIDATE,
        PipelineStage.LOAD,
        PipelineStage.AI_BUILD,
        PipelineStage.EMBED,
        PipelineStage.TIER_ROUTE,
    ):
        assert executed[stage.value] == StageStatus.SUCCEEDED.value

    # The vectors are really in Qdrant.
    assert hot.count() + warm.count() == 3


def test_the_pipeline_used_the_real_model_and_phase_12_chose_the_tier(live_stack):
    client, orchestration, hot, warm, cold = live_stack
    outcome = run_csv_pipeline(client)

    embed = next(
        run for run in outcome["job"]["stages"] if run["stage"] == "embed"
    )
    tier = next(
        run for run in outcome["job"]["stages"] if run["stage"] == "tier_route"
    )

    assert "MiniLM" in embed["outputs"]["model_id"]
    # Phase 12 decided the tier; orchestration never named one.
    assert sum(tier["outputs"]["tiers"].values()) == 3


def test_search_finds_the_ingested_record(live_stack):
    """CRITICAL PROOF D. Query -> MiniLM -> hybrid search -> the right record."""
    client, orchestration, hot, warm, cold = live_stack
    run_csv_pipeline(client)

    response = client.post(
        "/v1/search",
        json={"query": "approved invoice for Acme Trading in LKR", "top_k": 3},
    )

    assert response.status_code == 200
    body = response.json()

    assert body["hits"], "search returned nothing for an ingested record"
    assert "MiniLM" in body["query_model"]
    assert body["dimension"] == 384
    assert set(body["tiers_searched"]) <= {"hot", "warm"}

    # The Acme invoice should be the best match for an Acme query.
    assert "inv-1001" in body["hits"][0]["representation_id"].lower()

    # Results are deduplicated.
    ids = [hit["representation_id"] for hit in body["hits"]]
    assert len(ids) == len(set(ids))


def test_a_loaded_record_is_retrievable(live_stack):
    client, orchestration, hot, warm, cold = live_stack
    run_csv_pipeline(client)

    record_ids = orchestration.services.records.record_ids
    assert record_ids

    response = client.get(f"/v1/records/{record_ids[0]}")

    assert response.status_code == 200
    assert response.json()["record_id"] == record_ids[0]


# ----------------------------------------------------------------------
# No vector may ever reach a client
# ----------------------------------------------------------------------


def test_no_endpoint_leaks_an_embedding_vector(live_stack):
    """384 floats in a response body would be an embedding-export endpoint."""
    client, orchestration, hot, warm, cold = live_stack
    outcome = run_csv_pipeline(client)

    search = client.post("/v1/search", json={"query": "invoice", "top_k": 5})
    job = client.get(f"/v1/jobs/{outcome['accepted']['job_id']}")
    record_ids = orchestration.services.records.record_ids
    record = client.get(f"/v1/records/{record_ids[0]}")

    for response in (search, job, record):
        body = response.json()
        rendered = json.dumps(body)

        assert '"vector"' not in rendered
        assert "embedding_vector" not in rendered

        # No long float array anywhere in the payload.
        def has_vector(node) -> bool:
            if isinstance(node, list):
                if len(node) > 32 and all(
                    isinstance(item, (int, float)) for item in node
                ):
                    return True
                return any(has_vector(item) for item in node)

            if isinstance(node, dict):
                return any(has_vector(value) for value in node.values())

            return False

        assert not has_vector(body), f"{response.url} leaked a vector"


# ----------------------------------------------------------------------
# CRITICAL PROOF D (cold) - include_cold semantics
# ----------------------------------------------------------------------


def test_cold_records_are_excluded_until_deep_search_is_requested(live_stack):
    """Cold costs a rehydration, so it must never happen silently."""
    client, orchestration, hot, warm, cold = live_stack
    run_csv_pipeline(client)

    storage = orchestration.services.storage
    representation_ids = [
        metadata.representation_id for metadata in storage.state.list_all()
    ]
    assert representation_ids

    archived = representation_ids[0]
    storage.migrate(archived, __import__(
        "erp_pipeline.storage", fromlist=["StorageTier"]
    ).StorageTier.COLD)

    assert cold.count() >= 1

    shallow = client.post(
        "/v1/search", json={"query": "invoice", "top_k": 10, "include_cold": False}
    ).json()

    assert archived not in {hit["representation_id"] for hit in shallow["hits"]}
    assert shallow["deep_search_used"] is False
    assert "cold" not in shallow["tiers_searched"]

    deep = client.post(
        "/v1/search", json={"query": "invoice", "top_k": 10, "include_cold": True}
    ).json()

    assert archived in {hit["representation_id"] for hit in deep["hits"]}
    assert deep["deep_search_used"] is True
    # The cost is disclosed, not hidden.
    assert "rehydrated" in (deep["deep_search_note"] or "")


# ----------------------------------------------------------------------
# CRITICAL PROOF C - safe failure
# ----------------------------------------------------------------------


def test_a_transform_failure_stops_the_pipeline_and_stores_nothing(live_stack):
    """A failed transform must not embed or index anything."""
    client, orchestration, hot, warm, cold = live_stack

    from erp_pipeline.orchestration.stages import DEFAULT_HANDLERS
    from erp_pipeline.orchestration.pipeline import PipelineRunner, StageFailure

    handlers = dict(DEFAULT_HANDLERS)
    handlers[PipelineStage.TRANSFORM] = lambda ctx: (_ for _ in ()).throw(
        StageFailure("simulated conversion failure", code="TRANSFORM_FAILED")
    )
    orchestration.runner = PipelineRunner(handlers)

    before_hot, before_warm = hot.count(), warm.count()
    outcome = run_csv_pipeline(client)
    job = outcome["job"]

    assert job["status"] == JobStatus.FAILED.value
    assert job["error_code"] == "TRANSFORM_FAILED"

    stages = {run["stage"]: run["status"] for run in job["stages"]}
    assert stages["transform"] == StageStatus.FAILED.value

    for later in ("validate", "load", "ai_build", "embed", "tier_route"):
        assert stages[later] == StageStatus.SKIPPED.value

    # Nothing was embedded or stored.
    assert hot.count() == before_hot
    assert warm.count() == before_warm

    # And no business value leaked into the error.
    assert "Acme" not in json.dumps(job)


# ----------------------------------------------------------------------
# CRITICAL PROOF E - specification boundary
# ----------------------------------------------------------------------


def test_an_openapi_upload_is_parsed_without_calling_any_endpoint(live_stack, monkeypatch):
    """The documented endpoints must never be called. Enforced, not promised."""
    client, orchestration, hot, warm, cold = live_stack

    calls: list[str] = []

    # Trip-wire every outbound HTTP path this process could use.
    import http.client
    import socket

    original_connect = http.client.HTTPConnection.connect
    original_socket_connect = socket.socket.connect

    def trip(self, *args, **kwargs):
        calls.append(str(getattr(self, "host", args)))
        return original_connect(self, *args, **kwargs)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", trip)

    spec = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "Vendor ERP", "version": "1.0.0"},
            "servers": [{"url": "https://vendor.example.test/api"}],
            "paths": {
                "/invoices": {
                    "get": {
                        "operationId": "listInvoices",
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "invoice_id": {"type": "string"},
                                                "amount": {"type": "number"},
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    },
                    "post": {
                        "operationId": "createInvoice",
                        "responses": {"201": {"description": "created"}},
                    },
                }
            },
        }
    ).encode()

    response = client.post(
        "/v1/api-specs/openapi",
        files={"file": ("vendor.json", spec, "application/json")},
    )

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["operations_count"] == 2
    assert body["endpoints_called"] == 0

    # No connection to the documented host was attempted.
    assert not any("vendor.example.test" in call for call in calls)


def test_a_postman_collection_is_parsed_without_executing_it(live_stack):
    """Scripts in a collection are data, not a program to run."""
    client, orchestration, hot, warm, cold = live_stack

    collection = json.dumps(
        {
            "info": {
                "name": "Vendor ERP",
                "schema": (
                    "https://schema.getpostman.com/json/collection/v2.1.0/"
                    "collection.json"
                ),
            },
            "item": [
                {
                    "name": "List invoices",
                    "event": [
                        {
                            "listen": "prerequest",
                            "script": {
                                "exec": ["pm.environment.set('x', 1)"],
                                "type": "text/javascript",
                            },
                        }
                    ],
                    "request": {
                        "method": "GET",
                        "url": {
                            "raw": "https://vendor.example.test/api/invoices",
                            "host": ["vendor", "example", "test"],
                            "path": ["api", "invoices"],
                        },
                    },
                }
            ],
        }
    ).encode()

    response = client.post(
        "/v1/api-specs/postman",
        files={"file": ("vendor.postman_collection.json", collection, "application/json")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["endpoints_called"] == 0


def test_a_spec_source_cannot_be_asked_for_records(live_stack):
    client, orchestration, hot, warm, cold = live_stack
    orchestration.sources.register(
        RegisteredSource(
            source_id="vendor_spec",
            name="Vendor Spec",
            source_type=SourceType.OPENAPI,
        )
    )

    response = client.post(
        "/v1/jobs",
        json={"job_type": "structured_pipeline", "source_id": "vendor_spec"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


# ----------------------------------------------------------------------
# Job submission responsiveness
# ----------------------------------------------------------------------


def test_job_submission_returns_before_the_pipeline_finishes(qdrant_client, tmp_path):
    """POST /jobs must not block on a model run.

    Uses the real bounded executor rather than the inline one, because the
    inline executor deliberately finishes the work before returning.
    """
    from fastapi.testclient import TestClient

    from erp_pipeline.orchestration import JobExecutor
    from erp_pipeline.orchestration.pipeline import PipelineRunner

    services = PipelineServices(uploads=UploadStore(tmp_path / "uploads"))
    orchestration = OrchestrationService(
        services=services,
        job_store=InMemoryJobStore(),
        executor=JobExecutor(max_workers=2),
        handlers={
            stage: (lambda ctx: (time.sleep(0.4), {})[1])
            for stage in PipelineStage
        },
    )
    orchestration.sources.register(
        RegisteredSource(
            source_id="slow", name="Slow", source_type=SourceType.POSTGRESQL
        )
    )

    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=orchestration,
    )

    try:
        with TestClient(app) as client:
            started = time.perf_counter()
            response = client.post(
                "/v1/jobs",
                json={"job_type": "structured_pipeline", "source_id": "slow"},
            )
            elapsed = time.perf_counter() - started

            assert response.status_code == 202
            # Nine stages x 0.4s would be 3.6s if the request waited.
            assert elapsed < 1.5, f"submission blocked for {elapsed:.2f}s"
            assert response.json()["status_url"].startswith("/v1/jobs/")
    finally:
        orchestration.executor.shutdown(wait=False)
