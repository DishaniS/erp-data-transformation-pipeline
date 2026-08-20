"""Durable replacements for the three process-local registries.

THE PROBLEM THESE SOLVE
-----------------------
Jobs, canonical records and tier state were already durable. Three things were
not, and each produced the same failure: a restart left the system holding
identifiers that no longer resolved to anything.

    SourceRegistry   -> a registered source vanished, so its jobs 404'd
    UploadStore      -> the file survived on disk but the id did not
    mapping drafts   -> an ambiguous mapping awaiting human review was lost

All three live in ``erp_runtime`` beside the canonical records, because they
are runtime state of this application rather than catalog metadata.

WHAT IS NEVER STORED
--------------------
No password, ever. A source persists ``credential_ref`` - the NAME of a secret
- and the secret itself stays in the environment. No uploaded file *contents*
reach the database either; only the metadata needed to find the file again.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from erp_pipeline.orchestration.errors import SourceNotFoundError, UploadNotFoundError
from erp_pipeline.orchestration.sources import (
    RegisteredSource,
    SourceRegistry,
    scrub_metadata,
)
from erp_pipeline.orchestration.upload_store import StoredUpload, UploadStore
from erp_pipeline.schemas.enums import SourceType

LOGGER = logging.getLogger("erp_pipeline.runtime.persistence")

RUNTIME_SCHEMA = "erp_runtime"
SOURCES_TABLE = "registered_sources"
UPLOADS_TABLE = "uploads"
DRAFTS_TABLE = "mapping_drafts"


def _validate_schema(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        raise ValueError(f"{schema!r} is not a valid PostgreSQL schema name")

    return schema


def create_sources_sql(schema: str = RUNTIME_SCHEMA) -> str:
    # There is deliberately no password column. A schema that cannot hold a
    # secret cannot leak one.
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{SOURCES_TABLE} (
    source_id       TEXT        PRIMARY KEY,
    name            TEXT        NOT NULL,
    source_type     TEXT        NOT NULL,
    host            TEXT        NULL,
    port            INTEGER     NULL,
    database_name   TEXT        NULL,
    username        TEXT        NULL,
    credential_ref  TEXT        NULL,
    auth_database   TEXT        NULL,
    ssl_enabled     BOOLEAN     NOT NULL DEFAULT FALSE,
    description     TEXT        NULL,
    metadata_json   TEXT        NOT NULL DEFAULT '{{}}',
    registered_at   TIMESTAMPTZ NOT NULL
)
"""


def create_uploads_sql(schema: str = RUNTIME_SCHEMA) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{UPLOADS_TABLE} (
    upload_id       TEXT        PRIMARY KEY,
    display_name    TEXT        NOT NULL,
    suffix          TEXT        NOT NULL DEFAULT '',
    content_hash    TEXT        NOT NULL,
    size_bytes      BIGINT      NOT NULL,
    content_type    TEXT        NULL,
    relative_path   TEXT        NOT NULL,
    stored_at       TIMESTAMPTZ NOT NULL
)
"""


def create_drafts_sql(schema: str = RUNTIME_SCHEMA) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{DRAFTS_TABLE} (
    draft_id          TEXT        PRIMARY KEY,
    schema_id         TEXT        NOT NULL,
    source_entity     TEXT        NULL,
    status            TEXT        NOT NULL DEFAULT 'awaiting_review',
    ambiguous_fields  INTEGER     NOT NULL DEFAULT 0,
    evidence_json     TEXT        NOT NULL DEFAULT '{{}}',
    draft_version     INTEGER     NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL
)
"""


def bootstrap_runtime_persistence(engine: Any, schema: str = RUNTIME_SCHEMA) -> None:
    """Create the three runtime tables. Idempotent."""
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {_validate_schema(schema)}")
        )
        connection.execute(text(create_sources_sql(schema)))
        connection.execute(text(create_uploads_sql(schema)))
        connection.execute(text(create_drafts_sql(schema)))


# ----------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------


