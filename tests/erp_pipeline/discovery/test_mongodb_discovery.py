"""Collection discovery, bounded sampling and SourceSchema assembly.

Exercises ``discovery.mongodb`` against ``FakeMongoDatabase``, which returns
exactly the shapes pymongo returns. No MongoDB server is required; a real one
is used in ``test_live_mongodb_inference.py``.
"""

from __future__ import annotations

import pytest

from erp_pipeline.discovery.errors import (
    MetadataInspectionError,
    MongoInferenceError,
    UnsupportedDiscoverySourceError,
)
from erp_pipeline.discovery.models import MongoInferenceOptions
from erp_pipeline.discovery.mongodb import (
    SAMPLE_SORT_FIELD,
    MongoDBSchemaInference,
    infer_mongodb_schema,
)
from erp_pipeline.discovery.service import MongoDBInferenceService
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SchemaOrigin, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema

from tests.erp_pipeline.discovery.fakes import FakeInspector, FakeRelationalConnector
from tests.erp_pipeline.discovery.mongo_fakes import (
    FakeMongoCollection,
    FakeMongoConnector,
    FakeMongoDatabase,
    mongo_connector,
)

INVOICES = (
    {"_id": "1", "invoice": "INV1", "customer": {"id": 22}, "amount": 5000},
    {"_id": "2", "invoice": "INV2", "customer": {"id": 25, "name": "ABC"}, "amount": 9000},
)


# ============================================================
# Connector contract (Step 3)
# ============================================================

def test_a_relational_connector_is_rejected_clearly():
    connector = FakeRelationalConnector(FakeInspector(), SourceType.POSTGRESQL)

    with pytest.raises(UnsupportedDiscoverySourceError) as excinfo:
        infer_mongodb_schema(connector)

    assert "mongodb" in str(excinfo.value)
    assert "Phase 4" in str(excinfo.value)
    connector.close()


def test_a_non_connector_is_rejected():
    with pytest.raises(UnsupportedDiscoverySourceError):
        infer_mongodb_schema(object())


def test_inference_uses_the_connectors_database_handle_seam():
    connector = mongo_connector({"invoices": INVOICES})

    schema = infer_mongodb_schema(connector)

    assert schema.source_system_id == connector.source_system_id
    assert schema.metadata["database"] == "fake_mongo_db"


def test_a_failing_handle_becomes_a_metadata_inspection_error():
    connector = FakeMongoConnector(
        FakeMongoDatabase(), handle_error=RuntimeError("boom")
    )

    with pytest.raises(MetadataInspectionError):
        infer_mongodb_schema(connector)


def test_a_closed_connector_cannot_be_sampled():
    from erp_pipeline.connectors.errors import ConnectorClosedError

    connector = mongo_connector({"invoices": INVOICES})
    connector.close()

    with pytest.raises((ConnectorClosedError, MetadataInspectionError)):
        infer_mongodb_schema(connector)


def test_failing_collection_listing_becomes_a_metadata_inspection_error():
    connector = FakeMongoConnector(
        FakeMongoDatabase(failing_methods=["list_collections"])
    )

    with pytest.raises(MetadataInspectionError):
        infer_mongodb_schema(connector)


# ============================================================
# Collection discovery and filtering (Step 5)
# ============================================================

def test_each_collection_becomes_one_source_entity():
    connector = mongo_connector({"invoices": INVOICES, "customers": [{"_id": "c1"}]})

    schema = infer_mongodb_schema(connector)

    assert {entity.normalized_name for entity in schema.entities} == {
        "invoices", "customers",
    }
    assert all(entity.entity_kind is EntityKind.COLLECTION for entity in schema.entities)
    assert all(entity.namespace == "fake_mongo_db" for entity in schema.entities)


def test_system_collections_are_excluded_by_default():
    connector = mongo_connector(
        {"invoices": INVOICES, "system.views": [{"_id": "v"}], "system.js": [{"_id": "j"}]}
    )

    schema = infer_mongodb_schema(connector)

    assert [entity.source_name for entity in schema.entities] == ["invoices"]


def test_system_collections_can_be_opted_into():
    connector = mongo_connector({"invoices": INVOICES, "system.views": [{"_id": "v"}]})

    schema = infer_mongodb_schema(
        connector, MongoInferenceOptions(include_system_collections=True)
    )

    assert {entity.source_name for entity in schema.entities} == {
        "invoices", "system.views",
    }


def test_include_collections_filter():
    connector = mongo_connector(
        {"invoices": INVOICES, "customers": [{"_id": "c"}], "payments": [{"_id": "p"}]}
    )

    schema = infer_mongodb_schema(
        connector, MongoInferenceOptions(include_collections=["invoices", "payments"])
    )

    assert {entity.source_name for entity in schema.entities} == {"invoices", "payments"}


