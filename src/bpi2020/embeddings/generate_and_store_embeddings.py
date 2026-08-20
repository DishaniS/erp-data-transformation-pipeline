"""Generate embeddings and upload the unified BPI knowledge base to Qdrant.

Identity and linkage guarantees (Phase 0)
----------------------------------------
* Qdrant point IDs are UUIDv5 values derived from the unified ``record_id``,
  which is a stable business key. They are never derived from a PostgreSQL
  SERIAL, so a rerun updates the same point instead of creating a duplicate.
* PostgreSQL status updates match on the stable key
  (``case_record_id`` / ``document_record_id``), never on ``id``.
* Every UPDATE asserts that exactly one row was affected. Zero rows raises
  EMBEDDING_SOURCE_RECORD_NOT_FOUND; more than one raises
  EMBEDDING_SOURCE_RECORD_AMBIGUOUS.
* The run cannot report success while any linkage update failed.
"""

import argparse
import json
import re
import sys
from itertools import chain
from pathlib import Path
from typing import Any, Iterator

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sqlalchemy import text
from sqlalchemy.engine import Engine


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bpi2020.common.config import (
    EmbeddingSettings,
    PostgresSettings,
    get_bool_setting,
    get_vector_collection,
)
from bpi2020.common.health import check_postgres, check_qdrant
from bpi2020.common.stable_ids import make_qdrant_point_id
from bpi2020.qdrant_connection import QdrantSettings


UNIFIED_JSONL_PATH = (
    PROJECT_ROOT
    / "data"
    / "bpi2020"
    / "unified"
    / "bpi2020_unified_ai_knowledge_base.jsonl"
)

EMBEDDING_SETTINGS = EmbeddingSettings.from_env()
QDRANT_COLLECTION = get_vector_collection()
EMBEDDING_MODEL = EMBEDDING_SETTINGS.model_id
DEFAULT_BATCH_SIZE = EMBEDDING_SETTINGS.batch_size

# Pre-Phase-0 unified files identified records as "case_<serial>" /
# "document_<serial>". Those values are worthless as linkage keys because the
# SERIAL changes on every table rebuild, so they are rejected outright.
_LEGACY_SERIAL_RECORD_ID = re.compile(r"^(case|document)_\d+$")

_ai_engine: Engine | None = None
_pipeline_db: PostgresSettings | None = None


class EmbeddingLinkageError(RuntimeError):
    """Raised when a vector cannot be tied back to its PostgreSQL source row."""


def get_pipeline_db() -> PostgresSettings:
    """Resolve PostgreSQL settings lazily.

    Deferred so that importing this module - which the identity unit tests do -
    never requires database credentials, and so --skip-postgres works on a
    machine with no PostgreSQL configuration at all.
    """
    global _pipeline_db
    if _pipeline_db is None:
        _pipeline_db = PostgresSettings.pipeline()
    return _pipeline_db


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream the unified BPI knowledge base into Qdrant Cloud or local Qdrant."
    )
    parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Records embedded and uploaded per batch (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        help="Upload only the first N records; useful for validating a new cloud collection.",
    )
    parser.add_argument(
        "--recreate-collection",
        action=argparse.BooleanOptionalAction,
        default=get_bool_setting("VECTOR_DB_RECREATE_COLLECTION"),
        help="Delete and recreate the collection before upload (default: false).",
    )
    parser.add_argument(
        "--skip-postgres",
        action="store_true",
        help="Upload JSONL records without updating PostgreSQL statuses or logs.",
    )
    return parser.parse_args()


def get_ai_engine() -> Engine:
    """Create the PostgreSQL engine only when status updates are requested."""
    global _ai_engine
    if _ai_engine is None:
        _ai_engine = get_pipeline_db().create_engine()
    return _ai_engine


def iter_record_batches(
    batch_size: int,
    limit: int | None = None,
) -> Iterator[list[dict[str, Any]]]:
    """Read the large unified JSONL file without loading it all into memory."""
    if not UNIFIED_JSONL_PATH.exists():
        raise FileNotFoundError(
            f"Unified JSONL file not found: {UNIFIED_JSONL_PATH}\n"
            "Run build_unified_bpi_knowledge_base.py first."
        )

    batch: list[dict[str, Any]] = []
    loaded = 0

    with UNIFIED_JSONL_PATH.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if limit is not None and loaded >= limit:
                break

            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {UNIFIED_JSONL_PATH} at line {line_number}: {exc}"
                ) from exc

            batch.append(record)
            loaded += 1

            if len(batch) >= batch_size:
                yield batch
                batch = []

    if batch:
        yield batch


