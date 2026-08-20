"""Schema drift detection, classification and mapping impact.

Proofs C and D, and Steps 41-55. The recurring principle: schema change is
never silently absorbed, and never blindly panicked about either - the active
mapping decides which of the two it is.
"""

from __future__ import annotations

import pytest

from erp_pipeline.catalog.versioning import SchemaDiff, compare_schemas
from erp_pipeline.schemas.enums import FieldDataType as T, MappingStatus
from erp_pipeline.schemas.mapping_models import FieldMapping, MappingProfile
from erp_pipeline.sync import (
    DriftSeverity,
    DriftStatus,
    DriftType,
    ImpactAction,
    ImpactKind,
    SyncOptions,
    SyncStatus,
    analyze_mapping_impact,
    detect_drift,
)

from tests.erp_pipeline.sync.conftest import (
    Harness,
    invoice_profile,
    invoice_row,
    make_field,
    make_schema,
    schema_v1,
    schema_v2_type_changed_and_field_added,
    schema_v3_field_removed,
)


def gate(harness: Harness, new_schema, previous):
    return harness.service.check_drift(harness.target, new_schema, previous)


# ============================================================
# Reuse, not reimplementation (Step 42)
# ============================================================

def test_drift_detection_uses_the_phase_2_schema_diff():
    report = detect_drift(schema_v1(), schema_v2_type_changed_and_field_added())

    assert isinstance(report.diff, SchemaDiff)


def test_the_sync_package_builds_no_second_diff_engine():
    from pathlib import Path

    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/erp_pipeline/sync").rglob("*.py")
    )

    assert "def compare_schemas" not in text
    assert "from erp_pipeline.catalog.versioning import" in text


def test_the_sync_package_runs_no_schema_discovery():
    """Step 41: Phases 4-7 already do that."""
    from pathlib import Path

    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/erp_pipeline/sync").rglob("*.py")
    )

    for marker in ("inspect(", "get_columns", "INFORMATION_SCHEMA", "list_collections"):
        assert marker not in text


# ============================================================
# No drift
# ============================================================

def test_an_identical_schema_reports_no_drift():
    report = detect_drift(schema_v1(), schema_v1())

    assert report.status is DriftStatus.NO_DRIFT
    assert not report.has_drift


def test_a_first_discovery_is_a_baseline_not_drift():
    """Otherwise every new source would be blocked on its first run."""
    report = detect_drift(None, schema_v1())

    assert report.status is DriftStatus.NO_DRIFT
    assert report.old_schema_id is None


def test_no_drift_allows_data_sync(empty_harness):
    report = gate(empty_harness, schema_v1(), schema_v1())

    assert report.status.allows_data_sync


# ============================================================
# PROOF C - type change plus added field (Step 54)
# ============================================================

def test_proof_c_detects_the_type_change():
    report = detect_drift(schema_v1(), schema_v2_type_changed_and_field_added())

    type_changes = report.findings_of(DriftType.FIELD_TYPE_CHANGED)
    normalized = [f for f in type_changes if f.attribute == "normalized_data_type"]

    assert normalized
    assert any(
        f.field_name == "amount"
        and str(f.old_value) == "decimal"
        and str(f.new_value) == "string"
        for f in normalized
    )


def test_proof_c_detects_the_added_field():
    report = detect_drift(schema_v1(), schema_v2_type_changed_and_field_added())

    added = report.findings_of(DriftType.FIELD_ADDED)

    assert [f.field_name for f in added] == ["tax_amount"]


def test_proof_c_marks_the_mapped_type_change_for_review(empty_harness):
    report = gate(
        empty_harness, schema_v2_type_changed_and_field_added(), schema_v1()
    )

    impacts = report.impact.impacts_for("amount")

    assert impacts
    assert impacts[0].action is ImpactAction.MAPPING_REVIEW_REQUIRED
    assert impacts[0].kind is ImpactKind.TYPE_COMPATIBILITY_CHANGED


