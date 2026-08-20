"""The explainable storage policy router.

    StorageRoutingContext
            │
            ▼
    hard constraints        prohibit tiers outright — cannot be outscored
            │
            ▼
    six weighted factors    recency · access · criticality · latency · age · dormancy
            │
            ▼
    hysteresis              margins + minimum residence, to stop flapping
            │
            ▼
    RoutingDecision         tier + forced/scored + per-tier scores + evidence

THE TWO-STAGE DESIGN IS THE POINT
---------------------------------
Constraints are applied FIRST and remove tiers from the candidate set entirely.
Only what survives gets scored. That ordering is what makes the restricted-data
rule a guarantee: there is no arithmetic path by which a cold-storage cost
advantage can reach a prohibited tier, because the prohibited tier was never in
the running.

The alternative - subtracting a penalty from the cold score - fails the moment
someone tunes a weight, and fails silently.

EVERY DECISION EXPLAINS ITSELF
------------------------------
No code path returns a tier without also returning the per-tier scores, each
factor's contribution, the constraints applied and a human-readable reason.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.errors import PolicyViolationError
from erp_pipeline.storage.models import (
    BusinessCriticality,
    FactorContribution,
    LatencyRequirement,
    RoutingDecision,
    StorageLocation,
    StorageRoutingContext,
    StorageTier,
    TierScore,
    TransitionReason,
)
from erp_pipeline.storage.storage_policy import DEFAULT_POLICY, StoragePolicy


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


class StoragePolicyRouter:
    """Chooses a tier, and explains the choice completely."""

    def __init__(self, policy: StoragePolicy | None = None) -> None:
        self._policy = policy or DEFAULT_POLICY

    @property
    def policy(self) -> StoragePolicy:
        return self._policy

    # ------------------------------------------------------------
    # Factors (Step 10)
    # ------------------------------------------------------------

    def factors(self, context: StorageRoutingContext) -> dict[str, float]:
        """Normalize every routing input to [0, 1].

        Normalizing first is what makes the weights comparable and the
        explanation readable: a contribution is always ``weight × factor``, and
        both numbers mean something on their own.
        """
        policy = self._policy

        age = _clamp(context.age_days / policy.age_saturation_days)
        recency = 1.0 - age

        access = _clamp(
            context.recent_access_count / policy.access_saturation_count
        )

        # A never-read record is dormant for its whole life, not for ever: a
        # record created yesterday cannot have been ignored for four months.
        # Treating "never accessed" as maximal dormancy sent brand-new records
        # straight to the archive, which is exactly backwards.
        idle_days = (
            context.days_since_access
            if context.days_since_access is not None
            else context.age_days
        )
        dormancy = _clamp(idle_days / policy.dormancy_saturation_days)

        return {
            "recency": recency,
            "access": access,
            "criticality": context.business_criticality.weight,
            "latency": context.latency_requirement.weight,
            "age": age,
            "dormancy": dormancy,
        }

    # ------------------------------------------------------------
    # Hard constraints (Steps 6, 12)
    # ------------------------------------------------------------

    def prohibited_tiers(
        self, context: StorageRoutingContext
    ) -> dict[StorageTier, str]:
        """Tiers this record may NOT use, and why.

        These are compliance and physics, not preference. Nothing in the
        scoring stage can reinstate a tier that appears here.
        """
        policy = self._policy
        prohibited: dict[StorageTier, str] = {}

        # -- storage-location compliance --
        if policy.requires_on_premises(context.sensitivity):
            for tier in StorageTier:
                if policy.location_of(tier) is not StorageLocation.ON_PREMISES:
                    prohibited[tier] = (
                        f"sensitivity {context.sensitivity.value!r} is restricted "
                        f"to on-premises storage; tier {tier.value!r} is "
                        f"{policy.location_of(tier).value}"
                    )

        # -- availability / readability constraints on COLD --
        if policy.legal_hold_blocks_cold and context.legal_hold:
            prohibited[StorageTier.COLD] = (
                "the record is under legal hold and must remain directly "
                "readable"
            )
        elif policy.retention_blocks_cold and context.retention_active():
            prohibited[StorageTier.COLD] = (
                "an active retention requirement keeps this record out of the "
                "archive tier"
            )

        if (
            policy.low_latency_blocks_cold
            and context.latency_requirement is LatencyRequirement.LOW_LATENCY
        ):
            prohibited[StorageTier.COLD] = (
                "a low-latency requirement cannot be met by an archive tier "
                "that requires rehydration"
            )

        if (
            policy.critical_blocks_cold
            and context.business_criticality is BusinessCriticality.CRITICAL
        ):
            prohibited[StorageTier.COLD] = (
                "business-critical records are never archived, regardless of age"
            )

        return prohibited

    # ------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------

    def score_tiers(
        self, context: StorageRoutingContext
    ) -> tuple[TierScore, ...]:
        """Score every tier and record each factor's contribution."""
        values = self.factors(context)
        prohibited = self.prohibited_tiers(context)
        scores: list[TierScore] = []

        for tier in StorageTier:
            weights = self._policy.weights_for(tier).as_mapping()
            contributions: list[FactorContribution] = []
            total = 0.0

            for factor, weight in sorted(weights.items()):
                if weight == 0.0:
                    continue
                raw = values[factor]
                contribution = weight * raw
                total += contribution
                contributions.append(
                    FactorContribution(
                        factor=factor,
                        raw_value=raw,
                        weight=weight,
                        contribution=contribution,
                    )
                )

            reason = prohibited.get(tier)

            scores.append(
                TierScore(
                    tier=tier,
                    # A prohibited tier scores zero AND is flagged. Zeroing
                    # alone would be indistinguishable from "scored badly".
                    total=0.0 if reason else round(total, 6),
                    contributions=tuple(contributions),
                    prohibited=reason is not None,
                    prohibition_reason=reason,
                )
            )

        return tuple(scores)

    # ------------------------------------------------------------
    # The decision
    # ------------------------------------------------------------

    def route(
        self,
        context: StorageRoutingContext,
        override: StorageTier | None = None,
        override_reason: str | None = None,
        now: datetime | None = None,
    ) -> RoutingDecision:
        """Choose a tier for one record.

        ``override`` is an explicit administrator or research request. It wins
        against SCORES but not against CONSTRAINTS - a manual request for a
        prohibited tier raises rather than being quietly honoured or quietly
        ignored (Step 38).
        """
        policy = self._policy
        scores = self.score_tiers(context)
        prohibited = {s.tier: s.prohibition_reason for s in scores if s.prohibited}
        constraints = tuple(
            f"{tier.value}: {reason}" for tier, reason in sorted(
                prohibited.items(), key=lambda item: item[0].value
            )
        )

        if override is not None:
            if override in prohibited:
                raise PolicyViolationError(
                    f"manual override to {override.value!r} is refused: "
                    f"{prohibited[override]}",
                    sensitivity=context.sensitivity.value,
                    requested_tier=override.value,
                )

            return RoutingDecision(
                representation_id=context.representation_id,
                selected_tier=override,
                forced=True,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                reason=(
                    "manual override"
                    + (f": {override_reason}" if override_reason else "")
                ),
                reason_code=TransitionReason.MANUAL_OVERRIDE,
                scores=scores,
                constraints_applied=constraints,
                context=context,
            )

        allowed = [item for item in scores if not item.prohibited]

        if not allowed:
            # Cannot happen with the default policy, since HOT and WARM are
            # never prohibited together - but a custom policy could do it, and
            # silently picking something would defeat the whole design.
            raise PolicyViolationError(
                f"every tier is prohibited for {context.representation_id!r}; "
                "the policy leaves nowhere to put this record",
                sensitivity=context.sensitivity.value,
            )

        # Deterministic: highest score, ties broken by a fixed tier order so
        # two identical contexts can never route differently.
        order = {StorageTier.HOT: 0, StorageTier.WARM: 1, StorageTier.COLD: 2}
        ranked = sorted(allowed, key=lambda item: (-item.total, order[item.tier]))
        best = ranked[0]

        selected, forced, reason_code, reason = self._apply_hysteresis(
            context, best, ranked, prohibited, now
        )

        return RoutingDecision(
            representation_id=context.representation_id,
            selected_tier=selected,
            forced=forced,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            reason=reason,
            reason_code=reason_code,
            scores=scores,
            constraints_applied=constraints,
            context=context,
        )

    # ------------------------------------------------------------
    # Hysteresis (Step 13)
    # ------------------------------------------------------------

    def _apply_hysteresis(
        self,
        context: StorageRoutingContext,
        best: TierScore,
        ranked: Sequence[TierScore],
        prohibited: dict[StorageTier, str | None],
        now: datetime | None,
    ) -> tuple[StorageTier, bool, TransitionReason, str]:
        """Decide whether a better-scoring tier is worth an actual data move.

        Physically moving a vector costs a write, a verify and a delete. A
        challenger that is barely ahead is not worth that, and acting on it
        produces flapping: HOT today, WARM tomorrow, HOT again when one access
        lands. Margins plus a minimum residence time make the decision sticky
        without making it permanent.
        """
        policy = self._policy
        current = context.current_tier
        order = {StorageTier.HOT: 0, StorageTier.WARM: 1, StorageTier.COLD: 2}

        if current is None:
            return (
                best.tier,
                False,
                TransitionReason.INITIAL_PLACEMENT,
                self._describe(context, best),
            )

        if current in prohibited:
            # The record's current tier has become non-compliant; it must move
            # regardless of margins or residence time.
            return (
                best.tier,
                True,
                TransitionReason.SENSITIVITY_CONSTRAINT,
                (
                    f"current tier {current.value!r} is no longer permitted "
                    f"({prohibited[current]})"
                ),
            )

        if best.tier is current:
            return (
                current,
                False,
                TransitionReason.INITIAL_PLACEMENT,
                f"stays in {current.value}: {self._describe(context, best)}",
            )

        incumbent = next(
            (item.total for item in ranked if item.tier is current), 0.0
        )
        margin = best.total - incumbent
        promoting = order[best.tier] < order[current]
        required = (
            policy.promotion_margin if promoting else policy.demotion_margin
        )

        if margin < required:
            return (
                current,
                False,
                TransitionReason.INITIAL_PLACEMENT,
                (
                    f"stays in {current.value}: {best.tier.value} leads by "
                    f"{margin:.3f}, below the {required:.2f} "
                    f"{'promotion' if promoting else 'demotion'} margin"
                ),
            )

        residence = context.residence_days(now)

        if (
            not promoting
            and residence < policy.minimum_residence_days
        ):
            return (
                current,
                False,
                TransitionReason.INITIAL_PLACEMENT,
                (
                    f"stays in {current.value}: only {residence:.1f} days in "
                    f"tier, below the {policy.minimum_residence_days} day "
                    "minimum residence before demotion"
                ),
            )

        if promoting and not policy.allow_immediate_promotion:
            if residence < policy.minimum_residence_days:
                return (
                    current,
                    False,
                    TransitionReason.INITIAL_PLACEMENT,
                    f"stays in {current.value}: immediate promotion is disabled",
                )

        return (
            best.tier,
            False,
            self._reason_code(context, current, best.tier),
            self._describe(context, best),
        )

    # ------------------------------------------------------------
    # Explanation
    # ------------------------------------------------------------

    def _reason_code(
        self,
        context: StorageRoutingContext,
        current: StorageTier,
        target: StorageTier,
    ) -> TransitionReason:
        """The dominant driver, chosen from the factors that actually moved it."""
        order = {StorageTier.HOT: 0, StorageTier.WARM: 1, StorageTier.COLD: 2}
        promoting = order[target] < order[current]

        if promoting:
            if context.latency_requirement is LatencyRequirement.LOW_LATENCY:
                return TransitionReason.LATENCY_REQUIREMENT
            if context.business_criticality in (
                BusinessCriticality.HIGH,
                BusinessCriticality.CRITICAL,
            ):
                return TransitionReason.BUSINESS_PRIORITY_PROMOTION
            return TransitionReason.HIGH_ACCESS_PROMOTION

        values = self.factors(context)

        if values["dormancy"] >= values["age"]:
            return TransitionReason.LOW_ACCESS_DEMOTION

        return TransitionReason.AGE_DEMOTION

    def _describe(
        self, context: StorageRoutingContext, best: TierScore
    ) -> str:
        """A readable sentence naming the strongest contributions."""
        top = sorted(
            best.contributions, key=lambda item: -item.contribution
        )[:3]

        drivers = ", ".join(
            f"{item.factor}={item.raw_value:.2f}" for item in top if item.contribution > 0
        )

        return (
            f"{best.tier.value} scored {best.total:.3f}"
            + (f" driven by {drivers}" if drivers else "")
        )


__all__ = [
    "StoragePolicyRouter",
]
