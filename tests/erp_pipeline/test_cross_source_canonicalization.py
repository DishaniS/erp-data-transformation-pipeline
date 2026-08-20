"""PRIMARY PHASE 1 RESEARCH DEMONSTRATION.

Four ERP systems describe the same invoice in four incompatible ways:

    PostgreSQL   invoice_no, customer_ref, total_amount, approval_status
    MySQL        inv_id, cust_id, amount, status
    SQL Server   InvoiceNumber, CustomerCode, InvoiceValue, ApprovedFlag
    MongoDB      _id, invoice, buyer, financial.total (nested), approved (bool)

This module proves that all four converge on ONE canonical contract with an
identical normalized shape, while keeping their provenance distinct and their
identities free of collisions.

NO MAPPING ENGINE IS INVOLVED. The canonical records below are written by hand
as the OUTPUT a future mapping engine is expected to produce. That is the point
of Phase 1: the target is defined and testable before anything is built to hit
it.
"""

import json

import pytest

from erp_pipeline.schemas import (
    CanonicalRecord,
    EntityKind,
    FieldDataType,
    RecordProvenance,
    SchemaOrigin,
    SensitivityLevel,
    SourceEntity,
    SourceField,
    SourceReference,
    SourceSchema,
    SourceSystem,
    SourceType,
    parse_canonical_id,
)

# The canonical shape all four sources must produce. Every source below is
# expected to yield exactly this, whatever its own column names look like.
EXPECTED_CANONICAL_DATA = {
    "invoice_id": "INV-001",
    "customer_id": "CUS-44",
    "amount": 25000.00,
    "status": "approved",
}


# ============================================================
# The four source systems
# ============================================================

def postgres_system() -> SourceSystem:
    return SourceSystem(
        source_system_id="finance_erp_pg",
        name="Finance Legacy ERP",
        source_type=SourceType.POSTGRESQL,
        environment="research",
    )


def mysql_system() -> SourceSystem:
    return SourceSystem(
        source_system_id="ops_erp_mysql",
        name="Operations ERP",
        source_type=SourceType.MYSQL,
        environment="research",
    )


def sqlserver_system() -> SourceSystem:
    return SourceSystem(
        source_system_id="corp_erp_mssql",
        name="Corporate ERP",
        source_type=SourceType.SQL_SERVER,
        environment="research",
    )


def mongo_system() -> SourceSystem:
    return SourceSystem(
        source_system_id="billing_erp_mongo",
        name="Billing Platform",
        source_type=SourceType.MONGODB,
        environment="research",
    )


# ============================================================
# The four source schemas, each in its own vendor's vocabulary
# ============================================================

def postgres_schema() -> SourceSchema:
    return SourceSchema(
        schema_id="finance_erp_pg_public_v1",
        source_system_id="finance_erp_pg",
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(
            SourceEntity(
                entity_id="fin_invoice",
                source_name="fin_invoice",
                normalized_name="fin_invoice",
                entity_kind=EntityKind.TABLE,
                primary_key_fields=("invoice_no",),
                fields=(
                    SourceField(
                        source_name="invoice_no",
                        normalized_name="invoice_no",
                        source_data_type="VARCHAR(20)",
                        normalized_data_type=FieldDataType.STRING,
                        is_primary_key=True,
                        nullable=False,
                    ),
                    SourceField(
                        source_name="customer_ref",
                        normalized_name="customer_ref",
                        source_data_type="VARCHAR(20)",
                        normalized_data_type=FieldDataType.STRING,
                    ),
                    SourceField(
                        source_name="total_amount",
                        normalized_name="total_amount",
                        source_data_type="NUMERIC(12,2)",
                        normalized_data_type=FieldDataType.DECIMAL,
                    ),
                    SourceField(
                        source_name="approval_status",
                        normalized_name="approval_status",
                        source_data_type="VARCHAR(20)",
                        normalized_data_type=FieldDataType.STRING,
                    ),
                ),
            ),
        ),
    )


