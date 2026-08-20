"""Routing: hard constraints, scoring, hysteresis, overrides, explainability."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.errors import PolicyViolationError
from erp_pipeline.storage.models import (
    BusinessCriticality,
    LatencyRequirement,
    StorageLocation,
    StorageTier,
    TransitionReason,
)
from erp_pipeline.storage.storage_policy import (
    DEFAULT_POLICY,
    DEFAULT_TIER_LOCATIONS,
    StoragePolicy,
)
from erp_pipeline.storage.vector_router import StoragePolicyRouter

from .conftest import make_context, make_metadata

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture
def router() -> StoragePolicyRouter:
    return StoragePolicyRouter(DEFAULT_POLICY)


# ----------------------------------------------------------------------
# Hard constraints are applied BEFORE scoring - the core research claim
# ----------------------------------------------------------------------


def test_restricted_data_cannot_reach_an_external_tier(router: StoragePolicyRouter):
    """The invariant only means something when a tier is actually external."""
    policy = replace(
        DEFAULT_POLICY,
        tier_locations={
            StorageTier.HOT: StorageLocation.ON_PREMISES,
            StorageTier.WARM: StorageLocation.ON_PREMISES,
            StorageTier.COLD: StorageLocation.EXTERNAL,
        },
    )
    external_router = StoragePolicyRouter(policy)

    # Ancient and untouched: every scoring signal screams COLD.
    context = make_context(
        age_days=3000.0,
        dormancy_days=3000.0,
        access_count=0,
        sensitivity=SensitivityLevel.RESTRICTED,
        now=NOW,
    )
    decision = external_router.route(context, now=NOW)

    assert decision.selected_tier is not StorageTier.COLD
    assert StorageTier.COLD in decision.prohibited_tiers


def test_prohibited_tier_is_removed_not_merely_penalised(router: StoragePolicyRouter):
    """A penalty could be outvoted by a big enough cost advantage; removal cannot.

    This is the difference between a preference and a guarantee. The SAME
    record is routed under two topologies. Only the tier's location differs, so
    any change in outcome is attributable to the constraint and nothing else.
    """
    context = make_context(
        age_days=5000.0,
        dormancy_days=5000.0,
        access_count=0,
        sensitivity=SensitivityLevel.RESTRICTED,
        now=NOW,
    )

    # Topology A - every tier on-premises. Nothing is prohibited, and the
    # scoring signals make COLD the outright winner.
    on_premises = StoragePolicyRouter(DEFAULT_POLICY)
    scores = {s.tier: s.total for s in on_premises.score_tiers(context)}

    assert scores[StorageTier.COLD] == max(scores.values())
    assert on_premises.route(context, now=NOW).selected_tier is StorageTier.COLD

    # Topology B - COLD is external. The winning tier is now zeroed out and
    # excluded outright, so no score it could have earned would bring it back.
    external = StoragePolicyRouter(
        replace(
            DEFAULT_POLICY,
            tier_locations={
                **DEFAULT_TIER_LOCATIONS,
                StorageTier.COLD: StorageLocation.EXTERNAL,
            },
        )
    )
    decision = external.route(context, now=NOW)

    assert StorageTier.COLD in external.prohibited_tiers(context)
    assert decision.selected_tier is not StorageTier.COLD
    assert {s.tier: s.total for s in external.score_tiers(context)}[
        StorageTier.COLD
    ] == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retention_until": NOW + timedelta(days=400)},
        {"legal_hold": True},
        {"latency_requirement": LatencyRequirement.LOW_LATENCY},
        {"business_criticality": BusinessCriticality.CRITICAL},
    ],
)
def test_each_cold_blocking_condition_blocks_cold(router: StoragePolicyRouter, kwargs):
    context = make_context(
        age_days=4000.0, dormancy_days=4000.0, access_count=0, now=NOW, **kwargs
    )
    decision = router.route(context, now=NOW)

    assert decision.selected_tier is not StorageTier.COLD
    assert StorageTier.COLD in decision.prohibited_tiers


def test_routing_cannot_return_a_tier_with_no_candidates_left():
    """If policy prohibits everything, that is an error, never a silent default."""
    policy = replace(
        DEFAULT_POLICY,
        tier_locations={
            StorageTier.HOT: StorageLocation.EXTERNAL,
            StorageTier.WARM: StorageLocation.EXTERNAL,
            StorageTier.COLD: StorageLocation.EXTERNAL,
        },
    )
    context = make_context(sensitivity=SensitivityLevel.RESTRICTED, now=NOW)

    with pytest.raises(PolicyViolationError):
        StoragePolicyRouter(policy).route(context, now=NOW)


# ----------------------------------------------------------------------
# Scoring behaviour
# ----------------------------------------------------------------------


def test_fresh_frequently_read_record_goes_hot(router: StoragePolicyRouter):
    decision = router.route(
        make_context(age_days=0.5, dormancy_days=0.1, access_count=40, now=NOW),
        now=NOW,
    )

    assert decision.selected_tier is StorageTier.HOT


def test_old_untouched_record_goes_cold(router: StoragePolicyRouter):
    decision = router.route(
        make_context(age_days=900.0, dormancy_days=900.0, access_count=0, now=NOW),
        now=NOW,
    )

    assert decision.selected_tier is StorageTier.COLD


def test_a_brand_new_record_is_never_archived(router: StoragePolicyRouter):
    """Dormancy is bounded by age: a record cannot be dormant longer than it existed.

    Without that bound a just-created record looks infinitely stale and is
    archived the moment it is written.
    """
    decision = router.route(
        make_context(age_days=0.0, dormancy_days=0.0, access_count=0, now=NOW),
        now=NOW,
    )

    assert decision.selected_tier is not StorageTier.COLD


def test_recently_read_old_record_is_not_archived(router: StoragePolicyRouter):
    """Age alone must not archive something someone is actively using."""
    decision = router.route(
        make_context(age_days=1200.0, dormancy_days=0.2, access_count=25, now=NOW),
        now=NOW,
    )

    assert decision.selected_tier is not StorageTier.COLD


# ----------------------------------------------------------------------
# Hysteresis - the anti-thrash mechanism
# ----------------------------------------------------------------------


def test_minimum_residence_prevents_immediate_demotion(router: StoragePolicyRouter):
    metadata = make_metadata(
        tier=StorageTier.HOT, age_days=900.0, dormancy_days=900.0, access_count=0, now=NOW
    )
    metadata = replace(metadata, tier_since=NOW - timedelta(days=1))

    decision = router.route(metadata.to_context(now=NOW), now=NOW)

    assert decision.selected_tier is StorageTier.HOT
    assert "residence" in decision.explain().lower()


def test_marginal_score_difference_does_not_trigger_a_move(router: StoragePolicyRouter):
    """A move must beat the incumbent by a margin, not by a rounding error."""
    context = make_context(
        tier=StorageTier.WARM, age_days=200.0, dormancy_days=40.0, access_count=6, now=NOW
    )
    decision = router.route(context, now=NOW)
    scores = {score.tier: score.total for score in decision.scores}
    incumbent = scores[StorageTier.WARM]
    winner = scores[decision.selected_tier]

    if decision.selected_tier is not StorageTier.WARM:
        assert winner - incumbent >= min(
            DEFAULT_POLICY.promotion_margin, DEFAULT_POLICY.demotion_margin
        )


# ----------------------------------------------------------------------
# Overrides and explainability
# ----------------------------------------------------------------------


def test_override_wins_and_is_recorded_as_forced(router: StoragePolicyRouter):
    decision = router.route(
        make_context(age_days=0.1, dormancy_days=0.1, access_count=99, now=NOW),
        override=StorageTier.COLD,
        override_reason="operator archived it manually",
        now=NOW,
    )

    assert decision.selected_tier is StorageTier.COLD
    assert decision.forced is True
    assert "operator archived it manually" in decision.explain()


def test_override_cannot_break_a_hard_constraint():
    """An operator may overrule the score. They may not overrule the law."""
    policy = replace(
        DEFAULT_POLICY,
        tier_locations={**DEFAULT_TIER_LOCATIONS, StorageTier.COLD: StorageLocation.EXTERNAL},
    )
    context = make_context(sensitivity=SensitivityLevel.RESTRICTED, now=NOW)

    with pytest.raises(PolicyViolationError):
        StoragePolicyRouter(policy).route(
            context, override=StorageTier.COLD, override_reason="ignore policy", now=NOW
        )


def test_every_decision_explains_itself(router: StoragePolicyRouter):
    decision = router.route(make_context(now=NOW), now=NOW)

    assert decision.explain()
    assert decision.reason_code is not None
    assert decision.scores

    for score in decision.scores:
        assert score.contributions
        # Weighted contributions must actually reconstruct the total, otherwise
        # the explanation is decoration rather than the reason.
        assert score.total == pytest.approx(
            sum(c.contribution for c in score.contributions), abs=1e-6
        )


def test_decision_serialises_without_leaking_objects(router: StoragePolicyRouter):
    payload = router.route(make_context(now=NOW), now=NOW).to_dict()

    assert isinstance(payload["selected_tier"], str)
    assert isinstance(payload["reason_code"], str)


def test_policy_weights_are_normalised():
    """Weights that do not sum to 1.0 make scores incomparable between tiers."""
    for weights in (
        DEFAULT_POLICY.hot_weights,
        DEFAULT_POLICY.warm_weights,
        DEFAULT_POLICY.cold_weights,
    ):
        assert sum(weights.as_mapping().values()) == pytest.approx(1.0, abs=1e-9)


def test_policy_is_frozen_and_versioned():
    assert DEFAULT_POLICY.policy_id
    assert DEFAULT_POLICY.version

    with pytest.raises(Exception):
        DEFAULT_POLICY.policy_id = "mutated"  # type: ignore[misc]
