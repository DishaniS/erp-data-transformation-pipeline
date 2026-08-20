"""
Near-real-time incremental sync pipeline for BPI 2020 ERP data.

Source:
    bpi2020_old_erp_db raw tables

Target:
    erp_ai_native_db.cleaned_event_logs

Purpose:
    Simulates real-time ERP ingestion using polling-based incremental sync.

How it works:
    1. Reads last synced source_row_id from erp_ai_native_db.sync_state.
    2. Fetches new rows from old ERP DB where source_row_id > last_synced_source_id.
    3. Cleans and normalizes new records.
    4. UPSERTs cleaned records into erp_ai_native_db.cleaned_event_logs.
    5. Updates sync_state.
    6. Repeats every few seconds.

Identity behaviour (Phase 0)
----------------------------
Rows are written with the same deterministic ``event_record_id`` the batch
loader uses, and the write is an UPSERT on that key. Previously this was a
plain INSERT, so any replay - a reset sync_state, an overlapping batch, a
re-import of the source CSVs - silently duplicated events in
cleaned_event_logs. Now a replay is a no-op update.
"""

import re
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy import text


# ============================================================
# Project paths and environment
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bpi2020.common.config import PostgresSettings, get_int_setting
from bpi2020.common.health import check_postgres
from bpi2020.common.stable_ids import SOURCE_SYSTEM, make_event_record_id


# ============================================================
# Database configuration
# ============================================================

ERP_SOURCE_DB = PostgresSettings.erp_source()
PIPELINE_DB = PostgresSettings.pipeline()

OLD_DB_NAME = ERP_SOURCE_DB.database
AI_DB_NAME = PIPELINE_DB.database

old_engine = ERP_SOURCE_DB.create_engine()
ai_engine = PIPELINE_DB.create_engine()


# ============================================================
# Source table mapping
# ============================================================

RAW_TABLES: Dict[str, str] = {
    "domestic_declarations_raw": "domestic_declarations",
    "international_declarations_raw": "international_declarations",
    "travel_permit_raw": "travel_permit",
    "prepaid_travel_cost_raw": "prepaid_travel_cost",
    "extracted_event_log_raw": "extracted_event_log",
}


POLL_INTERVAL_SECONDS = get_int_setting("SYNC_POLL_INTERVAL_SECONDS", 10)
BATCH_SIZE = get_int_setting("SYNC_BATCH_SIZE", 1000)


# ============================================================
# Cleaning helpers
# ============================================================

def normalize_column_name(col: str) -> str:
    col = str(col).strip().lower()
    col = col.replace(":", "_")
    col = col.replace(" ", "_")
    col = col.replace("-", "_")
    col = col.replace("/", "_")
    col = re.sub(r"[^a-z0-9_]", "", col)
    col = re.sub(r"_+", "_", col)
    return col.strip("_")


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [normalize_column_name(c) for c in df.columns]
    return df


def clean_text_value(value):
    if pd.isna(value):
        return None

    if isinstance(value, str):
        value = value.strip()
        value = re.sub(r"\s+", " ", value)

        if value.lower() in ["", "nan", "none", "null", "na", "n/a"]:
            return None

    return value


def clean_text_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in df.select_dtypes(include=["object"]).columns:
        df[col] = df[col].apply(clean_text_value)

    return df


def detect_case_id_column(df: pd.DataFrame) -> Optional[str]:
    candidates = [
        "case_id",
        "case_concept_name",
        "case",
        "declaration_id",
        "request_id",
        "permit_id",
        "id",
    ]

    for col in candidates:
        if col in df.columns:
            return col

    for col in df.columns:
        if "case" in col and "id" in col:
            return col

    return None


def detect_activity_column(df: pd.DataFrame) -> Optional[str]:
    candidates = [
        "activity",
        "concept_name",
        "event",
        "task",
        "action",
    ]

    for col in candidates:
        if col in df.columns:
            return col

    for col in df.columns:
        if "activity" in col or "concept" in col:
            return col

    return None


def detect_timestamp_columns(df: pd.DataFrame) -> List[str]:
    keywords = [
        "timestamp",
        "time",
        "date",
        "created",
        "modified",
        "start",
        "end",
        "complete",
    ]

    return [col for col in df.columns if any(k in col for k in keywords)]


def detect_amount_columns(df: pd.DataFrame) -> List[str]:
    keywords = [
        "amount",
        "cost",
        "price",
        "value",
        "total",
        "budget",
        "requested",
        "approved",
        "payment",
        "paid",
    ]

    return [col for col in df.columns if any(k in col for k in keywords)]


