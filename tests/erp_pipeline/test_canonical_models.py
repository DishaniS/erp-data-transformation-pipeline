"""Canonical model contract tests.

Covers identity determinism, cross-system collision resistance, provenance,
sensitivity, serialization and the document contract.
"""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from erp_pipeline.schemas import (
    CanonicalDocument,
    CanonicalRecord,
    RecordProvenance,
    RecordType,
    SensitivityLevel,
    SourceReference,
    SourceType,
    ValidationError,
    make_canonical_record_id,
    make_deterministic_uuid,
    parse_canonical_id,
)
from erp_pipeline.version import CANONICAL_MODEL_VERSION


PG_SOURCE = SourceReference(
    source_system_id="finance_erp_pg",
    source_type=SourceType.POSTGRESQL,
    source_entity="fin_invoice",
    source_record_key="INV-001",
)


def build_record(**overrides) -> CanonicalRecord:
    kwargs = dict(
        source=PG_SOURCE,
        entity_type="invoice",
        stable_source_key="INV-001",
        normalized_data={
            "invoice_id": "INV-001",
            "customer_id": "CUS-44",
            "amount": 25000.00,
            "currency": "LKR",
            "status": "approved",
        },
    )
    kwargs.update(overrides)
    return CanonicalRecord.from_source(**kwargs)


# ============================================================
# Identity
# ============================================================

def test_canonical_record_id_is_deterministic():
    first = build_record()
    second = build_record()

    assert first.record_id == second.record_id
    assert first.record_id == "erp:finance_erp_pg:invoice:inv-001"


