"""Live PostgreSQL discovery against real databases.

Two targets: the untouched BPI source (read-only proof against a genuine
pre-existing database), and an isolated probe schema for controlled DDL
shapes that the BPI source happens not to contain.

Skipped, never faked, when PostgreSQL is unavailable.
"""

import pytest

from erp_pipeline.discovery import DiscoveryOptions, RelationalDiscoveryService, discover_schema
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, RelationshipType, SchemaOrigin
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceRelationship, SourceSchema


# ============================================================
# The real BPI source database - READ ONLY
# ============================================================

def test_live_discovery_against_real_bpi_source(bpi_source_connector):
    schema = discover_schema(bpi_source_connector)

    assert isinstance(schema, SourceSchema)
    assert schema.origin is SchemaOrigin.DISCOVERED
    assert schema.source_system_id == "finance_erp_pg"
    assert schema.schema_name == "public"
    assert len(schema.entities) > 0

    for entity in schema.entities:
        assert isinstance(entity, SourceEntity)
        assert entity.namespace == "public"
        assert entity.normalized_name.startswith("public.")
        assert len(entity.fields) > 0
        for field in entity.fields:
            assert isinstance(field, SourceField)
            assert field.source_data_type  # vendor type preserved
            assert field.semantic_type is None  # never inferred in Phase 4


def test_live_bpi_discovery_reports_actual_absence_of_constraints(bpi_source_connector):
    """The BPI raw tables were created by pandas `to_sql` and genuinely have
    no primary or foreign keys. Discovery must report that reality rather
    than fabricating keys."""
    schema = discover_schema(bpi_source_connector)

    assert all(entity.primary_key_fields == () for entity in schema.entities)
    assert schema.relationships == ()


def test_live_bpi_discovery_is_idempotent(bpi_source_connector):
    first = discover_schema(bpi_source_connector)
    second = discover_schema(bpi_source_connector)

    assert first.schema_id == second.schema_id
    assert first.compute_schema_hash() == second.compute_schema_hash()

    # Full structural equality, excluding `created_at`: that field records
    # when the Python object was built, which is deliberately not part of
    # structural identity (compute_schema_hash excludes it for the same
    # reason). Everything else must match byte for byte.
    first_payload = first.to_json_dict()
    second_payload = second.to_json_dict()
    first_payload.pop("created_at")
    second_payload.pop("created_at")
    assert first_payload == second_payload


def test_live_bpi_discovery_excludes_system_schemas(bpi_source_connector):
    schema = discover_schema(bpi_source_connector)
    namespaces = {entity.namespace for entity in schema.entities}

    assert "pg_catalog" not in namespaces
    assert "information_schema" not in namespaces


# ============================================================
# Isolated probe schema - controlled DDL shapes
# ============================================================

PROBE_DDL = """
CREATE TABLE {schema}.customer (
    id            INTEGER PRIMARY KEY,
    email         VARCHAR(255) NOT NULL UNIQUE,
    name          VARCHAR(120),
    credit_limit  NUMERIC(12,2) DEFAULT 0,
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    manager_id    INTEGER REFERENCES {schema}.customer(id),
    tags          TEXT[],
    profile       JSONB,
    external_uuid UUID,
    raw_blob      BYTEA
);
COMMENT ON TABLE {schema}.customer IS 'Probe customer table';
COMMENT ON COLUMN {schema}.customer.email IS 'Unique contact email';

CREATE TABLE {schema}.fin_invoice (
    tenant_id   INTEGER NOT NULL,
    invoice_no  VARCHAR(20) NOT NULL,
    customer_id INTEGER NOT NULL,
    total       NUMERIC(18,2),
    approval    VARCHAR(20),
    issued_on   DATE,
    PRIMARY KEY (tenant_id, invoice_no),
    CONSTRAINT fk_invoice_customer FOREIGN KEY (customer_id)
        REFERENCES {schema}.customer(id),
    CONSTRAINT uq_invoice_customer_date UNIQUE (customer_id, issued_on)
);

CREATE TABLE {schema}.invoice_line (
    tenant_id   INTEGER NOT NULL,
    invoice_no  VARCHAR(20) NOT NULL,
    line_no     INTEGER NOT NULL,
    amount      NUMERIC(12,2),
    PRIMARY KEY (tenant_id, invoice_no, line_no),
    CONSTRAINT fk_line_invoice FOREIGN KEY (tenant_id, invoice_no)
        REFERENCES {schema}.fin_invoice(tenant_id, invoice_no)
);

CREATE TABLE {schema}.no_key_table (
    anything VARCHAR(50),
    whatever INTEGER
);

CREATE INDEX ix_invoice_approval ON {schema}.fin_invoice(approval);
"""


