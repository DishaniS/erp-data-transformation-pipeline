"""Live MySQL discovery against the real Sakila sample database.

Completes the Phase 4 live-verification matrix for MySQL. Uses only the
existing Phase 3 connector, Phase 4 discovery and Phase 2 catalog components.

READ-ONLY throughout: the ``read_only`` account holds only ``SELECT,
SHOW VIEW`` on ``sakila``, and the discovery modules contain no DDL/DML (see
test_read_only_safety.py). Nothing here modifies Sakila.

Skipped, never faked, when the Sakila database or its credentials are
unavailable.
"""

import pytest

from erp_pipeline.discovery import discover_schema
from erp_pipeline.discovery.relational import RelationalSchemaDiscovery
from erp_pipeline.schemas.enums import FieldDataType, RelationshipType, SchemaOrigin, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceRelationship, SourceSchema

#: Real Sakila tables used as representative evidence.
REPRESENTATIVE_TABLES = ("actor", "customer", "film", "rental", "payment")


@pytest.fixture(scope="module")
def sakila_schema(sakila_connector):
    return discover_schema(sakila_connector)


# ============================================================
# Connection
# ============================================================

def test_live_mysql_connection_succeeds(sakila_connector):
    result = sakila_connector.test_connection()

    assert result.success is True
    assert result.source_type is SourceType.MYSQL
    assert result.database_name == "sakila"
    assert result.server_version is not None
    assert result.latency_ms > 0


def test_live_mysql_metadata_reports_pymysql_driver(sakila_connector):
    metadata = sakila_connector.get_source_metadata()

    assert metadata.server_vendor == "mysql"
    assert metadata.connector_implementation == "MySQLConnector"
    assert metadata.driver_name == "pymysql"
    assert metadata.capabilities.relational is True
    # MySQL has no namespace level inside a database.
    assert metadata.capabilities.supports_namespaces is False


def test_connection_settings_never_expose_the_password(sakila_connector):
    settings = sakila_connector._settings  # noqa: SLF001 - test introspection
    assert settings.password  # a password IS configured...

    # ...but it must not be reachable through any rendering of the object.
    assert settings.password not in repr(settings)
    assert settings.password not in str(settings)
    assert "password" not in settings.sanitized()
    assert settings.sanitized()["password_set"] is True


# ============================================================
# Discovery produces the generic Phase 1 contract
# ============================================================

def test_live_mysql_discovery_returns_a_source_schema(sakila_schema):
    assert isinstance(sakila_schema, SourceSchema)
    assert sakila_schema.origin is SchemaOrigin.DISCOVERED
    assert sakila_schema.source_system_id == "mysql_sakila"
    assert sakila_schema.schema_name == "sakila"
    assert sakila_schema.metadata["database"] == "sakila"

    assert all(isinstance(e, SourceEntity) for e in sakila_schema.entities)
    assert all(isinstance(f, SourceField) for e in sakila_schema.entities for f in e.fields)
    assert all(isinstance(r, SourceRelationship) for r in sakila_schema.relationships)


def test_live_mysql_discovers_the_expected_sakila_scale(sakila_schema):
    """Sakila's base tables. Views are excluded by default, which is why the
    count is the 16 base tables rather than Sakila's tables-plus-views."""
    assert len(sakila_schema.entities) == 16
    assert sum(len(e.fields) for e in sakila_schema.entities) == 90
    assert len(sakila_schema.relationships) == 22


def test_live_mysql_entities_have_no_namespace(sakila_schema):
    """MySQL must not be forced into PostgreSQL's two-level shape."""
    assert all(entity.namespace is None for entity in sakila_schema.entities)
    assert all("." not in entity.normalized_name for entity in sakila_schema.entities)


def test_live_mysql_system_databases_are_excluded(sakila_schema):
    names = {entity.normalized_name for entity in sakila_schema.entities}
    for system_table in ("tables", "columns", "user", "global_status"):
        assert system_table not in names


@pytest.mark.parametrize("table_name", REPRESENTATIVE_TABLES)
def test_live_mysql_representative_table_is_discovered(sakila_schema, table_name):
    entity = sakila_schema.entity_by_normalized_name(table_name)

    assert entity is not None, f"{table_name} was not discovered"
    assert entity.source_name == table_name
    assert len(entity.fields) > 0
    assert len(entity.primary_key_fields) == 1  # every one has a surrogate PK
    assert entity.fields[0].is_primary_key is True


def test_live_mysql_primary_keys_discovered_for_every_table(sakila_schema):
    """Sakila declares a primary key on all 16 base tables."""
    without_pk = [e.normalized_name for e in sakila_schema.entities if not e.primary_key_fields]
    assert without_pk == []


def test_live_mysql_composite_primary_key(sakila_schema):
    """film_actor and film_category use composite primary keys."""
    film_actor = sakila_schema.entity_by_normalized_name("film_actor")
    assert film_actor.primary_key_fields == ("actor_id", "film_id")

    film_category = sakila_schema.entity_by_normalized_name("film_category")
    assert film_category.primary_key_fields == ("film_id", "category_id")


