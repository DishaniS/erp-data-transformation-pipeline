"""Step 40: API specifications -> the Phase 2 Schema Catalog.

Both formats publish through the existing catalog with no architectural hack:
``SourceType.OPENAPI`` / ``SourceType.POSTMAN`` and ``SchemaOrigin.API_SPEC``
are already part of the frozen Phase 1 vocabulary, and the catalog's columns
carry no relational-only constraint.

Phase 7 owns none of the versioning logic; these tests verify the handoff, as
the Phase 4/5/6 equivalents do for their sources. The controlled-change tests
edit a COPY of a fixture in ``tmp_path`` - the committed fixtures are never
modified.
"""

from __future__ import annotations

import json

import pytest

from erp_pipeline.api_specs import (
    ApiSpecFormat,
    ApiSpecificationService,
    ApiSpecOptions,
    parse_api_spec,
)
from erp_pipeline.catalog.repository import CatalogRepository
from erp_pipeline.catalog.schema import bootstrap_catalog
from erp_pipeline.catalog.service import SchemaCatalogService
from erp_pipeline.catalog.versioning import compare_schemas
from erp_pipeline.schemas.enums import SchemaOrigin, SourceType

OPENAPI_SYSTEM = "api_spec_openapi_probe"
POSTMAN_SYSTEM = "api_spec_postman_probe"


@pytest.fixture()
def catalog(pipeline_connector):
    engine = pipeline_connector._sqlalchemy_engine  # noqa: SLF001 - test setup
    bootstrap_catalog(engine)
    return SchemaCatalogService(CatalogRepository(engine))


def _cleanup(catalog, source_system_id: str) -> None:
    from sqlalchemy import text

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
            {"sid": source_system_id},
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
                {"sid": source_system_id},
            )
        connection.execute(
            text(
                "DELETE FROM erp_catalog.schema_snapshots "
                "WHERE source_system_id = :sid"
            ),
            {"sid": source_system_id},
        )
        connection.execute(
            text("DELETE FROM erp_catalog.source_systems WHERE source_system_id = :sid"),
            {"sid": source_system_id},
        )


@pytest.fixture()
def openapi_catalog(catalog):
    options = ApiSpecOptions(source_system_id=OPENAPI_SYSTEM)
    service = ApiSpecificationService(options)
    catalog.register_source_system(
        service.source_system(ApiSpecFormat.OPENAPI, name="OpenAPI probe")
    )

    yield catalog, options

    _cleanup(catalog, OPENAPI_SYSTEM)


@pytest.fixture()
def postman_catalog(catalog):
    options = ApiSpecOptions(source_system_id=POSTMAN_SYSTEM)
    service = ApiSpecificationService(options)
    catalog.register_source_system(
        service.source_system(ApiSpecFormat.POSTMAN, name="Postman probe")
    )

    yield catalog, options

    _cleanup(catalog, POSTMAN_SYSTEM)


def _copy_fixture(spec_fixtures, tmp_path, name: str, target: str):
    """Work on a COPY so the committed fixture is never edited."""
    destination = tmp_path / target
    destination.write_bytes((spec_fixtures / name).read_bytes())
    return destination


# ============================================================
# Source systems and round trip
# ============================================================

def test_both_api_source_types_register_cleanly(openapi_catalog, postman_catalog):
    catalog, _ = openapi_catalog
    postman, _ = postman_catalog

    assert catalog.repository.get_source_system(OPENAPI_SYSTEM).source_type is (
        SourceType.OPENAPI
    )
    assert postman.repository.get_source_system(POSTMAN_SYSTEM).source_type is (
        SourceType.POSTMAN
    )


def test_an_openapi_schema_round_trips(spec_fixtures, openapi_catalog):
    catalog, options = openapi_catalog
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json", options)

    catalog.publish_schema(result.schema)
    retrieved = catalog.get_snapshot(result.schema.schema_id)

    published = result.schema.to_json_dict()
    stored = retrieved.to_json_dict()
    published.pop("created_at")
    stored.pop("created_at")

    assert stored == published
    assert retrieved.origin is SchemaOrigin.API_SPEC
    assert retrieved.compute_schema_hash() == result.schema.compute_schema_hash()


