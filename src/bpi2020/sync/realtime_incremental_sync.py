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
    4. Inserts cleaned records into erp_ai_native_db.cleaned_event_logs.
    5. Updates sync_state.
    6. Repeats every few seconds.
"""

import os
import re
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


# ============================================================
# Project paths and environment
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")


# ============================================================
# Database configuration
# ============================================================

OLD_DB_HOST = os.getenv("OLD_DB_HOST", "localhost")
OLD_DB_PORT = os.getenv("OLD_DB_PORT", "5432")
OLD_DB_NAME = os.getenv("OLD_DB_NAME", "bpi2020_old_erp_db")
OLD_DB_USER = os.getenv("OLD_DB_USER", "postgres")
OLD_DB_PASSWORD = os.getenv("OLD_DB_PASSWORD", "postgres123")

AI_DB_HOST = os.getenv("AI_DB_HOST", "localhost")
AI_DB_PORT = os.getenv("AI_DB_PORT", "5432")
AI_DB_NAME = os.getenv("AI_DB_NAME", "erp_ai_native_db")
AI_DB_USER = os.getenv("AI_DB_USER", "postgres")
AI_DB_PASSWORD = os.getenv("AI_DB_PASSWORD", "postgres123")


OLD_DATABASE_URL = (
    f"postgresql+psycopg2://{OLD_DB_USER}:{OLD_DB_PASSWORD}"
    f"@{OLD_DB_HOST}:{OLD_DB_PORT}/{OLD_DB_NAME}"
)

AI_DATABASE_URL = (
    f"postgresql+psycopg2://{AI_DB_USER}:{AI_DB_PASSWORD}"
    f"@{AI_DB_HOST}:{AI_DB_PORT}/{AI_DB_NAME}"
)

old_engine = create_engine(OLD_DATABASE_URL)
ai_engine = create_engine(AI_DATABASE_URL)


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


POLL_INTERVAL_SECONDS = int(os.getenv("SYNC_POLL_INTERVAL_SECONDS", "10"))
BATCH_SIZE = int(os.getenv("SYNC_BATCH_SIZE", "1000"))


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
    timestamp_cols = detect_timestamp_columns(df)

    insert_sql = text("""
        INSERT INTO cleaned_event_logs (
            source_table,
            process_type,
            normalized_case_id,
            normalized_activity,
            event_timestamp,
            record_data
        )
        VALUES (
            :source_table,
            :process_type,
            :normalized_case_id,
            :normalized_activity,
            :event_timestamp,
            CAST(:record_data AS JSONB)
        )
    """)

    inserted_count = 0

    with ai_engine.begin() as connection:
        for _, row in df.iterrows():
            record = make_json_safe_record(row.to_dict())

            connection.execute(
                insert_sql,
                {
                    "source_table": source_table,
                    "process_type": process_type,
                    "normalized_case_id": row.get("normalized_case_id"),
                    "normalized_activity": row.get("normalized_activity"),
                    "event_timestamp": get_main_event_timestamp(row, timestamp_cols),
                    "record_data": json.dumps(record, ensure_ascii=False, default=str),
                },
            )

            inserted_count += 1

    return inserted_count


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


def run_forever():
    print("\nStarting near-real-time incremental sync...")
    print(f"Source DB : {OLD_DB_NAME}")
    print(f"Target DB : {AI_DB_NAME}")
    print(f"Interval  : {POLL_INTERVAL_SECONDS} seconds")
    print(f"Batch size: {BATCH_SIZE}")
    print("\nPress CTRL + C to stop.\n")

    while True:
        run_once()
        time.sleep(POLL_INTERVAL_SECONDS)


def main():
    run_forever()


if __name__ == "__main__":
    main()