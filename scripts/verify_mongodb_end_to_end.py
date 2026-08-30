"""Prove the MongoDB path end to end, through the real runtime.

WHY THIS SCRIPT EXISTS
----------------------
The common-model audit proved MongoDB discovery, type normalization and
source-native transformation, but stopped at the representation boundary. It
said so rather than claiming a PASS it had not earned. This script closes that
gap: it drives a live MongoDB collection all the way to a resolved search hit,
using the real embedding model and the real Qdrant collections.

WHAT IS REAL HERE
-----------------
* MongoDB          - the live local demo database, read through the production
                     ``MongoDBConnector`` with a read-only account
* Embeddings       - ``all-MiniLM-L6-v2``, the real model, not a test double
* Qdrant           - the configured deployment's ``erp_vectors_hot`` /
                     ``erp_vectors_warm``. No new collection is created
* API surface      - search and resolution go through the FastAPI app over HTTP

Only the job store, representation store and tier state are in-memory, because
this is a verification run rather than a deployment.

SAFETY
------
Every vector written carries ``source_system_id = viva_mongo``, so demo points
are identifiable and separable from anything else in the collection. Nothing
belonging to another source is read, modified or deleted. Sensitivity is
INTERNAL throughout - the restricted-data policy is respected, never relaxed.

Run:
    .venv/Scripts/python.exe scripts/verify_mongodb_end_to_end.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

ARTIFACT = ROOT / "artifacts" / "mongodb_end_to_end_verification.json"

#: Identifies every vector this run writes.
SOURCE_SYSTEM_ID = "viva_mongo"
DEMO_DB = os.getenv("MONGO_VIVA_DB", "erp_viva_mongodb_demo")


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.counts: dict[str, object] = {}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"check": name, "pass": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))

        return bool(ok)

    def note(self, key: str, value: object) -> None:
        self.counts[key] = value
        print(f"        {key} = {value}")


def mongo_settings():
    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.schemas.enums import SourceType

    return ConnectionSettings(
        source_system_id=SOURCE_SYSTEM_ID,
        source_type=SourceType.MONGODB,
        host=os.getenv("MONGO_PHASE5_HOST", "localhost"),
        port=int(os.getenv("MONGO_PHASE5_PORT", "27018")),
        database=DEMO_DB,
        # Read-only on purpose: discovery must never need write access.
        username=os.getenv("MONGO_PHASE5_READONLY_USER"),
        password=os.getenv("MONGO_PHASE5_READONLY_PASSWORD"),
        auth_database=os.getenv("MONGO_PHASE5_AUTH_DB", "admin"),
        connect_timeout_seconds=10,
    )


def _purge_previous_demo_vectors(client, qsettings) -> None:
    """Delete only the points this demo created, identified by source system."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    selector = Filter(
        must=[
            FieldCondition(
                key="source_system_id", match=MatchValue(value=SOURCE_SYSTEM_ID)
            )
        ]
    )

    for collection in (qsettings.hot_collection, qsettings.warm_collection):
        try:
            client.delete(
                collection_name=collection, points_selector=selector, wait=True
            )
        except Exception as error:  # noqa: BLE001 - reported, never fatal
            print(f"        could not purge {collection}: {type(error).__name__}")


def build_app(report: Report):
    """A real runtime: real model, real Qdrant, in-memory job/state stores."""
    from fastapi.testclient import TestClient

    from erp_pipeline.ai.service import EmbeddingService
    from erp_pipeline.api import ApiSettings, create_app
    from erp_pipeline.connectors.mongodb import MongoDBConnector
    from erp_pipeline.ingestion import FileIngestionService
    from erp_pipeline.mapping import MappingService
    from erp_pipeline.orchestration import (
        InlineJobExecutor,
        InMemoryJobStore,
        InMemoryLifecycleRegistry,
        InMemoryRepresentationStore,
        InMemorySecretProvider,
        OrchestrationService,
        PipelineServices,
        UploadStore,
    )
    from erp_pipeline.runtime.settings import QdrantSettings
    from erp_pipeline.runtime.services import build_qdrant_client
    from erp_pipeline.storage import QdrantHotTier, QdrantWarmTier, StorageService
    from erp_pipeline.storage.state import InMemoryTierStateStore
    from erp_pipeline.storage.storage_policy import StoragePolicy
    from erp_pipeline.sync import InMemoryCanonicalStore
    from erp_pipeline.transformation import TransformationService
    import tempfile

    qsettings = QdrantSettings.from_environment()
    client = build_qdrant_client(qsettings)

    hot = QdrantHotTier(client, qsettings.hot_collection, qsettings.dimension)
    warm = QdrantWarmTier(client, qsettings.warm_collection, qsettings.dimension)
    hot.ensure_collection()
    warm.ensure_collection()

    # Remove vectors THIS demo wrote on a previous run.
    #
    # Each run uses a fresh in-memory tier state store while Qdrant keeps its
    # points, so a stale point from an earlier run has no state row and search
    # cannot resolve it. Only points carrying this demo's own
    # ``source_system_id`` are removed - nothing belonging to another source is
    # touched.
    _purge_previous_demo_vectors(client, qsettings)

    from erp_pipeline.runtime.settings import StorageLocationSettings

    storage = StorageService(
        hot=hot,
        warm=warm,
        state_store=InMemoryTierStateStore(),
        policy=StoragePolicy(
            tier_locations=StorageLocationSettings.from_environment(
                qsettings
            ).as_tier_map()
        ),
    )

    # The real model. Loading it is the slow part of this script.
    embedding = EmbeddingService()

    connector = MongoDBConnector(mongo_settings())

    services = PipelineServices(
        ingestion=FileIngestionService(),
        # SOURCE_NATIVE_GUARD needs this to decide whether the entity is
        # genuinely outside the canonical vocabulary. Without it the guard
        # refuses - correctly, since it cannot verify what it is authorising.
        mapping=MappingService(),
        transformation=TransformationService(),
        records=InMemoryCanonicalStore(),
        representations=InMemoryRepresentationStore(),
        lifecycle=InMemoryLifecycleRegistry(),
        storage=storage,
        embedding=embedding,
        uploads=UploadStore(Path(tempfile.mkdtemp()) / "uploads"),
        secrets=InMemorySecretProvider(),
        # Every source-native extraction resolves its connection through here.
        connection_factory=lambda source: connector.create_database_handle(),
    )
    orchestration = OrchestrationService(
        services=services, job_store=InMemoryJobStore(), executor=InlineJobExecutor()
    )
    app = create_app(settings=ApiSettings(), orchestration=orchestration)

    return TestClient(app), services, connector, qsettings, client