def test_nested_paths_types_and_relationships_survive(spec_fixtures, openapi_catalog):
    catalog, options = openapi_catalog
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json", options)
    catalog.publish_schema(result.schema)

    retrieved = catalog.get_snapshot(result.schema.schema_id)
    invoice = retrieved.entity_by_normalized_name("invoice")

    assert invoice.field_by_normalized_name("customer.contact.email").nested_path == (
        "customer", "contact",
    )
    assert invoice.field_by_normalized_name("lines_.sku").nested_path == (
        "lines", "[]",
    )
    assert retrieved.relationships


def test_a_postman_schema_round_trips(spec_fixtures, postman_catalog):
    catalog, options = postman_catalog
    result = parse_api_spec(spec_fixtures / "postman_response_examples.json", options)

    catalog.publish_schema(result.schema)
    retrieved = catalog.get_snapshot(result.schema.schema_id)

    assert retrieved.origin is SchemaOrigin.INFERRED
    assert retrieved.compute_schema_hash() == result.schema.compute_schema_hash()


def test_the_operation_index_survives_the_round_trip(spec_fixtures, openapi_catalog):
    """Step 20: the endpoint -> structure linkage must reach the catalog."""
    catalog, options = openapi_catalog
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json", options)
    catalog.publish_schema(result.schema)

    retrieved = catalog.get_snapshot(result.schema.schema_id)
    index = {
        entry["operation_key"]: entry
        for entry in retrieved.metadata["operations"]
    }

    assert index["post.invoices"]["request_entity_ids"] == ["createinvoicerequest"]
    assert index["get.invoices_id"]["response_entity_ids"] == ["invoice", "problem"]


def test_no_secret_reaches_the_catalog(spec_fixtures, postman_catalog):
    from tests.erp_pipeline.api_specs.conftest import SECRETS

    catalog, options = postman_catalog
    result = parse_api_spec(spec_fixtures / "postman_auth_secrets.json", options)
    catalog.publish_schema(result.schema)

    stored = json.dumps(
        catalog.get_snapshot(result.schema.schema_id).to_json_dict(), default=str
    )
    for secret in SECRETS:
        assert secret not in stored


# ============================================================
# Idempotency (Step 40)
# ============================================================

def test_reparsing_an_unchanged_openapi_spec_creates_no_new_version(
    spec_fixtures, openapi_catalog
):
    catalog, options = openapi_catalog
    path = spec_fixtures / "openapi_3_basic.json"

    first = catalog.publish_schema(parse_api_spec(path, options).schema)
    second = catalog.publish_schema(parse_api_spec(path, options).schema)

    assert first.created is True
    assert first.record.catalog_version == 1
    assert second.created is False
    assert second.record.catalog_version == 1


def test_reparsing_an_unchanged_postman_collection_creates_no_new_version(
    spec_fixtures, postman_catalog
):
    catalog, options = postman_catalog
    path = spec_fixtures / "postman_response_examples.json"

    first = catalog.publish_schema(parse_api_spec(path, options).schema)
    second = catalog.publish_schema(parse_api_spec(path, options).schema)

    assert first.record.catalog_version == 1
    assert second.created is False
    assert second.record.catalog_version == 1


def test_a_description_change_alone_does_not_create_a_version(
    spec_fixtures, tmp_path, openapi_catalog
):
    """Step 38: documentation edits are not structural changes."""
    catalog, options = openapi_catalog
    path = _copy_fixture(spec_fixtures, tmp_path, "openapi_3_basic.json", "spec.json")

    first = catalog.publish_schema(parse_api_spec(path, options).schema)

    document = json.loads(path.read_text(encoding="utf-8"))
    document["info"]["description"] = "Completely rewritten prose."
    document["paths"]["/invoices"]["get"]["summary"] = "A new summary."
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    second = catalog.publish_schema(parse_api_spec(path, options).schema)

    assert first.record.catalog_version == 1
    assert second.created is False
    assert second.record.catalog_version == 1


# ============================================================
# Controlled structural change (Step 40)
# ============================================================

