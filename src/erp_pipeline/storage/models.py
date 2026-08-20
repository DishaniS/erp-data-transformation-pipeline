"""Tier vocabulary, routing inputs, decisions and audit records.

WHAT IS REUSED
--------------
``EmbeddingRecord`` and ``AIRepresentation`` (Phase 11) and ``SensitivityLevel``
(Phase 1) are imported, not redefined. Phase 12 adds only the storage-side
metadata that genuinely did not exist: which tier a vector is in, why, when it
may move, and what happened when it did.

WHY THE ROUTER NEVER SEES BUSINESS CONTENT
------------------------------------------
``StorageRoutingContext`` carries sensitivity, age, access counts, criticality,
latency and retention - and nothing else. A router that could read an invoice's
amount would be one refactor away from routing on business values, which is
both a privacy problem and an unexplainable one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.schemas.identity import hash_json_payload, normalize_identifier

#: Version of the storage-layer behaviour, recorded on every decision and
#: transition so a placement made under different rules stays traceable.
STORAGE_ENGINE_VERSION = "1.0"


# ============================================================
# Tiers and storage scope (Steps 3, 6)
# ============================================================

class StorageTier(str, Enum):
    """Where a vector physically lives."""

    #: Full-precision, in-memory, lowest latency, highest resource cost.
    HOT = "hot"
    #: Quantized and on-disk: lower footprint, some retrieval trade-off.
    WARM = "warm"
    #: Compressed + authenticated-encrypted archive. Not searchable in place.
    COLD = "cold"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def is_searchable_in_place(self) -> bool:
        """COLD is archival: it must be rehydrated before it can be searched."""
        return self is not StorageTier.COLD


class StorageLocation(str, Enum):
    """Where a tier's data physically resides, for compliance purposes.

    The prototype runs everything locally, but the POLICY CAPABILITY has to
    exist and be tested: a deployment that later adds an off-premises cold
    archive must not be able to route restricted data into it by accident.
    """

    ON_PREMISES = "on_premises"
    EXTERNAL = "external"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class BusinessCriticality(str, Enum):
    """How important a record is to the business (Step 41).

    CONFIGURED, never inferred from an entity name. "invoice" is not
    intrinsically more critical than "customer", and pretending otherwise would
    bake one deployment's assumptions into the framework.
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def weight(self) -> float:
        return _CRITICALITY_WEIGHTS[self]


#: NORMAL is deliberately LOW, not mid-scale. These weights express "demand for
#: fast storage", and an ordinary record demands nothing in particular. An
#: earlier draft used 0.35 here and 0.5 for STANDARD latency, which quietly
#: pulled every default record toward HOT - the middle tier could then only be
#: reached by a record that was actively unusual, which defeats the point of
#: having one.
_CRITICALITY_WEIGHTS: Mapping[BusinessCriticality, float] = {
    BusinessCriticality.LOW: 0.0,
    BusinessCriticality.NORMAL: 0.25,
    BusinessCriticality.HIGH: 0.75,
    BusinessCriticality.CRITICAL: 1.0,
}


class LatencyRequirement(str, Enum):
    """How fast retrieval has to be (Step 42).

    A ROUTING REQUIREMENT, not a guarantee. Placing a record in HOT does not
    promise an SLA; it expresses that the caller asked for the fastest tier
    available.
    """

    RELAXED = "relaxed"
    STANDARD = "standard"
    LOW_LATENCY = "low_latency"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def weight(self) -> float:
        return _LATENCY_WEIGHTS[self]


#: STANDARD means "no special requirement", so it contributes little. See the
#: note on _CRITICALITY_WEIGHTS.
_LATENCY_WEIGHTS: Mapping[LatencyRequirement, float] = {
    LatencyRequirement.RELAXED: 0.0,
    LatencyRequirement.STANDARD: 0.25,
    LatencyRequirement.LOW_LATENCY: 1.0,
}


class TransitionReason(str, Enum):
    """Why a vector moved (Step 32). Stable codes, never free text."""

    INITIAL_PLACEMENT = "initial_placement"
    AGE_DEMOTION = "age_demotion"
    LOW_ACCESS_DEMOTION = "low_access_demotion"
    HIGH_ACCESS_PROMOTION = "high_access_promotion"
    BUSINESS_PRIORITY_PROMOTION = "business_priority_promotion"
    LATENCY_REQUIREMENT = "latency_requirement"
    RETENTION_POLICY = "retention_policy"
    SENSITIVITY_CONSTRAINT = "sensitivity_constraint"
    MANUAL_OVERRIDE = "manual_override"
    STORAGE_PRESSURE = "storage_pressure"
    REHYDRATION_REQUEST = "rehydration_request"
    CONTENT_UPDATE = "content_update"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ============================================================
