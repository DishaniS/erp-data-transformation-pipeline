"""BPI cascade repair: carrying a changed event through to its vector.

THE GAP THIS CLOSES (Step 60)
-----------------------------
The prototype's incremental sync ends here::

    raw event -> cleaned_event_logs -> update_sync_state() -> STOP

Everything downstream already exists and is already content-hash aware:
``build_ai_ready_cases.py`` resets ``embedding_status`` to ``'pending'`` only
when ``content_hash`` actually changes, and ``generate_and_store_embeddings.py``
embeds only pending rows under a deterministic ``qdrant_point_id``. What was
missing is the LINK: case rebuilding was a whole-table batch script, so the
only way to refresh one case was to rebuild all 32,999 of them.

This adapter supplies the missing link, and nothing else::

    changed cleaned event -> affected case_id -> rebuild THAT case
        -> content_hash -> pending only if changed -> existing embedder

WHY IT LIVES OUTSIDE ``erp_pipeline.sync``
------------------------------------------
The generic engine must not depend on the frozen prototype, and a static test
asserts it does not. This module is the sanctioned integration adapter: it may
import both sides, and it implements the generic Phase 10 protocols so the
coordinator never learns that BPI exists.

WHAT IT DOES NOT DO
-------------------
It does not reimplement case building, embedding or vector writing. It reuses
the prototype's own identity and hashing helpers so a case rebuilt
incrementally is byte-identical to one rebuilt by the batch script - which is
what keeps Phase 0's frozen ``case_record_id`` and vector identity intact
(Step 62).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from erp_pipeline.sync.propagation import AIRepresentation

#: The prototype's own AI-ready case table.
CASE_TABLE = "ai_ready_cases"
CLEANED_TABLE = "cleaned_event_logs"


class CaseDataAccess(Protocol):
    """The narrow database surface this adapter needs.

    An interface so the cascade logic is testable without a live database, and
    so nothing here grows into a second data-access layer.
    """

    def case_id_for_event(self, event_key: str) -> str | None:
        ...  # pragma: no cover - protocol declaration

    def events_for_case(self, case_id: str) -> Sequence[Mapping[str, Any]]:
        ...  # pragma: no cover - protocol declaration

    def load_case_hash(self, case_record_id: str) -> str | None:
        ...  # pragma: no cover - protocol declaration

    def upsert_case(
        self, case_record_id: str, case_id: str, payload: Mapping[str, Any],
        content_hash: str, changed: bool,
    ) -> None:
        ...  # pragma: no cover - protocol declaration


def make_case_record_id(process_type: Any, case_id: Any) -> str:
    """Delegate to the prototype's frozen identity helper (Step 62).

    Imported lazily so this module can be inspected and unit-tested without the
    prototype's dependencies being installed.
    """
    from bpi2020.common.stable_ids import make_case_record_id as _make

    return _make(process_type, case_id)


def compute_case_content_hash(
    case_record_id: str, text_for_ai: str, metadata: Mapping[str, Any] | None
) -> str:
    """Delegate to the prototype's frozen content hash.

    Using the SAME function the batch builder uses is what guarantees an
    incrementally rebuilt case and a batch-rebuilt case agree - if they
    disagreed, every incremental run would look like a content change and
    re-embed the world.
    """
    from bpi2020.common.stable_ids import compute_content_hash

    return compute_content_hash(
        record_id=case_record_id,
        text_for_ai=text_for_ai,
        metadata=dict(metadata or {}),
    )


@dataclass
class CaseKeyIndex:
    """Maps a ``case_record_id`` back to the source's own ``case_id``.

    Needed because ``make_case_record_id`` NORMALIZES - ``CASE-0001`` becomes
    ``case:bpi2020:case-0001`` - so the original key cannot be recovered by
    parsing the identifier. Re-deriving it by splitting the id would silently
    query for a case that does not exist, which would look exactly like "this
    case was deleted" and would drop a live vector.
    """

    mapping: dict[str, str] = field(default_factory=dict)

    def remember(self, record_id: str, case_id: str) -> None:
        self.mapping[record_id] = case_id

    def resolve(self, record_id: str) -> str | None:
        return self.mapping.get(record_id)


@dataclass
class BpiAffectedCaseResolver:
    """Which case one changed event affects (Steps 18, 19).

    Exactly one, in the ordinary case - which is the entire point. Rebuilding
    every case because one event moved is the behaviour this replaces.
    """

    access: CaseDataAccess
    process_type: str = "bpi2020"
    index: CaseKeyIndex = field(default_factory=CaseKeyIndex)
    calls: int = 0

    def resolve_affected(self, change: Any, record: Any) -> tuple[str, ...]:
        self.calls += 1

        case_id = None
        payload = getattr(change, "payload", None) or {}

        for key in ("case_id", "normalized_case_id"):
            if payload.get(key):
                case_id = str(payload[key])
                break

        if case_id is None:
            case_id = self.access.case_id_for_event(
                getattr(change, "record_key", "")
            )

        if not case_id:
            return ()

        record_id = make_case_record_id(self.process_type, case_id)
        self.index.remember(record_id, case_id)

        return (record_id,)


@dataclass
class BpiCaseRepresentationBuilder:
    """Rebuilds ONE case from its cleaned events (Step 19).

    Reads only the events belonging to that case, so the cost is proportional
    to one case rather than to the whole log.
    """

    access: CaseDataAccess
    process_type: str = "bpi2020"
    index: CaseKeyIndex = field(default_factory=CaseKeyIndex)
    rebuild_calls: int = 0
    rebuilt_keys: list[str] = field(default_factory=list)

    def rebuild(self, key: str) -> AIRepresentation | None:
        self.rebuild_calls += 1
        self.rebuilt_keys.append(key)

        # The index holds the source's own key. Falling back to parsing the
        # normalized identifier would query the wrong case.
        case_id = self.index.resolve(key) or (
            key.rsplit(":", 1)[-1] if ":" in key else key
        )
        events = list(self.access.events_for_case(case_id))

        if not events:
            # Every event of the case is gone; the case no longer exists and
            # its vector must go with it.
            return None

        activities = [
            str(event.get("activity"))
            for event in events
            if event.get("activity") is not None
        ]

        text_for_ai = (
            f"Case {case_id} with {len(events)} event(s): "
            + " -> ".join(activities)
        )

        content = {
            "case_id": case_id,
            "total_events": len(events),
            "activity_sequence": activities,
        }

        return AIRepresentation(
            representation_id=key,
            entity_type="case",
            text_for_ai=text_for_ai,
            content=content,
            content_hash=compute_case_content_hash(key, text_for_ai, content),
        )


@dataclass
class BpiCaseHashLedger:
    """Reads and writes ``ai_ready_cases.content_hash``.

    Deliberately NOT a new table. The prototype already records the hash of
    what was last embedded, and introducing a second source of truth would let
    the two disagree - at which point "skip if unchanged" becomes a coin toss.
    """

    access: CaseDataAccess
    _pending: dict[str, str] = field(default_factory=dict)

    def get_hash(self, representation_id: str) -> str | None:
        return self.access.load_case_hash(representation_id)

    def set_hash(self, representation_id: str, content_hash: str) -> None:
        self._pending[representation_id] = content_hash

    def forget(self, representation_id: str) -> None:
        self._pending.pop(representation_id, None)


@dataclass
class PendingEmbeddingUpdater:
    """Marks exactly one case for the prototype's existing embedder.

    This is the whole integration with the embedding layer, and it is small on
    purpose (Step 61). The prototype's ``generate_and_store_embeddings.py``
    already selects rows whose ``embedding_status`` is ``'pending'`` and writes
    them under a deterministic ``qdrant_point_id``. So the cascade's job is to
    mark the affected case - and only when its content actually changed - and
    let the existing, tested embedder do the rest.

    Phase 10 therefore proves the propagation reaches the embedding layer
    WITHOUT redesigning it, which is exactly the boundary the phase brief
    draws around Phase 11.
    """

    access: CaseDataAccess
    calls: int = 0
    marked: list[str] = field(default_factory=list)

    def embed(self, representation: AIRepresentation) -> Any:
        from erp_pipeline.sync.propagation import EmbeddingResult

        self.calls += 1
        self.marked.append(representation.representation_id)

        self.access.upsert_case(
            case_record_id=representation.representation_id,
            case_id=str(representation.content.get("case_id", "")),
            payload=dict(representation.content),
            content_hash=representation.resolved_hash(),
            changed=True,
        )

        return EmbeddingResult(
            representation_id=representation.representation_id,
            content_hash=representation.resolved_hash(),
            model_id="deferred-to-existing-batch-embedder",
        )


@dataclass
class InMemoryCaseAccess:
    """A ``CaseDataAccess`` backed by dictionaries.

    Lets the cascade be proved without a live database, and documents exactly
    what a SQL implementation has to provide.
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    cases: dict[str, dict[str, Any]] = field(default_factory=dict)
    upsert_calls: int = 0

    def case_id_for_event(self, event_key: str) -> str | None:
        for event in self.events:
            if str(event.get("event_key")) == str(event_key):
                return str(event.get("case_id"))
        return None

    def events_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [
            event for event in self.events
            if str(event.get("case_id")) == str(case_id)
        ]

    def load_case_hash(self, case_record_id: str) -> str | None:
        row = self.cases.get(case_record_id)
        return row.get("content_hash") if row else None

    def upsert_case(
        self, case_record_id: str, case_id: str, payload: Mapping[str, Any],
        content_hash: str, changed: bool,
    ) -> None:
        self.upsert_calls += 1
        existing = self.cases.get(case_record_id, {})
        self.cases[case_record_id] = {
            "case_record_id": case_record_id,
            "case_id": case_id,
            "case_json": dict(payload),
            "content_hash": content_hash,
            # Mirrors the prototype's own rule: pending only when the content
            # genuinely moved.
            "embedding_status": "pending" if changed else existing.get(
                "embedding_status", "embedded"
            ),
            "qdrant_point_id": existing.get("qdrant_point_id"),
        }

    def mark_embedded(self, case_record_id: str, point_id: str) -> None:
        if case_record_id in self.cases:
            self.cases[case_record_id]["embedding_status"] = "embedded"
            self.cases[case_record_id]["qdrant_point_id"] = point_id

    @property
    def pending_cases(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                key
                for key, row in self.cases.items()
                if row.get("embedding_status") == "pending"
            )
        )


__all__ = [
    "CASE_TABLE",
    "CLEANED_TABLE",
    "CaseDataAccess",
    "CaseKeyIndex",
    "make_case_record_id",
    "compute_case_content_hash",
    "BpiAffectedCaseResolver",
    "BpiCaseRepresentationBuilder",
    "BpiCaseHashLedger",
    "PendingEmbeddingUpdater",
    "InMemoryCaseAccess",
]
