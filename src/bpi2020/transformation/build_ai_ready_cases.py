"""
Build AI-ready case-level JSON documents from cleaned BPI 2020 ERP event logs.

Source:
    erp_ai_native_db.cleaned_event_logs

Target:
    erp_ai_native_db.ai_ready_cases

This script:
1. Reads cleaned event-level records from cleaned_event_logs.
2. Groups events by process_type + normalized_case_id.
3. Builds one structured AI-ready JSON document per ERP case.
4. Creates a natural-language case_summary for later embedding/RAG.
5. Saves AI-ready JSON/JSONL files.
6. Inserts case-level records into ai_ready_cases.
7. Logs the transformation in transformation_logs.
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Any, Optional

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


# ============================================================
# Paths
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "ai_ready"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")


# ============================================================
# Database Configuration
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
# Helper functions
# ============================================================

def safe_json_load(value: Any) -> Dict[str, Any]:
    """
    record_data may come from PostgreSQL as dict or string.
    This safely converts it to a Python dict.
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


def safe_timestamp(value):
    """
    Convert timestamp to ISO string safely.
    """
    if value is None or pd.isna(value):
        return None

    parsed = pd.to_datetime(value, errors="coerce", utc=True)

    if pd.isna(parsed):
        return None

    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_json_safe(value):
    """
    Make values JSON serializable.
    """
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, pd.Timestamp):
        return safe_timestamp(value)

    return value


def clean_event_record(row: pd.Series) -> Dict[str, Any]:
    """
    Convert one cleaned_event_logs row into a compact event object.
    """
    record_data = safe_json_load(row.get("record_data"))

    event = {
        "event_id": int(row["id"]) if row.get("id") is not None else None,
        "activity": make_json_safe(row.get("normalized_activity")),
        "timestamp": safe_timestamp(row.get("event_timestamp")),
        "source_table": make_json_safe(row.get("source_table")),
        "process_type": make_json_safe(row.get("process_type")),
        "attributes": record_data,
    }

    return event


def calculate_duration_days(start_timestamp: Optional[str], end_timestamp: Optional[str]) -> Optional[float]:
    """
    Calculate case duration in days.
    """
    if not start_timestamp or not end_timestamp:
        return None

    start = pd.to_datetime(start_timestamp, errors="coerce", utc=True)
    end = pd.to_datetime(end_timestamp, errors="coerce", utc=True)

    if pd.isna(start) or pd.isna(end):
        return None

    duration = end - start
    return round(duration.total_seconds() / 86400, 4)


def get_activity_sequence(events: List[Dict[str, Any]]) -> List[str]:
    """
    Extract ordered activity sequence.
    """
    sequence = []

    for event in events:
        activity = event.get("activity")
        if activity and activity not in ["None", "nan"]:
            sequence.append(str(activity))

    return sequence


def get_unique_activities(activity_sequence: List[str]) -> List[str]:
    """
    Extract unique activities while preserving order.
    """
    seen = set()
    unique = []

    for activity in activity_sequence:
        if activity not in seen:
            unique.append(activity)
            seen.add(activity)

    return unique


def build_case_summary(
    case_id: str,
    process_type: str,
    total_events: int,
    start_timestamp: Optional[str],
    end_timestamp: Optional[str],
    activity_sequence: List[str],
    duration_days: Optional[float],
) -> str:
    """
    Create natural-language summary for embedding/RAG.
    """
    unique_activities = get_unique_activities(activity_sequence)

    first_activity = activity_sequence[0] if activity_sequence else "unknown starting activity"
    last_activity = activity_sequence[-1] if activity_sequence else "unknown ending activity"

    summary = (
        f"ERP case {case_id} belongs to the {process_type} process. "
        f"The case contains {total_events} recorded workflow events. "
        f"The process starts with '{first_activity}' and ends with '{last_activity}'. "
    )

    if start_timestamp and end_timestamp:
        summary += (
            f"The case started at {start_timestamp} and ended at {end_timestamp}. "
        )

    if duration_days is not None:
        summary += f"The total case duration is approximately {duration_days} days. "

    if unique_activities:
        top_activities = unique_activities[:10]
        summary += (
            "The main activities observed in this case include: "
            + ", ".join(top_activities)
            + "."
        )

    return summary


def build_case_document(group_df: pd.DataFrame) -> Dict[str, Any]:
    """
    Build one AI-ready case JSON document from grouped events.
    """
    group_df = group_df.copy()

    # Sort events by timestamp when available.
    group_df["sort_timestamp"] = pd.to_datetime(
        group_df["event_timestamp"],
        errors="coerce",
        utc=True
    )

    group_df = group_df.sort_values(
        by=["sort_timestamp", "id"],
        na_position="last"
    )

    first_row = group_df.iloc[0]

    case_id = str(first_row["normalized_case_id"])
    process_type = str(first_row["process_type"])

    events = [clean_event_record(row) for _, row in group_df.iterrows()]

    timestamp_values = [
        event["timestamp"]
        for event in events
        if event.get("timestamp") is not None
    ]

    start_timestamp = min(timestamp_values) if timestamp_values else None
    end_timestamp = max(timestamp_values) if timestamp_values else None

    total_events = len(events)
    activity_sequence = get_activity_sequence(events)
    unique_activities = get_unique_activities(activity_sequence)
    duration_days = calculate_duration_days(start_timestamp, end_timestamp)

    case_summary = build_case_summary(
        case_id=case_id,
        process_type=process_type,
        total_events=total_events,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        activity_sequence=activity_sequence,
        duration_days=duration_days,
    )

    case_json = {
        "case_id": case_id,
        "process_type": process_type,
        "record_source": "bpi_challenge_2020",
        "source_database": "erp_ai_native_db",
        "source_table_layer": "cleaned_event_logs",
        "total_events": total_events,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "duration_days": duration_days,
        "activity_sequence": activity_sequence,
        "unique_activities": unique_activities,
        "events": events,
        "ai_text": case_summary,
    }

    return {
        "case_id": case_id,
        "process_type": process_type,
        "case_summary": case_summary,
        "case_json": case_json,
        "total_events": total_events,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
    }