@pytest.fixture()
def probe_schema_discovered(pipeline_connector, probe_schema):
    schema_name, run = probe_schema
    run(PROBE_DDL.format(schema=schema_name))

    options = DiscoveryOptions(include_schemas=[schema_name])
    return discover_schema(pipeline_connector, options), schema_name


def test_live_composite_primary_key(probe_schema_discovered):
    schema, schema_name = probe_schema_discovered

    invoice = schema.entity_by_normalized_name(f"{schema_name}.fin_invoice")
    assert invoice.primary_key_fields == ("tenant_id", "invoice_no")

    line = schema.entity_by_normalized_name(f"{schema_name}.invoice_line")
    assert line.primary_key_fields == ("tenant_id", "invoice_no", "line_no")


def test_live_table_without_primary_key(probe_schema_discovered):
    schema, schema_name = probe_schema_discovered

    entity = schema.entity_by_normalized_name(f"{schema_name}.no_key_table")
    assert entity.primary_key_fields == ()
    assert entity.has_primary_key is False


def test_live_single_and_composite_and_self_reference_foreign_keys(probe_schema_discovered):
    schema, schema_name = probe_schema_discovered

    by_pair = {
        (r.from_entity, r.to_entity): r for r in schema.relationships
    }

    self_ref = by_pair[(f"{schema_name}.customer", f"{schema_name}.customer")]
    assert self_ref.from_fields == ("manager_id",)
    assert self_ref.metadata["is_self_reference"] is True

    single = by_pair[(f"{schema_name}.fin_invoice", f"{schema_name}.customer")]
    assert single.from_fields == ("customer_id",)
    assert single.to_fields == ("id",)
    assert single.relationship_type is RelationshipType.FOREIGN_KEY
    assert single.confidence == 1.0

    composite = by_pair[(f"{schema_name}.invoice_line", f"{schema_name}.fin_invoice")]
    assert composite.from_fields == ("tenant_id", "invoice_no")
    assert composite.to_fields == ("tenant_id", "invoice_no")
    assert composite.metadata["is_composite"] is True


def test_live_single_column_unique_and_composite_unique(probe_schema_discovered):
    schema, schema_name = probe_schema_discovered

    customer = schema.entity_by_normalized_name(f"{schema_name}.customer")
    assert customer.field_by_normalized_name("email").is_unique is True

    invoice = schema.entity_by_normalized_name(f"{schema_name}.fin_invoice")
    # Neither member of the composite constraint is individually unique.
    assert invoice.field_by_normalized_name("customer_id").is_unique is False
    assert invoice.field_by_normalized_name("issued_on").is_unique is False

    composite = invoice.metadata["composite_unique_constraints"]
    assert len(composite) == 1
    assert composite[0]["columns"] == ["customer_id", "issued_on"]


def test_live_index_discovery(probe_schema_discovered):
    schema, schema_name = probe_schema_discovered

    invoice = schema.entity_by_normalized_name(f"{schema_name}.fin_invoice")
    index_names = {index["name"] for index in invoice.metadata["indexes"]}
    assert "ix_invoice_approval" in index_names