def test_canonical_record_id_survives_reconstruction_with_different_timestamps():
    """Identity must not depend on when the record was built."""
    early = build_record(created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    late = build_record(created_at=datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert early.record_id == late.record_id
    assert early.content_hash == late.content_hash


def test_same_business_key_in_two_source_systems_does_not_collide():
    """The primary collision-resistance requirement."""
    erp_a = SourceReference(
        source_system_id="erp_a", source_type=SourceType.POSTGRESQL
    )
    erp_b = SourceReference(source_system_id="erp_b", source_type=SourceType.MYSQL)

    record_a = CanonicalRecord.from_source(
        source=erp_a, entity_type="invoice", stable_source_key="1001"
    )
    record_b = CanonicalRecord.from_source(
        source=erp_b, entity_type="invoice", stable_source_key="1001"
    )

    assert record_a.record_id != record_b.record_id
    assert record_a.record_id == "erp:erp_a:invoice:1001"
    assert record_b.record_id == "erp:erp_b:invoice:1001"
    # The derived UUIDs must differ too, or a vector store would merge them.
    assert record_a.deterministic_uuid() != record_b.deterministic_uuid()


def test_same_business_key_across_entity_types_does_not_collide():
    source = SourceReference(
        source_system_id="erp_a", source_type=SourceType.POSTGRESQL
    )

    invoice = CanonicalRecord.from_source(
        source=source, entity_type="invoice", stable_source_key="1001"
    )
    order = CanonicalRecord.from_source(
        source=source, entity_type="purchase_order", stable_source_key="1001"
    )

    assert invoice.record_id != order.record_id


def test_canonical_id_is_unambiguously_parseable():
    record = build_record()
    system, entity_type, key = parse_canonical_id(record.record_id)

    assert system == "finance_erp_pg"
    assert entity_type == "invoice"
    assert key == "inv-001"


def test_canonical_id_components_cannot_smuggle_a_separator():
    """Normalization removes ':', so a hostile key cannot forge components."""
    record_id = make_canonical_record_id(
        source_system_id="erp_a",
        entity_type="invoice",
        stable_source_key="evil:injected:value",
    )

    assert record_id.count(":") == 3
    assert parse_canonical_id(record_id)[2] == "evil_injected_value"


def test_record_id_contains_no_database_serial():
    """A SERIAL must never be the basis of canonical identity."""
    record = build_record()

    # The source row's SERIAL is carried only as opaque provenance, never
    # as any part of the identity.
    with_serial = CanonicalRecord.from_source(
        source=PG_SOURCE,
        entity_type="invoice",
        stable_source_key="INV-001",
        provenance=RecordProvenance(original_record_id="33871"),
    )

    assert with_serial.record_id == record.record_id
    assert "33871" not in with_serial.record_id


def test_deterministic_uuid_is_uuid5_and_stable():
    record = build_record()
    derived = record.deterministic_uuid()

    assert derived == make_deterministic_uuid(record.record_id)
    assert uuid.UUID(derived).version == 5
    assert derived == build_record().deterministic_uuid()


# ============================================================
# Content hash
# ============================================================

def test_content_hash_changes_when_normalized_data_changes():
    original = build_record()
    changed = build_record(
        normalized_data={
            "invoice_id": "INV-001",
            "customer_id": "CUS-44",
            "amount": 26000.00,
            "currency": "LKR",
            "status": "approved",
        }
    )

    assert original.record_id == changed.record_id
    assert original.content_hash != changed.content_hash


def test_content_hash_is_key_order_independent():
    first = build_record(normalized_data={"a": 1, "b": 2})
    second = build_record(normalized_data={"b": 2, "a": 1})

    assert first.content_hash == second.content_hash


def test_content_hash_treats_absent_and_null_alike():
    first = build_record(normalized_data={"a": 1})
    second = build_record(normalized_data={"a": 1, "b": None})

    assert first.content_hash == second.content_hash


# ============================================================
# Validation
# ============================================================

def test_blank_record_id_rejected():
    with pytest.raises(ValidationError, match="must not be blank"):
        CanonicalRecord(
            record_id="  ",
            record_type=RecordType.STRUCTURED_RECORD,
            source=PG_SOURCE,
            entity_type="invoice",
        )


def test_blank_source_system_id_rejected():
    with pytest.raises(ValidationError, match="must not be blank"):
        SourceReference(source_system_id="", source_type=SourceType.POSTGRESQL)


def test_invalid_sensitivity_rejected():
    with pytest.raises(ValueError, match="not a valid SensitivityLevel"):
        build_record(sensitivity="top_secret")


@pytest.mark.parametrize(
    "level",
    [
        SensitivityLevel.PUBLIC,
        SensitivityLevel.INTERNAL,
        SensitivityLevel.CONFIDENTIAL,
        SensitivityLevel.RESTRICTED,
    ],
)
def test_every_sensitivity_level_is_accepted(level):
    record = build_record(sensitivity=level)
    assert record.to_json_dict()["sensitivity"] == level.value


def test_restricted_sensitivity_round_trips_as_string():
    record = build_record(sensitivity="restricted")
    assert record.sensitivity is SensitivityLevel.RESTRICTED


def test_normalized_data_must_be_an_object():
    with pytest.raises(ValidationError, match="must be a mapping/object"):
        build_record(normalized_data=[("invoice_id", "INV-001")])


def test_normalized_data_must_be_json_compatible():
    with pytest.raises(ValidationError, match="not JSON-serializable"):
        build_record(normalized_data={"created": {1, 2, 3}})


def test_normalized_data_rejects_non_string_keys():
    with pytest.raises(ValidationError, match="keys must be strings"):
        build_record(normalized_data={1: "one"})


def test_entity_type_must_be_normalized():
    with pytest.raises(ValidationError, match="normalized identifier"):
        build_record(entity_type="Purchase Order")


def test_entity_type_is_open_and_not_restricted_to_a_fixed_list():
    """A new ERP domain object must need no change to the contract."""
    for entity_type in (
        "invoice",
        "goods_receipt",
        "cost_center",
        "maintenance_work_order",
        "some_future_erp_object",
    ):
        record = CanonicalRecord.from_source(
            source=PG_SOURCE, entity_type=entity_type, stable_source_key="1"
        )
        assert record.entity_type == entity_type


def test_source_must_be_a_typed_reference_not_a_dict():
    with pytest.raises(ValidationError, match="must be a SourceReference"):
        CanonicalRecord(
            record_id="erp:a:invoice:1",
            record_type=RecordType.STRUCTURED_RECORD,
            source={"source_system_id": "a", "source_type": "postgresql"},
            entity_type="invoice",
        )


def test_provenance_must_not_carry_credentials():
    with pytest.raises(ValidationError, match="must not contain credentials"):
        RecordProvenance(metadata={"api_key": "abc123"})


def test_metadata_accepts_valid_json_types():
    record = build_record(
        metadata={
            "batch": 7,
            "ratio": 0.5,
            "flags": ["a", "b"],
            "nested": {"ok": True, "missing": None},
        }
    )

    payload = record.to_json_dict()
    assert json.loads(json.dumps(payload))["metadata"]["nested"]["ok"] is True


# ============================================================
# Serialization
# ============================================================

def test_canonical_record_serializes_to_json_safe_structure():
    record = build_record(
        provenance=RecordProvenance(
            schema_id="finance_erp_pg_public_v1",
            schema_version="1",
            ingestion_method="batch_extract",
            original_record_id="33871",
            extracted_at=datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc),
        )
    )

    payload = record.to_json_dict()

    # Must survive a real JSON round trip.
    assert json.loads(json.dumps(payload)) == payload

    assert payload["record_type"] == "structured_record"
    assert payload["source"]["source_type"] == "postgresql"
    assert payload["schema_version"] == CANONICAL_MODEL_VERSION
    assert payload["provenance"]["extracted_at"] == "2026-08-10T09:30:00Z"


def test_datetime_serialization_is_predictable_utc_rfc3339():
    record = build_record(
        created_at=datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc),
    )

    payload = record.to_json_dict()
    assert payload["created_at"] == "2026-08-10T12:00:00Z"
    assert payload["updated_at"] == "2026-08-10T12:00:00Z"


