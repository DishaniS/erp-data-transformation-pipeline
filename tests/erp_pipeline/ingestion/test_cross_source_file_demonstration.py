"""PHASE 6 CROSS-SOURCE DEMONSTRATION.

Phase 4 proved three relational engines converge on one contract. Phase 5
added a schemaless document store. This module proves the architecture's
actual claim, which is subtler than "everything converges":

    STRUCTURED sources      PostgreSQL, MySQL, MongoDB, CSV
                            -> SourceSchema / SourceEntity / SourceField

    UNSTRUCTURED sources    PDF, Image
                            -> ExtractedDocument with page provenance

Both are SOURCE-level representations. Neither is canonical yet.

The point is that a PDF is NOT forced into a fake tabular schema so it can
look like the others. A scanned invoice has no columns, and inventing some
would be a fabrication that every later phase would have to work around. The
pipeline supports both shapes because ERP data genuinely comes in both.

Relational schemas here come from the Phase 4 fakes, MongoDB from the Phase 5
fakes, and CSV/PDF/image from real files on disk - all produced by the actual
production code, never hand-constructed.
"""

from __future__ import annotations

import pytest

from erp_pipeline.discovery.mongodb import infer_mongodb_schema
from erp_pipeline.discovery.relational import discover_schema
from erp_pipeline.ingestion import (
    DocumentFileResult,
    ExtractedDocument,
    TabularFileResult,
    ingest_file,
)
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
def structured_schemas(csv_fixtures) -> dict[str, SourceSchema]:
    """One ``SourceSchema`` per structured source, from production code."""
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}
    schemas["mongodb"] = infer_mongodb_schema(
        mongo_connector({"invoices": MONGO_INVOICES})
    )
    schemas["csv"] = ingest_file(csv_fixtures / "normal.csv").schema
    return schemas


@pytest.fixture()
def extracted_documents(binary_fixtures) -> dict[str, ExtractedDocument]:
    """One ``ExtractedDocument`` per unstructured source, from real files."""
    pytest.importorskip("fitz")

    return {
        "pdf": ingest_file(binary_fixtures / "text_multi_page.pdf").document,
        "image": ingest_file(binary_fixtures / "text.png").document,
    }


# ============================================================
# Structured sources converge on the Phase 1 contract
# ============================================================

def test_all_structured_sources_produce_a_source_schema(structured_schemas):
    assert set(structured_schemas) == {
        "postgresql", "mysql", "sql_server", "mongodb", "csv",
    }

    for name, schema in structured_schemas.items():
        assert isinstance(schema, SourceSchema), name
        assert all(isinstance(e, SourceEntity) for e in schema.entities), name
        assert all(
            isinstance(f, SourceField) for e in schema.entities for f in e.fields
        ), name


