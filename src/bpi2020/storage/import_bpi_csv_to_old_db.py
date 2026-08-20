import re
import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bpi2020.common.config import PostgresSettings
from bpi2020.common.health import check_postgres

RAW_DIR = PROJECT_ROOT / "data" / "bpi2020" / "raw"
LEGACY_RAW_DIR = PROJECT_ROOT / "data" / "raw"

ERP_SOURCE_DB = PostgresSettings.erp_source()

engine = ERP_SOURCE_DB.create_engine()


DATASETS = {
    "DomesticDeclarations.csv": "domestic_declarations_raw",
    "InternationalDeclarations.csv": "international_declarations_raw",
    "PermitLog.csv": "travel_permit_raw",
    "PrepaidTravelCost.csv": "prepaid_travel_cost_raw",
    "extracted_event_log.csv": "extracted_event_log_raw",
}


COLUMN_MAPPING = {
    "id": "event_id",
    "org:resource": "resource",
    "concept:name": "activity",
    "time:timestamp": "event_timestamp",
    "org:role": "role",
    "case:id": "case_id",
    "case:concept:name": "case_name",
    "case:BudgetNumber": "budget_number",
    "case:DeclarationNumber": "declaration_number",
    "case:Amount": "amount",
}


def clean_column_name(col: str) -> str:
    """
    Convert BPI/XES column names into PostgreSQL-safe names.
    Example:
    case:concept:name -> case_concept_name
    time:timestamp -> time_timestamp
    """
    col = col.strip()
    col = COLUMN_MAPPING.get(col, col)

    col = col.strip().lower()
    col = col.replace(":", "_")
    col = col.replace(" ", "_")
    col = col.replace("-", "_")
    col = re.sub(r"[^a-z0-9_]", "", col)
    col = re.sub(r"_+", "_", col)

    return col.strip("_")

def make_unique_columns(columns):
    """
    Ensure cleaned column names are unique.
    If duplicate column names exist, add suffixes:
    case_permit_id, case_permit_id_2, case_permit_id_3
    """
    seen = {}
    unique_columns = []

    for col in columns:
        if col not in seen:
            seen[col] = 1
            unique_columns.append(col)
        else:
            seen[col] += 1
            unique_columns.append(f"{col}_{seen[col]}")

    return unique_columns


def load_csv(csv_path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(csv_path, dtype=str, low_memory=False)
    except UnicodeDecodeError:
        return pd.read_csv(csv_path, dtype=str, encoding="latin1", low_memory=False)


def resolve_csv_path(csv_file: str) -> Path:
    for base_dir in (RAW_DIR, LEGACY_RAW_DIR):
        candidate = base_dir / csv_file
        if candidate.exists():
            return candidate

    return RAW_DIR / csv_file


def ensure_source_row_id(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add a stable sequential source row identifier for incremental sync when the
    source CSV does not already include one.

    This value is the anchor of the whole downstream identity chain: every
    cleaned event derives ``event:{system}:{entity}:{source_row_id}`` from it.
    It is deterministic because it follows CSV row order, so re-importing the
    same CSV reproduces exactly the same identifiers.
    """
    df = df.copy()

    if "source_row_id" not in df.columns:
        df.insert(0, "source_row_id", range(1, len(df) + 1))

    return df


def import_dataset(csv_file: str, table_name: str):
    csv_path = resolve_csv_path(csv_file)

    if not csv_path.exists():
        print(f"Skipping: {csv_file} not found in {RAW_DIR}")
        return

    print(f"\nLoading: {csv_file}")
    df = load_csv(csv_path)

    original_columns = list(df.columns)

    cleaned_columns = [clean_column_name(c) for c in df.columns]
    df.columns = make_unique_columns(cleaned_columns)
    df = ensure_source_row_id(df)

    duplicates = pd.Series(cleaned_columns)[pd.Series(cleaned_columns).duplicated()].unique()

    if len(duplicates) > 0:
        print(f"Duplicate cleaned column names detected and renamed: {list(duplicates)}")

    # Preserve source traceability
    df["source_dataset"] = csv_file

    print(f"Original columns: {len(original_columns)}")
    print(f"Cleaned columns: {len(df.columns)}")
    print(f"Rows loaded: {len(df)}")
    print(f"Writing to table: {table_name}")

    df.to_sql(
        table_name,
        engine,
        if_exists="replace",
        index=False,
        chunksize=5000,
        method="multi"
    )

    print(f"Imported {len(df)} rows into {table_name}")


def main():
    print("Starting BPI 2020 raw CSV import into old ERP PostgreSQL database...")
    print(f"Target database          : {ERP_SOURCE_DB.safe_target}")
    print(f"Preferred raw data folder: {RAW_DIR}")

    print(f"\n{check_postgres(ERP_SOURCE_DB)}")

    # to_sql(if_exists="replace") drops and recreates each raw table. That is
    # intentional for a clean re-import from the source CSVs, and it is safe for
    # downstream identity because source_row_id is reassigned deterministically
    # from CSV row order. It does discard any manual edits made to these tables.
    print("\nWARNING: this replaces the following tables in the source database:")
    for table_name in DATASETS.values():
        print(f"  - {table_name}")

    for csv_file, table_name in DATASETS.items():
        import_dataset(csv_file, table_name)

    print("\nAll available BPI 2020 raw datasets imported successfully.")


if __name__ == "__main__":
    main()
