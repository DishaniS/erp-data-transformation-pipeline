"""Phase 7 - asking the ERP about its own structure.

    "Which table stores employee birth certificates?"

These tests use the REAL embedding model, not a deterministic stand-in. A
stand-in would make every ranking assertion here meaningless: the point is
whether semantic retrieval actually finds ``employees`` among plausible
decoys, and a fake model cannot answer that.

The rest of the file guards the boundaries: schema indexing must not index
rows, must not put schema text in Qdrant, and must not disturb the two content
kinds that existed before it.
"""

from __future__ import annotations

import json

import pytest

from erp_pipeline.ai.schema_representation import schema_to_representations
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemoryRepresentationStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.orchestration.models import JobRequest, JobType, PipelineStage
from erp_pipeline.orchestration.planner import PipelinePlanner
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    RelationshipType,
    SchemaOrigin,
)
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
)
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync import InMemoryCanonicalStore

from tests.erp_pipeline.api.test_search_resolution_and_filters import (  # noqa: E402
    InProcessTier,
    PatchedStorage,
)


def field(name, source_type, normalized, **kwargs):
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type=source_type,
        normalized_data_type=normalized,
        **kwargs,
    )


def entity(entity_id, name, fields, **kwargs):
    return SourceEntity(
        entity_id=entity_id,
        source_name=name,
        normalized_name=name,
        entity_kind=kwargs.pop("entity_kind", EntityKind.TABLE),
        fields=tuple(fields),
        **kwargs,
    )


TEXT = FieldDataType.STRING
NUM = FieldDataType.DECIMAL
INT = FieldDataType.INTEGER
BIN = FieldDataType.BINARY
DATE = FieldDataType.DATE


def hr_schema() -> SourceSchema:
    """One real target among four plausible decoys."""
    employees = entity(
        "legacy_hr.public.employees", "employees",
        [
            field("employee_id", "VARCHAR(20)", TEXT, is_primary_key=True,
                  nullable=False, required=True),
            field("full_name", "VARCHAR(200)", TEXT),
            field("department_id", "INTEGER", INT),
            field("birth_certificate", "BYTEA", BIN),
        ],
        primary_key_fields=("employee_id",),
    )
    # Decoys: each plausible for the query, none holding the column.
    notes = entity(
        "legacy_hr.public.employee_notes", "employee_notes",
        [field("note_id", "INTEGER", INT, is_primary_key=True, nullable=False,
               required=True),
         field("employee_id", "VARCHAR(20)", TEXT),
         field("note_text", "TEXT", TEXT)],
        primary_key_fields=("note_id",),
    )
    training = entity(
        "legacy_hr.public.employee_training", "employee_training",
        [field("training_id", "INTEGER", INT, is_primary_key=True,
               nullable=False, required=True),
         field("employee_id", "VARCHAR(20)", TEXT),
         field("course_name", "VARCHAR(200)", TEXT),
         field("completed_on", "DATE", DATE)],
        primary_key_fields=("training_id",),
    )
    archive = entity(
        "legacy_hr.public.document_archive", "document_archive",
        [field("archive_id", "INTEGER", INT, is_primary_key=True,
               nullable=False, required=True),
         field("archived_on", "DATE", DATE),
         field("retention_years", "INTEGER", INT)],
        primary_key_fields=("archive_id",),
    )
    births = entity(
        "legacy_hr.public.birth_records", "birth_records",
        [field("record_id", "INTEGER", INT, is_primary_key=True,
               nullable=False, required=True),
         field("registered_on", "DATE", DATE),
         field("registrar_office", "VARCHAR(200)", TEXT)],
        primary_key_fields=("record_id",),
    )
    departments = entity(
        "legacy_hr.public.departments", "departments",
        [field("department_id", "INTEGER", INT, is_primary_key=True,
               nullable=False, required=True),
         field("department_name", "VARCHAR(100)", TEXT)],
        primary_key_fields=("department_id",),
    )

    return SourceSchema(
        schema_id="sch_hr_1",
        source_system_id="legacy_hr",
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(employees, notes, training, archive, births, departments),
        relationships=(
            SourceRelationship(
                relationship_id="fk_emp_dept",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="employees", to_entity="departments",
                from_fields=("department_id",), to_fields=("department_id",),
            ),
        ),
        metadata={"database": "hrdb"},
        schema_hash="hash_hr_1",
    )