def test_a_new_declared_field_creates_catalog_version_two(
    spec_fixtures, tmp_path, openapi_catalog
):
    catalog, options = openapi_catalog
    path = _copy_fixture(spec_fixtures, tmp_path, "openapi_3_basic.json", "spec.json")

    v1 = parse_api_spec(path, options).schema
    v1_result = catalog.publish_schema(v1)

    document = json.loads(path.read_text(encoding="utf-8"))
    document["components"]["schemas"]["Invoice"]["properties"]["currency"] = {
        "type": "string"
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    v2 = parse_api_spec(path, options).schema
    v2_result = catalog.publish_schema(v2)

    assert v1.compute_schema_hash() != v2.compute_schema_hash()
    assert v1.schema_name == v2.schema_name  # same logical scope
    assert v1_result.record.catalog_version == 1
    assert v2_result.created is True
    assert v2_result.record.catalog_version == 2

    diff = compare_schemas(v1, v2)
    assert ("invoice", "currency") in diff.added_fields


def test_a_declared_type_change_is_reported(spec_fixtures, tmp_path, openapi_catalog):
    catalog, options = openapi_catalog
    path = _copy_fixture(spec_fixtures, tmp_path, "openapi_3_basic.json", "spec.json")

    v1 = parse_api_spec(path, options).schema
    catalog.publish_schema(v1)

    document = json.loads(path.read_text(encoding="utf-8"))
    document["components"]["schemas"]["Invoice"]["properties"]["lineCount"] = {
        "type": "string"
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    v2 = parse_api_spec(path, options).schema
    catalog.publish_schema(v2)

    changed = {(c.field, c.attribute) for c in compare_schemas(v1, v2).changed_fields}
    assert ("linecount", "normalized_data_type") in changed


def test_a_new_endpoint_schema_appears_as_an_added_entity(
    spec_fixtures, tmp_path, openapi_catalog
):
    catalog, options = openapi_catalog
    path = _copy_fixture(spec_fixtures, tmp_path, "openapi_3_basic.json", "spec.json")

    v1 = parse_api_spec(path, options).schema
    catalog.publish_schema(v1)

    document = json.loads(path.read_text(encoding="utf-8"))
    document["components"]["schemas"]["Payment"] = {
        "type": "object",
        "properties": {"paymentId": {"type": "string"}},
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    v2 = parse_api_spec(path, options).schema
    v2_result = catalog.publish_schema(v2)

    assert v2_result.record.catalog_version == 2
    assert "payment" in compare_schemas(v1, v2).added_entities


def test_a_new_observed_postman_field_creates_version_two(
    spec_fixtures, tmp_path, postman_catalog
):
    """The Postman equivalent: a saved example gains a field."""
    catalog, options = postman_catalog
    path = _copy_fixture(
        spec_fixtures, tmp_path, "postman_response_examples.json", "collection.json"
    )

    v1 = parse_api_spec(path, options).schema
    v1_result = catalog.publish_schema(v1)

    document = json.loads(path.read_text(encoding="utf-8"))
    saved = json.loads(document["item"][0]["response"][0]["body"])
    saved["discountCode"] = "SPRING"
    document["item"][0]["response"][0]["body"] = json.dumps(saved)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    v2 = parse_api_spec(path, options).schema
    v2_result = catalog.publish_schema(v2)

    assert v1_result.record.catalog_version == 1
    assert v2_result.created is True
    assert v2_result.record.catalog_version == 2
    assert ("get_invoice_response_200", "discountcode") in (
        compare_schemas(v1, v2).added_fields
    )


def test_history_lists_both_versions_in_order(
    spec_fixtures, tmp_path, openapi_catalog
):
    catalog, options = openapi_catalog
    path = _copy_fixture(spec_fixtures, tmp_path, "openapi_3_basic.json", "spec.json")

    catalog.publish_schema(parse_api_spec(path, options).schema)

    document = json.loads(path.read_text(encoding="utf-8"))
    document["components"]["schemas"]["Invoice"]["properties"]["currency"] = {
        "type": "string"
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    catalog.publish_schema(parse_api_spec(path, options).schema)

    history = catalog.history(OPENAPI_SYSTEM, "spec")
    assert [record.catalog_version for record in history] == [1, 2]


def test_the_service_helper_parses_and_publishes(spec_fixtures, openapi_catalog):
    catalog, options = openapi_catalog
    service = ApiSpecificationService(options)

    result, snapshot = service.parse_and_publish(
        spec_fixtures / "openapi_3_basic.json", catalog
    )

    assert snapshot.record.catalog_version == 1
    assert result.schema.schema_id == snapshot.record.schema_id


def test_the_catalog_summary_reflects_the_parsed_counts(
    spec_fixtures, openapi_catalog
):
    catalog, options = openapi_catalog
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json", options)
    catalog.publish_schema(result.schema)

    summary = catalog.summarize(result.schema.schema_id)

    assert summary.entity_count == len(result.schema.entities)
    assert summary.relationship_count == len(result.schema.relationships)
    assert summary.source_type == "openapi"
