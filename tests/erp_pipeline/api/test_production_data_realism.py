"""Production logic must take business values from data, never invent them.

WHAT THESE PIN
--------------
The audit that produced these tests found production code fabricating two
thirds of the canonical identity triple for a partially-declared document
upload:

    source_system_id = identity.source_system_id or "uploaded"
    source_entity    = identity.source_entity    or "documents"
    source_field     = identity.document_type    or "upload"

Those three are FILTERABLE Qdrant payload keys. A stand-in there is
indistinguishable from a real source system of that name, and it collapses
every anonymous upload - from any number of unrelated ERP systems - into one
synthetic identity.

The rule these tests enforce is narrow and absolute: a value that describes
the BUSINESS must come from the request, the registered source, the
discovered schema, the stored record or the document itself. Where none of
those supplies it, it is ABSENT - never defaulted, never guessed.

Infrastructure defaults (host, port, batch size) are explicitly out of scope
here; they are configuration, not business facts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from erp_pipeline.ai.attached_documents import DocumentAttachment
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
from erp_pipeline.storage.state import InMemoryTierStateStore
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
    TEST_FILTER_TOKEN_SECRET,
    DeterministicTestModel,
    PatchedStorage,
)


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
def two_system_api(tmp_path):
    """The SAME business key in two different ERP systems and entities.

    Nothing about the corpus is hardcoded in production code - it is supplied
    entirely by this fixture, which is the point: swapping these names must
    require no source change.
    """
    entity = employees_entity()
    transformer = SourceNativeTransformer()

    records = [
        transformer.transform_record(
            _row("EMP-0001", "Kasun Fernando", "Engineering"),
            entity,
            "acme_erp_pg",
            SourceType.POSTGRESQL,
        ),
        transformer.transform_record(
            _row("EMP-0002", "Nimal Perera", "Finance"),
            entity,
            "acme_erp_pg",
            SourceType.POSTGRESQL,
        ),
        transformer.transform_record(
            _row("EMP-0001", "Different Person Entirely", "Logistics"),
            entity,
            "globex_erp_mongo",
            SourceType.MONGODB,
        ),
    ]

    canonical_store = InMemoryCanonicalStore()
    tier = RecordingTier()
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
            s.schema_id: s
            for s in (schema("acme_erp_pg", entity), schema("globex_erp_mongo", entity))
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
        yield client, tier, services


# ======================================================================
# 1-4. Business identity comes from the request, never from the code
# ======================================================================


def test_two_different_source_systems_produce_distinct_results(two_system_api):
    """Neither system name appears anywhere in production source."""
    client, _, _ = two_system_api

    acme = client.get(
        "/v1/search",
        params={
            "source_system_id": "acme_erp_pg",
            "source_entity": "hr.employees",
            "record_key": "EMP-0001",
        },
    ).json()["hits"]
    globex = client.get(
        "/v1/search",
        params={
            "source_system_id": "globex_erp_mongo",
            "source_entity": "hr.employees",
            "record_key": "EMP-0001",
        },
    ).json()["hits"]

    assert {h["source_system_id"] for h in acme} == {"acme_erp_pg"}
    assert {h["source_system_id"] for h in globex} == {"globex_erp_mongo"}
    # Same business key, two systems: the canonical identity still differs.
    assert acme[0]["canonical_record_id"] != globex[0]["canonical_record_id"]


def test_a_source_entity_the_code_has_never_seen_works_unchanged(two_system_api):
    """`hr.employees` is fixture data, not a production constant.

    The entity name reaches the filter from the request and the discovered
    schema. A different entity would work identically; what must NOT happen is
    the code recognising this particular name.
    """
    client, _, _ = two_system_api

    body = client.get("/v1/search").json()
    entities = {
        (i["source_system_id"], i["source_entity"]) for i in body["available_search"]
    }

    assert ("acme_erp_pg", "hr.employees") in entities
    assert ("globex_erp_mongo", "hr.employees") in entities


def test_a_record_key_is_only_ever_the_one_supplied(two_system_api):
    """EMP-0001/EMP-0002 come from the corpus and the query - nowhere else."""
    client, _, _ = two_system_api

    for key in ("EMP-0001", "EMP-0002"):
        hits = client.get(
            "/v1/search",
            params={
                "source_system_id": "acme_erp_pg",
                "source_entity": "hr.employees",
                "record_key": key,
            },
        ).json()["hits"]

        assert hits, f"{key} should be retrievable"
        assert {h["record_key"] for h in hits} == {key}


def test_search_never_defaults_to_a_particular_employee(two_system_api):
    """An unscoped call returns METADATA, not somebody's record."""
    client, _, _ = two_system_api

    body = client.get("/v1/search").json()

    assert "available_search" in body
    assert "hits" not in body
    # No employee identifier is manufactured anywhere in the metadata response.
    rendered = str(body)
    assert "EMP-0001" not in rendered
    assert "EMP-0002" not in rendered


