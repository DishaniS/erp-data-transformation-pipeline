"""
Generate embeddings and store unified BPI ERP knowledge records in Qdrant.

Source:
    data/bpi2020/unified/bpi2020_unified_ai_knowledge_base.jsonl

Targets:
    Qdrant collection: bpi2020_erp_knowledge
    PostgreSQL:
        ai_ready_cases.embedding_status
        ai_ready_cases.qdrant_point_id
        ai_ready_documents.embedding_status
        ai_ready_documents.qdrant_point_id

Purpose:
    Converts the unified AI-ready ERP knowledge layer into vector-searchable
    representations for semantic retrieval and future RAG integration.
"""

import os
import json
import uuid
from pathlib import Path
from typing import Dict, List, Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct


# ============================================================
# Paths
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]

UNIFIED_JSONL_PATH = (
    PROJECT_ROOT
    / "data"
    / "bpi2020"
    / "unified"
    / "bpi2020_unified_ai_knowledge_base.jsonl"
)

load_dotenv(PROJECT_ROOT / ".env")


# ============================================================
# Environment config
# ============================================================

AI_DB_HOST = os.getenv("AI_DB_HOST", "localhost")
AI_DB_PORT = os.getenv("AI_DB_PORT", "5432")
AI_DB_NAME = os.getenv("AI_DB_NAME", "erp_ai_native_db")
AI_DB_USER = os.getenv("AI_DB_USER", "postgres")
AI_DB_PASSWORD = os.getenv("AI_DB_PASSWORD", "postgres")

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "bpi2020_erp_knowledge")

EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2"
)

EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "64"))


AI_DATABASE_URL = (
    f"postgresql+psycopg2://{AI_DB_USER}:{AI_DB_PASSWORD}"
    f"@{AI_DB_HOST}:{AI_DB_PORT}/{AI_DB_NAME}"
)

ai_engine = create_engine(AI_DATABASE_URL)


# ============================================================
# Load unified records
# ============================================================

def load_unified_records() -> List[Dict[str, Any]]:
    """
    Load unified AI-ready ERP records from JSONL.
    """
    if not UNIFIED_JSONL_PATH.exists():
        raise FileNotFoundError(
            f"Unified JSONL file not found: {UNIFIED_JSONL_PATH}\n"
            "Run build_unified_bpi_knowledge_base.py first."
        )

    records = []

    with open(UNIFIED_JSONL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            records.append(json.loads(line))

    return records


def build_embedding_text(record: Dict[str, Any]) -> str:
    """
    Build the text that will be embedded.

    This keeps the vector meaningful by combining title, record type,
    process/document context, and the AI-ready text field.
    """
    record_type = record.get("record_type", "")
    title = record.get("title", "")
    text_for_ai = record.get("text_for_ai", "")

    metadata = record.get("metadata", {}) or {}

    process_type = metadata.get("process_type", "")
    document_type = metadata.get("document_type", "")

    parts = [
        f"Record type: {record_type}",
        f"Title: {title}",
    ]

    if process_type:
        parts.append(f"Process type: {process_type}")

    if document_type:
        parts.append(f"Document type: {document_type}")

    parts.append(f"Content: {text_for_ai}")

    return "\n".join(parts)


def make_qdrant_payload(record: Dict[str, Any], embedding_text: str) -> Dict[str, Any]:
    """
    Store only useful searchable metadata in Qdrant payload.
    Avoid storing the full huge content_json inside Qdrant.
    """
    metadata = record.get("metadata", {}) or {}

    payload = {
        "unified_record_id": record.get("unified_record_id"),
        "record_type": record.get("record_type"),
        "source_table": record.get("source_table"),
        "source_record_id": record.get("source_record_id"),
        "title": record.get("title"),
        "primary_reference": record.get("primary_reference"),
        "text_for_ai": record.get("text_for_ai"),
        "embedding_text": embedding_text,
    }

    # Add selected metadata fields.
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
        if key in metadata:
            payload[key] = metadata.get(key)

    return payload


# ============================================================
# Qdrant functions
# ============================================================

def recreate_qdrant_collection(
    client: QdrantClient,
    collection_name: str,
    vector_size: int,
):
    """
    Recreate collection for reproducible prototype runs.
    """
    existing_collections = client.get_collections().collections
    existing_names = [collection.name for collection in existing_collections]

    if collection_name in existing_names:
        print(f"Deleting existing Qdrant collection: {collection_name}")
        client.delete_collection(collection_name=collection_name)

    print(f"Creating Qdrant collection: {collection_name}")

    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(
            size=vector_size,
            distance=Distance.COSINE,
        ),
    )


def upsert_points_to_qdrant(
    client: QdrantClient,
    collection_name: str,
    records: List[Dict[str, Any]],
    embeddings,
) -> Dict[int, str]:
    """
    Insert points into Qdrant.

    Returns:
        Mapping from source_record_id to qdrant_point_id.
    """
    source_to_point_id = {}

    points = []

    for record, embedding in zip(records, embeddings):
        point_id = str(uuid.uuid4())
        embedding_text = build_embedding_text(record)
        payload = make_qdrant_payload(record, embedding_text)

        source_record_id = record.get("source_record_id")

        if source_record_id is not None:
            source_to_point_id[int(source_record_id)] = point_id

        points.append(
            PointStruct(
                id=point_id,
                vector=embedding.tolist(),
                payload=payload,
            )
        )

    for start in range(0, len(points), EMBEDDING_BATCH_SIZE):
        batch = points[start:start + EMBEDDING_BATCH_SIZE]

        client.upsert(
            collection_name=collection_name,
            points=batch,
        )

        print(f"   Uploaded {min(start + EMBEDDING_BATCH_SIZE, len(points))}/{len(points)} vectors")

    return source_to_point_id


