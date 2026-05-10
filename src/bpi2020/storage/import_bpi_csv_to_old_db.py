import os
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine


load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "data" / "raw"

DB_HOST = os.getenv("BPI_OLD_DB_HOST", "localhost")
DB_PORT = os.getenv("BPI_OLD_DB_PORT", "5432")
DB_NAME = os.getenv("BPI_OLD_DB_NAME", "bpi2020_old_erp_db")
DB_USER = os.getenv("BPI_OLD_DB_USER", "postgres")
DB_PASSWORD = os.getenv("BPI_OLD_DB_PASSWORD")

if not DB_PASSWORD:
    raise ValueError("BPI_OLD_DB_PASSWORD is missing in .env file.")

DATABASE_URL = (
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

engine = create_engine(DATABASE_URL)


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


def import_dataset(csv_file: str, table_name: str):
    csv_path = RAW_DIR / csv_file

    if not csv_path.exists():
        print(f"Skipping: {csv_file} not found in {RAW_DIR}")
        return

    print(f"\nLoading: {csv_file}")
    df = load_csv(csv_path)

    original_columns = list(df.columns)

    cleaned_columns = [clean_column_name(c) for c in df.columns]
    df.columns = make_unique_columns(cleaned_columns)

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

    for csv_file, table_name in DATASETS.items():
        import_dataset(csv_file, table_name)

    print("\nAll available BPI 2020 raw datasets imported successfully.")


if __name__ == "__main__":
    main()