def build_embedding_text(record: dict[str, Any]) -> str:
    """Combine the searchable fields into the text sent to the embedding model."""
    metadata = record.get("metadata") or {}
    process_type = metadata.get("process_type") or record.get("process_type")
    document_type = metadata.get("document_type")

    parts = [
        f"Record type: {record.get('record_type', '')}",
        f"Title: {record.get('title', '')}",
    ]
    if process_type:
        parts.append(f"Process type: {process_type}")
    if document_type:
        parts.append(f"Document type: {document_type}")
    parts.append(f"Content: {record.get('text_for_ai', '')}")
    return "\n".join(parts)


def make_qdrant_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Keep searchable source text and selected metadata in the Qdrant payload."""
    metadata = record.get("metadata") or {}
    record_id = resolve_record_id(record)
    payload = {
        # Authoritative linkage back to PostgreSQL.
        "record_id": record_id,
        "unified_record_id": record_id,
        "record_type": record.get("record_type"),
        "source_system": record.get("source_system"),
        "source_entity": record.get("source_entity"),
        "stable_source_key": record.get("stable_source_key"),
        "content_hash": record.get("content_hash"),
        "schema_version": record.get("schema_version"),
        "source_table": record.get("source_table"),
        # Traceability only. Never resolve a record through this value.
        "source_record_id": record.get("source_record_id"),
        "title": record.get("title"),
        "primary_reference": record.get("primary_reference"),
        "text_for_ai": record.get("text_for_ai"),
    }

    for key in [
        "case_id",
        "process_type",
        "total_events",
        "start_timestamp",
        "end_timestamp",
        "document_id",
        "document_name",
        "document_type",
        "source_file_path",
        "text_length",
    ]:
        value = metadata.get(key)
        if value is not None:
            payload[key] = value

    if "process_type" not in payload and record.get("process_type") is not None:
        payload["process_type"] = record["process_type"]

    return payload


def resolve_record_id(record: dict[str, Any]) -> str:
    """Return the record's stable cross-layer key, or fail loudly.

    Accepts ``unified_record_id`` as an alias because Phase 0 writes the same
    stable value into both fields, but rejects the legacy ``case_<serial>``
    form: deriving vector identity from a PostgreSQL SERIAL is exactly the
    defect this stage exists to prevent.
    """
    record_id = record.get("record_id") or record.get("unified_record_id")

    if not record_id:
        raise EmbeddingLinkageError(
            "EMBEDDING_SOURCE_RECORD_NOT_FOUND: a unified record has no record_id. "
            f"Offending record keys: {sorted(record)[:12]}. "
            "Rebuild the unified knowledge base with "
            "build_unified_bpi_knowledge_base.py."
        )

    record_id = str(record_id)

    if _LEGACY_SERIAL_RECORD_ID.match(record_id):
        raise EmbeddingLinkageError(
            f"EMBEDDING_STALE_SERIAL_RECORD_ID: unified record_id {record_id!r} is a "
            "pre-Phase-0 identifier derived from a PostgreSQL SERIAL. Such ids "
            "change whenever the source table is rebuilt and must not be used as "
            "vector identity. Re-run build_ai_ready_cases.py / parse_bpi_documents.py "
            "and then build_unified_bpi_knowledge_base.py."
        )

    return record_id


def make_point_id(record: dict[str, Any]) -> str:
    """Derive the deterministic Qdrant point ID from the stable record_id."""
    return make_qdrant_point_id(resolve_record_id(record))


def ensure_qdrant_collection(
    client: QdrantClient,
    collection_name: str,
    vector_size: int,
    recreate: bool,
) -> bool:
    """Create or validate the collection; return True if it was replaced/created."""
    exists = client.collection_exists(collection_name)

    if exists and recreate:
        print(f"Deleting Qdrant collection: {collection_name}")
        client.delete_collection(collection_name=collection_name)
        exists = False

    if not exists:
        print(f"Creating Qdrant collection: {collection_name}")
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        return True

    collection = client.get_collection(collection_name)
    vectors_config = collection.config.params.vectors
    existing_size = getattr(vectors_config, "size", None)
    if existing_size is None:
        raise RuntimeError(
            f"Collection '{collection_name}' uses named vectors; this pipeline expects one unnamed vector."
        )
    if existing_size != vector_size:
        raise RuntimeError(
            f"Collection '{collection_name}' has vector size {existing_size}, but model "
            f"'{EMBEDDING_MODEL}' produces {vector_size}. Use --recreate-collection only "
            "if deleting the existing collection is intended."
        )

    print(f"Using existing Qdrant collection: {collection_name}")
    return False


def reset_embedding_statuses() -> None:
    with get_ai_engine().begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ai_ready_cases
                SET embedding_status = 'pending', qdrant_point_id = NULL
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE ai_ready_documents
                SET embedding_status = 'pending', qdrant_point_id = NULL
                """
            )
        )


