"""GET /v1/search redesign: one endpoint, two modes, canonical identity.

These are the seven proofs the redesign brief asked for:

    1. GET /v1/search with no params returns the live, dynamic metadata
       catalog - never a 422 demanding ``q``.
    2. EMP001 exact retrieval via canonical identity
       (source_system_id + source_entity + record_key).
    3. A semantic query scoped to EMP001 stays scoped to EMP001.
    4. EMP002 never leaks into an EMP001-scoped query, even when the query
       text would otherwise match EMP002 too.
    5. legacy_erp_pg/EMP001 and legacy_erp_mongo/EMP001 are the same business
       key in two systems and are never mixed.
    6. A schema change appears through GET /v1/search with no backend field
       list to update - the catalog is read live from the schema cache.
    7. An exact identity filter is pushed into Qdrant as a server-side filter
       - a single ANN call over the matching candidates, never an unfiltered
       scan of the whole collection.

All of this already existed in narrower form (test_get_dynamic_employee_search.py
uses EMP002/EMP003/EMP004); this file exercises it explicitly against EMP001,
the canonical identity example the redesign brief itself uses, and adds the
one property no existing test asserted directly: that an exact-scoped query
issues exactly one filtered vector-store call.
"""

from __future__ import annotations

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
from erp_pipeline.schemas.source_models import SourceEntity
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync import InMemoryCanonicalStore
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

from tests.erp_pipeline.api.test_get_dynamic_employee_search import (
    RecordingTier,
    employees_entity,
    field,
    get_search,
    schema,
)
from tests.erp_pipeline.api.test_search_resolution_and_filters import (
    DIMENSION,
    TEST_FILTER_TOKEN_SECRET,
    DeterministicTestModel,
    PatchedStorage,
)


class ScanTrackingTier(RecordingTier):
    """Records every ``search`` call, not just the last one.

    ``RecordingTier`` (used elsewhere) only remembers the most recent filter.
    Proving "no full scan when exact filters exist" needs the CALL COUNT too:
    a correct implementation issues exactly one filtered ANN call, never a
    broad call followed by a second, narrower retry.
    """

    def __init__(self) -> None:
        super().__init__()
        self.search_calls: list[object] = []

    def search(self, vector, limit=5, query_filter=None):
        self.search_calls.append(query_filter)

        return super().search(vector, limit=limit, query_filter=query_filter)


def _employee_row(key: str, name: str, department: str) -> SourceRecord:
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


def _with_cost_center(entity: SourceEntity) -> SourceEntity:
    """The SAME entity, as it would look after discovery grows by one column.

    Built by extending the existing entity's own field tuple rather than
    duplicating its literal definition, so this test cannot silently drift
    from what ``employees_entity()`` actually declares.
    """
    return SourceEntity(
        entity_id=entity.entity_id,
        source_name=entity.source_name,
        normalized_name=entity.normalized_name,
        namespace=entity.namespace,
        entity_kind=entity.entity_kind,
        primary_key_fields=entity.primary_key_fields,
        fields=(*entity.fields, field("cost_center")),
    )


