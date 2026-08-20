"""A persistent implementation of Phase 10's ``CanonicalRecordStore``.

WHY NOT A NEW MODEL
-------------------
Phase 10 already defines the contract (``upsert`` / ``get`` / ``delete``) and
Phase 1 already defines ``CanonicalRecord``. Inventing either again would fork
the pipeline's view of what a record is. This module adds storage and nothing
else: the same contract, backed by PostgreSQL instead of a dict, so a record
survives the process that loaded it.

The record is stored as its own canonical JSON. That keeps the frozen Phase 1
contract as the single source of truth about shape - a column-per-field table
would have to be migrated every time the canonical model grew.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from erp_pipeline.schemas.canonical_models import CanonicalRecord

RECORD_SCHEMA_NAME = "erp_runtime"
RECORDS_TABLE = "canonical_records"


def _validate_schema(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        raise ValueError(f"{schema!r} is not a valid PostgreSQL schema name")

    return schema


def create_records_sql(schema: str = RECORD_SCHEMA_NAME) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{RECORDS_TABLE} (
    canonical_id      TEXT        PRIMARY KEY,
    entity_type       TEXT        NOT NULL,
    source_system_id  TEXT        NULL,
    content_hash      TEXT        NULL,
    record_json       TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def bootstrap_record_schema(engine: Any, schema: str = RECORD_SCHEMA_NAME) -> None:
    """Create the runtime record namespace. Idempotent.

    Deliberately its own schema: mixing canonical business records into
    ``erp_catalog`` (metadata), ``erp_sync`` (watermarks) or
    ``erp_vector_storage`` (tier state) would blur four different lifecycles
    into one namespace.
    """
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {_validate_schema(schema)}")
        )
        connection.execute(text(create_records_sql(schema)))


class PostgresCanonicalRecordStore:
    """Phase 10's store contract, backed by PostgreSQL."""

    def __init__(self, engine: Any, schema: str = RECORD_SCHEMA_NAME) -> None:
        self._engine = engine
        self._schema = _validate_schema(schema)

    @property
    def schema(self) -> str:
        return self._schema

    def upsert(self, record: CanonicalRecord) -> CanonicalRecord:
        from sqlalchemy import text

        payload = _serialize(record)
        # The frozen Phase 1 contract names this `record_id`. `canonical_id`
        # is accepted as a fallback so a caller passing a compatible object
        # still works, but the contract's own name comes first.
        identifier = getattr(record, "record_id", None) or getattr(
            record, "canonical_id", None
        )

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{RECORDS_TABLE} (
                        canonical_id, entity_type, source_system_id,
                        content_hash, record_json, updated_at
                    ) VALUES (
                        :canonical_id, :entity_type, :source_system_id,
                        :content_hash, :record_json, :updated_at
                    )
                    ON CONFLICT (canonical_id) DO UPDATE SET
                        entity_type = EXCLUDED.entity_type,
                        source_system_id = EXCLUDED.source_system_id,
                        content_hash = EXCLUDED.content_hash,
                        record_json = EXCLUDED.record_json,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "canonical_id": identifier,
                    "entity_type": record.entity_type,
                    "source_system_id": getattr(record, "source_system_id", None),
                    "content_hash": getattr(record, "content_hash", None),
                    "record_json": payload,
                    "updated_at": datetime.now(timezone.utc),
                },
            )

        return record

    def get(self, canonical_id: str) -> CanonicalRecord | None:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT record_json FROM {self._schema}.{RECORDS_TABLE} "
                    "WHERE canonical_id = :canonical_id"
                ),
                {"canonical_id": canonical_id},
            ).mappings().first()

        if row is None:
            return None

        return _deserialize(row["record_json"])

    def delete(self, canonical_id: str) -> bool:
        from sqlalchemy import text

        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    f"DELETE FROM {self._schema}.{RECORDS_TABLE} "
                    "WHERE canonical_id = :canonical_id"
                ),
                {"canonical_id": canonical_id},
            )

        return bool(result.rowcount)

    def count(self) -> int:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            return int(
                connection.execute(
                    text(f"SELECT COUNT(*) FROM {self._schema}.{RECORDS_TABLE}")
                ).scalar()
                or 0
            )

    def record_ids(self, limit: int = 100) -> tuple[str, ...]:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT canonical_id FROM {self._schema}.{RECORDS_TABLE} "
                    "ORDER BY canonical_id LIMIT :limit"
                ),
                {"limit": limit},
            ).all()

        return tuple(row[0] for row in rows)


def _serialize(record: CanonicalRecord) -> str:
    """Use the contract's own serializer; never invent a second one."""
    if hasattr(record, "to_json_dict"):
        return json.dumps(record.to_json_dict(), default=str, sort_keys=True)

    if hasattr(record, "to_dict"):
        return json.dumps(record.to_dict(), default=str, sort_keys=True)

    from dataclasses import asdict

    return json.dumps(asdict(record), default=str, sort_keys=True)


def _deserialize(payload: str) -> CanonicalRecord:
    data = json.loads(payload)

    if hasattr(CanonicalRecord, "from_dict"):
        return CanonicalRecord.from_dict(data)  # type: ignore[attr-defined]

    return _RehydratedRecord(data)  # type: ignore[return-value]


class _RehydratedRecord(dict):
    """A read-only view when the frozen contract offers no ``from_dict``.

    Reconstructing a frozen Phase 1 dataclass from JSON would mean re-deriving
    its nested value objects here, which is precisely the duplication Phase 13
    must not introduce. The stored canonical JSON is returned as-is instead,
    and the API serializes it directly.
    """

    @property
    def record_id(self) -> str:
        return self.get("record_id", "") or self.get("canonical_id", "")

    @property
    def canonical_id(self) -> str:
        return self.record_id

    @property
    def entity_type(self) -> str:
        return self.get("entity_type", "")

    def to_dict(self) -> dict[str, Any]:
        return dict(self)

    def to_json_dict(self) -> dict[str, Any]:
        return dict(self)


__all__ = [
    "RECORD_SCHEMA_NAME",
    "RECORDS_TABLE",
    "PostgresCanonicalRecordStore",
    "bootstrap_record_schema",
    "create_records_sql",
]