def update_postgres_embedding_status(
    records: list[dict[str, Any]],
    point_ids: list[str],
) -> int:
    """Mark source rows embedded, matching on the stable key only.

    Returns the number of rows successfully linked. Raises
    ``EmbeddingLinkageError`` the moment a record cannot be matched to exactly
    one source row, so a vector can never exist in Qdrant while PostgreSQL
    quietly believes nothing happened.
    """
    update_case_sql = text(
        """
        UPDATE ai_ready_cases
        SET embedding_status = 'completed',
            qdrant_point_id = :qdrant_point_id,
            updated_at = CURRENT_TIMESTAMP
        WHERE case_record_id = :record_id
        """
    )
    update_document_sql = text(
        """
        UPDATE ai_ready_documents
        SET embedding_status = 'completed',
            qdrant_point_id = :qdrant_point_id,
            updated_at = CURRENT_TIMESTAMP
        WHERE document_record_id = :record_id
        """
    )

    statements = {
        "erp_case": (update_case_sql, "ai_ready_cases", "case_record_id"),
        "erp_document": (update_document_sql, "ai_ready_documents", "document_record_id"),
    }

    linked = 0

    with get_ai_engine().begin() as connection:
        for record, point_id in zip(records, point_ids):
            record_id = resolve_record_id(record)
            record_type = record.get("record_type")

            statement = statements.get(record_type)
            if statement is None:
                raise EmbeddingLinkageError(
                    f"EMBEDDING_UNKNOWN_RECORD_TYPE: record {record_id!r} has "
                    f"record_type {record_type!r}, which maps to no source table."
                )

            update_sql, table_name, key_column = statement

            result = connection.execute(
                update_sql,
                {"record_id": record_id, "qdrant_point_id": point_id},
            )

            if result.rowcount == 0:
                raise EmbeddingLinkageError(
                    f"EMBEDDING_SOURCE_RECORD_NOT_FOUND: no row in {table_name} has "
                    f"{key_column} = {record_id!r}. The vector was written to Qdrant "
                    "but PostgreSQL has no matching source record, so the two stores "
                    "would diverge. Rebuild the unified knowledge base from the "
                    "current database contents and re-run."
                )

            if result.rowcount > 1:
                raise EmbeddingLinkageError(
                    f"EMBEDDING_SOURCE_RECORD_AMBIGUOUS: {result.rowcount} rows in "
                    f"{table_name} share {key_column} = {record_id!r}. The stable key "
                    "must be unique. Check the unique index on that column."
                )

            linked += 1

    return linked


def log_embedding_stage(total_records: int, status: str, message: str) -> None:
    log_sql = text(
        """
        INSERT INTO transformation_logs (
            pipeline_stage, source_database, target_database, source_table,
            total_input_records, total_output_records, status, message
        )
        VALUES (
            :pipeline_stage, :source_database, :target_database, :source_table,
            :total_input_records, :total_output_records, :status, :message
        )
        """
    )
    with get_ai_engine().begin() as connection:
        connection.execute(
            log_sql,
            {
                "pipeline_stage": "generate_and_store_embeddings",
                "source_database": get_pipeline_db().database,
                "target_database": "qdrant",
                "source_table": "bpi2020_unified_ai_knowledge_base",
                "total_input_records": total_records,
                "total_output_records": total_records if status == "success" else 0,
                "status": status,
                "message": message,
            },
        )