def test_exclude_collections_filter():
    connector = mongo_connector({"invoices": INVOICES, "audit_log": [{"_id": "a"}]})

    schema = infer_mongodb_schema(
        connector, MongoInferenceOptions(exclude_collections=["audit_log"])
    )

    assert [entity.source_name for entity in schema.entities] == ["invoices"]


def test_views_are_excluded_by_default_and_can_be_included():
    database = FakeMongoDatabase(
        collections={
            "invoices": FakeMongoCollection(INVOICES),
            "invoice_summary": FakeMongoCollection([{"_id": "s", "total": 1}]),
        },
        collection_types={"invoice_summary": "view"},
    )
    connector = FakeMongoConnector(database)

    default_schema = MongoDBSchemaInference(connector).infer()
    assert [entity.source_name for entity in default_schema.entities] == ["invoices"]

    with_views = MongoDBSchemaInference(
        connector, MongoInferenceOptions(include_views=True)
    ).infer()
    assert {entity.source_name for entity in with_views.entities} == {
        "invoices", "invoice_summary",
    }
    summary_entity = with_views.entity_by_normalized_name("invoice_summary")
    assert summary_entity.metadata["collection_type"] == "view"


def test_no_matching_collections_yields_an_empty_schema_with_a_warning():
    connector = mongo_connector({"invoices": INVOICES})
    inference = MongoDBSchemaInference(
        connector, MongoInferenceOptions(include_collections=["nothing_here"])
    )

    schema = inference.infer()

    assert schema.entities == ()
    assert any("No collections matched" in warning for warning in inference.warnings)


def test_an_empty_collection_becomes_an_entity_with_no_fields():
    connector = mongo_connector({"empty": []})

    entity = infer_mongodb_schema(connector).entities[0]

    assert entity.fields == ()
    assert entity.primary_key_fields == ()
    assert entity.metadata["sample"]["documents_sampled"] == 0


def test_entity_order_does_not_depend_on_server_enumeration_order():
    documents = {"zeta": [{"_id": "z"}], "alpha": [{"_id": "a"}], "mid": [{"_id": "m"}]}

    forward = FakeMongoDatabase(
        {name: FakeMongoCollection(docs) for name, docs in documents.items()},
        listing_order=["zeta", "alpha", "mid"],
    )
    backward = FakeMongoDatabase(
        {name: FakeMongoCollection(docs) for name, docs in documents.items()},
        listing_order=["mid", "zeta", "alpha"],
    )

    first = MongoDBSchemaInference(FakeMongoConnector(forward)).infer()
    second = MongoDBSchemaInference(FakeMongoConnector(backward)).infer()

    assert [e.source_name for e in first.entities] == ["alpha", "mid", "zeta"]
    assert first.compute_schema_hash() == second.compute_schema_hash()


def test_case_differing_collection_names_both_survive():
    connector = mongo_connector({"Orders": [{"_id": "1"}], "orders": [{"_id": "2"}]})

    schema = infer_mongodb_schema(connector)

    names = [entity.normalized_name for entity in schema.entities]
    assert len(set(names)) == 2
    assert {entity.source_name for entity in schema.entities} == {"Orders", "orders"}


def test_one_unreadable_collection_does_not_abort_the_others():
    database = FakeMongoDatabase(
        {
            "invoices": FakeMongoCollection(INVOICES),
            "broken": FakeMongoCollection([{"_id": "b"}], failing_methods=["find"]),
        }
    )
    inference = MongoDBSchemaInference(FakeMongoConnector(database))

    schema = inference.infer()

    assert [entity.source_name for entity in schema.entities] == ["invoices"]
    assert any("broken" in warning for warning in inference.warnings)


def test_sampling_failure_raises_a_mongo_inference_error():
    collection = FakeMongoCollection([{"_id": "b"}], failing_methods=["find"])
    database = FakeMongoDatabase({"broken": collection})
    inference = MongoDBSchemaInference(FakeMongoConnector(database))

    with pytest.raises(MongoInferenceError):
        inference._sample_documents(database, "broken", 10, [])


# ============================================================
# Deterministic, bounded sampling (Steps 15, 19)
# ============================================================

def test_sampling_sorts_by_id_and_applies_a_limit():
    collection = FakeMongoCollection(
        [{"_id": f"{index:03d}", "n": index} for index in range(50)]
    )
    connector = FakeMongoConnector(FakeMongoDatabase({"items": collection}))

    infer_mongodb_schema(connector, MongoInferenceOptions(max_documents_per_collection=10))

    call = collection.find_calls[0]
    assert call["sort"] == [(SAMPLE_SORT_FIELD, 1)]
    assert call["limit"] == 10
    assert call["filter"] == {}


