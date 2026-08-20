"""Durable sync state: where extraction got to, per source system and entity.

A SEPARATE STORE FROM THE SCHEMA CATALOG (Step 33)
--------------------------------------------------
The Phase 2 catalog owns what a source LOOKS LIKE over time. This owns how far
through its DATA we have read. They are different lifecycles - republishing a
schema must never move a data checkpoint - so sync state lives in its own
``erp_sync`` PostgreSQL schema rather than being bolted onto catalog tables.

WHY NOT REUSE THE EXISTING BPI ``sync_state`` TABLE
---------------------------------------------------
The prototype's table is ``(source_table PK, last_synced_source_id BIGINT,
last_synced_at)``. It cannot express a timestamp watermark, cannot break ties
at an equal timestamp, has no per-entity granularity beyond a table name, and
records neither the schema version nor the mapping profile a checkpoint was
written against. Extending it in place would change a frozen prototype's
storage; Phase 10 gets its own generic table and leaves the prototype alone.

OPTIMISTIC CONCURRENCY (Steps 67, 68)
-------------------------------------
Every write asserts the ``version`` it read. Two runs cannot both advance the
same entity, because the second one's ``UPDATE ... WHERE version = :expected``
matches no row and raises. That is the whole concurrency mechanism - no lock
service, no lease daemon, and nothing that pretends to be atomic across two
databases.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from erp_pipeline.sync.errors import CheckpointConflictError
from erp_pipeline.sync.models import (
    EMPTY_WATERMARK,
    SyncState,
    SyncStatus,
    Watermark,
    WatermarkStrategy,
)

#: Dedicated namespace. Deliberately not ``erp_catalog``.
SYNC_SCHEMA_NAME = "erp_sync"
SYNC_STATE_TABLE = "sync_state"


@runtime_checkable
class SyncStateStore(Protocol):
    """Persistence for sync state."""

    def load(self, source_system_id: str, source_entity: str) -> SyncState | None:
        ...  # pragma: no cover - protocol declaration

    def save(self, state: SyncState, expected_version: int | None = None) -> SyncState:
        ...  # pragma: no cover - protocol declaration

    def list_states(self, source_system_id: str | None = None) -> tuple[SyncState, ...]:
        ...  # pragma: no cover - protocol declaration


class InMemorySyncStateStore:
    """Sync state held in a dictionary.

    The reference implementation of the concurrency contract, and what the
    tests run against. Enforces the same optimistic version check a real store
    must, so a test can prove conflict handling without a database.
    """

    def __init__(self, states: Iterable[SyncState] = ()) -> None:
        self._states: dict[str, SyncState] = {
            state.key: state for state in states
        }
        self.save_calls = 0

    def load(self, source_system_id: str, source_entity: str) -> SyncState | None:
        return self._states.get(f"{source_system_id}:{source_entity}")

    def save(
        self, state: SyncState, expected_version: int | None = None
    ) -> SyncState:
        self.save_calls += 1
        existing = self._states.get(state.key)

        if expected_version is not None:
            actual = existing.version if existing else 0
            if actual != expected_version:
                raise CheckpointConflictError(
                    f"Sync state for {state.key!r} changed concurrently: "
                    f"expected version {expected_version}, found {actual}. "
                    "Refusing to overwrite - two runs advancing one checkpoint "
                    "independently is how changes get skipped.",
                    expected_version=expected_version,
                    actual_version=actual,
                )

        self._states[state.key] = state
        return state

    def list_states(
        self, source_system_id: str | None = None
    ) -> tuple[SyncState, ...]:
        return tuple(
            sorted(
                (
                    state
                    for state in self._states.values()
                    if source_system_id is None
                    or state.source_system_id == source_system_id
                ),
                key=lambda item: item.key,
            )
        )

    def __len__(self) -> int:
        return len(self._states)


def ensure_state(
    store: SyncStateStore,
    source_system_id: str,
    source_entity: str,
    strategy: WatermarkStrategy,
    watermark_field: str | None = None,
    tie_break_field: str | None = None,
) -> SyncState:
    """Load existing state, or create a NEW one positioned at the beginning.

    Creating it here rather than in the coordinator keeps "what a fresh entity
    looks like" in one place: an empty watermark and status NEW, which every
    strategy interprets as "read from the start".
    """
    existing = store.load(source_system_id, source_entity)

    if existing is not None:
        return existing

    return SyncState(
        source_system_id=source_system_id,
        source_entity=source_entity,
        strategy=strategy,
        watermark=EMPTY_WATERMARK,
        watermark_field=watermark_field,
        tie_break_field=tie_break_field,
        status=SyncStatus.NEW,
        version=0,
    )


# ============================================================
# PostgreSQL-backed store
# ============================================================

CREATE_SYNC_SCHEMA_SQL = f"CREATE SCHEMA IF NOT EXISTS {SYNC_SCHEMA_NAME}"

CREATE_SYNC_STATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {SYNC_SCHEMA_NAME}.{SYNC_STATE_TABLE} (
    source_system_id              TEXT        NOT NULL,
    source_entity                 TEXT        NOT NULL,
    strategy                      TEXT        NOT NULL,
    watermark                     JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    watermark_field               TEXT        NULL,
    tie_break_field               TEXT        NULL,
    last_record_key               TEXT        NULL,
    schema_id                     TEXT        NULL,
    schema_hash                   TEXT        NULL,
    mapping_id                    TEXT        NULL,
    transformation_engine_version TEXT        NULL,
    last_run_id                   TEXT        NULL,
    status                        TEXT        NOT NULL,
    version                       INTEGER     NOT NULL DEFAULT 0,
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata                      JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    PRIMARY KEY (source_system_id, source_entity)
)
"""


