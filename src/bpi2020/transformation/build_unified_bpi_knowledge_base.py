"""
Build Unified BPI 2020 AI-Ready Knowledge Base

This script combines:
1. Structured ERP case-level records from erp_ai_native_db.ai_ready_cases
2. Unstructured PDF/image document records from erp_ai_native_db.ai_ready_documents

Output:
    data/bpi2020/unified/bpi2020_unified_ai_knowledge_base.json
    data/bpi2020/unified/bpi2020_unified_ai_knowledge_base.jsonl

Purpose:
    Creates one unified AI-ready knowledge layer for later embedding,
    vector storage, semantic search, and RAG.

Linkage contract (Phase 0)
--------------------------
``record_id`` is the ONLY authoritative cross-layer key. It is the stable
identifier owned by the source table (``ai_ready_cases.case_record_id`` or
``ai_ready_documents.document_record_id``) and is derived purely from business
identity, so it survives table rebuilds and SERIAL sequence drift.

``source_record_id`` is still written for backwards compatibility, but it is a
PostgreSQL SERIAL and is explicitly NOT a linkage key. Nothing downstream may
resolve a record through it. Before Phase 0 the unified layer used
``"case_{SERIAL}"`` as its identity, and every rebuild of ai_ready_cases
silently invalidated every previously written file and vector.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from sqlalchemy import text


# ============================================================
# Paths
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bpi2020.common.config import PostgresSettings
from bpi2020.common.health import check_postgres
from bpi2020.common.stable_ids import (
    SOURCE_SYSTEM,
    UNIFIED_SCHEMA_VERSION,
    compute_content_hash,
)

OUTPUT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "unified"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Database configuration
# ============================================================

PIPELINE_DB = PostgresSettings.pipeline()
AI_DB_NAME = PIPELINE_DB.database

ai_engine = PIPELINE_DB.create_engine()


class UnifiedLinkageError(RuntimeError):
    """Raised when a source row cannot supply a stable cross-layer identity."""


# ============================================================
# Helpers
# ============================================================

def safe_json_load(value: Any) -> Dict[str, Any]:
    """
    PostgreSQL JSONB may come as dict or string depending on driver behavior.
    This safely converts it to dict.
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}

    return {}


def safe_value(value: Any):
    """
    Convert pandas/SQL values into JSON-safe values.
    """
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, pd.Timestamp):
        return value.isoformat()

    return value


def build_case_knowledge_record(row: pd.Series) -> Dict[str, Any]:
    """
    Convert one ai_ready_cases row into a unified knowledge record.
    """
    case_json = safe_json_load(row.get("case_json"))

    record_id = row.get("case_record_id")
    case_id = str(row.get("case_id"))
    process_type = str(row.get("process_type"))

    if not record_id:
        raise UnifiedLinkageError(
            f"ai_ready_cases row id={row.get('id')} (case_id={case_id!r}) has no "
            "case_record_id. Run create_ai_native_db_schema.py to migrate, then "
            "rebuild with build_ai_ready_cases.py."
        )

    text_for_ai = row.get("case_summary") or case_json.get("ai_text") or ""

    metadata = {
        "case_id": case_id,
        "process_type": process_type,
        "total_events": safe_value(row.get("total_events")),
        "start_timestamp": safe_value(row.get("start_timestamp")),
        "end_timestamp": safe_value(row.get("end_timestamp")),
        "embedding_status": safe_value(row.get("embedding_status")),
        "qdrant_point_id": safe_value(row.get("qdrant_point_id")),
    }

    unified_record = {
        # Authoritative cross-layer key.
        "record_id": record_id,
        # Backwards-compatible alias carrying the same stable value. Older
        # consumers read unified_record_id; it is no longer a SERIAL.
        "unified_record_id": record_id,
        "record_type": "erp_case",
        "schema_version": UNIFIED_SCHEMA_VERSION,

        "source_system": SOURCE_SYSTEM,
        "source_entity": "ai_ready_cases",
        "stable_source_key": record_id,
        "source_layer": "ai_ready_cases",
        "source_database": AI_DB_NAME,
        "source_table": "ai_ready_cases",
        # NOT a linkage key. PostgreSQL SERIAL, retained for traceability and
        # backwards compatibility only. Resolve records by record_id.
        "source_record_id": safe_value(row.get("id")),

        "title": f"ERP Case {case_id} - {process_type}",
        "primary_reference": case_id,
        "process_type": process_type,

        "text_for_ai": text_for_ai,

        "metadata": metadata,

        # Authoritative hash is the one written by build_ai_ready_cases.py.
        # A fallback is derived here only for a database that was migrated but
        # not yet rebuilt; content_hash_source says which one is in play so the
        # two formulas are never silently confused.
        **_content_hash_fields(
            safe_value(row.get("content_hash")), record_id, text_for_ai, metadata
        ),

        "content_json": case_json,
    }

    return unified_record


