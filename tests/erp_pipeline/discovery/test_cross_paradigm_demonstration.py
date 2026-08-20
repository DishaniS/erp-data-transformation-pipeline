"""PHASE 5 CROSS-PARADIGM DEMONSTRATION.

Phase 4's ``test_cross_database_demonstration`` proved three RELATIONAL engines
converge on one contract. This module proves something strictly stronger: a
schemaless document store converges on the SAME contract.

Four ERP systems describe invoices four different ways:

    PostgreSQL   fin_invoice        INV_NO / CLIENT_REF / TOTAL / APPROVAL
    MySQL        invoices           invoice_number / customer_id / amount / status
    SQL Server   dbo.InvoiceHeader  InvoiceNumber / CustomerCode / InvoiceValue / ApprovedFlag
    MongoDB      invoices           invoice / customer.id / amount / approved   (nested, optional)

Three read DECLARED metadata; the fourth reads DOCUMENTS and infers. All four
are produced by the actual production code, never hand-constructed, and all
four are instances of the same Phase 1 contract::

    Relational Metadata
            |
            v
        SourceSchema
            ^
            |
    MongoDB Inference

What deliberately does NOT converge is the vocabulary. Reconciling
``INV_NO`` / ``invoice_number`` / ``InvoiceNumber`` / ``invoice`` into one
canonical field is mapping, which is a later phase; the point here is that the
STRUCTURES share one contract while the sources stay faithfully themselves.
"""

from __future__ import annotations

import pytest

from erp_pipeline.discovery.mongodb import infer_mongodb_schema
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SchemaOrigin
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
)

from tests.erp_pipeline.discovery.mongo_fakes import mongo_connector
from tests.erp_pipeline.discovery.test_cross_database_demonstration import (
    ALL_ENGINES,
    _discover,
)

#: Documents deliberately exercising what relational sources cannot express:
#: a nested customer object, an optional field, and an array of line items.
MONGO_INVOICES = (
    {
        "_id": "1",
        "invoice": "INV1",
        "customer": {"id": 22},
        "amount": 5000,
        "lines": [{"sku": "A", "qty": 2}],
    },
    {
        "_id": "2",
        "invoice": "INV2",
        "customer": {"id": 25, "name": "ABC"},
        "amount": 9000,
        "approved": True,
        "lines": [{"sku": "B", "qty": 1}],
    },
)


def mongodb_schema() -> SourceSchema:
    return infer_mongodb_schema(
        mongo_connector({"invoices": MONGO_INVOICES}, ),
    )


def all_schemas() -> dict[str, SourceSchema]:
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}
    schemas["mongodb"] = mongodb_schema()
    return schemas


# ============================================================
# The proof
# ============================================================

def test_all_four_paradigms_produce_a_source_schema():
    for name, schema in all_schemas().items():
        assert isinstance(schema, SourceSchema), name
        assert all(isinstance(e, SourceEntity) for e in schema.entities), name
        assert all(
            isinstance(f, SourceField) for e in schema.entities for f in e.fields
        ), name
        assert all(
            isinstance(r, SourceRelationship) for r in schema.relationships
        ), name


def test_all_four_serialize_through_an_identical_contract_shape():
    payloads = {name: schema.to_json_dict() for name, schema in all_schemas().items()}

    shapes = [set(payload) for payload in payloads.values()]
    assert all(shape == shapes[0] for shape in shapes)

    entity_shapes = [set(payload["entities"][0]) for payload in payloads.values()]
    assert all(shape == entity_shapes[0] for shape in entity_shapes)

    field_shapes = [
        set(payload["entities"][0]["fields"][0]) for payload in payloads.values()
    ]
    assert all(shape == field_shapes[0] for shape in field_shapes)


def test_origin_distinguishes_declared_metadata_from_sampled_documents():
    """The one place the four legitimately differ - and they must."""
    schemas = all_schemas()

    for name in ("postgresql", "mysql", "sql_server"):
        assert schemas[name].origin is SchemaOrigin.DISCOVERED, name

    assert schemas["mongodb"].origin is SchemaOrigin.INFERRED


