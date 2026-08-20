"""The versioned storage policy: constraints, weights and thresholds.

THE RESEARCH POINT
------------------
The contribution here is NOT ``if age > 90: cold``. It is a policy object that
separates two things most tiering implementations conflate:

    HARD CONSTRAINTS   compliance. Cannot be outscored, cannot be overridden,
                       not even by an administrator.

    PREFERENCE SCORES  six weighted factors that express what SHOULD happen
                       when nothing forbids anything.

That separation is what makes "restricted data never leaves approved storage"
a guarantee rather than a strong hint. A cost-driven score can always be beaten
by a bigger score; a constraint cannot be beaten at all.

EVERY NUMBER IS HERE
--------------------
No magic constant lives in the router. Thresholds, weights, windows and the
hysteresis settings are all fields on this object, they all contribute to
``fingerprint()``, and the fingerprint is recorded on every decision - so a
placement can always be traced to the exact rules that produced it.

THE WEIGHTS ARE EXPERIMENTAL ASSUMPTIONS
----------------------------------------
They were chosen to express a defensible ERP intuition and then measured. They
are NOT statistically optimized, and this module does not claim they are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.errors import StorageConfigurationError
from erp_pipeline.storage.models import StorageLocation, StorageTier


@dataclass(frozen=True)
class TierWeights:
    """How much each factor pulls toward one tier.

    Read a row as "what makes a record belong HERE". HOT is pulled by recent
    access, criticality and latency demand; COLD is pulled by age and by the
    ABSENCE of those things. WARM deliberately has no strong pull of its own -
    it is where records land when nothing argues strongly for either extreme,
    which is why its weights are the flattest.
    """

    recency: float = 0.0
    access: float = 0.0
    criticality: float = 0.0
    latency: float = 0.0
    age: float = 0.0
    dormancy: float = 0.0

    def total(self) -> float:
        return (
            self.recency + self.access + self.criticality
            + self.latency + self.age + self.dormancy
        )

    def as_mapping(self) -> dict[str, float]:
        return {
            "recency": self.recency,
            "access": self.access,
            "criticality": self.criticality,
            "latency": self.latency,
            "age": self.age,
            "dormancy": self.dormancy,
        }

    def fingerprint(self) -> str:
        return ",".join(
            f"{key}={value}" for key, value in sorted(self.as_mapping().items())
        )


#: Which physical locations each tier may use in this deployment. All local
#: today; the COLD entry is the one a future off-premises archive would change,
#: and the restricted-data constraint is written against exactly this map.
DEFAULT_TIER_LOCATIONS: Mapping[StorageTier, StorageLocation] = {
    StorageTier.HOT: StorageLocation.ON_PREMISES,
    StorageTier.WARM: StorageLocation.ON_PREMISES,
    StorageTier.COLD: StorageLocation.ON_PREMISES,
}


@dataclass(frozen=True)
class StoragePolicy:
    """A versioned, fully explicit tiering policy."""

    policy_id: str = "erp_hybrid_default"
    version: str = "1.0"

    # -- factor normalization --
    #: Age at which the age factor saturates - beyond this a record is simply
    #: "old". 180 days, not a financial year: this is a STORAGE decision, and
    #: an ERP document typically stops being queried long before it stops being
    #: legally interesting. A 365-day saturation left six-month-old records
    #: scoring as "recent", which kept them in HOT well past their useful heat.
    age_saturation_days: float = 180.0
    #: Window counted as "recent" access.
    recent_access_window_days: float = 30.0
    #: Recent-access count at which the access factor saturates.
    access_saturation_count: int = 20
    #: Days without a read at which the dormancy factor saturates.
    dormancy_saturation_days: float = 120.0

    # -- preference weights, per tier --
    hot_weights: TierWeights = field(
        default_factory=lambda: TierWeights(
            recency=0.20, access=0.30, criticality=0.25, latency=0.25
        )
    )
    #: WARM is the default destination for an unremarkable record, so its
    #: weights lean on age and dormancy and stay small everywhere else. It wins
    #: by the others not winning, which is what a middle tier should do.
    warm_weights: TierWeights = field(
        default_factory=lambda: TierWeights(
            recency=0.05, access=0.10, criticality=0.05,
            latency=0.05, age=0.40, dormancy=0.35,
        )
    )
    #: COLD leans on DORMANCY more than on age, deliberately. An even split
    #: let a record that was read last week be archived purely for being old,
    #: because age alone could carry the score. Archiving is about "nobody is
    #: looking at this any more", and age is only a proxy for that.
    cold_weights: TierWeights = field(
        default_factory=lambda: TierWeights(age=0.35, dormancy=0.65)
    )

    # -- hysteresis (Step 13) --
    #: A record must sit in a tier this long before it may be demoted. Stops a
    #: record oscillating when an access count hovers on a threshold.
    minimum_residence_days: float = 7.0
    #: A challenger must beat the incumbent tier by this margin to win.
    #: Without it, 0.501 vs 0.499 would trigger a physical data movement.
    promotion_margin: float = 0.10
    demotion_margin: float = 0.15
    #: Promotion may skip the residence check: a record that suddenly matters
    #: should become fast immediately, and promotion is the safe direction.
    allow_immediate_promotion: bool = True

    # -- hard constraints (Steps 6, 12) --
    #: Sensitivities restricted to on-premises storage. RESTRICTED is here by
    #: default; the field exists so a deployment can add CONFIDENTIAL without
    #: editing code.
    on_premises_only_sensitivities: frozenset[SensitivityLevel] = frozenset(
        {SensitivityLevel.RESTRICTED}
    )
    tier_locations: Mapping[StorageTier, StorageLocation] = field(
        default_factory=lambda: dict(DEFAULT_TIER_LOCATIONS)
    )
    #: A record under active retention or legal hold is never archived to COLD:
    #: it must stay directly readable.
    retention_blocks_cold: bool = True
    legal_hold_blocks_cold: bool = True
    #: A LOW_LATENCY requirement forbids COLD outright - a tier that needs
    #: rehydration cannot satisfy a low-latency requirement, and scoring it
    #: merely "badly" would let a big age score override physics.
    low_latency_blocks_cold: bool = True
    #: CRITICAL records are never archived, however old (Step 68E).
    critical_blocks_cold: bool = True

    def __post_init__(self) -> None:
        for name in (
            "age_saturation_days",
            "recent_access_window_days",
            "dormancy_saturation_days",
        ):
            if getattr(self, name) <= 0:
                raise StorageConfigurationError(
                    f"StoragePolicy.{name} must be positive, got "
                    f"{getattr(self, name)}."
                )

        if self.access_saturation_count < 1:
            raise StorageConfigurationError(
                "access_saturation_count must be at least 1."
            )

        for name in ("promotion_margin", "demotion_margin"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise StorageConfigurationError(
                    f"StoragePolicy.{name} must be a margin in [0, 1], got "
                    f"{value}."
                )

        if self.minimum_residence_days < 0:
            raise StorageConfigurationError(
                "minimum_residence_days must not be negative."
            )

        for tier in StorageTier:
            if tier not in self.tier_locations:
                raise StorageConfigurationError(
                    f"tier_locations does not declare a location for "
                    f"{tier.value!r}; a tier with no declared location cannot "
                    "be checked against the sensitivity constraints."
                )

    # ------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------

    def weights_for(self, tier: StorageTier) -> TierWeights:
        return {
            StorageTier.HOT: self.hot_weights,
            StorageTier.WARM: self.warm_weights,
            StorageTier.COLD: self.cold_weights,
        }[tier]

    def location_of(self, tier: StorageTier) -> StorageLocation:
        return self.tier_locations[tier]

    def requires_on_premises(self, sensitivity: SensitivityLevel) -> bool:
        return sensitivity in self.on_premises_only_sensitivities

    def identity(self) -> str:
        return f"{self.policy_id}@{self.version}"

    def fingerprint(self) -> str:
        """Everything that could change a placement, in one string."""
        return "/".join(
            (
                self.identity(),
                f"age_sat={self.age_saturation_days}",
                f"win={self.recent_access_window_days}",
                f"acc_sat={self.access_saturation_count}",
                f"dorm_sat={self.dormancy_saturation_days}",
                f"hot({self.hot_weights.fingerprint()})",
                f"warm({self.warm_weights.fingerprint()})",
                f"cold({self.cold_weights.fingerprint()})",
                f"resid={self.minimum_residence_days}",
                f"prom={self.promotion_margin}",
                f"demo={self.demotion_margin}",
                f"onprem={sorted(s.value for s in self.on_premises_only_sensitivities)}",
                f"loc={sorted((t.value, l.value) for t, l in self.tier_locations.items())}",
                f"blocks=({int(self.retention_blocks_cold)}"
                f"{int(self.legal_hold_blocks_cold)}"
                f"{int(self.low_latency_blocks_cold)}"
                f"{int(self.critical_blocks_cold)})",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "age_saturation_days": self.age_saturation_days,
            "recent_access_window_days": self.recent_access_window_days,
            "access_saturation_count": self.access_saturation_count,
            "dormancy_saturation_days": self.dormancy_saturation_days,
            "weights": {
                "hot": self.hot_weights.as_mapping(),
                "warm": self.warm_weights.as_mapping(),
                "cold": self.cold_weights.as_mapping(),
            },
            "hysteresis": {
                "minimum_residence_days": self.minimum_residence_days,
                "promotion_margin": self.promotion_margin,
                "demotion_margin": self.demotion_margin,
                "allow_immediate_promotion": self.allow_immediate_promotion,
            },
            "hard_constraints": {
                "on_premises_only_sensitivities": sorted(
                    s.value for s in self.on_premises_only_sensitivities
                ),
                "tier_locations": {
                    t.value: l.value for t, l in self.tier_locations.items()
                },
                "retention_blocks_cold": self.retention_blocks_cold,
                "legal_hold_blocks_cold": self.legal_hold_blocks_cold,
                "low_latency_blocks_cold": self.low_latency_blocks_cold,
                "critical_blocks_cold": self.critical_blocks_cold,
            },
            "fingerprint": self.fingerprint(),
        }


DEFAULT_POLICY = StoragePolicy()


__all__ = [
    "TierWeights",
    "DEFAULT_TIER_LOCATIONS",
    "StoragePolicy",
    "DEFAULT_POLICY",
]
