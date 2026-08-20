"""End-to-end proofs from every source technology (Steps 47-51).

THE RESEARCH CLAIM
------------------
Five very different source shapes - a relational row, a MongoDB document, a
CSV row read by the real Phase 6 iterator, and an API-shaped payload - become
the SAME canonical representation through ONE ``TransformationService``, with
no source-specific transformation path anywhere.

The CSV case deliberately uses the real Phase 6 ``iter_records()`` rather than
a hand-built dictionary, so the handoff Phase 6's own docstring anticipated is
actually exercised.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from erp_pipeline.schemas.canonical_models import CanonicalRecord
from erp_pipeline.schemas.enums import (
    FieldDataType as T,
    MappingStatus,
    SourceType,
)
from erp_pipeline.transformation import (
    IssueCode,
    SourceRecord,
    TransformationContext,
    TransformationService,
    transform_record,
    transform_records,
)

from tests.erp_pipeline.transformation.conftest import (
    FIXTURE_DIR,
    cross_source_cases,
    customer_profile,
    invoice_profile,
    make_mapping,
    make_profile,
)


# ============================================================
# Relational (Step 49)
# ============================================================

def test_a_postgresql_row_becomes_a_canonical_record(pg_context):
    record = SourceRecord.from_mapping(
        {
            "inv_no": "INV-001",
            "cust_no": "C001",
            "total_amt": Decimal("123.45"),
        }
    )

    result = transform_record(record, invoice_profile(), context=pg_context)

    assert result.is_transformed
    assert result.record.normalized_data["amount"] == Decimal("123.45")


def test_a_mysql_row_uses_the_same_api():
    context = TransformationContext(source_type=SourceType.MYSQL)
    record = SourceRecord.from_mapping(
        {"inv_no": "INV-001", "cust_no": "C001", "total_amt": 123.45}
    )

    result = transform_record(record, invoice_profile(), context=context)

    assert result.is_transformed
    assert result.record.normalized_data["amount"] == Decimal("123.45")


def test_a_relational_row_records_its_technology(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert result.record.source.source_type is SourceType.POSTGRESQL


# ============================================================
# MongoDB (Steps 48, 56)
# ============================================================

def _mongo_profile():
    return make_profile(
        "mongo.customer",
        [
            make_mapping("customer.id", "customer_id", T.STRING),
            make_mapping("customer.contact.email", "email", T.STRING),
            make_mapping("cname", "name", T.STRING),
        ],
        source_system_id="erp_mongo",
        source_entity="customers",
        target_entity_type="customer",
    )


def test_a_mongo_document_becomes_a_canonical_record(mongo_context):
    document = SourceRecord.from_mapping(
        {
            "customer": {
                "id": "C001",
                "contact": {"email": "x@example.test"},
            },
            "cname": "Acme",
        }
    )

    result = transform_record(document, _mongo_profile(), context=mongo_context)

    assert result.is_transformed
    assert result.record.normalized_data == {
        "customer_id": "C001",
        "email": "x@example.test",
        "name": "Acme",
    }


def test_a_missing_nested_branch_is_a_finding_not_a_key_error(mongo_context):
    """``customer.contact`` absent must not raise."""
    document = SourceRecord.from_mapping(
        {"customer": {"id": "C001"}, "cname": "Acme"}
    )

    result = transform_record(document, _mongo_profile(), context=mongo_context)

    assert result.is_transformed
    assert IssueCode.SOURCE_FIELD_MISSING.value in result.issue_codes()
    assert "email" not in result.record.normalized_data


def test_mongo_transformation_needs_no_live_database(mongo_context):
    """The engine never connects to anything."""
    result = transform_record(
        SourceRecord.from_mapping(
            {"customer": {"id": "C001", "contact": {}}, "cname": "Acme"}
        ),
        _mongo_profile(),
        context=mongo_context,
    )

    assert result.is_transformed


# ============================================================
# CSV through the real Phase 6 iterator (Step 47)
# ============================================================

def _csv_records():
    """Stream the fixture through Phase 6's own CSV ingestion."""
    from erp_pipeline.ingestion.service import FileIngestionService

    result = FileIngestionService().ingest(FIXTURE_DIR / "invoices.csv")

    return [
        SourceRecord.from_source_row(row, source_entity="invoices")
        for row in result.iter_records()
    ]


def test_the_phase_6_iterator_produces_usable_source_records():
    records = _csv_records()

    assert len(records) == 3
    assert records[0].values["inv_no"] == "INV-001"
    assert records[0].ordinal == 1


def test_a_csv_row_becomes_a_canonical_record(csv_context):
    profile = invoice_profile(
        "csv.inv", "erp_csv", "invoices",
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
        ),
    )

    summary = transform_records(_csv_records(), profile, csv_context)

    assert summary.records_read == 3
    assert summary.records_transformed == 2
    assert summary.records_failed == 1


