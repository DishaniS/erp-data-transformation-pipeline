"""Authoritative, durable tier state, access statistics and transition audit.

WHY THIS IS THE AUTHORITY (Step 49)
-----------------------------------
Three systems can each hold a copy of a vector - two Qdrant collections and a
directory of archives - and none of them can answer "where does this vector
OFFICIALLY live?". Only this store can. Everything else is a cache of it.

That matters most when a migration is interrupted: a vector may briefly exist
in two tiers, and the only way to know which one counts is to ask the state
store. Keeping tier assignment in a Python dictionary would lose that answer at
the first restart.

ITS OWN NAMESPACE
-----------------
``erp_vector_storage``, not Phase 2's ``erp_catalog`` and not Phase 10's
``erp_sync``. The catalog stores what a source looks like; sync stores how far
through its data we have read; this stores where the resulting vectors live.
Three lifecycles, three schemas.

OPTIMISTIC CONCURRENCY (Step 3)
-------------------------------
Every write asserts the ``state_version`` it read. Two migration workers cannot
both move one vector, because the second one's ``UPDATE ... WHERE
state_version = :expected`` matches no row and raises. That is the whole
mechanism - no lock service, no lease daemon.
"""

from __future__ import annotations

import re

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.errors import (
    ConcurrencyConflictError,
    StorageConfigurationError,
)
from erp_pipeline.storage.models import (
    BusinessCriticality,
    LatencyRequirement,
    StorageRecordMetadata,
    StorageTier,
    TierTransition,
    TransitionReason,
)

#: Dedicated namespace. Deliberately not erp_catalog and not erp_sync.
STORAGE_SCHEMA_NAME = "erp_vector_storage"
STATE_TABLE = "vector_storage_state"
TRANSITIONS_TABLE = "vector_tier_transitions"
ACCESS_TABLE = "vector_access_stats"


@runtime_checkable
class TierStateStore(Protocol):
    """Persistence for tier state, access statistics and transition audit."""

    def load(self, representation_id: str) -> StorageRecordMetadata | None:
        ...  # pragma: no cover - protocol declaration

    def save(
        self,
        metadata: StorageRecordMetadata,
        expected_version: int | None = None,
    ) -> StorageRecordMetadata:
        ...  # pragma: no cover - protocol declaration

    def delete(self, representation_id: str) -> bool:
        ...  # pragma: no cover - protocol declaration

    def list_all(
        self, tier: StorageTier | None = None
    ) -> tuple[StorageRecordMetadata, ...]:
        ...  # pragma: no cover - protocol declaration

    def record_access(self, representation_id: str) -> StorageRecordMetadata | None:
        ...  # pragma: no cover - protocol declaration

    def record_transition(self, transition: TierTransition) -> None:
        ...  # pragma: no cover - protocol declaration

    def transitions_for(
        self, representation_id: str
    ) -> tuple[TierTransition, ...]:
        ...  # pragma: no cover - protocol declaration


# ============================================================
# In-memory implementation
# ============================================================

