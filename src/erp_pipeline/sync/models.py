"""Sync state, watermarks, source changes and run reporting.

WHAT IS AND IS NOT NEW HERE
---------------------------
Nothing in this module replaces a frozen contract. ``CanonicalRecord``,
``TransformationRun``, ``DataQualityIssue``, ``MappingProfile`` and
``SourceSchema`` are all reused as they are. What this module adds is the
vocabulary Phase 10 genuinely needs and no earlier phase has:

``SyncState``     where extraction got to, per source system and entity
``Watermark``     the position itself, comparable and serializable
``SourceChange``  one detected change, with its operation and position
``SyncRunSummary``what one incremental run did, in counters

SYNC STATE IS NOT THE SCHEMA CATALOG
------------------------------------
The Phase 2 catalog stores what a source LOOKS LIKE over time. Sync state
stores how far through its DATA we have read. Conflating them would mean a
schema republish silently moved a data checkpoint, so they stay in separate
stores with separate lifecycles (Step 33).

PRIVACY
-------
``SourceChange`` carries the raw source record in memory, because Phase 9 has
to transform it. It is excluded from every serialization by default - the same
rule Phase 9's ``RejectedRecord`` follows.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.schemas.identity import hash_json_payload, normalize_identifier

#: Version of the sync engine's behaviour, recorded on every run so a
#: checkpoint written under different semantics is traceable.
SYNC_ENGINE_VERSION = "1.0"


# ============================================================
# Watermarks (Steps 4, 5)
# ============================================================

class WatermarkStrategy(str, Enum):
    """How a source reports "what changed since last time".

    Deliberately several, because no single strategy works for every ERP
    (Step 4). A source that supports none of them is not incrementally
    syncable, and the framework says so rather than pretending.
    """

    #: An updated-at style column. Simple, and unsafe alone - see COMPOSITE.
    TIMESTAMP = "timestamp"
    #: A monotonically increasing surrogate key. Safe alone when truly
    #: monotonic, but blind to in-place updates.
    MONOTONIC_ID = "monotonic_id"
    #: ``(timestamp, tie_breaker)``. The correct default for SQL sources: a
    #: timestamp-only watermark loses rows that share a timestamp across a
    #: batch boundary (Step 5).
    COMPOSITE = "composite"
    #: An opaque token the source itself issues and interprets.
    SOURCE_CURSOR = "source_cursor"
    #: Whole-artifact content hashing, for file and specification sources
    #: where row-level change detection does not exist (Steps 12, 13).
    CONTENT_HASH = "content_hash"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True, order=False)
class Watermark:
    """A position in a source's change ordering.

    Comparison is lexicographic over ``(timestamp, tie_breaker)`` for
    time-based strategies and over ``tie_breaker`` alone for id-based ones,
    which is exactly the ordering the extraction query must use for the
    watermark to mean anything.

    ``EMPTY`` (all components ``None``) means "nothing has been synced yet" and
    sorts before every real position.
    """

    timestamp: datetime | None = None
    #: The tie-breaking key: a primary key, an object id, a row id.
    tie_breaker: Any = None
    #: An opaque source-issued token, for SOURCE_CURSOR.
    cursor: str | None = None
    #: A content digest, for CONTENT_HASH sources.
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if self.timestamp is not None and self.timestamp.tzinfo is None:
            # A naive timestamp cannot be compared against an aware one, and a
            # watermark that raises on comparison mid-batch is worse than one
            # that refuses to be constructed.
            raise ValueError(
                "Watermark.timestamp must be timezone-aware. A naive watermark "
                "cannot be ordered against source timestamps reliably."
            )

    @property
    def is_empty(self) -> bool:
        return (
            self.timestamp is None
            and self.tie_breaker is None
            and self.cursor is None
            and self.content_hash is None
        )

    def sort_key(self) -> tuple:
        """A total order over watermarks of the same strategy.

        ``None`` components sort first, so an empty watermark precedes every
        real position and a fresh sync starts from the beginning.
        """
        return (
            self.timestamp is not None,
            self.timestamp or datetime.min.replace(tzinfo=timezone.utc),
            self.tie_breaker is not None,
            _comparable(self.tie_breaker),
        )

    def is_after(self, other: "Watermark") -> bool:
        return self.sort_key() > other.sort_key()

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": (
                self.timestamp.astimezone(timezone.utc).isoformat()
                if self.timestamp
                else None
            ),
            "tie_breaker": (
                None if self.tie_breaker is None else str(self.tie_breaker)
            ),
            "cursor": self.cursor,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "Watermark":
        if not payload:
            return cls()

        raw = payload.get("timestamp")
        timestamp = datetime.fromisoformat(raw) if raw else None
        if timestamp is not None and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        return cls(
            timestamp=timestamp,
            tie_breaker=payload.get("tie_breaker"),
            cursor=payload.get("cursor"),
            content_hash=payload.get("content_hash"),
        )

    def describe(self) -> str:
        """A safe one-line description - a position, never a business value."""
        if self.is_empty:
            return "<none>"
        parts = []
        if self.timestamp is not None:
            parts.append(self.timestamp.astimezone(timezone.utc).isoformat())
        if self.tie_breaker is not None:
            parts.append(f"#{self.tie_breaker}")
        if self.cursor:
            parts.append(f"cursor={self.cursor}")
        if self.content_hash:
            parts.append(f"hash={self.content_hash[:12]}")
        return "/".join(parts)


EMPTY_WATERMARK = Watermark()


def _comparable(value: Any) -> tuple[int, Any]:
    """Order mixed tie-breaker types without raising.

    Integer keys and string keys both occur, and a batch must never crash
    because one entity uses one and another uses the other.
    """
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (1, value)
    return (2, str(value))


# ============================================================
# Sync state (Step 3)
# ============================================================

class SyncStatus(str, Enum):
    """Lifecycle of one entity's synchronization."""

    #: Never synced. The next run reads from the beginning.
    NEW = "new"
    #: Caught up as of the recorded watermark.
    ACTIVE = "active"
    #: A run failed; the watermark is still valid and safe to resume from.
    FAILED = "failed"
    #: Schema drift makes further data processing unsafe (Step 51).
    BLOCKED = "blocked"
    #: Deliberately paused by an operator.
    PAUSED = "paused"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def allows_data_sync(self) -> bool:
        return self in (SyncStatus.NEW, SyncStatus.ACTIVE, SyncStatus.FAILED)