def finance_schema() -> SourceSchema:
    invoices = entity(
        "finance_erp.sales.invoices", "invoices",
        [field("inv_no", "VARCHAR(30)", TEXT, is_primary_key=True,
               nullable=False, required=True),
         field("cust_ref", "VARCHAR(30)", TEXT),
         field("total_amt", "DECIMAL(14,2)", NUM),
         field("curr", "CHAR(3)", TEXT),
         field("approval_status", "VARCHAR(20)", TEXT)],
        primary_key_fields=("inv_no",),
    )
    suppliers = entity(
        "finance_erp.sales.suppliers", "suppliers",
        [field("supplier_id", "VARCHAR(20)", TEXT, is_primary_key=True,
               nullable=False, required=True),
         field("supplier_name", "VARCHAR(200)", TEXT)],
        primary_key_fields=("supplier_id",),
    )
    purchase_orders = entity(
        "finance_erp.sales.purchase_orders", "purchase_orders",
        [field("po_number", "VARCHAR(30)", TEXT, is_primary_key=True,
               nullable=False, required=True),
         field("supplier_id", "VARCHAR(20)", TEXT),
         field("ordered_on", "DATE", DATE)],
        primary_key_fields=("po_number",),
    )

    return SourceSchema(
        schema_id="sch_fin_1",
        source_system_id="finance_erp",
        schema_name="sales",
        origin=SchemaOrigin.DISCOVERED,
        entities=(invoices, suppliers, purchase_orders),
        relationships=(
            SourceRelationship(
                relationship_id="fk_po_supplier",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="purchase_orders", to_entity="suppliers",
                from_fields=("supplier_id",), to_fields=("supplier_id",),
            ),
        ),
        metadata={"database": "findb"},
    )


def payroll_schema() -> SourceSchema:
    """A SECOND employees table, in a different system."""
    employees = entity(
        "legacy_payroll.public.employees", "employees",
        [field("emp_no", "VARCHAR(20)", TEXT, is_primary_key=True,
               nullable=False, required=True),
         field("gross_pay", "DECIMAL(12,2)", NUM),
         field("tax_code", "VARCHAR(10)", TEXT)],
        primary_key_fields=("emp_no",),
    )

    return SourceSchema(
        schema_id="sch_pay_1",
        source_system_id="legacy_payroll",
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(employees,),
        metadata={"database": "paydb"},
    )


@pytest.fixture(scope="module")
def embedding_service():
    """The REAL model. A stand-in would make every ranking assertion vacuous."""
    pytest.importorskip("sentence_transformers")

    from erp_pipeline.ai.embedding import SentenceTransformerModel

    return EmbeddingService(SentenceTransformerModel())


class RealModelTier(InProcessTier):
    """The shared in-process tier, sized for the production model.

    ``InProcessTier.dimension`` is a class attribute pinned to the fake model's
    width; these tests use the real 384-dimensional one, and the store checks
    tier width before writing.
    """

    dimension = 384