def test_proof_c_reports_the_new_field_as_unmapped(empty_harness):
    """Step 47: reported, never auto-mapped."""
    report = gate(
        empty_harness, schema_v2_type_changed_and_field_added(), schema_v1()
    )

    assert "tax_amount" in report.impact.unmapped_new_fields
    assert (
        report.impact.impacts_for("tax_amount")[0].action
        is ImpactAction.UNMAPPED_NEW_FIELD
    )


def test_proof_c_does_not_silently_continue(empty_harness):
    report = gate(
        empty_harness, schema_v2_type_changed_and_field_added(), schema_v1()
    )

    assert report.status is DriftStatus.REVIEW_REQUIRED


def test_a_decimal_to_string_change_is_not_excused_by_phase_9(empty_harness):
    """Step 48: Phase 9 could parse it, but the CONTRACT changed."""
    report = gate(
        empty_harness, schema_v2_type_changed_and_field_added(), schema_v1()
    )

    impact = report.impact.impacts_for("amount")[0]

    assert impact.action is ImpactAction.MAPPING_REVIEW_REQUIRED
    assert "lossy" in (impact.detail or "")


def test_type_compatibility_comes_from_phase_8():
    from pathlib import Path

    text = Path("src/erp_pipeline/sync/impact.py").read_text(encoding="utf-8")

    assert "from erp_pipeline.mapping.compatibility import" in text
    assert "compare_types(" in text


def test_a_compatible_type_change_does_not_demand_review(empty_harness):
    """INTEGER -> DECIMAL widens losslessly into a DECIMAL target."""
    widened = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING),
            make_field("amount", T.DECIMAL, source_data_type="numeric(14,2)"),
        ),
        schema_id="erp_pg.phase10.v1b",
    )

    report = gate(empty_harness, widened, schema_v1())

    assert report.status is not DriftStatus.BLOCKED


# ============================================================
# PROOF D - removed mapped field (Steps 46, 55)
# ============================================================

def test_proof_d_detects_the_removal():
    report = detect_drift(schema_v1(), schema_v3_field_removed())

    removed = report.findings_of(DriftType.FIELD_REMOVED)

    assert [f.field_name for f in removed] == ["amount"]
    assert removed[0].severity is DriftSeverity.BREAKING


def test_proof_d_marks_the_mapping_invalid(empty_harness):
    """``amount`` feeds a REQUIRED canonical target."""
    report = gate(empty_harness, schema_v3_field_removed(), schema_v1())

    impact = report.impact.impacts_for("amount")[0]

    assert impact.kind is ImpactKind.SOURCE_FIELD_REMOVED
    assert impact.action is ImpactAction.MAPPING_INVALID


def test_proof_d_blocks_the_sync(empty_harness):
    report = gate(empty_harness, schema_v3_field_removed(), schema_v1())

    assert report.status is DriftStatus.BLOCKED
    assert not report.status.allows_data_sync


def test_proof_d_processes_no_data(empty_harness):
    """Step 51: the gate runs BEFORE extraction."""
    empty_harness.source.add(invoice_row(1))

    result = empty_harness.service.run(
        empty_harness.target,
        empty_harness.source,
        new_schema=schema_v3_field_removed(),
        previous_schema=schema_v1(),
        strategy=empty_harness.strategy,
        watermark_field="updated_at",
        tie_break_field="id",
    )

    assert result.blocked
    assert result.summary is None
    assert len(empty_harness.canonical) == 0


def test_a_blocked_entity_is_persisted_as_blocked(empty_harness):
    empty_harness.service.run(
        empty_harness.target,
        empty_harness.source,
        new_schema=schema_v3_field_removed(),
        previous_schema=schema_v1(),
    )

    assert empty_harness.state.status is SyncStatus.BLOCKED


def test_a_blocked_entity_refuses_a_later_data_run(empty_harness):
    empty_harness.service.run(
        empty_harness.target,
        empty_harness.source,
        new_schema=schema_v3_field_removed(),
        previous_schema=schema_v1(),
    )
    empty_harness.source.add(invoice_row(1))

    summary = empty_harness.run()

    assert summary.status.value == "blocked"
    assert summary.changes_read == 0


