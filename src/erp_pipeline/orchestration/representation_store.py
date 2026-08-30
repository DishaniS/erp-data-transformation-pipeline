"""Durable storage for the AI-ready text behind every vector (Phase 5).

WHERE THE TEXT USED TO GO
-------------------------
``AIRepresentation.text_for_ai`` was built, embedded, and then dropped.
``EmbeddingService._record`` reads the text, hands it to the model, and returns
an ``EmbeddingRecord`` that has no text field at all. The only other copy lived
on ``PipelineContext.representations``, which exists for the duration of one
job.

So the system could find the right chunk of EMP002's birth certificate and had
no way to tell anyone what it said. Search returned an identity; nothing could
turn that identity back into content.

WHY NOT JUST PUT THE TEXT IN QDRANT
-----------------------------------
Because that makes the vector index a second copy of the corpus. Qdrant answers
"which representations are relevant?"; this store answers "what does this
representation contain?". Collapsing the two doubles storage, puts extracted
document text inside every payload a search touches, and gives the corpus two
sources of truth that can disagree. ``ai.vector._payload_for`` has always
defaulted ``include_text`` to ``False`` for exactly this reason, and Phase 5
does not change that.

WHY THE TABLE MIRRORS THE MODEL
-------------------------------
``AIRepresentation`` has four first-class fields plus ``metadata`` and
``content``. The table stores exactly that and nothing more: no field is
duplicated between a column and the JSON, so there is no way for the two to
disagree. Everything the API reports that is not a first-class field is read
back out of ``metadata_json``.

Lookup is by primary key, which is the only access pattern Phase 5 has. If a
later phase needs to query by parent record or content kind, those are promoted
to columns then, by the same additive migration the tier-state table uses.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.sync.propagation import AIRepresentation

REPRESENTATION_SCHEMA_NAME = "erp_runtime"
REPRESENTATIONS_TABLE = "ai_representations"


def _validate_schema(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        raise ValueError(f"{schema!r} is not a valid PostgreSQL schema name")

    return schema


def create_representations_sql(schema: str = REPRESENTATION_SCHEMA_NAME) -> str:
    """The table, shaped like the model it stores.

    ``text_for_ai`` is NULLABLE because a representation legitimately may have
    none - an image OCR read nothing from still has identity and provenance.
    Storing the row anyway is what keeps "the vector exists but the text is
    missing" from being indistinguishable from "this representation was never
    persisted".
    """
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{REPRESENTATIONS_TABLE} (
    representation_id      TEXT        PRIMARY KEY,
    entity_type            TEXT        NOT NULL,
    text_for_ai            TEXT        NULL,
    content_hash           TEXT        NULL,
    content_json           TEXT        NOT NULL DEFAULT '{{}}',
    metadata_json          TEXT        NOT NULL DEFAULT '{{}}',
    source_record_ids_json TEXT        NOT NULL DEFAULT '[]',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def bootstrap_representation_schema(
    engine: Any, schema: str = REPRESENTATION_SCHEMA_NAME
) -> None:
    """Create the representation table. Idempotent.

    Lives in ``erp_runtime`` beside ``canonical_records`` because it is the
    same kind of thing: the durable content this application serves, as
    opposed to catalog metadata, sync watermarks or tier state.
    """
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {_validate_schema(schema)}")
        )
        connection.execute(text(create_representations_sql(schema)))


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _load(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback

    try:
        return json.loads(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return fallback


def _protect(text: str | None, metadata: Mapping[str, Any], cipher: Any) -> str | None:
    """Encrypt this text if its classification requires it.

    Fails CLOSED. A classification that requires encryption and no usable key
    means the row is not written at all - and because Phase 5 persists before
    embedding, the vector never becomes searchable either. Absent beats
    exposed.
    """
    from erp_pipeline.orchestration.representation_crypto import requires_encryption

    if text is None or cipher is None:
        return text

    if not requires_encryption((metadata or {}).get("sensitivity")):
        return text

    return cipher.encrypt(text)


def _reveal(stored: str | None, cipher: Any) -> str | None:
    """Decrypt if the stored value is an envelope; return plaintext untouched.

    This is what makes legacy rows keep working: a value written before Phase
    10 carries no envelope prefix and passes straight through.
    """
    from erp_pipeline.orchestration.representation_crypto import is_encrypted

    if stored is None or not is_encrypted(stored):
        return stored

    if cipher is None:
        from erp_pipeline.orchestration.representation_crypto import (
            EncryptionKeyUnavailableError,
        )

        raise EncryptionKeyUnavailableError(
            "this representation is encrypted and no decryption key is "
            "configured for this deployment"
        )

    return cipher.decrypt(stored)


def _to_representation(
    row: Mapping[str, Any], cipher: Any = None
) -> AIRepresentation:
    return AIRepresentation(
        representation_id=row["representation_id"],
        entity_type=row["entity_type"],
        text_for_ai=_reveal(row["text_for_ai"], cipher),
        content=_load(row["content_json"], {}),
        source_record_ids=tuple(_load(row["source_record_ids_json"], [])),
        metadata=_load(row["metadata_json"], {}),
        content_hash=row["content_hash"],
    )


class InMemoryRepresentationStore:
    """The reference semantics a durable store must match.

    Upserting the same representation twice replaces one row rather than
    accumulating two - the same rule ``InMemoryEmbeddingStore`` follows, and
    the reason deterministic representation ids exist.
    """

    def __init__(self, cipher: Any = None) -> None:
        self._rows: dict[str, AIRepresentation] = {}
        self.upsert_calls = 0
        # Present so the in-memory reference matches the durable store's
        # behaviour, including its fail-closed refusal.
        self._cipher = cipher

    def upsert(self, representation: AIRepresentation) -> AIRepresentation:
        # Encrypting here would make the stored object differ from the one
        # returned, so the in-memory store only VALIDATES that a key exists -
        # the fail-closed behaviour - and keeps the object as given.
        _protect(
            representation.text_for_ai, representation.metadata, self._cipher
        )
        self.upsert_calls += 1
        self._rows[representation.representation_id] = representation

        return representation

    def upsert_many(
        self, representations: Iterable[AIRepresentation]
    ) -> int:
        return sum(1 for item in representations if self.upsert(item))

    def get(self, representation_id: str) -> AIRepresentation | None:
        return self._rows.get(representation_id)

    def get_many(
        self, representation_ids: Sequence[str]
    ) -> dict[str, AIRepresentation]:
        return {
            key: self._rows[key] for key in representation_ids if key in self._rows
        }

    def delete(self, representation_id: str) -> bool:
        return self._rows.pop(representation_id, None) is not None

    def count(self) -> int:
        return len(self._rows)

    def list_ids(self) -> tuple[str, ...]:
        return tuple(self._rows)


class PostgresRepresentationStore:
    """The same contract, backed by PostgreSQL."""

    def __init__(
        self,
        engine: Any,
        schema: str = REPRESENTATION_SCHEMA_NAME,
        cipher: Any = None,
    ) -> None:
        self._engine = engine
        self._schema = _validate_schema(schema)
        self._cipher = cipher

    @property
    def schema(self) -> str:
        return self._schema

    def upsert(self, representation: AIRepresentation) -> AIRepresentation:
        from sqlalchemy import text

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{REPRESENTATIONS_TABLE} (
                        representation_id, entity_type, text_for_ai,
                        content_hash, content_json, metadata_json,
                        source_record_ids_json, updated_at
                    ) VALUES (
                        :representation_id, :entity_type, :text_for_ai,
                        :content_hash, :content_json, :metadata_json,
                        :source_record_ids_json, :updated_at
                    )
                    ON CONFLICT (representation_id) DO UPDATE SET
                        entity_type = EXCLUDED.entity_type,
                        text_for_ai = EXCLUDED.text_for_ai,
                        content_hash = EXCLUDED.content_hash,
                        content_json = EXCLUDED.content_json,
                        metadata_json = EXCLUDED.metadata_json,
                        source_record_ids_json =
                            EXCLUDED.source_record_ids_json,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                self._row(representation),
            )

        return representation

    def upsert_many(self, representations: Iterable[AIRepresentation]) -> int:
        """One transaction for the batch.

        A per-representation transaction would make a thousand-chunk document a
        thousand round trips, and would leave a partial batch behind on failure
        with no way to tell how far it got.
        """
        from sqlalchemy import text

        rows = [self._row(item) for item in representations]

        if not rows:
            return 0

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.{REPRESENTATIONS_TABLE} (
                        representation_id, entity_type, text_for_ai,
                        content_hash, content_json, metadata_json,
                        source_record_ids_json, updated_at
                    ) VALUES (
                        :representation_id, :entity_type, :text_for_ai,
                        :content_hash, :content_json, :metadata_json,
                        :source_record_ids_json, :updated_at
                    )
                    ON CONFLICT (representation_id) DO UPDATE SET
                        entity_type = EXCLUDED.entity_type,
                        text_for_ai = EXCLUDED.text_for_ai,
                        content_hash = EXCLUDED.content_hash,
                        content_json = EXCLUDED.content_json,
                        metadata_json = EXCLUDED.metadata_json,
                        source_record_ids_json =
                            EXCLUDED.source_record_ids_json,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                rows,
            )

        return len(rows)

    def get(self, representation_id: str) -> AIRepresentation | None:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT * FROM {self._schema}.{REPRESENTATIONS_TABLE} "
                    "WHERE representation_id = :representation_id"
                ),
                {"representation_id": representation_id},
            ).mappings().first()

        return _to_representation(row, self._cipher) if row else None

    def get_many(
        self, representation_ids: Sequence[str]
    ) -> dict[str, AIRepresentation]:
        """One query for many ids, so a caller never loops over ``get``."""
        from sqlalchemy import bindparam, text

        wanted = list(dict.fromkeys(representation_ids))

        if not wanted:
            return {}

        statement = text(
            f"SELECT * FROM {self._schema}.{REPRESENTATIONS_TABLE} "
            "WHERE representation_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))

        with self._engine.connect() as connection:
            rows = connection.execute(
                statement, {"ids": wanted}
            ).mappings().all()

        return {
            row["representation_id"]: _to_representation(row, self._cipher)
            for row in rows
        }

    def delete(self, representation_id: str) -> bool:
        from sqlalchemy import text

        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    f"DELETE FROM {self._schema}.{REPRESENTATIONS_TABLE} "
                    "WHERE representation_id = :representation_id"
                ),
                {"representation_id": representation_id},
            )

        return bool(result.rowcount)

    def count(self) -> int:
        from sqlalchemy import text

        with self._engine.connect() as connection:
            return int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM "
                        f"{self._schema}.{REPRESENTATIONS_TABLE}"
                    )
                ).scalar()
                or 0
            )

    def _row(self, representation: AIRepresentation) -> dict[str, Any]:
        return {
            "representation_id": representation.representation_id,
            "entity_type": representation.entity_type,
            # Stored exactly as embedded. Re-truncating here would break the
            # property that retrievable text IS the embedded text.
            # Phase 10: encrypted when the classification requires it. The
            # column holds ciphertext; nothing else about the row changes.
            "text_for_ai": _protect(
                representation.text_for_ai,
                representation.metadata,
                self._cipher,
            ),
            "content_hash": representation.resolved_hash(),
            "content_json": _dump(dict(representation.content or {})),
            "metadata_json": _dump(dict(representation.metadata or {})),
            "source_record_ids_json": _dump(
                list(representation.source_record_ids or ())
            ),
            "updated_at": datetime.now(timezone.utc),
        }


__all__ = [
    "REPRESENTATION_SCHEMA_NAME",
    "REPRESENTATIONS_TABLE",
    "InMemoryRepresentationStore",
    "PostgresRepresentationStore",
    "bootstrap_representation_schema",
    "create_representations_sql",
]