class Harness:
    def __init__(self, tmp_path, embedding_service):
        RealModelTier.dimension = embedding_service.dimension
        self.representations = InMemoryRepresentationStore()
        self.storage = PatchedStorage(
            hot=RealModelTier(),
            state_store=InMemoryTierStateStore(),
        )
        self.services = PipelineServices(
            records=InMemoryCanonicalStore(),
            representations=self.representations,
            storage=self.storage,
            embedding=embedding_service,
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

    def index(self, schema: SourceSchema) -> str:
        self.services.schema_cache[schema.schema_id] = schema
        job_id, status, error = self.orchestration.index_schema(schema.schema_id)

        assert error is None, error

        return status


@pytest.fixture(scope="module")
def indexed(tmp_path_factory, embedding_service):
    built = Harness(tmp_path_factory.mktemp("schema"), embedding_service)

    for schema in (hr_schema(), finance_schema(), payroll_schema()):
        assert built.index(schema) == "succeeded"

    return built


@pytest.fixture
def client(indexed):
    from fastapi.testclient import TestClient

    with TestClient(indexed.app) as test_client:
        yield test_client


def search(client, query, top_k=5, **filters):
    filters.setdefault("content_kind", "schema")

    return client.post(
        "/v1/search",
        json={"query": query, "top_k": top_k, "filters": filters},
    ).json()["hits"]


def entities_of(client, hits):
    names = []

    for hit in hits:
        body = client.get(f"/v1/representations/{hit['representation_id']}").json()
        names.append(body["source_entity"])

    return names


# ======================================================================
# TEST A / C - the headline query, against decoys
# ======================================================================


def test_the_birth_certificate_query_finds_the_employees_table(client):
    """Five plausible decoys; only ``employees`` holds the column."""
    hits = search(client, "Which ERP table contains employee birth certificates?")

    assert hits
    assert entities_of(client, hits)[0] == "employees"


def test_the_top_hit_shows_the_column_and_both_of_its_types(client):
    hits = search(client, "Which table stores employee birth certificates?")
    body = client.get(
        f"/v1/representations/{hits[0]['representation_id']}"
    ).json()

    assert "birth_certificate" in body["text"]
    assert "BYTEA" in body["text"]
    assert "binary" in body["text"]


def test_a_decoy_named_after_the_query_does_not_win(client):
    """``birth_records`` is the trap: right words, wrong table."""
    hits = search(client, "employee birth certificate column", top_k=3)
    ranked = entities_of(client, hits)

    assert ranked[0] == "employees"


# ======================================================================
# TEST B / D / E - other query shapes
# ======================================================================


def test_an_invoice_amount_query_finds_the_invoices_table(client):
    hits = search(client, "Where is the invoice total amount stored?")

    assert entities_of(client, hits)[0] == "invoices"


def test_a_datatype_query_finds_the_binary_column_within_the_top_three(client):
    """A KNOWN rank-1 failure, asserted at the recall it actually achieves.

    "Which employee field stores binary document data?" does not rank
    ``legacy_hr.employees`` first. Measured behaviour:

        unfiltered       legacy_payroll.employees, hr.employee_notes,
                         hr.employees                      <- correct, 3rd
        source-filtered  hr.employee_notes, hr.employees   <- correct, 2nd

    Two systems both have an ``employees`` table and the query names neither,
    so the strongest signal in it - "employee" - is ambiguous by construction.
    The datatype words carry less weight than the entity name.

    The query is NOT reworded to make this pass, and no vocabulary was tuned
    after seeing it fail. The honest assertion is the recall the system
    actually delivers; the rank-1 miss is reported in the Phase 7 evaluation
    and in the report's limitations.
    """
    hits = search(client, "Which employee field stores binary document data?", top_k=3)
    bodies = [
        client.get(f"/v1/representations/{hit['representation_id']}").json()
        for hit in hits
    ]

    binary_holders = [
        body for body in bodies if "Normalized Type: binary" in body["text"]
    ]

    assert binary_holders, "the binary column is not retrievable at all"
    assert binary_holders[0]["source_entity"] == "employees"
    assert binary_holders[0]["source_system_id"] == "legacy_hr"


def test_a_relationship_query_finds_the_real_discovered_relationship(client):
    hits = search(client, "How are purchase orders related to suppliers?", top_k=3)
    texts = [
        client.get(f"/v1/representations/{hit['representation_id']}").json()["text"]
        for hit in hits
    ]

    assert any(
        "purchase_orders.supplier_id -> suppliers.supplier_id" in text
        for text in texts
    )


# ======================================================================
# TEST G - cross-source scoping
# ======================================================================


def test_two_systems_both_have_an_employees_table(client):
    """The disambiguation problem is real, not hypothetical."""
    hits = search(client, "employees table", top_k=20)
    systems = {
        client.get(f"/v1/representations/{hit['representation_id']}").json()[
            "source_system_id"
        ]
        for hit in hits
    }

    assert "legacy_hr" in systems


def test_the_source_filter_excludes_the_other_system(client):
    hits = search(client, "employees table", top_k=20, source_system_id="legacy_hr")

    assert hits

    for hit in hits:
        body = client.get(
            f"/v1/representations/{hit['representation_id']}"
        ).json()

        assert body["source_system_id"] == "legacy_hr"
        assert body["source_entity"] != "gross_pay"


def test_the_payroll_filter_returns_only_payroll(client):
    hits = search(
        client, "employee pay", top_k=20, source_system_id="legacy_payroll"
    )

    assert hits

    for hit in hits:
        body = client.get(
            f"/v1/representations/{hit['representation_id']}"
        ).json()

        assert body["source_system_id"] == "legacy_payroll"


def test_the_schema_name_filter_scopes_a_search(client):
    hits = search(client, "invoice", top_k=20, schema_name="sales")

    assert hits

    for hit in hits:
        body = client.get(
            f"/v1/representations/{hit['representation_id']}"
        ).json()

        assert body["schema_name"] == "sales"


def test_the_entity_kind_filter_works(client):
    hits = search(client, "employees", top_k=20, entity_kind="table")

    assert hits

    for hit in hits:
        body = client.get(
            f"/v1/representations/{hit['representation_id']}"
        ).json()

        assert body["entity_kind"] == "table"


# ======================================================================
# TEST P - every schema hit resolves
# ======================================================================


@pytest.mark.parametrize(
    "query",
    [
        "employee birth certificate",
        "invoice amount currency",
        "supplier relationship",
        "department name",
        "payroll tax code",
    ],
)
def test_every_schema_hit_resolves_to_its_text(client, query):
    hits = search(client, query, top_k=10)

    assert hits

    for hit in hits:
        response = client.get(
            f"/v1/representations/{hit['representation_id']}"
        )

        assert response.status_code == 200
        assert response.json()["text"]
        assert response.json()["content_kind"] == "schema"


def test_a_schema_hit_carries_full_provenance(client):
    hits = search(client, "employee birth certificate")
    body = client.get(f"/v1/representations/{hits[0]['representation_id']}").json()

    assert body["content_kind"] == "schema"
    assert body["source_system_id"] == "legacy_hr"
    assert body["schema_name"] == "public"
    assert body["schema_id"] == "sch_hr_1"
    assert body["entity_id"] == "legacy_hr.public.employees"
    assert body["entity_kind"] == "table"
    assert body["schema_chunk_index"] == 0


# ======================================================================
# TEST L / M - what must never appear
# ======================================================================


def test_no_business_value_is_reachable_through_schema_search(client, indexed):
    surface = json.dumps(
        [
            indexed.representations.get(key).text_for_ai
            for key in indexed.representations.list_ids()
        ]
    )

    for value in ("EMP002", "Nimal Silva", "INV-204", "45000.00", "250000"):
        assert value not in surface


def test_schema_text_never_reaches_the_vector_payload(indexed):
    from erp_pipeline.storage.migration import _payload_for

    surface = json.dumps(
        [_payload_for(state) for state in indexed.storage.state.list_all()],
        default=str,
    )

    assert "Content Kind: ERP Schema" not in surface
    assert "Source Type:" not in surface
    assert "text_for_ai" not in surface


def test_the_payload_carries_schema_identity_only(indexed):
    from erp_pipeline.storage.migration import _payload_for

    for state in indexed.storage.state.list_all():
        payload = _payload_for(state)

        if payload.get("content_kind") != "schema":
            continue

        assert payload["source_system_id"]
        assert payload["schema_name"]
        assert payload["entity_kind"]
        assert "text" not in payload


# ======================================================================
# TEST O - the vocabulary stays closed
# ======================================================================


def test_an_undefined_content_kind_is_still_refused(client):
    response = client.post(
        "/v1/search",
        json={"query": "x", "filters": {"content_kind": "schema_table"}},
    )

    assert response.status_code == 422


def test_schema_is_now_an_accepted_content_kind(client):
    response = client.post(
        "/v1/search", json={"query": "x", "filters": {"content_kind": "schema"}}
    )

    assert response.status_code == 200


def test_an_unknown_filter_is_still_refused(client):
    response = client.post(
        "/v1/search",
        json={"query": "x", "filters": {"table_name": "employees"}},
    )

    assert response.status_code == 422


# ======================================================================
# TEST J / K - reindex and schema change
# ======================================================================


def test_reindexing_the_same_schema_creates_no_duplicates(
    tmp_path, embedding_service
):
    built = Harness(tmp_path, embedding_service)
    built.index(hr_schema())
    first = built.representations.count()
    built.index(hr_schema())

    assert built.representations.count() == first


def test_a_grown_table_updates_its_searchable_structure(
    tmp_path, embedding_service
):
    """TEST K: the current structure is what search returns."""
    built = Harness(tmp_path, embedding_service)

    before = SourceSchema(
        schema_id="v1", source_system_id="legacy_hr", schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(
            entity(
                "legacy_hr.public.employees", "employees",
                [field("employee_id", "VARCHAR(20)", TEXT, is_primary_key=True,
                       nullable=False, required=True),
                 field("full_name", "VARCHAR(200)", TEXT)],
                primary_key_fields=("employee_id",),
            ),
        ),
    )
    built.index(before)
    count_before = built.representations.count()
    stored_before = [
        built.representations.get(key).text_for_ai
        for key in built.representations.list_ids()
    ]

    assert not any("birth_certificate" in text for text in stored_before)

    after = SourceSchema(
        # A DIFFERENT snapshot id - the catalog keeps both.
        schema_id="v2", source_system_id="legacy_hr", schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(
            entity(
                "legacy_hr.public.employees", "employees",
                [field("employee_id", "VARCHAR(20)", TEXT, is_primary_key=True,
                       nullable=False, required=True),
                 field("full_name", "VARCHAR(200)", TEXT),
                 field("birth_certificate", "BYTEA", BIN)],
                primary_key_fields=("employee_id",),
            ),
        ),
    )
    built.index(after)
    stored_after = [
        built.representations.get(key).text_for_ai
        for key in built.representations.list_ids()
    ]

    # Updated in place: the new column is searchable, the old text is gone,
    # and no rival "employees-v1" representation remains.
    assert built.representations.count() == count_before
    assert any("birth_certificate" in text for text in stored_after)
    assert all("schema_id" not in text for text in stored_after)


def test_a_shrunk_table_leaves_no_stale_field_groups(
    tmp_path, embedding_service
):
    """A wide table that loses columns must not keep answering for them."""
    built = Harness(tmp_path, embedding_service)

    def sized(count, schema_id):
        return SourceSchema(
            schema_id=schema_id, source_system_id="erp", schema_name="public",
            origin=SchemaOrigin.DISCOVERED,
            entities=(
                entity(
                    "erp.public.wide", "wide",
                    [field(f"column_{i:03d}", "VARCHAR(50)", TEXT)
                     for i in range(count)],
                ),
            ),
        )

    built.index(sized(60, "wide_v1"))
    wide_count = built.representations.count()

    assert wide_count > 1

    built.index(sized(5, "wide_v2"))

    assert built.representations.count() < wide_count

    surviving = [
        built.representations.get(key).text_for_ai
        for key in built.representations.list_ids()
    ]

    assert not any("column_059" in text for text in surviving)


# ======================================================================
# Planning and the manual route
# ======================================================================


def test_the_schema_plan_reuses_the_standard_tail():
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.SCHEMA_PIPELINE, schema_id="s1"), None
    )

    assert plan.stages == (
        PipelineStage.AI_BUILD,
        PipelineStage.PERSIST_REPRESENTATIONS,
        PipelineStage.EMBED,
        PipelineStage.TIER_ROUTE,
        # Phase 9: schema chunks are lifecycle-managed too - a shrunk entity
        # must not keep answering for field groups it no longer has.
        PipelineStage.LIFECYCLE_COMMIT,
    )