def test_no_random_sampling_is_used():
    """A random sample would make two runs over an unchanged collection
    disagree, producing a spurious new catalog version each time."""
    collection = FakeMongoCollection(
        [{"_id": f"{index:03d}", f"key_{index}": index} for index in range(30)]
    )
    connector = FakeMongoConnector(FakeMongoDatabase({"items": collection}))
    options = MongoInferenceOptions(max_documents_per_collection=5)

    first = MongoDBSchemaInference(connector, options).infer()
    second = MongoDBSchemaInference(connector, options).infer()

    assert first.compute_schema_hash() == second.compute_schema_hash()
    # The first five _ids, every time.
    assert {field.source_name for field in first.entities[0].fields} == {
        "_id", "key_0", "key_1", "key_2", "key_3", "key_4",
    }


def test_document_cap_is_reported_without_claiming_full_coverage():
    collection = FakeMongoCollection(
        [{"_id": f"{index:05d}"} for index in range(2000)], estimated_count=50000
    )
    connector = FakeMongoConnector(FakeMongoDatabase({"items": collection}))

    entity = infer_mongodb_schema(
        connector, MongoInferenceOptions(max_documents_per_collection=500)
    ).entities[0]

    assert entity.metadata["sample"]["documents_sampled"] == 500
    assert entity.metadata["sample"]["full_scan"] is False
    assert entity.metadata["estimated_document_count"] == 50000
    assert "coverage" not in str(entity.metadata)


def test_total_document_budget_stops_later_collections():
    database = FakeMongoDatabase(
        {
            "a_first": FakeMongoCollection([{"_id": str(i), "x": i} for i in range(10)]),
            "b_second": FakeMongoCollection([{"_id": str(i), "y": i} for i in range(10)]),
        }
    )
    inference = MongoDBSchemaInference(
        FakeMongoConnector(database),
        MongoInferenceOptions(max_documents_per_collection=10, max_total_documents=10),
    )

    schema = inference.infer()
    second = schema.entity_by_normalized_name("b_second")

    assert second.fields == ()
    assert second.metadata["sample_budget_exhausted"] is True
    assert inference.summary().budget_exhausted is True
    assert inference.summary().total_documents_sampled == 10


def test_a_failing_sort_falls_back_to_natural_order_and_says_so():
    collection = FakeMongoCollection(list(INVOICES), fail_sorted_find=True)
    database = FakeMongoDatabase({"invoices": collection})
    inference = MongoDBSchemaInference(FakeMongoConnector(database))

    schema = inference.infer()
    entity = schema.entities[0]

    assert entity.metadata["sample"]["deterministic_sampling"] is False
    assert entity.metadata["sample"]["sort_field"] is None
    assert any("natural order" in warning for warning in inference.warnings)
    # Still produced a usable observed schema.
    assert entity.field_by_normalized_name("invoice") is not None


def test_deterministic_sampling_can_be_switched_off():
    collection = FakeMongoCollection(list(INVOICES))
    connector = FakeMongoConnector(FakeMongoDatabase({"invoices": collection}))

    infer_mongodb_schema(connector, MongoInferenceOptions(deterministic_sampling=False))

    assert collection.find_calls[0]["sort"] is None


def test_a_missing_count_estimate_never_fails_the_run():
    collection = FakeMongoCollection(
        list(INVOICES), failing_methods=["estimated_document_count"]
    )
    connector = FakeMongoConnector(FakeMongoDatabase({"invoices": collection}))

    entity = infer_mongodb_schema(connector).entities[0]

    assert entity.metadata["estimated_document_count"] is None
    assert entity.fields != ()


# ============================================================
# Collection validator metadata (Step 20)
# ============================================================

VALIDATOR_OPTIONS = {
    "validator": {"$jsonSchema": {"required": ["invoice"], "bsonType": "object"}},
    "validationLevel": "strict",
    "validationAction": "error",
}


def test_validator_presence_is_reported_without_claiming_it_was_parsed():
    database = FakeMongoDatabase(
        {"invoices": FakeMongoCollection(INVOICES)},
        collection_options={"invoices": VALIDATOR_OPTIONS},
    )

    entity = MongoDBSchemaInference(FakeMongoConnector(database)).infer().entities[0]

    assert entity.metadata["validator_present"] is True
    assert entity.metadata["validator_parsed"] is False
    assert entity.metadata["validation_level"] == "strict"
    assert entity.metadata["validation_action"] == "error"