# Routing input (Step 7)
# ============================================================

@dataclass(frozen=True)
class StorageRoutingContext:
    """Everything the router is allowed to see. Metadata only.

    ``age_days`` is defined ONCE, explicitly (Step 40): days since the
    representation's content was created or last materially changed - i.e. the
    canonical record's own business timeline, not the row's insert timestamp
    and not the last time somebody read it. Read recency is
    ``days_since_access``, kept separate because "old but frequently read" and
    "new but never read" are different routing situations.
    """

    representation_id: str
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    age_days: float = 0.0
    access_count: int = 0
    #: Accesses within the policy's recent window. The signal that matters for
    #: promotion: a record read 900 times two years ago is not hot now.
    recent_access_count: int = 0
    days_since_access: float | None = None
    business_criticality: BusinessCriticality = BusinessCriticality.NORMAL
    latency_requirement: LatencyRequirement = LatencyRequirement.STANDARD
    retention_until: datetime | None = None
    current_tier: StorageTier | None = None
    #: When the record last changed tier, for the hysteresis check.
    tier_since: datetime | None = None
    #: A record under legal hold is never demoted to an archive it cannot be
    #: read from quickly.
    legal_hold: bool = False
    entity_type: str | None = None

    def residence_days(self, now: datetime | None = None) -> float:
        if self.tier_since is None:
            return float("inf")

        moment = now or datetime.now(timezone.utc)
        return max(0.0, (moment - self.tier_since).total_seconds() / 86400.0)

    def retention_active(self, now: datetime | None = None) -> bool:
        if self.retention_until is None:
            return False

        return self.retention_until > (now or datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Privacy-safe: metadata only, by construction."""
        return {
            "representation_id": self.representation_id,
            "sensitivity": self.sensitivity.value,
            "age_days": round(self.age_days, 4),
            "access_count": self.access_count,
            "recent_access_count": self.recent_access_count,
            "days_since_access": self.days_since_access,
            "business_criticality": self.business_criticality.value,
            "latency_requirement": self.latency_requirement.value,
            "retention_until": (
                self.retention_until.isoformat() if self.retention_until else None
            ),
            "current_tier": self.current_tier.value if self.current_tier else None,
            "legal_hold": self.legal_hold,
            "entity_type": self.entity_type,
        }


# ============================================================
# Routing output (Step 9)
# ============================================================

@dataclass(frozen=True)
class FactorContribution:
    """One factor's contribution to one tier's score."""

    factor: str
    raw_value: float
    weight: float
    contribution: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "raw_value": round(self.raw_value, 6),
            "weight": round(self.weight, 6),
            "contribution": round(self.contribution, 6),
        }


@dataclass(frozen=True)
class TierScore:
    """A tier's total score and how it was reached."""

    tier: StorageTier
    total: float
    contributions: tuple[FactorContribution, ...] = ()
    #: True when a hard constraint removed this tier from consideration.
    prohibited: bool = False
    prohibition_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "total": round(self.total, 6),
            "prohibited": self.prohibited,
            "prohibition_reason": self.prohibition_reason,
            "contributions": [item.to_dict() for item in self.contributions],
        }


@dataclass(frozen=True)
class RoutingDecision:
    """Which tier was chosen, and the complete reasoning (Step 9).

    ``forced`` distinguishes the two fundamentally different ways a tier gets
    chosen: a hard constraint or an explicit override LEFT NO CHOICE, versus
    scoring PREFERRED one. Collapsing them would hide whether a placement is
    negotiable.
    """

    representation_id: str
    selected_tier: StorageTier
    forced: bool
    policy_id: str
    policy_version: str
    reason: str
    reason_code: TransitionReason
    scores: tuple[TierScore, ...] = ()
    constraints_applied: tuple[str, ...] = ()
    context: StorageRoutingContext | None = None
    engine_version: str = STORAGE_ENGINE_VERSION

    def score_for(self, tier: StorageTier) -> float:
        for item in self.scores:
            if item.tier is tier:
                return item.total
        return 0.0

    @property
    def prohibited_tiers(self) -> tuple[StorageTier, ...]:
        return tuple(item.tier for item in self.scores if item.prohibited)

    def explain(self) -> str:
        parts = [
            f"{self.representation_id} -> {self.selected_tier.value.upper()}",
            f"({'forced' if self.forced else 'scored'}, {self.reason_code.value})",
            self.reason,
        ]
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "representation_id": self.representation_id,
            "selected_tier": self.selected_tier.value,
            "forced": self.forced,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reason": self.reason,
            "reason_code": self.reason_code.value,
            "scores": [item.to_dict() for item in self.scores],
            "constraints_applied": list(self.constraints_applied),
            "prohibited_tiers": [t.value for t in self.prohibited_tiers],
            "context": self.context.to_dict() if self.context else None,
            "engine_version": self.engine_version,
            "explanation": self.explain(),
        }


