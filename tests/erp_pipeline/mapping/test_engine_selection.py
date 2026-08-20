"""Candidate generation, selection, ambiguity, collisions and overrides.

The behavioural heart of Phase 8: what the engine decides, and - just as
importantly - what it refuses to decide.
"""

from __future__ import annotations

import pytest

from erp_pipeline.mapping import (
    DEFAULT_CANONICAL_MODEL,
    CanonicalTargetNotFoundError,
    ConfidenceLevel,
    FieldOutcome,
    InvalidMappingOverrideError,
    MappingEngine,
    MappingOptions,
    MappingOverride,
    MappingService,
    NameMatchKind,
    RejectedCandidate,
    ScoringWeights,
    SourceFieldNotFoundError,
    generate_mapping,
)
from erp_pipeline.mapping.errors import MappingConfigurationError
from erp_pipeline.schemas.enums import FieldDataType, MappingStatus

from tests.erp_pipeline.mapping.conftest import make_entity, make_field, make_schema
from erp_pipeline.schemas.enums import EntityKind, SchemaOrigin, SourceType

T = FieldDataType


def schema_with(*fields, entity_name: str = "fin_invoice"):
    return make_schema(
        "probe_sys", SourceType.POSTGRESQL, SchemaOrigin.DISCOVERED,
        entities=(make_entity(entity_name, tuple(fields)),),
    )


# ============================================================
# Matching strength (Steps 9, 10)
# ============================================================

def test_an_exact_name_match_is_the_strongest_evidence():
    result = generate_mapping(schema_with(make_field("amount", T.DECIMAL)))
    decision = result.decision_for("amount")

    assert decision.selected.evidence.name.kind is NameMatchKind.EXACT
    assert decision.selected.target_field == "amount"


def test_a_normalized_match_is_recognized_across_conventions():
    result = generate_mapping(schema_with(make_field("invoiceId", T.STRING)))
    decision = result.decision_for("invoiceId")

    assert decision.selected.evidence.name.kind is NameMatchKind.NORMALIZED_EXACT
    assert decision.selected.target_field == "invoice_id"


def test_an_explicit_alias_match_quotes_its_declaration():
    result = generate_mapping(schema_with(make_field("cust_no", T.STRING)))
    decision = result.decision_for("cust_no")
    evidence = decision.selected.evidence.name

    assert evidence.kind is NameMatchKind.EXPLICIT_ALIAS
    assert evidence.matched_alias == "cust_no"
    assert decision.selected.target_field == "customer_id"


def test_an_exact_match_outranks_a_merely_similar_one():
    """`invoice_id` must beat `invoice_no`-style alternatives."""
    result = generate_mapping(schema_with(make_field("invoice_id", T.STRING)))
    candidates = result.decision_for("invoice_id").candidates

    assert candidates[0].target_field == "invoice_id"
    assert candidates[0].score.total > (
        candidates[1].score.total if len(candidates) > 1 else 0
    )


def test_token_overlap_produces_a_weaker_candidate():
    result = generate_mapping(
        schema_with(make_field("invoice_reference_code", T.STRING))
    )
    decision = result.decision_for("invoice_reference_code")

    assert decision.candidates
    assert decision.candidates[0].evidence.name.kind in (
        NameMatchKind.TOKEN_OVERLAP, NameMatchKind.EXPLICIT_ALIAS,
    )


def test_candidates_are_ordered_by_score_then_name():
    result = generate_mapping(schema_with(make_field("customer_id", T.STRING)))
    candidates = result.decision_for("customer_id").candidates

    scores = [candidate.score.total for candidate in candidates]
    assert scores == sorted(scores, reverse=True)


def test_a_field_with_no_name_evidence_produces_no_candidates():
    """Entity and path context corroborate a name match; they never substitute
    for one."""
    result = generate_mapping(
        schema_with(make_field("legacy_internal_flag_74", T.INTEGER))
    )
    decision = result.decision_for("legacy_internal_flag_74")

    assert decision.candidates == ()
    assert decision.outcome is FieldOutcome.UNMAPPED


# ============================================================
# Context (Steps 7, 13)
# ============================================================

