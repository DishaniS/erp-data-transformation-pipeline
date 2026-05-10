import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine


load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "erp_legacy_db")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not DB_PASSWORD:
    raise ValueError("DB_PASSWORD is missing. Add it to your .env file.")

DATABASE_URL = (
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

engine = create_engine(DATABASE_URL)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data" / "processed"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TABLES = [
    "customer",
    "address",
    "category",
    "model",
    "product",
    "order_header",
    "order_detail",
]


def extract_table(table_name: str) -> pd.DataFrame:
    query = f"SELECT * FROM {table_name};"
    df = pd.read_sql(query, engine)
    print(f"{table_name}: {len(df)} rows extracted")
    return df


def main():
    for table in TABLES:
        df = extract_table(table)
        output_path = OUTPUT_DIR / f"{table}_extracted.csv"
        df.to_csv(output_path, index=False)
        print(f"Saved: {output_path}")

    print("\nERP extraction completed successfully.")


if __name__ == "__main__":
    main()