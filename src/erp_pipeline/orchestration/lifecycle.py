"""Which version of a thing is the CURRENT one (Phase 9).

THE PROBLEM
-----------
Phase 3 identifies a document by its CONTENT and its ERP attachment. That is
right, and it creates a lifecycle gap:

    EMP002.birth_certificate = bytes A   ->  representation ai:document:…A
    ERP replaces it with     = bytes B   ->  representation ai:document:…B

Different bytes, so a different ``document_id``, so a different
``representation_id``, so a different vector. Nothing overwrites anything, and
BOTH remain searchable. A query for EMP002's certificate returns the superseded
one alongside the real one, and nothing marks which is which.

Structured records do not have this problem: their representation id derives
from the canonical record id, so an update upserts in place. Documents, remote
assets and schema chunks do, because their identity includes their content.

THE LOGICAL SLOT
----------------
What changed is not the document - it is what occupies a SLOT in the ERP:

    record:erp:legacy_hr:employees:emp002
    attachment:erp:legacy_hr:employees:emp002|birth_certificate
    schema:legacy_hr.public.employees

A slot holds one current SET of representations - a contract is several chunks -
and re-indexing replaces the set rather than adding to it.

WHY THE REGISTRY, NOT NEW IDS
-----------------------------
Representation ids are already load-bearing in Qdrant, the representation store,
search results, evaluations and tests. Redefining them to carry lifecycle would
churn all of that to express something orthogonal. So identity stays exactly as
implemented and lifecycle is recorded ALONGSIDE it.

WHY `is_current` IS ALSO ON THE STORAGE STATE
---------------------------------------------
Deleting a superseded vector can fail - Qdrant may be briefly unreachable. If
"current" lived only in this registry, a failed delete would leave the old
vector answering queries until someone noticed.

``StorageRecordMetadata.is_current`` is therefore the backstop, checked in
``HybridVectorStore._merge`` on the same terms filters already are: PostgreSQL
is authoritative about what is current, and physical cleanup is allowed to lag
without ever making a stale answer visible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

LIFECYCLE_SCHEMA_NAME = "erp_runtime"
LIFECYCLE_TABLE = "representation_lifecycle"

#: The three slot kinds that exist. A representation whose kind cannot be
#: determined gets no lifecycle row rather than a guessed one - an unmanaged
#: representation behaves exactly as it did before Phase 9.
SLOT_RECORD = "record"
SLOT_ATTACHMENT = "attachment"
SLOT_SCHEMA = "schema"

#: Separates the parts of a slot key. Matches the attachment separator Phase 3
#: already uses, so the two read alike.
SLOT_SEPARATOR = "|"


def _validate_schema(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        raise ValueError(f"{schema!r} is not a valid PostgreSQL schema name")

    return schema


def logical_key_for(representation: Any) -> str | None:
    """The ERP slot this representation occupies, or ``None`` if it has none.

    Derived entirely from metadata the earlier phases already record - no new
    identity is invented, and nothing employee-specific appears anywhere.

    Returns ``None`` for a representation with no determinable slot, most
    notably an ANONYMOUS uploaded document. Two unrelated PDFs uploaded with no
    ERP association are not versions of each other, and guessing that they are
    would make one of them silently disappear.
    """
    metadata: Mapping[str, Any] = getattr(representation, "metadata", None) or {}
    content_kind = metadata.get("content_kind")

    if content_kind == "schema":
        entity_id = metadata.get("entity_id")

        return f"{SLOT_SCHEMA}:{entity_id}" if entity_id else None

    if content_kind == "document_chunk":
        # The slot is WHERE the document hangs, plus WHICH document it is for
        # that parent. Updating a birth certificate must not disturb the
        # employment contract beside it.
        parent = metadata.get("parent_record_id")
        role = metadata.get("source_field") or metadata.get("document_type")

        if parent and role:
            return f"{SLOT_ATTACHMENT}:{parent}{SLOT_SEPARATOR}{role}"

        # An upload with no parent record but a declared business identity
        # still names a slot: the same employee's same document type.
        key_name = metadata.get("business_key_name")
        key_value = metadata.get("business_key_value")

        if key_name and key_value and role:
            return (
                f"{SLOT_ATTACHMENT}:{key_name}={key_value}"
                f"{SLOT_SEPARATOR}{role}"
            )

        # Anonymous document: no slot, no lifecycle management.
        return None

    canonical = metadata.get("canonical_record_id")

    if canonical:
        return f"{SLOT_RECORD}:{canonical}"

    return None


def content_generation(representations: Sequence[Any]) -> str:
    """A digest identifying THIS version of a slot's contents.

    Built from the representation ids in sorted order, so re-running an
    unchanged source produces the same generation and the registry can tell
    "nothing changed" from "replaced with something identical-looking".
    """
    import hashlib

    ids = sorted(
        getattr(item, "representation_id", "") for item in representations
    )

    return hashlib.sha256(SLOT_SEPARATOR.join(ids).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LifecycleEntry:
    """One representation's place in a slot's history."""

    logical_key: str
    representation_id: str
    generation: str
    is_current: bool = True
    created_at: datetime | None = None
    superseded_at: datetime | None = None
    superseded_by: str | None = None
    sync_run_id: str | None = None
    #: Set when the vector was superseded but its physical removal has not yet
    #: succeeded. Reconciliation retries these; search already excludes them.
    cleanup_pending: bool = False


@dataclass(frozen=True)
class ReplacementResult:
    """What one slot replacement did."""

    logical_key: str
    generation: str
    promoted: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    unchanged: bool = False


def create_lifecycle_sql(schema: str = LIFECYCLE_SCHEMA_NAME) -> str:
    """The registry table.

    Holds identity and lifecycle state only - never representation text. The
    text lives once, in ``ai_representations``, and copying it here would create
    a second thing to keep consistent.
    """
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{LIFECYCLE_TABLE} (
    logical_key       TEXT        NOT NULL,
    representation_id TEXT        NOT NULL,
    generation        TEXT        NOT NULL,
    is_current        BOOLEAN     NOT NULL DEFAULT TRUE,
    cleanup_pending   BOOLEAN     NOT NULL DEFAULT FALSE,
    superseded_by     TEXT        NULL,
    sync_run_id       TEXT        NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_at     TIMESTAMPTZ NULL,
    PRIMARY KEY (logical_key, representation_id)
)
"""


