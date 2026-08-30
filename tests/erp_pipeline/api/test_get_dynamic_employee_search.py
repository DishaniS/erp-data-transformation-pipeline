"""GET employee search: canonical identity plus schema-driven Qdrant filters."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from erp_pipeline.ai.embedding import DeterministicTestModel
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemoryRepresentationStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.orchestration.extraction import ExtractionRequest, MongoSnapshotExtractor
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SchemaOrigin,
    SensitivityLevel,
    SourceType,
)
from erp_pipeline.schemas.search_fields import filter_value_token
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync import InMemoryCanonicalStore
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

from tests.erp_pipeline.api.test_search_resolution_and_filters import (
    DIMENSION,
    TEST_FILTER_TOKEN_SECRET,
    InProcessTier,
    PatchedStorage,
)


def field(
    name: str,
    *,
    normalized: str | None = None,
    primary: bool = False,
    filterable: bool = True,
) -> SourceField:
    return SourceField(
        source_name=name,
        normalized_name=normalized or name,
        source_data_type="text",
        normalized_data_type=FieldDataType.STRING,
        is_primary_key=primary,
        nullable=not primary,
        description=f"Current employee {name}.",
        metadata={"filterable": filterable},
    )


def employees_entity() -> SourceEntity:
    return SourceEntity(
        entity_id="hr.employees",
        source_name="employees",
        normalized_name="employees",
        namespace="hr",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("employee_id",),
        fields=(
            field("employee_id", primary=True),
            field("full_name"),
            field("department"),
            field("status"),
            # A newly discovered attribute. Production code never names it.
            field("Shift Code", normalized="shift_code"),
            field("private_note", filterable=False),
        ),
    )


def schema(system: str, entity: SourceEntity) -> SourceSchema:
    return SourceSchema(
        schema_id=f"{system}.hr.v1",
        source_system_id=system,
        schema_name="hr",
        origin=SchemaOrigin.DISCOVERED,
        entities=(entity,),
    )


class RecordingTier(InProcessTier):
    def __init__(self) -> None:
        super().__init__()
        self.received_filter = None

    def search(self, vector, limit=5, query_filter=None):
        self.received_filter = query_filter
        return super().search(vector, limit=limit, query_filter=query_filter)

    def fetch(self, query_filter=None, limit=100):
        self.received_filter = query_filter
        return super().fetch(query_filter=query_filter, limit=limit)


@pytest.fixture
def employee_api(tmp_path):
    entity = employees_entity()
    pg_schema = schema("legacy_erp_pg", entity)
    mongo_schema = schema("legacy_erp_mongo", entity)
    transformer = SourceNativeTransformer()

    pg_rows = (
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "full_name": "Nimal Perera",
                "department": "Engineering",
                "status": "Active",
                "Shift Code": "DAY-A",
                "private_note": "not a Qdrant filter",
            }
        ),
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP003",
                "full_name": "Nimal Perera",
                "department": "Sales",
                "status": "Active",
                "Shift Code": "NIGHT-B",
                "private_note": "not a Qdrant filter",
            }
        ),
    )
    records = list(
        transformer.transform_records(
            pg_rows,
            entity,
            "legacy_erp_pg",
            SourceType.POSTGRESQL,
        ).records
    )
    records.append(
        transformer.transform_record(
            SourceRecord.from_mapping(
                {
                    "employee_id": "EMP004",
                    "full_name": "Restricted Employee",
                    "department": "Engineering",
                    "status": "Active",
                    "Shift Code": "DAY-A",
                }
            ),
            entity,
            "legacy_erp_pg",
            SourceType.POSTGRESQL,
            sensitivity=SensitivityLevel.RESTRICTED,
        )
    )
    records.append(
        transformer.transform_record(
            SourceRecord.from_mapping(
                {
                    "employee_id": "EMP002",
                    "full_name": "Mongo Employee",
                    "department": "Engineering",
                    "status": "Active",
                    "Shift Code": "DAY-A",
                }
            ),
            entity,
            "legacy_erp_mongo",
            SourceType.MONGODB,
        )
    )

    canonical_store = InMemoryCanonicalStore()
    representation_store = InMemoryRepresentationStore()
    tier = RecordingTier()
    storage = PatchedStorage(hot=tier, state_store=InMemoryTierStateStore())
    embedding = EmbeddingService(
        DeterministicTestModel(dimension=DIMENSION),
        filter_token_secret=TEST_FILTER_TOKEN_SECRET,
    )

    for record in records:
        canonical_store.upsert(record)
        representation = canonical_record_to_representation(record)
        representation_store.upsert(representation)
        storage.store(embedding.embed_one(representation))

    services = PipelineServices(
        records=canonical_store,
        representations=representation_store,
        storage=storage,
        embedding=embedding,
        schema_cache={
            pg_schema.schema_id: pg_schema,
            mongo_schema.schema_id: mongo_schema,
        },
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
        yield client, tier, records


def get_search(client: TestClient, **params):
    query = {"q": "employee engineering", "limit": 20, **params}
    return client.get("/v1/search", params=query)


def test_emp002_exact_lookup_uses_complete_source_identity(employee_api):
    client, _, _ = employee_api
    response = get_search(
        client,
        employee_id="EMP002",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
    )

    assert response.status_code == 200
    hits = response.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["record_key"] == "EMP002"
    assert hits[0]["source_system_id"] == "legacy_erp_pg"
    assert hits[0]["source_entity"] == "hr.employees"


def test_semantic_query_is_prefiltered_to_emp002_and_dynamic_attributes(employee_api):
    client, tier, _ = employee_api
    body = get_search(
        client,
        employee_id="EMP002",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        department="Engineering",
        status="Active",
    ).json()

    assert {hit["record_key"] for hit in body["hits"]} == {"EMP002"}
    conditions = {condition.key: condition.match.value for condition in tier.received_filter.must}
    # Closed identity fields stay exactly as supplied.
    assert conditions["record_key"] == "EMP002"
    assert conditions["source_system_id"] == "legacy_erp_pg"
    assert conditions["source_entity"] == "hr.employees"
    # Dynamic (catalog-driven) fields are tokenized before they ever reach
    # Qdrant - the raw value is never the server-side match target.
    assert conditions["department"] != "Engineering"
    assert conditions["status"] != "Active"
    assert conditions["department"] == filter_value_token(
        TEST_FILTER_TOKEN_SECRET,
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="department",
        value="Engineering",
    )
    assert conditions["status"] == filter_value_token(
        TEST_FILTER_TOKEN_SECRET,
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="status",
        value="Active",
    )
    # And the response echoed back to the caller shows the human value, not
    # the token it was matched by.
    assert body["filters_applied"]["department"] == "Engineering"
    assert body["filters_applied"]["status"] == "Active"


def test_same_emp002_in_two_sources_never_collides(employee_api):
    client, _, _ = employee_api
    pg = get_search(
        client,
        employee_id="EMP002",
        source_system_id="legacy_erp_pg",
    ).json()["hits"]
    mongo = get_search(
        client,
        employee_id="EMP002",
        source_system_id="legacy_erp_mongo",
    ).json()["hits"]

    assert {hit["source_system_id"] for hit in pg} == {"legacy_erp_pg"}
    assert {hit["source_system_id"] for hit in mongo} == {"legacy_erp_mongo"}
    assert pg[0]["canonical_record_id"] != mongo[0]["canonical_record_id"]


def test_no_cross_employee_leakage_even_when_names_are_identical(employee_api):
    client, _, _ = employee_api
    hits = get_search(
        client,
        employee_id="EMP002",
        source_system_id="legacy_erp_pg",
        q="Nimal Perera",
    ).json()["hits"]

    assert hits
    assert all(hit["record_key"] == "EMP002" for hit in hits)


def test_qdrant_hit_resolves_the_matching_representation(employee_api):
    client, _, _ = employee_api
    hit = get_search(
        client,
        employee_id="EMP002",
        source_system_id="legacy_erp_pg",
    ).json()["hits"][0]
    resolved = client.get(
        f"/v1/representations/{hit['representation_id']}"
    ).json()

    assert resolved["representation_id"] == hit["representation_id"]
    assert resolved["record_key"] == "EMP002"
    assert resolved["source_system_id"] == "legacy_erp_pg"
    assert resolved["source_entity"] == "hr.employees"


def test_sensitivity_remains_a_server_side_filter(employee_api):
    client, _, _ = employee_api
    hits = get_search(
        client,
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        department="Engineering",
        sensitivity="internal",
    ).json()["hits"]

    assert {hit["record_key"] for hit in hits} == {"EMP002"}
    assert all(hit["metadata"]["sensitivity"] == "internal" for hit in hits)


def test_new_schema_field_is_ingested_indexable_and_discoverable(employee_api):
    """GET /v1/search with no query is the ONLY metadata surface.

    There is no separate ``/search/schema`` endpoint: the same route that
    performs a search reports, when called bare, exactly what the schema
    currently makes discoverable.
    """
    client, tier, _ = employee_api
    # Bare call: source_system_id/source_entity now switch GET /v1/search
    # into search mode, so the unscoped catalog is filtered client-side.
    metadata = client.get("/v1/search").json()
    entities = {
        (item["source_system_id"], item["source_entity"]): item
        for item in metadata["available_search"]
    }
    entity = entities[("legacy_erp_pg", "hr.employees")]
    discovered = {field["name"]: field for field in entity["fields"]}

    assert discovered["shift_code"]["filterable"] is True
    assert discovered["shift_code"]["description"] == "Current employee Shift Code."
    assert discovered["shift_code"]["business_key"] is False
    assert discovered["employee_id"]["business_key"] is True
    assert "private_note" not in discovered

    # The raw value never reaches the payload - only its token does.
    expected_token = filter_value_token(
        TEST_FILTER_TOKEN_SECRET,
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        field_name="shift_code",
        value="DAY-A",
    )
    assert any(
        payload.get("shift_code") == expected_token for payload in tier.payloads.values()
    )
    assert not any(
        payload.get("shift_code") == "DAY-A" for payload in tier.payloads.values()
    )
    assert all("private_note" not in payload for payload in tier.payloads.values())

    hits = get_search(
        client,
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        shift_code="NIGHT-B",
    ).json()["hits"]
    assert {hit["record_key"] for hit in hits} == {"EMP003"}


def test_calling_search_with_no_parameters_at_all_returns_metadata(employee_api):
    """The literal ``GET /v1/search`` case: no params, still 200, never a 422
    demanding ``q``.
    """
    client, _, _ = employee_api
    response = client.get("/v1/search")

    assert response.status_code == 200
    body = response.json()
    assert "available_search" in body
    assert "hits" not in body
    systems = {item["source_system_id"] for item in body["available_search"]}
    assert {"legacy_erp_pg", "legacy_erp_mongo"} <= systems


def test_unknown_dynamic_filter_is_rejected_not_ignored(employee_api):
    client, _, _ = employee_api
    response = get_search(
        client,
        source_system_id="legacy_erp_pg",
        typo_department="Engineering",
    )

    assert response.status_code == 422
    assert "typo_department" in response.text


def test_exact_employee_lookup_requires_source_system(employee_api):
    client, _, _ = employee_api
    response = get_search(client, employee_id="EMP002")

    assert response.status_code == 422


def test_post_search_is_retained_for_existing_clients(employee_api):
    client, _, _ = employee_api
    response = client.post(
        "/v1/search",
        json={
            "query": "employee",
            "filters": {
                "record_key": "EMP002",
                "source_system_id": "legacy_erp_pg",
            },
        },
    )

    assert response.status_code == 200
    assert {hit["record_key"] for hit in response.json()["hits"]} == {"EMP002"}


def test_post_compatibility_route_cannot_bypass_source_identity(employee_api):
    client, _, _ = employee_api
    response = client.post(
        "/v1/search",
        json={"query": "employee", "filters": {"record_key": "EMP002"}},
    )

    assert response.status_code == 422


class Cursor(list):
    def sort(self, *_args):
        return self


class Collection:
    def find(self, *_args, **_kwargs):
        return Cursor(
            [
                {
                    "_id": "mongo-object-id-507f1f77bcf86cd799439011",
                    "employee_id": "EMP002",
                    "department": "Engineering",
                }
            ]
        )


class Database:
    def __getitem__(self, _name):
        return Collection()


def test_mongo_object_id_is_provenance_never_employee_identity():
    entity = employees_entity()
    request = ExtractionRequest(
        schema("legacy_erp_mongo", entity),
        entity,
        key_fields=("employee_id",),
    )
    extracted = MongoSnapshotExtractor().extract(request, lambda: Database())[0]

    assert extracted.record_key == "EMP002"
    assert extracted.metadata["source_object_id"].startswith("mongo-object-id")
    assert "_id" not in extracted.values

    canonical = SourceNativeTransformer().transform_record(
        extracted,
        entity,
        "legacy_erp_mongo",
        SourceType.MONGODB,
        key_fields=("employee_id",),
    )
    assert canonical.source.source_record_key == "EMP002"
    assert canonical.metadata["source_object_id"].startswith("mongo-object-id")
    assert "mongo-object-id" not in canonical.record_id


def test_get_search_keeps_post_level_api_key_protection(tmp_path):
    app = create_app(
        settings=ApiSettings(
            upload_dir=tmp_path / "uploads",
            api_key="search-secret",
            protect_reads=False,
        )
    )
    with TestClient(app) as client:
        assert client.get("/v1/search", params={"q": "employee"}).status_code == 401