# ============================================================
# Vendor datatypes preserved alongside normalized ones
# ============================================================

def test_live_mysql_preserves_vendor_datatypes(sakila_schema):
    film = sakila_schema.entity_by_normalized_name("film")
    fields = {f.normalized_name: f for f in film.fields}

    # Vendor spelling, precision and unsignedness preserved verbatim.
    assert "DECIMAL(4, 2)" in fields["rental_rate"].source_data_type
    assert "ENUM" in fields["rating"].source_data_type
    assert "VARCHAR(128)" in fields["title"].source_data_type
    assert "UNSIGNED" in fields["film_id"].source_data_type.upper()

    # ...normalizing to the common contract types.
    assert fields["rental_rate"].normalized_data_type is FieldDataType.DECIMAL
    assert fields["rating"].normalized_data_type is FieldDataType.STRING
    assert fields["title"].normalized_data_type is FieldDataType.STRING
    assert fields["film_id"].normalized_data_type is FieldDataType.INTEGER
    assert fields["description"].normalized_data_type is FieldDataType.STRING
    assert fields["last_update"].normalized_data_type is FieldDataType.DATETIME


def test_live_mysql_unsigned_integer_variants_all_normalize_to_integer(sakila_schema):
    customer = sakila_schema.entity_by_normalized_name("customer")
    fields = {f.normalized_name: f for f in customer.fields}

    # SMALLINT UNSIGNED and TINYINT UNSIGNED are different vendor spellings...
    assert fields["customer_id"].source_data_type != fields["store_id"].source_data_type
    # ...that share one normalized type.
    assert fields["customer_id"].normalized_data_type is FieldDataType.INTEGER
    assert fields["store_id"].normalized_data_type is FieldDataType.INTEGER


def test_live_mysql_nullability_discovered(sakila_schema):
    customer = sakila_schema.entity_by_normalized_name("customer")
    fields = {f.normalized_name: f for f in customer.fields}

    assert fields["customer_id"].nullable is False
    assert fields["first_name"].nullable is False
    assert fields["email"].nullable is True


def test_live_mysql_never_infers_semantic_type(sakila_schema):
    """customer.email must NOT be tagged as an email address in Phase 4."""
    for entity in sakila_schema.entities:
        for field in entity.fields:
            assert field.semantic_type is None


# ============================================================
# Real Sakila relationships
# ============================================================

def test_live_mysql_foreign_keys_are_source_relationships(sakila_schema):
    assert len(sakila_schema.relationships) == 22

    for relationship in sakila_schema.relationships:
        assert isinstance(relationship, SourceRelationship)
        assert relationship.relationship_type is RelationshipType.FOREIGN_KEY
        # A declared database constraint is fact, not inference.
        assert relationship.confidence == 1.0
        assert relationship.from_fields
        assert relationship.to_fields


def test_live_mysql_known_sakila_relationships_present(sakila_schema):
    """Spot-check relationships that genuinely exist in Sakila."""
    pairs = {
        (r.from_entity, tuple(r.from_fields), r.to_entity, tuple(r.to_fields))
        for r in sakila_schema.relationships
    }

    expected = {
        ("rental", ("customer_id",), "customer", ("customer_id",)),
        ("rental", ("inventory_id",), "inventory", ("inventory_id",)),
        ("rental", ("staff_id",), "staff", ("staff_id",)),
        ("payment", ("customer_id",), "customer", ("customer_id",)),
        ("payment", ("rental_id",), "rental", ("rental_id",)),
        ("payment", ("staff_id",), "staff", ("staff_id",)),
        ("customer", ("address_id",), "address", ("address_id",)),
        ("customer", ("store_id",), "store", ("store_id",)),
        ("film", ("language_id",), "language", ("language_id",)),
        ("film_actor", ("actor_id",), "actor", ("actor_id",)),
        ("city", ("country_id",), "country", ("country_id",)),
    }

    missing = expected - pairs
    assert missing == set(), f"expected Sakila relationships not discovered: {missing}"


def test_live_mysql_two_foreign_keys_to_the_same_table_are_distinct(sakila_schema):
    """film has TWO FKs to language (language_id and original_language_id);
    both must be recorded, with distinct relationship ids."""
    to_language = [
        r for r in sakila_schema.relationships
        if r.from_entity == "film" and r.to_entity == "language"
    ]

    assert len(to_language) == 2
    assert len({r.relationship_id for r in to_language}) == 2
    assert {r.from_fields[0] for r in to_language} == {"language_id", "original_language_id"}


def test_live_mysql_indexes_discovered(sakila_schema):
    total_indexes = sum(
        len(e.metadata.get("indexes", [])) for e in sakila_schema.entities
    )
    assert total_indexes > 0

    rental = sakila_schema.entity_by_normalized_name("rental")
    assert len(rental.metadata.get("indexes", [])) > 0