def test_csv_text_becomes_a_real_decimal(csv_context):
    """CSV gives every value as a string; the canonical amount is a Decimal."""
    profile = invoice_profile(
        "csv.inv", "erp_csv", "invoices",
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
        ),
    )

    summary = transform_records(_csv_records(), profile, csv_context)
    amounts = [
        record.normalized_data["amount"] for record in summary.successful_records
    ]

    assert amounts == [Decimal("2500.50"), Decimal("1200.00")]
    assert all(isinstance(amount, Decimal) for amount in amounts)


def test_the_bad_csv_row_is_rejected_with_a_conversion_issue(csv_context):
    """The mandatory bad-value case, through the real CSV path."""
    profile = invoice_profile(
        "csv.inv", "erp_csv", "invoices",
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
        ),
    )

    summary = transform_records(_csv_records(), profile, csv_context)

    assert summary.rejected_records[0].ordinal == 3
    assert IssueCode.TYPE_CONVERSION_FAILED.value in (
        summary.rejected_records[0].reasons
    )


def test_a_csv_record_keeps_its_row_number_as_provenance(csv_context):
    profile = invoice_profile(
        "csv.inv", "erp_csv", "invoices",
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
        ),
    )

    summary = transform_records(_csv_records(), profile, csv_context)

    assert summary.successful_records[0].provenance.original_record_id == "1"


def test_there_is_no_separate_csv_transformation_path():
    """``from_source_row`` adapts; it does not branch the engine."""
    source = Path("src/erp_pipeline/transformation").rglob("*.py")
    offenders: list[str] = []

    for path in source:
        text = path.read_text(encoding="utf-8")
        for marker in ("erp_pipeline.ingestion", "erp_pipeline.discovery",
                       "erp_pipeline.api_specs", "erp_pipeline.connectors"):
            if f"import {marker}" in text or f"from {marker}" in text:
                offenders.append(f"{path}: {marker}")

    assert offenders == []


# ============================================================
# API-shaped records (Step 50)
# ============================================================

def test_an_api_shaped_record_transforms(api_context):
    profile = invoice_profile(
        "api.inv", "erp_api", "InvoiceResponse",
        fields=(
            ("invoiceId", "invoice_id", T.STRING),
            ("customer.customerId", "customer_id", T.STRING),
            ("totalAmount", "amount", T.DECIMAL),
        ),
    )
    payload = SourceRecord.from_mapping(
        {
            "invoiceId": "INV-001",
            "customer": {"customerId": "C001"},
            "totalAmount": "2500.50",
        }
    )

    result = transform_record(payload, profile, context=api_context)

    assert result.is_transformed
    assert result.record.normalized_data["amount"] == Decimal("2500.50")


def test_the_engine_does_not_care_that_a_schema_came_from_a_spec(api_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=api_context,
    )

    assert result.is_transformed
    assert result.record.source.source_type is SourceType.OPENAPI


# ============================================================
# The cross-source proof (Step 51)
# ============================================================

def test_every_source_technology_is_transformed_by_one_service():
    service = TransformationService()
    results = []

    for label, source_type, profile, record in cross_source_cases():
        context = TransformationContext(source_type=source_type)
        results.append((label, service.transform_record(record, profile, context)))

    assert all(result.is_transformed for _, result in results)
    assert len(results) == 5


def test_all_five_sources_produce_identical_canonical_data():
    """Different field names, different types, one canonical shape."""
    service = TransformationService()
    payloads = []

    for _, source_type, profile, record in cross_source_cases():
        context = TransformationContext(source_type=source_type)
        result = service.transform_record(record, profile, context)
        payloads.append(dict(result.record.normalized_data))

    expected = {
        "invoice_id": "INV-001",
        "customer_id": "C001",
        "amount": Decimal("2500.50"),
    }

    assert payloads == [expected] * 5


def test_all_five_sources_produce_the_same_canonical_entity_type():
    service = TransformationService()

    for _, source_type, profile, record in cross_source_cases():
        result = service.transform_record(
            record, profile, TransformationContext(source_type=source_type)
        )
        assert result.record.entity_type == "invoice"


def test_identities_stay_distinct_per_source_system():
    """Same business key in five systems must not collide."""
    service = TransformationService()
    ids = set()

    for _, source_type, profile, record in cross_source_cases():
        result = service.transform_record(
            record, profile, TransformationContext(source_type=source_type)
        )
        ids.add(result.record.record_id)

    assert len(ids) == 5


def test_each_source_keeps_its_own_honest_provenance():
    service = TransformationService()
    seen = set()

    for _, source_type, profile, record in cross_source_cases():
        result = service.transform_record(
            record, profile, TransformationContext(source_type=source_type)
        )
        seen.add(result.record.source.source_type)

    assert seen == {
        SourceType.POSTGRESQL,
        SourceType.MYSQL,
        SourceType.MONGODB,
        SourceType.CSV,
        SourceType.OPENAPI,
    }