def build_document_knowledge_record(row: pd.Series) -> Dict[str, Any]:
    """
    Convert one ai_ready_documents row into a unified knowledge record.
    """
    document_json = safe_json_load(row.get("document_json"))

    record_id = row.get("document_record_id")
    document_id = str(row.get("document_id"))
    document_name = str(row.get("document_name"))
    document_type = str(row.get("document_type"))

    if not record_id:
        raise UnifiedLinkageError(
            f"ai_ready_documents row id={row.get('id')} "
            f"(document_id={document_id!r}) has no document_record_id. Run "
            "create_ai_native_db_schema.py to migrate, then rebuild with "
            "parse_bpi_documents.py."
        )

    text_for_ai = row.get("text_for_ai") or row.get("extracted_text") or ""

    metadata = {
        "document_id": document_id,
        "document_name": document_name,
        "document_type": document_type,
        "source_file_path": safe_value(row.get("source_file_path")),
        "text_length": len(row.get("extracted_text") or ""),
        "embedding_status": safe_value(row.get("embedding_status")),
        "qdrant_point_id": safe_value(row.get("qdrant_point_id")),
    }

    unified_record = {
        "record_id": record_id,
        "unified_record_id": record_id,
        "record_type": "erp_document",
        "schema_version": UNIFIED_SCHEMA_VERSION,

        "source_system": SOURCE_SYSTEM,
        "source_entity": "ai_ready_documents",
        # The document's own stable business key inside that entity.
        "stable_source_key": document_id,
        "source_layer": "ai_ready_documents",
        "source_database": AI_DB_NAME,
        "source_table": "ai_ready_documents",
        # NOT a linkage key. See the note in build_case_knowledge_record.
        "source_record_id": safe_value(row.get("id")),

        "title": document_name,
        "primary_reference": document_id,
        "document_type": document_type,

        "text_for_ai": text_for_ai,

        "metadata": metadata,

        **_content_hash_fields(
            safe_value(row.get("content_hash")), record_id, text_for_ai, metadata
        ),

        "content_json": document_json,
    }

    return unified_record


def _content_hash_fields(
    source_hash: Any,
    record_id: str,
    text_for_ai: str,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Choose the content hash and record where it came from.

    The producing stage owns the canonical hash. Deriving a different one here
    would be worse than useless for change detection, so when the source hash
    is missing the fallback is labelled rather than passed off as canonical.
    """
    if source_hash:
        return {"content_hash": source_hash, "content_hash_source": "source_table"}

    hashable_metadata = {
        key: value
        for key, value in metadata.items()
        if key not in {"embedding_status", "qdrant_point_id"}
    }

    return {
        "content_hash": compute_content_hash(record_id, text_for_ai, hashable_metadata),
        "content_hash_source": "derived_in_unified_layer",
    }


def save_unified_outputs(records: List[Dict[str, Any]]) -> None:
    """
    Save unified records as JSON and JSONL.
    """
    json_path = OUTPUT_DIR / "bpi2020_unified_ai_knowledge_base.json"
    jsonl_path = OUTPUT_DIR / "bpi2020_unified_ai_knowledge_base.jsonl"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False, default=str)

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    print(f"\nSaved unified JSON : {json_path}")
    print(f"Saved unified JSONL: {jsonl_path}")


def save_summary(records: List[Dict[str, Any]]) -> None:
    """
    Save summary report for documentation/logbook.
    """
    summary = {
        "total_records": len(records),
        "record_type_counts": {},
        "source_tables": {},
    }

    for record in records:
        record_type = record.get("record_type")
        source_table = record.get("source_table")

        summary["record_type_counts"][record_type] = (
            summary["record_type_counts"].get(record_type, 0) + 1
        )

        summary["source_tables"][source_table] = (
            summary["source_tables"].get(source_table, 0) + 1
        )

    summary_path = OUTPUT_DIR / "bpi2020_unified_summary.json"

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)

    print(f"Saved summary JSON : {summary_path}")

    print("\nUnified Knowledge Base Summary")
    print("=" * 50)
    print(f"Total records: {summary['total_records']}")

    print("\nRecord type counts:")
    for key, value in summary["record_type_counts"].items():
        print(f"  {key}: {value}")

    print("\nSource table counts:")
    for key, value in summary["source_tables"].items():
        print(f"  {key}: {value}")


def log_transformation(total_input_records: int, total_output_records: int, status: str, message: str):
    """
    Log unified knowledge base generation.
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
                "pipeline_stage": "build_unified_bpi_knowledge_base",
                "source_database": AI_DB_NAME,
                "target_database": "file_system",
                "source_table": "ai_ready_cases + ai_ready_documents",
                "total_input_records": total_input_records,
                "total_output_records": total_output_records,
                "status": status,
                "message": message,
            },
        )


