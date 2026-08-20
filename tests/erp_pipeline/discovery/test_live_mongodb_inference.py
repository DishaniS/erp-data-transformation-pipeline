"""LIVE MongoDB verification for Phase 5.

Runs the real production inference path against a real MongoDB server:

    MongoDBConnector -> create_database_handle() -> list_collections/find
    -> observed SourceSchema -> Phase 2 catalog

Isolation. Everything happens inside one throwaway database
(``erp_phase5_test`` by default) seeded by these fixtures. No pre-existing
MongoDB database is read or written.

Privilege separation (Step 32). The FIXTURE seeds data with an administrative
account. DISCOVERY itself runs as ``phase5_reader``, which holds only the
``read`` role on the test database - so a write from the production code path
is impossible at the server, not merely absent from the source. One test
proves that account really cannot write.

Skips - never fails, never fakes - when MongoDB is unreachable or unconfigured.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from erp_pipeline.discovery.models import MongoInferenceOptions
from erp_pipeline.discovery.mongodb import MongoDBSchemaInference, infer_mongodb_schema
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SchemaOrigin, SourceType

TEST_DATABASE = "erp_phase5_test"
SOURCE_SYSTEM_ID = "mongo_phase5"

#: A collection used ONLY by the schema-change test, kept out of the main
#: scope so the drift proof cannot disturb the other assertions.
DRIFT_COLLECTION = "drift_probe"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(_project_root() / ".env")


def _connection_settings(username_env: str, password_env: str, auth_database: str):
    from erp_pipeline.connectors.config import ConnectionSettings

    password = os.getenv(password_env)
    if not password:
        pytest.skip(f"{password_env} is not configured in .env")

    return ConnectionSettings(
        source_system_id=SOURCE_SYSTEM_ID,
        source_type=SourceType.MONGODB,
        host=os.getenv("MONGO_PHASE5_HOST", "localhost"),
        port=int(os.getenv("MONGO_PHASE5_PORT", "27018")),
        database=os.getenv("MONGO_PHASE5_DB", TEST_DATABASE),
        username=os.getenv(username_env),
        password=password,
        auth_database=auth_database,
        connect_timeout_seconds=10,
    )


# ============================================================
# Deterministic synthetic ERP-like documents (Step 31)
# ============================================================

def _seed_documents():
    """Build the test corpus.

    Fixed ObjectIds and fixed timestamps, so the ``_id``-sorted sample is the
    same set of documents on every run and the resulting schema hash is
    reproducible across machines.
    """
    from bson import Binary, Decimal128, ObjectId

    def oid(suffix: str) -> ObjectId:
        return ObjectId(f"6500000000000000000000{suffix}")

    issued = dt.datetime(2026, 1, 15, 9, 30, tzinfo=dt.timezone.utc)

    customers = [
        {
            "_id": oid("01"),
            "code": "CUST-001",
            "name": "Northwind Supplies",
            "contact": {"email": "ops@northwind.invalid", "phone": "+94110000001"},
            "active": True,
            "created_at": issued,
        },
        {
            "_id": oid("02"),
            "code": "CUST-002",
            "name": "Vector Logistics",
            # `contact.phone` missing, `contact` itself present -> partial
            # presence at a nested path.
            "contact": {"email": "hello@vector.invalid"},
            "active": False,
            "created_at": issued,
            # Present in only one document -> observed optional.
            "credit_limit": Decimal128("25000.50"),
        },
    ]

    invoices = [
        {
            "_id": oid("11"),
            "invoice_no": "INV-1001",
            "customer_id": oid("01"),
            "customer": {"id": 22, "code": "CUST-001"},
            "amount": Decimal128("5000.00"),
            "currency": "LKR",
            "issued_at": issued,
            "lines": [
                {"sku": "SKU-A", "qty": 2, "unit_price": Decimal128("1500.00")},
                {"sku": "SKU-B", "qty": 1, "unit_price": Decimal128("2000.00")},
            ],
            "tags": ["urgent", "approved"],
            "note": None,
        },
        {
            "_id": oid("12"),
            "invoice_no": "INV-1002",
            "customer_id": oid("02"),
            "customer": {"id": 25, "code": "CUST-002", "name": "Vector Logistics"},
            "amount": Decimal128("9000.00"),
            "currency": "LKR",
            "issued_at": issued,
            "lines": [{"sku": "SKU-C", "qty": 4, "unit_price": Decimal128("500.00")}],
            "tags": [],
            "approved": True,
            "attachment": Binary(b"\x89PNG\r\n\x1a\n"),
        },
    ]

    payments = [
        # `reference` is an int here and a string there: a genuine mixed type.
        {"_id": oid("21"), "invoice_no": "INV-1001", "reference": 900001, "paid": True},
        {"_id": oid("22"), "invoice_no": "INV-1002", "reference": "900002", "paid": True},
        {"_id": oid("23"), "invoice_no": "INV-1002", "reference": 900003.5, "paid": False},
    ]

    purchase_orders = [
        {
            "_id": oid("31"),
            "po_no": "PO-77",
            "supplier": {"code": "SUP-1", "address": {"city": "Colombo"}},
            "expected_at": issued,
            "quantities": [1, 2, 3],
        }
    ]

    drift = [
        {"_id": oid("41"), "invoice": "INV1", "customer": {"id": 22}, "amount": 5000},
        {"_id": oid("42"), "invoice": "INV2", "customer": {"id": 25}, "amount": 9000},
    ]

    return {
        "customers": customers,
        "invoices": invoices,
        "payments": payments,
        "purchase_orders": purchase_orders,
        DRIFT_COLLECTION: drift,
    }


@pytest.fixture(scope="session")
def seeded_mongodb():
    """Seed the isolated test database, then drop it.

    This fixture is the ONLY place in the Phase 5 test suite that writes to
    MongoDB, and it writes exclusively to ``erp_phase5_test``.
    """
    _load_env()

    pytest.importorskip("pymongo", reason="pymongo is not installed")
    from pymongo import MongoClient
    from pymongo.errors import OperationFailure, PyMongoError

    password = os.getenv("MONGO_PHASE5_ADMIN_PASSWORD")
    if not password:
        pytest.skip("MONGO_PHASE5_ADMIN_PASSWORD is not configured in .env")

    host = os.getenv("MONGO_PHASE5_HOST", "localhost")
    port = int(os.getenv("MONGO_PHASE5_PORT", "27018"))
    database_name = os.getenv("MONGO_PHASE5_DB", TEST_DATABASE)

    client = MongoClient(
        host=host,
        port=port,
        username=os.getenv("MONGO_PHASE5_ADMIN_USER", "phase5_admin"),
        password=password,
        authSource=os.getenv("MONGO_PHASE5_AUTH_DB", "admin"),
        serverSelectionTimeoutMS=5000,
    )

    try:
        client.admin.command("ping")
    except PyMongoError as exc:  # noqa: BLE001 - availability probe
        client.close()
        pytest.skip(f"Phase 5 MongoDB unreachable at {host}:{port}: {exc}")

    if database_name != TEST_DATABASE and not database_name.startswith("erp_phase5"):
        client.close()
        pytest.skip(
            f"Refusing to seed {database_name!r}: Phase 5 fixtures only ever "
            "write to an isolated erp_phase5* database."
        )

    database = client[database_name]
    database.client.drop_database(database_name)

    for collection_name, documents in _seed_documents().items():
        database[collection_name].insert_many(documents)

    # A collection carrying a real validator, so validator PRESENCE detection
    # is verified against MongoDB itself rather than against a fake.
    database.create_collection(
        "validated_ledger",
        validator={"$jsonSchema": {"bsonType": "object", "required": ["entry_no"]}},
        validationLevel="strict",
        validationAction="error",
    )
    database["validated_ledger"].insert_many(
        [{"entry_no": "L-1", "debit": 10}, {"entry_no": "L-2", "debit": 20}]
    )

    # Read-only account for the discovery runs themselves (Step 32).
    reader = os.getenv("MONGO_PHASE5_READONLY_USER", "phase5_reader")
    reader_password = os.getenv("MONGO_PHASE5_READONLY_PASSWORD")
    if reader_password:
        try:
            database.command(
                "createUser",
                reader,
                pwd=reader_password,
                roles=[{"role": "read", "db": database_name}],
            )
        except OperationFailure as exc:
            # 51003 / "already exists" - the account survived a previous run.
            if "already exists" not in str(exc):
                raise

    yield database_name

    client.drop_database(database_name)
    client.close()


@pytest.fixture()
def mongo_connector_live(seeded_mongodb):
    """Connector used for DISCOVERY: read-only credentials where available."""
    from erp_pipeline.connectors.mongodb import MongoDBConnector

    if os.getenv("MONGO_PHASE5_READONLY_PASSWORD"):
        settings = _connection_settings(
            "MONGO_PHASE5_READONLY_USER",
            "MONGO_PHASE5_READONLY_PASSWORD",
            auth_database=seeded_mongodb,
        )
    else:  # pragma: no cover - only when no read-only account is configured
        settings = _connection_settings(
            "MONGO_PHASE5_ADMIN_USER",
            "MONGO_PHASE5_ADMIN_PASSWORD",
            auth_database=os.getenv("MONGO_PHASE5_AUTH_DB", "admin"),
        )

    connector = MongoDBConnector(settings)
    try:
        connector.test_connection()
    except Exception as exc:  # noqa: BLE001 - availability probe
        connector.close()
        pytest.skip(f"Phase 5 MongoDB unreachable: {exc}")

    yield connector
    connector.close()


@pytest.fixture()
def live_schema(mongo_connector_live):
    return infer_mongodb_schema(
        mongo_connector_live,
        MongoInferenceOptions(exclude_collections=[DRIFT_COLLECTION]),
    )


# ============================================================
# Connectivity and collection discovery
# ============================================================

def test_live_connection_and_capabilities(mongo_connector_live):
    result = mongo_connector_live.test_connection()

    assert result.success is True
    assert result.server_version
    assert mongo_connector_live.get_capabilities().document_database is True


def test_live_collections_are_discovered(live_schema):
    names = {entity.source_name for entity in live_schema.entities}

    assert names == {
        "customers", "invoices", "payments", "purchase_orders", "validated_ledger",
    }
    assert all(
        entity.entity_kind is EntityKind.COLLECTION for entity in live_schema.entities
    )
    assert all(entity.namespace == TEST_DATABASE for entity in live_schema.entities)


def test_live_schema_is_marked_inferred(live_schema):
    assert live_schema.origin is SchemaOrigin.INFERRED
    assert live_schema.metadata["schema_claim"] == "observed"
    assert live_schema.metadata["engine"] == "mongodb"


def test_live_system_collections_are_excluded(live_schema):
    assert not any(
        entity.source_name.startswith("system.") for entity in live_schema.entities
    )


# ============================================================
# Observed structure against real BSON
# ============================================================

def test_live_objectid_is_the_primary_key(live_schema):
    invoices = live_schema.entity_by_normalized_name("invoices")
    identifier = invoices.fields[0]

    assert identifier.source_name == "_id"
    assert identifier.source_data_type == "objectId"
    assert identifier.normalized_data_type is FieldDataType.STRING
    assert identifier.is_primary_key is True
    assert invoices.primary_key_fields == ("id",)


def test_live_nested_documents_are_inferred(live_schema):
    customers = live_schema.entity_by_normalized_name("customers")

    email = customers.field_by_normalized_name("contact.email")
    assert email.nested_path == ("contact",)
    assert email.normalized_data_type is FieldDataType.STRING
    assert email.required is True

    phone = customers.field_by_normalized_name("contact.phone")
    assert phone.metadata["observed"]["presence_ratio"] == 0.5
    assert phone.required is False


def test_live_deeply_nested_path_is_inferred(live_schema):
    orders = live_schema.entity_by_normalized_name("purchase_orders")

    city = orders.field_by_normalized_name("supplier.address.city")
    assert city.nested_path == ("supplier", "address")


def test_live_arrays_of_objects_and_primitives(live_schema):
    invoices = live_schema.entity_by_normalized_name("invoices")

    lines = invoices.field_by_normalized_name("lines")
    assert lines.is_array is True
    assert lines.source_data_type == "array<object>"

    sku = invoices.field_by_normalized_name("lines_.sku")
    assert sku.nested_path == ("lines", "[]")
    assert sku.normalized_data_type is FieldDataType.STRING
    assert sku.metadata["field_path"] == "lines[].sku"

    assert invoices.field_by_normalized_name("lines_.qty").normalized_data_type is (
        FieldDataType.INTEGER
    )

    quantities = live_schema.entity_by_normalized_name(
        "purchase_orders"
    ).field_by_normalized_name("quantities")
    assert quantities.source_data_type == "array<int>"


def test_live_decimal128_datetime_and_binary(live_schema):
    invoices = live_schema.entity_by_normalized_name("invoices")

    amount = invoices.field_by_normalized_name("amount")
    assert amount.source_data_type == "decimal"
    assert amount.normalized_data_type is FieldDataType.DECIMAL

    issued_at = invoices.field_by_normalized_name("issued_at")
    assert issued_at.normalized_data_type is FieldDataType.DATETIME

    attachment = invoices.field_by_normalized_name("attachment")
    assert attachment.source_data_type == "binData"
    assert attachment.normalized_data_type is FieldDataType.BINARY


def test_live_mixed_types_are_preserved_not_collapsed(live_schema):
    reference = live_schema.entity_by_normalized_name(
        "payments"
    ).field_by_normalized_name("reference")

    assert reference.normalized_data_type is FieldDataType.UNKNOWN
    assert reference.metadata["mixed_types"] is True
    assert set(reference.metadata["bson_type_distribution"]) == {
        "int", "string", "double",
    }


def test_live_null_and_optional_fields(live_schema):
    invoices = live_schema.entity_by_normalized_name("invoices")

    note = invoices.field_by_normalized_name("note")
    assert note.metadata["observed"]["null_count"] == 1
    assert note.required is False

    approved = invoices.field_by_normalized_name("approved")
    assert approved.normalized_data_type is FieldDataType.BOOLEAN
    assert approved.metadata["observed"]["presence_ratio"] == 0.5


def test_live_empty_array_is_still_an_array(live_schema):
    tags = live_schema.entity_by_normalized_name("invoices").field_by_normalized_name(
        "tags"
    )

    assert tags.is_array is True
    assert tags.source_data_type.startswith("array<")


def test_live_objectid_reference_does_not_become_a_relationship(live_schema):
    """`customer_id` holds a real ObjectId pointing at the customers
    collection. Without a declared constraint that is still not evidence."""
    customer_id = live_schema.entity_by_normalized_name(
        "invoices"
    ).field_by_normalized_name("customer_id")

    assert customer_id.source_data_type == "objectId"
    assert live_schema.relationships == ()


# ============================================================
# Validator presence, sampling, privacy
# ============================================================

def test_live_validator_presence_is_detected(live_schema):
    ledger = live_schema.entity_by_normalized_name("validated_ledger")

    assert ledger.metadata["validator_present"] is True
    assert ledger.metadata["validator_parsed"] is False
    assert ledger.metadata["validation_level"] == "strict"
    assert ledger.metadata["validation_action"] == "error"

    assert live_schema.entity_by_normalized_name("invoices").metadata[
        "validator_present"
    ] is False


def test_live_sampling_is_bounded_and_reports_its_size(mongo_connector_live):
    schema = infer_mongodb_schema(
        mongo_connector_live, MongoInferenceOptions(max_documents_per_collection=1)
    )

    invoices = schema.entity_by_normalized_name("invoices")
    assert invoices.metadata["sample"]["documents_sampled"] == 1
    assert invoices.metadata["sample"]["full_scan"] is False
    assert invoices.metadata["estimated_document_count"] == 2


def test_live_sampling_is_deterministic(mongo_connector_live):
    options = MongoInferenceOptions(max_documents_per_collection=1)

    first = infer_mongodb_schema(mongo_connector_live, options)
    second = infer_mongodb_schema(mongo_connector_live, options)

    assert first.schema_id == second.schema_id
    assert first.compute_schema_hash() == second.compute_schema_hash()


def test_live_reinference_is_idempotent(mongo_connector_live, live_schema):
    again = infer_mongodb_schema(
        mongo_connector_live,
        MongoInferenceOptions(exclude_collections=[DRIFT_COLLECTION]),
    )

    assert again.compute_schema_hash() == live_schema.compute_schema_hash()
    assert again.schema_id == live_schema.schema_id


def test_live_output_leaks_no_document_values(live_schema):
    import json

    payload = json.dumps(live_schema.to_json_dict(), default=str)

    for value in (
        "Northwind Supplies", "ops@northwind.invalid", "INV-1001", "SKU-A",
        "CUST-001", "Colombo", "900002", "25000.50", "urgent",
    ):
        assert value not in payload, f"leaked sampled value: {value!r}"


def test_live_summary_reports_aggregate_evidence(mongo_connector_live):
    inference = MongoDBSchemaInference(
        mongo_connector_live,
        MongoInferenceOptions(exclude_collections=[DRIFT_COLLECTION]),
    )
    inference.infer()

    summary = inference.summary()

    assert summary.database == TEST_DATABASE
    assert summary.collections_inferred == 5
    # 2 customers + 2 invoices + 3 payments + 1 purchase order + 2 ledger rows
    assert summary.total_documents_sampled == 10
    assert summary.partial is False


# ============================================================
# Read-only enforcement (Steps 32, 33)
# ============================================================

def test_the_discovery_account_cannot_write(seeded_mongodb):
    """The strongest possible read-only evidence: the server itself refuses."""
    if not os.getenv("MONGO_PHASE5_READONLY_PASSWORD"):
        pytest.skip("No read-only MongoDB account is configured")

    from pymongo import MongoClient
    from pymongo.errors import OperationFailure

    client = MongoClient(
        host=os.getenv("MONGO_PHASE5_HOST", "localhost"),
        port=int(os.getenv("MONGO_PHASE5_PORT", "27018")),
        username=os.getenv("MONGO_PHASE5_READONLY_USER", "phase5_reader"),
        password=os.getenv("MONGO_PHASE5_READONLY_PASSWORD"),
        authSource=seeded_mongodb,
        serverSelectionTimeoutMS=5000,
    )

    try:
        with pytest.raises(OperationFailure):
            client[seeded_mongodb]["invoices"].insert_one({"_id": "should-not-exist"})
    finally:
        client.close()


def test_inference_did_not_modify_the_source(seeded_mongodb, live_schema):
    """Document counts are unchanged after a full inference run."""
    if not os.getenv("MONGO_PHASE5_ADMIN_PASSWORD"):
        pytest.skip("No administrative MongoDB account is configured")

    from pymongo import MongoClient

    client = MongoClient(
        host=os.getenv("MONGO_PHASE5_HOST", "localhost"),
        port=int(os.getenv("MONGO_PHASE5_PORT", "27018")),
        username=os.getenv("MONGO_PHASE5_ADMIN_USER", "phase5_admin"),
        password=os.getenv("MONGO_PHASE5_ADMIN_PASSWORD"),
        authSource=os.getenv("MONGO_PHASE5_AUTH_DB", "admin"),
        serverSelectionTimeoutMS=5000,
    )

    try:
        database = client[seeded_mongodb]
        assert database["invoices"].count_documents({}) == 2
        assert database["payments"].count_documents({}) == 3
        assert set(database.list_collection_names()) == {
            "customers", "invoices", "payments", "purchase_orders",
            "validated_ledger", DRIFT_COLLECTION,
        }
    finally:
        client.close()


# ============================================================
# Live catalog integration and controlled drift (Steps 26-28)
# ============================================================

@pytest.fixture()
def live_catalog(pipeline_connector):
    from erp_pipeline.catalog.repository import CatalogRepository
    from erp_pipeline.catalog.schema import bootstrap_catalog
    from erp_pipeline.catalog.service import SchemaCatalogService
    from erp_pipeline.schemas.source_models import SourceSystem
    from sqlalchemy import text

    engine = pipeline_connector._sqlalchemy_engine  # noqa: SLF001 - test setup
    bootstrap_catalog(engine)
    catalog = SchemaCatalogService(CatalogRepository(engine))

    catalog.register_source_system(
        SourceSystem(
            source_system_id=SOURCE_SYSTEM_ID,
            name="Phase 5 live MongoDB source",
            source_type=SourceType.MONGODB,
            environment="research",
        )
    )

    yield catalog

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
                "DELETE FROM erp_catalog.schema_snapshots WHERE source_system_id = :sid"
            ),
            {"sid": SOURCE_SYSTEM_ID},
        )
        connection.execute(
            text("DELETE FROM erp_catalog.source_systems WHERE source_system_id = :sid"),
            {"sid": SOURCE_SYSTEM_ID},
        )


def test_live_inference_publishes_and_round_trips(
    mongo_connector_live, live_catalog, live_schema
):
    live_catalog.publish_schema(live_schema)

    retrieved = live_catalog.get_snapshot(live_schema.schema_id)

    assert retrieved.origin is SchemaOrigin.INFERRED
    assert retrieved.compute_schema_hash() == live_schema.compute_schema_hash()
    assert len(retrieved.entities) == len(live_schema.entities)


def test_live_unchanged_republish_creates_no_new_version(
    mongo_connector_live, live_catalog
):
    options = MongoInferenceOptions(exclude_collections=[DRIFT_COLLECTION])

    first = live_catalog.publish_schema(infer_mongodb_schema(mongo_connector_live, options))
    second = live_catalog.publish_schema(infer_mongodb_schema(mongo_connector_live, options))

    assert first.created is True
    assert first.record.catalog_version == 1
    assert second.created is False
    assert second.record.catalog_version == 1


def test_live_controlled_document_change_creates_version_two(
    seeded_mongodb, mongo_connector_live, live_catalog
):
    """Step 27, live: add ONE document to the isolated drift collection and
    prove the catalog advances with a matching SchemaDiff."""
    from bson import ObjectId
    from erp_pipeline.catalog.versioning import compare_schemas
    from pymongo import MongoClient

    options = MongoInferenceOptions(include_collections=[DRIFT_COLLECTION])

    v1 = infer_mongodb_schema(mongo_connector_live, options)
    v1_result = live_catalog.publish_schema(v1)

    admin = MongoClient(
        host=os.getenv("MONGO_PHASE5_HOST", "localhost"),
        port=int(os.getenv("MONGO_PHASE5_PORT", "27018")),
        username=os.getenv("MONGO_PHASE5_ADMIN_USER", "phase5_admin"),
        password=os.getenv("MONGO_PHASE5_ADMIN_PASSWORD"),
        authSource=os.getenv("MONGO_PHASE5_AUTH_DB", "admin"),
        serverSelectionTimeoutMS=5000,
    )
    inserted_id = ObjectId("650000000000000000000043")

    try:
        admin[seeded_mongodb][DRIFT_COLLECTION].insert_one(
            {
                "_id": inserted_id,
                "invoice": "INV3",
                "customer": {"id": 30, "name": "ABC"},
                "amount": 6000,
                "approved": True,
            }
        )

        v2 = infer_mongodb_schema(mongo_connector_live, options)
        v2_result = live_catalog.publish_schema(v2)

        assert v1.compute_schema_hash() != v2.compute_schema_hash()
        assert v1.schema_name == v2.schema_name
        assert v1_result.record.catalog_version == 1
        assert v2_result.created is True
        assert v2_result.record.catalog_version == 2

        diff = compare_schemas(v1, v2)
        assert set(diff.added_fields) == {
            (DRIFT_COLLECTION, "customer.name"),
            (DRIFT_COLLECTION, "approved"),
        }
        assert diff.removed_fields == ()
        assert diff.added_entities == ()
    finally:
        # Restore the drift collection so the test is repeatable within a
        # session; the whole database is dropped at session end regardless.
        admin[seeded_mongodb][DRIFT_COLLECTION].delete_one({"_id": inserted_id})
        admin.close()
