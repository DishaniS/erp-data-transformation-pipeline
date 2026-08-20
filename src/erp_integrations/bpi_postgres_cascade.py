"""Production BPI cascade: real PostgreSQL, real case rebuild, real vectors.

WHAT THIS COMPLETES
-------------------
``bpi_case_cascade.py`` defined the ``CaseDataAccess`` contract and proved the
cascade against an in-memory implementation. That left the original production
problem only half fixed: nothing here actually read ``cleaned_event_logs`` or
wrote ``ai_ready_cases``. This module is that missing half.

    changed cleaned event
        -> normalized_case_id                    (SQL, one indexed lookup)
        -> events of THAT case only              (SQL, one indexed range)
        -> build_case_document(...)              (the BATCH builder, reused)
        -> UPSERT ai_ready_cases                 (the BATCH upsert, reused)
        -> content_hash compared against the row's PREVIOUS hash
        -> embed only if it moved                (real model, real payload)
        -> same deterministic Qdrant point id    (frozen convention)

THE BATCH BUILDER IS REUSED, NOT REIMPLEMENTED (Step 3)
--------------------------------------------------------
``build_ai_ready_cases.build_case_document`` already takes the rows of ONE case
- the batch script calls it per group. So a single-case rebuild needs no new
case-building logic at all: it needs the right rows and the same function.
Anything else would risk an incremental representation that differs from the
batch one, which would make every incremental run look like a content change.

The upsert SQL is likewise the batch statement, including its rule that
``embedding_status`` becomes ``'pending'`` only when ``content_hash`` actually
changed.

THE HASH ORDERING TRAP
----------------------
The generic coordinator calls ``builder.rebuild()`` before
``ledger.get_hash()``. If the rebuild writes the new hash to
``ai_ready_cases`` first, the ledger would read back the value just written and
every case would look unchanged - silently disabling re-embedding entirely.

So the rebuild captures the PREVIOUS hash before it writes, into
``PreviousHashRegistry``, and the ledger reads that. This mirrors the batch
script, which also loads existing hashes before upserting.

BOUNDARY
--------
Lives outside ``erp_pipeline`` because a frozen Phase 1 test forbids anything
under that package from importing ``bpi2020``. Speaks to the pipeline only
through the generic Phase 10 protocols.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from erp_pipeline.sync.propagation import AIRepresentation, EmbeddingResult

#: Real tables in the AI-native database.
CLEANED_EVENTS_TABLE = "cleaned_event_logs"
AI_READY_CASES_TABLE = "ai_ready_cases"


# ============================================================
# Reused BPI logic (lazy imports keep this module inspectable)
# ============================================================

def build_case_document(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build ONE case using the batch builder itself (Step 3).

    ``build_ai_ready_cases.build_case_document`` is already per-case: the batch
    script groups by ``normalized_case_id`` and calls it once per group. Feeding
    it exactly one group's rows therefore produces byte-identical output to a
    full batch rebuild of that case, by construction rather than by agreement.
    """
    import pandas as pd

    from bpi2020.transformation.build_ai_ready_cases import (
        build_case_document as _build,
    )

    if not rows:
        raise ValueError("build_case_document needs at least one event row")

    return _build(pd.DataFrame([dict(row) for row in rows]))


def make_qdrant_point_id(record_id: str) -> str:
    """The frozen Phase 0 vector identity convention (Step 4)."""
    from bpi2020.common.stable_ids import make_qdrant_point_id as _make

    return _make(record_id)


