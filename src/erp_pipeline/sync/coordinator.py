"""The incremental coordinator: one change, all the way down.

    load state -> fetch bounded changes -> for each change:
        Phase 9 transform -> canonical upsert -> affected representations
        -> rebuild -> content hash -> embed only if changed -> vector update
    -> checkpoint safely -> report

THE PROBLEM THIS FIXES
----------------------
The prototype's incremental path stopped after writing an intermediate cleaned
table. Everything downstream - the case aggregate, the AI-ready text, the
embedding, the vector - was refreshed only by full rebuild scripts. So a source
change was "synced" while the vector store still answered questions from stale
content. This module carries a single change the whole way, and refuses to
rebuild anything it does not have to.

CHECKPOINT SAFETY (Steps 6, 31)
-------------------------------
The watermark advances to the last change that completed EVERY stage, and never
past a change that did not. A row that was merely READ does not move it; a row
whose vector write failed does not move it either. That is what makes an
interrupted run resumable without losing work.

TRANSFORMATION IS NOT REIMPLEMENTED (Step 14)
---------------------------------------------
Phase 9's ``TransformationService`` does the transforming. This module supplies
it with records and consumes its results; there is no second conversion,
validation or canonical-assembly path anywhere in this package.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.mapping.canonical_model import CanonicalTargetModel
from erp_pipeline.schemas.canonical_models import CanonicalRecord, SourceReference
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.schemas.identity import (
    make_canonical_record_id,
    normalize_identifier,
)
from erp_pipeline.schemas.mapping_models import MappingProfile
from erp_pipeline.transformation import (
    SourceRecord,
    TransformationContext,
    TransformationOptions,
    TransformationService,
    TRANSFORMATION_ENGINE_VERSION,
)
from erp_pipeline.sync.errors import PropagationError, SyncConfigurationError
from erp_pipeline.sync.models import (
    DEFAULT_SYNC_OPTIONS,
    SYNC_ENGINE_VERSION,
    ChangeOperation,
    ChangeResult,
    FailurePolicy,
    QuarantinedChange,
    SourceChange,
    SyncOptions,
    SyncRunStatus,
    SyncRunSummary,
    SyncStage,
    SyncState,
    SyncStatus,
    Watermark,
)
from erp_pipeline.sync.propagation import (
    AffectedRepresentationResolver,
    AIRepresentation,
    AIRepresentationBuilder,
    CanonicalRecordStore,
    EmbeddingUpdater,
    RepresentationHashLedger,
    VectorRecordStore,
)
from erp_pipeline.sync.state import SyncStateStore


@dataclass(frozen=True)
class SyncTarget:
    """What one incremental run operates on."""

    source_system_id: str
    source_entity: str
    source_type: SourceType
    mapping_profile: MappingProfile
    schema_id: str | None = None
    schema_hash: str | None = None
    ingestion_method: str = "incremental_sync"


@dataclass
class PropagationPipeline:
    """The downstream stages, all optional and all interface-typed.

    A run with only a canonical store is legitimate - it syncs canonical data
    and stops. Supplying a resolver, builder, ledger, embedder and vector store
    is what extends the same run all the way to the vector layer, and no stage
    knows which concrete technology sits behind the next one.
    """

    canonical_store: CanonicalRecordStore | None = None
    resolver: AffectedRepresentationResolver | None = None
    builder: AIRepresentationBuilder | None = None
    ledger: RepresentationHashLedger | None = None
    embedder: EmbeddingUpdater | None = None
    vector_store: VectorRecordStore | None = None

    @property
    def reaches_vectors(self) -> bool:
        return all(
            component is not None
            for component in (
                self.resolver,
                self.builder,
                self.ledger,
                self.embedder,
                self.vector_store,
            )
        )


class IncrementalCoordinator:
    """Runs one entity's incremental synchronization."""

    def __init__(
        self,
        state_store: SyncStateStore,
        pipeline: PropagationPipeline,
        transformation_service: TransformationService | None = None,
        canonical_model: CanonicalTargetModel | None = None,
        transformation_options: TransformationOptions | None = None,
    ) -> None:
        self._state_store = state_store
        self._pipeline = pipeline
        # Reuse, never reimplement: Phase 9 owns transformation entirely.
        self._transformer = transformation_service or TransformationService(
            canonical_model=canonical_model, options=transformation_options
        )

    @property
    def transformation_service(self) -> TransformationService:
        return self._transformer

    # ------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------

    def run(
        self,
        target: SyncTarget,
        state: SyncState,
        changes: Sequence[SourceChange],
        options: SyncOptions | None = None,
        run_id: str | None = None,
    ) -> SyncRunSummary:
        """Process a bounded set of changes and checkpoint safely."""
        options = options or DEFAULT_SYNC_OPTIONS
        resolved_run_id = run_id or self._default_run_id(target, state)

        started = time.monotonic()
        watermark_before = state.watermark

        results: list[ChangeResult] = []
        quarantined: list[QuarantinedChange] = []

        #: The position it is SAFE to resume after. Only advanced past a change
        #: that completed every stage, and frozen the moment one does not.
        safe_watermark = state.watermark
        checkpoint_open = True
        last_key: str | None = state.last_record_key

        for change in changes:
            result = self._process_change(change, target, options)
            results.append(result)

            if result.succeeded:
                if checkpoint_open:
                    safe_watermark = change.watermark
                    last_key = change.record_key
                continue

            if result.quarantined is not None:
                quarantined.append(result.quarantined)

            if options.failure_policy is FailurePolicy.BLOCK:
                # Stop here. Everything after this change is unread, and the
                # checkpoint stays before the failure so a retry sees it again.
                checkpoint_open = False
                break

            if options.failure_policy is FailurePolicy.QUARANTINE:
                # Keep processing to collect the full picture, but never let
                # the checkpoint pass the failure - that would lose it.
                checkpoint_open = False
                continue

            # SKIP: the caller has explicitly accepted losing this change.
            if checkpoint_open:
                safe_watermark = change.watermark
                last_key = change.record_key

        advanced = safe_watermark.is_after(watermark_before) or (
            watermark_before.is_empty and not safe_watermark.is_empty
        )

        failures = sum(1 for item in results if not item.succeeded)
        skipped = sum(
            1
            for item in results
            if not item.succeeded and options.failure_policy is FailurePolicy.SKIP
        )
        failed = failures - skipped

        new_state = state.advanced_to(
            safe_watermark,
            last_record_key=last_key,
            run_id=resolved_run_id,
            status=SyncStatus.ACTIVE if failed == 0 else SyncStatus.FAILED,
            schema_id=target.schema_id,
            schema_hash=target.schema_hash,
            mapping_id=target.mapping_profile.mapping_id,
            engine_version=TRANSFORMATION_ENGINE_VERSION,
        )

        self._state_store.save(new_state, expected_version=state.version)

        return self._summarize(
            run_id=resolved_run_id,
            target=target,
            results=results,
            quarantined=tuple(quarantined),
            watermark_before=watermark_before,
            watermark_after=safe_watermark,
            checkpoint_advanced=advanced,
            failed=failed,
            skipped=skipped,
            duration=round(time.monotonic() - started, 6),
        )

    # ------------------------------------------------------------
    # One change, through every stage
    # ------------------------------------------------------------

    def _process_change(
        self,
        change: SourceChange,
        target: SyncTarget,
        options: SyncOptions,
    ) -> ChangeResult:
        stage = SyncStage.TRANSFORM
        record: CanonicalRecord | None = None
        canonical_upserts = 0
        canonical_deletes = 0
        issue_codes: tuple[str, ...] = ()

        try:
            if change.operation is ChangeOperation.DELETE:
                stage = SyncStage.CANONICAL
                record_id, canonical_deletes = self._delete_canonical(
                    change, target
                )
                # A tombstone stand-in, never stored. The resolver still has to
                # be able to say which representations the deletion affects,
                # and it identifies them from the record - so a deletion that
                # handed it nothing would leave the vector behind, which is
                # precisely the stale-index failure this phase exists to fix.
                record = self._tombstone(change, target, record_id)
            else:
                # --- Phase 9 ---
                result = self._transform(change, target)
                issue_codes = result.issue_codes()

                if not result.is_transformed:
                    return self._quarantine(
                        change,
                        SyncStage.TRANSFORM,
                        reasons=(
                            result.rejected.reasons
                            if result.rejected
                            else ("TRANSFORMATION_FAILED",)
                        ),
                        issue_codes=issue_codes,
                    )

                record = result.record
                record_id = record.record_id

                # --- canonical ---
                stage = SyncStage.CANONICAL
                if self._pipeline.canonical_store is not None:
                    self._pipeline.canonical_store.upsert(record)
                    canonical_upserts = 1

            # --- downstream representation / embedding / vector ---
            stage = SyncStage.REPRESENTATION
            propagation = self._propagate(change, record, options)

            return ChangeResult(
                change=change,
                stage_reached=SyncStage.COMPLETE,
                succeeded=True,
                canonical_record_id=record_id,
                canonical_upserts=canonical_upserts,
                canonical_deletes=canonical_deletes,
                issue_codes=issue_codes,
                **propagation,
            )

        except Exception as exc:  # noqa: BLE001 - deliberate per-change barrier
            # KeyboardInterrupt/SystemExit derive from BaseException and pass
            # straight through, exactly as in Phase 9.
            return self._quarantine(
                change,
                stage,
                reasons=(f"{type(exc).__name__}",),
                issue_codes=issue_codes,
            )

    def _transform(self, change: SourceChange, target: SyncTarget) -> Any:
        """Hand one change to Phase 9. No transformation logic lives here."""
        source_record = SourceRecord.from_mapping(
            dict(change.payload or {}),
            record_key=change.record_key,
            ordinal=change.ordinal,
            source_entity=change.source_entity,
        )

        context = TransformationContext(
            source_type=target.source_type,
            schema_id=target.schema_id,
            ingestion_method=target.ingestion_method,
        )

        return self._transformer.transform_record(
            source_record, target.mapping_profile, context
        )

    def _delete_canonical(
        self, change: SourceChange, target: SyncTarget
    ) -> tuple[str | None, int]:
        """Remove the canonical record a deleted source row produced (Step 17).

        The record id is derived the same deterministic way Phase 9 derives it,
        so a deletion finds exactly the record the insert created.
        """
        record_id = make_canonical_record_id(
            source_system_id=target.source_system_id,
            entity_type=target.mapping_profile.target_entity_type,
            stable_source_key=change.record_key,
        )

        if self._pipeline.canonical_store is None:
            return record_id, 0

        removed = self._pipeline.canonical_store.delete(record_id)
        return record_id, 1 if removed else 0

    def _tombstone(
        self, change: SourceChange, target: SyncTarget, record_id: str
    ) -> CanonicalRecord:
        """A non-persisted marker carrying the deleted record's identity.

        Built with the same deterministic id derivation the insert used, so the
        resolver resolves exactly the representations the live record fed.
        """
        return CanonicalRecord(
            record_id=record_id,
            source=SourceReference(
                source_system_id=target.source_system_id,
                source_type=target.source_type,
                source_entity=target.source_entity,
                source_record_key=change.record_key,
            ),
            entity_type=target.mapping_profile.target_entity_type,
            normalized_data={},
            metadata={"tombstone": True},
        )

    # ------------------------------------------------------------
    # Steps 18-25: affected representations only
    # ------------------------------------------------------------

    def _propagate(
        self,
        change: SourceChange,
        record: CanonicalRecord | None,
        options: SyncOptions,
    ) -> dict[str, Any]:
        """Rebuild, hash-compare, embed and store vectors - minimally."""
        empty = {
            "representation_keys": (),
            "representations_changed": (),
            "representations_unchanged": (),
            "embeddings_generated": 0,
            "embeddings_skipped": 0,
            "vectors_upserted": 0,
            "vectors_deleted": 0,
        }

        pipeline = self._pipeline

        if pipeline.resolver is None or pipeline.builder is None:
            return empty

        keys = tuple(pipeline.resolver.resolve_affected(change, record))

        if not keys:
            return empty

        changed: list[str] = []
        unchanged: list[str] = []
        generated = 0
        skipped = 0
        upserted = 0
        deleted = 0

        for key in keys:
            representation = pipeline.builder.rebuild(key)

            if representation is None:
                # The aggregate no longer exists - its last member was deleted.
                # Its vector must go too, or the index keeps answering from
                # content that is gone.
                deleted += self._drop_representation(key)
                continue

            new_hash = representation.resolved_hash()
            previous = (
                pipeline.ledger.get_hash(key) if pipeline.ledger else None
            )

            if previous == new_hash and not options.force_reembed:
                # THE POINT OF THE WHOLE PHASE: identical content is not
                # re-embedded, however many times its source is touched.
                unchanged.append(key)
                skipped += 1
                continue

            changed.append(key)

            if pipeline.embedder is None:
                continue

            embedding = pipeline.embedder.embed(representation)
            generated += 1

            if pipeline.vector_store is not None:
                pipeline.vector_store.upsert(representation, embedding)
                upserted += 1

            # Recorded LAST, so a failure at any earlier stage leaves the
            # ledger showing the old hash and the retry redoes the work.
            if pipeline.ledger is not None:
                pipeline.ledger.set_hash(key, new_hash)

        return {
            "representation_keys": keys,
            "representations_changed": tuple(changed),
            "representations_unchanged": tuple(unchanged),
            "embeddings_generated": generated,
            "embeddings_skipped": skipped,
            "vectors_upserted": upserted,
            "vectors_deleted": deleted,
        }

    def _drop_representation(self, key: str) -> int:
        from erp_pipeline.sync.hashing import vector_id_for

        removed = 0

        if self._pipeline.vector_store is not None:
            if self._pipeline.vector_store.delete(vector_id_for(key)):
                removed = 1

        if self._pipeline.ledger is not None:
            self._pipeline.ledger.forget(key)

        return removed

    # ------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------

    def _quarantine(
        self,
        change: SourceChange,
        stage: SyncStage,
        reasons: Sequence[str],
        issue_codes: Sequence[str] = (),
    ) -> ChangeResult:
        quarantined = QuarantinedChange(
            reference=change.reference(),
            record_key=change.record_key,
            source_entity=change.source_entity,
            stage=stage,
            reasons=tuple(reasons) or ("UNSPECIFIED_FAILURE",),
            watermark=change.watermark,
            issue_codes=tuple(issue_codes),
        )

        return ChangeResult(
            change=change,
            stage_reached=stage,
            succeeded=False,
            quarantined=quarantined,
            issue_codes=tuple(issue_codes),
        )

    def _summarize(
        self,
        run_id: str,
        target: SyncTarget,
        results: Sequence[ChangeResult],
        quarantined: tuple[QuarantinedChange, ...],
        watermark_before: Watermark,
        watermark_after: Watermark,
        checkpoint_advanced: bool,
        failed: int,
        skipped: int,
        duration: float,
    ) -> SyncRunSummary:
        processed = sum(1 for item in results if item.succeeded)

        resolved_keys: set[str] = set()
        for item in results:
            resolved_keys.update(item.representation_keys)

        status = (
            SyncRunStatus.SUCCEEDED
            if failed == 0 and skipped == 0
            else SyncRunStatus.PARTIAL
            if processed > 0
            else SyncRunStatus.FAILED
        )

        return SyncRunSummary(
            run_id=run_id,
            source_system_id=target.source_system_id,
            source_entity=target.source_entity,
            status=status,
            watermark_before=watermark_before,
            watermark_after=watermark_after,
            changes_read=len(results),
            changes_processed=processed,
            changes_failed=failed,
            changes_skipped=skipped,
            canonical_upserts=sum(item.canonical_upserts for item in results),
            canonical_deletes=sum(item.canonical_deletes for item in results),
            representations_resolved=len(resolved_keys),
            representations_rebuilt=sum(
                len(item.representation_keys) for item in results
            ),
            representations_changed=sum(
                len(item.representations_changed) for item in results
            ),
            representations_unchanged=sum(
                len(item.representations_unchanged) for item in results
            ),
            embeddings_generated=sum(
                item.embeddings_generated for item in results
            ),
            embeddings_skipped=sum(item.embeddings_skipped for item in results),
            vectors_upserted=sum(item.vectors_upserted for item in results),
            vectors_deleted=sum(item.vectors_deleted for item in results),
            checkpoint_advanced=checkpoint_advanced,
            duration_seconds=duration,
            results=tuple(results),
            quarantined=quarantined,
            schema_id=target.schema_id,
            schema_hash=target.schema_hash,
            mapping_id=target.mapping_profile.mapping_id,
            transformation_engine_version=TRANSFORMATION_ENGINE_VERSION,
            sync_engine_version=SYNC_ENGINE_VERSION,
        )

    def _default_run_id(self, target: SyncTarget, state: SyncState) -> str:
        """Deterministic run id derived from position, not from a clock."""
        return normalize_identifier(
            f"sync.{target.source_system_id}.{target.source_entity}."
            f"v{state.version}"
        )


__all__ = [
    "SyncTarget",
    "PropagationPipeline",
    "IncrementalCoordinator",
]