def test_the_validator_body_itself_is_never_stored():
    """It can embed literal business values (allowed enum members, bounds)."""
    database = FakeMongoDatabase(
        {"invoices": FakeMongoCollection(INVOICES)},
        collection_options={"invoices": VALIDATOR_OPTIONS},
    )

    schema = MongoDBSchemaInference(FakeMongoConnector(database)).infer()

    assert "jsonSchema" not in str(schema.to_json_dict())
    assert "bsonType" not in str(schema.to_json_dict())


def test_a_validator_does_not_change_observed_requiredness():
    """`invoice` is required by the validator but missing from one sampled
    document. The observed answer stays "not always present"."""
    documents = [{"_id": "1", "invoice": "INV1"}, {"_id": "2"}]
    database = FakeMongoDatabase(
        {"invoices": FakeMongoCollection(documents)},
        collection_options={"invoices": VALIDATOR_OPTIONS},
    )

    entity = MongoDBSchemaInference(FakeMongoConnector(database)).infer().entities[0]

    assert entity.metadata["validator_present"] is True
    assert entity.field_by_normalized_name("invoice").required is False


def test_absent_validator_is_reported_as_absent():
    connector = mongo_connector({"invoices": INVOICES})

    entity = infer_mongodb_schema(connector).entities[0]

    assert entity.metadata["validator_present"] is False


def test_validator_reporting_can_be_switched_off():
    database = FakeMongoDatabase(
        {"invoices": FakeMongoCollection(INVOICES)},
        collection_options={"invoices": VALIDATOR_OPTIONS},
    )

    entity = MongoDBSchemaInference(
        FakeMongoConnector(database),
        MongoInferenceOptions(include_validator_presence=False),
    ).infer().entities[0]

    assert "validator_present" not in entity.metadata


# ============================================================
# SourceSchema output (Steps 21, 22, 23)
# ============================================================

def test_the_output_is_the_phase_1_contract_with_inferred_origin():
    schema = infer_mongodb_schema(mongo_connector({"invoices": INVOICES}))

    assert isinstance(schema, SourceSchema)
    assert schema.origin is SchemaOrigin.INFERRED
    assert all(isinstance(entity, SourceEntity) for entity in schema.entities)
    assert all(
        isinstance(field, SourceField)
        for entity in schema.entities
        for field in entity.fields
    )


def test_schema_metadata_states_that_the_result_is_observed():
    schema = infer_mongodb_schema(mongo_connector({"invoices": INVOICES}))

    assert schema.metadata["schema_claim"] == "observed"
    assert "not from a declared MongoDB schema" in schema.metadata["observed_schema_note"]
    assert schema.metadata["engine"] == "mongodb"
    assert schema.metadata["inference_options"]["max_documents_per_collection"] == 500


def test_no_relationships_are_ever_inferred():
    """Step 13/14: a customer_id field, or an ObjectId, is not evidence."""
    documents = [
        {"_id": "1", "customer_id": "c1", "user_id": 5, "invoice_id": "INV1"},
        {"_id": "2", "customer_id": "c2", "user_id": 6, "invoice_id": "INV2"},
    ]
    connector = mongo_connector({"invoices": documents, "customers": [{"_id": "c1"}]})

    schema = infer_mongodb_schema(connector)

    assert schema.relationships == ()
    assert schema.metadata["relationship_inference"] == "disabled"


def test_embedded_documents_are_nested_fields_not_fabricated_entities():
    connector = mongo_connector({"invoices": INVOICES})

    schema = infer_mongodb_schema(connector)

    assert len(schema.entities) == 1
    entity = schema.entities[0]
    assert entity.field_by_normalized_name("customer").normalized_data_type is (
        FieldDataType.OBJECT
    )
    assert entity.field_by_normalized_name("customer.id") is not None


def test_identity_is_deterministic_and_content_addressed():
    connector = mongo_connector({"invoices": INVOICES})

    first = infer_mongodb_schema(connector)
    second = infer_mongodb_schema(connector)

    assert first.schema_id == second.schema_id
    assert first.schema_hash == second.schema_hash
    assert first.compute_schema_hash() == second.compute_schema_hash()
    assert first.schema_hash[:12] in first.schema_id


def test_identity_carries_no_timestamp_or_random_component():
    connector = mongo_connector({"invoices": INVOICES})

    schema = infer_mongodb_schema(connector)

    assert schema.schema_id.startswith("fake_mongo.fake_mongo_db.")
    assert schema.discovered_at is None
    assert schema.entities[0].entity_id == "fake_mongo.fake_mongo_db.invoices"


