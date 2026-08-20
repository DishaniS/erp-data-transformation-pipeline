"""Pure unit tests for erp_pipeline.catalog.versioning.

No database needed. compare_schemas, next_catalog_version, and
summarize_schema all operate purely on in-memory SourceSchema/SourceSystem
objects.
"""

import pytest

from erp_pipeline.catalog.versioning import (
    BreakingLevel,
    compare_schemas,
    is_identical_content,
    next_catalog_version,
    summarize_schema,
)
from erp_pipeline.schemas import (
    EntityKind,
    FieldDataType,
    RelationshipType,
    SchemaOrigin,
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
    SourceSystem,
    SourceType,
)

from tests.erp_pipeline.catalog.fixtures import (
    build_finance_erp_schema,
    build_finance_erp_schema_v2,
    build_finance_erp_source_system,
)


# ============================================================
# Catalog version arithmetic
# ============================================================

def test_next_catalog_version_starts_at_one():
    assert next_catalog_version(None) == 1


def test_next_catalog_version_increments():
    assert next_catalog_version(1) == 2
    assert next_catalog_version(41) == 42


def test_next_catalog_version_rejects_non_positive_current():
    with pytest.raises(ValueError):
        next_catalog_version(0)


def test_is_identical_content_is_a_hash_comparison():
    assert is_identical_content("abc", "abc") is True
    assert is_identical_content("abc", "def") is False


# ============================================================
# Schema comparison building blocks
# ============================================================

def _entity(name, fields, primary_key_fields=("id",)):
    return SourceEntity(
        entity_id=name,
        source_name=name,
        normalized_name=name,
        entity_kind=EntityKind.TABLE,
        primary_key_fields=primary_key_fields,
        fields=fields,
    )


def _field(name, data_type=FieldDataType.STRING, **kwargs):
    return SourceField(
        source_name=name, normalized_name=name, normalized_data_type=data_type, **kwargs
    )


def _schema(schema_id, entities, relationships=()):
    return SourceSchema(
        schema_id=schema_id,
        source_system_id="test_sys",
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=entities,
        relationships=relationships,
    )


def test_compare_schemas_detects_added_and_removed_entities():
    old = _schema(
        "s1",
        (
            _entity("customer", (_field("id", is_primary_key=True, nullable=False),)),
            _entity("vendor", (_field("id", is_primary_key=True, nullable=False),)),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity("customer", (_field("id", is_primary_key=True, nullable=False),)),
            _entity("invoice", (_field("id", is_primary_key=True, nullable=False),)),
        ),
    )

    diff = compare_schemas(old, new)

    assert diff.added_entities == ("invoice",)
    assert diff.removed_entities == ("vendor",)
    assert diff.breaking_level == BreakingLevel.BREAKING  # removal is breaking


def test_compare_schemas_no_changes_is_non_breaking_with_empty_diff():
    schema = build_finance_erp_schema()
    diff = compare_schemas(schema, schema)

    assert not diff.has_structural_changes
    assert diff.breaking_level == BreakingLevel.NON_BREAKING
    assert diff.added_entities == ()
    assert diff.removed_entities == ()
    assert diff.changed_fields == ()


def test_compare_schemas_ignores_timestamp_only_differences():
    """Comparison must never treat a timestamp difference as a schema change."""
    from datetime import datetime, timedelta, timezone

    old = _schema(
        "s1",
        (_entity("customer", (_field("id", is_primary_key=True, nullable=False),)),),
    )
    new_object = SourceSchema(
        schema_id="s1",
        source_system_id="test_sys",
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=old.entities,
        created_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    diff = compare_schemas(old, new_object)
    assert not diff.has_structural_changes


def test_compare_schemas_detects_added_and_removed_fields():
    old = _schema(
        "s1",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("legacy_note"),
                ),
            ),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("currency_code"),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)

    assert diff.removed_fields == (("invoice", "legacy_note"),)
    assert diff.added_fields == (("invoice", "currency_code"),)


def test_compare_schemas_detects_changed_field_attributes():
    old = _schema(
        "s1",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("amount", data_type=FieldDataType.STRING),
                ),
            ),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("amount", data_type=FieldDataType.DECIMAL),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)

    assert len(diff.changed_fields) == 1
    change = diff.changed_fields[0]
    assert change.entity == "invoice"
    assert change.field == "amount"
    assert change.attribute == "normalized_data_type"
    assert change.old_value == "string"
    assert change.new_value == "decimal"
    assert diff.breaking_level == BreakingLevel.BREAKING


