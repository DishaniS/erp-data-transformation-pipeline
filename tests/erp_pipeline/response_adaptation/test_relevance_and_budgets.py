"""Query-aware field selection and output budgets (Phase 14).

The relevance scorer is the phase's new mechanism, so these tests pin the two
properties that make it defensible as research: it is DETERMINISTIC, and every
decision is EXPLAINABLE from signals the test can read.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from erp_pipeline.response_adaptation import formatter as fmt
from erp_pipeline.response_adaptation.formatter import (
    TRUNCATION_MARKER,
    apply_budget_to_decisions,
    build_payload,
    limit_decisions,
)
from erp_pipeline.response_adaptation.models import (
    AdaptationOptions,
    AdaptationPolicy,
    RelevanceWeights,
)
from erp_pipeline.response_adaptation.relevance import (
    REASON_BLOCKED_FIELD,
    REASON_FIELD_BUDGET,
    REASON_MANDATORY,
    REASON_NO_QUERY,
    REASON_NO_SIGNAL,
    REASON_SELECTED,
    RelevanceScorer,
    intent_expansions,
    query_tokens,
    removal_summary,
)
from erp_pipeline.schemas.enums import SensitivityLevel

INVOICE_FIELDS = [
    ("inv_no", "invoice.invoice_id"),
    ("cust_ref", "invoice.customer_id"),
    ("total_amt", "invoice.amount"),
    ("curr", "invoice.currency"),
    ("approval_status", "invoice.status"),
    ("issue_dt", "invoice.issued_on"),
    ("row_version", None),
    ("etl_batch_id", None),
    ("created_by", None),
]

CANONICAL = {
    "invoice_id": "INV-204",
    "customer_id": "CUS-17",
    "amount": "45000.00",
    "currency": "LKR",
    "status": "A",
    "issued_on": "2026-01-05",
}

SOURCE = {
    "inv_no": "INV-204",
    "cust_ref": "CUS-17",
    "total_amt": "45000.00",
    "curr": "LKR",
    "approval_status": "A",
    "issue_dt": "2026-01-05",
    "row_version": 7,
    "etl_batch_id": "B-99",
    "created_by": "svc_acct",
}


@pytest.fixture
def scorer() -> RelevanceScorer:
    return RelevanceScorer()


@pytest.fixture
def options() -> AdaptationOptions:
    return AdaptationOptions()


def selected(decisions):
    return [item.source_field for item in decisions if item.selected]


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------


def test_a_field_the_query_names_outscores_one_it_does_not(scorer, options):
    decisions = {
        item.source_field: item
        for item in scorer.rank(
            "what currency is this invoice in?",
            INVOICE_FIELDS,
            "invoice",
            options.minimum_relevance_score,
        )
    }

    assert decisions["curr"].score > decisions["created_by"].score
    assert decisions["curr"].selected
    assert not decisions["created_by"].selected


def test_operational_plumbing_fields_are_removed(scorer, options):
    decisions = scorer.rank(
        "how much is this invoice for?",
        INVOICE_FIELDS,
        "invoice",
        options.minimum_relevance_score,
    )

    for noise in ("row_version", "etl_batch_id", "created_by"):
        assert noise not in selected(decisions)


def test_the_canonical_vocabulary_alone_can_select_a_field(scorer, options):
    """The ERP-awareness claim, isolated.

    ``cust_ref`` is reachable from "customer" by TWO independent routes: the
    pipeline's abbreviation table expands ``cust`` to ``customer``, and the
    canonical model lists ``customer_ref`` as an alias of ``customer_id``. A
    test asserting the literal name does not match would therefore be wrong -
    it does, and legitimately.

    So the claim is tested by removing the other route instead: with the name
    signal weighted to zero, the canonical vocabulary on its own still has to
    find the field.
    """
    alias_only = RelevanceScorer(
        RelevanceWeights(alias=1.0, name=0.0, entity=0.0, identity=0.0)
    )
    decisions = {
        item.source_field: item
        for item in alias_only.rank(
            "who is the customer?", INVOICE_FIELDS, "invoice",
            options.minimum_relevance_score,
        )
    }

    assert decisions["cust_ref"].selected
    assert decisions["cust_ref"].signals["alias"] > 0.0
    # A field with no canonical target is invisible to the alias signal, which
    # is what makes the previous assertion evidence rather than coincidence.
    assert decisions["row_version"].signals["alias"] == 0.0
    assert not decisions["row_version"].selected


def test_a_question_phrasing_reaches_a_field_it_never_names(scorer, options):
    """"How much" contains no form of the word "amount"."""
    assert "amount" in query_tokens("how much is this invoice for?")

    decisions = {
        item.source_field: item
        for item in scorer.rank(
            "how much is this invoice for?", INVOICE_FIELDS, "invoice",
            options.minimum_relevance_score,
        )
    }

    assert decisions["total_amt"].selected


def test_intent_expansion_is_reported_separately_from_literal_tokens():
    assert intent_expansions(("how", "much")) == ("amount", "total", "price")
    assert intent_expansions(("how", "big")) == ()


def test_the_entity_noun_does_not_lift_every_field_at_once(scorer, options):
    """"Invoice" appears inside ``invoice_amount``, ``invoice_date`` and
    ``invoice_status`` as aliases. If the word counted lexically, asking about
    one would select all of them and the signal would stop discriminating."""
    decisions = {
        item.source_field: item
        for item in scorer.rank(
            "what currency is this invoice in?", INVOICE_FIELDS, "invoice",
            options.minimum_relevance_score,
        )
    }

    assert decisions["curr"].selected
    assert not decisions["issue_dt"].selected
    assert not decisions["approval_status"].selected


def test_scoring_is_deterministic_across_instances(options):
    first = RelevanceScorer().rank(
        "who is the customer", INVOICE_FIELDS, "invoice",
        options.minimum_relevance_score,
    )
    second = RelevanceScorer().rank(
        "who is the customer", INVOICE_FIELDS, "invoice",
        options.minimum_relevance_score,
    )

    assert [(d.source_field, d.score, d.reason) for d in first] == [
        (d.source_field, d.score, d.reason) for d in second
    ]


def test_every_decision_carries_the_four_signals(scorer, options):
    for decision in scorer.rank("anything", INVOICE_FIELDS, "invoice",
                                options.minimum_relevance_score):
        assert set(decision.signals) == {"alias", "name", "entity", "identity"}
        assert all(0.0 <= value <= 1.0 for value in decision.signals.values())
        assert decision.reason


# ----------------------------------------------------------------------
# Mandatory fields and fallbacks
# ----------------------------------------------------------------------


def test_the_identity_field_survives_a_query_that_ignores_it(scorer, options):
    """An answer nobody can trace back to a record is not an answer."""
    decisions = {
        item.source_field: item
        for item in scorer.rank(
            "what currency?", INVOICE_FIELDS, "invoice",
            options.minimum_relevance_score,
        )
    }

    assert decisions["inv_no"].selected
    assert decisions["inv_no"].mandatory
    assert decisions["inv_no"].reason == REASON_MANDATORY


def test_the_identity_field_survives_a_budget_of_one(scorer, options):
    decisions = scorer.rank(
        "currency", INVOICE_FIELDS, "invoice",
        options.minimum_relevance_score, max_fields=1,
    )

    assert selected(decisions) == ["inv_no"]
    assert removal_summary(decisions)[REASON_FIELD_BUDGET] > 0


def test_no_query_means_nothing_is_dropped_for_irrelevance(scorer, options):
    decisions = scorer.rank(None, INVOICE_FIELDS, "invoice",
                            options.minimum_relevance_score)

    assert len(selected(decisions)) == len(INVOICE_FIELDS)
    assert {item.reason for item in decisions} == {REASON_NO_QUERY, REASON_MANDATORY}


def test_a_query_matching_nothing_abstains_instead_of_returning_only_an_id(
    scorer, options
):
    """The conservative failure: cost context, never recall.

    Returning the identity field alone would be a confidently wrong answer, and
    the caller could not tell it apart from a genuinely empty record.
    """
    decisions = scorer.rank(
        "what is the weather", INVOICE_FIELDS, "invoice",
        options.minimum_relevance_score,
    )

    assert len(selected(decisions)) == len(INVOICE_FIELDS)
    assert any(item.reason == REASON_NO_SIGNAL for item in decisions)


def test_disabling_selection_keeps_every_field(scorer, options):
    decisions = scorer.rank(
        "currency", INVOICE_FIELDS, "invoice",
        options.minimum_relevance_score, enabled=False,
    )

    assert len(selected(decisions)) == len(INVOICE_FIELDS)


def test_a_blocked_field_is_refused_even_when_it_is_mandatory(scorer, options):
    decisions = {
        item.source_field: item
        for item in scorer.rank(
            "invoice id", INVOICE_FIELDS, "invoice",
            options.minimum_relevance_score, blocked_fields={"inv_no"},
        )
    }

    assert not decisions["inv_no"].selected
    assert decisions["inv_no"].reason == REASON_BLOCKED_FIELD


def test_custom_weights_change_the_outcome(scorer, options):
    """The weights are real configuration, not decoration."""
    alias_only = RelevanceScorer(
        RelevanceWeights(alias=1.0, name=0.0, entity=0.0, identity=0.0)
    )
    decisions = {
        item.source_field: item
        for item in alias_only.rank(
            "row version", INVOICE_FIELDS, "invoice", 0.25
        )
    }

    # ``row_version`` has no canonical target, so an alias-only scorer cannot
    # see it at all, however literally the query names it.
    assert decisions["row_version"].score == 0.0


# ----------------------------------------------------------------------
# Budgets
# ----------------------------------------------------------------------


def test_selected_fields_are_emitted_under_canonical_names(options):
    decisions = RelevanceScorer().rank(
        "how much and what currency", INVOICE_FIELDS, "invoice",
        options.minimum_relevance_score,
    )
    result = build_payload(decisions, CANONICAL, SOURCE, options)

    assert "amount" in result.payload
    assert "total_amt" not in result.payload


def test_an_unmapped_field_keeps_its_source_name(options):
    decisions = RelevanceScorer().rank(
        None, INVOICE_FIELDS, "invoice", options.minimum_relevance_score
    )
    result = build_payload(decisions, CANONICAL, SOURCE, options)

    assert result.payload["etl_batch_id"] == "B-99"


def test_an_oversized_value_is_clipped_with_a_visible_marker(options):
    long_source = dict(SOURCE, created_by="x" * 500)
    decisions = RelevanceScorer().rank(
        None, INVOICE_FIELDS, "invoice", options.minimum_relevance_score
    )
    result = build_payload(
        decisions, CANONICAL, long_source, replace(options, max_value_characters=50)
    )

    assert result.payload["created_by"].endswith(TRUNCATION_MARKER)
    assert "created_by" in result.clipped_fields
    assert result.truncated


def test_a_number_is_never_clipped(options):
    """Truncating ``45000.00`` to ``450`` is not a shorter amount, it is a
    wrong one."""
    decisions = RelevanceScorer().rank(
        None, [("row_version", None)], "invoice", options.minimum_relevance_score
    )
    result = build_payload(
        decisions, {}, {"row_version": 1234567}, replace(options, max_value_characters=10)
    )

    assert result.payload["row_version"] == 1234567


def test_the_character_budget_removes_fields_and_says_so(options):
    decisions = RelevanceScorer().rank(
        None, INVOICE_FIELDS, "invoice", options.minimum_relevance_score
    )
    result = build_payload(
        decisions, CANONICAL, SOURCE, replace(options, max_output_characters=90)
    )

    assert result.truncated
    assert result.dropped_fields
    assert len(str(result.payload)) < len(str(SOURCE))


def test_budget_removals_are_written_back_into_the_decisions(options):
    """A report claiming a field was selected while the payload lacks it would
    be a report of an output that does not exist."""
    decisions = RelevanceScorer().rank(
        None, INVOICE_FIELDS, "invoice", options.minimum_relevance_score
    )
    result = build_payload(
        decisions, CANONICAL, SOURCE, replace(options, max_output_characters=90)
    )
    rewritten = apply_budget_to_decisions(decisions, result)

    assert len(selected(rewritten)) == len(result.payload)
    assert fmt.REASON_CHARACTER_BUDGET in {
        item.reason for item in rewritten if not item.selected
    }


def test_the_identity_field_is_the_last_to_be_cut_by_the_budget(options):
    decisions = RelevanceScorer().rank(
        None, INVOICE_FIELDS, "invoice", options.minimum_relevance_score
    )
    result = build_payload(
        decisions, CANONICAL, SOURCE, replace(options, max_output_characters=40)
    )

    assert "invoice_id" in result.payload


def test_a_blocked_sensitivity_withholds_the_payload_but_names_the_fields(options):
    """Withholding is a different fact from absence, and the caller must be
    able to tell them apart."""
    blocked = replace(
        options,
        policy=AdaptationPolicy(
            blocked_sensitivities=frozenset({SensitivityLevel.RESTRICTED})
        ),
    )
    decisions = RelevanceScorer().rank(
        None, INVOICE_FIELDS, "invoice", options.minimum_relevance_score
    )
    result = build_payload(
        decisions, CANONICAL, SOURCE, blocked, SensitivityLevel.RESTRICTED
    )

    assert result.payload == {}
    assert result.withheld_fields
    assert result.truncated


def test_an_allowed_sensitivity_passes_through(options):
    blocked = replace(
        options,
        policy=AdaptationPolicy(
            blocked_sensitivities=frozenset({SensitivityLevel.RESTRICTED})
        ),
    )
    decisions = RelevanceScorer().rank(
        None, INVOICE_FIELDS, "invoice", options.minimum_relevance_score
    )
    result = build_payload(
        decisions, CANONICAL, SOURCE, blocked, SensitivityLevel.INTERNAL
    )

    assert result.payload


def test_a_decimal_is_serialized_as_an_exact_string(options):
    from decimal import Decimal

    decisions = RelevanceScorer().rank(
        None, [("total_amt", "invoice.amount")], "invoice",
        options.minimum_relevance_score,
    )
    result = build_payload(
        decisions, {"amount": Decimal("45000.10")}, SOURCE, options
    )

    assert result.payload["amount"] == "45000.10"


def test_the_report_is_capped_and_prefers_keeping_rejections(options):
    """An explanation must not grow into a second copy of the payload, and
    "why was this dropped" is the half that cannot be read off the output."""
    decisions = RelevanceScorer().rank(
        "currency", INVOICE_FIELDS, "invoice", options.minimum_relevance_score
    )
    kept, truncated = limit_decisions(decisions, 3)

    assert truncated
    assert len(kept) == 3
    assert sum(1 for item in kept if not item.selected) >= 2
