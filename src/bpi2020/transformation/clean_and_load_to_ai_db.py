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
4. UPSERTs cleaned records into erp_ai_native_db.cleaned_event_logs.
5. Writes pipeline execution metadata into transformation_logs.

Identity behaviour (Phase 0)
----------------------------
Each cleaned event carries a deterministic ``event_record_id`` of the form
``event:{source_system}:{source_entity}:{source_record_key}`` built from the
legacy table name and its ``source_row_id``. Loading is an UPSERT on that key
instead of "DELETE everything, then INSERT", which means:

* a rerun on identical input produces the same row count and the same identity,
* the near-real-time sync writing into the same table cannot create duplicates,
* rows for source records that disappeared are removed by an explicit,
  reported prune step rather than by wiping the table first.
"""

import argparse
import re
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
from bpi2020.common.stable_ids import SOURCE_SYSTEM, make_event_record_id

OUTPUT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "cleaned"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Database Configuration
# ============================================================

ERP_SOURCE_DB = PostgresSettings.erp_source()
PIPELINE_DB = PostgresSettings.pipeline()

OLD_DB_NAME = ERP_SOURCE_DB.database
AI_DB_NAME = PIPELINE_DB.database

old_engine = ERP_SOURCE_DB.create_engine()
ai_engine = PIPELINE_DB.create_engine()

# Cleaned rows carry a full JSONB copy of the source record, so batches stay
# modest to bound memory during executemany.
UPSERT_BATCH_SIZE = 2000


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


UPSERT_EVENT_SQL = text("""
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


def build_event_params(
    row: pd.Series,
    source_table: str,
    process_type: str,
    timestamp_cols: List[str],
) -> Dict[str, object]:
    """
    Build the UPSERT parameters for one cleaned event row.

    ``source_row_id`` is assigned by the CSV importer from CSV row order, so it
    is deterministic across re-imports and makes a safe stable key. A row
    without it cannot be given a stable identity, which is a hard error rather
    than something to insert and forget about.
    """
    source_row_id = row.get("source_row_id")

    if source_row_id is None or (isinstance(source_row_id, float) and pd.isna(source_row_id)):
        raise ValueError(
            f"Row in {source_table} has no source_row_id, so no stable "
            "event_record_id can be derived. Re-run import_bpi_csv_to_old_db.py, "
            "which adds source_row_id to every raw table."
        )

    if isinstance(source_row_id, float) and source_row_id.is_integer():
        source_row_id = int(source_row_id)

    record = make_json_safe_record(row.to_dict())

    return {
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
    }


def insert_cleaned_records(
    df: pd.DataFrame,
    source_table: str,
    process_type: str,
) -> Tuple[int, List[str]]:
    """
    UPSERT cleaned rows and return (row count, stable event ids written).

    The returned ids drive the obsolete-row prune in main().
    """
    timestamp_cols = detect_timestamp_columns(df)

    written = 0
    event_record_ids: List[str] = []
    batch: List[Dict[str, object]] = []

    with ai_engine.begin() as connection:
        for _, row in df.iterrows():
            params = build_event_params(row, source_table, process_type, timestamp_cols)
            event_record_ids.append(params["event_record_id"])
            batch.append(params)

            if len(batch) >= UPSERT_BATCH_SIZE:
                connection.execute(UPSERT_EVENT_SQL, batch)
                written += len(batch)
                batch = []

        if batch:
            connection.execute(UPSERT_EVENT_SQL, batch)
            written += len(batch)

    return written, event_record_ids


def prune_obsolete_events(event_record_ids: List[str]) -> int:
    """
    Delete cleaned_event_logs rows whose source record no longer exists.

    This replaces the old blanket "DELETE FROM cleaned_event_logs" that ran
    before every load. It is explicit, scoped to genuinely obsolete rows, and
    reports the count so an unexpected deletion is visible.
    """
    with ai_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TEMP TABLE current_event_ids (
                    event_record_id TEXT PRIMARY KEY
                ) ON COMMIT DROP
                """
            )
        )

        insert_sql = text(
            "INSERT INTO current_event_ids (event_record_id) VALUES (:event_record_id)"
        )
        for start in range(0, len(event_record_ids), 5000):
            chunk = event_record_ids[start : start + 5000]
            connection.execute(
                insert_sql, [{"event_record_id": value} for value in chunk]
            )

        result = connection.execute(
            text(
                """
                DELETE FROM cleaned_event_logs e
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM current_event_ids c
                    WHERE c.event_record_id = e.event_record_id
                )
                """
            )
        )

        return result.rowcount or 0


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


def clean_and_load_table(source_table: str, process_type: str) -> List[str]:
    print("\n" + "=" * 80)
    print(f"Processing source table: {source_table}")
    print(f"Process type          : {process_type}")
    print("=" * 80)

    query = f'SELECT * FROM "{source_table}"'
    df = pd.read_sql(query, old_engine)

    input_count = len(df)
    print(f"   Input records: {input_count}")

    cleaned_df = clean_dataframe(df, source_table, process_type)

    save_cleaned_files(cleaned_df, process_type)

    written_count, event_record_ids = insert_cleaned_records(
        cleaned_df, source_table, process_type
    )

    log_transformation(
        pipeline_stage="clean_and_load_event_logs",
        source_table=source_table,
        input_count=input_count,
        output_count=written_count,
        status="success",
        message=f"Cleaned and upserted {written_count} records into cleaned_event_logs.",
    )

    print(f"   Upserted records into AI DB: {written_count}")

    return event_record_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean legacy ERP tables and load them into cleaned_event_logs."
    )
    parser.add_argument(
        "--keep-obsolete",
        action="store_true",
        help=(
            "Keep cleaned_event_logs rows whose source record is no longer present "
            "(default: remove them and report the count)."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("\nStarting clean-and-load pipeline...")
    print(f"Source DB: {ERP_SOURCE_DB.safe_target}")
    print(f"Target DB: {PIPELINE_DB.safe_target}")
    print(f"Output folder: {OUTPUT_DIR}")

    print(f"\n{check_postgres(ERP_SOURCE_DB)}")
    print(
        f"{check_postgres(PIPELINE_DB, required_tables=('cleaned_event_logs', 'transformation_logs'))}"
    )

    all_event_record_ids: List[str] = []
    failed_tables: List[str] = []

    for source_table, process_type in RAW_TABLES.items():
        try:
            all_event_record_ids.extend(clean_and_load_table(source_table, process_type))
        except Exception as e:
            failed_tables.append(source_table)
            print(f"\nERROR processing {source_table}: {e}")

            log_transformation(
                pipeline_stage="clean_and_load_event_logs",
                source_table=source_table,
                input_count=0,
                output_count=0,
                status="failed",
                message=str(e),
            )

    pruned_count = 0

    if failed_tables:
        # Pruning now would delete rows belonging to a table that failed to load,
        # so it is skipped rather than risking data loss on a partial run.
        print(
            f"\nSkipping obsolete-row prune: {len(failed_tables)} source table(s) "
            f"failed to load ({', '.join(failed_tables)})."
        )
    elif args.keep_obsolete:
        print("\nSkipping obsolete-row prune (--keep-obsolete).")
    else:
        pruned_count = prune_obsolete_events(all_event_record_ids)

    print("\nLoad summary")
    print("-" * 50)
    print(f"  Stable event ids written : {len(all_event_record_ids)}")
    print(f"  Obsolete rows pruned     : {pruned_count}")
    print(f"  Failed source tables     : {len(failed_tables)}")

    print("\nClean-and-load pipeline completed.")


if __name__ == "__main__":
    main()