"""TransformationRule execution and computed fields (Steps 18-20, 57).

The safety property under test is narrow and absolute: a transformation is
DATA, and nothing in a mapping profile can become executable code.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from erp_pipeline.schemas.enums import (
    FieldDataType as T,
    TransformationOperation,
)
from erp_pipeline.schemas.mapping_models import TransformationRule
from erp_pipeline.transformation import (
    ComputedField,
    ComputedOperation,
    IssueCode,
    RuleContext,
    SourceRecord,
    TransformationOptions,
    apply_rule,
    apply_rules,
    supported_operations,
    transform_record,
)
from erp_pipeline.transformation.errors import (
    ComputedFieldCycleError,
    TransformationConfigurationError,
    UnsupportedOperationError,
)

from tests.erp_pipeline.transformation.conftest import (
    customer_profile,
    invoice_profile,
    make_mapping,
    make_profile,
)

OPTIONS = TransformationOptions()
PACKAGE = Path("src/erp_pipeline/transformation")


def context(values: dict | None = None, target_type=None) -> RuleContext:
    return RuleContext(
        source_values=values or {}, options=OPTIONS, target_type=target_type
    )


def rule(operation: TransformationOperation, **config) -> TransformationRule:
    return TransformationRule(operation=operation, config=config)


# ============================================================
# The registry (Step 18)
# ============================================================

def test_every_frozen_operation_is_implemented():
    """The engine implements exactly the frozen enum - no more, no less."""
    assert set(supported_operations()) == {
        operation.value for operation in TransformationOperation
    }


def test_the_engine_adds_no_operation_the_contract_does_not_declare():
    for name in supported_operations():
        TransformationOperation.from_value(name)


def test_an_unimplemented_operation_is_fatal_not_skipped(monkeypatch):
    """Silently ignoring a declared step would produce a wrong record."""
    from erp_pipeline.transformation import rules as rule_module

    registry = dict(rule_module._REGISTRY)
    registry.pop(TransformationOperation.TRIM)
    monkeypatch.setattr(rule_module, "_REGISTRY", registry)

    with pytest.raises(UnsupportedOperationError):
        apply_rule("  x  ", rule(TransformationOperation.TRIM), context())


# ============================================================
# No code execution (Steps 18, 42)
# ============================================================

def test_the_package_contains_no_dynamic_execution():
    """Static proof: no eval, exec, compile or __import__ anywhere."""
    forbidden = {"eval", "exec", "compile", "__import__"}
    offenders: list[str] = []

    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in forbidden:
                    offenders.append(f"{path}:{node.lineno} {node.func.id}")

    assert offenders == []


def test_a_rule_config_cannot_carry_a_callable():
    """Phase 1 validates config as a JSON object, which excludes callables."""
    from erp_pipeline.schemas.validation import ValidationError

    with pytest.raises(ValidationError):
        TransformationRule(
            operation=TransformationOperation.CAST,
            config={"to": len},
        )


# ============================================================
# Individual operations
# ============================================================

def test_copy_is_the_identity():
    assert apply_rule("x", rule(TransformationOperation.COPY), context()).value == "x"


def test_rename_does_not_change_the_value():
    """Renaming is expressed by target_field, not by mutating the value."""
    assert apply_rule("x", rule(TransformationOperation.RENAME), context()).value == "x"


def test_cast_converts_to_the_declared_type():
    result = apply_rule(
        "2500.50", rule(TransformationOperation.CAST, to="decimal"), context()
    )

    assert result.value == Decimal("2500.50")


def test_cast_with_an_unknown_type_fails_safely():
    result = apply_rule(
        "x", rule(TransformationOperation.CAST, to="quaternion"), context()
    )

    assert not result.ok


def test_cast_reports_a_conversion_failure():
    result = apply_rule(
        "hello", rule(TransformationOperation.CAST, to="decimal"), context()
    )

    assert not result.ok
    assert result.code is IssueCode.TYPE_CONVERSION_FAILED


def test_default_applies_only_to_null():
    assert apply_rule(
        None, rule(TransformationOperation.DEFAULT, value="X"), context()
    ).value == "X"
    assert apply_rule(
        "present", rule(TransformationOperation.DEFAULT, value="X"), context()
    ).value == "present"


def test_enum_map_translates_a_declared_code():
    result = apply_rule(
        "P",
        rule(TransformationOperation.ENUM_MAP, values={"P": "PENDING"}),
        context(),
    )

    assert result.value == "PENDING"


def test_enum_map_can_fall_back_when_declared():
    result = apply_rule(
        "Z",
        rule(
            TransformationOperation.ENUM_MAP,
            values={"P": "PENDING"},
            on_unknown="fallback",
            fallback="UNSPECIFIED",
        ),
        context(),
    )

    assert result.value == "UNSPECIFIED"


def test_nested_path_rereads_from_the_source():
    result = apply_rule(
        None,
        rule(TransformationOperation.NESTED_PATH, path=["financial", "total"]),
        context({"financial": {"total": "99.00"}}),
    )

    assert result.value == "99.00"


def test_nested_path_reports_a_missing_path():
    result = apply_rule(
        None,
        rule(TransformationOperation.NESTED_PATH, path=["a", "b"]),
        context({"a": {}}),
    )

    assert not result.ok
    assert result.code is IssueCode.SOURCE_FIELD_MISSING


def test_date_parse_uses_the_declared_format():
    result = apply_rule(
        "03/04/2026",
        rule(TransformationOperation.DATE_PARSE, format="%d/%m/%Y", to="date"),
        context(),
    )

    assert result.value == date(2026, 4, 3)


def test_date_parse_produces_an_aware_datetime():
    result = apply_rule(
        "03/04/2026 09:30",
        rule(
            TransformationOperation.DATE_PARSE,
            format="%d/%m/%Y %H:%M",
            to="datetime",
        ),
        context(),
    )

    assert result.value == datetime(2026, 4, 3, 9, 30, tzinfo=timezone.utc)


def test_date_parse_rejects_a_non_matching_value():
    result = apply_rule(
        "not a date",
        rule(TransformationOperation.DATE_PARSE, format="%d/%m/%Y"),
        context(),
    )

    assert not result.ok


def test_concat_joins_declared_source_fields():
    result = apply_rule(
        None,
        rule(TransformationOperation.CONCAT, fields=["a", "b"], separator=" "),
        context({"a": "Jane", "b": "Doe"}),
    )

    assert result.value == "Jane Doe"


def test_concat_reports_a_missing_input():
    result = apply_rule(
        None,
        rule(TransformationOperation.CONCAT, fields=["a", "b"]),
        context({"a": "Jane"}),
    )

    assert not result.ok
    assert result.code is IssueCode.SOURCE_FIELD_MISSING


def test_split_takes_the_declared_component():
    result = apply_rule(
        "a,b,c", rule(TransformationOperation.SPLIT, separator=",", index=1),
        context(),
    )

    assert result.value == "b"


def test_split_reports_an_out_of_range_index():
    result = apply_rule(
        "a", rule(TransformationOperation.SPLIT, separator=",", index=5), context()
    )

    assert not result.ok


def test_trim_strips_whitespace():
    assert apply_rule(
        "  x  ", rule(TransformationOperation.TRIM), context()
    ).value == "x"


def test_constant_ignores_the_source_value():
    assert apply_rule(
        "anything", rule(TransformationOperation.CONSTANT, value="FIXED"), context()
    ).value == "FIXED"


def test_redact_masks_with_a_fixed_mask():
    """A constant mask leaks neither content nor length."""
    from erp_pipeline.transformation import REDACTION_MASK

    assert apply_rule(
        "SECRET", rule(TransformationOperation.REDACT), context()
    ).value == REDACTION_MASK


def test_redact_leaves_null_as_null():
    assert apply_rule(
        None, rule(TransformationOperation.REDACT), context()
    ).value is None


# ============================================================
# Rule chains
# ============================================================

def test_rules_run_in_declared_order():
    result = apply_rules(
        "  2500.50  ",
        (
            rule(TransformationOperation.TRIM),
            rule(TransformationOperation.CAST, to="decimal"),
        ),
        context(),
    )

    assert result.value == Decimal("2500.50")


def test_a_chain_stops_at_the_first_failure():
    result = apply_rules(
        "hello",
        (
            rule(TransformationOperation.CAST, to="decimal"),
            rule(TransformationOperation.CONSTANT, value="never reached"),
        ),
        context(),
    )

    assert not result.ok


def test_a_rule_failure_rejects_the_record(pg_context):
    profile = make_profile(
        "rule.fail",
        [
            make_mapping("cust_no", "customer_id", T.STRING),
            make_mapping(
                "cust_name",
                "name",
                T.STRING,
                transformations=(
                    rule(TransformationOperation.SPLIT, separator=",", index=9),
                ),
            ),
        ],
    )

    result = transform_record(
        SourceRecord.from_mapping({"cust_no": "C001", "cust_name": "Acme"}),
        profile,
        context=pg_context,
    )

    assert not result.is_transformed
    assert IssueCode.RULE_EXECUTION_FAILED.value in result.issue_codes()


# ============================================================
# Computed fields (Steps 19, 20, 57)
# ============================================================

def _computed_profile():
    return make_profile(
        "computed.profile",
        [make_mapping("cust_no", "customer_id", T.STRING)],
    )


def test_a_computed_field_concatenates_source_fields(pg_context):
    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.CONCAT,
                sources=("first_name", "last_name"),
                separator=" ",
            ),
        )
    )

    result = transform_record(
        SourceRecord.from_mapping(
            {"cust_no": "C001", "first_name": "Jane", "last_name": "Doe"}
        ),
        _computed_profile(),
        options=options,
        context=pg_context,
    )

    assert result.is_transformed
    assert result.record.normalized_data["name"] == "Jane Doe"


def test_a_computed_field_is_deterministic(pg_context):
    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.CONCAT,
                sources=("first_name", "last_name"),
                separator=" ",
            ),
        )
    )
    record = SourceRecord.from_mapping(
        {"cust_no": "C001", "first_name": "Jane", "last_name": "Doe"}
    )

    first = transform_record(
        record, _computed_profile(), options=options, context=pg_context
    )
    second = transform_record(
        record, _computed_profile(), options=options, context=pg_context
    )

    assert first.record.normalized_data == second.record.normalized_data


def test_coalesce_takes_the_first_present_value(pg_context):
    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.COALESCE,
                sources=("preferred_name", "legal_name"),
            ),
        )
    )

    result = transform_record(
        SourceRecord.from_mapping({"cust_no": "C001", "legal_name": "Acme Ltd"}),
        _computed_profile(),
        options=options,
        context=pg_context,
    )

    assert result.record.normalized_data["name"] == "Acme Ltd"


def test_a_constant_computed_field_needs_no_source(pg_context):
    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.CONSTANT,
                constant="UNSPECIFIED",
            ),
        )
    )

    result = transform_record(
        SourceRecord.from_mapping({"cust_no": "C001"}),
        _computed_profile(),
        options=options,
        context=pg_context,
    )

    assert result.record.normalized_data["name"] == "UNSPECIFIED"


def test_a_missing_computed_input_is_reported(pg_context):
    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.CONCAT,
                sources=("first_name", "last_name"),
                separator=" ",
            ),
        )
    )

    result = transform_record(
        SourceRecord.from_mapping({"cust_no": "C001", "first_name": "Jane"}),
        _computed_profile(),
        options=options,
        context=pg_context,
    )

    assert IssueCode.COMPUTED_FIELD_INPUT_MISSING.value in result.issue_codes()


def test_a_computed_field_may_skip_missing_inputs_when_declared(pg_context):
    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.CONCAT,
                sources=("first_name", "last_name"),
                separator=" ",
                require_all_inputs=False,
            ),
        )
    )

    result = transform_record(
        SourceRecord.from_mapping({"cust_no": "C001", "first_name": "Jane"}),
        _computed_profile(),
        options=options,
        context=pg_context,
    )

    assert result.is_transformed
    assert result.record.normalized_data["name"] == "Jane"


def test_a_computed_field_can_read_a_mapped_canonical_value(pg_context):
    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.COALESCE,
                sources=("nickname", "customer_id"),
            ),
        )
    )

    result = transform_record(
        SourceRecord.from_mapping({"cust_no": "C001"}),
        _computed_profile(),
        options=options,
        context=pg_context,
    )

    assert result.record.normalized_data["name"] == "C001"


def test_computed_dependencies_are_evaluated_in_order(pg_context):
    """``name`` depends on ``phone``, declared afterwards."""
    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.CONCAT,
                sources=("phone",),
            ),
            ComputedField(
                target_field="phone",
                operation=ComputedOperation.CONSTANT,
                constant="0771234567",
            ),
        )
    )

    result = transform_record(
        SourceRecord.from_mapping({"cust_no": "C001"}),
        _computed_profile(),
        options=options,
        context=pg_context,
    )

    assert result.is_transformed
    assert result.record.normalized_data["name"] == "0771234567"


def test_a_computed_dependency_cycle_is_refused():
    """A depends on B, B depends on A - no evaluation order exists."""
    from erp_pipeline.transformation import RecordTransformer

    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.CONCAT,
                sources=("phone",),
            ),
            ComputedField(
                target_field="phone",
                operation=ComputedOperation.CONCAT,
                sources=("name",),
            ),
        )
    )

    with pytest.raises(ComputedFieldCycleError) as excinfo:
        RecordTransformer(options=options)

    assert excinfo.value.code == IssueCode.COMPUTED_FIELD_DEPENDENCY_CYCLE.value


def test_a_cycle_is_detected_before_any_record_is_read():
    """Finding it on record 40,000 would help nobody."""
    from erp_pipeline.transformation import TransformationService

    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="a", operation=ComputedOperation.CONCAT, sources=("b",)
            ),
            ComputedField(
                target_field="b", operation=ComputedOperation.CONCAT, sources=("a",)
            ),
        )
    )

    with pytest.raises(ComputedFieldCycleError):
        TransformationService(options=options)


def test_a_self_referential_computed_field_is_not_a_cycle():
    """Reading a SOURCE field of the same name is legitimate."""
    from erp_pipeline.transformation import RecordTransformer

    options = TransformationOptions(
        computed_fields=(
            ComputedField(
                target_field="name",
                operation=ComputedOperation.CONCAT,
                sources=("name",),
            ),
        )
    )

    RecordTransformer(options=options)


def test_two_computed_fields_cannot_target_the_same_field():
    with pytest.raises(TransformationConfigurationError):
        TransformationOptions(
            computed_fields=(
                ComputedField(
                    target_field="name",
                    operation=ComputedOperation.CONSTANT,
                    constant="a",
                ),
                ComputedField(
                    target_field="name",
                    operation=ComputedOperation.CONSTANT,
                    constant="b",
                ),
            )
        )


def test_a_non_constant_computed_field_needs_a_source():
    with pytest.raises(TransformationConfigurationError):
        ComputedField(target_field="name", operation=ComputedOperation.CONCAT)


def test_a_constant_computed_field_must_declare_no_source():
    with pytest.raises(TransformationConfigurationError):
        ComputedField(
            target_field="name",
            operation=ComputedOperation.CONSTANT,
            sources=("a",),
        )