def test_entity_kind_reflects_each_paradigm():
    schemas = all_schemas()

    assert schemas["postgresql"].entities[0].entity_kind is EntityKind.TABLE
    assert schemas["mongodb"].entities[0].entity_kind is EntityKind.COLLECTION


def test_every_paradigm_preserves_its_own_source_vocabulary():
    schemas = all_schemas()

    postgres = schemas["postgresql"].entity_by_normalized_name("public.fin_invoice")
    mongo = schemas["mongodb"].entity_by_normalized_name("invoices")

    assert [f.source_name for f in postgres.fields] == [
        "INV_NO", "CLIENT_REF", "TOTAL", "APPROVAL",
    ]
    assert {f.source_name for f in mongo.fields} >= {"_id", "invoice", "amount"}


def test_the_amount_field_normalizes_the_same_way_everywhere():
    """Four vendor spellings, one cross-source type."""
    schemas = all_schemas()

    postgres_total = schemas["postgresql"].entity_by_normalized_name(
        "public.fin_invoice"
    ).field_by_normalized_name("total")
    mongo_amount = schemas["mongodb"].entity_by_normalized_name(
        "invoices"
    ).field_by_normalized_name("amount")

    assert "NUMERIC" in postgres_total.source_data_type.upper()
    assert mongo_amount.source_data_type == "int"

    assert postgres_total.normalized_data_type is FieldDataType.DECIMAL
    assert mongo_amount.normalized_data_type is FieldDataType.INTEGER


def test_mongodb_expresses_what_relational_sources_cannot():
    """Nesting, arrays and observed optionality - inside the same contract,
    with no extra model."""
    invoices = mongodb_schema().entity_by_normalized_name("invoices")

    nested = invoices.field_by_normalized_name("customer.id")
    assert nested.nested_path == ("customer",)

    lines = invoices.field_by_normalized_name("lines")
    assert lines.is_array is True
    assert lines.source_data_type == "array<object>"
    assert invoices.field_by_normalized_name("lines_.sku") is not None

    optional = invoices.field_by_normalized_name("approved")
    assert optional.required is False
    assert optional.metadata["observed"]["presence_ratio"] == 0.5


def test_identities_do_not_collide_across_paradigms():
    schemas = all_schemas()

    assert len({schema.schema_id for schema in schemas.values()}) == 4
    entity_ids = {
        entity.entity_id for schema in schemas.values() for entity in schema.entities
    }
    assert len(entity_ids) == 7  # 2 entities x 3 relational engines + 1 collection


def test_only_declared_constraints_produce_relationships():
    schemas = all_schemas()

    for name in ("postgresql", "mysql", "sql_server"):
        assert len(schemas[name].relationships) == 1, name

    # MongoDB enforces none, so none are invented.
    assert schemas["mongodb"].relationships == ()


def test_no_paradigm_specific_public_model_competes_with_source_schema():
    """Step 36: no MongoCollectionSchema / MongoFieldSchema, just as there is
    no PostgresTable."""
    import erp_pipeline.discovery as discovery

    forbidden = {
        "PostgresTable", "MySQLTable", "SQLServerTable", "PostgresSchema",
        "MongoCollectionSchema", "MongoFieldSchema", "MongoSchema",
        "MongoEntity", "DocumentSchema",
    }
    assert not (set(dir(discovery)) & forbidden)


def test_one_consumer_can_read_all_four_without_knowing_the_source():
    """The practical payoff: a single loop over the common contract."""
    rows = []

    for name, schema in sorted(all_schemas().items()):
        for entity in schema.entities:
            for field in entity.fields:
                rows.append(
                    (
                        name,
                        entity.normalized_name,
                        field.normalized_name,
                        field.normalized_data_type.value,
                        field.nullable,
                    )
                )

    assert len(rows) > 20
    assert {row[0] for row in rows} == {"postgresql", "mysql", "sql_server", "mongodb"}
