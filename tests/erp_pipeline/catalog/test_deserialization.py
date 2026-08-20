"""Pure round-trip tests for erp_pipeline.schemas.deserialization.

No database needed - every test here goes model -> to_json_dict() ->
from_*_dict() -> model and checks the result serializes back to an identical
JSON structure.
"""

import json
from datetime import datetime, timezone

import pytest

from erp_pipeline.schemas import (
    CanonicalDocument,
    CanonicalRecord,
    DeserializationError,
    EntityKind,
    FieldDataType,
    FieldMapping,
    MappingProfile,
    MappingStatus,
    QualitySeverity,
    RecordProvenance,
    RelationshipType,
    RunStatus,
    SchemaOrigin,
    SourceEntity,
    SourceField,
    SourceReference,
    SourceRelationship,
    SourceSchema,
    SourceSystem,
    SourceType,
    TransformationOperation,
    TransformationRule,
    canonical_document_from_dict,
    canonical_record_from_dict,
    data_quality_issue_from_dict,
    field_mapping_from_dict,
    mapping_profile_from_dict,
    source_entity_from_dict,
    source_field_from_dict,
    source_relationship_from_dict,
    source_schema_from_dict,
    source_system_from_dict,
    transformation_rule_from_dict,
    transformation_run_from_dict,
)
from erp_pipeline.schemas.run_models import DataQualityIssue, TransformationRun


def _round_trip_equal(model, from_dict_fn) -> bool:
    payload = model.to_json_dict()
    rebuilt = from_dict_fn(payload)
    return rebuilt.to_json_dict() == payload


def test_source_system_round_trips():
    model = SourceSystem(
        source_system_id="finance_erp_pg",
        name="Finance ERP",
        source_type=SourceType.POSTGRESQL,
        environment="research",
        metadata={"region": "apac"},
    )
    assert _round_trip_equal(model, source_system_from_dict)


def test_source_field_round_trips_with_nested_path():
    model = SourceField(
        source_name="total",
        normalized_name="financial_total",
        source_data_type="int32",
        normalized_data_type=FieldDataType.DECIMAL,
        nested_path=("financial", "summary"),
        metadata={"origin": "mongo"},
    )
    assert _round_trip_equal(model, source_field_from_dict)
    rebuilt = source_field_from_dict(model.to_json_dict())
    assert rebuilt.nested_path == ("financial", "summary")


def test_source_field_round_trips_without_nested_path():
    model = SourceField(source_name="id", normalized_name="id", is_primary_key=True, nullable=False)
    rebuilt = source_field_from_dict(model.to_json_dict())
    assert rebuilt.nested_path is None


def test_source_entity_round_trips_with_fields():
    model = SourceEntity(
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
        ),
    )
    assert _round_trip_equal(model, source_entity_from_dict)


def test_source_relationship_round_trips():
    model = SourceRelationship(
        relationship_id="fk_invoice_customer",
        relationship_type=RelationshipType.FOREIGN_KEY,
        from_entity="invoice",
        from_fields=("customer_ref",),
        to_entity="customer",
        to_fields=("customer_id",),
        confidence=0.85,
    )
    assert _round_trip_equal(model, source_relationship_from_dict)


def test_source_schema_round_trips_fully():
    model = SourceSchema(
        schema_id="finance_erp_pg_public_v1",
        source_system_id="finance_erp_pg",
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(
            SourceEntity(
                entity_id="inv",
                source_name="inv",
                normalized_name="inv",
                primary_key_fields=("id",),
                fields=(
                    SourceField(
                        source_name="id",
                        normalized_name="id",
                        is_primary_key=True,
                        nullable=False,
                    ),
                ),
            ),
        ),
        relationships=(),
        metadata={"note": "test"},
    )
    payload = model.to_json_dict()
    rebuilt = source_schema_from_dict(payload)
    assert rebuilt.to_json_dict() == payload
    assert rebuilt.compute_schema_hash() == model.compute_schema_hash()


def test_canonical_record_round_trips():
    model = CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id="finance_erp_pg", source_type=SourceType.POSTGRESQL
        ),
        entity_type="invoice",
        stable_source_key="INV-001",
        normalized_data={"amount": 25000.0, "status": "approved"},
        provenance=RecordProvenance(ingestion_method="batch_extract"),
    )
    assert _round_trip_equal(model, canonical_record_from_dict)


def test_canonical_document_round_trips():
    model = CanonicalDocument.from_source(
        source=SourceReference(source_system_id="policy_library", source_type=SourceType.PDF),
        document_id="abc123",
        text="some policy text",
        page_count=3,
    )
    assert _round_trip_equal(model, canonical_document_from_dict)


def test_transformation_rule_round_trips():
    model = TransformationRule(
        operation=TransformationOperation.ENUM_MAP,
        config={"values": {"Y": "approved"}},
    )
    assert _round_trip_equal(model, transformation_rule_from_dict)


def test_field_mapping_round_trips_with_transformations():
    model = FieldMapping(
        source_field="total_amount",
        target_field="amount",
        transformations=(
            TransformationRule(
                operation=TransformationOperation.CAST, config={"to": "decimal"}
            ),
        ),
        status=MappingStatus.SUGGESTED,
    )
    assert _round_trip_equal(model, field_mapping_from_dict)


def test_mapping_profile_round_trips_fully():
    model = MappingProfile(
        mapping_id="finance_erp_pg_invoice_v1",
        source_system_id="finance_erp_pg",
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(source_field="invoice_no", target_field="invoice_id"),
        ),
        status=MappingStatus.APPROVED,
        approved_by="IT22267290",
        approved_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    assert _round_trip_equal(model, mapping_profile_from_dict)


def test_transformation_run_round_trips():
    model = TransformationRun(
        run_id="run_0001",
        source_system_id="finance_erp_pg",
        status=RunStatus.SUCCEEDED,
        started_at=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 10, 9, 5, tzinfo=timezone.utc),
        records_read=100,
    )
    assert _round_trip_equal(model, transformation_run_from_dict)


def test_data_quality_issue_round_trips():
    model = DataQualityIssue(
        issue_id="issue_0001",
        severity=QualitySeverity.ERROR,
        code="NON_NUMERIC_AMOUNT",
        message="Could not parse amount.",
    )
    assert _round_trip_equal(model, data_quality_issue_from_dict)


def test_deserialization_never_uses_eval_exec_or_pickle():
    """Task 1 hard requirement, verified structurally."""
    import ast
    from pathlib import Path

    module_path = Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "schemas" / "deserialization.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    forbidden_names = {"eval", "exec", "compile", "pickle", "__import__"}
    found = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            found.add(node.id)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "pickle":
                    found.add("pickle")
        if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
            found.add(node.attr)

    assert not found, f"deserialization.py uses forbidden constructs: {found}"


def test_malformed_payload_raises_deserialization_error_not_crash():
    with pytest.raises(DeserializationError):
        source_system_from_dict({"name": "missing required fields"})


def test_json_round_trip_through_real_json_dumps_loads():
    """Prove the full text round trip, not just the dict round trip."""
    entity = SourceEntity(
        entity_id="inv", source_name="inv", normalized_name="inv"
    )
    text = json.dumps(entity.to_json_dict())
    rebuilt = source_entity_from_dict(json.loads(text))
    assert rebuilt.to_json_dict() == entity.to_json_dict()
