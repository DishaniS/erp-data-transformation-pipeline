"""PHASE 7 CROSS-SOURCE DEMONSTRATION.

The architecture's full claim, now that every source type exists:

    STRUCTURED sources          PostgreSQL, MySQL, SQL Server, MongoDB, CSV,
                                OpenAPI, Postman
                                -> SourceSchema / SourceEntity / SourceField

    UNSTRUCTURED sources        PDF, Image
                                -> ExtractedDocument with page provenance

and within the structured half, HOW the structure became known is preserved
rather than flattened:

    DISCOVERED    PostgreSQL, MySQL, SQL Server   declared database catalogs
    API_SPEC      OpenAPI / Swagger               a declared API contract
    INFERRED      MongoDB, CSV, Postman           observed from samples

Three different provenances, one contract. That is the point: a consumer reads
every source through the same models, and can still tell which claims are
guaranteed and which are observations.

Every schema here is produced by real production code over real inputs - the
relational ones through the Phase 4 fakes, MongoDB through the Phase 5 fakes,
and CSV/OpenAPI/Postman from real files on disk.
"""

from __future__ import annotations

import pytest

from erp_pipeline.api_specs import parse_api_spec
from erp_pipeline.discovery.mongodb import infer_mongodb_schema
from erp_pipeline.ingestion import ExtractedDocument, ingest_file
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SchemaOrigin
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceSchema,
)

from tests.erp_pipeline.discovery.mongo_fakes import mongo_connector
from tests.erp_pipeline.discovery.test_cross_database_demonstration import (
    ALL_ENGINES,
    _discover,
)

MONGO_INVOICES = (
    {"_id": "1", "invoice": "INV1", "customer": {"id": 22}, "amount": 5000},
    {"_id": "2", "invoice": "INV2", "customer": {"id": 25, "name": "ABC"},
     "amount": 9000, "approved": True},
)


@pytest.fixture()
def csv_fixture_dir() -> "object":
    from pathlib import Path

    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "ingestion"


@pytest.fixture()
def structured_schemas(spec_fixtures, csv_fixture_dir) -> dict[str, SourceSchema]:
    """One ``SourceSchema`` per structured source, all from production code."""
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}
    schemas["mongodb"] = infer_mongodb_schema(
        mongo_connector({"invoices": MONGO_INVOICES})
    )
    schemas["csv"] = ingest_file(csv_fixture_dir / "normal.csv").schema
    schemas["openapi"] = parse_api_spec(
        spec_fixtures / "openapi_3_basic.json"
    ).schema
    schemas["postman"] = parse_api_spec(
        spec_fixtures / "postman_response_examples.json"
    ).schema
    return schemas


# ============================================================
# All seven structured sources converge
# ============================================================

def test_every_structured_source_produces_a_source_schema(structured_schemas):
    assert set(structured_schemas) == {
        "postgresql", "mysql", "sql_server", "mongodb", "csv", "openapi",
        "postman",
    }

    for name, schema in structured_schemas.items():
        assert isinstance(schema, SourceSchema), name
        assert schema.entities, name
        assert all(isinstance(e, SourceEntity) for e in schema.entities), name
        assert all(
            isinstance(f, SourceField) for e in schema.entities for f in e.fields
        ), name


def test_every_structured_source_serializes_through_one_contract_shape(
    structured_schemas,
):
    payloads = {
        name: schema.to_json_dict() for name, schema in structured_schemas.items()
    }

    shapes = [set(payload) for payload in payloads.values()]
    assert all(shape == shapes[0] for shape in shapes)

    entity_shapes = [set(payload["entities"][0]) for payload in payloads.values()]
    assert all(shape == entity_shapes[0] for shape in entity_shapes)

    field_shapes = [
        set(payload["entities"][0]["fields"][0]) for payload in payloads.values()
    ]
    assert all(shape == field_shapes[0] for shape in field_shapes)


def test_origin_records_how_each_structure_became_known(structured_schemas):
    """Three provenances, honestly distinguished."""
    for name in ("postgresql", "mysql", "sql_server"):
        assert structured_schemas[name].origin is SchemaOrigin.DISCOVERED, name

    assert structured_schemas["openapi"].origin is SchemaOrigin.API_SPEC

    for name in ("mongodb", "csv", "postman"):
        assert structured_schemas[name].origin is SchemaOrigin.INFERRED, name


def test_entity_kind_reflects_each_paradigm(structured_schemas):
    kinds = {
        name: schema.entities[0].entity_kind
        for name, schema in structured_schemas.items()
    }

    assert kinds["postgresql"] is EntityKind.TABLE
    assert kinds["mongodb"] is EntityKind.COLLECTION
    assert kinds["csv"] is EntityKind.DATASET
    assert kinds["openapi"] is EntityKind.API_SCHEMA
    assert kinds["postman"] is EntityKind.API_SCHEMA


def test_only_declared_constraints_produce_keys(structured_schemas):
    """A database declares primary keys. An API contract, a CSV and a document
    store do not - and none of the three invents one."""
    for name in ("postgresql", "mysql", "sql_server"):
        assert structured_schemas[name].entities[0].primary_key_fields, name

    for name in ("csv", "openapi", "postman"):
        assert all(
            entity.primary_key_fields == ()
            for entity in structured_schemas[name].entities
        ), name