def convert_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in detect_timestamp_columns(df):
        converted = pd.to_datetime(df[col], errors="coerce", utc=True)

        if converted.notna().sum() > 0:
            df[col] = converted.dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return df


def convert_amounts(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in detect_amount_columns(df):
        cleaned = (
            df[col]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("€", "", regex=False)
            .str.replace("$", "", regex=False)
            .str.replace("£", "", regex=False)
            .str.strip()
        )

        numeric = pd.to_numeric(cleaned, errors="coerce")

        if numeric.notna().sum() > 0:
            df[col] = numeric

    return df


def add_metadata_columns(df: pd.DataFrame, source_table: str, process_type: str) -> pd.DataFrame:
    df = df.copy()

    case_col = detect_case_id_column(df)
    activity_col = detect_activity_column(df)

    df["source_table"] = source_table
    df["process_type"] = process_type
    df["record_source"] = "bpi_challenge_2020"
    df["cleaning_stage"] = "realtime_incremental_cleaned_event"

    if case_col:
        df["normalized_case_id"] = df[case_col].astype(str)
    else:
        df["normalized_case_id"] = None

    if activity_col:
        df["normalized_activity"] = df[activity_col].astype(str)
    else:
        df["normalized_activity"] = None

    return df


def clean_dataframe(df: pd.DataFrame, source_table: str, process_type: str) -> pd.DataFrame:
    df = normalize_columns(df)
    df = clean_text_columns(df)
    df = convert_timestamps(df)
    df = convert_amounts(df)
    df = df.drop_duplicates()
    df = df.where(pd.notnull(df), None)
    df = add_metadata_columns(df, source_table, process_type)
    return df


def get_main_event_timestamp(row: pd.Series, timestamp_cols: List[str]):
    for col in timestamp_cols:
        value = row.get(col)

        if value is not None and not pd.isna(value):
            parsed = pd.to_datetime(value, errors="coerce", utc=True)

            if pd.notna(parsed):
                return parsed.to_pydatetime()

    return None


def make_json_safe_record(record: dict) -> dict:
    safe_record = {}

    for key, value in record.items():
        if pd.isna(value):
            safe_record[key] = None
        elif isinstance(value, pd.Timestamp):
            safe_record[key] = value.isoformat()
        else:
            safe_record[key] = value

    return safe_record


# ============================================================
# Sync state functions
# ============================================================

def get_last_synced_id(source_table: str) -> int:
    query = text("""
        SELECT last_synced_source_id
        FROM sync_state
        WHERE source_table = :source_table
    """)

    with ai_engine.begin() as connection:
        result = connection.execute(query, {"source_table": source_table}).fetchone()

    if result is None:
        return 0

    return int(result[0])


def update_sync_state(source_table: str, last_synced_source_id: int):
    query = text("""
        INSERT INTO sync_state (
            source_table,
            last_synced_source_id,
            last_synced_at
        )
        VALUES (
            :source_table,
            :last_synced_source_id,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (source_table)
        DO UPDATE SET
            last_synced_source_id = EXCLUDED.last_synced_source_id,
            last_synced_at = CURRENT_TIMESTAMP
    """)

    with ai_engine.begin() as connection:
        connection.execute(
            query,
            {
                "source_table": source_table,
                "last_synced_source_id": last_synced_source_id,
            },
        )


def log_sync(
    source_table: str,
    input_count: int,
    output_count: int,
    status: str,
    message: str,
):
    query = text("""
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
            query,
            {
                "pipeline_stage": "realtime_incremental_sync",
                "source_database": OLD_DB_NAME,
                "target_database": AI_DB_NAME,
                "source_table": source_table,
                "total_input_records": input_count,
                "total_output_records": output_count,
                "status": status,
                "message": message,
            },
        )


# ============================================================
# Incremental sync logic
# ============================================================

def fetch_new_rows(source_table: str, last_synced_id: int) -> pd.DataFrame:
    query = f"""
        SELECT *
        FROM "{source_table}"
        WHERE source_row_id > {last_synced_id}
        ORDER BY source_row_id
        LIMIT {BATCH_SIZE};
    """

    return pd.read_sql(query, old_engine)


def insert_cleaned_records(df: pd.DataFrame, source_table: str, process_type: str) -> int:
    """UPSERT synced rows on the stable event key so replays cannot duplicate."""
    timestamp_cols = detect_timestamp_columns(df)

    upsert_sql = text("""
        INSERT INTO cleaned_event_logs (
            event_record_id,
            source_system,
            source_entity,
            source_record_key,
            source_table,
            process_type,
            normalized_case_id,
            normalized_activity,
            event_timestamp,
            record_data
        )
        VALUES (
            :event_record_id,
            :source_system,
            :source_entity,
            :source_record_key,
            :source_table,
            :process_type,
            :normalized_case_id,
            :normalized_activity,
            :event_timestamp,
            CAST(:record_data AS JSONB)
        )
        ON CONFLICT (event_record_id)
        DO UPDATE SET
            source_system = EXCLUDED.source_system,
            source_entity = EXCLUDED.source_entity,
            source_record_key = EXCLUDED.source_record_key,
            source_table = EXCLUDED.source_table,
            process_type = EXCLUDED.process_type,
            normalized_case_id = EXCLUDED.normalized_case_id,
            normalized_activity = EXCLUDED.normalized_activity,
            event_timestamp = EXCLUDED.event_timestamp,
            record_data = EXCLUDED.record_data
    """)

    written_count = 0

    with ai_engine.begin() as connection:
        for _, row in df.iterrows():
            source_row_id = row.get("source_row_id")

            if source_row_id is None or pd.isna(source_row_id):
                raise ValueError(
                    f"Row in {source_table} has no source_row_id, so no stable "
                    "event_record_id can be derived. Re-run "
                    "import_bpi_csv_to_old_db.py."
                )

            if isinstance(source_row_id, float) and source_row_id.is_integer():
                source_row_id = int(source_row_id)

            record = make_json_safe_record(row.to_dict())

            connection.execute(
                upsert_sql,
                {
                    "event_record_id": make_event_record_id(source_table, source_row_id),
                    "source_system": SOURCE_SYSTEM,
                    "source_entity": source_table,
                    "source_record_key": str(source_row_id),
                    "source_table": source_table,
                    "process_type": process_type,
                    "normalized_case_id": row.get("normalized_case_id"),
                    "normalized_activity": row.get("normalized_activity"),
                    "event_timestamp": get_main_event_timestamp(row, timestamp_cols),
                    "record_data": json.dumps(record, ensure_ascii=False, default=str),
                },
            )

            written_count += 1

    return written_count


def sync_one_table(source_table: str, process_type: str):
    last_synced_id = get_last_synced_id(source_table)

    new_rows = fetch_new_rows(source_table, last_synced_id)

    if new_rows.empty:
        print(f"   No new rows: {source_table}")
        return

    print(f"   New rows found in {source_table}: {len(new_rows)}")

    max_source_row_id = int(new_rows["source_row_id"].max())

    cleaned_df = clean_dataframe(new_rows, source_table, process_type)

    inserted_count = insert_cleaned_records(cleaned_df, source_table, process_type)

    update_sync_state(source_table, max_source_row_id)

    log_sync(
        source_table=source_table,
        input_count=len(new_rows),
        output_count=inserted_count,
        status="success",
        message=(
            f"Synced {inserted_count} new records. "
            f"Last synced source_row_id = {max_source_row_id}."
        ),
    )

    print(
        f"   Synced {inserted_count} rows from {source_table}. "
        f"Last source_row_id: {max_source_row_id}"
    )


def run_once():
    print("\nChecking for new ERP records...")

    for source_table, process_type in RAW_TABLES.items():
        try:
            sync_one_table(source_table, process_type)
        except Exception as e:
            print(f"   ERROR syncing {source_table}: {e}")

            log_sync(
                source_table=source_table,
                input_count=0,
                output_count=0,
                status="failed",
                message=str(e),
            )


def check_dependencies():
    print(check_postgres(ERP_SOURCE_DB))
    print(
        check_postgres(
            PIPELINE_DB, required_tables=("cleaned_event_logs", "sync_state")
        )
    )


def run_forever():
    print("\nStarting near-real-time incremental sync...")
    print(f"Source DB : {ERP_SOURCE_DB.safe_target}")
    print(f"Target DB : {PIPELINE_DB.safe_target}")
    print(f"Interval  : {POLL_INTERVAL_SECONDS} seconds")
    print(f"Batch size: {BATCH_SIZE}")

    check_dependencies()

    print("\nPress CTRL + C to stop.\n")

    while True:
        run_once()
        time.sleep(POLL_INTERVAL_SECONDS)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Near-real-time incremental sync from the legacy ERP tables."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync pass and exit instead of polling forever.",
    )
    args = parser.parse_args()

    if args.once:
        print("\nRunning a single incremental sync pass...")
        print(f"Source DB : {ERP_SOURCE_DB.safe_target}")
        print(f"Target DB : {PIPELINE_DB.safe_target}")
        check_dependencies()
        run_once()
        print("\nSingle sync pass completed.")
        return

    run_forever()


if __name__ == "__main__":
    main()