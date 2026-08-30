"""A record's declared sensitivity must reach the router and constrain it.

WHY THIS IS THE RESEARCH-CRITICAL TEST
--------------------------------------
The component's stated novelty is *cost-efficient SECURE hybrid tiered vector
storage*, and its security argument is that hard constraints are applied BEFORE
scoring, so a cost advantage can never outvote a compliance rule.

That argument was unreachable in the runtime. Orchestration called
``storage.store(record)`` with no profile, so every record routed as
``INTERNAL`` regardless of what its canonical record declared, and
``prohibited_tiers()`` returned empty every single time. The mechanism was
correct and never exercised.

These tests prove the value now travels, and - crucially - that the constraint
wins against a score, examined through the ``RoutingDecision`` evidence rather
than by looking only at which tier came out.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ai.embedding import DeterministicTestModel
from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.schemas.canonical_models import CanonicalRecord, SourceReference
from erp_pipeline.schemas.enums import SensitivityLevel, SourceType
from erp_pipeline.storage.errors import PolicyViolationError
from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import TierSet
from erp_pipeline.storage.models import (
    StorageLocation,
    StorageRoutingContext,
    StorageTier,
)
from erp_pipeline.storage.service import DEFAULT_PROFILE, StorageProfile, StorageService
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.storage.storage_policy import StoragePolicy, TierWeights
from erp_pipeline.storage.vector_router import StoragePolicyRouter


# ============================================================
# A topology where an EXTERNAL tier would otherwise win
# ============================================================

#: HOT on-premises, WARM and COLD in a cloud region. This is the topology the
#: default policy does NOT have - it places all three on-premises, which means
#: the on-premises constraint currently prohibits nothing and the invariant is
#: never actually tested. Here it has something to prohibit.
HYBRID_LOCATIONS = {
    StorageTier.HOT: StorageLocation.ON_PREMISES,
    StorageTier.WARM: StorageLocation.EXTERNAL,
    StorageTier.COLD: StorageLocation.EXTERNAL,
}


@pytest.fixture
def hybrid_policy() -> StoragePolicy:
    """A policy where the EXTERNAL tier scores best for a fresh record.

    WARM's weights are inflated so it beats HOT on raw score. If the
    constraint were applied as a penalty inside the scoring stage rather than
    as a prior removal, a large enough score would overcome it - which is
    precisely the failure mode the two-stage design exists to prevent.
    """
    return StoragePolicy(
        policy_id="test_hybrid",
        version="1.0",
        tier_locations=dict(HYBRID_LOCATIONS),
        hot_weights=TierWeights(recency=0.10),
        warm_weights=TierWeights(recency=0.90),
        cold_weights=TierWeights(recency=0.80),
        on_premises_only_sensitivities=frozenset({SensitivityLevel.RESTRICTED}),
    )


@pytest.fixture
def router(hybrid_policy: StoragePolicy) -> StoragePolicyRouter:
    return StoragePolicyRouter(hybrid_policy)


def context(sensitivity: SensitivityLevel, **overrides) -> StorageRoutingContext:
    payload = {
        "representation_id": f"ai:invoice:{sensitivity.value}",
        "sensitivity": sensitivity,
        "age_days": 0.0,
    }
    payload.update(overrides)

    return StorageRoutingContext(**payload)


# ============================================================
# The topology really does favour the external tier
# ============================================================


def test_an_ordinary_record_goes_to_the_highest_scoring_tier(router):
    """Establishes the baseline: without a constraint, WARM wins on score.

    Without this, a later assertion that RESTRICTED avoided WARM would prove
    nothing - WARM might simply never have been a candidate.
    """
    decision = router.route(context(SensitivityLevel.INTERNAL))

    assert decision.selected_tier is StorageTier.WARM
    assert decision.score_for(StorageTier.WARM) > decision.score_for(
        StorageTier.HOT
    )


# ============================================================
# The security invariant
# ============================================================


def test_a_restricted_record_never_reaches_an_external_tier(router):
    decision = router.route(context(SensitivityLevel.RESTRICTED))

    assert decision.selected_tier is StorageTier.HOT
    assert HYBRID_LOCATIONS[decision.selected_tier] is StorageLocation.ON_PREMISES


def test_the_external_tiers_are_prohibited_not_merely_outscored(router):
    """The evidence, not just the outcome.

    A tier that lost on points and a tier that was removed from the running
    are different things, and only the second is a guarantee.
    """
    decision = router.route(context(SensitivityLevel.RESTRICTED))

    assert set(decision.prohibited_tiers) == {StorageTier.WARM, StorageTier.COLD}


def test_the_prohibition_reason_names_sensitivity_and_location(router):
    decision = router.route(context(SensitivityLevel.RESTRICTED))

    warm = next(s for s in decision.scores if s.tier is StorageTier.WARM)

    assert warm.prohibited is True
    assert "restricted" in warm.prohibition_reason
    assert "external" in warm.prohibition_reason.lower()


def test_a_prohibited_tier_scores_zero_and_is_flagged(router):
    """Zeroing alone would be indistinguishable from 'scored badly'."""
    decision = router.route(context(SensitivityLevel.RESTRICTED))

    warm = next(s for s in decision.scores if s.tier is StorageTier.WARM)

    assert warm.total == 0.0
    assert warm.prohibited is True


def test_the_constraint_is_recorded_on_the_decision(router):
    decision = router.route(context(SensitivityLevel.RESTRICTED))

    assert any("warm" in applied for applied in decision.constraints_applied)


def test_the_constraint_holds_even_when_the_external_score_is_overwhelming():
    """The arithmetic path a penalty-based design would eventually lose."""
    policy = StoragePolicy(
        policy_id="extreme",
        tier_locations=dict(HYBRID_LOCATIONS),
        hot_weights=TierWeights(recency=0.01),
        warm_weights=TierWeights(recency=1.0, access=1.0, criticality=1.0),
        cold_weights=TierWeights(recency=1.0),
        on_premises_only_sensitivities=frozenset({SensitivityLevel.RESTRICTED}),
    )
    decision = StoragePolicyRouter(policy).route(
        context(SensitivityLevel.RESTRICTED, recent_access_count=100)
    )

    assert decision.selected_tier is StorageTier.HOT


def test_a_manual_override_to_a_prohibited_tier_is_refused(router):
    """An override beats a SCORE. It must not beat a CONSTRAINT."""
    with pytest.raises(PolicyViolationError) as error:
        router.route(
            context(SensitivityLevel.RESTRICTED), override=StorageTier.WARM
        )

    assert "restricted" in str(error.value)


@pytest.mark.parametrize(
    "level",
    [SensitivityLevel.PUBLIC, SensitivityLevel.INTERNAL,
     SensitivityLevel.CONFIDENTIAL],
)
def test_unrestricted_levels_still_use_ordinary_scoring(router, level):
    """The constraint must not quietly become a blanket rule."""
    decision = router.route(context(level))

    assert decision.selected_tier is StorageTier.WARM
    assert decision.prohibited_tiers == ()


def test_a_policy_can_restrict_confidential_too(router):
    """The policy field exists so a deployment widens the rule without a code
    change."""
    policy = StoragePolicy(
        tier_locations=dict(HYBRID_LOCATIONS),
        hot_weights=TierWeights(recency=0.10),
        warm_weights=TierWeights(recency=0.90),
        on_premises_only_sensitivities=frozenset(
            {SensitivityLevel.RESTRICTED, SensitivityLevel.CONFIDENTIAL}
        ),
    )
    decision = StoragePolicyRouter(policy).route(
        context(SensitivityLevel.CONFIDENTIAL)
    )

    assert decision.selected_tier is StorageTier.HOT


# ============================================================
# Propagation: canonical record -> profile
# ============================================================


def canonical_record(sensitivity: SensitivityLevel):
    return CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id="finance_erp",
            source_type=SourceType.POSTGRESQL,
            source_entity="fin_invoice",
            source_record_key="INV-9",
        ),
        entity_type="invoice",
        stable_source_key="INV-9",
        normalized_data={"invoice_id": "INV-9"},
        sensitivity=sensitivity,
    )


@pytest.mark.parametrize("level", list(SensitivityLevel))
def test_sensitivity_survives_representation_and_embedding(level):
    representation = canonical_record_to_representation(canonical_record(level))
    embedding = EmbeddingService(
        DeterministicTestModel(dimension=4)
    ).embed_one(representation)

    assert representation.metadata["sensitivity"] == level.value
    assert embedding.metadata["sensitivity"] == level.value


@pytest.mark.parametrize("level", list(SensitivityLevel))
def test_a_profile_is_derived_from_the_records_own_metadata(level):
    representation = canonical_record_to_representation(canonical_record(level))
    embedding = EmbeddingService(
        DeterministicTestModel(dimension=4)
    ).embed_one(representation)

    assert StorageProfile.from_metadata(embedding.metadata).sensitivity is level


def test_metadata_without_a_sensitivity_falls_back_to_the_base_profile():
    assert StorageProfile.from_metadata({}).sensitivity is (
        DEFAULT_PROFILE.sensitivity
    )


def test_an_unrecognized_sensitivity_is_refused_not_downgraded():
    """Silently treating an unknown label as INTERNAL is exactly how
    restricted data ends up in the wrong tier."""
    with pytest.raises(ValueError):
        StorageProfile.from_metadata({"sensitivity": "ultra-secret"})


def test_the_base_profile_supplies_what_metadata_does_not_declare():
    from erp_pipeline.storage.models import BusinessCriticality

    base = StorageProfile(business_criticality=BusinessCriticality.CRITICAL)
    derived = StorageProfile.from_metadata({"sensitivity": "restricted"}, base)

    assert derived.sensitivity is SensitivityLevel.RESTRICTED
    assert derived.business_criticality is BusinessCriticality.CRITICAL


# ============================================================
# Propagation: profile -> stored routing decision
# ============================================================


class SimpleTier:
    #: HOT and WARM scores get merged, so the store refuses to build unless
    #: both tiers agree on dimension. A stand-in tier has to declare one.
    dimension = 4

    def __init__(self):
        self.written = {}

    def upsert(self, record, payload=None):
        self.written[record.representation_id] = record
        return True

    def get_vector(self, representation_id):
        record = self.written.get(representation_id)
        return record.vector if record else None

    def exists(self, representation_id):
        return representation_id in self.written

    def delete(self, representation_id):
        return self.written.pop(representation_id, None) is not None

    def search(self, vector, limit=5, query_filter=None):
        return []

    def count(self):
        return len(self.written)


def embed(level: SensitivityLevel):
    representation = canonical_record_to_representation(canonical_record(level))

    return EmbeddingService(DeterministicTestModel(dimension=4)).embed_one(
        representation
    )


def test_the_stored_state_records_the_records_real_sensitivity(hybrid_policy):
    service = StorageService(
        hot=SimpleTier(),
        warm=SimpleTier(),
        state_store=InMemoryTierStateStore(),
        policy=hybrid_policy,
    )

    metadata, _decision = service.store(embed(SensitivityLevel.RESTRICTED))

    assert metadata.sensitivity is SensitivityLevel.RESTRICTED


def test_a_restricted_record_is_stored_on_premises_end_to_end(hybrid_policy):
    """The full chain: canonical record -> representation -> embedding ->
    profile -> router -> tier."""
    service = StorageService(
        hot=SimpleTier(),
        warm=SimpleTier(),
        state_store=InMemoryTierStateStore(),
        policy=hybrid_policy,
    )

    metadata, decision = service.store(embed(SensitivityLevel.RESTRICTED))

    assert metadata.current_tier is StorageTier.HOT
    assert StorageTier.WARM in decision.prohibited_tiers


def test_an_internal_record_still_follows_the_score(hybrid_policy):
    service = StorageService(
        hot=SimpleTier(),
        warm=SimpleTier(),
        state_store=InMemoryTierStateStore(),
        policy=hybrid_policy,
    )

    metadata, decision = service.store(embed(SensitivityLevel.INTERNAL))

    assert metadata.current_tier is StorageTier.WARM
    assert decision.prohibited_tiers == ()


def test_an_explicit_profile_still_wins(hybrid_policy):
    """A caller that supplies a profile has made an explicit choice."""
    service = StorageService(
        hot=SimpleTier(),
        warm=SimpleTier(),
        state_store=InMemoryTierStateStore(),
        policy=hybrid_policy,
    )

    metadata, _ = service.store(
        embed(SensitivityLevel.INTERNAL),
        profile=StorageProfile(sensitivity=SensitivityLevel.RESTRICTED),
    )

    assert metadata.current_tier is StorageTier.HOT


# ============================================================
# Orchestration passes metadata, never a tier
# ============================================================


def test_orchestration_derives_the_profile_from_the_record(hybrid_policy):
    from erp_pipeline.orchestration.service import PipelineServices

    services = PipelineServices(
        storage=StorageService(
            hot=SimpleTier(),
            warm=SimpleTier(),
            state_store=InMemoryTierStateStore(),
            policy=hybrid_policy,
        )
    )

    metadata, decision = services.store_vector(embed(SensitivityLevel.RESTRICTED))

    assert metadata.sensitivity is SensitivityLevel.RESTRICTED
    assert metadata.current_tier is StorageTier.HOT


def test_orchestration_never_names_a_tier():
    """The architecture rule: orchestration supplies metadata, storage decides."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[3]
        / "src"
        / "erp_pipeline"
        / "orchestration"
        / "service.py"
    ).read_text(encoding="utf-8")

    for forbidden in ("StorageTier.HOT", "StorageTier.WARM", "StorageTier.COLD"):
        assert forbidden not in source, forbidden
