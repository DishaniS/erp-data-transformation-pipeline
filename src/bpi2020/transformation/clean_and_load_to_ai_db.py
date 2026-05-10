"""
Clean and load BPI 2020 raw ERP event logs into the AI-native database.

Source DB:
    bpi2020_old_erp_db

Target DB:
    erp_ai_native_db

This script:
1. Reads raw tables from the simulated legacy ERP database.
2. Cleans and normalizes columns, timestamps, amounts, nulls, and duplicates.
3. Saves cleaned JSON and JSONL files.
4. Inserts cleaned records into erp_ai_native_db.cleaned_event_logs.
5. Writes pipeline execution metadata into transformation_logs.
"""

import os
import re
import json
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


# ============================================================
# Paths
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "cleaned"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")


# ============================================================
# Database Configuration
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
# Raw table mapping
# ============================================================

RAW_TABLES: Dict[str, str] = {
    "domestic_declarations_raw": "domestic_declarations",
    "international_declarations_raw": "international_declarations",
    "travel_permit_raw": "travel_permit",
    "prepaid_travel_cost_raw": "prepaid_travel_cost",
    "extracted_event_log_raw": "extracted_event_log",
}


# ============================================================
# Utility functions
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
        "case_concept_name",
        "case_id",
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
        "concept_name",
        "activity",
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
    timestamp_cols = detect_timestamp_columns(df)

    for col in timestamp_cols:
        converted = pd.to_datetime(df[col], errors="coerce", utc=True)

        if converted.notna().sum() > 0:
            df[col] = converted.dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return df


def convert_amounts(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    amount_cols = detect_amount_columns(df)

    for col in amount_cols:
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
    df["cleaning_stage"] = "cleaned_event_log"

    if case_col:
        df["normalized_case_id"] = df[case_col].astype(str)
    else:
        df["normalized_case_id"] = None

    if activity_col:
        df["normalized_activity"] = df[activity_col].astype(str)
    else:
        df["normalized_activity"] = None

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


def save_cleaned_files(df: pd.DataFrame, process_type: str):
    records = df.to_dict(orient="records")
    records = [make_json_safe_record(r) for r in records]

    json_path = OUTPUT_DIR / f"{process_type}_cleaned.json"
    jsonl_path = OUTPUT_DIR / f"{process_type}_cleaned.jsonl"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False, default=str)

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    print(f"   Saved JSON : {json_path}")
    print(f"   Saved JSONL: {jsonl_path}")


def insert_cleaned_records(df: pd.DataFrame, source_table: str, process_type: str) -> int:
    inserted_count = 0
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


def log_transformation(
    pipeline_stage: str,
    source_table: str,
    input_count: int,
    output_count: int,
    status: str,
    message: str,
):
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
                "pipeline_stage": pipeline_stage,
                "source_database": OLD_DB_NAME,
                "target_database": AI_DB_NAME,
                "source_table": source_table,
                "total_input_records": input_count,
                "total_output_records": output_count,
                "status": status,
                "message": message,
            },
        )


def clean_dataframe(df: pd.DataFrame, source_table: str, process_type: str) -> pd.DataFrame:
    df = normalize_columns(df)
    df = clean_text_columns(df)
    df = convert_timestamps(df)
    df = convert_amounts(df)

    before = len(df)
    df = df.drop_duplicates()
    removed = before - len(df)

    if removed > 0:
        print(f"   Removed duplicate rows: {removed}")

    df = df.where(pd.notnull(df), None)
    df = add_metadata_columns(df, source_table, process_type)

    return df


def clean_and_load_table(source_table: str, process_type: str):
    print("\n" + "=" * 80)
    print(f"Processing source table: {source_table}")
    print(f"Process type          : {process_type}")
    print("=" * 80)

    query = f'SELECT * FROM "{source_table}"'
    df = pd.read_sql(query, old_engine)

    input_count = len(df)
    print(f"   Input records: {input_count}")

    cleaned_df = clean_dataframe(df, source_table, process_type)
    output_count = len(cleaned_df)

    save_cleaned_files(cleaned_df, process_type)

    inserted_count = insert_cleaned_records(cleaned_df, source_table, process_type)

    log_transformation(
        pipeline_stage="clean_and_load_event_logs",
        source_table=source_table,
        input_count=input_count,
        output_count=inserted_count,
        status="success",
        message=f"Cleaned and loaded {inserted_count} records into cleaned_event_logs.",
    )

    print(f"   Inserted records into AI DB: {inserted_count}")


def main():
    print("\nStarting clean-and-load pipeline...")
    print(f"Source DB: {OLD_DB_NAME}")
    print(f"Target DB: {AI_DB_NAME}")
    print(f"Output folder: {OUTPUT_DIR}")

    # Optional: clear old cleaned records before re-running.
    with ai_engine.begin() as connection:
        connection.execute(text("DELETE FROM cleaned_event_logs;"))
        connection.execute(text("DELETE FROM transformation_logs WHERE pipeline_stage = 'clean_and_load_event_logs';"))

    for source_table, process_type in RAW_TABLES.items():
        try:
            clean_and_load_table(source_table, process_type)
        except Exception as e:
            print(f"\nERROR processing {source_table}: {e}")

            log_transformation(
                pipeline_stage="clean_and_load_event_logs",
                source_table=source_table,
                input_count=0,
                output_count=0,
                status="failed",
                message=str(e),
            )

    print("\nClean-and-load pipeline completed.")


if __name__ == "__main__":
    main()