def test_relationships_come_only_from_declarations(structured_schemas):
    """Foreign keys from a database, $refs from OpenAPI. Nothing from a field
    name, and nothing at all where the source declares nothing."""
    for name in ("postgresql", "mysql", "sql_server"):
        assert structured_schemas[name].relationships, name

    assert structured_schemas["openapi"].relationships

    for name in ("mongodb", "csv", "postman"):
        assert structured_schemas[name].relationships == (), name


def test_no_source_infers_business_meaning(structured_schemas):
    """Phase 8's job, and no earlier phase has quietly started it."""
    for name, schema in structured_schemas.items():
        assert all(
            field.semantic_type is None
            for entity in schema.entities
            for field in entity.fields
        ), name


def test_an_amount_normalizes_consistently_across_paradigms(structured_schemas):
    """Seven vocabularies, one cross-source type lattice."""
    postgres_total = structured_schemas["postgresql"].entity_by_normalized_name(
        "public.fin_invoice"
    ).field_by_normalized_name("total")
    csv_amount = structured_schemas["csv"].entities[0].field_by_normalized_name(
        "amount"
    )
    openapi_total = structured_schemas["openapi"].entity_by_normalized_name(
        "invoice"
    ).field_by_normalized_name("totalamount")

    assert "NUMERIC" in postgres_total.source_data_type.upper()
    assert csv_amount.source_data_type == "mixed<decimal|integer>"
    assert openapi_total.source_data_type == "number(double)"

    assert postgres_total.normalized_data_type is FieldDataType.DECIMAL
    assert csv_amount.normalized_data_type is FieldDataType.DECIMAL
    assert openapi_total.normalized_data_type is FieldDataType.DECIMAL


def test_nested_paths_use_one_vocabulary_everywhere(structured_schemas):
    """``customer.id`` and ``lines[]`` mean the same thing whether they came
    from a document store or an API contract."""
    mongo = structured_schemas["mongodb"].entity_by_normalized_name("invoices")
    openapi = structured_schemas["openapi"].entity_by_normalized_name("invoice")
    postman = structured_schemas["postman"].entity_by_normalized_name(
        "get_invoice_response_200"
    )

    assert mongo.field_by_normalized_name("customer.id").nested_path == ("customer",)
    assert openapi.field_by_normalized_name("lines_.sku").nested_path == (
        "lines", "[]",
    )
    assert postman.field_by_normalized_name("lines_.sku").nested_path == (
        "lines", "[]",
    )


def test_one_consumer_reads_all_seven_without_knowing_the_source(structured_schemas):
    """The practical payoff: a single loop over the common contract."""
    rows = [
        (name, entity.normalized_name, field.normalized_name,
         field.normalized_data_type.value, schema.origin.value)
        for name, schema in sorted(structured_schemas.items())
        for entity in schema.entities
        for field in entity.fields
    ]

    assert {row[0] for row in rows} == {
        "postgresql", "mysql", "sql_server", "mongodb", "csv", "openapi",
        "postman",
    }
    assert {row[4] for row in rows} == {"discovered", "inferred", "api_spec"}
    assert len(rows) > 60


# ============================================================
# Unstructured sources stay document-shaped
# ============================================================

def test_documents_do_not_produce_a_source_schema(tmp_path):
    """A PDF has no columns, and an API spec's rules do not apply to it."""
    pytest.importorskip("fitz")
    import fitz

    pdf_path = tmp_path / "note.pdf"
    document = fitz.open()
    page = document.new_page(width=200, height=200)
    page.insert_text((20, 100), "DOCUMENT CONTENT", fontsize=12)
    document.save(pdf_path)
    document.close()

    result = ingest_file(pdf_path)

    assert result.is_document
    assert isinstance(result.document, ExtractedDocument)
    assert not hasattr(result, "schema")
    assert result.document.pages[0].page_number == 1


def test_the_two_halves_of_the_architecture_stay_distinct(
    structured_schemas, csv_fixture_dir
):
    structured = ingest_file(csv_fixture_dir / "normal.csv")

    assert structured.is_tabular
    assert isinstance(structured.schema, SourceSchema)
    # And every API source sits on the structured side.
    assert isinstance(structured_schemas["openapi"], SourceSchema)
    assert isinstance(structured_schemas["postman"], SourceSchema)


def test_no_source_specific_public_model_competes_with_source_schema():
    """No PostgresTable, no MongoCollectionSchema, no CsvSchema, and no
    ApiSchema replacing the common contract."""
    import erp_pipeline.api_specs as api_specs
    import erp_pipeline.discovery as discovery
    import erp_pipeline.ingestion as ingestion

    forbidden = {
        "PostgresTable", "MySQLTable", "SQLServerTable",
        "MongoCollectionSchema", "MongoFieldSchema",
        "CsvSchema", "CsvTable",
        "OpenApiSchemaModel", "PostmanSchema", "ApiSourceSchema",
        "ApiEntity", "ApiFieldSchema",
    }

    for module in (discovery, ingestion, api_specs):
        assert not (set(dir(module)) & forbidden), module.__name__


def test_api_specific_models_supplement_rather_than_replace(spec_fixtures):
    """``ApiOperation`` holds what ``SourceSchema`` cannot - which structure
    belongs to which endpoint - without becoming a rival contract."""
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json")

    assert isinstance(result.schema, SourceSchema)
    assert result.operations
    # The link between the two is by entity id, never by duplication.
    for operation in result.operations:
        for entity_id in operation.request_entity_ids + operation.response_entity_ids:
            assert result.schema.entity_by_normalized_name(entity_id) is not None