@dataclass(frozen=True)
class SyncState:
    """Durable extraction progress for one (source system, entity).

    Watermarks are tracked PER ENTITY (Step 34). One global timestamp for a
    whole ERP would mean a slow-moving ``customers`` table dragging a
    fast-moving ``invoices`` table backwards, or worse, forwards past unread
    rows.

    ``version`` is an optimistic-concurrency revision. Every checkpoint write
    asserts the version it read, so two runs cannot both advance the same
    entity and silently skip whatever the loser had not processed (Steps 67,
    68).
    """

    source_system_id: str
    source_entity: str
    strategy: WatermarkStrategy
    watermark: Watermark = EMPTY_WATERMARK
    #: Source field carrying the ordering value (``updated_at``).
    watermark_field: str | None = None
    #: Source field breaking ties at an equal watermark (``id``).
    tie_break_field: str | None = None
    #: Business key of the last record durably processed. Diagnostic only -
    #: the watermark is the authority.
    last_record_key: str | None = None
    #: Which schema snapshot the last successful run ran against (Step 53).
    schema_id: str | None = None
    schema_hash: str | None = None
    #: Which mapping produced the canonical records (Step 53).
    mapping_id: str | None = None
    transformation_engine_version: str | None = None
    last_run_id: str | None = None
    status: SyncStatus = SyncStatus.NEW
    version: int = 0
    updated_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity of this state row."""
        return f"{self.source_system_id}:{self.source_entity}"

    @property
    def is_fresh(self) -> bool:
        return self.watermark.is_empty

    def advanced_to(
        self,
        watermark: Watermark,
        *,
        last_record_key: str | None = None,
        run_id: str | None = None,
        status: SyncStatus = SyncStatus.ACTIVE,
        schema_id: str | None = None,
        schema_hash: str | None = None,
        mapping_id: str | None = None,
        engine_version: str | None = None,
    ) -> "SyncState":
        """A copy positioned at a new watermark, with the version bumped.

        Never moves BACKWARDS: an out-of-order checkpoint would re-read
        already-processed changes at best and skip changes at worst. A caller
        attempting it keeps the existing position.
        """
        target = (
            watermark
            if watermark.is_after(self.watermark) or self.watermark.is_empty
            else self.watermark
        )

        return replace(
            self,
            watermark=target,
            last_record_key=last_record_key or self.last_record_key,
            last_run_id=run_id or self.last_run_id,
            status=status,
            schema_id=schema_id or self.schema_id,
            schema_hash=schema_hash or self.schema_hash,
            mapping_id=mapping_id or self.mapping_id,
            transformation_engine_version=(
                engine_version or self.transformation_engine_version
            ),
            version=self.version + 1,
            updated_at=datetime.now(timezone.utc),
        )

    def with_status(self, status: SyncStatus) -> "SyncState":
        return replace(
            self,
            status=status,
            version=self.version + 1,
            updated_at=datetime.now(timezone.utc),
        )

    def to_dict(self) -> dict[str, Any]:
        """Privacy-safe: positions, ids and versions only."""
        return {
            "source_system_id": self.source_system_id,
            "source_entity": self.source_entity,
            "strategy": self.strategy.value,
            "watermark": self.watermark.to_dict(),
            "watermark_field": self.watermark_field,
            "tie_break_field": self.tie_break_field,
            "last_record_key": self.last_record_key,
            "schema_id": self.schema_id,
            "schema_hash": self.schema_hash,
            "mapping_id": self.mapping_id,
            "transformation_engine_version": self.transformation_engine_version,
            "last_run_id": self.last_run_id,
            "status": self.status.value,
            "version": self.version,
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
            "metadata": dict(self.metadata),
        }


# ============================================================
# Source changes (Step 8)
# ============================================================

class ChangeOperation(str, Enum):
    """What happened to a source record."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def is_removal(self) -> bool:
        return self is ChangeOperation.DELETE