def save_ai_ready_files(case_documents: List[Dict[str, Any]]) -> None:
    """
    Save AI-ready case documents as JSON and JSONL.
    """
    json_path = OUTPUT_DIR / "bpi2020_ai_ready_cases.json"
    jsonl_path = OUTPUT_DIR / "bpi2020_ai_ready_cases.jsonl"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(case_documents, f, indent=2, ensure_ascii=False, default=str)

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for doc in case_documents:
            f.write(json.dumps(doc, ensure_ascii=False, default=str) + "\n")

    print(f"\nSaved AI-ready JSON : {json_path}")
    print(f"Saved AI-ready JSONL: {jsonl_path}")


def insert_ai_ready_cases(case_documents: List[Dict[str, Any]]) -> int:
    """
    Insert case-level AI-ready documents into ai_ready_cases table.
    """
    insert_sql = text("""
        INSERT INTO ai_ready_cases (
            case_id,
            process_type,
            case_summary,
            case_json,
            total_events,
            start_timestamp,
            end_timestamp,
            embedding_status
        )
        VALUES (
            :case_id,
            :process_type,
            :case_summary,
            CAST(:case_json AS JSONB),
            :total_events,
            :start_timestamp,
            :end_timestamp,
            'pending'
        )
    """)

    inserted_count = 0

    with ai_engine.begin() as connection:
        connection.execute(text("DELETE FROM ai_ready_cases;"))

        for doc in case_documents:
            connection.execute(
                insert_sql,
                {
                    "case_id": doc["case_id"],
                    "process_type": doc["process_type"],
                    "case_summary": doc["case_summary"],
                    "case_json": json.dumps(doc["case_json"], ensure_ascii=False, default=str),
                    "total_events": doc["total_events"],
                    "start_timestamp": doc["start_timestamp"],
                    "end_timestamp": doc["end_timestamp"],
                },
            )

            inserted_count += 1

    return inserted_count


def log_transformation(
    input_count: int,
    output_count: int,
    status: str,
    message: str,
):
    """
    Store pipeline execution log.
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
                "pipeline_stage": "build_ai_ready_cases",
                "source_database": AI_DB_NAME,
                "target_database": AI_DB_NAME,
                "source_table": "cleaned_event_logs",
                "total_input_records": input_count,
                "total_output_records": output_count,
                "status": status,
                "message": message,
            },
        )


# ============================================================
# Main pipeline
# ============================================================

def main():
    print("\nStarting AI-ready case-building pipeline...")
    print(f"Source table : cleaned_event_logs")
    print(f"Target table : ai_ready_cases")
    print(f"Output folder: {OUTPUT_DIR}")

    query = """
        SELECT
            id,
            source_table,
            process_type,
            normalized_case_id,
            normalized_activity,
            event_timestamp,
            record_data
        FROM cleaned_event_logs
        WHERE normalized_case_id IS NOT NULL
        ORDER BY process_type, normalized_case_id, event_timestamp NULLS LAST, id;
    """

    df = pd.read_sql(query, ai_engine)

    input_count = len(df)

    print(f"\nLoaded cleaned event records: {input_count}")

    if input_count == 0:
        print("No cleaned records found. Run clean_and_load_to_ai_db.py first.")
        log_transformation(
            input_count=0,
            output_count=0,
            status="failed",
            message="No cleaned records found in cleaned_event_logs.",
        )
        return

    case_documents = []

    grouped = df.groupby(["process_type", "normalized_case_id"], dropna=True)

    total_cases = grouped.ngroups
    print(f"Detected unique ERP cases: {total_cases}")

    for index, ((process_type, case_id), group_df) in enumerate(grouped, start=1):
        case_doc = build_case_document(group_df)
        case_documents.append(case_doc)

        if index % 1000 == 0:
            print(f"   Built {index}/{total_cases} case documents...")

    save_ai_ready_files(case_documents)

    inserted_count = insert_ai_ready_cases(case_documents)

    log_transformation(
        input_count=input_count,
        output_count=inserted_count,
        status="success",
        message=f"Built and inserted {inserted_count} AI-ready case-level documents.",
    )

    print(f"\nInserted AI-ready cases into database: {inserted_count}")
    print("\nAI-ready case-building pipeline completed.")


if __name__ == "__main__":
    main()