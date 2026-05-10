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
"""

import os
import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


# ============================================================
# Paths
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "unified"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")


# ============================================================
# Database configuration
# ============================================================

AI_DB_HOST = os.getenv("AI_DB_HOST", "localhost")
AI_DB_PORT = os.getenv("AI_DB_PORT", "5432")
AI_DB_NAME = os.getenv("AI_DB_NAME", "erp_ai_native_db")
AI_DB_USER = os.getenv("AI_DB_USER", "postgres")
AI_DB_PASSWORD = os.getenv("AI_DB_PASSWORD", "postgres")

AI_DATABASE_URL = (
    f"postgresql+psycopg2://{AI_DB_USER}:{AI_DB_PASSWORD}"
    f"@{AI_DB_HOST}:{AI_DB_PORT}/{AI_DB_NAME}"
)

ai_engine = create_engine(AI_DATABASE_URL)


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

    record_id = f"case_{row.get('id')}"
    case_id = str(row.get("case_id"))
    process_type = str(row.get("process_type"))

    text_for_ai = row.get("case_summary") or case_json.get("ai_text") or ""

    unified_record = {
        "unified_record_id": record_id,
        "record_type": "erp_case",
        "source_layer": "ai_ready_cases",
        "source_database": AI_DB_NAME,
        "source_table": "ai_ready_cases",
        "source_record_id": safe_value(row.get("id")),

        "title": f"ERP Case {case_id} - {process_type}",
        "primary_reference": case_id,
        "process_type": process_type,

        "text_for_ai": text_for_ai,

        "metadata": {
            "case_id": case_id,
            "process_type": process_type,
            "total_events": safe_value(row.get("total_events")),
            "start_timestamp": safe_value(row.get("start_timestamp")),
            "end_timestamp": safe_value(row.get("end_timestamp")),
            "embedding_status": safe_value(row.get("embedding_status")),
            "qdrant_point_id": safe_value(row.get("qdrant_point_id")),
        },

        "content_json": case_json,
    }

    return unified_record


def build_document_knowledge_record(row: pd.Series) -> Dict[str, Any]:
    """
    Convert one ai_ready_documents row into a unified knowledge record.
    """
    document_json = safe_json_load(row.get("document_json"))

    record_id = f"document_{row.get('id')}"
    document_id = str(row.get("document_id"))
    document_name = str(row.get("document_name"))
    document_type = str(row.get("document_type"))

    text_for_ai = row.get("text_for_ai") or row.get("extracted_text") or ""

    unified_record = {
        "unified_record_id": record_id,
        "record_type": "erp_document",
        "source_layer": "ai_ready_documents",
        "source_database": AI_DB_NAME,
        "source_table": "ai_ready_documents",
        "source_record_id": safe_value(row.get("id")),

        "title": document_name,
        "primary_reference": document_id,
        "document_type": document_type,

        "text_for_ai": text_for_ai,

        "metadata": {
            "document_id": document_id,
            "document_name": document_name,
            "document_type": document_type,
            "source_file_path": safe_value(row.get("source_file_path")),
            "text_length": len(row.get("extracted_text") or ""),
            "embedding_status": safe_value(row.get("embedding_status")),
            "qdrant_point_id": safe_value(row.get("qdrant_point_id")),
        },

        "content_json": document_json,
    }

    return unified_record


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
    print(f"Source DB     : {AI_DB_NAME}")
    print("Source tables : ai_ready_cases, ai_ready_documents")
    print(f"Output folder : {OUTPUT_DIR}")

    case_query = """
        SELECT
            id,
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
        ORDER BY id;
    """

    document_query = """
        SELECT
            id,
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
        ORDER BY id;
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


if __name__ == "__main__":
    main()