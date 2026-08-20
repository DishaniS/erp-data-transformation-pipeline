"""A transparent, normalized cost proxy. Not money.

WHAT THIS IS NOT (Step 94)
--------------------------
It is not a price. There is no cloud tariff here, no dollars, no "70% cheaper".
Inventing prices would make every downstream conclusion unfalsifiable, because
nobody could check the inputs.

WHAT IT IS
----------
    normalized_cost = storage_bytes x resource_multiplier

with the multipliers stated as EXPERIMENTAL ASSUMPTIONS and carried into the
benchmark artifact so a reader can substitute their own and recompute.

WHY MULTIPLIERS AT ALL
----------------------
Bytes alone understate the difference between the tiers, and understating it
would be as misleading as overstating it. A HOT byte sits in RAM inside a
continuously running search process; a COLD byte is a file nobody touches until
someone asks. Those are genuinely different resources, and a model that ignored
that would say a byte is a byte.

The RATIOS are the assumption. Their justification is stated, not asserted:
they are ordinal (HOT > WARM > COLD) and they are round numbers chosen to be
obviously approximate rather than falsely precise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from erp_pipeline.storage.models import MeasurementKind, StorageTier

#: EXPERIMENTAL ASSUMPTIONS. Not prices, not benchmarked, not derived from any
#: vendor. Deliberately round so nobody mistakes them for measurements.
#:
#:   HOT  1.00  full-precision vectors resident in RAM inside a running
#:              search service, replicated in the process's working set
#:   WARM 0.40  on-disk vectors with an int8 quantized copy; still served by
#:              the same process, but off the RAM budget
#:   COLD 0.05  inert encrypted files; no process, no index, no memory - the
#:              cost is disk and nothing else
DEFAULT_RESOURCE_MULTIPLIERS: Mapping[StorageTier, float] = {
    StorageTier.HOT: 1.00,
    StorageTier.WARM: 0.40,
    StorageTier.COLD: 0.05,
}

MULTIPLIER_RATIONALE: Mapping[str, str] = {
    "hot": (
        "full-precision vectors held in RAM by a continuously running search "
        "service; the most expensive resource per byte"
    ),
    "warm": (
        "vectors on disk with an int8 quantized copy used for search; served "
        "by the same process but outside the RAM budget"
    ),
    "cold": (
        "encrypted files at rest; no index, no process and no memory until a "
        "rehydration is requested"
    ),
}


@dataclass(frozen=True)
class TierCost:
    """One tier's normalized cost, with every input visible."""

    tier: StorageTier
    storage_bytes: float
    storage_measurement: MeasurementKind
    resource_multiplier: float
    record_count: int

    @property
    def normalized_cost(self) -> float:
        return round(self.storage_bytes * self.resource_multiplier, 4)

    @property
    def cost_per_record(self) -> float:
        if self.record_count <= 0:
            return 0.0
        return round(self.normalized_cost / self.record_count, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "record_count": self.record_count,
            "storage_bytes": round(self.storage_bytes, 2),
            "storage_measurement": self.storage_measurement.value,
            "resource_multiplier": self.resource_multiplier,
            "normalized_cost": self.normalized_cost,
            "cost_per_record": self.cost_per_record,
        }


@dataclass(frozen=True)
class CostModel:
    """The formula, its assumptions, and the numbers it produced."""

    multipliers: Mapping[StorageTier, float] = field(
        default_factory=lambda: dict(DEFAULT_RESOURCE_MULTIPLIERS)
    )
    formula: str = "normalized_cost = storage_bytes x resource_multiplier"
    basis: str = "VECTOR_PAYLOAD_PROXY bytes, so all three tiers are comparable"

    def cost_for(
        self,
        tier: StorageTier,
        storage_bytes: float,
        record_count: int,
        measurement: MeasurementKind = MeasurementKind.PROXY,
    ) -> TierCost:
        return TierCost(
            tier=tier,
            storage_bytes=storage_bytes,
            storage_measurement=measurement,
            resource_multiplier=self.multipliers[tier],
            record_count=record_count,
        )

    def relative_to_hot(self, costs: Mapping[StorageTier, TierCost]) -> dict[str, float]:
        """Each tier's cost as a fraction of HOT's. The headline ratio."""
        hot = costs.get(StorageTier.HOT)

        if hot is None or hot.normalized_cost <= 0:
            return {}

        return {
            tier.value: round(cost.normalized_cost / hot.normalized_cost, 6)
            for tier, cost in costs.items()
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "basis": self.basis,
            "resource_multipliers": {
                tier.value: value for tier, value in self.multipliers.items()
            },
            "multiplier_rationale": dict(MULTIPLIER_RATIONALE),
            "assumptions": [
                "The multipliers are EXPERIMENTAL ASSUMPTIONS, not prices and "
                "not measurements.",
                "They are ordinal (HOT > WARM > COLD) and deliberately round.",
                "No cloud tariff, vendor quote or currency is implied anywhere.",
                "Substituting different multipliers changes the cost figures "
                "and nothing else; the measured bytes and latencies stand on "
                "their own.",
            ],
        }


DEFAULT_COST_MODEL = CostModel()


__all__ = [
    "DEFAULT_RESOURCE_MULTIPLIERS",
    "MULTIPLIER_RATIONALE",
    "TierCost",
    "CostModel",
    "DEFAULT_COST_MODEL",
]
