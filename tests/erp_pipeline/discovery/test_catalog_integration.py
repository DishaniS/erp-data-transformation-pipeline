"""Discovery -> Phase 2 Schema Catalog integration.

Proves the discovered SourceSchema round-trips through the existing catalog
unchanged, that rediscovering an unchanged database does not create a new
catalog version, and that a controlled structural change does - with
SchemaDiff naming the actual change.

Phase 4 owns none of that versioning logic; these tests verify the handoff.
"""

import pytest

from erp_pipeline.catalog.repository import CatalogRepository
from erp_pipeline.catalog.schema import bootstrap_catalog
from erp_pipeline.catalog.service import SchemaCatalogService
from erp_pipeline.catalog.versioning import compare_schemas
from erp_pipeline.discovery import DiscoveryOptions, RelationalDiscoveryService, discover_schema
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.schemas.source_models import SourceSystem


V1_DDL = """
CREATE TABLE {schema}.customer (
    id   INTEGER PRIMARY KEY,
    name VARCHAR(120)
);
"""

V2_DDL = """
ALTER TABLE {schema}.customer ADD COLUMN email VARCHAR(255);
"""


@pytest.fixture()
def catalog(pipeline_connector):
    """A catalog service backed by the same pipeline database."""
    engine = pipeline_connector._sqlalchemy_engine  # noqa: SLF001 - test setup
    bootstrap_catalog(engine)
    return SchemaCatalogService(CatalogRepository(engine))


@pytest.fixture()
def registered_source_system(catalog):
    from sqlalchemy import text

    source_system = SourceSystem(
        source_system_id="discovery_probe_pg",
        name="Discovery probe source",
        source_type=SourceType.POSTGRESQL,
        environment="research",
    )
    catalog.register_source_system(source_system)

    yield source_system

    # Remove everything this test session filed, leaving the catalog as found.
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
            {"sid": source_system.source_system_id},
        )
        for table in ("source_entities", "source_relationships", "schema_snapshots"):
            connection.execute(
                text(
                    f"""
                    DELETE FROM erp_catalog.{table} WHERE schema_id IN (
                        SELECT schema_id FROM erp_catalog.schema_snapshots
                        WHERE source_system_id = :sid)
                    """
                    if table != "schema_snapshots"
                    else "DELETE FROM erp_catalog.schema_snapshots WHERE source_system_id = :sid"
                ),
                {"sid": source_system.source_system_id},
            )
        connection.execute(
            text("DELETE FROM erp_catalog.source_systems WHERE source_system_id = :sid"),
            {"sid": source_system.source_system_id},
        )


def test_discovered_schema_round_trips_through_the_catalog(
    pipeline_connector, probe_schema, catalog, registered_source_system
):
    schema_name, run = probe_schema
    run(V1_DDL.format(schema=schema_name))

    options = DiscoveryOptions(include_schemas=[schema_name])
    discovered = discover_schema(pipeline_connector, options)

    catalog.publish_schema(discovered)
    retrieved = catalog.get_snapshot(discovered.schema_id)

    discovered_payload = discovered.to_json_dict()
    retrieved_payload = retrieved.to_json_dict()
    # created_at is object-construction time, not structural identity.
    discovered_payload.pop("created_at")
    retrieved_payload.pop("created_at")

    assert retrieved_payload == discovered_payload
    assert retrieved.compute_schema_hash() == discovered.compute_schema_hash()


def test_rediscovering_unchanged_database_does_not_create_a_new_version(
    pipeline_connector, probe_schema, catalog, registered_source_system
):
    """Step 22: discovery running again must not bump the catalog version."""
    schema_name, run = probe_schema
    run(V1_DDL.format(schema=schema_name))

    options = DiscoveryOptions(include_schemas=[schema_name])

    first_schema = discover_schema(pipeline_connector, options)
    first_result = catalog.publish_schema(first_schema)

    second_schema = discover_schema(pipeline_connector, options)
    second_result = catalog.publish_schema(second_schema)

    assert first_schema.compute_schema_hash() == second_schema.compute_schema_hash()
    assert first_schema.schema_id == second_schema.schema_id

    assert first_result.created is True
    assert first_result.record.catalog_version == 1
    assert second_result.created is False
    assert second_result.record.catalog_version == 1


def test_controlled_structural_change_creates_catalog_version_two(
    pipeline_connector, probe_schema, catalog, registered_source_system
):
    """Step 23: add one column to an ISOLATED test schema and prove the
    catalog advances to version 2 with a matching SchemaDiff."""
    schema_name, run = probe_schema
    options = DiscoveryOptions(include_schemas=[schema_name])

    run(V1_DDL.format(schema=schema_name))
    v1 = discover_schema(pipeline_connector, options)
    v1_result = catalog.publish_schema(v1)

    # The only change: one new nullable column on the isolated test table.
    run(V2_DDL.format(schema=schema_name))
    v2 = discover_schema(pipeline_connector, options)
    v2_result = catalog.publish_schema(v2)

    assert v1.compute_schema_hash() != v2.compute_schema_hash()
    assert v1.schema_id != v2.schema_id
    assert v1.schema_name == v2.schema_name  # same logical scope

    assert v1_result.record.catalog_version == 1
    assert v2_result.created is True
    assert v2_result.record.catalog_version == 2

    diff = compare_schemas(v1, v2)
    assert diff.added_fields == ((f"{schema_name}.customer", "email"),)
    assert diff.removed_fields == ()
    assert diff.added_entities == ()
    assert diff.removed_entities == ()


def test_history_lists_both_versions_in_order(
    pipeline_connector, probe_schema, catalog, registered_source_system
):
    schema_name, run = probe_schema
    options = DiscoveryOptions(include_schemas=[schema_name])

    run(V1_DDL.format(schema=schema_name))
    catalog.publish_schema(discover_schema(pipeline_connector, options))

    run(V2_DDL.format(schema=schema_name))
    catalog.publish_schema(discover_schema(pipeline_connector, options))

    history = catalog.history("discovery_probe_pg", schema_name)
    assert [record.catalog_version for record in history] == [1, 2]


def test_discover_and_publish_service_helper(
    pipeline_connector, probe_schema, catalog, registered_source_system
):
    schema_name, run = probe_schema
    run(V1_DDL.format(schema=schema_name))

    service = RelationalDiscoveryService(DiscoveryOptions(include_schemas=[schema_name]))
    result, snapshot = service.discover_and_publish(pipeline_connector, catalog)

    assert snapshot.record.catalog_version == 1
    assert result.schema.schema_id == snapshot.record.schema_id
    assert result.profiling.enabled is False


def test_catalog_summary_reflects_discovered_counts(
    pipeline_connector, probe_schema, catalog, registered_source_system
):
    schema_name, run = probe_schema
    run(V1_DDL.format(schema=schema_name))

    discovered = discover_schema(
        pipeline_connector, DiscoveryOptions(include_schemas=[schema_name])
    )
    catalog.publish_schema(discovered)

    summary = catalog.summarize(discovered.schema_id)
    assert summary.entity_count == 1
    assert summary.field_count == 2  # id, name
    assert summary.catalog_version == 1