# ============================================================
# PostgreSQL status updates
# ============================================================

def reset_embedding_statuses():
    """
    Reset statuses before full rebuild.
    """
    with ai_engine.begin() as connection:
        connection.execute(
            text("""
                UPDATE ai_ready_cases
                SET embedding_status = 'pending',
                    qdrant_point_id = NULL;
            """)
        )

        connection.execute(
            text("""
                UPDATE ai_ready_documents
                SET embedding_status = 'pending',
                    qdrant_point_id = NULL;
            """)
        )


def update_postgres_embedding_status(records: List[Dict[str, Any]], point_ids: List[str]):
    """
    Update ai_ready_cases and ai_ready_documents with Qdrant point IDs.
    """
    update_case_sql = text("""
        UPDATE ai_ready_cases
        SET embedding_status = 'completed',
            qdrant_point_id = :qdrant_point_id
        WHERE id = :source_record_id;
    """)

    update_document_sql = text("""
        UPDATE ai_ready_documents
        SET embedding_status = 'completed',
            qdrant_point_id = :qdrant_point_id
        WHERE id = :source_record_id;
    """)

    with ai_engine.begin() as connection:
        for record, point_id in zip(records, point_ids):
            source_record_id = record.get("source_record_id")
            record_type = record.get("record_type")

            if source_record_id is None:
                continue

            if record_type == "erp_case":
                connection.execute(
                    update_case_sql,
                    {
                        "source_record_id": source_record_id,
                        "qdrant_point_id": point_id,
                    },
                )

            elif record_type == "erp_document":
                connection.execute(
                    update_document_sql,
                    {
                        "source_record_id": source_record_id,
                        "qdrant_point_id": point_id,
                    },
                )


def log_embedding_stage(total_records: int, status: str, message: str):
    """
    Log embedding generation stage.
    """
    log_sql = text("""
        INSERT INTO transformation_logs (
            pipeline_stage,
            source_database,
            target_database,
            source_table,
            total_input_records,
            total_output_records,
            status,
            message
        )
        VALUES (
            :pipeline_stage,
            :source_database,
            :target_database,
            :source_table,
            :total_input_records,
            :total_output_records,
            :status,
            :message
        )
    """)

    with ai_engine.begin() as connection:
        connection.execute(
            log_sql,
            {
                "pipeline_stage": "generate_and_store_embeddings",
                "source_database": AI_DB_NAME,
                "target_database": "qdrant",
                "source_table": "bpi2020_unified_ai_knowledge_base",
                "total_input_records": total_records,
                "total_output_records": total_records if status == "success" else 0,
                "status": status,
                "message": message,
            },
        )


# ============================================================
# Main
# ============================================================

def main():
    print("\nStarting embedding generation and Qdrant storage...")
    print(f"Unified file      : {UNIFIED_JSONL_PATH}")
    print(f"Embedding model   : {EMBEDDING_MODEL}")
    print(f"Qdrant collection : {QDRANT_COLLECTION}")
    print(f"Qdrant host/port  : {QDRANT_HOST}:{QDRANT_PORT}")

    records = load_unified_records()

    if not records:
        print("\nNo unified records found.")
        log_embedding_stage(
            total_records=0,
            status="failed",
            message="No unified records found for embedding generation.",
        )
        return

    print(f"\nLoaded unified records: {len(records)}")

    embedding_texts = [build_embedding_text(record) for record in records]

    print("\nLoading embedding model...")
    model = SentenceTransformer(EMBEDDING_MODEL)

    print("\nGenerating embeddings...")
    embeddings = model.encode(
        embedding_texts,
        batch_size=EMBEDDING_BATCH_SIZE,
        show_progress_bar=True,
        normalize_embeddings=True,
    )

    vector_size = embeddings.shape[1]
    print(f"\nEmbedding vector size: {vector_size}")

    print("\nConnecting to Qdrant...")
    qdrant_client = QdrantClient(
        host=QDRANT_HOST,
        port=QDRANT_PORT,
    )

    recreate_qdrant_collection(
        client=qdrant_client,
        collection_name=QDRANT_COLLECTION,
        vector_size=vector_size,
    )

    print("\nResetting PostgreSQL embedding statuses...")
    reset_embedding_statuses()

    print("\nUploading vectors to Qdrant...")

    points = []
    point_ids = []

    for record, embedding_text, embedding in zip(records, embedding_texts, embeddings):
        point_id = str(uuid.uuid4())
        point_ids.append(point_id)

        payload = make_qdrant_payload(record, embedding_text)

        points.append(
            PointStruct(
                id=point_id,
                vector=embedding.tolist(),
                payload=payload,
            )
        )

    for start in range(0, len(points), EMBEDDING_BATCH_SIZE):
        batch = points[start:start + EMBEDDING_BATCH_SIZE]

        qdrant_client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=batch,
        )

        print(f"   Uploaded {min(start + EMBEDDING_BATCH_SIZE, len(points))}/{len(points)} vectors")

    print("\nUpdating PostgreSQL embedding statuses...")
    update_postgres_embedding_status(records, point_ids)

    log_embedding_stage(
        total_records=len(records),
        status="success",
        message=f"Generated and stored {len(records)} embeddings in Qdrant collection {QDRANT_COLLECTION}.",
    )

    print("\nEmbedding generation and Qdrant storage completed successfully.")


if __name__ == "__main__":
    main()