def test_entity_context_decides_between_identically_named_targets():
    """`customer_id` exists on both invoice and customer; the table says
    which is meant."""
    invoice_result = generate_mapping(
        schema_with(make_field("customer_id", T.STRING), entity_name="fin_invoice")
    )
    customer_result = generate_mapping(
        schema_with(make_field("customer_id", T.STRING), entity_name="fin_customer")
    )

    assert invoice_result.decision_for("customer_id").selected.target_entity_type == (
        "invoice"
    )
    assert customer_result.decision_for("customer_id").selected.target_entity_type == (
        "customer"
    )


def test_a_nested_path_prefers_the_matching_entity():
    """`customer.contact.email` must beat a bare supplier-side interpretation."""
    schema = make_schema(
        "mongo_sys", SourceType.MONGODB, SchemaOrigin.INFERRED,
        entities=(
            make_entity(
                "invoices",
                (make_field("email", T.STRING, path=("customer", "contact")),),
                kind=EntityKind.COLLECTION,
            ),
        ),
    )

    decision = generate_mapping(schema).decision_for("customer.contact.email")

    assert decision.selected.qualified_target == "customer.email"
    assert decision.selected.evidence.path.score > 0


def test_path_context_is_recorded_as_evidence():
    schema = make_schema(
        "mongo_sys", SourceType.MONGODB, SchemaOrigin.INFERRED,
        entities=(
            make_entity(
                "invoices",
                (make_field("id", T.INTEGER, path=("customer",)),),
                kind=EntityKind.COLLECTION,
            ),
        ),
    )

    decision = generate_mapping(schema).decision_for("customer.id")
    evidence = decision.candidates[0].evidence.path

    assert evidence.source_context == "customer.id"
    assert evidence.shared_tokens


def test_an_uninformative_entity_name_does_not_veto_matching():
    """A CSV called `export_2026_q1` says nothing; its fields must still map."""
    schema = make_schema(
        "csv_sys", SourceType.CSV, SchemaOrigin.INFERRED,
        entities=(
            make_entity(
                "export_2026_q1",
                (make_field("invoice_id", T.STRING),),
                kind=EntityKind.DATASET,
            ),
        ),
    )

    decision = generate_mapping(schema).decision_for("invoice_id")

    assert decision.candidates
    assert decision.candidates[0].target_field == "invoice_id"


# ============================================================
# Type conflicts (Step 12)
# ============================================================

def test_a_strong_name_with_an_impossible_type_is_not_auto_selected():
    """The exact case Step 12 describes: name evidence is strong, the type
    makes the mapping impossible, so a human decides."""
    result = generate_mapping(schema_with(make_field("customer_id", T.OBJECT)))
    decision = result.decision_for("customer_id")

    assert decision.outcome is FieldOutcome.REVIEW_REQUIRED
    assert decision.selected is None
    assert decision.candidates[0].has_type_conflict is True
    assert "incompatible" in decision.reason


def test_the_evidence_records_both_the_strong_name_and_the_bad_type():
    result = generate_mapping(schema_with(make_field("customer_id", T.OBJECT)))
    evidence = result.decision_for("customer_id").candidates[0].evidence

    assert evidence.name.kind is NameMatchKind.EXACT
    assert evidence.has_type_conflict is True


def test_an_array_source_against_a_scalar_target_is_not_auto_selected():
    result = generate_mapping(
        schema_with(make_field("email", T.STRING, is_array=True),
                    entity_name="fin_customer")
    )
    decision = result.decision_for("email")

    assert decision.outcome is FieldOutcome.REVIEW_REQUIRED
    assert decision.candidates[0].evidence.type_comparison.cardinality_conflict


def test_a_widening_type_is_still_auto_selected():
    """`INTEGER -> DECIMAL` is lossless and should not block anything."""
    result = generate_mapping(schema_with(make_field("total_amount", T.INTEGER)))
    decision = result.decision_for("total_amount")

    assert decision.outcome is FieldOutcome.AUTO_SELECTED
    assert decision.selected.target_field == "amount"


# ============================================================
# Confidence and auto-selection (Steps 18, 19)
# ============================================================

def test_confidence_bands_follow_the_configured_thresholds():
    options = MappingOptions(high_threshold=0.9, medium_threshold=0.6)

    assert options.confidence_level(0.95) is ConfidenceLevel.HIGH
    assert options.confidence_level(0.7) is ConfidenceLevel.MEDIUM
    assert options.confidence_level(0.3) is ConfidenceLevel.LOW