# ============================================================
# Main
# ============================================================

def main():
    print("\nStarting unified BPI AI-ready knowledge base build...")
    print(f"Source DB     : {PIPELINE_DB.safe_target}")
    print("Source tables : ai_ready_cases, ai_ready_documents")
    print(f"Output folder : {OUTPUT_DIR}")

    print(
        f"\n{check_postgres(PIPELINE_DB, required_tables=('ai_ready_cases', 'ai_ready_documents'))}"
    )

    # Ordered by the stable key, not by the SERIAL, so output ordering is
    # reproducible across rebuilds.
    case_query = """
        SELECT
            id,
            case_record_id,
            content_hash,
            case_id,
            process_type,
            case_summary,
            case_json,
            total_events,
            start_timestamp,
            end_timestamp,
            embedding_status,
            qdrant_point_id
        FROM ai_ready_cases
        ORDER BY case_record_id;
    """

    document_query = """
        SELECT
            id,
            document_record_id,
            content_hash,
            document_id,
            document_type,
            document_name,
            source_file_path,
            extracted_text,
            text_for_ai,
            document_json,
            embedding_status,
            qdrant_point_id
        FROM ai_ready_documents
        ORDER BY document_record_id;
    """

    cases_df = pd.read_sql(case_query, ai_engine)
    documents_df = pd.read_sql(document_query, ai_engine)

    print(f"\nLoaded ERP case records     : {len(cases_df)}")
    print(f"Loaded ERP document records : {len(documents_df)}")

    unified_records: List[Dict[str, Any]] = []

    for _, row in cases_df.iterrows():
        unified_records.append(build_case_knowledge_record(row))

    for _, row in documents_df.iterrows():
        unified_records.append(build_document_knowledge_record(row))

    duplicate_ids = _find_duplicate_record_ids(unified_records)
    if duplicate_ids:
        raise UnifiedLinkageError(
            "Duplicate record_id values in the unified layer: "
            f"{duplicate_ids[:5]} (total {len(duplicate_ids)}). "
            "Refusing to write a knowledge base with ambiguous identity."
        )

    total_input_records = len(cases_df) + len(documents_df)
    total_output_records = len(unified_records)

    if total_output_records == 0:
        print("\nNo records found. Build ai_ready_cases and ai_ready_documents first.")

        log_transformation(
            total_input_records=0,
            total_output_records=0,
            status="failed",
            message="No records found in ai_ready_cases or ai_ready_documents.",
        )
        return

    save_unified_outputs(unified_records)
    save_summary(unified_records)

    log_transformation(
        total_input_records=total_input_records,
        total_output_records=total_output_records,
        status="success",
        message=f"Unified knowledge base created with {total_output_records} records.",
    )

    print("\nUnified BPI AI-ready knowledge base build completed.")


def _find_duplicate_record_ids(records: List[Dict[str, Any]]) -> List[str]:
    """Return record_id values that appear more than once."""
    seen: set = set()
    duplicates: List[str] = []

    for record in records:
        record_id = record.get("record_id")
        if record_id in seen:
            duplicates.append(record_id)
        seen.add(record_id)

    return duplicates


if __name__ == "__main__":
    main()