"""The tier monitor: evaluate, plan, execute.

    evaluate()           route every record again, report what disagrees
    plan_migrations()    turn disagreements into an ordered plan
    execute_migrations() run the plan through the migration engine

NO DAEMON (Step 13)
-------------------
There is no loop, no thread and no scheduler here. A monitor pass is something
a caller invokes; deciding WHEN to invoke it is Phase 13's job. Building a
background daemon now would embed a scheduling policy in the storage layer,
where nobody could configure it.

DRY RUN IS THE DEFAULT (Step 12)
--------------------------------
``plan_migrations()`` mutates nothing, ever. Planning and executing are
separate calls because moving vectors is expensive and irreversible enough that
somebody should be able to look at the plan first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from erp_pipeline.storage.migration import MigrationEngine
from erp_pipeline.storage.models import (
    MigrationPlan,
    MigrationResult,
    PlannedMigration,
    RoutingDecision,
    StorageRecordMetadata,
    StorageTier,
)
from erp_pipeline.storage.state import TierStateStore
from erp_pipeline.storage.vector_router import StoragePolicyRouter


@dataclass(frozen=True)
class EvaluationEntry:
    """One record's current placement versus what the policy now wants."""

    metadata: StorageRecordMetadata
    decision: RoutingDecision

    @property
    def needs_migration(self) -> bool:
        return self.decision.selected_tier is not self.metadata.current_tier

    def to_dict(self) -> dict[str, Any]:
        return {
            "representation_id": self.metadata.representation_id,
            "current_tier": self.metadata.current_tier.value,
            "proposed_tier": self.decision.selected_tier.value,
            "needs_migration": self.needs_migration,
            "reason_code": self.decision.reason_code.value,
            "reason": self.decision.reason,
            "forced": self.decision.forced,
        }


class TierMonitor:
    """Re-evaluates placements and proposes migrations."""

    def __init__(
        self,
        state_store: TierStateStore,
        engine: MigrationEngine,
        router: StoragePolicyRouter | None = None,
    ) -> None:
        self._state = state_store
        self._engine = engine
        self._router = router or engine.router

    @property
    def router(self) -> StoragePolicyRouter:
        return self._router

    # ------------------------------------------------------------
    # Evaluate
    # ------------------------------------------------------------

    def evaluate(
        self,
        tier: StorageTier | None = None,
        now: datetime | None = None,
    ) -> tuple[EvaluationEntry, ...]:
        """Route every tracked record again. Reads only - changes nothing.

        The router's own hysteresis does the work of NOT proposing marginal
        moves, so anything that comes back wanting to migrate has already
        cleared the margin and residence checks.
        """
        entries: list[EvaluationEntry] = []

        for metadata in self._state.list_all(tier):
            decision = self._router.route(metadata.to_context(now), now=now)
            entries.append(EvaluationEntry(metadata=metadata, decision=decision))

        return tuple(entries)

    # ------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------

    def plan_migrations(
        self,
        tier: StorageTier | None = None,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> MigrationPlan:
        """Build an ordered plan. Never mutates storage or state."""
        entries = self.evaluate(tier, now)
        moving = [entry for entry in entries if entry.needs_migration]

        # Deterministic order so two planning runs over the same state produce
        # the same plan - which is what makes a dry run trustworthy.
        moving.sort(key=lambda entry: entry.metadata.representation_id)

        if limit is not None:
            moving = moving[:limit]

        return MigrationPlan(
            migrations=tuple(
                PlannedMigration(
                    representation_id=entry.metadata.representation_id,
                    from_tier=entry.metadata.current_tier,
                    to_tier=entry.decision.selected_tier,
                    decision=entry.decision,
                )
                for entry in moving
            ),
            evaluated=len(entries),
            unchanged=len(entries) - len(moving),
            policy_id=self._router.policy.policy_id,
            policy_version=self._router.policy.version,
            dry_run=True,
        )

    # ------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------

    def execute_migrations(
        self,
        plan: MigrationPlan | None = None,
        tier: StorageTier | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> MigrationResult:
        """Execute a plan, or plan and execute in one call.

        ``dry_run=True`` returns the plan with zero successes and zero
        failures, having touched nothing - the same shape a real run returns,
        so a caller can render either.
        """
        resolved = plan or self.plan_migrations(tier, limit, now)

        if dry_run:
            return MigrationResult(
                plan=MigrationPlan(
                    migrations=resolved.migrations,
                    evaluated=resolved.evaluated,
                    unchanged=resolved.unchanged,
                    policy_id=resolved.policy_id,
                    policy_version=resolved.policy_version,
                    dry_run=True,
                ),
                succeeded=0,
                failed=0,
                transitions=(),
                duration_seconds=0.0,
            )

        executable = MigrationPlan(
            migrations=resolved.migrations,
            evaluated=resolved.evaluated,
            unchanged=resolved.unchanged,
            policy_id=resolved.policy_id,
            policy_version=resolved.policy_version,
            dry_run=False,
        )

        return self._engine.execute(executable)

    # ------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------

    def distribution(self, now: datetime | None = None) -> dict[str, int]:
        """How records are currently spread across tiers."""
        counts = {tier.value: 0 for tier in StorageTier}

        for metadata in self._state.list_all():
            counts[metadata.current_tier.value] += 1

        return counts


__all__ = [
    "EvaluationEntry",
    "TierMonitor",
]