def bootstrap_sync_schema(engine: Any) -> None:
    """Create the ``erp_sync`` namespace and its table if absent.

    Idempotent, and deliberately touches nothing outside its own schema.
    """
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(text(CREATE_SYNC_SCHEMA_SQL))
        connection.execute(text(CREATE_SYNC_STATE_SQL))


class PostgresSyncStateStore:
    """Sync state in PostgreSQL, with a real optimistic-concurrency check.

    The version guard is enforced by the database, not by the process: the
    ``UPDATE ... WHERE version = :expected`` either matches a row or does not,
    so two concurrent runs cannot both believe they won.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def load(self, source_system_id: str, source_entity: str) -> SyncState | None:
        from sqlalchemy import text

        query = text(
            f"""
            SELECT * FROM {SYNC_SCHEMA_NAME}.{SYNC_STATE_TABLE}
            WHERE source_system_id = :system AND source_entity = :entity
            """
        )

        with self._engine.connect() as connection:
            row = connection.execute(
                query, {"system": source_system_id, "entity": source_entity}
            ).mappings().first()

        return _row_to_state(row) if row else None

    def save(
        self, state: SyncState, expected_version: int | None = None
    ) -> SyncState:
        from sqlalchemy import text

        payload = {
            "system": state.source_system_id,
            "entity": state.source_entity,
            "strategy": state.strategy.value,
            "watermark": json.dumps(state.watermark.to_dict()),
            "watermark_field": state.watermark_field,
            "tie_break_field": state.tie_break_field,
            "last_record_key": state.last_record_key,
            "schema_id": state.schema_id,
            "schema_hash": state.schema_hash,
            "mapping_id": state.mapping_id,
            "engine_version": state.transformation_engine_version,
            "last_run_id": state.last_run_id,
            "status": state.status.value,
            "version": state.version,
            "updated_at": state.updated_at or datetime.now(timezone.utc),
            "metadata": json.dumps(dict(state.metadata)),
        }

        with self._engine.begin() as connection:
            if expected_version is not None:
                existing = connection.execute(
                    text(
                        f"""
                        SELECT version FROM {SYNC_SCHEMA_NAME}.{SYNC_STATE_TABLE}
                        WHERE source_system_id = :system
                          AND source_entity = :entity
                        FOR UPDATE
                        """
                    ),
                    {"system": state.source_system_id, "entity": state.source_entity},
                ).scalar()

                actual = existing if existing is not None else 0

                if actual != expected_version:
                    raise CheckpointConflictError(
                        f"Sync state for {state.key!r} changed concurrently: "
                        f"expected version {expected_version}, found {actual}.",
                        expected_version=expected_version,
                        actual_version=actual,
                    )

            connection.execute(
                text(
                    f"""
                    INSERT INTO {SYNC_SCHEMA_NAME}.{SYNC_STATE_TABLE} (
                        source_system_id, source_entity, strategy, watermark,
                        watermark_field, tie_break_field, last_record_key,
                        schema_id, schema_hash, mapping_id,
                        transformation_engine_version, last_run_id, status,
                        version, updated_at, metadata
                    ) VALUES (
                        :system, :entity, :strategy, CAST(:watermark AS JSONB),
                        :watermark_field, :tie_break_field, :last_record_key,
                        :schema_id, :schema_hash, :mapping_id,
                        :engine_version, :last_run_id, :status,
                        :version, :updated_at, CAST(:metadata AS JSONB)
                    )
                    ON CONFLICT (source_system_id, source_entity) DO UPDATE SET
                        strategy = EXCLUDED.strategy,
                        watermark = EXCLUDED.watermark,
                        watermark_field = EXCLUDED.watermark_field,
                        tie_break_field = EXCLUDED.tie_break_field,
                        last_record_key = EXCLUDED.last_record_key,
                        schema_id = EXCLUDED.schema_id,
                        schema_hash = EXCLUDED.schema_hash,
                        mapping_id = EXCLUDED.mapping_id,
                        transformation_engine_version =
                            EXCLUDED.transformation_engine_version,
                        last_run_id = EXCLUDED.last_run_id,
                        status = EXCLUDED.status,
                        version = EXCLUDED.version,
                        updated_at = EXCLUDED.updated_at,
                        metadata = EXCLUDED.metadata
                    """
                ),
                payload,
            )

        return state

    def list_states(
        self, source_system_id: str | None = None
    ) -> tuple[SyncState, ...]:
        from sqlalchemy import text

        if source_system_id is None:
            query = text(
                f"SELECT * FROM {SYNC_SCHEMA_NAME}.{SYNC_STATE_TABLE} "
                "ORDER BY source_system_id, source_entity"
            )
            params: dict[str, Any] = {}
        else:
            query = text(
                f"SELECT * FROM {SYNC_SCHEMA_NAME}.{SYNC_STATE_TABLE} "
                "WHERE source_system_id = :system "
                "ORDER BY source_entity"
            )
            params = {"system": source_system_id}

        with self._engine.connect() as connection:
            rows = connection.execute(query, params).mappings().all()

        return tuple(_row_to_state(row) for row in rows)


def _row_to_state(row: Mapping[str, Any]) -> SyncState:
    """Rebuild a ``SyncState`` from one persisted row."""
    watermark_payload = row["watermark"]
    if isinstance(watermark_payload, str):
        watermark_payload = json.loads(watermark_payload)

    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    return SyncState(
        source_system_id=row["source_system_id"],
        source_entity=row["source_entity"],
        strategy=WatermarkStrategy(row["strategy"]),
        watermark=Watermark.from_dict(watermark_payload),
        watermark_field=row["watermark_field"],
        tie_break_field=row["tie_break_field"],
        last_record_key=row["last_record_key"],
        schema_id=row["schema_id"],
        schema_hash=row["schema_hash"],
        mapping_id=row["mapping_id"],
        transformation_engine_version=row["transformation_engine_version"],
        last_run_id=row["last_run_id"],
        status=SyncStatus(row["status"]),
        version=int(row["version"]),
        updated_at=row["updated_at"],
        metadata=dict(metadata or {}),
    )


__all__ = [
    "SYNC_SCHEMA_NAME",
    "SYNC_STATE_TABLE",
    "SyncStateStore",
    "InMemorySyncStateStore",
    "PostgresSyncStateStore",
    "bootstrap_sync_schema",
    "ensure_state",
]