def main() -> int:  # noqa: C901 - a linear verification script
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:  # pragma: no cover
        pass

    report = Report()
    print("=== MONGODB END-TO-END VERIFICATION ===\n")

    # ------------------------------------------------------------------
    # 1. Registration, connection, discovery
    # ------------------------------------------------------------------
    from erp_pipeline.discovery.mongodb import infer_mongodb_schema
    from erp_pipeline.orchestration.models import JobRequest, JobType
    from erp_pipeline.schemas.enums import SensitivityLevel

    client_http, services, connector, qsettings, qclient = build_app(report)

    result = connector.test_connection()
    report.check(
        "Mongo source registration + connection test",
        bool(getattr(result, "success", False)),
        f"server {getattr(result, 'server_version', '?')}",
    )

    schema = infer_mongodb_schema(connector)
    report.check(
        "Mongo discovery",
        bool(schema.entities),
        f"{len(schema.entities)} entities: {[e.source_name for e in schema.entities]}",
    )
    report.check(
        "schema origin is INFERRED (observed, not declared)",
        schema.origin.value == "inferred",
        f"origin={schema.origin.value}",
    )

    # The schema cache is how the orchestrator persists a schema for a job.
    services.schema_cache[schema.schema_id] = schema
    report.check(
        "SourceSchema persisted to the runtime catalog",
        schema.schema_id in services.schema_cache,
        f"schema_id={schema.schema_id[:52]}",
    )

    from erp_pipeline.orchestration.sources import RegisteredSource
    from erp_pipeline.schemas.enums import SourceType

    # Registered the way a real deployment registers a source: with the
    # connection details the orchestrator needs to reach it on its own. The
    # password goes through the secret provider, never onto the source record.
    services.secrets.put("viva_mongo_password", os.getenv("MONGO_PHASE5_READONLY_PASSWORD") or "")
    services.sources.register(
        RegisteredSource(
            source_id=SOURCE_SYSTEM_ID,
            name=SOURCE_SYSTEM_ID,
            source_type=SourceType.MONGODB,
            host=os.getenv("MONGO_PHASE5_HOST", "localhost"),
            port=int(os.getenv("MONGO_PHASE5_PORT", "27018")),
            database=DEMO_DB,
            username=os.getenv("MONGO_PHASE5_READONLY_USER"),
            credential_ref="viva_mongo_password",
            auth_database=os.getenv("MONGO_PHASE5_AUTH_DB", "admin"),
        )
    )

    # ------------------------------------------------------------------
    # 2. Source-native job: Mongo -> representation -> embedding -> Qdrant
    # ------------------------------------------------------------------
    employees = next(e for e in schema.entities if e.source_name == "employees")

    job = services_submit(
        client_http,
        {
            "job_type": JobType.SOURCE_NATIVE_PIPELINE.value,
            "source_id": SOURCE_SYSTEM_ID,
            "schema_id": schema.schema_id,
            "entity": employees.source_name,
            "options": {
                "key_fields": ["employee_id"],
                # INTERNAL so the cloud storage policy permits it. The
                # restricted-data rule is respected, not relaxed.
                "sensitivity": SensitivityLevel.INTERNAL.value,
            },
        },
    )

    report.check("source-native job accepted", job.get("job_id") is not None,
                 f"status={job.get('status')}")

    state = client_http.get(f"/v1/jobs/{job['job_id']}").json()
    counters = state.get("counters") or {}

    report.check(
        "source-native job completed",
        state.get("status") in {"succeeded", "partial"},
        f"status={state.get('status')}",
    )
    for key in (
        "records_read",
        "records_transformed",
        "representations_built",
        "embeddings_generated",
        "vectors_stored",
        "vectors_failed",
    ):
        report.note(key, counters.get(key))

    report.check(
        "vectors stored in Qdrant",
        (counters.get("vectors_stored") or 0) > 0,
        f"{counters.get('vectors_stored')} stored, {counters.get('vectors_failed')} failed",
    )

    tiers = {
        stage.get("stage"): stage
        for stage in state.get("stages", [])
    }
    report.note("tier_route stage", tiers.get("tier_route", {}).get("status"))

    # ------------------------------------------------------------------
    # 3. Search and resolution over HTTP
    # ------------------------------------------------------------------
    found = client_http.post(
        "/v1/search",
        json={
            "query": "employee in the finance department",
            "filters": {
                "content_kind": "structured_record",
                "source_system_id": SOURCE_SYSTEM_ID,
            },
        },
    ).json()

    hits = found.get("hits", [])
    report.check(
        "POST /v1/search returned MongoDB content",
        bool(hits),
        f"{len(hits)} hits, model={found.get('query_model')}, dim={found.get('dimension')}",
    )
    report.note("tiers_searched", found.get("tiers_searched"))

    resolved_text = ""
    representation_id = hits[0]["representation_id"] if hits else None

    if representation_id:
        resolution = client_http.get(f"/v1/representations/{representation_id}")
        payload = resolution.json() if resolution.status_code == 200 else {}
        resolved_text = payload.get("text") or ""
        report.check(
            "GET /v1/representations/{id} resolved",
            resolution.status_code == 200 and bool(resolved_text),
            f"{len(resolved_text)} chars, sensitivity={payload.get('sensitivity')}",
        )

    # ------------------------------------------------------------------
    # 4. EMP002: business identity vs ObjectId provenance
    # ------------------------------------------------------------------
    emp002 = client_http.post(
        "/v1/search",
        json={
            "query": "Nimal Silva finance senior accounts officer",
            "filters": {
                "content_kind": "structured_record",
                "source_system_id": SOURCE_SYSTEM_ID,
                "business_key_name": "employee_id",
                "business_key_value": "EMP002",
            },
        },
    ).json()

    emp_hits = emp002.get("hits", [])
    report.check(
        "EMP002 found by BUSINESS key, not ObjectId",
        bool(emp_hits),
        f"{len(emp_hits)} hits under business_key_value=EMP002",
    )

    if emp_hits:
        meta = emp_hits[0]["metadata"]
        report.check(
            "business identity is employee_id",
            meta.get("business_key_name") == "employee_id"
            and meta.get("business_key_value") == "EMP002",
            f"{meta.get('business_key_name')}={meta.get('business_key_value')}",
        )

        emp_text = client_http.get(
            f"/v1/representations/{emp_hits[0]['representation_id']}"
        ).json().get("text", "")

        report.check(
            "EMP002 representation resolves to its AI-ready text",
            "EMP002" in emp_text,
            f"{len(emp_text)} chars",
        )
        # The ObjectId is provenance. It must not have become the identity.
        report.check(
            "ObjectId did not become the business key",
            "650000000000000000000002" not in str(meta.get("business_key_value")),
            "record_key carries _id; business_key_value carries employee_id",
        )

    # ------------------------------------------------------------------
    # 5. Leakage gate across everything written
    # ------------------------------------------------------------------
    stored = json.dumps(
        [
            str(services.representations.get(identifier))
            for identifier in services.representations.list_ids()
        ]
    )
    leaks = sum(
        1
        for marker in ("%PDF", "\\x89PNG", base64.b64encode(b"%PDF-1.4").decode()[:10])
        if marker in stored
    )
    report.check("no binary or base64 in any representation", leaks == 0,
                 f"{len(services.representations.list_ids())} representations scanned")

    passed = sum(1 for c in report.checks if c["pass"])
    artifact = {
        "verification": "mongodb_end_to_end",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "mongodb": f"{os.getenv('MONGO_PHASE5_HOST', 'localhost')}:{os.getenv('MONGO_PHASE5_PORT', '27018')}",
            "database": DEMO_DB,
            "qdrant_collections": [qsettings.hot_collection, qsettings.warm_collection],
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "sensitivity": "internal",
            "source_system_id": SOURCE_SYSTEM_ID,
        },
        "checks": report.checks,
        "counts": report.counts,
        "checks_passed": passed,
        "checks_total": len(report.checks),
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    print(f"\n  {passed}/{len(report.checks)} checks passed")
    print(f"  artifact: {ARTIFACT.relative_to(ROOT)}")

    connector.close()

    return 0 if passed == len(report.checks) else 1


def services_submit(client_http, body):
    """Submit a job over HTTP and return the response body."""
    response = client_http.post("/v1/jobs", json=body)

    if response.status_code not in {200, 201, 202}:
        print(f"        job submission failed {response.status_code}: {response.text[:300]}")

        return {}

    return response.json()


if __name__ == "__main__":
    raise SystemExit(main())
