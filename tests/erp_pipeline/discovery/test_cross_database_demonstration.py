"""PHASE 4 CROSS-DATABASE DEMONSTRATION.

Three ERP systems describe invoices with three completely different table and
column vocabularies:

    PostgreSQL   fin_invoice        INV_NO / CLIENT_REF / TOTAL / APPROVAL
    MySQL        invoices           invoice_number / customer_id / amount / status
    SQL Server   dbo.InvoiceHeader  InvoiceNumber / CustomerCode / InvoiceValue / ApprovedFlag

This module proves all three are produced BY THE ACTUAL PHASE 4 DISCOVERY
CODE (never hand-constructed) and all three are instances of exactly the same
generic Phase 1 contract.

Mapping these to one canonical invoice shape is Phase 8 and is deliberately
absent here - the point of this demonstration is that the STRUCTURES converge
on one contract while the source vocabularies stay faithfully different.

The MySQL and SQL Server cases run against a FakeInspector reproducing exactly
what those dialects return, because no MySQL/SQL Server research database is
configured in this environment. The PostgreSQL case is additionally proven
against a real live database in test_live_postgresql_discovery.py.
"""

import pytest
from sqlalchemy import types as sqltypes
from sqlalchemy.dialects import mssql, mysql

from erp_pipeline.discovery.relational import discover_schema
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, RelationshipType, SchemaOrigin, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceRelationship, SourceSchema

from tests.erp_pipeline.discovery.fakes import FakeInspector, FakeRelationalConnector, column


# ============================================================
# A: PostgreSQL
# ============================================================

def postgresql_connector():
    inspector = FakeInspector(
        schema_names=["public"],
        tables={"public": ["fin_invoice", "fin_client"]},
        columns={
            ("public", "fin_invoice"): [
                column("INV_NO", sqltypes.String(20), nullable=False),
                column("CLIENT_REF", sqltypes.String(20)),
                column("TOTAL", sqltypes.Numeric(18, 2)),
                column("APPROVAL", sqltypes.String(20)),
            ],
            ("public", "fin_client"): [
                column("CLIENT_REF", sqltypes.String(20), nullable=False),
            ],
        },
        primary_keys={
            ("public", "fin_invoice"): {"constrained_columns": ["INV_NO"]},
            ("public", "fin_client"): {"constrained_columns": ["CLIENT_REF"]},
        },
        foreign_keys={
            ("public", "fin_invoice"): [
                {
                    "name": "fk_invoice_client",
                    "constrained_columns": ["CLIENT_REF"],
                    "referred_schema": "public",
                    "referred_table": "fin_client",
                    "referred_columns": ["CLIENT_REF"],
                }
            ]
        },
        dialect_name="postgresql",
    )
    return FakeRelationalConnector(
        inspector, SourceType.POSTGRESQL, "finance_erp_pg", "finance_db"
    )


# ============================================================
# B: MySQL
# ============================================================

def mysql_connector():
    inspector = FakeInspector(
        tables={None: ["invoices", "customers"]},
        columns={
            (None, "invoices"): [
                column("invoice_number", mysql.VARCHAR(32), nullable=False),
                column("customer_id", mysql.INTEGER()),
                column("amount", mysql.DECIMAL(12, 2)),
                column("status", mysql.ENUM("new", "approved", "rejected")),
            ],
            (None, "customers"): [
                column("customer_id", mysql.INTEGER(), nullable=False),
            ],
        },
        primary_keys={
            (None, "invoices"): {"constrained_columns": ["invoice_number"]},
            (None, "customers"): {"constrained_columns": ["customer_id"]},
        },
        foreign_keys={
            (None, "invoices"): [
                {
                    "name": "fk_invoices_customers",
                    "constrained_columns": ["customer_id"],
                    "referred_schema": None,
                    "referred_table": "customers",
                    "referred_columns": ["customer_id"],
                }
            ]
        },
        dialect_name="mysql",
    )
    return FakeRelationalConnector(inspector, SourceType.MYSQL, "ops_erp_mysql", "ops_db")


# ============================================================
# C: SQL Server
# ============================================================

def sqlserver_connector():
    inspector = FakeInspector(
        schema_names=["dbo"],
        tables={"dbo": ["InvoiceHeader", "Customer"]},
        columns={
            ("dbo", "InvoiceHeader"): [
                column("InvoiceNumber", mssql.NVARCHAR(50), nullable=False),
                column("CustomerCode", mssql.NVARCHAR(50)),
                column("InvoiceValue", mssql.MONEY()),
                column("ApprovedFlag", mssql.BIT()),
            ],
            ("dbo", "Customer"): [
                column("CustomerCode", mssql.NVARCHAR(50), nullable=False),
            ],
        },
        primary_keys={
            ("dbo", "InvoiceHeader"): {"constrained_columns": ["InvoiceNumber"]},
            ("dbo", "Customer"): {"constrained_columns": ["CustomerCode"]},
        },
        foreign_keys={
            ("dbo", "InvoiceHeader"): [
                {
                    "name": "FK_InvoiceHeader_Customer",
                    "constrained_columns": ["CustomerCode"],
                    "referred_schema": "dbo",
                    "referred_table": "Customer",
                    "referred_columns": ["CustomerCode"],
                }
            ]
        },
        dialect_name="mssql",
    )
    return FakeRelationalConnector(
        inspector, SourceType.SQL_SERVER, "corp_erp_mssql", "corp_db"
    )