def mysql_schema() -> SourceSchema:
    return SourceSchema(
        schema_id="ops_erp_mysql_v1",
        source_system_id="ops_erp_mysql",
        schema_name="ops",
        origin=SchemaOrigin.DISCOVERED,
        entities=(
            SourceEntity(
                entity_id="tbl_invoice",
                source_name="tbl_invoice",
                normalized_name="tbl_invoice",
                entity_kind=EntityKind.TABLE,
                primary_key_fields=("inv_id",),
                fields=(
                    SourceField(
                        source_name="inv_id",
                        normalized_name="inv_id",
                        source_data_type="VARCHAR(32)",
                        normalized_data_type=FieldDataType.STRING,
                        is_primary_key=True,
                        nullable=False,
                    ),
                    SourceField(
                        source_name="cust_id",
                        normalized_name="cust_id",
                        source_data_type="VARCHAR(32)",
                        normalized_data_type=FieldDataType.STRING,
                    ),
                    SourceField(
                        source_name="amount",
                        normalized_name="amount",
                        source_data_type="DECIMAL(12,2)",
                        normalized_data_type=FieldDataType.DECIMAL,
                    ),
                    SourceField(
                        source_name="status",
                        normalized_name="status",
                        source_data_type="ENUM('new','approved','rejected')",
                        normalized_data_type=FieldDataType.STRING,
                    ),
                ),
            ),
        ),
    )


def sqlserver_schema() -> SourceSchema:
    return SourceSchema(
        schema_id="corp_erp_mssql_dbo_v1",
        source_system_id="corp_erp_mssql",
        schema_name="dbo",
        origin=SchemaOrigin.DISCOVERED,
        entities=(
            SourceEntity(
                entity_id="invoice_header",
                source_name="InvoiceHeader",
                normalized_name="invoice_header",
                entity_kind=EntityKind.TABLE,
                namespace="dbo",
                primary_key_fields=("invoice_number",),
                fields=(
                    SourceField(
                        source_name="InvoiceNumber",
                        normalized_name="invoice_number",
                        source_data_type="NVARCHAR(50)",
                        normalized_data_type=FieldDataType.STRING,
                        is_primary_key=True,
                        nullable=False,
                    ),
                    SourceField(
                        source_name="CustomerCode",
                        normalized_name="customer_code",
                        source_data_type="NVARCHAR(50)",
                        normalized_data_type=FieldDataType.STRING,
                    ),
                    SourceField(
                        source_name="InvoiceValue",
                        normalized_name="invoice_value",
                        source_data_type="MONEY",
                        normalized_data_type=FieldDataType.DECIMAL,
                    ),
                    SourceField(
                        source_name="ApprovedFlag",
                        normalized_name="approved_flag",
                        source_data_type="BIT",
                        normalized_data_type=FieldDataType.BOOLEAN,
                    ),
                ),
            ),
        ),
    )


def mongo_schema() -> SourceSchema:
    return SourceSchema(
        schema_id="billing_erp_mongo_v1",
        source_system_id="billing_erp_mongo",
        schema_name="billing",
        origin=SchemaOrigin.INFERRED,
        entities=(
            SourceEntity(
                entity_id="invoices",
                source_name="invoices",
                normalized_name="invoices",
                entity_kind=EntityKind.COLLECTION,
                primary_key_fields=("id",),
                fields=(
                    SourceField(
                        source_name="_id",
                        normalized_name="id",
                        source_data_type="ObjectId",
                        normalized_data_type=FieldDataType.STRING,
                        is_primary_key=True,
                        nullable=False,
                    ),
                    SourceField(
                        source_name="invoice",
                        normalized_name="invoice",
                        source_data_type="string",
                        normalized_data_type=FieldDataType.STRING,
                    ),
                    SourceField(
                        source_name="buyer",
                        normalized_name="buyer",
                        source_data_type="string",
                        normalized_data_type=FieldDataType.STRING,
                    ),
                    SourceField(
                        source_name="total",
                        normalized_name="financial_total",
                        source_data_type="int32",
                        normalized_data_type=FieldDataType.DECIMAL,
                        nested_path=("financial",),
                    ),
                    SourceField(
                        source_name="approved",
                        normalized_name="approved",
                        source_data_type="bool",
                        normalized_data_type=FieldDataType.BOOLEAN,
                    ),
                ),
            ),
        ),
    )


# ============================================================
# The four canonical records a future mapping engine must produce
# ============================================================