def test_persistence_precedes_embedding_for_schemas():
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.SCHEMA_PIPELINE, schema_id="s1"), None
    )
    stages = list(plan.stages)

    assert stages.index(PipelineStage.PERSIST_REPRESENTATIONS) < stages.index(
        PipelineStage.EMBED
    )


def test_a_schema_job_without_a_schema_id_is_refused():
    from erp_pipeline.orchestration.errors import InvalidPipelineRequestError

    with pytest.raises(InvalidPipelineRequestError):
        PipelinePlanner().plan(
            JobRequest(job_type=JobType.SCHEMA_PIPELINE), None
        )


def test_the_manual_schema_job_route_works(client, indexed):
    """TEST J: re-index on demand, through the existing job API."""
    response = client.post(
        "/v1/jobs",
        json={"job_type": "schema_pipeline", "schema_id": "sch_hr_1"},
    )

    assert response.status_code == 202

    job = client.get(f"/v1/jobs/{response.json()['job_id']}").json()

    assert job["status"] == "succeeded"


def test_a_single_entity_can_be_reindexed(client):
    response = client.post(
        "/v1/jobs",
        json={
            "job_type": "schema_pipeline",
            "schema_id": "sch_hr_1",
            "entity": "employees",
        },
    )

    assert response.status_code == 202

    job = client.get(f"/v1/jobs/{response.json()['job_id']}").json()

    assert job["status"] == "succeeded"
    assert job["counters"]["schema_entities_indexed"] == 1


