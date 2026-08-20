"""The public Phase 10 entry point: drift gate, then incremental data sync.

    service = SyncService(state_store, pipeline)

    drift   = service.check_drift(target, new_schema, previous_schema)
    summary = service.run_incremental(target, extractor)
    result  = service.run(target, extractor, new_schema, previous_schema)

THE GATE COMES FIRST (Step 51)
------------------------------
``run()`` discovers-and-compares before it reads a single data row::

    current schema -> compare with catalog -> mapping impact
        BLOCKED  -> process no data
        otherwise -> run incremental extraction

Transforming under a schema the active mapping can no longer survive produces
canonical records that are wrong in a way no downstream stage can detect. It is
far cheaper to stop.

SCHEMA DISCOVERY IS NOT DONE HERE (Step 41)
-------------------------------------------
The caller supplies the freshly discovered ``SourceSchema``, produced by
Phase 4/5/6/7 exactly as those phases already do it. This service compares and
decides; it does not introspect anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Sequence

from erp_pipeline.mapping.canonical_model import CanonicalTargetModel
from erp_pipeline.schemas.source_models import SourceSchema
from erp_pipeline.sync.coordinator import (
    IncrementalCoordinator,
    PropagationPipeline,
    SyncTarget,
)
from erp_pipeline.sync.drift import (
    DriftReport,
    DriftStatus,
    detect_drift,
)
from erp_pipeline.sync.errors import SyncBlockedError
from erp_pipeline.sync.extractor import IncrementalExtractor
from erp_pipeline.sync.impact import MappingImpactReport, analyze_mapping_impact
from erp_pipeline.sync.models import (
    DEFAULT_SYNC_OPTIONS,
    EMPTY_WATERMARK,
    SyncOptions,
    SyncRunStatus,
    SyncRunSummary,
    SyncState,
    SyncStatus,
    WatermarkStrategy,
)
from erp_pipeline.sync.state import SyncStateStore, ensure_state
from erp_pipeline.transformation import TransformationOptions, TransformationService


@dataclass(frozen=True)
class SyncResult:
    """A full run: what the gate decided, and what the data sync did."""

    drift: DriftReport | None
    summary: SyncRunSummary | None
    blocked: bool = False

    @property
    def ran_data_sync(self) -> bool:
        return self.summary is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "drift": self.drift.to_dict() if self.drift else None,
            "summary": self.summary.to_dict() if self.summary else None,
        }


class SyncService:
    """Coordinates schema drift checking and incremental data synchronization."""

    def __init__(
        self,
        state_store: SyncStateStore,
        pipeline: PropagationPipeline,
        transformation_service: TransformationService | None = None,
        canonical_model: CanonicalTargetModel | None = None,
        transformation_options: TransformationOptions | None = None,
    ) -> None:
        self._state_store = state_store
        self._canonical_model = canonical_model
        self._coordinator = IncrementalCoordinator(
            state_store=state_store,
            pipeline=pipeline,
            transformation_service=transformation_service,
            canonical_model=canonical_model,
            transformation_options=transformation_options,
        )

    @property
    def coordinator(self) -> IncrementalCoordinator:
        return self._coordinator

    @property
    def state_store(self) -> SyncStateStore:
        return self._state_store

    # ------------------------------------------------------------
    # Schema drift (Steps 41-51)
    # ------------------------------------------------------------

    def check_drift(
        self,
        target: SyncTarget,
        new_schema: SourceSchema,
        previous_schema: SourceSchema | None,
    ) -> DriftReport:
        """Compare two snapshots and decide whether data sync may proceed.

        The structural comparison is Phase 2's; the verdict additionally
        consults the active mapping, so a removed column nobody maps does not
        stop a pipeline and a removed column feeding a required target does.
        """
        report = detect_drift(
            previous_schema, new_schema, target.source_system_id
        )

        impact: MappingImpactReport = analyze_mapping_impact(
            report,
            target.mapping_profile,
            new_schema,
            self._canonical_model,
        )

        status = (
            impact.status
            if report.has_drift
            else DriftStatus.NO_DRIFT
        )

        return DriftReport(
            source_system_id=report.source_system_id,
            old_schema_id=report.old_schema_id,
            new_schema_id=report.new_schema_id,
            status=status,
            findings=report.findings,
            diff=report.diff,
            impact=impact,
            severity=report.severity,
            reasons=report.reasons,
        )

    # ------------------------------------------------------------
    # Incremental data sync (Steps 14, 35, 36)
    # ------------------------------------------------------------

    def run_incremental(
        self,
        target: SyncTarget,
        extractor: IncrementalExtractor,
        options: SyncOptions | None = None,
        strategy: WatermarkStrategy = WatermarkStrategy.COMPOSITE,
        watermark_field: str | None = None,
        tie_break_field: str | None = None,
        run_id: str | None = None,
    ) -> SyncRunSummary:
        """Fetch one bounded batch of changes and propagate them."""
        options = options or DEFAULT_SYNC_OPTIONS

        state = ensure_state(
            self._state_store,
            target.source_system_id,
            target.source_entity,
            strategy,
            watermark_field=watermark_field,
            tie_break_field=tie_break_field,
        )

        if not state.status.allows_data_sync:
            return SyncRunSummary(
                run_id=run_id or "blocked",
                source_system_id=target.source_system_id,
                source_entity=target.source_entity,
                status=SyncRunStatus.BLOCKED,
                watermark_before=state.watermark,
                watermark_after=state.watermark,
                message=(
                    f"sync state is {state.status.value}; data processing is "
                    "not permitted until it is cleared"
                ),
            )

        changes = list(extractor.fetch_changes(state, options))

        return self._coordinator.run(
            target=target,
            state=state,
            changes=changes,
            options=options,
            run_id=run_id,
        )

    def catch_up(
        self,
        target: SyncTarget,
        extractor: IncrementalExtractor,
        options: SyncOptions | None = None,
        max_batches: int = 100,
        **state_kwargs: Any,
    ) -> tuple[SyncRunSummary, ...]:
        """Run bounded batches until the source is caught up (Step 35).

        Bounded twice over: each batch is limited by ``batch_size``, and the
        number of batches is limited too, so a runaway source cannot turn a
        catch-up into an unbounded loop.
        """
        summaries: list[SyncRunSummary] = []

        for _ in range(max_batches):
            summary = self.run_incremental(
                target, extractor, options, **state_kwargs
            )
            summaries.append(summary)

            if summary.changes_read == 0 or not summary.checkpoint_advanced:
                break

        return tuple(summaries)

    # ------------------------------------------------------------
    # The full gated run (Step 51)
    # ------------------------------------------------------------

    def run(
        self,
        target: SyncTarget,
        extractor: IncrementalExtractor,
        new_schema: SourceSchema | None = None,
        previous_schema: SourceSchema | None = None,
        options: SyncOptions | None = None,
        raise_on_block: bool = False,
        **state_kwargs: Any,
    ) -> SyncResult:
        """Drift gate, then data sync - the recommended entry point."""
        options = options or DEFAULT_SYNC_OPTIONS
        report: DriftReport | None = None

        if options.check_drift and new_schema is not None:
            report = self.check_drift(target, new_schema, previous_schema)

            if report.is_blocked and options.block_on_breaking_drift:
                self._mark_blocked(target)

                if raise_on_block:
                    raise SyncBlockedError(
                        "Schema drift makes the active mapping unusable; "
                        "refusing to process data changes. "
                        + "; ".join(report.impact.reasons[:3])
                        if report.impact
                        else "",
                        report=report,
                    )

                return SyncResult(drift=report, summary=None, blocked=True)

        summary = self.run_incremental(target, extractor, options, **state_kwargs)

        return SyncResult(drift=report, summary=summary, blocked=False)

    def _mark_blocked(self, target: SyncTarget) -> None:
        """Persist the blocked state so a later run cannot quietly proceed."""
        state = self._state_store.load(
            target.source_system_id, target.source_entity
        )

        if state is None:
            state = SyncState(
                source_system_id=target.source_system_id,
                source_entity=target.source_entity,
                strategy=WatermarkStrategy.COMPOSITE,
                watermark=EMPTY_WATERMARK,
            )

        self._state_store.save(
            state.with_status(SyncStatus.BLOCKED),
            expected_version=state.version,
        )

    def clear_block(self, target: SyncTarget) -> SyncState | None:
        """Return a blocked entity to service after a human has reviewed it.

        Deliberately explicit: nothing clears a block automatically, because
        the block exists precisely because a decision was needed.
        """
        state = self._state_store.load(
            target.source_system_id, target.source_entity
        )

        if state is None:
            return None

        return self._state_store.save(
            state.with_status(SyncStatus.ACTIVE),
            expected_version=state.version,
        )


__all__ = [
    "SyncResult",
    "SyncService",
]