def test_one_service_instance_is_reusable_across_technologies():
    """No per-source state leaks between calls."""
    service = TransformationService()
    cases = cross_source_cases()

    first_pass = [
        service.transform_record(
            record, profile, TransformationContext(source_type=source_type)
        ).record.normalized_data
        for _, source_type, profile, record in cases
    ]
    second_pass = [
        service.transform_record(
            record, profile, TransformationContext(source_type=source_type)
        ).record.normalized_data
        for _, source_type, profile, record in cases
    ]

    assert first_pass == second_pass


# ============================================================
# The phase brief's worked example
# ============================================================

def test_the_briefs_two_entity_example_produces_two_canonical_records(pg_context):
    """``cust_no`` -> customer, ``total_amt`` -> invoice.

    ``MappingProfile`` is scoped to ONE canonical entity type by the frozen
    contract, so one source record feeding two canonical entities is two
    profiles producing two records - not one record with entity-keyed nesting.
    This follows the repository's own convention, where ``normalized_data``
    holds bare field names scoped by ``entity_type``.
    """
    record = SourceRecord.from_mapping(
        {
            "cust_no": "C001",
            "cust_name": "Acme Trading",
            "inv_no": "INV-001",
            "total_amt": "2500.50",
        }
    )

    service = TransformationService()

    customer = service.transform_record(record, customer_profile(), pg_context)
    invoice = service.transform_record(record, invoice_profile(), pg_context)

    assert customer.record.entity_type == "customer"
    assert customer.record.normalized_data["customer_id"] == "C001"

    assert invoice.record.entity_type == "invoice"
    assert invoice.record.normalized_data["amount"] == Decimal("2500.50")
    assert isinstance(invoice.record.normalized_data["amount"], Decimal)


def test_the_briefs_bad_value_example(pg_context):
    """``{"total_amt": "hello"}`` must not become 0, null or "hello"."""
    record = SourceRecord.from_mapping(
        {"inv_no": "INV-001", "cust_no": "C001", "total_amt": "hello"}
    )

    summary = transform_records([record], invoice_profile(), pg_context)

    assert summary.records_read == 1
    assert summary.records_transformed == 0
    assert summary.records_failed == 1
    assert summary.successful_records == ()

    conversion = [
        issue
        for issue in summary.issues
        if issue.code == IssueCode.TYPE_CONVERSION_FAILED.value
    ]

    assert conversion
    assert conversion[0].field_name == "total_amt"


# ============================================================
# Only decided mappings execute (Step 5)
# ============================================================

@pytest.mark.parametrize(
    "status",
    [
        MappingStatus.SUGGESTED,
        MappingStatus.REVIEW_REQUIRED,
        MappingStatus.REJECTED,
    ],
)
def test_an_undecided_mapping_is_not_executed(status, pg_context):
    profile = make_profile(
        "undecided.profile",
        [
            make_mapping("cust_no", "customer_id", T.STRING),
            make_mapping("cust_name", "name", T.STRING),
            make_mapping("mail", "email", T.STRING, status=status),
        ],
    )

    result = transform_record(
        SourceRecord.from_mapping(
            {"cust_no": "C001", "cust_name": "Acme", "mail": "x@example.test"}
        ),
        profile,
        context=pg_context,
    )

    assert result.is_transformed
    assert "email" not in result.record.normalized_data


@pytest.mark.parametrize(
    "status", [MappingStatus.AUTO_ACCEPTED, MappingStatus.APPROVED]
)
def test_a_decided_mapping_is_executed(status, pg_context):
    profile = make_profile(
        "decided.profile",
        [
            make_mapping("cust_no", "customer_id", T.STRING),
            make_mapping("cust_name", "name", T.STRING),
            make_mapping("mail", "email", T.STRING, status=status),
        ],
    )

    result = transform_record(
        SourceRecord.from_mapping(
            {"cust_no": "C001", "cust_name": "Acme", "mail": "x@example.test"}
        ),
        profile,
        context=pg_context,
    )

    assert result.record.normalized_data["email"] == "x@example.test"


def test_a_profile_with_nothing_executable_is_reported(pg_context):
    profile = make_profile(
        "nothing.profile",
        [
            make_mapping(
                "cust_no", "customer_id", T.STRING,
                status=MappingStatus.REVIEW_REQUIRED,
            )
        ],
    )

    result = transform_record(
        SourceRecord.from_mapping({"cust_no": "C001"}), profile, context=pg_context
    )

    assert IssueCode.NO_FIELDS_MAPPED.value in result.issue_codes()


def test_phase_9_never_reinvents_a_mapping(pg_context):
    """A source field the profile does not mention is simply not mapped."""
    result = transform_record(
        SourceRecord.from_mapping(
            {"cust_no": "C001", "cust_name": "Acme", "email_addr": "x@example.test"}
        ),
        customer_profile(),
        context=pg_context,
    )

    assert set(result.record.normalized_data) == {"customer_id", "name"}
