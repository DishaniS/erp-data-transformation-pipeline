"""Shared fixtures for the Phase 9 transformation tests.

PRIVACY SENTINELS
-----------------
``SECRET_*`` values are planted in source records that legitimately carry them.
They may appear in a ``CanonicalRecord`` - that is the whole job. They must
NEVER appear in an issue, a rejection report, a run summary, a log or an
exception, and ``test_transformation_boundary.py`` asserts exactly that.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import pytest

from erp_pipeline.schemas.enums import (
    FieldDataType,
    MappingStatus,
    SourceType,
)
from erp_pipeline.schemas.mapping_models import (
    FieldMapping,
    MappingProfile,
    TransformationRule,
)
from erp_pipeline.transformation import (
    SourceRecord,
    TransformationContext,
)

T = FieldDataType

#: Values a leak test looks for. Distinctive enough that a substring search
#: cannot produce a false positive.
SECRET_CUSTOMER = "SECRET_CUSTOMER_93821"
SECRET_ACCOUNT = "SECRET_ACCOUNT_22118"
SECRET_EMAIL = "SECRET_EMAIL_44519"

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "transformation"


# ============================================================
# Profile builders
# ============================================================

def make_mapping(
    source_field: str,
    target_field: str,
    target_type: FieldDataType | None = None,
    *,
    status: MappingStatus = MappingStatus.AUTO_ACCEPTED,
    transformations: Sequence[TransformationRule] = (),
    source_type: FieldDataType | None = None,
) -> FieldMapping:
    return FieldMapping(
        source_field=source_field,
        target_field=target_field,
        source_type=source_type,
        target_type=target_type,
        transformations=tuple(transformations),
        status=status,
    )


def make_profile(
    mapping_id: str,
    field_mappings: Sequence[FieldMapping],
    *,
    source_system_id: str = "erp_a",
    source_entity: str = "fin_customer",
    target_entity_type: str = "customer",
    source_schema_id: str | None = "erp_a.main.v1",
) -> MappingProfile:
    return MappingProfile(
        mapping_id=mapping_id,
        source_system_id=source_system_id,
        source_entity=source_entity,
        target_entity_type=target_entity_type,
        source_schema_id=source_schema_id,
        field_mappings=tuple(field_mappings),
    )


def customer_profile(
    mapping_id: str = "cust.profile",
    source_system_id: str = "erp_a",
    source_entity: str = "fin_customer",
    fields: Sequence[tuple[str, str, FieldDataType]] | None = None,
) -> MappingProfile:
    """A minimal profile satisfying the canonical customer's required fields."""
    declared = fields or (
        ("cust_no", "customer_id", T.STRING),
        ("cust_name", "name", T.STRING),
    )
    return make_profile(
        mapping_id,
        [make_mapping(s, t, ty) for s, t, ty in declared],
        source_system_id=source_system_id,
        source_entity=source_entity,
        target_entity_type="customer",
    )


def invoice_profile(
    mapping_id: str = "inv.profile",
    source_system_id: str = "erp_a",
    source_entity: str = "fin_invoice",
    fields: Sequence[tuple[str, str, FieldDataType]] | None = None,
) -> MappingProfile:
    """A minimal profile satisfying the canonical invoice's required fields."""
    declared = fields or (
        ("inv_no", "invoice_id", T.STRING),
        ("cust_no", "customer_id", T.STRING),
        ("total_amt", "amount", T.DECIMAL),
    )
    return make_profile(
        mapping_id,
        [make_mapping(s, t, ty) for s, t, ty in declared],
        source_system_id=source_system_id,
        source_entity=source_entity,
        target_entity_type="invoice",
    )


# ============================================================
# Contexts
# ============================================================

@pytest.fixture()
def pg_context() -> TransformationContext:
    return TransformationContext(
        source_type=SourceType.POSTGRESQL,
        schema_id="erp_a.public.v1",
        ingestion_method="batch_extract",
    )


@pytest.fixture()
def mongo_context() -> TransformationContext:
    return TransformationContext(
        source_type=SourceType.MONGODB,
        ingestion_method="batch_extract",
    )


@pytest.fixture()
def csv_context() -> TransformationContext:
    return TransformationContext(
        source_type=SourceType.CSV,
        ingestion_method="file_upload",
    )


@pytest.fixture()
def api_context() -> TransformationContext:
    return TransformationContext(
        source_type=SourceType.OPENAPI,
        ingestion_method="api_pull",
    )