def test_non_utc_datetime_is_converted_preserving_the_instant():
    from datetime import timedelta

    colombo = timezone(timedelta(hours=5, minutes=30))
    record = build_record(
        created_at=datetime(2026, 8, 10, 17, 30, 0, tzinfo=colombo),
    )

    assert record.to_json_dict()["created_at"] == "2026-08-10T12:00:00Z"


def test_naive_datetime_is_rejected_rather_than_guessed():
    with pytest.raises(ValidationError, match="timezone-aware"):
        build_record(created_at=datetime(2026, 8, 10, 12, 0, 0))


def test_decimal_serializes_as_string_to_preserve_precision():
    record = build_record(normalized_data={"amount": Decimal("25000.10")})
    payload = record.to_json_dict()

    assert payload["normalized_data"]["amount"] == "25000.10"
    assert json.loads(json.dumps(payload))["normalized_data"]["amount"] == "25000.10"


def test_exclude_none_produces_a_compact_payload():
    record = build_record()
    compact = record.to_json_dict(exclude_none=True)

    assert "provenance" not in compact
    assert "record_id" in compact


def test_to_json_returns_parseable_text():
    record = build_record()
    assert json.loads(record.to_json())["record_id"] == record.record_id


def test_model_version_constant_is_used_not_a_literal():
    """Version must come from the single constant, not a scattered literal."""
    record = build_record()
    assert record.schema_version == CANONICAL_MODEL_VERSION
    assert record.to_json_dict()["schema_version"] == CANONICAL_MODEL_VERSION


def test_canonical_record_has_no_storage_or_vector_fields():
    """The canonical model must not assume a storage engine or vector store."""
    payload = build_record().to_json_dict()

    forbidden = {
        "id",
        "serial",
        "table_name",
        "qdrant_point_id",
        "embedding",
        "vector",
        "collection",
        "connection",
    }
    assert not (set(payload) & forbidden)


# ============================================================
# CanonicalDocument
# ============================================================

def test_canonical_document_represents_a_pdf():
    source = SourceReference(
        source_system_id="policy_library",
        source_type=SourceType.PDF,
        source_entity="finance_policies",
        source_record_key="finance_reimbursement_policy.pdf",
    )

    document = CanonicalDocument.from_source(
        source=source,
        document_id="f194b2d65c37b8c1c3d48c69",
        title="Finance Reimbursement and Payment Processing Policy",
        document_type="policy_document",
        mime_type="application/pdf",
        text="Finance Reimbursement and Payment Processing Policy ...",
        page_count=6,
        language="en",
        sensitivity=SensitivityLevel.INTERNAL,
        provenance=RecordProvenance(
            ingestion_method="file_upload",
            source_file_path="data/policies/finance_reimbursement_policy.pdf",
            page_number=1,
        ),
    )

    payload = document.to_json_dict()

    assert payload["record_type"] == "document"
    assert payload["record_id"] == "erp:policy_library:document:f194b2d65c37b8c1c3d48c69"
    assert payload["source"]["source_type"] == "pdf"
    assert payload["mime_type"] == "application/pdf"
    assert payload["page_count"] == 6
    assert json.loads(json.dumps(payload)) == payload


