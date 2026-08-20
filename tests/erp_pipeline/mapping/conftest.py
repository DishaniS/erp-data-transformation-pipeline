"""Shared fixtures for Phase 8: the same ERP concepts from six technologies.

These are ``SourceSchema`` objects in exactly the shape Phases 4-7 produce -
including their real quirks: MongoDB's nested paths, CSV's abbreviations and
UNKNOWN types, OpenAPI's camelCase, Postman's inferred types. The mapping
engine sees nothing but these, which is what makes the source-independence
claim testable.

Each schema also carries a synthetic secret in a place a careless engine might
read (an entity description, field metadata). Phase 8 works on schemas rather
than data, so these should never surface anywhere - see
``test_mapping_boundary.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SchemaOrigin,
    SourceType,
)
from erp_pipeline.schemas.identity import normalize_identifier
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema

#: Planted where a schema can carry free text. Must never reach a candidate,
#: a profile, a log or an error.
SECRET_DESCRIPTION = "SECRET_SCHEMA_NOTE_55012"
SECRET_METADATA = "SECRET_FIELD_SAMPLE_88431"
SECRETS: tuple[str, ...] = (SECRET_DESCRIPTION, SECRET_METADATA)


def make_field(
    name: str,
    data_type: FieldDataType,
    *,
    path: tuple[str, ...] | None = None,
    nullable: bool = True,
    required: bool = False,
    is_array: bool = False,
    source_data_type: str | None = None,
    with_secret: bool = False,
) -> SourceField:
    """Build a ``SourceField`` the way the discovery phases do."""
    full_path = ".".join(list(path or ()) + [name])

    metadata = {"source_column_name": name}
    if with_secret:
        # A field whose metadata carries an example value, as a Postman- or
        # Mongo-derived schema legitimately might.
        metadata["observed_sample_note"] = SECRET_METADATA

    return SourceField(
        source_name=name,
        normalized_name=normalize_identifier(full_path),
        source_data_type=source_data_type,
        normalized_data_type=data_type,
        nullable=nullable,
        required=required,
        is_array=is_array,
        nested_path=path,
        metadata=metadata,
    )


def make_schema(
    source_system_id: str,
    source_type: SourceType,
    origin: SchemaOrigin,
    entities: tuple[SourceEntity, ...],
    schema_name: str = "main",
) -> SourceSchema:
    return SourceSchema(
        schema_id=normalize_identifier(f"{source_system_id}.{schema_name}.v1"),
        source_system_id=source_system_id,
        schema_name=schema_name,
        origin=origin,
        entities=entities,
        schema_hash="0" * 64,
        metadata={"engine": source_type.value},
    )


def make_entity(
    name: str,
    fields: tuple[SourceField, ...],
    *,
    kind: EntityKind = EntityKind.TABLE,
    with_secret: bool = False,
) -> SourceEntity:
    return SourceEntity(
        entity_id=normalize_identifier(f"entity.{name}"),
        source_name=name,
        normalized_name=normalize_identifier(name),
        entity_kind=kind,
        fields=fields,
        description=SECRET_DESCRIPTION if with_secret else None,
    )


# ============================================================
# The same ERP concepts, six ways (Step 38)
# ============================================================

@pytest.fixture()
def postgres_schema() -> SourceSchema:
    """Relational, snake_case, fully typed - the easy case."""
    return make_schema(
        "finance_erp_pg", SourceType.POSTGRESQL, SchemaOrigin.DISCOVERED,
        entities=(
            make_entity(
                "fin_invoice",
                (
                    make_field("invoice_no", FieldDataType.STRING, nullable=False,
                               required=True, source_data_type="VARCHAR(20)"),
                    make_field("customer_id", FieldDataType.STRING,
                               source_data_type="VARCHAR(20)"),
                    make_field("total_amount", FieldDataType.DECIMAL,
                               source_data_type="NUMERIC(18, 2)"),
                    make_field("currency_code", FieldDataType.STRING),
                    make_field("approval_status", FieldDataType.STRING),
                    make_field("issue_date", FieldDataType.DATE),
                    make_field("legacy_internal_flag_74", FieldDataType.INTEGER),
                ),
            ),
            make_entity(
                "fin_customer",
                (
                    make_field("customer_id", FieldDataType.STRING, nullable=False,
                               required=True),
                    make_field("customer_name", FieldDataType.STRING),
                    make_field("email", FieldDataType.STRING),
                    make_field("phone_number", FieldDataType.STRING),
                ),
                with_secret=True,
            ),
        ),
    )


@pytest.fixture()
def mysql_schema() -> SourceSchema:
    """Relational, camelCase columns and terser names."""
    return make_schema(
        "ops_erp_mysql", SourceType.MYSQL, SchemaOrigin.DISCOVERED,
        entities=(
            make_entity(
                "invoices",
                (
                    make_field("invoiceId", FieldDataType.STRING, nullable=False),
                    make_field("customerId", FieldDataType.INTEGER),
                    make_field("total", FieldDataType.DECIMAL),
                    make_field("status", FieldDataType.STRING),
                    make_field("issuedAt", FieldDataType.DATETIME),
                ),
            ),
            make_entity(
                "customers",
                (
                    make_field("customerId", FieldDataType.INTEGER, nullable=False),
                    make_field("fullName", FieldDataType.STRING),
                    make_field("email_address", FieldDataType.STRING),
                ),
            ),
        ),
    )


@pytest.fixture()
def mongodb_schema() -> SourceSchema:
    """Document store: nested paths, an inferred schema, some UNKNOWN types."""
    return make_schema(
        "billing_erp_mongo", SourceType.MONGODB, SchemaOrigin.INFERRED,
        entities=(
            make_entity(
                "invoices",
                (
                    make_field("_id", FieldDataType.STRING, nullable=False,
                               source_data_type="objectId"),
                    make_field("invoice", FieldDataType.STRING),
                    make_field("id", FieldDataType.INTEGER, path=("customer",)),
                    make_field("email", FieldDataType.STRING,
                               path=("customer", "contact")),
                    make_field("total", FieldDataType.DECIMAL,
                               path=("financial",)),
                    make_field("approved", FieldDataType.BOOLEAN),
                    make_field("note", FieldDataType.UNKNOWN, with_secret=True),
                ),
                kind=EntityKind.COLLECTION,
            ),
        ),
    )


@pytest.fixture()
def csv_schema() -> SourceSchema:
    """A flat export with abbreviated headers and an uninformative dataset name."""
    return make_schema(
        "erp_csv_export", SourceType.CSV, SchemaOrigin.INFERRED,
        entities=(
            make_entity(
                "customer_export",
                (
                    make_field("cust_no", FieldDataType.STRING),
                    make_field("cust_name", FieldDataType.STRING),
                    make_field("email_addr", FieldDataType.STRING),
                    make_field("tel", FieldDataType.STRING),
                ),
                kind=EntityKind.DATASET,
            ),
            make_entity(
                "invoice_export",
                (
                    make_field("inv_no", FieldDataType.STRING),
                    make_field("cust_no", FieldDataType.STRING),
                    make_field("total_amt", FieldDataType.DECIMAL),
                    make_field("ccy", FieldDataType.STRING),
                    make_field("stat", FieldDataType.STRING),
                ),
                kind=EntityKind.DATASET,
            ),
        ),
    )


@pytest.fixture()
def openapi_schema() -> SourceSchema:
    """A declared API contract: camelCase, precise types."""
    return make_schema(
        "erp_api_openapi", SourceType.OPENAPI, SchemaOrigin.API_SPEC,
        entities=(
            make_entity(
                "Invoice",
                (
                    make_field("invoiceId", FieldDataType.STRING, nullable=False),
                    make_field("customerId", FieldDataType.STRING),
                    make_field("totalAmount", FieldDataType.DECIMAL,
                               source_data_type="number(double)"),
                    make_field("currency", FieldDataType.STRING),
                    make_field("status", FieldDataType.STRING),
                    make_field("issuedOn", FieldDataType.DATE,
                               source_data_type="string(date)"),
                ),
                kind=EntityKind.API_SCHEMA,
            ),
            make_entity(
                "Customer",
                (
                    make_field("customerId", FieldDataType.STRING, nullable=False),
                    make_field("displayName", FieldDataType.STRING),
                    make_field("email", FieldDataType.STRING,
                               path=("contact",)),
                ),
                kind=EntityKind.API_SCHEMA,
            ),
        ),
    )


@pytest.fixture()
def postman_schema() -> SourceSchema:
    """Structure inferred from saved examples: mixed and UNKNOWN types."""
    return make_schema(
        "erp_api_postman", SourceType.POSTMAN, SchemaOrigin.INFERRED,
        entities=(
            make_entity(
                "Get Invoice_response_200",
                (
                    make_field("customer_id", FieldDataType.STRING),
                    make_field("emailAddress", FieldDataType.STRING),
                    make_field("amount", FieldDataType.INTEGER),
                    make_field("invoice_id", FieldDataType.STRING,
                               with_secret=True),
                ),
                kind=EntityKind.API_SCHEMA,
            ),
        ),
    )


@pytest.fixture()
def all_source_schemas(
    postgres_schema, mysql_schema, mongodb_schema, csv_schema,
    openapi_schema, postman_schema,
) -> dict[str, SourceSchema]:
    """Every technology, keyed by name, for the cross-source demonstration."""
    return {
        "postgresql": postgres_schema,
        "mysql": mysql_schema,
        "mongodb": mongodb_schema,
        "csv": csv_schema,
        "openapi": openapi_schema,
        "postman": postman_schema,
    }


# ============================================================
# Catalog access for the persistence tests
# ============================================================

@pytest.fixture(scope="session")
def pipeline_connector():
    """Connector to the pipeline database, for mapping-profile persistence.

    A local twin of the fixture the other suites define - pytest conftest
    files are directory-scoped. Skips, never fails, when PostgreSQL is
    unreachable.
    """
    import os

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")

    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.connectors.postgresql import PostgreSQLConnector

    password = os.getenv("AI_DB_PASSWORD")
    if not password:
        pytest.skip("AI_DB_PASSWORD is not configured in .env")

    settings = ConnectionSettings(
        source_system_id="mapping_probe",
        source_type=SourceType.POSTGRESQL,
        host=os.getenv("AI_DB_HOST", "localhost"),
        port=int(os.getenv("AI_DB_PORT", "5432")),
        database=os.getenv("AI_DB_NAME", "erp_ai_native_db"),
        username=os.getenv("AI_DB_USER", "postgres"),
        password=password,
        connect_timeout_seconds=10,
    )

    connector = PostgreSQLConnector(settings)
    try:
        connector.test_connection()
    except Exception as exc:  # noqa: BLE001 - availability probe
        connector.close()
        pytest.skip(f"Pipeline PostgreSQL unreachable: {exc}")

    yield connector
    connector.close()
