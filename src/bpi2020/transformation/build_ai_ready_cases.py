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
6. UPSERTs case-level records into ai_ready_cases keyed by case_record_id.
7. Logs the transformation in transformation_logs.

Identity behaviour (Phase 0)
----------------------------
Each case carries a deterministic ``case_record_id`` of the form
``case:{process_type}:{normalized_case_id}``. Rebuilding is an UPSERT on that
key rather than DELETE + INSERT, so:

* a rerun on identical input creates no duplicates and changes no identity,
* the ai_ready_cases.id SERIAL stays put instead of drifting on every run,
* embedding_status is only reset to 'pending' when content_hash actually
  changed, so unchanged cases keep their existing vector linkage.

Rows for cases that no longer exist in cleaned_event_logs are removed by an
explicit, reported prune step (disable it with --keep-obsolete).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional

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
from bpi2020.common.stable_ids import compute_content_hash, make_case_record_id

OUTPUT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "ai_ready"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Database Configuration
# ============================================================

PIPELINE_DB = PostgresSettings.pipeline()
AI_DB_NAME = PIPELINE_DB.database

ai_engine = PIPELINE_DB.create_engine()

# Number of case rows sent per executemany batch. Case JSON documents are large
# (tens of KB each), so the batch stays small to bound memory.
UPSERT_BATCH_SIZE = 500


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
        # Deterministic cross-layer identity of the event.
        "event_record_id": make_json_safe(row.get("event_record_id")),
        # Internal cleaned_event_logs SERIAL. Retained for traceability only.
        # It is NOT an authoritative identifier: it changes whenever
        # cleaned_event_logs is rebuilt, and it is excluded from content_hash.
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

    case_record_id = make_case_record_id(process_type, case_id)

    case_json = {
        "case_record_id": case_record_id,
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

    # The hash covers identity plus the AI-facing content and the metadata that
    # reaches the vector payload. It deliberately excludes the per-event
    # cleaned_event_logs SERIALs, so rebuilding cleaned_event_logs does not
    # invalidate every case embedding.
    content_hash = compute_content_hash(
        record_id=case_record_id,
        text_for_ai=case_summary,
        metadata={
            "case_id": case_id,
            "process_type": process_type,
            "total_events": total_events,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "duration_days": duration_days,
            "activity_sequence": activity_sequence,
        },
    )

    return {
        "case_record_id": case_record_id,
        "content_hash": content_hash,
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


def load_existing_case_hashes() -> Dict[str, Optional[str]]:
    """
    Read the current case_record_id -> content_hash map.

    Used to report how many cases are new, changed, or unchanged, without
    relying on the SERIAL primary key for anything.
    """
    query = text("SELECT case_record_id, content_hash FROM ai_ready_cases")

    with ai_engine.connect() as connection:
        return {
            row[0]: row[1]
            for row in connection.execute(query)
            if row[0] is not None
        }


def upsert_ai_ready_cases(case_documents: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    UPSERT case-level AI-ready documents into ai_ready_cases.

    Conflict target is the stable case_record_id, so a rerun on identical input
    updates the same rows instead of deleting them and re-inserting with new
    SERIAL values.

    embedding_status is reset to 'pending' only when content_hash changed.
    An unchanged case therefore keeps embedding_status = 'completed' and its
    existing qdrant_point_id, which is what makes reruns cheap and safe.
    """
    upsert_sql = text("""
        INSERT INTO ai_ready_cases (
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
            updated_at
        )
        VALUES (
            :case_record_id,
            :content_hash,
            :case_id,
            :process_type,
            :case_summary,
            CAST(:case_json AS JSONB),
            :total_events,
            :start_timestamp,
            :end_timestamp,
            'pending',
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (case_record_id)
        DO UPDATE SET
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
                WHEN ai_ready_cases.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN 'pending'
                ELSE ai_ready_cases.embedding_status
            END
    """)

    existing_hashes = load_existing_case_hashes()

    stats = {
        "written": 0,
        "new": 0,
        "content_changed": 0,
        "content_unchanged": 0,
    }

    batch: List[Dict[str, Any]] = []

    with ai_engine.begin() as connection:
        for doc in case_documents:
            case_record_id = doc["case_record_id"]

            if case_record_id not in existing_hashes:
                stats["new"] += 1
            elif existing_hashes[case_record_id] != doc["content_hash"]:
                stats["content_changed"] += 1
            else:
                stats["content_unchanged"] += 1

            batch.append(
                {
                    "case_record_id": case_record_id,
                    "content_hash": doc["content_hash"],
                    "case_id": doc["case_id"],
                    "process_type": doc["process_type"],
                    "case_summary": doc["case_summary"],
                    "case_json": json.dumps(
                        doc["case_json"], ensure_ascii=False, default=str
                    ),
                    "total_events": doc["total_events"],
                    "start_timestamp": doc["start_timestamp"],
                    "end_timestamp": doc["end_timestamp"],
                }
            )

            if len(batch) >= UPSERT_BATCH_SIZE:
                connection.execute(upsert_sql, batch)
                stats["written"] += len(batch)
                print(f"   Upserted {stats['written']}/{len(case_documents)} cases...")
                batch = []

        if batch:
            connection.execute(upsert_sql, batch)
            stats["written"] += len(batch)

    return stats


def prune_obsolete_cases(case_documents: List[Dict[str, Any]]) -> int:
    """
    Delete ai_ready_cases rows whose case no longer exists in the source.

    This replaces the old blanket "DELETE FROM ai_ready_cases". It is explicit,
    scoped to genuinely obsolete rows, and reports exactly how many rows it
    removed so a surprising number is visible instead of silent.
    """
    current_ids = [doc["case_record_id"] for doc in case_documents]

    with ai_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TEMP TABLE current_case_ids (
                    case_record_id TEXT PRIMARY KEY
                ) ON COMMIT DROP
                """
            )
        )

        insert_sql = text(
            "INSERT INTO current_case_ids (case_record_id) VALUES (:case_record_id)"
        )
        for start in range(0, len(current_ids), 5000):
            chunk = current_ids[start : start + 5000]
            connection.execute(
                insert_sql, [{"case_record_id": value} for value in chunk]
            )

        result = connection.execute(
            text(
                """
                DELETE FROM ai_ready_cases a
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM current_case_ids c
                    WHERE c.case_record_id = a.case_record_id
                )
                """
            )
        )

        return result.rowcount or 0


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

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build AI-ready ERP case records from cleaned_event_logs."
    )
    parser.add_argument(
        "--keep-obsolete",
        action="store_true",
        help=(
            "Keep ai_ready_cases rows whose case is no longer present in "
            "cleaned_event_logs (default: remove them and report the count)."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("\nStarting AI-ready case-building pipeline...")
    print(f"Source table : cleaned_event_logs")
    print(f"Target table : ai_ready_cases")
    print(f"Database     : {PIPELINE_DB.safe_target}")
    print(f"Output folder: {OUTPUT_DIR}")

    print(f"\n{check_postgres(PIPELINE_DB, required_tables=('cleaned_event_logs', 'ai_ready_cases'))}")

    query = """
        SELECT
            id,
            event_record_id,
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

    duplicate_ids = _find_duplicate_case_record_ids(case_documents)
    if duplicate_ids:
        raise RuntimeError(
            "Duplicate case_record_id values were generated in this run: "
            f"{duplicate_ids[:5]} (total {len(duplicate_ids)}). "
            "Stable case identity is not unique for this dataset; refusing to write."
        )

    save_ai_ready_files(case_documents)

    stats = upsert_ai_ready_cases(case_documents)

    pruned_count = 0
    if args.keep_obsolete:
        print("\nSkipping obsolete-row prune (--keep-obsolete).")
    else:
        pruned_count = prune_obsolete_cases(case_documents)

    print("\nUpsert summary")
    print("-" * 50)
    print(f"  Cases written        : {stats['written']}")
    print(f"  New cases            : {stats['new']}")
    print(f"  Content changed      : {stats['content_changed']}")
    print(f"  Content unchanged    : {stats['content_unchanged']}")
    print(f"  Obsolete rows pruned : {pruned_count}")

    log_transformation(
        input_count=input_count,
        output_count=stats["written"],
        status="success",
        message=(
            f"Upserted {stats['written']} AI-ready case documents "
            f"(new={stats['new']}, changed={stats['content_changed']}, "
            f"unchanged={stats['content_unchanged']}, pruned={pruned_count})."
        ),
    )

    print("\nAI-ready case-building pipeline completed.")


def _find_duplicate_case_record_ids(case_documents: List[Dict[str, Any]]) -> List[str]:
    """Return case_record_id values produced more than once in this run."""
    seen: set = set()
    duplicates: List[str] = []

    for doc in case_documents:
        record_id = doc["case_record_id"]
        if record_id in seen:
            duplicates.append(record_id)
        seen.add(record_id)

    return duplicates


if __name__ == "__main__":
    main()