def test_live_defaults_and_nullability(probe_schema_discovered):
    schema, schema_name = probe_schema_discovered

    customer = schema.entity_by_normalized_name(f"{schema_name}.customer")
    fields = {f.normalized_name: f for f in customer.fields}

    assert "0" in fields["credit_limit"].metadata["column_default"]
    assert "CURRENT_TIMESTAMP" in fields["created_at"].metadata["column_default"]
    assert fields["email"].nullable is False
    assert fields["name"].nullable is True


def test_live_vendor_types_preserved_and_normalized(probe_schema_discovered):
    schema, schema_name = probe_schema_discovered

    customer = schema.entity_by_normalized_name(f"{schema_name}.customer")
    fields = {f.normalized_name: f for f in customer.fields}

    expectations = {
        "id": FieldDataType.INTEGER,
        "email": FieldDataType.STRING,
        "credit_limit": FieldDataType.DECIMAL,
        "is_active": FieldDataType.BOOLEAN,
        "created_at": FieldDataType.DATETIME,
        "tags": FieldDataType.ARRAY,
        "profile": FieldDataType.OBJECT,
        "external_uuid": FieldDataType.STRING,
        "raw_blob": FieldDataType.BINARY,
    }
    for name, expected in expectations.items():
        assert fields[name].normalized_data_type is expected, name

    assert "255" in fields["email"].source_data_type
    assert "12" in fields["credit_limit"].source_data_type
    assert fields["tags"].is_array is True


def test_live_table_and_column_comments(probe_schema_discovered):
    schema, schema_name = probe_schema_discovered

    customer = schema.entity_by_normalized_name(f"{schema_name}.customer")
    assert customer.description == "Probe customer table"
    assert customer.field_by_normalized_name("email").description == "Unique contact email"


def test_live_column_order_preserved(probe_schema_discovered):
    schema, schema_name = probe_schema_discovered

    invoice = schema.entity_by_normalized_name(f"{schema_name}.fin_invoice")
    assert [f.source_name for f in invoice.fields] == [
        "tenant_id", "invoice_no", "customer_id", "total", "approval", "issued_on",
    ]


# ============================================================
# Live profiling - aggregates only, against real data
# ============================================================

def test_live_profiling_returns_aggregates_and_no_values(pipeline_connector, probe_schema):
    schema_name, run = probe_schema
    run(
        f"""
        CREATE TABLE {schema_name}.profiled (
            id INTEGER PRIMARY KEY,
            email VARCHAR(255),
            amount NUMERIC(12,2)
        );
        INSERT INTO {schema_name}.profiled VALUES
            (1, 'alice@example.com', 100.00),
            (2, 'bob@example.com', 250.50),
            (3, NULL, 75.25);
        """
    )

    service = RelationalDiscoveryService(
        DiscoveryOptions(
            include_schemas=[schema_name],
            profiling_enabled=True,
            profile_distinct_count=True,
            profile_length_stats=True,
        )
    )
    result = service.discover(pipeline_connector)

    table = result.profiling.tables[0]
    assert table.row_count == 3

    email = next(c for c in table.columns if c.column_name == "email")
    assert email.null_count == 1
    assert email.null_percentage == pytest.approx(33.3333, abs=0.01)
    assert email.max_length == len("alice@example.com")

    amount = next(c for c in table.columns if c.column_name == "amount")
    assert amount.numeric_min == pytest.approx(75.25)
    assert amount.numeric_max == pytest.approx(250.50)

    # Privacy guarantee, stated precisely: no TEXT content ever leaves the
    # source. A numeric MIN/MAX is a bound the spec explicitly permits
    # (Step 17) and is by definition a numeric value - which is why MIN/MAX is
    # restricted to numeric columns and never issued against text, binary or
    # temporal ones. The email addresses stored in this table must therefore
    # be absent from the profile entirely, in any form.
    import json

    serialized = json.dumps(result.profiling.to_dict())
    for text_value in ("alice@example.com", "bob@example.com", "alice", "bob", "@example"):
        assert text_value not in serialized

    # The email column was profiled - but only as counts and lengths.
    assert email.distinct_count == 2
    assert email.min_length is not None