def create_lifecycle_index_sql(schema: str = LIFECYCLE_SCHEMA_NAME) -> str:
    """Index the two questions asked of this table.

    "What is current for this slot?" on every replacement, and "what still
    needs cleaning up?" on every reconciliation pass.
    """
    validated = _validate_schema(schema)

    return (
        f"CREATE INDEX IF NOT EXISTS {LIFECYCLE_TABLE}_current_idx "
        f"ON {validated}.{LIFECYCLE_TABLE} (logical_key, is_current)"
    )


def create_cleanup_index_sql(schema: str = LIFECYCLE_SCHEMA_NAME) -> str:
    validated = _validate_schema(schema)

    return (
        f"CREATE INDEX IF NOT EXISTS {LIFECYCLE_TABLE}_cleanup_idx "
        f"ON {validated}.{LIFECYCLE_TABLE} (cleanup_pending)"
    )


def bootstrap_lifecycle_schema(
    engine: Any, schema: str = LIFECYCLE_SCHEMA_NAME
) -> None:
    """Create the registry. Idempotent, additive, no destructive reset."""
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {_validate_schema(schema)}")
        )
        connection.execute(text(create_lifecycle_sql(schema)))
        connection.execute(text(create_lifecycle_index_sql(schema)))
        connection.execute(text(create_cleanup_index_sql(schema)))