class PostgresSourceRegistry(SourceRegistry):
    """A source registry that survives a restart.

    Subclasses the in-memory registry so every existing caller keeps working;
    the dict becomes a write-through cache over the table.
    """

    def __init__(self, engine: Any, schema: str = RUNTIME_SCHEMA) -> None:
        super().__init__()
        self._engine = engine
        self._schema = _validate_schema(schema)

    def register(self, source: RegisteredSource) -> RegisteredSource:
        from sqlalchemy import text

        cleaned = super().register(source)

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{SOURCES_TABLE} (
                        source_id, name, source_type, host, port, database_name,
                        username, credential_ref, auth_database, ssl_enabled,
                        description, metadata_json, registered_at
                    ) VALUES (
                        :source_id, :name, :source_type, :host, :port, :database_name,
                        :username, :credential_ref, :auth_database, :ssl_enabled,
                        :description, :metadata_json, :registered_at
                    )
                    ON CONFLICT (source_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        source_type = EXCLUDED.source_type,
                        host = EXCLUDED.host,
                        port = EXCLUDED.port,
                        database_name = EXCLUDED.database_name,
                        username = EXCLUDED.username,
                        credential_ref = EXCLUDED.credential_ref,
                        auth_database = EXCLUDED.auth_database,
                        ssl_enabled = EXCLUDED.ssl_enabled,
                        description = EXCLUDED.description,
                        metadata_json = EXCLUDED.metadata_json
                    """
                ),
                {
                    "source_id": cleaned.source_id,
                    "name": cleaned.name,
                    "source_type": cleaned.source_type.value,
                    "host": cleaned.host,
                    "port": cleaned.port,
                    "database_name": cleaned.database,
                    "username": cleaned.username,
                    "credential_ref": cleaned.credential_ref,
                    "auth_database": cleaned.auth_database,
                    "ssl_enabled": cleaned.ssl_enabled,
                    "description": cleaned.description,
                    "metadata_json": json.dumps(dict(cleaned.metadata), default=str),
                    "registered_at": cleaned.registered_at,
                },
            )

        return cleaned

    def find(self, source_id: str) -> RegisteredSource | None:
        cached = super().find(source_id)

        if cached is not None:
            return cached

        from sqlalchemy import text

        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{SOURCES_TABLE} "
                    "WHERE source_id = :source_id"
                ),
                {"source_id": source_id},
            ).mappings().first()

        if row is None:
            return None

        source = _row_to_source(row)
        super().register(source)

        return source

    def get(self, source_id: str) -> RegisteredSource:
        source = self.find(source_id)

        if source is None:
            raise SourceNotFoundError(
                f"source {source_id!r} is not registered", source_id=source_id
            )

        return source

    def list(self, limit: int = 100, offset: int = 0) -> tuple[RegisteredSource, ...]:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{SOURCES_TABLE} "
                    "ORDER BY registered_at LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            ).mappings().all()

        return tuple(_row_to_source(row) for row in rows)


def _row_to_source(row: Mapping[str, Any]) -> RegisteredSource:
    return RegisteredSource(
        source_id=row["source_id"],
        name=row["name"],
        source_type=SourceType(row["source_type"]),
        host=row["host"],
        port=row["port"],
        database=row["database_name"],
        username=row["username"],
        credential_ref=row["credential_ref"],
        auth_database=row["auth_database"],
        ssl_enabled=bool(row["ssl_enabled"]),
        description=row["description"],
        metadata=scrub_metadata(json.loads(row["metadata_json"] or "{}")),
        registered_at=row["registered_at"],
    )


# ----------------------------------------------------------------------
# Uploads
# ----------------------------------------------------------------------


class PostgresUploadStore(UploadStore):
    """An upload store whose index survives a restart.

    The bytes were always durable; only the id-to-path mapping was not. The
    stored path is kept RELATIVE to the upload root so the row stays valid if
    the volume is mounted at a different location.
    """

    def __init__(
        self,
        root: Path | str,
        engine: Any,
        max_bytes: int | None = None,
        schema: str = RUNTIME_SCHEMA,
    ) -> None:
        if max_bytes is None:
            super().__init__(root)
        else:
            super().__init__(root, max_bytes)

        self._engine = engine
        self._schema = _validate_schema(schema)

    def store_stream(self, stream: Any, filename: str | None = None,
                     content_type: str | None = None) -> StoredUpload:
        from sqlalchemy import text

        stored = super().store_stream(stream, filename, content_type)

        try:
            relative = stored.path.relative_to(self.root)
        except ValueError:  # pragma: no cover - defensive
            relative = Path(stored.upload_id) / stored.display_name

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{UPLOADS_TABLE} (
                        upload_id, display_name, suffix, content_hash,
                        size_bytes, content_type, relative_path, stored_at
                    ) VALUES (
                        :upload_id, :display_name, :suffix, :content_hash,
                        :size_bytes, :content_type, :relative_path, :stored_at
                    )
                    ON CONFLICT (upload_id) DO NOTHING
                    """
                ),
                {
                    "upload_id": stored.upload_id,
                    "display_name": stored.display_name,
                    "suffix": stored.suffix,
                    "content_hash": stored.content_hash,
                    "size_bytes": stored.size_bytes,
                    "content_type": stored.content_type,
                    "relative_path": str(relative).replace("\\", "/"),
                    "stored_at": stored.stored_at,
                },
            )

        return stored

    def get(self, upload_id: str) -> StoredUpload:
        from sqlalchemy import text

        try:
            return super().get(upload_id)
        except UploadNotFoundError:
            pass

        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{UPLOADS_TABLE} "
                    "WHERE upload_id = :upload_id"
                ),
                {"upload_id": upload_id},
            ).mappings().first()

        if row is None:
            raise UploadNotFoundError(
                f"upload {upload_id!r} is not known to this service",
                upload_id=upload_id,
            )

        stored = StoredUpload(
            upload_id=row["upload_id"],
            display_name=row["display_name"],
            suffix=row["suffix"],
            content_hash=row["content_hash"],
            size_bytes=row["size_bytes"],
            stored_at=row["stored_at"],
            path=self.root / row["relative_path"],
            content_type=row["content_type"],
        )
        self._index[upload_id] = stored

        return stored