def test_all_structured_sources_serialize_through_one_contract_shape(
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


def test_origin_records_how_each_structure_was_learned(structured_schemas):
    """Declared metadata is discovered; sampled content is inferred. The
    distinction survives all the way to the catalog."""
    for name in ("postgresql", "mysql", "sql_server"):
        assert structured_schemas[name].origin is SchemaOrigin.DISCOVERED, name

    assert structured_schemas["mongodb"].origin is SchemaOrigin.INFERRED
    assert structured_schemas["csv"].origin is SchemaOrigin.INFERRED


def test_entity_kind_reflects_each_paradigm(structured_schemas):
    assert structured_schemas["postgresql"].entities[0].entity_kind is (
        EntityKind.TABLE
    )
    assert structured_schemas["mongodb"].entities[0].entity_kind is (
        EntityKind.COLLECTION
    )
    assert structured_schemas["csv"].entities[0].entity_kind is EntityKind.DATASET


def test_only_declared_constraints_produce_keys_and_relationships(
    structured_schemas,
):
    """A database declares them; a document store and a CSV do not, and Phase
    5/6 refuse to invent them."""
    for name in ("postgresql", "mysql", "sql_server"):
        assert structured_schemas[name].relationships, name
        assert structured_schemas[name].entities[0].primary_key_fields, name

    assert structured_schemas["mongodb"].relationships == ()
    assert structured_schemas["csv"].relationships == ()
    assert structured_schemas["csv"].entities[0].primary_key_fields == ()


def test_one_consumer_reads_every_structured_source_without_knowing_its_type(
    structured_schemas,
):
    rows = [
        (name, entity.normalized_name, field.normalized_name,
         field.normalized_data_type.value)
        for name, schema in sorted(structured_schemas.items())
        for entity in schema.entities
        for field in entity.fields
    ]

    assert {row[0] for row in rows} == {
        "postgresql", "mysql", "sql_server", "mongodb", "csv",
    }
    assert len(rows) > 20


def test_a_csv_amount_normalizes_like_a_database_amount(structured_schemas):
    """Five vocabularies, one cross-source type lattice."""
    postgres_total = structured_schemas["postgresql"].entity_by_normalized_name(
        "public.fin_invoice"
    ).field_by_normalized_name("total")
    csv_amount = structured_schemas["csv"].entities[0].field_by_normalized_name(
        "amount"
    )

    assert "NUMERIC" in postgres_total.source_data_type.upper()
    assert csv_amount.source_data_type == "mixed<decimal|integer>"

    assert postgres_total.normalized_data_type is FieldDataType.DECIMAL
    assert csv_amount.normalized_data_type is FieldDataType.DECIMAL


# ============================================================
# Unstructured sources use document extraction instead
# ============================================================

def test_documents_produce_extracted_documents_not_schemas(extracted_documents):
    for name, document in extracted_documents.items():
        assert isinstance(document, ExtractedDocument), name
        assert not isinstance(document, SourceSchema), name
        assert document.pages, name


def test_no_fake_tabular_schema_is_invented_for_a_document(binary_fixtures):
    pytest.importorskip("fitz")

    pdf = ingest_file(binary_fixtures / "text_multi_page.pdf")
    image = ingest_file(binary_fixtures / "text.png")

    for result in (pdf, image):
        assert isinstance(result, DocumentFileResult)
        assert not hasattr(result, "schema")
        assert "entities" not in result.to_dict()
        assert "fields" not in str(result.to_dict())


def test_documents_carry_page_provenance_that_a_schema_could_not_express(
    extracted_documents,
):
    pdf = extracted_documents["pdf"]

    assert [page.page_number for page in pdf.pages] == [1, 2, 3]
    assert all(page.extraction_method for page in pdf.pages)


def test_both_halves_share_one_result_contract(binary_fixtures, csv_fixtures):
    """Different payloads, same envelope: a caller can handle any file
    uniformly and only branch where the meanings genuinely differ."""
    pytest.importorskip("fitz")

    results = [
        ingest_file(csv_fixtures / "normal.csv"),
        ingest_file(binary_fixtures / "text_multi_page.pdf"),
        ingest_file(binary_fixtures / "text.png"),
    ]

    for result in results:
        assert result.file.content_hash
        assert result.file.file_id.startswith("file.sha256.")
        assert result.provenance.extractor
        assert result.status is not None
        assert isinstance(result.to_dict(), dict)

    assert sum(1 for r in results if isinstance(r, TabularFileResult)) == 1
    assert sum(1 for r in results if isinstance(r, DocumentFileResult)) == 2


def test_the_two_halves_are_distinguishable_without_isinstance(
    binary_fixtures, csv_fixtures
):
    pytest.importorskip("fitz")

    csv_result = ingest_file(csv_fixtures / "normal.csv")
    pdf_result = ingest_file(binary_fixtures / "text_single_page.pdf")

    assert csv_result.is_tabular and not csv_result.is_document
    assert pdf_result.is_document and not pdf_result.is_tabular
    assert csv_result.file_type.is_tabular
    assert pdf_result.file_type.is_document


def test_no_file_specific_public_model_competes_with_source_schema():
    """There is no CsvSchema, just as there is no PostgresTable."""
    import erp_pipeline.ingestion as ingestion

    forbidden = {
        "CsvSchema", "CsvTable", "CsvEntity", "CsvField",
        "PdfSchema", "ImageSchema", "FileSchema", "DocumentSchema",
    }
    assert not (set(dir(ingestion)) & forbidden)