class InMemoryLifecycleRegistry:
    """The reference semantics a durable registry must match."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], LifecycleEntry] = {}

    # -- reads --

    def current_ids(self, logical_key: str) -> tuple[str, ...]:
        return tuple(
            entry.representation_id
            for entry in self._entries.values()
            if entry.logical_key == logical_key and entry.is_current
        )

    def current_generation(self, logical_key: str) -> str | None:
        for entry in self._entries.values():
            if entry.logical_key == logical_key and entry.is_current:
                return entry.generation

        return None

    def is_current(self, representation_id: str) -> bool | None:
        """``None`` when this representation has no lifecycle row at all."""
        found = [
            entry for entry in self._entries.values()
            if entry.representation_id == representation_id
        ]

        if not found:
            return None

        return any(entry.is_current for entry in found)

    def pending_cleanup(self, limit: int = 100) -> tuple[LifecycleEntry, ...]:
        return tuple(
            entry for entry in self._entries.values() if entry.cleanup_pending
        )[:limit]

    def entries_for(self, logical_key: str) -> tuple[LifecycleEntry, ...]:
        return tuple(
            entry for entry in self._entries.values()
            if entry.logical_key == logical_key
        )

    def count(self) -> int:
        return len(self._entries)

    # -- writes --

    def replace_current(
        self,
        logical_key: str,
        representation_ids: Sequence[str],
        generation: str,
        sync_run_id: str | None = None,
        now: datetime | None = None,
    ) -> ReplacementResult:
        """Make this set current and supersede whatever it replaces.

        Called only AFTER the new set is persisted, embedded and stored, so a
        failure earlier in the pipeline leaves the previous version current and
        searchable rather than removing it and having nothing.
        """
        moment = now or datetime.now(timezone.utc)
        incoming = set(representation_ids)
        existing_current = set(self.current_ids(logical_key))

        if incoming == existing_current and existing_current:
            # Same slot, same set. Re-running an unchanged source must not
            # churn the registry or mark anything for cleanup.
            return ReplacementResult(
                logical_key=logical_key, generation=generation, unchanged=True
            )

        superseded = sorted(existing_current - incoming)

        for representation_id in superseded:
            key = (logical_key, representation_id)
            previous = self._entries[key]
            self._entries[key] = LifecycleEntry(
                logical_key=logical_key,
                representation_id=representation_id,
                generation=previous.generation,
                is_current=False,
                created_at=previous.created_at,
                superseded_at=moment,
                superseded_by=generation,
                sync_run_id=previous.sync_run_id,
                cleanup_pending=True,
            )

        for representation_id in sorted(incoming):
            key = (logical_key, representation_id)
            previous = self._entries.get(key)
            self._entries[key] = LifecycleEntry(
                logical_key=logical_key,
                representation_id=representation_id,
                generation=generation,
                is_current=True,
                created_at=previous.created_at if previous else moment,
                sync_run_id=sync_run_id,
                cleanup_pending=False,
            )

        return ReplacementResult(
            logical_key=logical_key,
            generation=generation,
            promoted=tuple(sorted(incoming)),
            superseded=tuple(superseded),
        )

    def retire_slot(
        self, logical_key: str, now: datetime | None = None
    ) -> tuple[str, ...]:
        """Supersede everything in a slot, for a deleted ERP record."""
        moment = now or datetime.now(timezone.utc)
        retired = self.current_ids(logical_key)

        for representation_id in retired:
            key = (logical_key, representation_id)
            previous = self._entries[key]
            self._entries[key] = LifecycleEntry(
                logical_key=logical_key,
                representation_id=representation_id,
                generation=previous.generation,
                is_current=False,
                created_at=previous.created_at,
                superseded_at=moment,
                superseded_by=None,
                sync_run_id=previous.sync_run_id,
                cleanup_pending=True,
            )

        return retired

    def mark_cleaned(self, logical_key: str, representation_id: str) -> bool:
        key = (logical_key, representation_id)
        entry = self._entries.get(key)

        if entry is None or not entry.cleanup_pending:
            return False

        self._entries[key] = LifecycleEntry(
            logical_key=entry.logical_key,
            representation_id=entry.representation_id,
            generation=entry.generation,
            is_current=False,
            created_at=entry.created_at,
            superseded_at=entry.superseded_at,
            superseded_by=entry.superseded_by,
            sync_run_id=entry.sync_run_id,
            cleanup_pending=False,
        )

        return True


def group_by_slot(
    representations: Iterable[Any],
) -> dict[str, list[Any]]:
    """Representations bucketed by the ERP slot they occupy.

    Anything with no determinable slot is left out entirely - it keeps working
    exactly as it did before Phase 9, which is the right answer for an
    anonymous uploaded document.
    """
    grouped: dict[str, list[Any]] = {}

    for representation in representations:
        key = logical_key_for(representation)

        if key is None:
            continue

        grouped.setdefault(key, []).append(representation)

    return grouped


__all__ = [
    "LIFECYCLE_SCHEMA_NAME",
    "LIFECYCLE_TABLE",
    "SLOT_ATTACHMENT",
    "SLOT_RECORD",
    "SLOT_SCHEMA",
    "InMemoryLifecycleRegistry",
    "LifecycleEntry",
    "ReplacementResult",
    "bootstrap_lifecycle_schema",
    "content_generation",
    "create_lifecycle_sql",
    "group_by_slot",
    "logical_key_for",
]
