from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_DIR = PROJECT_ROOT / "data" / "processed"
OUTPUT_DIR = PROJECT_ROOT / "data" / "cleaned"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


FILES = {
    "customer": "customer_extracted.csv",
    "address": "address_extracted.csv",
    "category": "category_extracted.csv",
    "model": "model_extracted.csv",
    "product": "product_extracted.csv",
    "order_header": "order_header_extracted.csv",
    "order_detail": "order_detail_extracted.csv",
}


def clean_null_values(df: pd.DataFrame) -> pd.DataFrame:
    return df.replace(["NULL", "null", "None", ""], pd.NA)


def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(" ", "_")
    )
    return df


def convert_dates(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        if "date" in col:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def convert_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    possible_numeric_cols = [
        "standardcost",
        "listprice",
        "weight",
        "subtotal",
        "taxamt",
        "freight",
        "totaldue",
        "unitprice",
        "unitpricediscount",
        "linetotal",
    ]

    for col in possible_numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def clean_table(table_name: str, filename: str) -> pd.DataFrame:
    input_path = INPUT_DIR / filename
    df = pd.read_csv(input_path)

    df = standardize_columns(df)
    df = clean_null_values(df)
    df = convert_dates(df)
    df = convert_numeric_columns(df)

    df = df.drop_duplicates()

    output_path = OUTPUT_DIR / f"{table_name}_cleaned.csv"
    df.to_csv(output_path, index=False)

    print(f"{table_name}: cleaned {len(df)} rows → {output_path}")
    return df


def main():
    for table_name, filename in FILES.items():
        clean_table(table_name, filename)

    print("\nERP data cleaning and normalization completed successfully.")


if __name__ == "__main__":
    main()