# ============================================================
# Storage state (Steps 4, 49)
# ============================================================

@dataclass(frozen=True)
class StorageRecordMetadata:
    """The authoritative record of where a vector is and how it got there.

    Deliberately does NOT contain the vector: that belongs to whichever tier is
    holding it, and duplicating it here would create a second source of truth
    for the thing hardest to keep consistent.
    """

    representation_id: str
    embedding_id: str
    vector_id: str
    current_tier: StorageTier
    content_hash: str
    model_id: str
    dimension: int
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    business_criticality: BusinessCriticality = BusinessCriticality.NORMAL
    latency_requirement: LatencyRequirement = LatencyRequirement.STANDARD
    entity_type: str | None = None
    access_count: int = 0
    recent_access_count: int = 0
    last_accessed_at: datetime | None = None
    created_at: datetime | None = None
    content_updated_at: datetime | None = None
    retention_until: datetime | None = None
    legal_hold: bool = False
    tier_since: datetime | None = None
    policy_id: str | None = None
    policy_version: str | None = None
    #: Optimistic concurrency guard (Step 51).
    version: int = 0
    updated_at: datetime | None = None

    def with_tier(
        self,
        tier: StorageTier,
        policy_id: str | None = None,
        policy_version: str | None = None,
        now: datetime | None = None,
    ) -> "StorageRecordMetadata":
        moment = now or datetime.now(timezone.utc)

        return replace(
            self,
            current_tier=tier,
            tier_since=moment,
            policy_id=policy_id or self.policy_id,
            policy_version=policy_version or self.policy_version,
            version=self.version + 1,
            updated_at=moment,
        )

    def with_access(self, now: datetime | None = None) -> "StorageRecordMetadata":
        moment = now or datetime.now(timezone.utc)

        return replace(
            self,
            access_count=self.access_count + 1,
            recent_access_count=self.recent_access_count + 1,
            last_accessed_at=moment,
            version=self.version + 1,
            updated_at=moment,
        )

    def age_days(self, now: datetime | None = None) -> float:
        """Days since the CONTENT was created or last materially changed."""
        anchor = self.content_updated_at or self.created_at

        if anchor is None:
            return 0.0

        moment = now or datetime.now(timezone.utc)
        return max(0.0, (moment - anchor).total_seconds() / 86400.0)

    def to_context(self, now: datetime | None = None) -> StorageRoutingContext:
        moment = now or datetime.now(timezone.utc)
        since_access = (
            (moment - self.last_accessed_at).total_seconds() / 86400.0
            if self.last_accessed_at
            else None
        )

        return StorageRoutingContext(
            representation_id=self.representation_id,
            sensitivity=self.sensitivity,
            age_days=self.age_days(moment),
            access_count=self.access_count,
            recent_access_count=self.recent_access_count,
            days_since_access=since_access,
            business_criticality=self.business_criticality,
            latency_requirement=self.latency_requirement,
            retention_until=self.retention_until,
            current_tier=self.current_tier,
            tier_since=self.tier_since,
            legal_hold=self.legal_hold,
            entity_type=self.entity_type,
        )

    def to_dict(self) -> dict[str, Any]:
        """Privacy-safe: identities, tier, counts and policy - never content."""
        return {
            "representation_id": self.representation_id,
            "embedding_id": self.embedding_id,
            "vector_id": self.vector_id,
            "current_tier": self.current_tier.value,
            "content_hash": self.content_hash,
            "model_id": self.model_id,
            "dimension": self.dimension,
            "sensitivity": self.sensitivity.value,
            "business_criticality": self.business_criticality.value,
            "latency_requirement": self.latency_requirement.value,
            "entity_type": self.entity_type,
            "access_count": self.access_count,
            "recent_access_count": self.recent_access_count,
            "last_accessed_at": (
                self.last_accessed_at.isoformat() if self.last_accessed_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "content_updated_at": (
                self.content_updated_at.isoformat()
                if self.content_updated_at
                else None
            ),
            "retention_until": (
                self.retention_until.isoformat() if self.retention_until else None
            ),
            "legal_hold": self.legal_hold,
            "tier_since": self.tier_since.isoformat() if self.tier_since else None,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "version": self.version,
        }


# ============================================================
# Transitions and plans (Steps 31, 74)
# ============================================================

@dataclass(frozen=True)
class TierTransition:
    """One auditable tier movement (Step 31).

    Never carries the vector. A transition history over a 33,000-record corpus
    would otherwise become a second copy of the entire index.
    """

    transition_id: str
    representation_id: str
    vector_id: str
    from_tier: StorageTier | None
    to_tier: StorageTier
    reason: TransitionReason
    policy_id: str
    policy_version: str
    succeeded: bool
    occurred_at: datetime
    detail: str | None = None
    duration_seconds: float | None = None
    bytes_written: int | None = None
    forced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "representation_id": self.representation_id,
            "vector_id": self.vector_id,
            "from_tier": self.from_tier.value if self.from_tier else None,
            "to_tier": self.to_tier.value,
            "reason": self.reason.value,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "succeeded": self.succeeded,
            "forced": self.forced,
            "occurred_at": self.occurred_at.isoformat(),
            "detail": self.detail,
            "duration_seconds": self.duration_seconds,
            "bytes_written": self.bytes_written,
        }