def test_raising_the_threshold_makes_the_engine_more_conservative():
    schema = schema_with(make_field("cust_no", T.STRING))

    lenient = generate_mapping(schema, options=MappingOptions(high_threshold=0.7))
    strict = generate_mapping(schema, options=MappingOptions(high_threshold=0.99))

    assert lenient.decision_for("cust_no").outcome is FieldOutcome.AUTO_SELECTED
    assert strict.decision_for("cust_no").outcome is FieldOutcome.REVIEW_REQUIRED


def test_thresholds_are_not_magic_constants_in_the_code():
    """They arrive through options, so a research run can tune them."""
    options = MappingOptions(
        high_threshold=0.5, medium_threshold=0.4, ambiguity_margin=0.0
    )
    result = generate_mapping(
        schema_with(make_field("some_status_code", T.STRING)), options=options
    )

    assert result.decisions[0].outcome in (
        FieldOutcome.AUTO_SELECTED, FieldOutcome.AMBIGUOUS,
    )


def test_contradictory_thresholds_are_refused():
    with pytest.raises(MappingConfigurationError):
        MappingOptions(high_threshold=0.4, medium_threshold=0.9)


def test_weights_that_do_not_sum_to_one_are_refused():
    with pytest.raises(MappingConfigurationError, match="sum to 1.0"):
        ScoringWeights(name=0.9, type=0.9, entity=0.1, path=0.1)


# ============================================================
# Ambiguity (Step 20)
# ============================================================

def test_two_close_candidates_are_never_silently_resolved():
    """`total` fits invoice.amount and purchase_order.amount equally well."""
    schema = make_schema(
        "csv_sys", SourceType.CSV, SchemaOrigin.INFERRED,
        entities=(
            make_entity("export_2026_q1", (make_field("total", T.DECIMAL),),
                        kind=EntityKind.DATASET),
        ),
    )

    decision = generate_mapping(schema).decision_for("total")

    assert decision.outcome is FieldOutcome.AMBIGUOUS
    assert decision.selected is None
    assert decision.ambiguity is not None
    assert decision.ambiguity.margin < decision.ambiguity.required_margin


def test_an_ambiguity_names_both_contenders():
    schema = make_schema(
        "csv_sys", SourceType.CSV, SchemaOrigin.INFERRED,
        entities=(
            make_entity("export_2026_q1", (make_field("total", T.DECIMAL),),
                        kind=EntityKind.DATASET),
        ),
    )

    ambiguity = generate_mapping(schema).decision_for("total").ambiguity

    assert {ambiguity.best_target, ambiguity.runner_up_target} == {
        "invoice.amount", "purchase_order.amount",
    }
    assert "below the required margin" in ambiguity.explain()


def test_the_ambiguity_margin_is_configurable():
    schema = make_schema(
        "csv_sys", SourceType.CSV, SchemaOrigin.INFERRED,
        entities=(
            make_entity("export_2026_q1", (make_field("total", T.DECIMAL),),
                        kind=EntityKind.DATASET),
        ),
    )

    permissive = generate_mapping(
        schema, options=MappingOptions(ambiguity_margin=0.0)
    )

    assert permissive.decision_for("total").outcome is FieldOutcome.AUTO_SELECTED


def test_ambiguities_are_collected_on_the_result():
    schema = make_schema(
        "csv_sys", SourceType.CSV, SchemaOrigin.INFERRED,
        entities=(
            make_entity("export_2026_q1", (make_field("total", T.DECIMAL),),
                        kind=EntityKind.DATASET),
        ),
    )

    result = generate_mapping(schema)

    assert len(result.ambiguities) == 1


# ============================================================
# Unmapped fields (Step 21)
# ============================================================

def test_a_field_with_no_canonical_home_stays_unmapped():
    result = generate_mapping(
        schema_with(make_field("legacy_internal_flag_74", T.INTEGER))
    )

    assert result.decision_for("legacy_internal_flag_74").outcome is (
        FieldOutcome.UNMAPPED
    )