def test_canonical_document_represents_an_ocr_image():
    source = SourceReference(
        source_system_id="scanned_receipts",
        source_type=SourceType.IMAGE,
        source_entity="travel_receipts",
        source_record_key="travel_receipt_001.png",
    )

    document = CanonicalDocument.from_source(
        source=source,
        document_id="0009bc4deb12b3635a1c19fc",
        title="Travel receipt 001",
        document_type="invoice_or_receipt",
        mime_type="image/png",
        text="Distance 35.2 km Fare LKR 4,450.00 Total LKR 4,900.00",
        page_count=1,
        language="en",
        provenance=RecordProvenance(
            ingestion_method="file_upload",
            source_file_path="data/images/travel_receipt_001.png",
            metadata={"ocr_engine": "tesseract", "ocr_confidence": 0.87},
        ),
    )

    payload = document.to_json_dict()

    assert payload["source"]["source_type"] == "image"
    assert payload["mime_type"] == "image/png"
    assert payload["provenance"]["metadata"]["ocr_engine"] == "tesseract"


def test_pdf_and_image_documents_share_one_contract_but_differ_in_provenance():
    pdf = CanonicalDocument.from_source(
        source=SourceReference(
            source_system_id="policy_library", source_type=SourceType.PDF
        ),
        document_id="aaa111",
        text="policy text",
    )
    image = CanonicalDocument.from_source(
        source=SourceReference(
            source_system_id="scanned_receipts", source_type=SourceType.IMAGE
        ),
        document_id="bbb222",
        text="ocr text",
    )

    assert type(pdf) is type(image)
    assert set(pdf.to_json_dict()) == set(image.to_json_dict())
    assert pdf.record_id != image.record_id
    assert pdf.source.source_type is SourceType.PDF
    assert image.source.source_type is SourceType.IMAGE


def test_document_identity_follows_content_derived_document_id():
    source = SourceReference(
        source_system_id="policy_library", source_type=SourceType.PDF
    )

    original = CanonicalDocument.from_source(source=source, document_id="hash_v1")
    edited = CanonicalDocument.from_source(source=source, document_id="hash_v2")

    assert original.record_id != edited.record_id


def test_document_text_change_changes_the_content_hash():
    source = SourceReference(
        source_system_id="policy_library", source_type=SourceType.PDF
    )

    first = CanonicalDocument.from_source(
        source=source, document_id="hash_v1", text="original"
    )
    second = CanonicalDocument.from_source(
        source=source, document_id="hash_v1", text="revised"
    )

    assert first.record_id == second.record_id
    assert first.content_hash != second.content_hash


def test_document_rejects_negative_page_count():
    with pytest.raises(ValidationError, match="must not be negative"):
        CanonicalDocument.from_source(
            source=SourceReference(
                source_system_id="policy_library", source_type=SourceType.PDF
            ),
            document_id="aaa111",
            page_count=-1,
        )


def test_document_and_record_share_the_envelope_fields():
    record = build_record()
    document = CanonicalDocument.from_source(
        source=SourceReference(
            source_system_id="policy_library", source_type=SourceType.PDF
        ),
        document_id="aaa111",
    )

    shared = {
        "record_id",
        "record_type",
        "source",
        "schema_version",
        "content_hash",
        "sensitivity",
        "provenance",
        "created_at",
        "updated_at",
        "metadata",
    }

    assert shared <= set(record.to_json_dict())
    assert shared <= set(document.to_json_dict())
    # ...but the payload fields are genuinely different.
    assert "normalized_data" in record.to_json_dict()
    assert "normalized_data" not in document.to_json_dict()
    assert "text" in document.to_json_dict()