def test_live_mysql_composite_unique_constraint_preserved(sakila_schema):
    """rental has a composite UNIQUE on (rental_date, inventory_id, customer_id).
    Its member columns must NOT be marked individually unique."""
    rental = sakila_schema.entity_by_normalized_name("rental")
    composite = rental.metadata.get("composite_unique_constraints", [])

    assert len(composite) == 1
    assert set(composite[0]["columns"]) == {"rental_date", "inventory_id", "customer_id"}

    for column_name in ("rental_date", "inventory_id", "customer_id"):
        assert rental.field_by_normalized_name(column_name).is_unique is False


# ============================================================
# Determinism
# ============================================================

def test_live_mysql_discovery_is_deterministic(sakila_connector, sakila_schema):
    second = discover_schema(sakila_connector)

    assert second.compute_schema_hash() == sakila_schema.compute_schema_hash()
    assert second.schema_id == sakila_schema.schema_id

    first_payload = sakila_schema.to_json_dict()
    second_payload = second.to_json_dict()
    # created_at records object-construction time, not structural identity.
    first_payload.pop("created_at")
    second_payload.pop("created_at")
    assert first_payload == second_payload


def test_live_mysql_stored_hash_matches_recomputed(sakila_schema):
    assert sakila_schema.schema_hash == sakila_schema.compute_schema_hash()


def test_live_mysql_discovery_produces_no_blocking_warnings(sakila_connector):
    discovery = RelationalSchemaDiscovery(sakila_connector)
    schema = discovery.discover()

    # Discovery may warn (Sakila has a spatial column SQLAlchemy cannot type),
    # but it must still produce a complete structural result.
    assert len(schema.entities) == 16


# ============================================================
# Phase 2 catalog integration
# ============================================================

def test_live_mysql_schema_publishes_and_is_idempotent(sakila_connector):
    """Discover Sakila -> publish -> version 1; rediscover unchanged ->
    still version 1. Uses an isolated catalog source id and cleans up."""
    from sqlalchemy import text

    from erp_pipeline.catalog.config import CatalogDatabaseSettings
    from erp_pipeline.catalog.repository import CatalogRepository
    from erp_pipeline.catalog.schema import bootstrap_catalog
    from erp_pipeline.catalog.service import SchemaCatalogService
    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.connectors.mysql import MySQLConnector
    from erp_pipeline.schemas.source_models import SourceSystem

    try:
        catalog_settings = CatalogDatabaseSettings.from_env()
        engine = catalog_settings.create_engine()
        bootstrap_catalog(engine)
    except Exception as exc:  # noqa: BLE001 - availability probe
        pytest.skip(f"Catalog PostgreSQL unavailable: {exc}")

    catalog_source_id = "mysql_sakila_pytest"
    catalog = SchemaCatalogService(CatalogRepository(engine))

    def cleanup():
        with engine.begin() as connection:
            connection.execute(text("""
                DELETE FROM erp_catalog.source_fields WHERE schema_id IN (
                    SELECT schema_id FROM erp_catalog.schema_snapshots
                    WHERE source_system_id = :sid)"""), {"sid": catalog_source_id})
            for table in ("source_entities", "source_relationships"):
                connection.execute(text(f"""
                    DELETE FROM erp_catalog.{table} WHERE schema_id IN (
                        SELECT schema_id FROM erp_catalog.schema_snapshots
                        WHERE source_system_id = :sid)"""), {"sid": catalog_source_id})
            connection.execute(text(
                "DELETE FROM erp_catalog.schema_snapshots WHERE source_system_id = :sid"),
                {"sid": catalog_source_id})
            connection.execute(text(
                "DELETE FROM erp_catalog.source_systems WHERE source_system_id = :sid"),
                {"sid": catalog_source_id})

    cleanup()
    settings = sakila_connector._settings  # noqa: SLF001 - reuse verified settings
    isolated = ConnectionSettings(
        source_system_id=catalog_source_id,
        source_type=SourceType.MYSQL,
        host=settings.host, port=settings.port, database=settings.database,
        username=settings.username, password=settings.password,
        connect_timeout_seconds=settings.connect_timeout_seconds,
    )

    try:
        catalog.register_source_system(SourceSystem(
            source_system_id=catalog_source_id,
            name="Sakila MySQL (pytest live verification)",
            source_type=SourceType.MYSQL,
            environment="research",
        ))

        with MySQLConnector(isolated) as connector:
            first_schema = discover_schema(connector)
            first = catalog.publish_schema(first_schema)

            second_schema = discover_schema(connector)
            second = catalog.publish_schema(second_schema)

        assert first.created is True
        assert first.record.catalog_version == 1

        # An unchanged Sakila must NOT produce catalog version 2.
        assert second.created is False
        assert second.record.catalog_version == 1

        history = catalog.history(catalog_source_id, first_schema.schema_name)
        assert [record.catalog_version for record in history] == [1]

        summary = catalog.summarize(first_schema.schema_id)
        assert summary.entity_count == 16
        assert summary.field_count == 90
        assert summary.relationship_count == 22

        # Full structural round trip through the catalog.
        retrieved = catalog.get_snapshot(first_schema.schema_id)
        assert retrieved.compute_schema_hash() == first_schema.compute_schema_hash()
    finally:
        cleanup()
        engine.dispose()
