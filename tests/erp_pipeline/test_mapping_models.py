"""Mapping contract tests.

These verify the SHAPE of mapping definitions. No mapping engine exists in
Phase 1, so nothing here executes a transformation - the point is that a
mapping is reviewable, serializable data and that it cannot carry code.
"""

import json
from datetime import datetime, timezone

import pytest

from erp_pipeline.schemas import (
    FieldDataType,
    FieldMapping,
    MappingProfile,
    MappingStatus,
    TransformationOperation,
    TransformationRule,
    ValidationError,
)
from erp_pipeline.version import MAPPING_MODEL_VERSION


def build_profile(**overrides) -> MappingProfile:
    kwargs = dict(
        mapping_id="finance_erp_pg_invoice_v1",
        source_system_id="finance_erp_pg",
        source_entity="fin_invoice",
        target_entity_type="invoice",
        source_schema_id="finance_erp_pg_public_v1",
        field_mappings=(
            FieldMapping(
                source_field="invoice_no",
                target_field="invoice_id",
                source_type=FieldDataType.STRING,
                target_type=FieldDataType.STRING,
                confidence=1.0,
                status=MappingStatus.APPROVED,
            ),
            FieldMapping(
                source_field="total_amount",
                target_field="amount",
                source_type=FieldDataType.DECIMAL,
                target_type=FieldDataType.DECIMAL,
                transformations=(
                    TransformationRule(
                        operation=TransformationOperation.CAST,
                        config={"to": "decimal", "scale": 2},
                    ),
                ),
                confidence=0.92,
                status=MappingStatus.SUGGESTED,
                reason="Name similarity plus compatible numeric type.",
            ),
        ),
    )
    kwargs.update(overrides)
    return MappingProfile(**kwargs)


# ============================================================
# TransformationRule: data, never code
# ============================================================

def test_transformation_rule_is_declarative_data():
    rule = TransformationRule(
        operation=TransformationOperation.ENUM_MAP,
        config={"values": {"Y": "approved", "N": "rejected"}},
        description="Map the SQL Server approval flag onto canonical statuses.",
    )

    payload = rule.to_json_dict()
    assert payload["operation"] == "enum_map"
    assert payload["config"]["values"]["Y"] == "approved"
    assert json.loads(json.dumps(payload)) == payload


def test_transformation_rule_rejects_callable_config():
    """A mapping author must not be able to smuggle executable behaviour."""
    with pytest.raises(ValidationError, match="not JSON-serializable"):
        TransformationRule(
            operation=TransformationOperation.CAST,
            config={"fn": lambda value: value * 2},
        )


def test_transformation_rule_rejects_unknown_operation():
    with pytest.raises(ValueError, match="not a valid TransformationOperation"):
        TransformationRule(operation="exec_python", config={})


def test_transformation_rule_has_no_code_field():
    """No field exists that an engine could pass to eval/exec."""
    payload = TransformationRule(operation=TransformationOperation.COPY).to_json_dict()

    forbidden = {"code", "expression", "script", "python", "eval", "lambda", "sql"}
    assert not (set(payload) & forbidden)


def test_nested_path_operation_carries_a_path_config():
    """The operation a MongoDB mapping would need is expressible as data."""
    rule = TransformationRule(
        operation=TransformationOperation.NESTED_PATH,
        config={"path": ["financial", "total"]},
    )

    assert rule.to_json_dict()["config"]["path"] == ["financial", "total"]


# ============================================================
# FieldMapping
# ============================================================

def test_field_mapping_defaults_to_suggested():
    mapping = FieldMapping(source_field="inv_id", target_field="invoice_id")

    assert mapping.status is MappingStatus.SUGGESTED
    assert mapping.confidence == 1.0
    assert mapping.transformations == ()


@pytest.mark.parametrize("confidence", [0.0, 0.25, 1.0])
def test_field_mapping_accepts_confidence_boundaries(confidence):
    mapping = FieldMapping(
        source_field="a", target_field="b", confidence=confidence
    )
    assert mapping.confidence == confidence


@pytest.mark.parametrize("confidence", [-0.001, 1.001, 42])
def test_field_mapping_rejects_confidence_out_of_range(confidence):
    with pytest.raises(ValidationError, match="between 0.0 and 1.0"):
        FieldMapping(source_field="a", target_field="b", confidence=confidence)


