"""MongoDB inference -> Phase 2 Schema Catalog integration.

Proves the inferred SourceSchema round-trips through the EXISTING catalog
unchanged, that re-inferring an unchanged collection does not create a new
catalog version, and that a controlled change to the observed structure does -
with SchemaDiff naming the actual new fields.

Phase 5 owns none of that versioning logic; these tests verify the handoff,
exactly as the Phase 4 equivalents do for relational discovery.

The MongoDB side uses the in-memory fakes so the observed structure can be
changed deterministically; the CATALOG side is the real PostgreSQL-backed
catalog. Steps 26-28 are additionally re-proved against a live MongoDB server
in ``test_live_mongodb_inference.py``.
"""

from __future__ import annotations

import pytest

from erp_pipeline.catalog.repository import CatalogRepository
from erp_pipeline.catalog.schema import bootstrap_catalog
from erp_pipeline.catalog.service import SchemaCatalogService
from erp_pipeline.catalog.versioning import compare_schemas
from erp_pipeline.discovery.mongodb import infer_mongodb_schema
from erp_pipeline.discovery.service import MongoDBInferenceService
from erp_pipeline.schemas.enums import SchemaOrigin, SourceType
from erp_pipeline.schemas.source_models import SourceSystem

from tests.erp_pipeline.discovery.mongo_fakes import mongo_connector

SOURCE_SYSTEM_ID = "fake_mongo"

V1_DOCUMENTS = (
    {"_id": "1", "invoice": "INV1", "customer": {"id": 22}, "amount": 5000},
    {"_id": "2", "invoice": "INV2", "customer": {"id": 25}, "amount": 9000},
)

#: The controlled change: one document adding a nested field and a new
#: top-level field. Nothing else differs.
V2_DOCUMENTS = V1_DOCUMENTS + (
    {
        "_id": "3",
        "invoice": "INV3",
        "customer": {"id": 30, "name": "ABC"},
        "amount": 6000,
        "approved": True,
    },
)


@pytest.fixture()
def catalog(pipeline_connector):
    """A catalog service backed by the pipeline database."""
    engine = pipeline_connector._sqlalchemy_engine  # noqa: SLF001 - test setup
    bootstrap_catalog(engine)
    return SchemaCatalogService(CatalogRepository(engine))