def test_a_block_is_only_cleared_deliberately(empty_harness):
    empty_harness.service.run(
        empty_harness.target,
        empty_harness.source,
        new_schema=schema_v3_field_removed(),
        previous_schema=schema_v1(),
    )

    empty_harness.service.clear_block(empty_harness.target)

    assert empty_harness.state.status is SyncStatus.ACTIVE


def test_an_unmapped_removed_field_does_not_block(empty_harness):
    """A false alarm teaches people to ignore the gate."""
    with_extra = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING),
            make_field("amount", T.DECIMAL),
            make_field("internal_note", T.STRING),
        ),
        schema_id="erp_pg.phase10.v0",
    )

    report = gate(empty_harness, schema_v1(), with_extra)

    assert report.status is not DriftStatus.BLOCKED


# ============================================================
# Drift types and classification (Steps 43, 44, 49)
# ============================================================

def test_an_added_entity_is_non_breaking():
    old = schema_v1()
    new = make_schema(
        (make_field("id", T.STRING),), entity_name="phase10_payment",
        schema_id="erp_pg.phase10.v9",
    )
    combined = make_schema(
        tuple(old.entities[0].fields), schema_id="erp_pg.phase10.v9"
    )

    report = detect_drift(old, combined)

    assert report.status is DriftStatus.NO_DRIFT


def test_a_removed_entity_is_breaking(empty_harness):
    other = make_schema(
        (make_field("id", T.STRING),),
        entity_name="phase10_other",
        schema_id="erp_pg.phase10.vx",
    )

    report = detect_drift(schema_v1(), other)

    assert report.findings_of(DriftType.ENTITY_REMOVED)
    assert report.severity is DriftSeverity.BREAKING


def test_a_nullability_change_is_detected():
    tightened = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING, nullable=False),
            make_field("amount", T.DECIMAL),
        ),
        schema_id="erp_pg.phase10.v4",
    )

    report = detect_drift(schema_v1(), tightened)

    findings = report.findings_of(DriftType.FIELD_NULLABILITY_CHANGED)

    assert findings
    assert findings[0].severity is DriftSeverity.POTENTIALLY_BREAKING


def test_a_primary_key_change_is_breaking():
    """Step 49: incremental identity depends on it."""
    repointed = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=False),
            make_field("customer_id", T.STRING, nullable=False, is_primary_key=True),
            make_field("amount", T.DECIMAL),
        ),
        schema_id="erp_pg.phase10.v5",
    )

    report = detect_drift(schema_v1(), repointed)

    findings = report.findings_of(DriftType.PRIMARY_KEY_CHANGED)

    assert findings
    assert all(f.severity is DriftSeverity.BREAKING for f in findings)


def test_a_primary_key_change_demands_review(empty_harness):
    repointed = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=False),
            make_field("customer_id", T.STRING, nullable=False, is_primary_key=True),
            make_field("amount", T.DECIMAL),
        ),
        schema_id="erp_pg.phase10.v5",
    )

    report = gate(empty_harness, repointed, schema_v1())

    assert report.status in (DriftStatus.REVIEW_REQUIRED, DriftStatus.BLOCKED)


def test_a_new_optional_field_is_non_breaking():
    report = detect_drift(schema_v1(), schema_v2_type_changed_and_field_added())

    added = report.findings_of(DriftType.FIELD_ADDED)[0]

    assert added.severity is DriftSeverity.NON_BREAKING


def test_a_new_required_field_is_potentially_breaking():
    with_required = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING),
            make_field("amount", T.DECIMAL),
            make_field("tax_code", T.STRING, nullable=False, required=True),
        ),
        schema_id="erp_pg.phase10.v6",
    )

    report = detect_drift(schema_v1(), with_required)

    added = report.findings_of(DriftType.FIELD_ADDED)[0]

    assert added.severity is DriftSeverity.POTENTIALLY_BREAKING