ALL_ENGINES = {
    "postgresql": postgresql_connector,
    "mysql": mysql_connector,
    "sql_server": sqlserver_connector,
}


def _discover(factory):
    connector = factory()
    try:
        return discover_schema(connector)
    finally:
        connector.close()


# ============================================================
# The proof
# ============================================================

def test_all_three_engines_produce_a_source_schema():
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}

    for name, schema in schemas.items():
        assert isinstance(schema, SourceSchema), name
        assert schema.origin is SchemaOrigin.DISCOVERED, name
        assert all(isinstance(e, SourceEntity) for e in schema.entities), name
        assert all(isinstance(f, SourceField) for e in schema.entities for f in e.fields), name
        assert all(isinstance(r, SourceRelationship) for r in schema.relationships), name


def test_all_three_serialize_through_an_identical_contract_shape():
    payloads = {name: _discover(factory).to_json_dict() for name, factory in ALL_ENGINES.items()}

    shapes = [set(payload) for payload in payloads.values()]
    assert all(shape == shapes[0] for shape in shapes)

    entity_shapes = [
        set(payload["entities"][0]) for payload in payloads.values()
    ]
    assert all(shape == entity_shapes[0] for shape in entity_shapes)

    field_shapes = [
        set(payload["entities"][0]["fields"][0]) for payload in payloads.values()
    ]
    assert all(shape == field_shapes[0] for shape in field_shapes)


def test_source_vocabularies_stay_faithfully_different():
    """Discovery must NOT normalize away the sources' own naming - that is
    Phase 8's job, not Phase 4's."""
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}

    postgres_invoice = schemas["postgresql"].entity_by_normalized_name("public.fin_invoice")
    mysql_invoice = schemas["mysql"].entity_by_normalized_name("invoices")
    sqlserver_invoice = schemas["sql_server"].entity_by_normalized_name("dbo.invoiceheader")

    assert [f.source_name for f in postgres_invoice.fields] == [
        "INV_NO", "CLIENT_REF", "TOTAL", "APPROVAL",
    ]
    assert [f.source_name for f in mysql_invoice.fields] == [
        "invoice_number", "customer_id", "amount", "status",
    ]
    assert [f.source_name for f in sqlserver_invoice.fields] == [
        "InvoiceNumber", "CustomerCode", "InvoiceValue", "ApprovedFlag",
    ]


def test_each_engine_preserves_its_own_vendor_datatypes():
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}

    postgres_total = schemas["postgresql"].entity_by_normalized_name(
        "public.fin_invoice"
    ).field_by_normalized_name("total")
    mysql_amount = schemas["mysql"].entity_by_normalized_name(
        "invoices"
    ).field_by_normalized_name("amount")
    sqlserver_value = schemas["sql_server"].entity_by_normalized_name(
        "dbo.invoiceheader"
    ).field_by_normalized_name("invoicevalue")

    # Three different vendor spellings...
    assert "NUMERIC" in postgres_total.source_data_type.upper()
    assert "DECIMAL" in mysql_amount.source_data_type.upper()
    assert "MONEY" in sqlserver_value.source_data_type.upper()

    # ...normalizing to the same common type.
    assert postgres_total.normalized_data_type is FieldDataType.DECIMAL
    assert mysql_amount.normalized_data_type is FieldDataType.DECIMAL
    assert sqlserver_value.normalized_data_type is FieldDataType.DECIMAL


def test_sqlserver_bit_and_mysql_enum_normalize_correctly():
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}

    approved_flag = schemas["sql_server"].entity_by_normalized_name(
        "dbo.invoiceheader"
    ).field_by_normalized_name("approvedflag")
    assert approved_flag.normalized_data_type is FieldDataType.BOOLEAN

    status = schemas["mysql"].entity_by_normalized_name(
        "invoices"
    ).field_by_normalized_name("status")
    assert status.normalized_data_type is FieldDataType.STRING


def test_namespace_semantics_differ_correctly_per_engine():
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}

    assert schemas["postgresql"].entities[0].namespace == "public"
    assert schemas["sql_server"].entities[0].namespace == "dbo"
    # MySQL has no namespace level inside a database.
    assert schemas["mysql"].entities[0].namespace is None


def test_every_engine_discovers_its_primary_key_and_foreign_key():
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}

    for name, schema in schemas.items():
        assert len(schema.relationships) == 1, name
        relationship = schema.relationships[0]
        assert relationship.relationship_type is RelationshipType.FOREIGN_KEY, name
        assert relationship.confidence == 1.0, name

        invoice_entity = next(
            e for e in schema.entities if "invoice" in e.normalized_name.lower()
        )
        assert len(invoice_entity.primary_key_fields) == 1, name


def test_identities_do_not_collide_across_source_systems():
    schemas = {name: _discover(factory) for name, factory in ALL_ENGINES.items()}

    schema_ids = {schema.schema_id for schema in schemas.values()}
    assert len(schema_ids) == 3

    entity_ids = {
        entity.entity_id for schema in schemas.values() for entity in schema.entities
    }
    assert len(entity_ids) == 6  # 2 entities x 3 engines, all distinct


def test_no_engine_specific_public_model_exists():
    """Step 36: there must be no PostgresTable/MySQLTable/SQLServerTable."""
    import erp_pipeline.discovery as discovery

    forbidden = {"PostgresTable", "MySQLTable", "SQLServerTable", "PostgresSchema"}
    assert not (set(dir(discovery)) & forbidden)