# ======================================================================
# 5-7. Document identity is declared or absent - never invented
# ======================================================================


def test_an_undeclared_upload_invents_no_source_identity():
    """THE REGRESSION. Was: source_system_id="uploaded", entity="documents".

    A caller who declares only a business key must not have a source system
    and entity manufactured for them. Those are two thirds of the canonical
    identity triple and are filterable payload keys.
    """
    attachment = DocumentAttachment(
        document_id="sha256-abc",
        business_key_name="employee_id",
        business_key_value="EMP-0002",
        attachment_scope="employee_id=EMP-0002",
    )

    payload = attachment.to_metadata()

    # Absent, not a stand-in: a filter on either correctly EXCLUDES this doc.
    assert "source_system_id" not in payload
    assert "source_entity" not in payload
    assert payload.get("source_system_id") != "uploaded"
    assert payload.get("source_entity") != "documents"
    # What WAS declared is carried through faithfully.
    assert payload["business_key_value"] == "EMP-0002"
    assert payload["record_key"] == "EMP-0002"


def test_an_undeclared_upload_attaches_to_no_parent_record():
    """No parent is derived from the business key.

    An `employee_id` is not a canonical record id. Manufacturing one would put
    a fabricated reference into a field consumers actually resolve.
    """
    attachment = DocumentAttachment(
        document_id="sha256-abc",
        business_key_name="employee_id",
        business_key_value="EMP-0002",
        attachment_scope="employee_id=EMP-0002",
    )

    payload = attachment.to_metadata()

    assert "parent_record_id" not in payload


def test_document_type_is_never_forced_to_a_vocabulary_value():
    """No default type - not `medical_claim`, not `upload`, not anything."""
    undeclared = DocumentAttachment(document_id="d1", attachment_scope="d1")

    assert "document_type" not in undeclared.to_metadata()

    # A database BLOB legitimately uses its ERP COLUMN NAME as the type: that
    # is real ERP context, not a guess. Kept, and proven to be the column.
    from_column = DocumentAttachment(
        document_id="d2", attachment_scope="d2", source_field="birth_certificate"
    )

    assert from_column.to_metadata()["document_type"] == "birth_certificate"

    # An explicit declaration always wins over the column.
    declared = DocumentAttachment(
        document_id="d3",
        attachment_scope="d3",
        source_field="birth_certificate",
        document_type="identity_document",
    )

    assert declared.to_metadata()["document_type"] == "identity_document"


def test_a_fully_declared_upload_carries_exactly_what_was_declared():
    """The positive case: declared identity is passed through unchanged."""
    attachment = DocumentAttachment(
        parent_record_id="erp:acme_erp_pg:hr.employees:emp-0002",
        source_system_id="acme_erp_pg",
        source_entity="hr.employees",
        source_field="birth_certificate",
        document_id="sha256-abc",
        business_key_name="employee_id",
        business_key_value="EMP-0002",
        document_type="birth_certificate",
        sensitivity="internal",
    )

    payload = attachment.to_metadata()

    assert payload["source_system_id"] == "acme_erp_pg"
    assert payload["source_entity"] == "hr.employees"
    assert payload["source_field"] == "birth_certificate"
    assert payload["parent_record_id"] == "erp:acme_erp_pg:hr.employees:emp-0002"
    assert payload["document_type"] == "birth_certificate"
    assert payload["sensitivity"] == "internal"


def test_qdrant_payload_identity_matches_the_actual_source(two_system_api):
    """Every identity key in a stored point traces to the record it came from."""
    _, tier, _ = two_system_api

    seen = {
        (p.get("source_system_id"), p.get("source_entity"), p.get("record_key"))
        for p in tier.payloads.values()
    }

    assert ("acme_erp_pg", "hr.employees", "EMP-0001") in seen
    assert ("acme_erp_pg", "hr.employees", "EMP-0002") in seen
    assert ("globex_erp_mongo", "hr.employees", "EMP-0001") in seen
    # Nothing manufactured leaked into a payload.
    for placeholder in ("uploaded", "documents", "unknown_source", "unknown_entity"):
        assert not any(
            placeholder in (p.get("source_system_id"), p.get("source_entity"))
            for p in tier.payloads.values()
        )


