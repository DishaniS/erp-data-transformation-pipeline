"""CSV ingestion -> Phase 2 Schema Catalog integration (Steps 19, 34).

A CSV genuinely has a structure, so its ``SourceSchema`` publishes through the
existing catalog with no architectural hack: the catalog's ``source_type`` and
``origin`` columns are plain text with no relational-only constraint, and
``SourceType.CSV`` is already part of the frozen Phase 1 vocabulary.

PDFs and images are deliberately NOT published - see the final test.

Phase 6 owns none of the versioning logic; these tests verify the handoff, as
the Phase 4 and Phase 5 equivalents do for their sources.
"""

from __future__ import annotations

import pytest

from erp_pipeline.catalog.repository import CatalogRepository
from erp_pipeline.catalog.schema import bootstrap_catalog
from erp_pipeline.catalog.service import SchemaCatalogService
from erp_pipeline.catalog.versioning import compare_schemas
from erp_pipeline.ingestion import (
    FileIngestionService,
    IngestionOptions,
    UnsupportedFileTypeError,
    ingest_file,
)
from erp_pipeline.schemas.enums import SchemaOrigin, SourceType

SOURCE_SYSTEM_ID = "file_ingestion_probe"

V1_CSV = (
    "invoice_no,customer,amount\n"
    "INV-1,Acme,1500\n"
    "INV-2,Globex,2750\n"
)

#: The controlled change: one added column. Nothing else differs.
V2_CSV = (
    "invoice_no,customer,amount,currency\n"
    "INV-1,Acme,1500,USD\n"
    "INV-2,Globex,2750,USD\n"
)


@pytest.fixture()
def options() -> IngestionOptions:
    return IngestionOptions(source_system_id=SOURCE_SYSTEM_ID)


@pytest.fixture()
def catalog(pipeline_connector):
    engine = pipeline_connector._sqlalchemy_engine  # noqa: SLF001 - test setup
    bootstrap_catalog(engine)
    return SchemaCatalogService(CatalogRepository(engine))


@pytest.fixture()
def registered_file_source(catalog, options):
    from sqlalchemy import text

    source_system = FileIngestionService(options).source_system(
        name="Phase 6 file ingestion probe"
    )
    catalog.register_source_system(source_system)

    yield source_system

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
                "DELETE FROM erp_catalog.schema_snapshots WHERE source_system_id = :sid"
            ),
            {"sid": SOURCE_SYSTEM_ID},
        )
        connection.execute(
            text("DELETE FROM erp_catalog.source_systems WHERE source_system_id = :sid"),
            {"sid": SOURCE_SYSTEM_ID},
        )


def write_csv(tmp_path, content: str):
    """Always the same filename, so the schema SCOPE is stable across edits."""
    path = tmp_path / "invoices.csv"
    path.write_text(content, encoding="utf-8")
    return path


# ============================================================
# Source system and round trip
# ============================================================

def test_a_file_source_system_registers_cleanly(catalog, registered_file_source):
    stored = catalog.repository.get_source_system(SOURCE_SYSTEM_ID)

    assert stored.source_type is SourceType.CSV
    assert stored.source_system_id == SOURCE_SYSTEM_ID


def test_an_inferred_csv_schema_round_trips(tmp_path, catalog,
                                            registered_file_source, options):
    result = ingest_file(write_csv(tmp_path, V1_CSV), options)

    catalog.publish_schema(result.schema)
    retrieved = catalog.get_snapshot(result.schema.schema_id)

    published = result.schema.to_json_dict()
    stored = retrieved.to_json_dict()
    published.pop("created_at")
    stored.pop("created_at")

    assert stored == published
    assert retrieved.origin is SchemaOrigin.INFERRED
    assert retrieved.compute_schema_hash() == result.schema.compute_schema_hash()


def test_column_types_and_observations_survive_the_round_trip(
    tmp_path, catalog, registered_file_source, options
):
    result = ingest_file(write_csv(tmp_path, V1_CSV), options)
    catalog.publish_schema(result.schema)

    entity = catalog.get_snapshot(result.schema.schema_id).entities[0]
    amount = entity.field_by_normalized_name("amount")

    assert amount.normalized_data_type.value == "integer"
    assert amount.metadata["schema_claim"] == "observed"
    assert entity.metadata["delimiter"] == ","


# ============================================================
# Idempotency (Step 34)
# ============================================================

def test_reingesting_the_same_file_creates_no_new_version(
    tmp_path, catalog, registered_file_source, options
):
    path = write_csv(tmp_path, V1_CSV)

    first = catalog.publish_schema(ingest_file(path, options).schema)
    second = catalog.publish_schema(ingest_file(path, options).schema)

    assert first.created is True
    assert first.record.catalog_version == 1
    assert second.created is False
    assert second.record.catalog_version == 1