def test_an_unmapped_field_does_not_reach_the_profile():
    result = generate_mapping(
        schema_with(
            make_field("invoice_id", T.STRING),
            make_field("customer_id", T.STRING),
            make_field("amount", T.DECIMAL),
            make_field("legacy_internal_flag_74", T.INTEGER),
        )
    )
    profile = result.profiles[0]

    assert "legacy_internal_flag_74" not in [
        item.source_field for item in profile.field_mappings
    ]


def test_the_engine_does_not_force_full_coverage():
    result = generate_mapping(
        schema_with(
            make_field("invoice_id", T.STRING),
            make_field("zzz_unrelated_column", T.STRING),
        )
    )

    assert result.coverage.coverage_ratio < 1.0


# ============================================================
# Target collisions (Step 22)
# ============================================================

def test_two_fields_selected_for_one_target_are_flagged():
    result = generate_mapping(
        schema_with(
            make_field("cust_no", T.STRING),
            make_field("customer_number", T.STRING),
            entity_name="fin_customer",
        )
    )

    assert result.collisions
    collision = result.collisions[0]
    assert collision.target == "customer.customer_id"
    assert len(collision.source_fields) == 2


def test_only_the_strongest_survives_a_collision():
    result = generate_mapping(
        schema_with(
            make_field("cust_no", T.STRING),
            make_field("customer_number", T.STRING),
            entity_name="fin_customer",
        )
    )

    selected = [d for d in result.decisions if d.outcome is FieldOutcome.AUTO_SELECTED]
    demoted = [d for d in result.decisions
               if d.outcome is FieldOutcome.REVIEW_REQUIRED]

    assert len(selected) == 1
    assert len(demoted) == 1
    assert "also selected by" in demoted[0].reason


def test_collision_resolution_is_deterministic():
    schema = schema_with(
        make_field("cust_no", T.STRING),
        make_field("customer_number", T.STRING),
        entity_name="fin_customer",
    )

    first = generate_mapping(schema)
    second = generate_mapping(schema)

    assert first.collisions[0].kept_source_field == (
        second.collisions[0].kept_source_field
    )


def test_collision_detection_can_be_switched_off():
    result = generate_mapping(
        schema_with(
            make_field("cust_no", T.STRING),
            make_field("customer_number", T.STRING),
            entity_name="fin_customer",
        ),
        options=MappingOptions(detect_target_collisions=False),
    )

    assert result.collisions == ()


# ============================================================
# One target per source field (Step 23)
# ============================================================

def test_a_source_field_is_never_auto_mapped_to_two_targets():
    result = generate_mapping(
        schema_with(make_field("customer_id", T.STRING), entity_name="fin_invoice")
    )
    decision = result.decision_for("customer_id")

    assert decision.selected is not None
    mapped_targets = [
        item.target_field
        for profile in result.profiles
        for item in profile.field_mappings
        if item.source_field == "customer_id"
    ]
    assert len(mapped_targets) == 1


# ============================================================
# Manual override (Step 32)
# ============================================================

def test_a_manual_override_wins_over_the_engines_suggestion():
    schema = schema_with(make_field("cust_code", T.STRING), entity_name="fin_customer")

    result = generate_mapping(
        schema,
        overrides=(
            MappingOverride(
                source_field="cust_code", target="customer.name",
                reason="local convention", decided_by="analyst",
            ),
        ),
    )
    decision = result.decision_for("cust_code")

    assert decision.outcome is FieldOutcome.MANUAL_OVERRIDE
    assert decision.selected.qualified_target == "customer.name"


def test_an_override_is_distinguishable_from_an_automatic_choice():
    schema = schema_with(make_field("cust_code", T.STRING), entity_name="fin_customer")

    result = generate_mapping(
        schema,
        overrides=(
            MappingOverride(source_field="cust_code", target="customer.name"),
        ),
    )
    field_mapping = result.profiles[0].field_mappings[0]

    assert field_mapping.status is MappingStatus.APPROVED
    assert field_mapping.metadata["selection"] == "manual_override"


def test_an_override_can_force_a_field_to_stay_unmapped():
    result = generate_mapping(
        schema_with(make_field("invoice_id", T.STRING)),
        overrides=(
            MappingOverride(
                source_field="invoice_id", target=None,
                reason="this column is a local sequence, not the business key",
            ),
        ),
    )
    decision = result.decision_for("invoice_id")

    assert decision.outcome is FieldOutcome.UNMAPPED
    assert "manually left unmapped" in decision.reason