# ======================================================================
# 8. Runtime metadata reflects the runtime, not a literal
# ======================================================================


def test_reported_embedding_metadata_follows_configuration():
    """Was: literal "sentence-transformers/all-MiniLM-L6-v2" and literal 384.

    The model id must come from the constant the loader actually builds from,
    and the pre-load width must be the CONFIGURED width - otherwise an
    operator running a 768-dimension deployment is told 384 by
    `/v1/capabilities` while the vector store expects the other number.
    """
    from erp_pipeline.ai.embedding import DEFAULT_MODEL_ID
    from erp_pipeline.runtime.services import _LazyEmbeddingService

    service = _LazyEmbeddingService(configured_dimension=384)

    assert service.model_id == DEFAULT_MODEL_ID
    assert service.dimension == 384
    # Reporting must not have loaded the model.
    assert service.loaded is False

    reconfigured = _LazyEmbeddingService(configured_dimension=768)

    assert reconfigured.dimension == 768
    assert reconfigured.loaded is False


def test_capabilities_reports_the_wired_embedding_service(two_system_api):
    """Not a literal: the value comes from whatever service is wired in."""
    client, _, services = two_system_api

    body = client.get("/v1/capabilities").json()

    assert body["embedding_model"] == services.embedding.model_id
    assert body["embedding_dimension"] == services.embedding.dimension
    # This fixture wires a deterministic test model, so the response proves it
    # is reporting the WIRED service rather than the production default.
    assert body["embedding_dimension"] == DIMENSION


# ======================================================================
# 10-12. Composition purity, source-driven behaviour, honest failure
# ======================================================================


def test_production_composition_imports_no_test_or_demo_module():
    """`src/` must never reach into tests, fixtures, demos or seed data."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline"
    forbidden = ("tests", "conftest", "fixture", "demo", "seed_", "sample_data")
    offenders = []

    for path in root.rglob("*.py"):
        if "__pycache__" in str(path):
            continue

        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))

        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]

            for name in names:
                lowered = name.lower()
                if any(marker in lowered for marker in forbidden):
                    offenders.append(f"{path.name}: {name}")

    assert offenders == [], f"production code imports test/demo modules: {offenders}"


def test_connector_choice_follows_the_registered_source_type():
    """Behaviour is selected from SourceType, never from a hardcoded name."""
    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.connectors.mongodb import MongoDBConnector
    from erp_pipeline.connectors.mysql import MySQLConnector
    from erp_pipeline.connectors.postgresql import PostgreSQLConnector
    from erp_pipeline.connectors.registry import ConnectorRegistry
    from erp_pipeline.connectors.sqlserver import SQLServerConnector

    expected = {
        SourceType.POSTGRESQL: PostgreSQLConnector,
        SourceType.MYSQL: MySQLConnector,
        SourceType.SQL_SERVER: SQLServerConnector,
        SourceType.MONGODB: MongoDBConnector,
    }

    for source_type, connector_class in expected.items():
        settings = ConnectionSettings(
            source_system_id="any_registered_source",
            source_type=source_type,
            host="db.invalid",
            port=1234,
            database="any_database",
            username="reader",
        )

        # Constructed from the registered type alone - no name is inspected.
        assert isinstance(ConnectorRegistry.create(settings), connector_class)


def test_a_transformation_without_a_source_system_fails_rather_than_inventing_one():
    """Was: `or "unknown_source"`.

    This value becomes `SourceReference.source_system_id` on every canonical
    record and the `source_system_id` key on every resulting Qdrant point. A
    stand-in would index real business rows under a source system that does
    not exist, and nothing downstream could tell that from the truth.
    """
    from erp_pipeline.orchestration.errors import InvalidPipelineRequestError

    services = PipelineServices()
    entity = employees_entity()

    class _SchemaWithoutSystem:
        source_system_id = None
        schema_id = None
        schema_version = None

    with pytest.raises(InvalidPipelineRequestError) as raised:
        services.transform_source_native(
            [_row("EMP-0001", "Kasun Fernando", "Engineering")],
            entity,
            _SchemaWithoutSystem(),
            source_id=None,
            source_type=SourceType.POSTGRESQL,
        )

    assert "source_system_id" in str(raised.value)
