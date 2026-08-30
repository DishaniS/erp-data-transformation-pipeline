"""Two search robustness fixes, verified end to end.

FIX 1 - mode selection reacts to what was actually supplied, not to ``q``
alone. Previously ``GET /v1/search?source_system_id=...&record_key=EMP-0001``
(no ``q``) silently returned the metadata catalog, ignoring the filters
entirely - the exact defect this file's ``without_q`` tests pin.

FIX 2 - the metadata catalog is built from the union of the in-process
schema cache and the PERSISTED catalog (``SchemaCatalogService``), so it
survives a restart. ``test_metadata_rebuilds_after_a_simulated_restart``
proves this against a REAL Postgres-backed catalog with an EMPTY in-memory
cache - exactly what a freshly started worker process looks like.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.sync import InMemoryCanonicalStore
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

from tests.erp_pipeline.api.test_get_dynamic_employee_search import (
    RecordingTier,
    employees_entity,
    schema,
)
from tests.erp_pipeline.api.test_search_resolution_and_filters import (
    DIMENSION,
    DeterministicTestModel,
    PatchedStorage,
)
from erp_pipeline.storage.state import InMemoryTierStateStore


def _row(key: str, name: str, department: str) -> SourceRecord:
    return SourceRecord.from_mapping(
        {
            "employee_id": key,
            "full_name": name,
            "department": department,
            "status": "Active",
            "Shift Code": "DAY-A",
            "private_note": "not a Qdrant filter",
        }
    )


@pytest.fixture
def api(tmp_path):
    """EMP-0001 and EMP-0002 under legacy_erp_pg - the real key shape."""
    entity = employees_entity()
    pg_schema = schema("legacy_erp_pg", entity)
    transformer = SourceNativeTransformer()

    records = [
        transformer.transform_record(
            _row("EMP-0001", "Kasun Fernando", "Engineering"),
            entity,
            "legacy_erp_pg",
            SourceType.POSTGRESQL,
        ),
        transformer.transform_record(
            _row("EMP-0002", "Nimal Perera", "Finance"),
            entity,
            "legacy_erp_pg",
            SourceType.POSTGRESQL,
        ),
    ]

    canonical_store = InMemoryCanonicalStore()
    tier = RecordingTier()
    storage = PatchedStorage(hot=tier, state_store=InMemoryTierStateStore())
    embedding = EmbeddingService(DeterministicTestModel(dimension=DIMENSION))

    for record in records:
        canonical_store.upsert(record)
        storage.store(embedding.embed_one(canonical_record_to_representation(record)))

    services = PipelineServices(
        records=canonical_store,
        storage=storage,
        embedding=embedding,
        schema_cache={pg_schema.schema_id: pg_schema},
    )
    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=OrchestrationService(
            services=services,
            job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        ),
    )

    with TestClient(app) as client:
        yield client, tier, services


# ======================================================================
# FIX 1 - mode selection
# ======================================================================


def test_metadata_mode_with_no_params(api):
    client, _, _ = api

    response = client.get("/v1/search")

    assert response.status_code == 200
    body = response.json()
    assert "available_search" in body
    assert "hits" not in body
    entities = {
        (item["source_system_id"], item["source_entity"])
        for item in body["available_search"]
    }
    assert ("legacy_erp_pg", "hr.employees") in entities


def test_exact_emp0001_retrieval_without_q(api):
    """The literal case the fix exists for: no ``q``, still an exact hit."""
    client, tier, _ = api

    response = client.get(
        "/v1/search",
        params={
            "source_system_id": "legacy_erp_pg",
            "source_entity": "hr.employees",
            "record_key": "EMP-0001",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "available_search" not in body
    assert len(body["hits"]) == 1
    hit = body["hits"][0]
    assert hit["record_key"] == "EMP-0001"
    assert hit["source_system_id"] == "legacy_erp_pg"
    assert hit["source_entity"] == "hr.employees"
    assert hit["representation_id"]
    assert hit["metadata"]["business_key_value"] == "EMP-0001"

    # No semantic ranking ran: exactly one filtered call, no query vector.
    assert len(tier.payloads) == 2  # both EMP-0001 and EMP-0002 are stored
    assert tier.received_filter is not None
    conditions = {c.key: c.match.value for c in tier.received_filter.must}
    assert conditions["record_key"] == "EMP-0001"


def test_semantic_emp0001_retrieval_with_q(api):
    """The same identity, but with a query - semantic ranking now applies."""
    client, _, _ = api

    response = client.get(
        "/v1/search",
        params={
            "q": "employee engineering record",
            "source_system_id": "legacy_erp_pg",
            "source_entity": "hr.employees",
            "record_key": "EMP-0001",
        },
    )

    assert response.status_code == 200
    hits = response.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["record_key"] == "EMP-0001"
    # A real dot-product score was computed here, not the fetch-only 1.0
    # sentinel a filter-only match reports (see test_exact_..._without_q).
    assert hits[0]["score"] != 1.0


def test_no_emp0002_leakage(api):
    """Scoped to EMP-0001, EMP-0002 must never appear - with or without q."""
    client, _, _ = api

    without_q = client.get(
        "/v1/search",
        params={
            "source_system_id": "legacy_erp_pg",
            "record_key": "EMP-0001",
        },
    ).json()["hits"]
    with_q = client.get(
        "/v1/search",
        params={
            "q": "Nimal Perera Finance",
            "source_system_id": "legacy_erp_pg",
            "record_key": "EMP-0001",
        },
    ).json()["hits"]

    assert without_q and all(h["record_key"] == "EMP-0001" for h in without_q)
    assert with_q and all(h["record_key"] == "EMP-0001" for h in with_q)


def test_filter_only_fetch_rejects_an_unfiltered_call(api):
    """Belt and braces: the storage-layer guard against an unscoped fetch.

    Unreachable through the router today - anything that reaches search mode
    without ``q`` necessarily carries a filter - but ``HybridVectorStore.fetch``
    is a public method, so any future caller inherits the guard rather than
    accidentally dumping the collection.
    """
    from erp_pipeline.storage.errors import StorageConfigurationError
    from erp_pipeline.storage.filters import NO_FILTERS

    _, _, services = api

    with pytest.raises(StorageConfigurationError):
        services.storage.fetch(NO_FILTERS)


# ======================================================================
# FIX 2 - metadata survives a restart
# ======================================================================


@pytest.fixture
def real_catalog():
    """A catalog service backed by the local pipeline Postgres database.

    Skips (rather than fails) when that database is unreachable - the same
    convention every other live-Postgres fixture in this suite uses.
    Cleans up exactly the rows this fixture creates, leaving the catalog as
    it was found.
    """
    from dotenv import load_dotenv
    from sqlalchemy import create_engine, text

    load_dotenv()

    from erp_pipeline.catalog.repository import CatalogRepository
    from erp_pipeline.catalog.schema import bootstrap_catalog
    from erp_pipeline.catalog.service import SchemaCatalogService

    host = os.getenv("AI_DB_HOST", "localhost")
    port = int(os.getenv("AI_DB_PORT", "5432"))
    user = os.getenv("AI_DB_USER")
    password = os.getenv("AI_DB_PASSWORD")
    database = os.getenv("AI_DB_NAME", "erp_ai_native_db")

    if not user or not password:
        pytest.skip("local pipeline PostgreSQL credentials are not configured")

    from sqlalchemy.engine import URL

    engine = create_engine(
        URL.create(
            "postgresql+psycopg2",
            username=user,
            password=password,
            host=host,
            port=port,
            database=database,
        )
    )

    try:
        with engine.connect():
            pass
    except Exception as exc:  # noqa: BLE001 - availability probe
        pytest.skip(f"local pipeline PostgreSQL unreachable: {exc}")

    bootstrap_catalog(engine)
    service = SchemaCatalogService(CatalogRepository(engine))
    source_system_id = "search_metadata_restart_probe"

    yield service, source_system_id

    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM erp_catalog.source_fields WHERE schema_id IN ("
                "SELECT schema_id FROM erp_catalog.schema_snapshots "
                "WHERE source_system_id = :sid)"
            ),
            {"sid": source_system_id},
        )
        for table in ("source_entities", "source_relationships"):
            connection.execute(
                text(
                    f"DELETE FROM erp_catalog.{table} WHERE schema_id IN ("
                    "SELECT schema_id FROM erp_catalog.schema_snapshots "
                    "WHERE source_system_id = :sid)"
                ),
                {"sid": source_system_id},
            )
        connection.execute(
            text(
                "DELETE FROM erp_catalog.schema_snapshots WHERE source_system_id = :sid"
            ),
            {"sid": source_system_id},
        )
        connection.execute(
            text("DELETE FROM erp_catalog.source_systems WHERE source_system_id = :sid"),
            {"sid": source_system_id},
        )
    engine.dispose()


def test_metadata_rebuilds_after_a_simulated_restart(tmp_path, real_catalog):
    """The in-memory cache is EMPTY - exactly what a fresh worker looks like.

    Everything the metadata response needs comes from the persisted catalog
    instead, proving GET /v1/search does not depend on the process that
    discovered the schema still being the one answering requests.
    """
    from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SchemaOrigin
    from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema, SourceSystem

    catalog_service, source_system_id = real_catalog

    catalog_service.register_source_system(
        SourceSystem(
            source_system_id=source_system_id,
            name=source_system_id,
            source_type=SourceType.POSTGRESQL,
        )
    )
    entity = SourceEntity(
        entity_id=f"{source_system_id}.hr.employees",
        source_name="employees",
        normalized_name="hr.employees",
        namespace="hr",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("name",),
        fields=(
            SourceField(
                source_name="name",
                normalized_name="name",
                source_data_type="TEXT",
                normalized_data_type=FieldDataType.STRING,
                is_primary_key=True,
                nullable=False,
            ),
            SourceField(
                source_name="department_name",
                normalized_name="department_name",
                source_data_type="TEXT",
                normalized_data_type=FieldDataType.STRING,
            ),
        ),
    )
    persisted_schema = SourceSchema(
        schema_id=f"{source_system_id}.hr.v1",
        source_system_id=source_system_id,
        schema_name="hr",
        origin=SchemaOrigin.DISCOVERED,
        entities=(entity,),
    )
    catalog_service.publish_schema(persisted_schema)

    # A FRESH PipelineServices, schema_cache EMPTY - nothing discovered by
    # this process. The only thing it is given is the catalog connection,
    # which is exactly what survives an App Service restart.
    services = PipelineServices(catalog=catalog_service)
    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=OrchestrationService(
            services=services,
            job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        ),
    )

    assert services.schema_cache == {}

    with TestClient(app) as client:
        body = client.get("/v1/search").json()

    entities = {
        (item["source_system_id"], item["source_entity"]): item
        for item in body["available_search"]
    }
    found = entities.get((source_system_id, "hr.employees"))

    assert found is not None, (
        f"expected {source_system_id}/hr.employees in the rebuilt catalog; "
        f"got {sorted(entities)}"
    )
    field_names = {f["name"] for f in found["fields"]}
    assert "department_name" in field_names
    business_key_field = next(f for f in found["fields"] if f["name"] == "name")
    assert business_key_field["business_key"] is True