def test_compare_schemas_detects_semantic_type_change_and_agrees_with_hash():
    """Regression test (Phase 0-2 audit fix).

    Proves compare_schemas() and compute_schema_hash() now agree that a
    semantic_type-only change is structural: the diff must report it AND the
    two schemas' hashes must differ. Before the fix, the hashes were
    identical despite this diff, which let CatalogRepository.
    save_schema_snapshot() silently deduplicate a real change.
    """
    old = _schema(
        "s1",
        (
            _entity(
                "customer",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("email", data_type=FieldDataType.STRING),
                ),
            ),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "customer",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    SourceField(
                        source_name="email",
                        normalized_name="email",
                        normalized_data_type=FieldDataType.STRING,
                        semantic_type="email_address",
                    ),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)

    assert len(diff.changed_fields) == 1
    change = diff.changed_fields[0]
    assert change.entity == "customer"
    assert change.field == "email"
    assert change.attribute == "semantic_type"
    assert change.old_value is None
    assert change.new_value == "email_address"

    # The hash must agree with the diff: a schema compare_schemas() calls
    # structurally different must not hash identically.
    assert old.compute_schema_hash() != new.compute_schema_hash()


def test_compare_schemas_detects_added_and_removed_relationships():
    entities = (
        _entity("invoice", (_field("id", is_primary_key=True, nullable=False),)),
        _entity("customer", (_field("id", is_primary_key=True, nullable=False),)),
        _entity("vendor", (_field("id", is_primary_key=True, nullable=False),)),
    )
    old = _schema(
        "s1",
        entities,
        relationships=(
            SourceRelationship(
                relationship_id="r1",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="invoice",
                from_fields=("customer_id",),
                to_entity="customer",
                to_fields=("id",),
            ),
        ),
    )
    new = _schema(
        "s2",
        entities,
        relationships=(
            SourceRelationship(
                relationship_id="r2_different_id_same_structure",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="invoice",
                from_fields=("customer_id",),
                to_entity="customer",
                to_fields=("id",),
            ),
            SourceRelationship(
                relationship_id="r3",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="invoice",
                from_fields=("vendor_id",),
                to_entity="vendor",
                to_fields=("id",),
            ),
        ),
    )

    diff = compare_schemas(old, new)

    # The first relationship is structurally identical despite a different
    # relationship_id, so it must NOT appear as both removed and added.
    assert diff.removed_relationships == ()
    assert diff.added_relationships == (("invoice", "vendor"),)


def test_compare_schemas_relationship_removal_is_potentially_breaking():
    entities = (
        _entity("invoice", (_field("id", is_primary_key=True, nullable=False),)),
        _entity("customer", (_field("id", is_primary_key=True, nullable=False),)),
    )
    relationship = SourceRelationship(
        relationship_id="r1",
        relationship_type=RelationshipType.FOREIGN_KEY,
        from_entity="invoice",
        from_fields=("customer_id",),
        to_entity="customer",
        to_fields=("id",),
    )
    old = _schema("s1", entities, relationships=(relationship,))
    new = _schema("s2", entities, relationships=())

    diff = compare_schemas(old, new)

    assert diff.removed_relationships == (("invoice", "customer"),)
    assert diff.breaking_level == BreakingLevel.POTENTIALLY_BREAKING


# ============================================================
# Rename candidate heuristic (Task 20) - conservative by design
# ============================================================

def test_rename_candidate_reported_for_unambiguous_single_swap():
    old = _schema(
        "s1",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("old_status_code", data_type=FieldDataType.STRING),
                ),
            ),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("new_status_code", data_type=FieldDataType.STRING),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)

    assert len(diff.possible_rename_candidates) == 1
    candidate = diff.possible_rename_candidates[0]
    assert candidate.entity == "invoice"
    assert candidate.removed_field == "old_status_code"
    assert candidate.added_field == "new_status_code"

    # Still reported as plain added/removed too - a candidate is additive
    # information, not a replacement for the raw diff.
    assert ("invoice", "old_status_code") in diff.removed_fields
    assert ("invoice", "new_status_code") in diff.added_fields


def test_no_rename_candidate_when_types_disagree():
    """Never confirm a rename claim the evidence does not support."""
    old = _schema(
        "s1",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("legacy_flag", data_type=FieldDataType.STRING),
                ),
            ),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("new_amount", data_type=FieldDataType.DECIMAL),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)

    assert diff.possible_rename_candidates == ()
    assert ("invoice", "legacy_flag") in diff.removed_fields
    assert ("invoice", "new_amount") in diff.added_fields


def test_no_rename_candidate_across_different_entities():
    """A remove in one entity and an add in another is never a rename candidate."""
    old = _schema(
        "s1",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("legacy_field", data_type=FieldDataType.STRING),
                ),
            ),
            _entity("customer", (_field("id", is_primary_key=True, nullable=False),)),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity("invoice", (_field("id", is_primary_key=True, nullable=False),)),
            _entity(
                "customer",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("new_field", data_type=FieldDataType.STRING),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)
    assert diff.possible_rename_candidates == ()


def test_no_rename_candidate_when_multiple_fields_swapped():
    """Ambiguous evidence (2 removed, 2 added) must not produce a guess."""
    old = _schema(
        "s1",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("field_a", data_type=FieldDataType.STRING),
                    _field("field_b", data_type=FieldDataType.STRING),
                ),
            ),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("field_c", data_type=FieldDataType.STRING),
                    _field("field_d", data_type=FieldDataType.STRING),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)
    assert diff.possible_rename_candidates == ()
    assert len(diff.removed_fields) == 2
    assert len(diff.added_fields) == 2