def test_an_entity_not_in_the_schema_fails_the_job(client):
    response = client.post(
        "/v1/jobs",
        json={
            "job_type": "schema_pipeline",
            "schema_id": "sch_hr_1",
            "entity": "no_such_table",
        },
    )
    job = client.get(f"/v1/jobs/{response.json()['job_id']}").json()

    assert job["status"] == "failed"


# ======================================================================
# TEST N - the other content kinds are untouched
# ======================================================================


def test_schema_vectors_do_not_appear_in_a_document_search(client):
    hits = client.post(
        "/v1/search",
        json={
            "query": "employee birth certificate",
            "top_k": 20,
            "filters": {"content_kind": "document_chunk"},
        },
    ).json()["hits"]

    assert hits == []


def test_schema_vectors_do_not_appear_in_a_record_search(client):
    hits = client.post(
        "/v1/search",
        json={
            "query": "employee",
            "top_k": 20,
            "filters": {"content_kind": "structured_record"},
        },
    ).json()["hits"]

    assert hits == []


def test_an_unfiltered_search_can_still_reach_schema_vectors(client):
    hits = client.post(
        "/v1/search",
        json={"query": "employee birth certificate", "top_k": 20},
    ).json()["hits"]

    assert hits


def test_schema_vectors_share_the_existing_collections(indexed):
    """No new Qdrant collection: the distinction is metadata."""
    tiers = {
        state.current_tier.value for state in indexed.storage.state.list_all()
    }

    assert tiers <= {"hot", "warm", "cold"}