def main() -> None:
    args = parse_args()
    qdrant_settings = QdrantSettings.from_env()

    print("\nStarting embedding generation and Qdrant upload...")
    print(f"Unified file      : {UNIFIED_JSONL_PATH}")
    print(f"Embedding model   : {EMBEDDING_MODEL}")
    print(f"Qdrant target     : {qdrant_settings.target}")
    print(f"Qdrant collection : {QDRANT_COLLECTION}")
    print(f"Batch size        : {args.batch_size}")
    print(f"Collection mode   : {'recreate' if args.recreate_collection else 'upsert'}")
    print(f"PostgreSQL updates: {'disabled' if args.skip_postgres else 'enabled'}")
    if args.limit:
        print(f"Record limit      : {args.limit}")

    print("\nDependency checks")
    print("-" * 60)
    print(check_qdrant(qdrant_settings, QDRANT_COLLECTION))
    if args.skip_postgres:
        print("[SKIP] PostgreSQL: --skip-postgres, embedding statuses will not be updated")
    else:
        print(
            check_postgres(
                get_pipeline_db(), required_tables=("ai_ready_cases", "ai_ready_documents")
            )
        )

    batches = iter_record_batches(args.batch_size, args.limit)
    try:
        first_batch = next(batches)
    except StopIteration:
        print("\nNo unified records found.")
        if not args.skip_postgres:
            log_embedding_stage(0, "failed", "No unified records found for embedding generation.")
        return

    print("\nLoading embedding model...")
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBEDDING_MODEL)
    client = qdrant_settings.create_client()
    collection_ready = False
    total_uploaded = 0
    total_linked = 0

    for records in chain([first_batch], batches):
        embedding_texts = [build_embedding_text(record) for record in records]
        embeddings = model.encode(
            embedding_texts,
            batch_size=args.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )

        if not collection_ready:
            vector_size = int(embeddings.shape[1])
            print(f"Embedding vector size: {vector_size}")
            collection_changed = ensure_qdrant_collection(
                client,
                QDRANT_COLLECTION,
                vector_size,
                args.recreate_collection,
            )
            if collection_changed and not args.skip_postgres:
                print("Resetting PostgreSQL embedding statuses...")
                reset_embedding_statuses()
            collection_ready = True

        point_ids = [make_point_id(record) for record in records]
        points = [
            PointStruct(
                id=point_id,
                vector=embedding.tolist(),
                payload=make_qdrant_payload(record),
            )
            for record, point_id, embedding in zip(records, point_ids, embeddings)
        ]

        client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=points,
            wait=True,
        )
        total_uploaded += len(points)

        if not args.skip_postgres:
            # Any linkage failure raises, which aborts the run before it can
            # claim success. The failure is logged to transformation_logs so the
            # partial upload is visible afterwards.
            try:
                total_linked += update_postgres_embedding_status(records, point_ids)
            except EmbeddingLinkageError as exc:
                log_embedding_stage(
                    total_uploaded,
                    "failed",
                    f"Cross-store linkage failure after {total_uploaded} vectors: {exc}",
                )
                raise

        print(f"Uploaded {total_uploaded} vectors")

    if args.skip_postgres:
        print(
            f"\nCompleted. Uploaded {total_uploaded} vectors. "
            "PostgreSQL linkage was NOT updated (--skip-postgres), so embedding_status "
            "in the database does not reflect this run."
        )
        return

    if total_linked != total_uploaded:
        # Defensive: update_postgres_embedding_status raises per record, so this
        # should be unreachable. It exists so a future change cannot quietly
        # reintroduce a partial-linkage success.
        message = (
            f"Uploaded {total_uploaded} vectors but linked only {total_linked} "
            "PostgreSQL rows."
        )
        log_embedding_stage(total_uploaded, "failed", message)
        raise EmbeddingLinkageError(f"EMBEDDING_LINKAGE_INCOMPLETE: {message}")

    log_embedding_stage(
        total_uploaded,
        "success",
        f"Stored {total_uploaded} embeddings in Qdrant collection {QDRANT_COLLECTION} "
        f"and linked {total_linked} PostgreSQL source rows by stable key.",
    )

    print(
        f"\nCompleted successfully. Uploaded {total_uploaded} vectors and linked "
        f"{total_linked} PostgreSQL rows by stable key."
    )


if __name__ == "__main__":
    main()