# ============================================================
# Profiles as fixtures
# ============================================================

@pytest.fixture()
def simple_customer_profile() -> MappingProfile:
    return customer_profile()


@pytest.fixture()
def simple_invoice_profile() -> MappingProfile:
    return invoice_profile()


# ============================================================
# Records
# ============================================================

def customer_record(
    cust_no: str = "C001",
    cust_name: str = "Acme Trading",
    ordinal: int | None = None,
    **extra: Any,
) -> SourceRecord:
    values: dict[str, Any] = {"cust_no": cust_no, "cust_name": cust_name}
    values.update(extra)
    return SourceRecord.from_mapping(values, ordinal=ordinal)


def invoice_record(
    inv_no: str = "INV-001",
    cust_no: str = "C001",
    total_amt: Any = "2500.50",
    ordinal: int | None = None,
    **extra: Any,
) -> SourceRecord:
    values: dict[str, Any] = {
        "inv_no": inv_no,
        "cust_no": cust_no,
        "total_amt": total_amt,
    }
    values.update(extra)
    return SourceRecord.from_mapping(values, ordinal=ordinal)


# ============================================================
# The same ERP concepts, five source shapes (Step 51)
# ============================================================

#: Each entry is (label, SourceType, MappingProfile, SourceRecord). All five
#: describe the SAME invoice - INV-001 for customer C001, 2500.50 - written the
#: way each technology writes it.
def cross_source_cases() -> tuple[tuple[str, SourceType, MappingProfile, SourceRecord], ...]:
    postgres = (
        "postgresql",
        SourceType.POSTGRESQL,
        invoice_profile(
            "pg.inv", "erp_pg", "fin_invoice",
            fields=(
                ("invoice_no", "invoice_id", T.STRING),
                ("customer_ref", "customer_id", T.STRING),
                ("total_amount", "amount", T.DECIMAL),
            ),
        ),
        SourceRecord.from_mapping(
            {
                "invoice_no": "INV-001",
                "customer_ref": "C001",
                "total_amount": Decimal("2500.50"),
            },
            ordinal=1,
        ),
    )

    mysql = (
        "mysql",
        SourceType.MYSQL,
        invoice_profile(
            "mysql.inv", "erp_mysql", "invoices",
            fields=(
                ("invoiceId", "invoice_id", T.STRING),
                ("customerId", "customer_id", T.STRING),
                ("total", "amount", T.DECIMAL),
            ),
        ),
        SourceRecord.from_mapping(
            {"invoiceId": "INV-001", "customerId": "C001", "total": 2500.50},
            ordinal=1,
        ),
    )

    mongodb = (
        "mongodb",
        SourceType.MONGODB,
        invoice_profile(
            "mongo.inv", "erp_mongo", "invoices",
            fields=(
                ("invoice.id", "invoice_id", T.STRING),
                ("customer.id", "customer_id", T.STRING),
                ("invoice.total", "amount", T.DECIMAL),
            ),
        ),
        SourceRecord.from_mapping(
            {
                "invoice": {"id": "INV-001", "total": "2500.50"},
                "customer": {"id": "C001"},
            },
            ordinal=1,
        ),
    )

    csv_case = (
        "csv",
        SourceType.CSV,
        invoice_profile(
            "csv.inv", "erp_csv", "export_2026_q1",
            fields=(
                ("inv_no", "invoice_id", T.STRING),
                ("cust_no", "customer_id", T.STRING),
                ("total_amt", "amount", T.DECIMAL),
            ),
        ),
        SourceRecord.from_mapping(
            {"inv_no": "INV-001", "cust_no": "C001", "total_amt": "2500.50"},
            ordinal=1,
        ),
    )

    api = (
        "openapi",
        SourceType.OPENAPI,
        invoice_profile(
            "api.inv", "erp_api", "InvoiceResponse",
            fields=(
                ("invoiceId", "invoice_id", T.STRING),
                ("customerId", "customer_id", T.STRING),
                ("totalAmount", "amount", T.DECIMAL),
            ),
        ),
        SourceRecord.from_mapping(
            {
                "invoiceId": "INV-001",
                "customerId": "C001",
                "totalAmount": "2500.50",
            },
            ordinal=1,
        ),
    )

    return (postgres, mysql, mongodb, csv_case, api)