@dataclass(frozen=True)
class SourceChange:
    """One detected source change.

    ``payload`` holds the raw source record IN MEMORY, because Phase 9 must
    transform it. It is never serialized by ``to_dict()``: the same rule
    Phase 9 applies to rejected records applies here, and a sync report that
    quietly became a copy of the ERP would be the worst possible leak.
    """

    source_system_id: str
    source_entity: str
    record_key: str
    operation: ChangeOperation
    watermark: Watermark
    payload: Mapping[str, Any] | None = None
    ordinal: int | None = None

    @property
    def idempotency_key(self) -> str:
        """Deterministic processing identity (Step 69).

        Derived from what actually identifies this change - system, entity,
        record and position - and explicitly NOT from a run UUID, so replaying
        the same change in a later run is recognizable as the same work.
        """
        return normalize_identifier(
            "chg."
            + hash_json_payload(
                {
                    "system": self.source_system_id,
                    "entity": self.source_entity,
                    "record": self.record_key,
                    "operation": self.operation.value,
                    "watermark": self.watermark.to_dict(),
                }
            )[:16]
        )

    def reference(self) -> str:
        """A safe way to name this change in a report - never its contents."""
        return f"{self.source_entity}:{self.record_key}"

    def to_dict(self) -> dict[str, Any]:
        """Privacy-safe by default: the payload is deliberately absent."""
        return {
            "source_system_id": self.source_system_id,
            "source_entity": self.source_entity,
            "record_key": self.record_key,
            "operation": self.operation.value,
            "watermark": self.watermark.to_dict(),
            "ordinal": self.ordinal,
            "idempotency_key": self.idempotency_key,
        }


# ============================================================
# Propagation stages and outcomes (Steps 37, 39, 40, 64, 65)
# ============================================================