def test_field_mapping_target_must_be_a_normalized_canonical_name():
    with pytest.raises(ValidationError, match="normalized identifier"):
        FieldMapping(source_field="InvoiceNumber", target_field="Invoice Id")


def test_field_mapping_source_field_keeps_vendor_spelling():
    """The source side is the vendor's name and must not be forced to normalize."""
    mapping = FieldMapping(source_field="InvoiceNumber", target_field="invoice_id")
    assert mapping.source_field == "InvoiceNumber"


def test_field_mapping_rejects_non_rule_transformation():
    with pytest.raises(ValidationError, match="never a callable or a code string"):
        FieldMapping(
            source_field="a",
            target_field="b",
            transformations=[lambda value: value],
        )


# ============================================================
# MappingProfile
# ============================================================

def test_mapping_profile_serializes_completely():
    profile = build_profile()
    payload = profile.to_json_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["mapping_id"] == "finance_erp_pg_invoice_v1"
    assert payload["target_entity_type"] == "invoice"
    assert payload["model_version"] == MAPPING_MODEL_VERSION
    assert len(payload["field_mappings"]) == 2
    assert payload["field_mappings"][1]["transformations"][0]["operation"] == "cast"


def test_mapping_profile_reports_target_fields_and_pending_review():
    profile = build_profile()

    assert profile.target_fields == ("invoice_id", "amount")
    pending = profile.mappings_requiring_review()
    assert len(pending) == 1
    assert pending[0].target_field == "amount"


def test_mapping_profile_rejects_duplicate_instruction():
    with pytest.raises(ValidationError, match="more than once"):
        build_profile(
            field_mappings=(
                FieldMapping(source_field="a", target_field="invoice_id"),
                FieldMapping(source_field="a", target_field="invoice_id"),
            )
        )


def test_mapping_profile_allows_two_sources_feeding_one_target():
    """A future concat/coalesce needs this, so it must stay legal."""
    profile = build_profile(
        field_mappings=(
            FieldMapping(source_field="first_name", target_field="customer_name"),
            FieldMapping(source_field="last_name", target_field="customer_name"),
        )
    )

    assert profile.target_fields == ("customer_name",)


def test_approved_profile_must_record_who_approved_it():
    with pytest.raises(ValidationError, match="must record who granted it"):
        build_profile(status=MappingStatus.APPROVED)


def test_approved_profile_with_approver_is_valid():
    profile = build_profile(
        status=MappingStatus.APPROVED,
        approved_by="IT22267290",
        approved_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )

    assert profile.to_json_dict()["approved_by"] == "IT22267290"
    assert profile.to_json_dict()["approved_at"] == "2026-08-10T00:00:00Z"


def test_mapping_profile_rejects_blank_id():
    with pytest.raises(ValidationError, match="must not be blank"):
        build_profile(mapping_id="")


def test_mapping_profile_metadata_rejects_credentials():
    with pytest.raises(ValidationError, match="must not contain credentials"):
        build_profile(metadata={"source_db_password": "hunter2"})


def test_mapping_profile_is_scoped_to_one_source_system():
    """The same canonical entity needs a different profile per ERP."""
    pg_profile = build_profile()
    mysql_profile = build_profile(
        mapping_id="ops_erp_mysql_invoice_v1",
        source_system_id="ops_erp_mysql",
        source_entity="tbl_invoice",
        source_schema_id=None,
        field_mappings=(
            FieldMapping(source_field="inv_id", target_field="invoice_id"),
        ),
    )

    assert pg_profile.target_entity_type == mysql_profile.target_entity_type
    assert pg_profile.source_system_id != mysql_profile.source_system_id
    assert pg_profile.mapping_id != mysql_profile.mapping_id


def test_no_mapping_engine_is_exposed():
    """Phase 1 defines contracts only; there must be nothing that executes."""
    import erp_pipeline.schemas.mapping_models as module

    executable_names = [
        name
        for name in dir(module)
        if any(token in name.lower() for token in ("apply", "execute", "run", "engine", "transform_value"))
    ]
    assert executable_names == []