def test_schema_name_is_the_stable_scope_not_the_content():
    connector_v1 = mongo_connector({"invoices": INVOICES})
    connector_v2 = mongo_connector(
        {"invoices": list(INVOICES) + [{"_id": "3", "approved": True}]}
    )

    v1 = infer_mongodb_schema(connector_v1)
    v2 = infer_mongodb_schema(connector_v2)

    assert v1.schema_name == v2.schema_name == "fake_mongo_db"
    assert v1.schema_id != v2.schema_id


def test_narrowing_the_scope_changes_the_schema_name():
    connector = mongo_connector({"invoices": INVOICES, "customers": [{"_id": "c"}]})

    scoped = infer_mongodb_schema(
        connector, MongoInferenceOptions(include_collections=["invoices"])
    )

    assert scoped.schema_name == "invoices"


def test_sample_size_alone_does_not_change_the_structural_hash():
    """Raising the budget over a collection whose structure is uniform must
    not look like a schema change - the counts live in unhashed metadata."""
    documents = [{"_id": f"{i:03d}", "a": i, "b": "x"} for i in range(20)]
    connector = mongo_connector({"items": documents})

    small = infer_mongodb_schema(connector, MongoInferenceOptions(max_documents_per_collection=5))
    large = infer_mongodb_schema(connector, MongoInferenceOptions(max_documents_per_collection=20))

    assert small.compute_schema_hash() == large.compute_schema_hash()
    assert small.entities[0].metadata["sample"]["documents_sampled"] == 5
    assert large.entities[0].metadata["sample"]["documents_sampled"] == 20


@pytest.mark.parametrize(
    "changed_documents",
    [
        pytest.param(
            [{"_id": "1", "invoice": "INV1", "customer": {"id": 22}, "amount": 5000,
              "approved": True}],
            id="new_field",
        ),
        pytest.param(
            [{"_id": "1", "invoice": "INV1", "customer": {"id": "22"}, "amount": 5000}],
            id="type_change",
        ),
        pytest.param(
            [{"_id": "1", "invoice": "INV1", "customer": {"id": 22, "tier": "A"},
              "amount": 5000}],
            id="nested_structure_change",
        ),
        pytest.param(
            [{"_id": "1", "invoice": "INV1", "customer": [{"id": 22}], "amount": 5000}],
            id="object_becomes_array",
        ),
        pytest.param(
            [{"_id": "1", "invoice": "INV1", "amount": 5000}],
            id="removed_field",
        ),
    ],
)
def test_genuine_structural_changes_change_the_hash(changed_documents):
    baseline = infer_mongodb_schema(
        mongo_connector({"invoices": [INVOICES[0]]})
    ).compute_schema_hash()

    changed = infer_mongodb_schema(
        mongo_connector({"invoices": changed_documents})
    ).compute_schema_hash()

    assert changed != baseline


# ============================================================
# Summary and service (Steps 24, 25)
# ============================================================

def test_summary_reports_aggregate_evidence_only():
    connector = mongo_connector({"invoices": INVOICES, "customers": [{"_id": "c"}]})
    inference = MongoDBSchemaInference(connector)
    inference.infer()

    summary = inference.summary()

    assert summary.database == "fake_mongo_db"
    assert summary.collections_discovered == 2
    assert summary.collections_inferred == 2
    assert summary.total_documents_sampled == 3
    assert summary.partial is False
    invoices = next(c for c in summary.collections if c.collection_name == "invoices")
    assert invoices.documents_sampled == 2
    assert invoices.field_path_count == 6


def test_the_service_returns_the_schema_plus_supplemental_evidence():
    connector = mongo_connector({"invoices": INVOICES})

    result = MongoDBInferenceService().infer(connector)

    assert isinstance(result.schema, SourceSchema)
    assert result.inference.total_documents_sampled == 2
    assert result.schema_hash == result.schema.compute_schema_hash()
    assert result.to_dict()["inference"]["collections"][0]["documents_sampled"] == 2


def test_supplemental_evidence_is_not_embedded_in_the_schema():
    """Keeping it out is what lets the structural hash ignore sample size."""
    connector = mongo_connector({"invoices": INVOICES})

    result = MongoDBInferenceService().infer(connector)

    assert "inference" not in result.schema.to_json_dict()
    assert "observations" not in str(result.schema.metadata)


def test_options_reject_a_nonsensical_budget():
    with pytest.raises(ValueError):
        MongoInferenceOptions(max_documents_per_collection=0)

    with pytest.raises(ValueError):
        MongoInferenceOptions(max_depth=-1)