class SyncStage(str, Enum):
    """Where in the downstream pipeline a change got to.

    Recorded on every failure so a retry knows what has already happened and a
    report says where the pipeline actually stopped - "it failed" is not
    actionable, "it failed at VECTOR" is.
    """

    EXTRACT = "extract"
    TRANSFORM = "transform"
    CANONICAL = "canonical"
    REPRESENTATION = "representation"
    EMBEDDING = "embedding"
    VECTOR = "vector"
    COMPLETE = "complete"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class FailurePolicy(str, Enum):
    """What a run does when one change cannot be processed (Step 39)."""

    #: Stop the run at the first failure and leave the checkpoint before it.
    #: The safest option and the DEFAULT: a change that failed is a change
    #: whose downstream state is unknown, and reading past it risks a
    #: permanently stale representation.
    BLOCK = "block"
    #: Record the failure, quarantine the change, and keep going. The
    #: checkpoint still stops before the earliest failure.
    QUARANTINE = "quarantine"
    #: Record the failure and keep going, allowing the checkpoint to pass it.
    #: Explicitly loses the change - only for callers who have decided that is
    #: acceptable.
    SKIP = "skip"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class QuarantinedChange:
    """A change that could not be processed (Step 40).

    Privacy-safe by construction: it records WHERE the change was and WHY it
    failed, never what it contained.
    """

    reference: str
    record_key: str
    source_entity: str
    stage: SyncStage
    reasons: tuple[str, ...]
    watermark: Watermark
    issue_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reasons:
            from erp_pipeline.sync.errors import SyncError

            raise SyncError(
                "A quarantined change must state at least one reason. An "
                "unexplained failure is not actionable."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "record_key": self.record_key,
            "source_entity": self.source_entity,
            "stage": self.stage.value,
            "reasons": list(self.reasons),
            "issue_codes": list(self.issue_codes),
            "watermark": self.watermark.to_dict(),
        }


@dataclass(frozen=True)
class ChangeResult:
    """What happened to one change as it travelled downstream."""

    change: SourceChange
    stage_reached: SyncStage
    succeeded: bool
    canonical_record_id: str | None = None
    representation_keys: tuple[str, ...] = ()
    representations_changed: tuple[str, ...] = ()
    representations_unchanged: tuple[str, ...] = ()
    embeddings_generated: int = 0
    embeddings_skipped: int = 0
    vectors_upserted: int = 0
    vectors_deleted: int = 0
    canonical_upserts: int = 0
    canonical_deletes: int = 0
    quarantined: QuarantinedChange | None = None
    issue_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "change": self.change.to_dict(),
            "stage_reached": self.stage_reached.value,
            "succeeded": self.succeeded,
            "canonical_record_id": self.canonical_record_id,
            "representation_keys": list(self.representation_keys),
            "representations_changed": list(self.representations_changed),
            "representations_unchanged": list(self.representations_unchanged),
            "embeddings_generated": self.embeddings_generated,
            "embeddings_skipped": self.embeddings_skipped,
            "vectors_upserted": self.vectors_upserted,
            "vectors_deleted": self.vectors_deleted,
            "issue_codes": list(self.issue_codes),
            "quarantined": (
                self.quarantined.to_dict() if self.quarantined else None
            ),
        }


# ============================================================
# Run reporting (Steps 37, 38, 70)
# ============================================================

class SyncRunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class SyncOptions:
    """Everything that changes how a run behaves."""

    #: Maximum changes read in one run. Bounded by default: an unbounded
    #: incremental fetch is a full reload wearing a different name (Step 35).
    batch_size: int = 500
    failure_policy: FailurePolicy = FailurePolicy.BLOCK
    #: Run the drift gate before touching data (Step 51).
    check_drift: bool = True
    #: Refuse to process data when drift analysis says BLOCKED.
    block_on_breaking_drift: bool = True
    #: Propagate deletions downstream where the strategy can detect them.
    process_deletes: bool = True
    #: Emit vector work even when the content hash is unchanged. Off, because
    #: skipping unchanged content is the entire point of hashing (Step 22).
    force_reembed: bool = False

    def __post_init__(self) -> None:
        from erp_pipeline.sync.errors import SyncConfigurationError

        if self.batch_size < 1:
            raise SyncConfigurationError(
                f"batch_size must be at least 1, got {self.batch_size}."
            )

    def fingerprint(self) -> str:
        return (
            f"sync@{SYNC_ENGINE_VERSION}/batch={self.batch_size}"
            f"/fail={self.failure_policy.value}/drift={int(self.check_drift)}"
            f"/block={int(self.block_on_breaking_drift)}"
            f"/del={int(self.process_deletes)}/force={int(self.force_reembed)}"
        )