def test_a_drift_finding_carries_no_business_values():
    report = detect_drift(schema_v1(), schema_v2_type_changed_and_field_added())

    payload = str(report.to_dict())

    assert "SECRET" not in payload
    for finding in report.findings:
        assert finding.entity
        assert isinstance(finding.to_dict()["old_value"], (str, type(None)))


# ============================================================
# Mapping impact without a profile
# ============================================================

def test_drift_without_an_active_mapping_is_informational():
    report = detect_drift(schema_v1(), schema_v3_field_removed())

    impact = analyze_mapping_impact(report, None, schema_v3_field_removed())

    assert impact.status is DriftStatus.NON_BREAKING_DRIFT
    assert impact.impacts == ()


def test_only_decided_mappings_are_analyzed():
    """A suggested mapping is not in production, so it cannot be broken."""
    profile = MappingProfile(
        mapping_id="suggested.only",
        source_system_id="erp_pg",
        source_entity="phase10_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(
                source_field="amount",
                target_field="amount",
                target_type=T.DECIMAL,
                status=MappingStatus.SUGGESTED,
            ),
        ),
    )

    report = detect_drift(schema_v1(), schema_v3_field_removed())
    impact = analyze_mapping_impact(report, profile, schema_v3_field_removed())

    assert impact.status is not DriftStatus.BLOCKED


def test_the_impact_report_names_source_target_drift_and_action(empty_harness):
    """Exactly the shape Step 45 asks for."""
    report = gate(
        empty_harness, schema_v2_type_changed_and_field_added(), schema_v1()
    )
    impact = report.impact.impacts_for("amount")[0]

    assert impact.source_field == "amount"
    assert impact.target_field == "amount"
    assert impact.drift_type is DriftType.FIELD_TYPE_CHANGED
    assert impact.action is ImpactAction.MAPPING_REVIEW_REQUIRED


def test_the_impact_report_serializes_safely(empty_harness):
    report = gate(
        empty_harness, schema_v2_type_changed_and_field_added(), schema_v1()
    )

    payload = report.impact.to_dict()

    assert payload["mapping_id"] == "p10.inv"
    assert payload["impact_count"] >= 2
    assert "unmapped_new_fields" in payload


# ============================================================
# Determinism (Step 74)
# ============================================================

def test_drift_classification_is_deterministic():
    first = detect_drift(schema_v1(), schema_v2_type_changed_and_field_added())
    second = detect_drift(schema_v1(), schema_v2_type_changed_and_field_added())

    assert first.to_dict() == second.to_dict()


def test_mapping_impact_is_deterministic(empty_harness):
    first = gate(empty_harness, schema_v2_type_changed_and_field_added(), schema_v1())
    second = gate(empty_harness, schema_v2_type_changed_and_field_added(), schema_v1())

    assert first.impact.to_dict() == second.impact.to_dict()


# ============================================================
# Drift gate ordering (Step 51)
# ============================================================

def test_a_non_breaking_drift_still_runs_the_data_sync(empty_harness):
    empty_harness.source.add(invoice_row(1))

    result = empty_harness.service.run(
        empty_harness.target,
        empty_harness.source,
        new_schema=schema_v1(),
        previous_schema=schema_v1(),
        strategy=empty_harness.strategy,
        watermark_field="updated_at",
        tie_break_field="id",
    )

    assert not result.blocked
    assert result.summary.changes_read == 1


def test_the_drift_gate_can_be_switched_off(empty_harness):
    empty_harness.source.add(invoice_row(1))

    result = empty_harness.service.run(
        empty_harness.target,
        empty_harness.source,
        new_schema=schema_v3_field_removed(),
        previous_schema=schema_v1(),
        options=SyncOptions(batch_size=10, check_drift=False),
        strategy=empty_harness.strategy,
        watermark_field="updated_at",
        tie_break_field="id",
    )

    assert not result.blocked
    assert result.drift is None