# ----------------------------------------------------------------------
# Mapping drafts
# ----------------------------------------------------------------------


class PostgresMappingDraftStore:
    """Ambiguous mappings awaiting human review.

    Behaves like the dict it replaces (``__contains__``, ``get``, ``__setitem__``,
    ``pop``) so the orchestration service needs no special-casing.

    Only the evidence needed to make and record a decision is stored - this is
    deliberately NOT a second copy of Phase 8's ``MappingProfile``. Once a
    human approves, the profile goes to Phase 2's catalog and the draft is
    retired.
    """

    def __init__(self, engine: Any, schema: str = RUNTIME_SCHEMA) -> None:
        self._engine = engine
        self._schema = _validate_schema(schema)

    def __setitem__(self, draft_id: str, payload: Mapping[str, Any]) -> None:
        self.save(draft_id, payload)

    def save(self, draft_id: str, payload: Mapping[str, Any]) -> None:
        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        evidence = {
            key: value
            for key, value in payload.items()
            # The live MappingResult object is not serializable and is a cache,
            # not state; it is recomputed from the schema when needed.
            if key not in {"result"}
        }

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{DRAFTS_TABLE} (
                        draft_id, schema_id, source_entity, status,
                        ambiguous_fields, evidence_json, draft_version,
                        created_at, updated_at
                    ) VALUES (
                        :draft_id, :schema_id, :source_entity, :status,
                        :ambiguous_fields, :evidence_json, 0, :now, :now
                    )
                    ON CONFLICT (draft_id) DO UPDATE SET
                        schema_id = EXCLUDED.schema_id,
                        source_entity = EXCLUDED.source_entity,
                        status = EXCLUDED.status,
                        ambiguous_fields = EXCLUDED.ambiguous_fields,
                        evidence_json = EXCLUDED.evidence_json,
                        draft_version = {self._schema}.{DRAFTS_TABLE}.draft_version + 1,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "draft_id": draft_id,
                    "schema_id": payload.get("schema_id") or "",
                    "source_entity": payload.get("source_entity"),
                    "status": payload.get("status", "awaiting_review"),
                    "ambiguous_fields": int(payload.get("ambiguous_fields") or 0),
                    "evidence_json": json.dumps(evidence, default=str),
                    "now": now,
                },
            )

    def get(self, draft_id: str, default: Any = None) -> Any:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{DRAFTS_TABLE} "
                    "WHERE draft_id = :draft_id"
                ),
                {"draft_id": draft_id},
            ).mappings().first()

        if row is None:
            return default

        payload = json.loads(row["evidence_json"] or "{}")
        payload.update(
            {
                "schema_id": row["schema_id"],
                "source_entity": row["source_entity"],
                "status": row["status"],
                "ambiguous_fields": row["ambiguous_fields"],
                "version": row["draft_version"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

        return payload

    def __contains__(self, draft_id: str) -> bool:
        return self.get(draft_id) is not None

    def __getitem__(self, draft_id: str) -> Any:
        found = self.get(draft_id)

        if found is None:
            raise KeyError(draft_id)

        return found

    def pop(self, draft_id: str, default: Any = None) -> Any:
        from sqlalchemy import text

        found = self.get(draft_id, default)

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"DELETE FROM {self._schema}.{DRAFTS_TABLE} "
                    "WHERE draft_id = :draft_id"
                ),
                {"draft_id": draft_id},
            )

        return found


__all__ = [
    "RUNTIME_SCHEMA",
    "SOURCES_TABLE",
    "UPLOADS_TABLE",
    "DRAFTS_TABLE",
    "bootstrap_runtime_persistence",
    "PostgresSourceRegistry",
    "PostgresUploadStore",
    "PostgresMappingDraftStore",
]