def build_unified_case_record(
    case_row: Mapping[str, Any], case_document: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The unified-knowledge shape the existing embedder consumes.

    Mirrors ``build_unified_bpi_knowledge_base.build_case_knowledge_record`` so
    the embedding text and the Qdrant payload are the same ones the batch
    pipeline would produce for this case. Only the fields the embedder actually
    reads are assembled - this is a bridge, not a second unified builder.
    """
    record_id = case_row["case_record_id"]
    case_id = str(case_row["case_id"])
    process_type = str(case_row["process_type"])

    return {
        "record_id": record_id,
        "unified_record_id": record_id,
        "record_type": "erp_case",
        "source_system": "bpi_challenge_2020",
        "source_entity": AI_READY_CASES_TABLE,
        "stable_source_key": record_id,
        "source_table": AI_READY_CASES_TABLE,
        "source_record_id": case_row.get("id"),
        "title": f"ERP Case {case_id} - {process_type}",
        "primary_reference": case_id,
        "process_type": process_type,
        "text_for_ai": case_row.get("case_summary") or "",
        "content_hash": case_row.get("content_hash"),
        "metadata": {
            "case_id": case_id,
            "process_type": process_type,
            "total_events": case_row.get("total_events"),
            "start_timestamp": case_row.get("start_timestamp"),
            "end_timestamp": case_row.get("end_timestamp"),
        },
    }


# ============================================================
# Previous-hash registry
# ============================================================

@dataclass
class PreviousHashRegistry:
    """The hash a case had BEFORE this run rebuilt it.

    Exists because of the ordering described in the module docstring: the
    rebuild writes ``ai_ready_cases``, so by the time the ledger is consulted
    the stored hash is already the new one.
    """

    hashes: dict[str, str | None] = field(default_factory=dict)

    def remember(self, case_record_id: str, previous: str | None) -> None:
        # First write wins: within one run a case may be resolved by several
        # events, and only the hash from before the FIRST rebuild is "previous".
        self.hashes.setdefault(case_record_id, previous)

    def get(self, case_record_id: str) -> str | None:
        return self.hashes.get(case_record_id)

    def forget(self, case_record_id: str) -> None:
        self.hashes.pop(case_record_id, None)

    def clear(self) -> None:
        self.hashes.clear()


# ============================================================
# Real PostgreSQL data access (Step 2)
# ============================================================

@dataclass
class PostgresCaseAccess:
    """``CaseDataAccess`` over the real AI-native tables.

    ``schema`` lets an integration test point the same code at an isolated
    copy of the tables, so a live proof never touches the 270,211-event /
    32,999-case baseline.

    Counters are instrumentation for the no-full-rebuild proof: a claim that
    only one case was read is worth little without a count of the queries that
    actually ran.
    """

    engine: Any
    schema: str | None = None
    #: Instrumentation.
    event_lookups: int = 0
    case_event_queries: int = 0
    case_hash_reads: int = 0
    case_upserts: int = 0
    rows_read: int = 0

    def _table(self, name: str) -> str:
        return f"{self.schema}.{name}" if self.schema else name

    @property
    def events_table(self) -> str:
        return self._table(CLEANED_EVENTS_TABLE)

    @property
    def cases_table(self) -> str:
        return self._table(AI_READY_CASES_TABLE)

    # -- reads --

    def case_id_for_event(self, event_key: str) -> str | None:
        """Which case a changed cleaned event belongs to.

        One indexed lookup by the event's stable identity. Never a scan.
        """
        from sqlalchemy import text

        self.event_lookups += 1

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT normalized_case_id FROM {self.events_table} "
                    "WHERE event_record_id = :key LIMIT 1"
                ),
                {"key": str(event_key)},
            ).first()

        return None if row is None or row[0] is None else str(row[0])

    def events_for_case(self, case_id: str) -> list[dict[str, Any]]:
        """Every cleaned event of ONE case - and nothing else.

        This is where "no full rebuild" is actually enforced: the predicate is
        the indexed ``normalized_case_id``, so the cost is one case's events,
        not 270,211 rows.
        """
        from sqlalchemy import text

        self.case_event_queries += 1

        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"""
                    SELECT id, event_record_id, source_table, process_type,
                           normalized_case_id, normalized_activity,
                           event_timestamp, record_data
                    FROM {self.events_table}
                    WHERE normalized_case_id = :case_id
                    ORDER BY event_timestamp NULLS LAST, id
                    """
                ),
                {"case_id": str(case_id)},
            ).mappings().all()

        self.rows_read += len(rows)

        return [dict(row) for row in rows]

    def load_case_hash(self, case_record_id: str) -> str | None:
        from sqlalchemy import text

        self.case_hash_reads += 1

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT content_hash FROM {self.cases_table} "
                    "WHERE case_record_id = :id"
                ),
                {"id": case_record_id},
            ).first()

        return None if row is None else row[0]

    def load_case_row(self, case_record_id: str) -> dict[str, Any] | None:
        from sqlalchemy import text

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT * FROM {self.cases_table} WHERE case_record_id = :id"
                ),
                {"id": case_record_id},
            ).mappings().first()

        return None if row is None else dict(row)

    def load_case_row_by_case_id(self, case_id: str) -> dict[str, Any] | None:
        """Look a case row up by its business case id.

        Used when a change payload carries the case but not its process type.
        """
        from sqlalchemy import text

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT * FROM {self.cases_table} WHERE case_id = :cid LIMIT 1"
                ),
                {"cid": str(case_id)},
            ).mappings().first()

        return None if row is None else dict(row)

    def first_process_type_for_case(self, case_id: str) -> str | None:
        """Process type from the case's own cleaned events."""
        from sqlalchemy import text

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT process_type FROM {self.events_table} "
                    "WHERE normalized_case_id = :cid LIMIT 1"
                ),
                {"cid": str(case_id)},
            ).first()

        return None if row is None or row[0] is None else str(row[0])

    def count_cases(self) -> int:
        from sqlalchemy import text

        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    text(f"SELECT COUNT(*) FROM {self.cases_table}")
                ).scalar()
                or 0
            )

    # -- writes --

    def upsert_case(
        self,
        case_record_id: str,
        case_id: str,
        payload: Mapping[str, Any],
        content_hash: str,
        changed: bool,
    ) -> None:
        """Persist ONE rebuilt case using the BATCH upsert statement.

        Reused verbatim from ``build_ai_ready_cases.upsert_ai_ready_cases``,
        including its rule that ``embedding_status`` drops to ``'pending'``
        only when ``content_hash`` is genuinely different. That rule is what
        makes an unchanged case keep its existing vector - so restating it
        differently here would break the very property this phase exists to
        provide.

        ``changed`` is deliberately NOT consulted: the SQL decides, from the
        row that is actually stored, which is one fewer thing that can drift.
        """
        import json

        from sqlalchemy import text

        self.case_upserts += 1

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {self.cases_table} (
                        case_record_id, content_hash, case_id, process_type,
                        case_summary, case_json, total_events,
                        start_timestamp, end_timestamp, embedding_status,
                        updated_at
                    ) VALUES (
                        :case_record_id, :content_hash, :case_id, :process_type,
                        :case_summary, CAST(:case_json AS JSONB), :total_events,
                        :start_timestamp, :end_timestamp, 'pending',
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (case_record_id) DO UPDATE SET
                        case_id = EXCLUDED.case_id,
                        process_type = EXCLUDED.process_type,
                        case_summary = EXCLUDED.case_summary,
                        case_json = EXCLUDED.case_json,
                        total_events = EXCLUDED.total_events,
                        start_timestamp = EXCLUDED.start_timestamp,
                        end_timestamp = EXCLUDED.end_timestamp,
                        content_hash = EXCLUDED.content_hash,
                        updated_at = CURRENT_TIMESTAMP,
                        embedding_status = CASE
                            WHEN {self.cases_table}.content_hash
                                 IS DISTINCT FROM EXCLUDED.content_hash
                                THEN 'pending'
                            ELSE {self.cases_table}.embedding_status
                        END
                    """
                ),
                {
                    "case_record_id": case_record_id,
                    "content_hash": content_hash,
                    "case_id": case_id,
                    "process_type": payload.get("process_type"),
                    "case_summary": payload.get("case_summary"),
                    "case_json": json.dumps(
                        payload.get("case_json", {}),
                        ensure_ascii=False,
                        default=str,
                    ),
                    "total_events": payload.get("total_events"),
                    "start_timestamp": payload.get("start_timestamp"),
                    "end_timestamp": payload.get("end_timestamp"),
                },
            )

    def mark_embedded(self, case_record_id: str, point_id: str) -> None:
        """Mirror the embedder's own write-back."""
        from sqlalchemy import text

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    UPDATE {self.cases_table}
                    SET embedding_status = 'completed',
                        qdrant_point_id = :point_id,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE case_record_id = :id
                    """
                ),
                {"id": case_record_id, "point_id": point_id},
            )

    def delete_case(self, case_record_id: str) -> bool:
        from sqlalchemy import text

        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    f"DELETE FROM {self.cases_table} WHERE case_record_id = :id"
                ),
                {"id": case_record_id},
            )

        return bool(result.rowcount)

    def reset_counters(self) -> None:
        self.event_lookups = 0
        self.case_event_queries = 0
        self.case_hash_reads = 0
        self.case_upserts = 0
        self.rows_read = 0


# ============================================================
# Affected-case resolution (Step 2)
# ============================================================

@dataclass
class PostgresAffectedCaseResolver:
    """Which case one changed cleaned event belongs to.

    Takes ``process_type`` from the EVENT ROW rather than from a constant.
    ``make_case_record_id(process_type, case_id)`` includes it, so a fixed
    default would compute a key the builder never writes - the cascade would
    resolve a case that does not exist and silently rebuild nothing.

    Falls back to an indexed lookup by ``event_record_id`` when the change
    payload does not carry the case itself.
    """

    access: PostgresCaseAccess
    index: Any = None
    calls: int = 0
    resolved_case_ids: list[str] = field(default_factory=list)

    def resolve_affected(self, change: Any, record: Any) -> tuple[str, ...]:
        self.calls += 1

        payload = getattr(change, "payload", None) or {}

        case_id = payload.get("normalized_case_id") or payload.get("case_id")
        process_type = payload.get("process_type")

        if not case_id:
            case_id = self.access.case_id_for_event(
                getattr(change, "record_key", "")
            )

        if not case_id:
            return ()

        case_id = str(case_id)

        if not process_type:
            row = self.access.load_case_row_by_case_id(case_id)
            process_type = (row or {}).get("process_type")

        if not process_type:
            process_type = self.access.first_process_type_for_case(case_id)

        if not process_type:
            return ()

        from bpi2020.common.stable_ids import make_case_record_id

        record_id = make_case_record_id(process_type, case_id)

        if self.index is not None:
            self.index.remember(record_id, case_id)

        self.resolved_case_ids.append(case_id)

        return (record_id,)

    def reset_counters(self) -> None:
        self.calls = 0
        self.resolved_case_ids = []


# ============================================================
# One-case rebuild (Steps 3, 5)
# ============================================================

@dataclass
class PostgresCaseRepresentationBuilder:
    """Rebuilds ONE case from the real tables, batch-identically.

    Order inside ``rebuild`` matters and is deliberate:

    1. read the PREVIOUS content hash and remember it
    2. read only this case's events
    3. build via the batch builder
    4. upsert via the batch statement
    5. return the representation carrying the batch hash

    Step 1 has to precede step 4 or the ledger would compare the new hash
    against itself.
    """

    access: PostgresCaseAccess
    previous_hashes: PreviousHashRegistry = field(
        default_factory=PreviousHashRegistry
    )
    index: Any = None
    rebuild_calls: int = 0
    rebuilt_keys: list[str] = field(default_factory=list)

    def rebuild(self, key: str) -> AIRepresentation | None:
        self.rebuild_calls += 1
        self.rebuilt_keys.append(key)

        case_id = self._case_id_for(key)

        if case_id is None:
            return None

        self.previous_hashes.remember(key, self.access.load_case_hash(key))

        rows = self.access.events_for_case(case_id)

        if not rows:
            # Every event of the case is gone. The case no longer exists, so
            # its stored row and its vector must go too - otherwise the index
            # keeps answering from content that is not there any more.
            self.access.delete_case(key)
            return None

        document = build_case_document(rows)

        self.access.upsert_case(
            case_record_id=document["case_record_id"],
            case_id=document["case_id"],
            payload=document,
            content_hash=document["content_hash"],
            changed=True,
        )

        return AIRepresentation(
            representation_id=document["case_record_id"],
            entity_type="case",
            text_for_ai=document["case_summary"],
            content=dict(document["case_json"]),
            # The AUTHORITATIVE hash, produced by the batch builder. Never
            # recomputed here under a different formula.
            content_hash=document["content_hash"],
        )

    def _case_id_for(self, key: str) -> str | None:
        if self.index is not None:
            resolved = self.index.resolve(key)
            if resolved:
                return resolved

        row = self.access.load_case_row(key)
        if row and row.get("case_id"):
            return str(row["case_id"])

        # Last resort: the normalized tail of the record id. Correct only when
        # the source case id was already normalized, which is why the index and
        # the stored row are consulted first.
        return key.rsplit(":", 1)[-1] if ":" in key else key

    def reset_counters(self) -> None:
        self.rebuild_calls = 0
        self.rebuilt_keys = []


@dataclass
class PostgresCaseHashLedger:
    """Serves the hash a case had before this run rebuilt it (Step 5)."""

    access: PostgresCaseAccess
    previous_hashes: PreviousHashRegistry = field(
        default_factory=PreviousHashRegistry
    )

    def get_hash(self, representation_id: str) -> str | None:
        if representation_id in self.previous_hashes.hashes:
            return self.previous_hashes.get(representation_id)

        # Not rebuilt in this run - the stored value IS the previous value.
        return self.access.load_case_hash(representation_id)

    def set_hash(self, representation_id: str, content_hash: str) -> None:
        # The upsert already stored it. Recording it again would be a second
        # source of truth for the same fact.
        self.previous_hashes.forget(representation_id)

    def forget(self, representation_id: str) -> None:
        self.previous_hashes.forget(representation_id)


# ============================================================
# Real embedding adapter (Step 6)
# ============================================================

@dataclass
class BpiEmbeddingUpdater:
    """Embeds ONE affected case with the repository's real model.

    Reuses ``generate_and_store_embeddings.build_embedding_text`` so the text
    sent to the model is the same text the batch embedder would send. The model
    is loaded lazily and once, because loading it per record would dominate the
    cost of an incremental run.

    The batch script is untouched: this adds a single-record path beside it, it
    does not modify or replace it.
    """

    access: PostgresCaseAccess
    model_id: str | None = None
    _model: Any = None
    calls: int = 0
    embedded_ids: list[str] = field(default_factory=list)

    def _load_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            from bpi2020.common.config import EmbeddingSettings

            model_id = self.model_id or EmbeddingSettings.from_env().model_id
            self._model = SentenceTransformer(model_id)

        return self._model

    def embed(self, representation: AIRepresentation) -> EmbeddingResult:
        from bpi2020.embeddings.generate_and_store_embeddings import (
            build_embedding_text,
        )

        self.calls += 1
        self.embedded_ids.append(representation.representation_id)

        case_row = self.access.load_case_row(representation.representation_id)

        if case_row is None:  # pragma: no cover - rebuild always writes first
            raise RuntimeError(
                "the case row disappeared between rebuild and embedding: "
                f"{representation.representation_id}"
            )

        unified = build_unified_case_record(case_row)
        text = build_embedding_text(unified)

        model = self._load_model()
        vector = model.encode(text, show_progress_bar=False).tolist()

        return EmbeddingResult(
            representation_id=representation.representation_id,
            content_hash=representation.resolved_hash(),
            vector=tuple(float(value) for value in vector),
            model_id=self.model_id or getattr(model, "_model_id", "unknown"),
            dimensions=len(vector),
        )


# ============================================================
# Real vector adapter (Step 7)
# ============================================================

@dataclass
class QdrantCaseVectorStore:
    """Writes ONE case vector into the EXISTING Qdrant collection.

    No new collection, and no new identity scheme: the point id comes from the
    frozen ``make_qdrant_point_id(case_record_id)``, so updating a case replaces
    its point rather than adding another. The payload is built by the existing
    ``make_qdrant_payload``.
    """

    client: Any
    access: PostgresCaseAccess
    collection_name: str | None = None
    upsert_calls: int = 0
    delete_calls: int = 0

    def _collection(self) -> str:
        if self.collection_name:
            return self.collection_name

        from bpi2020.common.config import get_vector_collection

        return get_vector_collection()

    def upsert(
        self, representation: AIRepresentation, embedding: EmbeddingResult
    ) -> bool:
        from qdrant_client.models import PointStruct

        from bpi2020.embeddings.generate_and_store_embeddings import (
            make_qdrant_payload,
        )

        self.upsert_calls += 1

        case_row = self.access.load_case_row(representation.representation_id)
        unified = build_unified_case_record(case_row or {})
        point_id = make_qdrant_point_id(representation.representation_id)

        self.client.upsert(
            collection_name=self._collection(),
            points=[
                PointStruct(
                    id=point_id,
                    vector=list(embedding.vector or ()),
                    payload=make_qdrant_payload(unified),
                )
            ],
        )

        # Mirror the batch embedder's write-back so the two paths leave the
        # row in the same state.
        self.access.mark_embedded(representation.representation_id, point_id)

        return True

    def delete(self, vector_id: str) -> bool:
        self.delete_calls += 1

        self.client.delete(
            collection_name=self._collection(), points_selector=[vector_id]
        )

        return True


def qdrant_point_count(client: Any, collection_name: str | None = None) -> int:
    """Current point count, for the no-duplicate-vector proof."""
    if collection_name is None:
        from bpi2020.common.config import get_vector_collection

        collection_name = get_vector_collection()

    return int(client.get_collection(collection_name).points_count or 0)


__all__ = [
    "CLEANED_EVENTS_TABLE",
    "AI_READY_CASES_TABLE",
    "build_case_document",
    "make_qdrant_point_id",
    "build_unified_case_record",
    "PreviousHashRegistry",
    "PostgresCaseAccess",
    "PostgresAffectedCaseResolver",
    "PostgresCaseRepresentationBuilder",
    "PostgresCaseHashLedger",
    "BpiEmbeddingUpdater",
    "QdrantCaseVectorStore",
    "qdrant_point_count",
]