@pytest.fixture
def api(tmp_path):
    """EMP001 in two ERP systems, EMP002 as the leakage trap, plus 30 decoys.

    The decoys exist so "no full scan" is a meaningful claim: if the exact
    filter were not pushed server-side, an EMP001 query would have 33
    candidates to rank instead of 2.
    """
    entity = employees_entity()
    pg_schema = schema("legacy_erp_pg", entity)
    mongo_schema = schema("legacy_erp_mongo", entity)
    transformer = SourceNativeTransformer()

    def transform(system: str, source_type: SourceType, row: SourceRecord):
        return transformer.transform_record(row, entity, system, source_type)

    records = [
        transform(
            "legacy_erp_pg",
            SourceType.POSTGRESQL,
            _employee_row("EMP001", "Kasun Fernando", "Engineering"),
        ),
        transform(
            "legacy_erp_pg",
            SourceType.POSTGRESQL,
            _employee_row("EMP002", "Nimal Perera", "Engineering"),
        ),
        transform(
            "legacy_erp_mongo",
            SourceType.MONGODB,
            _employee_row("EMP001", "Kasun Fernando", "Engineering"),
        ),
    ]
    records.extend(
        transform(
            "legacy_erp_pg",
            SourceType.POSTGRESQL,
            _employee_row(f"EMP{100 + i}", f"Decoy Employee {i}", "Engineering"),
        )
        for i in range(30)
    )

    canonical_store = InMemoryCanonicalStore()
    tier = ScanTrackingTier()
    storage = PatchedStorage(hot=tier, state_store=InMemoryTierStateStore())
    embedding = EmbeddingService(
        DeterministicTestModel(dimension=DIMENSION),
        filter_token_secret=TEST_FILTER_TOKEN_SECRET,
    )

    for record in records:
        canonical_store.upsert(record)
        storage.store(embedding.embed_one(canonical_record_to_representation(record)))

    services = PipelineServices(
        records=canonical_store,
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
        yield client, tier, services, pg_schema.schema_id


def _entity_field_names(client: TestClient, system: str, entity_name: str) -> set[str]:
    # Bare call: any identity parameter (including source_system_id /
    # source_entity) now switches GET /v1/search into search mode, so the
    # metadata catalog is reached only unscoped and filtered client-side.
    body = client.get("/v1/search").json()

    return {
        f["name"]
        for item in body["available_search"]
        if item["source_system_id"] == system and item["source_entity"] == entity_name
        for f in item["fields"]
    }


# ======================================================================
# 1. GET /v1/search with no params -> dynamic metadata, not a 422
# ======================================================================


def test_search_with_no_params_returns_dynamic_metadata_not_an_error(api):
    client, _, _, _ = api

    response = client.get("/v1/search")

    assert response.status_code == 200
    body = response.json()
    assert "hits" not in body
    assert "available_search" in body

    entities = {
        (item["source_system_id"], item["source_entity"]): item
        for item in body["available_search"]
    }

    assert ("legacy_erp_pg", "hr.employees") in entities
    assert ("legacy_erp_mongo", "hr.employees") in entities

    pg_fields = {f["name"] for f in entities[("legacy_erp_pg", "hr.employees")]["fields"]}
    assert {"employee_id", "full_name", "department", "status", "shift_code"} <= pg_fields
    # Never hardcoded to employees, and never leaking a field its own schema
    # metadata marked non-filterable.
    assert "private_note" not in pg_fields


# ======================================================================
# 2. EMP001 exact retrieval via canonical identity
# ======================================================================


def test_emp001_exact_retrieval_by_canonical_identity(api):
    client, _, _, _ = api

    hits = get_search(
        client,
        employee_id="EMP001",
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
    ).json()["hits"]

    assert len(hits) == 1
    assert hits[0]["record_key"] == "EMP001"
    assert hits[0]["source_system_id"] == "legacy_erp_pg"
    assert hits[0]["source_entity"] == "hr.employees"


# ======================================================================
# 3. Semantic search stays inside the EMP001 scope
# ======================================================================


def test_semantic_search_is_scoped_to_emp001(api):
    client, _, _, _ = api

    hits = get_search(
        client,
        employee_id="EMP001",
        source_system_id="legacy_erp_pg",
        q="employee engineering department record",
    ).json()["hits"]

    assert hits
    assert {hit["record_key"] for hit in hits} == {"EMP001"}


# ======================================================================
# 4. No EMP002 leakage
# ======================================================================


def test_no_emp002_leakage_into_an_emp001_scoped_query(api):
    client, _, _, _ = api

    # The query text names EMP002's own department and would rank EMP002
    # highly in an unscoped search; the identity filter must still win.
    hits = get_search(
        client,
        employee_id="EMP001",
        source_system_id="legacy_erp_pg",
        q="Nimal Perera Engineering department",
    ).json()["hits"]

    assert hits
    assert all(hit["record_key"] == "EMP001" for hit in hits)


# ======================================================================
# 5. PostgreSQL / Mongo EMP001 isolation
# ======================================================================


def test_postgresql_and_mongo_emp001_are_never_mixed(api):
    client, _, _, _ = api

    pg_hits = get_search(
        client, employee_id="EMP001", source_system_id="legacy_erp_pg"
    ).json()["hits"]
    mongo_hits = get_search(
        client, employee_id="EMP001", source_system_id="legacy_erp_mongo"
    ).json()["hits"]

    assert {hit["source_system_id"] for hit in pg_hits} == {"legacy_erp_pg"}
    assert {hit["source_system_id"] for hit in mongo_hits} == {"legacy_erp_mongo"}
    # Same business key, two systems: the canonical identity differs even
    # though EMP001 alone does not.
    assert pg_hits[0]["canonical_record_id"] != mongo_hits[0]["canonical_record_id"]


# ======================================================================
# 6. A schema change appears through GET /v1/search automatically
# ======================================================================


def test_a_schema_change_appears_through_get_search_with_no_hardcoded_field_list(api):
    client, _, services, pg_schema_id = api

    before = _entity_field_names(client, "legacy_erp_pg", "hr.employees")
    assert "cost_center" not in before

    # Simulate discovery re-running and the catalog picking up a new column -
    # no backend code change, no new endpoint call, just an updated schema
    # snapshot under the SAME schema_id the service already tracks.
    grown_entity = _with_cost_center(employees_entity())
    services.schema_cache[pg_schema_id] = schema("legacy_erp_pg", grown_entity)

    after = _entity_field_names(client, "legacy_erp_pg", "hr.employees")
    assert "cost_center" in after

    # The new field is now a genuinely accepted filter, not merely listed.
    # Before this update it would have been an unknown filter (422).
    response = get_search(
        client,
        source_system_id="legacy_erp_pg",
        source_entity="hr.employees",
        cost_center="CC-04",
    )
    assert response.status_code == 200


# ======================================================================
# 7. No full Qdrant scan when exact filters exist
# ======================================================================


def test_exact_identity_filter_is_pushed_server_side_not_scanned_afterward(api):
    client, tier, _, _ = api

    body = get_search(
        client,
        employee_id="EMP001",
        source_system_id="legacy_erp_pg",
        q="employee record",
    ).json()

    # Exactly one ANN call: no unfiltered call followed by a client-side
    # narrowing pass.
    assert len(tier.search_calls) == 1

    query_filter = tier.search_calls[0]
    assert query_filter is not None
    conditions = {c.key: c.match.value for c in query_filter.must}
    assert conditions["record_key"] == "EMP001"
    assert conditions["source_system_id"] == "legacy_erp_pg"

    # The 30 decoy employees stored alongside EMP001 in the SAME collection
    # never surface: the candidate set Qdrant ranked was already scoped to
    # the matching identity, not the whole collection filtered afterward.
    assert {hit["record_key"] for hit in body["hits"]} == {"EMP001"}