DEFAULT_SYNC_OPTIONS = SyncOptions()


@dataclass(frozen=True)
class SyncRunSummary:
    """Everything one incremental run did.

    COUNTER INVARIANTS (Step 38), asserted by tests::

        changes_read == changes_processed + changes_failed + changes_skipped
        embedding_candidates == embeddings_generated + embeddings_skipped

    Every counter is a count of work actually performed, not an estimate.
    """

    run_id: str
    source_system_id: str
    source_entity: str
    status: SyncRunStatus
    watermark_before: Watermark
    watermark_after: Watermark
    changes_read: int = 0
    changes_processed: int = 0
    changes_failed: int = 0
    changes_skipped: int = 0
    canonical_upserts: int = 0
    canonical_deletes: int = 0
    representations_resolved: int = 0
    representations_rebuilt: int = 0
    representations_changed: int = 0
    representations_unchanged: int = 0
    embeddings_generated: int = 0
    embeddings_skipped: int = 0
    vectors_upserted: int = 0
    vectors_deleted: int = 0
    checkpoint_advanced: bool = False
    duration_seconds: float = 0.0
    results: tuple[ChangeResult, ...] = ()
    quarantined: tuple[QuarantinedChange, ...] = ()
    drift_report: Any = None
    #: Recorded for reproducibility (Step 53).
    schema_id: str | None = None
    schema_hash: str | None = None
    mapping_id: str | None = None
    transformation_engine_version: str | None = None
    sync_engine_version: str = SYNC_ENGINE_VERSION
    message: str | None = None

    @property
    def counters_balance(self) -> bool:
        return self.changes_read == (
            self.changes_processed + self.changes_failed + self.changes_skipped
        )

    @property
    def embedding_candidates(self) -> int:
        return self.embeddings_generated + self.embeddings_skipped

    @property
    def embedding_counters_balance(self) -> bool:
        return self.embedding_candidates == (
            self.embeddings_generated + self.embeddings_skipped
        )

    @property
    def is_clean(self) -> bool:
        return (
            self.status is SyncRunStatus.SUCCEEDED
            and self.changes_failed == 0
        )

    def to_dict(self) -> dict[str, Any]:
        """Privacy-safe metrics (Step 70). No source values anywhere."""
        return {
            "run_id": self.run_id,
            "source_system_id": self.source_system_id,
            "source_entity": self.source_entity,
            "status": self.status.value,
            "watermark_before": self.watermark_before.to_dict(),
            "watermark_after": self.watermark_after.to_dict(),
            "changes_read": self.changes_read,
            "changes_processed": self.changes_processed,
            "changes_failed": self.changes_failed,
            "changes_skipped": self.changes_skipped,
            "canonical_upserts": self.canonical_upserts,
            "canonical_deletes": self.canonical_deletes,
            "representations_resolved": self.representations_resolved,
            "representations_rebuilt": self.representations_rebuilt,
            "representations_changed": self.representations_changed,
            "representations_unchanged": self.representations_unchanged,
            "embeddings_generated": self.embeddings_generated,
            "embeddings_skipped": self.embeddings_skipped,
            "embedding_candidates": self.embedding_candidates,
            "vectors_upserted": self.vectors_upserted,
            "vectors_deleted": self.vectors_deleted,
            "checkpoint_advanced": self.checkpoint_advanced,
            "counters_balance": self.counters_balance,
            "duration_seconds": self.duration_seconds,
            "quarantined": [item.to_dict() for item in self.quarantined],
            "schema_id": self.schema_id,
            "schema_hash": self.schema_hash,
            "mapping_id": self.mapping_id,
            "transformation_engine_version": self.transformation_engine_version,
            "sync_engine_version": self.sync_engine_version,
            "message": self.message,
        }


__all__ = [
    "SYNC_ENGINE_VERSION",
    "WatermarkStrategy",
    "Watermark",
    "EMPTY_WATERMARK",
    "SyncStatus",
    "SyncState",
    "ChangeOperation",
    "SourceChange",
    "SyncStage",
    "FailurePolicy",
    "QuarantinedChange",
    "ChangeResult",
    "SyncRunStatus",
    "SyncOptions",
    "DEFAULT_SYNC_OPTIONS",
    "SyncRunSummary",
]