def make_transition_id(
    representation_id: str, to_tier: StorageTier, occurred_at: datetime
) -> str:
    """Deterministic given its inputs, and unique per moment."""
    return normalize_identifier(
        "tx."
        + hash_json_payload(
            {
                "representation": representation_id,
                "to": to_tier.value,
                "at": occurred_at.isoformat(),
            }
        )[:20]
    )


@dataclass(frozen=True)
class PlannedMigration:
    """One proposed movement, with the decision that justified it."""

    representation_id: str
    from_tier: StorageTier
    to_tier: StorageTier
    decision: RoutingDecision

    def to_dict(self) -> dict[str, Any]:
        return {
            "representation_id": self.representation_id,
            "from_tier": self.from_tier.value,
            "to_tier": self.to_tier.value,
            "reason_code": self.decision.reason_code.value,
            "reason": self.decision.reason,
            "forced": self.decision.forced,
        }


@dataclass(frozen=True)
class MigrationPlan:
    """What a monitor pass would do, before it does anything (Step 74)."""

    migrations: tuple[PlannedMigration, ...] = ()
    evaluated: int = 0
    unchanged: int = 0
    policy_id: str = ""
    policy_version: str = ""
    dry_run: bool = True

    @property
    def count(self) -> int:
        return len(self.migrations)

    def by_transition(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.migrations:
            key = f"{item.from_tier.value}->{item.to_tier.value}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "unchanged": self.unchanged,
            "planned": self.count,
            "dry_run": self.dry_run,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "by_transition": self.by_transition(),
            "migrations": [item.to_dict() for item in self.migrations],
        }


@dataclass(frozen=True)
class MigrationResult:
    """What a monitor pass actually did."""

    plan: MigrationPlan
    succeeded: int = 0
    failed: int = 0
    transitions: tuple[TierTransition, ...] = ()
    duration_seconds: float = 0.0

    @property
    def counters_balance(self) -> bool:
        return self.plan.count == self.succeeded + self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "planned": self.plan.count,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "counters_balance": self.counters_balance,
            "duration_seconds": self.duration_seconds,
            "by_transition": self.plan.by_transition(),
            "transitions": [item.to_dict() for item in self.transitions],
        }


# ============================================================
# Health and footprint (Steps 56-59, 75)
# ============================================================

@dataclass(frozen=True)
class TierHealth:
    """Whether a tier can currently be written to and read from."""

    tier: StorageTier
    available: bool
    detail: str | None = None
    record_count: int | None = None
    configuration: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "available": self.available,
            "detail": self.detail,
            "record_count": self.record_count,
            "configuration": dict(self.configuration),
        }


class MeasurementKind(str, Enum):
    """How a number was obtained. Never blurred (Step 59)."""

    #: Read from the actual store or the filesystem.
    MEASURED = "measured"
    #: Derived from measured inputs by a documented formula.
    PROXY = "proxy"
    #: An assumption, stated as one.
    ESTIMATED = "estimated"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class StorageFootprint:
    """One tier's storage size, and how honestly it was obtained."""

    tier: StorageTier
    record_count: int
    bytes_total: float
    kind: MeasurementKind
    method: str
    bytes_per_record: float = 0.0
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "record_count": self.record_count,
            "bytes_total": round(self.bytes_total, 2),
            "bytes_per_record": round(self.bytes_per_record, 2),
            "measurement": self.kind.value,
            "method": self.method,
            "detail": dict(self.detail),
        }


__all__ = [
    "STORAGE_ENGINE_VERSION",
    "StorageTier",
    "StorageLocation",
    "BusinessCriticality",
    "LatencyRequirement",
    "TransitionReason",
    "SensitivityLevel",
    "StorageRoutingContext",
    "FactorContribution",
    "TierScore",
    "RoutingDecision",
    "StorageRecordMetadata",
    "TierTransition",
    "make_transition_id",
    "PlannedMigration",
    "MigrationPlan",
    "MigrationResult",
    "TierHealth",
    "MeasurementKind",
    "StorageFootprint",
]