class InMemoryTierStateStore:
    """Tier state in dictionaries.

    Not a toy: it enforces the SAME optimistic-version contract a SQL store
    must, so concurrency behaviour can be proved without a database.
    """

    def __init__(self) -> None:
        self._records: dict[str, StorageRecordMetadata] = {}
        self._transitions: list[TierTransition] = []
        self.save_calls = 0

    def load(self, representation_id: str) -> StorageRecordMetadata | None:
        return self._records.get(representation_id)

    def save(
        self,
        metadata: StorageRecordMetadata,
        expected_version: int | None = None,
    ) -> StorageRecordMetadata:
        self.save_calls += 1
        existing = self._records.get(metadata.representation_id)

        if expected_version is not None:
            actual = existing.version if existing else 0
            if actual != expected_version:
                raise ConcurrencyConflictError(
                    f"tier state for {metadata.representation_id!r} changed "
                    f"concurrently: expected version {expected_version}, found "
                    f"{actual}. Refusing to overwrite - two workers migrating "
                    "one vector is how a vector ends up in two tiers or none.",
                    expected_version=expected_version,
                    actual_version=actual,
                )

        self._records[metadata.representation_id] = metadata
        return metadata

    def delete(self, representation_id: str) -> bool:
        return self._records.pop(representation_id, None) is not None

    def list_all(
        self, tier: StorageTier | None = None
    ) -> tuple[StorageRecordMetadata, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._records.values()
                    if tier is None or item.current_tier is tier
                ),
                key=lambda item: item.representation_id,
            )
        )

    def record_access(self, representation_id: str) -> StorageRecordMetadata | None:
        existing = self._records.get(representation_id)

        if existing is None:
            return None

        updated = existing.with_access()
        self._records[representation_id] = updated

        return updated

    def record_transition(self, transition: TierTransition) -> None:
        self._transitions.append(transition)

    def transitions_for(
        self, representation_id: str
    ) -> tuple[TierTransition, ...]:
        return tuple(
            item
            for item in self._transitions
            if item.representation_id == representation_id
        )

    @property
    def all_transitions(self) -> tuple[TierTransition, ...]:
        return tuple(self._transitions)

    def __len__(self) -> int:
        return len(self._records)


# ============================================================
# PostgreSQL implementation (Steps 2, 50)
# ============================================================

def _validate_schema(schema: str) -> str:
    """Reject anything that is not a plain identifier.

    The schema name is interpolated into DDL, which a bound parameter cannot
    carry. Validating here means a caller cannot smuggle SQL through it.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        raise StorageConfigurationError(
            f"{schema!r} is not a valid PostgreSQL schema identifier"
        )

    return schema


def create_schema_sql(schema: str = STORAGE_SCHEMA_NAME) -> str:
    return f"CREATE SCHEMA IF NOT EXISTS {_validate_schema(schema)}"


CREATE_SCHEMA_SQL = create_schema_sql()

def create_state_sql(schema: str = STORAGE_SCHEMA_NAME) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{STATE_TABLE} (
    representation_id     TEXT        PRIMARY KEY,
    embedding_id          TEXT        NOT NULL,
    vector_id             TEXT        NOT NULL,
    current_tier          TEXT        NOT NULL,
    content_hash          TEXT        NOT NULL,
    model_id              TEXT        NOT NULL,
    dimension             INTEGER     NOT NULL,
    sensitivity           TEXT        NOT NULL,
    business_criticality  TEXT        NOT NULL,
    latency_requirement   TEXT        NOT NULL,
    entity_type           TEXT        NULL,
    access_count          BIGINT      NOT NULL DEFAULT 0,
    recent_access_count   BIGINT      NOT NULL DEFAULT 0,
    last_accessed_at      TIMESTAMPTZ NULL,
    created_at            TIMESTAMPTZ NULL,
    content_updated_at    TIMESTAMPTZ NULL,
    retention_until       TIMESTAMPTZ NULL,
    legal_hold            BOOLEAN     NOT NULL DEFAULT FALSE,
    tier_since            TIMESTAMPTZ NULL,
    policy_id             TEXT        NULL,
    policy_version        TEXT        NULL,
    state_version         INTEGER     NOT NULL DEFAULT 0,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


CREATE_STATE_SQL = create_state_sql()

def create_transitions_sql(schema: str = STORAGE_SCHEMA_NAME) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{TRANSITIONS_TABLE} (
    transition_id     TEXT        PRIMARY KEY,
    representation_id TEXT        NOT NULL,
    vector_id         TEXT        NOT NULL,
    from_tier         TEXT        NULL,
    to_tier           TEXT        NOT NULL,
    reason            TEXT        NOT NULL,
    policy_id         TEXT        NOT NULL,
    policy_version    TEXT        NOT NULL,
    succeeded         BOOLEAN     NOT NULL,
    forced            BOOLEAN     NOT NULL DEFAULT FALSE,
    occurred_at       TIMESTAMPTZ NOT NULL,
    detail            TEXT        NULL,
    duration_seconds  DOUBLE PRECISION NULL,
    bytes_written     BIGINT      NULL
)
"""


CREATE_TRANSITIONS_SQL = create_transitions_sql()