def postgres_record() -> CanonicalRecord:
    return CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id="finance_erp_pg",
            source_type=SourceType.POSTGRESQL,
            source_entity="fin_invoice",
            source_record_key="INV-001",
        ),
        entity_type="invoice",
        stable_source_key="INV-001",
        normalized_data=dict(EXPECTED_CANONICAL_DATA),
        sensitivity=SensitivityLevel.INTERNAL,
        provenance=RecordProvenance(
            schema_id="finance_erp_pg_public_v1",
            schema_version="1",
            ingestion_method="batch_extract",
            original_record_id="33871",
        ),
    )


def mysql_record() -> CanonicalRecord:
    return CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id="ops_erp_mysql",
            source_type=SourceType.MYSQL,
            source_entity="tbl_invoice",
            source_record_key="INV-001",
        ),
        entity_type="invoice",
        stable_source_key="INV-001",
        normalized_data=dict(EXPECTED_CANONICAL_DATA),
        sensitivity=SensitivityLevel.INTERNAL,
        provenance=RecordProvenance(
            schema_id="ops_erp_mysql_v1",
            ingestion_method="incremental_sync",
        ),
    )


def sqlserver_record() -> CanonicalRecord:
    return CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id="corp_erp_mssql",
            source_type=SourceType.SQL_SERVER,
            source_entity="dbo.InvoiceHeader",
            source_record_key="INV-001",
        ),
        entity_type="invoice",
        stable_source_key="INV-001",
        # ApprovedFlag BIT 1 becomes the canonical string "approved" - the kind
        # of enum_map a future TransformationRule will describe.
        normalized_data=dict(EXPECTED_CANONICAL_DATA),
        sensitivity=SensitivityLevel.CONFIDENTIAL,
        provenance=RecordProvenance(
            schema_id="corp_erp_mssql_dbo_v1",
            ingestion_method="batch_extract",
        ),
    )


def mongo_record() -> CanonicalRecord:
    return CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id="billing_erp_mongo",
            source_type=SourceType.MONGODB,
            source_entity="invoices",
            source_record_key="INV-001",
        ),
        entity_type="invoice",
        stable_source_key="INV-001",
        # financial.total is lifted out of the nested document and approved:true
        # becomes "approved".
        normalized_data=dict(EXPECTED_CANONICAL_DATA),
        sensitivity=SensitivityLevel.INTERNAL,
        provenance=RecordProvenance(
            schema_id="billing_erp_mongo_v1",
            ingestion_method="batch_extract",
            original_record_id="66b2f1e4c2a4f1a3b7d9e001",
        ),
    )


ALL_RECORDS = {
    "postgresql": postgres_record,
    "mysql": mysql_record,
    "sql_server": sqlserver_record,
    "mongodb": mongo_record,
}

ALL_SCHEMAS = {
    "postgresql": postgres_schema,
    "mysql": mysql_schema,
    "sql_server": sqlserver_schema,
    "mongodb": mongo_schema,
}


# ============================================================
# The proof
# ============================================================

def test_all_four_sources_are_describable_by_one_source_contract():
    schemas = [factory() for factory in ALL_SCHEMAS.values()]

    assert len(schemas) == 4
    for schema in schemas:
        assert isinstance(schema, SourceSchema)
        # Every schema serializes through the identical contract.
        payload = schema.to_json_dict()
        assert set(payload) == set(schemas[0].to_json_dict())
        assert json.loads(json.dumps(payload)) == payload


def test_vendor_types_are_preserved_per_source():
    """Normalization must not erase what each vendor actually declared."""
    amount_types = {}
    for name, factory in ALL_SCHEMAS.items():
        entity = factory().entities[0]
        amount_field = next(
            field
            for field in entity.fields
            if field.normalized_data_type is FieldDataType.DECIMAL
        )
        amount_types[name] = amount_field.source_data_type

    assert amount_types == {
        "postgresql": "NUMERIC(12,2)",
        "mysql": "DECIMAL(12,2)",
        "sql_server": "MONEY",
        "mongodb": "int32",
    }
    # ...yet all four normalize to the same cross-source type.
    assert len(set(amount_types.values())) == 4


def test_all_four_records_conform_to_the_same_contract():
    records = [factory() for factory in ALL_RECORDS.values()]

    assert len(records) == 4
    for record in records:
        assert isinstance(record, CanonicalRecord)
        assert set(record.to_json_dict()) == set(records[0].to_json_dict())