def test_an_override_naming_an_unknown_target_is_refused():
    with pytest.raises(CanonicalTargetNotFoundError):
        generate_mapping(
            schema_with(make_field("invoice_id", T.STRING)),
            overrides=(
                MappingOverride(source_field="invoice_id", target="nope.nothing"),
            ),
        )


def test_an_override_naming_an_unknown_source_field_is_refused():
    with pytest.raises(SourceFieldNotFoundError):
        generate_mapping(
            schema_with(make_field("invoice_id", T.STRING)),
            overrides=(
                MappingOverride(source_field="does_not_exist",
                                target="invoice.amount"),
            ),
        )


def test_an_override_with_an_impossible_type_is_refused():
    """A human decision is trusted over the engine's, but not over reality."""
    with pytest.raises(InvalidMappingOverrideError, match="cannot convert|conflicts"):
        generate_mapping(
            schema_with(make_field("payload", T.OBJECT)),
            overrides=(
                MappingOverride(source_field="payload", target="invoice.amount"),
            ),
        )


def test_an_override_keeps_the_engines_suggestions_visible():
    """So a reviewer can see what they overrode."""
    result = generate_mapping(
        schema_with(make_field("invoice_no", T.STRING)),
        overrides=(
            MappingOverride(source_field="invoice_no", target="invoice.status"),
        ),
    )
    decision = result.decision_for("invoice_no")

    assert decision.candidates[0].qualified_target == "invoice.status"
    assert any(c.qualified_target == "invoice.invoice_id" for c in decision.candidates)


def test_an_override_never_modifies_the_source_schema():
    schema = schema_with(make_field("invoice_no", T.STRING))
    before = schema.to_json_dict()

    generate_mapping(
        schema,
        overrides=(
            MappingOverride(source_field="invoice_no", target="invoice.status"),
        ),
    )

    assert schema.to_json_dict() == before


# ============================================================
# Rejected candidates (Step 33)
# ============================================================

def test_a_rejected_candidate_is_not_offered_again():
    schema = schema_with(make_field("cust_no", T.STRING), entity_name="fin_customer")

    before = generate_mapping(schema)
    after = generate_mapping(
        schema,
        rejected=(
            RejectedCandidate(
                source_field="cust_no", target="customer.customer_id",
                reason="reviewer says this is a local code",
            ),
        ),
    )

    assert before.decision_for("cust_no").selected.qualified_target == (
        "customer.customer_id"
    )
    assert all(
        candidate.qualified_target != "customer.customer_id"
        for candidate in after.decision_for("cust_no").candidates
    )


def test_rejection_is_scoped_to_its_source_field():
    schema = schema_with(
        make_field("cust_no", T.STRING),
        make_field("customer_id", T.STRING),
        entity_name="fin_customer",
    )

    result = generate_mapping(
        schema,
        rejected=(
            RejectedCandidate(source_field="cust_no",
                              target="customer.customer_id"),
        ),
    )

    assert result.decision_for("customer_id").selected.qualified_target == (
        "customer.customer_id"
    )


# ============================================================
# Entity matching
# ============================================================

def test_an_unmatched_entity_is_reported_rather_than_guessed():
    schema = make_schema(
        "csv_sys", SourceType.CSV, SchemaOrigin.INFERRED,
        entities=(
            make_entity("export_2026_q1", (make_field("invoice_id", T.STRING),),
                        kind=EntityKind.DATASET),
        ),
    )

    result = generate_mapping(schema)

    assert "export_2026_q1" in result.unmatched_entities


def test_an_entity_alias_matches_a_vendor_table_name():
    engine = MappingEngine()
    entity = make_entity("tbl_customer", (make_field("x", T.STRING),))

    matched, score = engine.match_entity(entity)

    assert matched == "customer"
    assert score >= 0.9


def test_max_candidates_is_respected():
    result = generate_mapping(
        schema_with(make_field("customer_id", T.STRING)),
        options=MappingOptions(max_candidates_per_field=2),
    )

    assert len(result.decision_for("customer_id").candidates) <= 2
