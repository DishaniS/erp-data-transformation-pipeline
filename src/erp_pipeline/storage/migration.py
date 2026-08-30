"""The migration engine: moving a vector between tiers without losing it.

THE ORDER IS THE SAFETY (Steps 8, 30)
-------------------------------------
    1. read authoritative source state
    2. read the vector from the source tier
    3. WRITE the destination
    4. VERIFY the destination
    5. update the authoritative tier state
    6. record the transition
    7. retire the source copy

Nothing before step 5 touches the source. So a failure at 3 or 4 leaves the
source intact and authoritative, and the record is simply still where it was.
The dangerous ordering - delete first, write second - is never used.

Step 7 is deliberately last and deliberately non-fatal. If the source delete
fails after the state has moved, the record is CORRECT but has a stale extra
copy. That is a tidiness problem, and hybrid search deduplicates it. Treating
it as a failure and rolling back would be far worse: it would move the
authoritative pointer back to a tier we have just decided is wrong.

IDENTITY IS INVARIANT (Steps 9, 28)
-----------------------------------
``representation_id``, ``embedding_id``, ``vector_id``, ``content_hash``,
``model_id`` and ``dimension`` are carried through unchanged. A migration is a
change of ADDRESS, never a change of identity - if it minted a new vector id,
every reference held anywhere else would silently break.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.storage.cold_tier import ColdArchiveTier
from erp_pipeline.storage.errors import (
    ColdArchiveIntegrityError,
    MigrationError,
    PolicyViolationError,
    VectorIdentityMismatchError,
)
from erp_pipeline.storage.hot_tier import QdrantHotTier
from erp_pipeline.storage.models import (
    MigrationPlan,
    MigrationResult,
    PlannedMigration,
    StorageRecordMetadata,
    StorageTier,
    TierTransition,
    TransitionReason,
    make_transition_id,
)
from erp_pipeline.storage.state import TierStateStore
from erp_pipeline.storage.vector_router import StoragePolicyRouter
from erp_pipeline.storage.warm_tier import QdrantWarmTier

#: Tolerance for verifying a vector survived a round trip. Float32 through JSON
#: is exact to well within this; the allowance exists for the quantized read
#: path, not for sloppiness.
VECTOR_TOLERANCE = 1e-6


@dataclass
class TierSet:
    """The three concrete tiers, in one place."""

    hot: QdrantHotTier | None = None
    warm: QdrantWarmTier | None = None
    cold: ColdArchiveTier | None = None

    def get(self, tier: StorageTier) -> Any:
        backend = {
            StorageTier.HOT: self.hot,
            StorageTier.WARM: self.warm,
            StorageTier.COLD: self.cold,
        }[tier]

        if backend is None:
            from erp_pipeline.storage.errors import TierUnavailableError

            raise TierUnavailableError(
                f"tier {tier.value!r} is not configured", tier=tier.value
            )

        return backend

    def available(self) -> tuple[StorageTier, ...]:
        return tuple(
            tier
            for tier in StorageTier
            if {
                StorageTier.HOT: self.hot,
                StorageTier.WARM: self.warm,
                StorageTier.COLD: self.cold,
            }[tier]
            is not None
        )


class MigrationEngine:
    """Executes tier transitions safely and auditably."""

    def __init__(
        self,
        tiers: TierSet,
        state_store: TierStateStore,
        router: StoragePolicyRouter | None = None,
    ) -> None:
        self._tiers = tiers
        self._state = state_store
        self._router = router or StoragePolicyRouter()

    @property
    def tiers(self) -> TierSet:
        return self._tiers

    @property
    def router(self) -> StoragePolicyRouter:
        return self._router

    # ------------------------------------------------------------
    # Reading a vector out of whichever tier holds it
    # ------------------------------------------------------------

    def read_record(
        self, metadata: StorageRecordMetadata
    ) -> tuple[EmbeddingRecord, dict[str, Any]]:
        """Reconstruct the full embedding from its current tier."""
        tier = metadata.current_tier

        if tier is StorageTier.COLD:
            cold = self._tiers.get(StorageTier.COLD)
            record = cold.rehydrate(metadata.representation_id)
            payload = cold.stored_payload(metadata.representation_id)
            return record, payload

        backend = self._tiers.get(tier)
        vector = backend.get_vector(metadata.representation_id)

        if vector is None:
            raise MigrationError(
                f"{metadata.representation_id!r} is recorded in "
                f"{tier.value!r} but no vector is stored there",
                stage="read_source",
                source_intact=False,
            )

        record = EmbeddingRecord(
            embedding_id=metadata.embedding_id,
            representation_id=metadata.representation_id,
            entity_type=metadata.entity_type,
            content_hash=metadata.content_hash,
            model_id=metadata.model_id,
            dimension=metadata.dimension,
            status=EmbeddingStatus.GENERATED,
            vector=vector,
        )

        return record, _payload_for(metadata)

    # ------------------------------------------------------------
    # One migration (Steps 8, 30)
    # ------------------------------------------------------------

    def migrate(
        self,
        metadata: StorageRecordMetadata,
        destination: StorageTier,
        reason: TransitionReason = TransitionReason.MANUAL_OVERRIDE,
        forced: bool = False,
        expected_version: int | None = None,
    ) -> tuple[StorageRecordMetadata, TierTransition]:
        """Move one vector, preserving identity and never losing the source."""
        started = time.monotonic()
        source = metadata.current_tier

        if source is destination:
            transition = self._transition(
                metadata, source, destination, reason,
                succeeded=True, forced=forced,
                detail="already in the destination tier; nothing to do",
                duration=0.0,
            )
            self._state.record_transition(transition)
            return metadata, transition

        # -- policy re-check: never migrate into a prohibited tier (Step 7) --
        prohibited = self._router.prohibited_tiers(metadata.to_context())

        if destination in prohibited:
            transition = self._transition(
                metadata, source, destination, reason,
                succeeded=False, forced=forced,
                detail=f"destination prohibited: {prohibited[destination]}",
                duration=round(time.monotonic() - started, 6),
            )
            self._state.record_transition(transition)

            raise PolicyViolationError(
                f"migration of {metadata.representation_id!r} to "
                f"{destination.value!r} is refused: {prohibited[destination]}",
                sensitivity=metadata.sensitivity.value,
                requested_tier=destination.value,
            )

        stage = "read_source"

        try:
            record, payload = self.read_record(metadata)

            # -- 3. write destination --
            stage = "write_destination"
            bytes_written = self._write(destination, record, payload)

            # -- 4. verify destination --
            stage = "verify_destination"
            self._verify(destination, record)

        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            transition = self._transition(
                metadata, source, destination, reason,
                succeeded=False, forced=forced,
                detail=f"failed at {stage}: {type(exc).__name__}",
                duration=round(time.monotonic() - started, 6),
            )
            self._state.record_transition(transition)

            if isinstance(exc, (PolicyViolationError, ColdArchiveIntegrityError)):
                raise

            raise MigrationError(
                f"migration of {metadata.representation_id!r} from "
                f"{source.value} to {destination.value} failed at {stage} "
                f"({type(exc).__name__}); the source copy is untouched",
                stage=stage,
                source_intact=True,
            ) from exc

        # -- 5. the state moves only now --
        updated = metadata.with_tier(
            destination,
            policy_id=self._router.policy.policy_id,
            policy_version=self._router.policy.version,
        )
        self._state.save(
            updated,
            expected_version=(
                expected_version if expected_version is not None else metadata.version
            ),
        )

        # -- 6. audit --
        transition = self._transition(
            metadata, source, destination, reason,
            succeeded=True, forced=forced,
            detail=None,
            duration=round(time.monotonic() - started, 6),
            bytes_written=bytes_written,
        )
        self._state.record_transition(transition)

        # -- 7. retire the source, non-fatally --
        try:
            self._retire(source, metadata.representation_id)
        except Exception:  # noqa: BLE001 - deliberate: state is already correct
            # A stale extra copy is a tidiness problem that hybrid search
            # deduplicates. Rolling back here would move the authoritative
            # pointer to a tier we have just decided is wrong.
            pass

        return updated, transition

    # ------------------------------------------------------------
    # Tier operations
    # ------------------------------------------------------------

    def _write(
        self,
        tier: StorageTier,
        record: EmbeddingRecord,
        payload: Mapping[str, Any],
    ) -> int | None:
        backend = self._tiers.get(tier)

        if tier is StorageTier.COLD:
            envelope = backend.archive(record, payload)
            return envelope.ciphertext_bytes

        backend.upsert(record, payload)

        return (record.dimension * 4) if record.dimension else None

    def _verify(self, tier: StorageTier, record: EmbeddingRecord) -> None:
        """Confirm the destination really holds the vector before anything else.

        For COLD this decrypts and compares - the only way to know the archive
        is readable is to read it. Trusting a successful write would let a
        corrupt archive pass verification and then lose the source.
        """
        backend = self._tiers.get(tier)

        if tier is StorageTier.COLD:
            restored = backend.rehydrate(record.representation_id)
            _assert_same_vector(record, restored)
            return

        stored = backend.get_vector(record.representation_id)

        if stored is None:
            raise MigrationError(
                f"destination {tier.value!r} reports no vector after the write",
                stage="verify_destination",
            )

        if len(stored) != record.dimension:
            raise VectorIdentityMismatchError(
                f"destination {tier.value!r} stored {len(stored)} dimensions, "
                f"expected {record.dimension}"
            )

    def _retire(self, tier: StorageTier, representation_id: str) -> None:
        self._tiers.get(tier).delete(representation_id)

    def _transition(
        self,
        metadata: StorageRecordMetadata,
        source: StorageTier | None,
        destination: StorageTier,
        reason: TransitionReason,
        succeeded: bool,
        forced: bool,
        detail: str | None,
        duration: float,
        bytes_written: int | None = None,
    ) -> TierTransition:
        occurred = datetime.now(timezone.utc)

        return TierTransition(
            transition_id=make_transition_id(
                metadata.representation_id, destination, occurred
            ),
            representation_id=metadata.representation_id,
            vector_id=metadata.vector_id,
            from_tier=source,
            to_tier=destination,
            reason=reason,
            policy_id=self._router.policy.policy_id,
            policy_version=self._router.policy.version,
            succeeded=succeeded,
            forced=forced,
            occurred_at=occurred,
            detail=detail,
            duration_seconds=duration,
            bytes_written=bytes_written,
        )

    # ------------------------------------------------------------
    # Batch execution (Step 74)
    # ------------------------------------------------------------

    def execute(self, plan: MigrationPlan) -> MigrationResult:
        """Execute a plan, continuing past individual failures."""
        started = time.monotonic()
        transitions: list[TierTransition] = []
        succeeded = 0
        failed = 0

        for planned in plan.migrations:
            metadata = self._state.load(planned.representation_id)

            if metadata is None:
                failed += 1
                continue

            try:
                _, transition = self.migrate(
                    metadata,
                    planned.to_tier,
                    reason=planned.decision.reason_code,
                    forced=planned.decision.forced,
                )
                transitions.append(transition)
                succeeded += 1
            except Exception:  # noqa: BLE001 - already audited by migrate()
                failed += 1

        return MigrationResult(
            plan=plan,
            succeeded=succeeded,
            failed=failed,
            transitions=tuple(transitions),
            duration_seconds=round(time.monotonic() - started, 6),
        )


def _assert_same_vector(
    original: EmbeddingRecord, restored: EmbeddingRecord
) -> None:
    """Identity and numeric equality, both checked (Steps 28, 37)."""
    if restored.representation_id != original.representation_id:
        raise VectorIdentityMismatchError(
            "restored representation id does not match the original"
        )

    if restored.embedding_id != original.embedding_id:
        raise VectorIdentityMismatchError(
            "restored embedding id does not match the original"
        )

    if restored.content_hash != original.content_hash:
        raise VectorIdentityMismatchError(
            "restored content hash does not match the original"
        )

    if restored.dimension != original.dimension:
        raise VectorIdentityMismatchError(
            f"restored dimension {restored.dimension} != {original.dimension}"
        )

    if original.vector is None or restored.vector is None:
        raise VectorIdentityMismatchError("a vector is missing after restore")

    worst = max(
        abs(a - b) for a, b in zip(original.vector, restored.vector)
    )

    if worst > VECTOR_TOLERANCE:
        raise VectorIdentityMismatchError(
            f"restored vector differs from the original by {worst:.3e}, "
            f"beyond the {VECTOR_TOLERANCE:.0e} tolerance"
        )


def _payload_for(metadata: StorageRecordMetadata) -> dict[str, Any]:
    """The safe payload stored beside a vector. Identities only, no content.

    The identity fields are what a server-side retrieval filter matches on, so
    they must be in the payload rather than only in PostgreSQL. Keys whose
    value is ``None`` are omitted: a payload key present-and-null and a key
    absent behave differently under a Qdrant match, and omitting is the
    honest encoding of "this record has no such value".
    """
    payload: dict[str, Any] = {
        "representation_id": metadata.representation_id,
        "embedding_id": metadata.embedding_id,
        "content_hash": metadata.content_hash,
        "model_id": metadata.model_id,
        "dimension": metadata.dimension,
        "entity_type": metadata.entity_type,
        "sensitivity": metadata.sensitivity.value,
    }

    optional = {
        "canonical_record_id": metadata.canonical_record_id,
        "source_system_id": metadata.source_system_id,
        "source_entity": metadata.source_entity,
        "record_key": metadata.record_key,
        "document_id": metadata.document_id,
        # -- Phase 4 --
        # Identity a server-side filter matches on, so the ANN search itself is
        # constrained rather than the results being trimmed afterwards.
        "content_kind": metadata.content_kind,
        "parent_record_id": metadata.parent_record_id,
        "source_field": metadata.source_field,
        "business_key_name": metadata.business_key_name,
        "business_key_value": metadata.business_key_value,
        "document_type": metadata.document_type,
        # -- Phase 7 schema provenance --
        "schema_name": metadata.schema_name,
        "entity_kind": metadata.entity_kind,
        "schema_id": metadata.schema_id,
        "schema_version": metadata.schema_version,
        "entity_id": metadata.entity_id,
        "schema_chunk_index": metadata.schema_chunk_index,
        "logical_key": metadata.logical_key,
        # Provenance. Stored as the integers they are, not stringified for the
        # convenience of a filter contract that does not match on them.
        "page_start": metadata.page_start,
        "page_end": metadata.page_end,
        "chunk_index": metadata.chunk_index,
    }

    payload.update({k: v for k, v in optional.items() if v is not None})

    # Dynamic business attributes are top-level Qdrant payload keys so a GET
    # query parameter maps to one server-side FieldCondition. Static provenance
    # always wins on collision.
    payload.update(
        {
            str(key): value
            for key, value in (metadata.filter_attributes or {}).items()
            if str(key) not in payload and value is not None
        }
    )

    return payload


__all__ = [
    "VECTOR_TOLERANCE",
    "TierSet",
    "MigrationEngine",
]