def test_all_four_records_produce_identical_canonical_data():
    """The core research claim: four representations, one canonical shape."""
    for name, factory in ALL_RECORDS.items():
        record = factory()
        assert record.normalized_data == EXPECTED_CANONICAL_DATA, name
        assert record.entity_type == "invoice", name


def test_record_ids_do_not_collide_across_source_systems():
    record_ids = [factory().record_id for factory in ALL_RECORDS.values()]

    assert len(set(record_ids)) == 4
    assert sorted(record_ids) == [
        "erp:billing_erp_mongo:invoice:inv-001",
        "erp:corp_erp_mssql:invoice:inv-001",
        "erp:finance_erp_pg:invoice:inv-001",
        "erp:ops_erp_mysql:invoice:inv-001",
    ]


def test_derived_uuids_do_not_collide_across_source_systems():
    """A vector store keyed on the derived UUID must keep them apart too."""
    uuids = [factory().deterministic_uuid() for factory in ALL_RECORDS.values()]
    assert len(set(uuids)) == 4


def test_identical_business_key_yields_different_identity_per_system():
    for factory in ALL_RECORDS.values():
        record = factory()
        system, entity_type, key = parse_canonical_id(record.record_id)

        assert system == record.source.source_system_id
        assert entity_type == "invoice"
        assert key == "inv-001"  # the same business key in all four


def test_provenance_stays_distinct_despite_identical_canonical_data():
    records = {name: factory() for name, factory in ALL_RECORDS.items()}

    source_types = {
        name: record.source.source_type.value for name, record in records.items()
    }
    assert source_types == {
        "postgresql": "postgresql",
        "mysql": "mysql",
        "sql_server": "sql_server",
        "mongodb": "mongodb",
    }

    source_entities = {
        name: record.source.source_entity for name, record in records.items()
    }
    assert source_entities == {
        "postgresql": "fin_invoice",
        "mysql": "tbl_invoice",
        "sql_server": "dbo.InvoiceHeader",
        "mongodb": "invoices",
    }

    # Ingestion detail is preserved per record, not flattened away.
    assert records["mysql"].provenance.ingestion_method == "incremental_sync"
    assert records["postgresql"].provenance.ingestion_method == "batch_extract"


def test_content_hashes_differ_because_identity_differs():
    """Same business content, different records: hashes must not collide."""
    hashes = [factory().content_hash for factory in ALL_RECORDS.values()]
    assert len(set(hashes)) == 4


def test_sensitivity_is_per_record_not_per_contract():
    records = {name: factory() for name, factory in ALL_RECORDS.items()}

    assert records["sql_server"].sensitivity is SensitivityLevel.CONFIDENTIAL
    assert records["mysql"].sensitivity is SensitivityLevel.INTERNAL


def test_every_record_serializes_to_json():
    for name, factory in ALL_RECORDS.items():
        payload = factory().to_json_dict()
        assert json.loads(json.dumps(payload)) == payload, name


def test_no_record_leaks_a_source_serial_into_identity():
    """The Mongo ObjectId and the PG SERIAL stay in provenance only."""
    pg = postgres_record()
    mongo = mongo_record()

    assert pg.provenance.original_record_id == "33871"
    assert "33871" not in pg.record_id

    assert mongo.provenance.original_record_id == "66b2f1e4c2a4f1a3b7d9e001"
    assert "66b2f1e4c2a4f1a3b7d9e001" not in mongo.record_id


@pytest.mark.parametrize("name", list(ALL_RECORDS))
def test_no_record_carries_connection_details(name):
    payload = ALL_RECORDS[name]().to_json_dict()
    serialized = json.dumps(payload).lower()

    for marker in ("password", "secret", "token", "connection_string", "@localhost"):
        assert marker not in serialized


def test_source_systems_are_all_describable_without_credentials():
    systems = [
        postgres_system(),
        mysql_system(),
        sqlserver_system(),
        mongo_system(),
    ]

    assert len({system.source_system_id for system in systems}) == 4
    for system in systems:
        serialized = json.dumps(system.to_json_dict()).lower()
        assert "password" not in serialized
        assert "secret" not in serialized