# ============================================================
# Breaking-change classification (Task 21)
# ============================================================

def test_new_optional_field_is_non_breaking():
    old = _schema(
        "s1",
        (_entity("invoice", (_field("id", is_primary_key=True, nullable=False),)),),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("notes", nullable=True, required=False),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)
    assert diff.breaking_level == BreakingLevel.NON_BREAKING


def test_new_required_non_nullable_field_is_potentially_breaking():
    old = _schema(
        "s1",
        (_entity("invoice", (_field("id", is_primary_key=True, nullable=False),)),),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("mandatory_ref", nullable=False, required=True),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)
    assert diff.breaking_level == BreakingLevel.POTENTIALLY_BREAKING


def test_removed_field_is_breaking():
    old = _schema(
        "s1",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("legacy"),
                ),
            ),
        ),
    )
    new = _schema(
        "s2", (_entity("invoice", (_field("id", is_primary_key=True, nullable=False),)),)
    )

    diff = compare_schemas(old, new)
    assert diff.breaking_level == BreakingLevel.BREAKING


def test_primary_key_status_change_is_breaking():
    old = _schema(
        "s1",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("secondary_key", is_primary_key=False),
                ),
                primary_key_fields=("id",),
            ),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("secondary_key", is_primary_key=True, nullable=False),
                ),
                primary_key_fields=("id", "secondary_key"),
            ),
        ),
    )

    diff = compare_schemas(old, new)
    assert diff.breaking_level == BreakingLevel.BREAKING
    assert any("primary-key status changed" in reason for reason in diff.breaking_reasons)


def test_nullable_to_non_nullable_is_potentially_breaking():
    old = _schema(
        "s1",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("optional_note", nullable=True),
                ),
            ),
        ),
    )
    new = _schema(
        "s2",
        (
            _entity(
                "invoice",
                (
                    _field("id", is_primary_key=True, nullable=False),
                    _field("optional_note", nullable=False),
                ),
            ),
        ),
    )

    diff = compare_schemas(old, new)
    assert diff.breaking_level == BreakingLevel.POTENTIALLY_BREAKING


def test_summary_renders_readable_text():
    old = _schema(
        "s1", (_entity("invoice", (_field("id", is_primary_key=True, nullable=False),)),)
    )
    new = _schema(
        "s2", (_entity("invoice", (_field("id", is_primary_key=True, nullable=False),)),)
    )
    diff = compare_schemas(old, new)
    text = diff.summary()
    assert "s1" in text
    assert "s2" in text
    assert "non_breaking" in text


# ============================================================
# Synthetic FinanceERP V1 -> V2 diff (uses the Task 23 fixture)
# ============================================================

def test_finance_erp_v1_to_v2_diff_matches_the_controlled_changes():
    v1 = build_finance_erp_schema()
    v2 = build_finance_erp_schema_v2()

    diff = compare_schemas(v1, v2)

    assert diff.added_fields == (("customer", "loyalty_tier"),)
    assert diff.removed_fields == (("vendor", "version"),)
    assert len(diff.changed_fields) == 2  # source_data_type + normalized_data_type
    assert {c.attribute for c in diff.changed_fields} == {
        "source_data_type",
        "normalized_data_type",
    }
    assert diff.added_relationships == (("budget", "employee"),)
    assert diff.removed_relationships == ()
    assert diff.breaking_level == BreakingLevel.BREAKING


# ============================================================
# Snapshot summary (Task 22) - calculated, never hardcoded
# ============================================================

def test_summarize_schema_calculates_real_counts():
    system = build_finance_erp_source_system()
    schema = build_finance_erp_schema()

    summary = summarize_schema(system, schema, catalog_version=1)

    assert summary.entity_count == 25
    assert summary.field_count == 184
    assert summary.relationship_count == 31
    assert summary.catalog_version == 1
    assert summary.source_type == "postgresql"
    assert summary.schema_hash == schema.compute_schema_hash()


def test_summarize_schema_reflects_actual_content_not_a_constant():
    """Changing the schema must change the summary, proving it is calculated."""
    system = build_finance_erp_source_system()
    v1 = build_finance_erp_schema()
    v2 = build_finance_erp_schema_v2()

    summary_v1 = summarize_schema(system, v1, catalog_version=1)
    summary_v2 = summarize_schema(system, v2, catalog_version=2)

    assert summary_v1.field_count != summary_v2.field_count or (
        summary_v1.relationship_count != summary_v2.relationship_count
    )
    assert summary_v1.schema_hash != summary_v2.schema_hash


def test_summary_render_contains_calculated_values():
    system = build_finance_erp_source_system()
    schema = build_finance_erp_schema()
    summary = summarize_schema(system, schema, catalog_version=1)

    text = summary.render()
    assert "25" in text
    assert "184" in text
    assert "31" in text