def create_transitions_index_sql(schema: str = STORAGE_SCHEMA_NAME) -> str:
    return (
        f"CREATE INDEX IF NOT EXISTS idx_{TRANSITIONS_TABLE}_representation "
        f"ON {_validate_schema(schema)}.{TRANSITIONS_TABLE} (representation_id)"
    )


CREATE_TRANSITIONS_INDEX_SQL = create_transitions_index_sql()

#: Access statistics kept as an append-light counter table so a future phase can
#: window them without rewriting the state row on every read.
def create_access_sql(schema: str = STORAGE_SCHEMA_NAME) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{ACCESS_TABLE} (
    representation_id TEXT        PRIMARY KEY,
    access_count      BIGINT      NOT NULL DEFAULT 0,
    recent_count      BIGINT      NOT NULL DEFAULT 0,
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_accessed_at  TIMESTAMPTZ NULL
)
"""


CREATE_ACCESS_SQL = create_access_sql()


def bootstrap_storage_schema(
    engine: Any, schema: str = STORAGE_SCHEMA_NAME
) -> None:
    """Create the namespace and its tables. Idempotent, and scoped to itself.

    ``schema`` exists so a test can bootstrap an isolated namespace instead of
    creating production tables as a side effect of being verified.
    """
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(text(create_schema_sql(schema)))
        connection.execute(text(create_state_sql(schema)))
        connection.execute(text(create_transitions_sql(schema)))
        connection.execute(text(create_transitions_index_sql(schema)))
        connection.execute(text(create_access_sql(schema)))


class PostgresTierStateStore:
    """Durable tier state in PostgreSQL, with a real version guard."""

    def __init__(self, engine: Any, schema: str = STORAGE_SCHEMA_NAME) -> None:
        self._engine = engine
        self._schema = _validate_schema(schema)

    @property
    def schema(self) -> str:
        return self._schema

    # -- reads --

    def load(self, representation_id: str) -> StorageRecordMetadata | None:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{STATE_TABLE} "
                    "WHERE representation_id = :rid"
                ),
                {"rid": representation_id},
            ).mappings().first()

        return _row_to_metadata(row) if row else None

    def list_all(
        self, tier: StorageTier | None = None
    ) -> tuple[StorageRecordMetadata, ...]:
        from sqlalchemy import text

        if tier is None:
            query = text(
                f"SELECT * FROM {self._schema}.{STATE_TABLE} "
                "ORDER BY representation_id"
            )
            params: dict[str, Any] = {}
        else:
            query = text(
                f"SELECT * FROM {self._schema}.{STATE_TABLE} "
                "WHERE current_tier = :tier ORDER BY representation_id"
            )
            params = {"tier": tier.value}

        with self._engine.connect() as connection:
            rows = connection.execute(query, params).mappings().all()

        return tuple(_row_to_metadata(row) for row in rows)

    # -- writes --

    def save(
        self,
        metadata: StorageRecordMetadata,
        expected_version: int | None = None,
    ) -> StorageRecordMetadata:
        from sqlalchemy import text

        payload = {
            "rid": metadata.representation_id,
            "embedding_id": metadata.embedding_id,
            "vector_id": metadata.vector_id,
            "current_tier": metadata.current_tier.value,
            "content_hash": metadata.content_hash,
            "model_id": metadata.model_id,
            "dimension": metadata.dimension,
            "sensitivity": metadata.sensitivity.value,
            "business_criticality": metadata.business_criticality.value,
            "latency_requirement": metadata.latency_requirement.value,
            "entity_type": metadata.entity_type,
            "access_count": metadata.access_count,
            "recent_access_count": metadata.recent_access_count,
            "last_accessed_at": metadata.last_accessed_at,
            "created_at": metadata.created_at,
            "content_updated_at": metadata.content_updated_at,
            "retention_until": metadata.retention_until,
            "legal_hold": metadata.legal_hold,
            "tier_since": metadata.tier_since,
            "policy_id": metadata.policy_id,
            "policy_version": metadata.policy_version,
            "state_version": metadata.version,
            "updated_at": metadata.updated_at or datetime.now(timezone.utc),
        }

        with self._engine.begin() as connection:
            if expected_version is not None:
                current = connection.execute(
                    text(
                        f"SELECT state_version FROM "
                        f"{self._schema}.{STATE_TABLE} "
                        "WHERE representation_id = :rid FOR UPDATE"
                    ),
                    {"rid": metadata.representation_id},
                ).scalar()

                actual = current if current is not None else 0

                if actual != expected_version:
                    raise ConcurrencyConflictError(
                        f"tier state for {metadata.representation_id!r} changed "
                        f"concurrently: expected version {expected_version}, "
                        f"found {actual}.",
                        expected_version=expected_version,
                        actual_version=actual,
                    )

            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{STATE_TABLE} (
                        representation_id, embedding_id, vector_id,
                        current_tier, content_hash, model_id, dimension,
                        sensitivity, business_criticality, latency_requirement,
                        entity_type, access_count, recent_access_count,
                        last_accessed_at, created_at, content_updated_at,
                        retention_until, legal_hold, tier_since,
                        policy_id, policy_version, state_version, updated_at
                    ) VALUES (
                        :rid, :embedding_id, :vector_id,
                        :current_tier, :content_hash, :model_id, :dimension,
                        :sensitivity, :business_criticality, :latency_requirement,
                        :entity_type, :access_count, :recent_access_count,
                        :last_accessed_at, :created_at, :content_updated_at,
                        :retention_until, :legal_hold, :tier_since,
                        :policy_id, :policy_version, :state_version, :updated_at
                    )
                    ON CONFLICT (representation_id) DO UPDATE SET
                        embedding_id = EXCLUDED.embedding_id,
                        vector_id = EXCLUDED.vector_id,
                        current_tier = EXCLUDED.current_tier,
                        content_hash = EXCLUDED.content_hash,
                        model_id = EXCLUDED.model_id,
                        dimension = EXCLUDED.dimension,
                        sensitivity = EXCLUDED.sensitivity,
                        business_criticality = EXCLUDED.business_criticality,
                        latency_requirement = EXCLUDED.latency_requirement,
                        entity_type = EXCLUDED.entity_type,
                        access_count = EXCLUDED.access_count,
                        recent_access_count = EXCLUDED.recent_access_count,
                        last_accessed_at = EXCLUDED.last_accessed_at,
                        created_at = EXCLUDED.created_at,
                        content_updated_at = EXCLUDED.content_updated_at,
                        retention_until = EXCLUDED.retention_until,
                        legal_hold = EXCLUDED.legal_hold,
                        tier_since = EXCLUDED.tier_since,
                        policy_id = EXCLUDED.policy_id,
                        policy_version = EXCLUDED.policy_version,
                        state_version = EXCLUDED.state_version,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                payload,
            )

        return metadata

    def delete(self, representation_id: str) -> bool:
        from sqlalchemy import text

        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    f"DELETE FROM {self._schema}.{STATE_TABLE} "
                    "WHERE representation_id = :rid"
                ),
                {"rid": representation_id},
            )

        return bool(result.rowcount)

    # -- access statistics (Step 4) --

    def record_access(self, representation_id: str) -> StorageRecordMetadata | None:
        from sqlalchemy import text

        moment = datetime.now(timezone.utc)

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{ACCESS_TABLE} (
                        representation_id, access_count, recent_count,
                        last_accessed_at
                    ) VALUES (:rid, 1, 1, :at)
                    ON CONFLICT (representation_id) DO UPDATE SET
                        access_count = {ACCESS_TABLE}.access_count + 1,
                        recent_count = {ACCESS_TABLE}.recent_count + 1,
                        last_accessed_at = EXCLUDED.last_accessed_at
                    """
                ),
                {"rid": representation_id, "at": moment},
            )

            # The state row carries the counters the router reads, so it is
            # updated too - WITHOUT a version guard, because recording a read
            # must never lose a race against a concurrent migration.
            connection.execute(
                text(
                    f"""
                    UPDATE {self._schema}.{STATE_TABLE}
                    SET access_count = access_count + 1,
                        recent_access_count = recent_access_count + 1,
                        last_accessed_at = :at
                    WHERE representation_id = :rid
                    """
                ),
                {"rid": representation_id, "at": moment},
            )

        return self.load(representation_id)

    # -- transition audit (Step 11) --

    def record_transition(self, transition: TierTransition) -> None:
        from sqlalchemy import text

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{TRANSITIONS_TABLE} (
                        transition_id, representation_id, vector_id,
                        from_tier, to_tier, reason, policy_id, policy_version,
                        succeeded, forced, occurred_at, detail,
                        duration_seconds, bytes_written
                    ) VALUES (
                        :transition_id, :rid, :vector_id,
                        :from_tier, :to_tier, :reason, :policy_id, :policy_version,
                        :succeeded, :forced, :occurred_at, :detail,
                        :duration_seconds, :bytes_written
                    )
                    ON CONFLICT (transition_id) DO NOTHING
                    """
                ),
                {
                    "transition_id": transition.transition_id,
                    "rid": transition.representation_id,
                    "vector_id": transition.vector_id,
                    "from_tier": (
                        transition.from_tier.value if transition.from_tier else None
                    ),
                    "to_tier": transition.to_tier.value,
                    "reason": transition.reason.value,
                    "policy_id": transition.policy_id,
                    "policy_version": transition.policy_version,
                    "succeeded": transition.succeeded,
                    "forced": transition.forced,
                    "occurred_at": transition.occurred_at,
                    "detail": transition.detail,
                    "duration_seconds": transition.duration_seconds,
                    "bytes_written": transition.bytes_written,
                },
            )

    def transitions_for(
        self, representation_id: str
    ) -> tuple[TierTransition, ...]:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{TRANSITIONS_TABLE} "
                    "WHERE representation_id = :rid ORDER BY occurred_at"
                ),
                {"rid": representation_id},
            ).mappings().all()

        return tuple(_row_to_transition(row) for row in rows)


def _row_to_metadata(row: Mapping[str, Any]) -> StorageRecordMetadata:
    return StorageRecordMetadata(
        representation_id=row["representation_id"],
        embedding_id=row["embedding_id"],
        vector_id=row["vector_id"],
        current_tier=StorageTier(row["current_tier"]),
        content_hash=row["content_hash"],
        model_id=row["model_id"],
        dimension=int(row["dimension"]),
        sensitivity=SensitivityLevel(row["sensitivity"]),
        business_criticality=BusinessCriticality(row["business_criticality"]),
        latency_requirement=LatencyRequirement(row["latency_requirement"]),
        entity_type=row["entity_type"],
        access_count=int(row["access_count"]),
        recent_access_count=int(row["recent_access_count"]),
        last_accessed_at=row["last_accessed_at"],
        created_at=row["created_at"],
        content_updated_at=row["content_updated_at"],
        retention_until=row["retention_until"],
        legal_hold=bool(row["legal_hold"]),
        tier_since=row["tier_since"],
        policy_id=row["policy_id"],
        policy_version=row["policy_version"],
        version=int(row["state_version"]),
        updated_at=row["updated_at"],
    )


def _row_to_transition(row: Mapping[str, Any]) -> TierTransition:
    return TierTransition(
        transition_id=row["transition_id"],
        representation_id=row["representation_id"],
        vector_id=row["vector_id"],
        from_tier=StorageTier(row["from_tier"]) if row["from_tier"] else None,
        to_tier=StorageTier(row["to_tier"]),
        reason=TransitionReason(row["reason"]),
        policy_id=row["policy_id"],
        policy_version=row["policy_version"],
        succeeded=bool(row["succeeded"]),
        forced=bool(row["forced"]),
        occurred_at=row["occurred_at"],
        detail=row["detail"],
        duration_seconds=row["duration_seconds"],
        bytes_written=row["bytes_written"],
    )


__all__ = [
    "STORAGE_SCHEMA_NAME",
    "STATE_TABLE",
    "TRANSITIONS_TABLE",
    "ACCESS_TABLE",
    "TierStateStore",
    "InMemoryTierStateStore",
    "PostgresTierStateStore",
    "bootstrap_storage_schema",
]