def test_a_reordered_but_structurally_identical_sample_stays_version_one(
    tmp_path, catalog, registered_file_source, options
):
    """More rows of the same shape are not a schema change."""
    path = write_csv(tmp_path, V1_CSV)
    first = catalog.publish_schema(ingest_file(path, options).schema)

    path.write_text(V1_CSV + "INV-3,Initech,3000\n", encoding="utf-8")
    second = catalog.publish_schema(ingest_file(path, options).schema)

    assert first.record.catalog_version == 1
    assert second.created is False
    assert second.record.catalog_version == 1


# ============================================================
# Controlled structural change (Step 19)
# ============================================================

def test_a_new_column_creates_catalog_version_two(
    tmp_path, catalog, registered_file_source, options
):
    path = write_csv(tmp_path, V1_CSV)
    v1 = ingest_file(path, options).schema
    v1_result = catalog.publish_schema(v1)

    path.write_text(V2_CSV, encoding="utf-8")
    v2 = ingest_file(path, options).schema
    v2_result = catalog.publish_schema(v2)

    assert v1.compute_schema_hash() != v2.compute_schema_hash()
    assert v1.schema_id != v2.schema_id
    assert v1.schema_name == v2.schema_name  # same logical scope

    assert v1_result.record.catalog_version == 1
    assert v2_result.created is True
    assert v2_result.record.catalog_version == 2


def test_the_diff_names_the_actual_new_column(
    tmp_path, catalog, registered_file_source, options
):
    path = write_csv(tmp_path, V1_CSV)
    v1 = ingest_file(path, options).schema
    catalog.publish_schema(v1)

    path.write_text(V2_CSV, encoding="utf-8")
    v2 = ingest_file(path, options).schema
    catalog.publish_schema(v2)

    diff = catalog.compare_versions(v1.schema_id, v2.schema_id)

    assert diff.added_fields == (("invoices", "currency"),)
    assert diff.removed_fields == ()
    assert diff.added_entities == ()


def test_a_type_change_is_reported_as_a_changed_field(
    tmp_path, catalog, registered_file_source, options
):
    path = write_csv(tmp_path, V1_CSV)
    v1 = ingest_file(path, options).schema
    catalog.publish_schema(v1)

    path.write_text(
        "invoice_no,customer,amount\nINV-1,Acme,one thousand\n", encoding="utf-8"
    )
    v2 = ingest_file(path, options).schema
    catalog.publish_schema(v2)

    diff = compare_schemas(v1, v2)
    changed = {(c.field, c.attribute) for c in diff.changed_fields}

    assert ("amount", "normalized_data_type") in changed


def test_history_lists_both_versions_in_order(
    tmp_path, catalog, registered_file_source, options
):
    path = write_csv(tmp_path, V1_CSV)
    catalog.publish_schema(ingest_file(path, options).schema)

    path.write_text(V2_CSV, encoding="utf-8")
    catalog.publish_schema(ingest_file(path, options).schema)

    history = catalog.history(SOURCE_SYSTEM_ID, "invoices")

    assert [record.catalog_version for record in history] == [1, 2]


def test_the_service_helper_ingests_and_publishes(
    tmp_path, catalog, registered_file_source, options
):
    service = FileIngestionService(options)

    result, snapshot = service.ingest_and_publish(write_csv(tmp_path, V1_CSV), catalog)

    assert snapshot.record.catalog_version == 1
    assert result.schema.schema_id == snapshot.record.schema_id
    # The rows remain streamable after publishing.
    assert len(list(result.iter_records())) == 2


def test_the_summary_reflects_the_inferred_counts(
    tmp_path, catalog, registered_file_source, options
):
    result = ingest_file(write_csv(tmp_path, V1_CSV), options)
    catalog.publish_schema(result.schema)

    summary = catalog.summarize(result.schema.schema_id)

    assert summary.entity_count == 1
    assert summary.field_count == 3
    assert summary.relationship_count == 0
    assert summary.source_type == "csv"


# ============================================================
# Documents are deliberately not published
# ============================================================

def test_a_pdf_cannot_be_published_to_the_schema_catalog(
    binary_fixtures, catalog, registered_file_source, options
):
    """The catalog stores STRUCTURAL descriptions. A document has none, and
    publishing an empty schema for one would imply a capability that does not
    exist."""
    pytest.importorskip("fitz")
    service = FileIngestionService(options)

    with pytest.raises(UnsupportedFileTypeError, match="SourceSchema"):
        service.ingest_and_publish(
            binary_fixtures / "text_single_page.pdf", catalog
        )


def test_an_image_cannot_be_published_to_the_schema_catalog(
    binary_fixtures, catalog, registered_file_source, options
):
    service = FileIngestionService(options)

    with pytest.raises(UnsupportedFileTypeError):
        service.ingest_and_publish(binary_fixtures / "text.png", catalog)