@pytest.fixture()
def registered_mongo_source(catalog):
    from sqlalchemy import text

    source_system = SourceSystem(
        source_system_id=SOURCE_SYSTEM_ID,
        name="Phase 5 inference probe source",
        source_type=SourceType.MONGODB,
        environment="research",
    )
    catalog.register_source_system(source_system)

    yield source_system

    # Leave the catalog exactly as found.
    engine = catalog.repository._engine  # noqa: SLF001 - test cleanup
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                DELETE FROM erp_catalog.source_fields WHERE schema_id IN (
                    SELECT schema_id FROM erp_catalog.schema_snapshots
                    WHERE source_system_id = :sid)
                """
            ),
            {"sid": SOURCE_SYSTEM_ID},
        )
        for table in ("source_entities", "source_relationships"):
            connection.execute(
                text(
                    f"""
                    DELETE FROM erp_catalog.{table} WHERE schema_id IN (
                        SELECT schema_id FROM erp_catalog.schema_snapshots
                        WHERE source_system_id = :sid)
                    """
                ),
                {"sid": SOURCE_SYSTEM_ID},
            )
        connection.execute(
            text(
                "DELETE FROM erp_catalog.schema_snapshots "
                "WHERE source_system_id = :sid"
            ),
            {"sid": SOURCE_SYSTEM_ID},
        )
        connection.execute(
            text("DELETE FROM erp_catalog.source_systems WHERE source_system_id = :sid"),
            {"sid": SOURCE_SYSTEM_ID},
        )


def infer(documents):
    return infer_mongodb_schema(mongo_connector({"invoices": documents}))


# ============================================================
# Round trip (Step 25)
# ============================================================

def test_inferred_schema_round_trips_through_the_catalog(catalog, registered_mongo_source):
    inferred = infer(V1_DOCUMENTS)

    catalog.publish_schema(inferred)
    retrieved = catalog.get_snapshot(inferred.schema_id)

    inferred_payload = inferred.to_json_dict()
    retrieved_payload = retrieved.to_json_dict()
    # created_at is object-construction time, not structural identity.
    inferred_payload.pop("created_at")
    retrieved_payload.pop("created_at")

    assert retrieved_payload == inferred_payload
    assert retrieved.compute_schema_hash() == inferred.compute_schema_hash()


def test_the_inferred_origin_survives_the_round_trip(catalog, registered_mongo_source):
    """A stored snapshot must keep saying it was inferred, not discovered."""
    inferred = infer(V1_DOCUMENTS)
    catalog.publish_schema(inferred)

    retrieved = catalog.get_snapshot(inferred.schema_id)

    assert retrieved.origin is SchemaOrigin.INFERRED
    assert retrieved.metadata["schema_claim"] == "observed"


def test_nested_paths_and_array_flags_survive_the_round_trip(catalog, registered_mongo_source):
    documents = [{"_id": "1", "customer": {"id": 1}, "items": [{"sku": "A"}], "tags": ["x"]}]
    inferred = infer(documents)
    catalog.publish_schema(inferred)

    entity = catalog.get_snapshot(inferred.schema_id).entity_by_normalized_name("invoices")

    assert entity.field_by_normalized_name("customer.id").nested_path == ("customer",)
    assert entity.field_by_normalized_name("items_.sku").nested_path == ("items", "[]")
    assert entity.field_by_normalized_name("tags").is_array is True


# ============================================================
# Idempotent re-inference (Step 26)
# ============================================================

def test_reinferring_unchanged_documents_does_not_create_a_new_version(
    catalog, registered_mongo_source
):
    first_schema = infer(V1_DOCUMENTS)
    first_result = catalog.publish_schema(first_schema)

    second_schema = infer(V1_DOCUMENTS)
    second_result = catalog.publish_schema(second_schema)

    assert first_schema.compute_schema_hash() == second_schema.compute_schema_hash()
    assert first_schema.schema_id == second_schema.schema_id

    assert first_result.created is True
    assert first_result.record.catalog_version == 1
    assert second_result.created is False
    assert second_result.record.catalog_version == 1


# ============================================================
# Controlled observed-structure change (Step 27)
# ============================================================

def test_new_observed_fields_create_catalog_version_two(catalog, registered_mongo_source):
    v1 = infer(V1_DOCUMENTS)
    v1_result = catalog.publish_schema(v1)

    v2 = infer(V2_DOCUMENTS)
    v2_result = catalog.publish_schema(v2)

    assert v1.compute_schema_hash() != v2.compute_schema_hash()
    assert v1.schema_id != v2.schema_id
    assert v1.schema_name == v2.schema_name  # same logical scope

    assert v1_result.record.catalog_version == 1
    assert v2_result.created is True
    assert v2_result.record.catalog_version == 2


def test_schema_diff_reports_the_actual_new_fields(catalog, registered_mongo_source):
    v1 = infer(V1_DOCUMENTS)
    v2 = infer(V2_DOCUMENTS)
    catalog.publish_schema(v1)
    catalog.publish_schema(v2)

    diff = compare_schemas(v1, v2)

    assert set(diff.added_fields) == {
        ("invoices", "customer.name"),
        ("invoices", "approved"),
    }
    assert diff.removed_fields == ()
    assert diff.added_entities == ()
    assert diff.removed_entities == ()
    assert diff.added_relationships == ()


def test_diff_computed_from_persisted_snapshots_agrees(catalog, registered_mongo_source):
    v1 = infer(V1_DOCUMENTS)
    v2 = infer(V2_DOCUMENTS)
    catalog.publish_schema(v1)
    catalog.publish_schema(v2)

    diff = catalog.compare_versions(v1.schema_id, v2.schema_id)

    assert set(diff.added_fields) == {
        ("invoices", "customer.name"),
        ("invoices", "approved"),
    }


def test_a_new_collection_appears_as_an_added_entity(catalog, registered_mongo_source):
    v1 = infer_mongodb_schema(mongo_connector({"invoices": V1_DOCUMENTS}))
    v2 = infer_mongodb_schema(
        mongo_connector({"invoices": V1_DOCUMENTS, "payments": [{"_id": "p1", "paid": 1}]})
    )

    catalog.publish_schema(v1)
    v2_result = catalog.publish_schema(v2)

    assert v2_result.record.catalog_version == 2
    assert compare_schemas(v1, v2).added_entities == ("payments",)


def test_history_lists_both_versions_in_order(catalog, registered_mongo_source):
    catalog.publish_schema(infer(V1_DOCUMENTS))
    catalog.publish_schema(infer(V2_DOCUMENTS))

    history = catalog.history(SOURCE_SYSTEM_ID, "fake_mongo_db")

    assert [record.catalog_version for record in history] == [1, 2]


def test_infer_and_publish_service_helper(catalog, registered_mongo_source):
    connector = mongo_connector({"invoices": V1_DOCUMENTS})

    result, snapshot = MongoDBInferenceService().infer_and_publish(connector, catalog)

    assert snapshot.record.catalog_version == 1
    assert result.schema.schema_id == snapshot.record.schema_id
    assert result.inference.total_documents_sampled == 2


def test_catalog_summary_reflects_the_inferred_counts(catalog, registered_mongo_source):
    inferred = infer(V1_DOCUMENTS)
    catalog.publish_schema(inferred)

    summary = catalog.summarize(inferred.schema_id)

    assert summary.entity_count == 1
    # _id, invoice, customer, customer.id, amount
    assert summary.field_count == 5
    assert summary.relationship_count == 0
    assert summary.catalog_version == 1
    assert summary.source_type == "mongodb"


def test_a_larger_sample_of_identical_structure_publishes_no_new_version(
    catalog, registered_mongo_source
):
    """The sample size lives in unhashed metadata, so widening the budget over
    a structurally uniform collection must not manufacture a version."""
    from erp_pipeline.discovery.models import MongoInferenceOptions

    documents = [{"_id": f"{i:03d}", "invoice": f"INV{i}", "amount": i} for i in range(20)]
    connector = mongo_connector({"invoices": documents})

    small = infer_mongodb_schema(
        connector, MongoInferenceOptions(max_documents_per_collection=5)
    )
    large = infer_mongodb_schema(
        connector, MongoInferenceOptions(max_documents_per_collection=20)
    )

    first = catalog.publish_schema(small)
    second = catalog.publish_schema(large)

    assert first.record.catalog_version == 1
    assert second.created is False
    assert second.record.catalog_version == 1
