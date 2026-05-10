"""
Create AI-native database schema for cleaned BPI 2020 ERP data.

This script creates cleaned-data tables inside erp_ai_native_db.
The old database bpi2020_old_erp_db remains unchanged.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


# ============================================================
# Load environment variables
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")


AI_DB_HOST = os.getenv("AI_DB_HOST", "localhost")
AI_DB_PORT = os.getenv("AI_DB_PORT", "5432")
AI_DB_NAME = os.getenv("AI_DB_NAME", "erp_ai_native_db")
AI_DB_USER = os.getenv("AI_DB_USER", "postgres")
AI_DB_PASSWORD = os.getenv("AI_DB_PASSWORD", "postgres")


AI_DATABASE_URL = (
    f"postgresql+psycopg2://{AI_DB_USER}:{AI_DB_PASSWORD}"
    f"@{AI_DB_HOST}:{AI_DB_PORT}/{AI_DB_NAME}"
)

engine = create_engine(AI_DATABASE_URL)


# ============================================================
# SQL schema
# ============================================================

CREATE_SCHEMA_SQL = """

CREATE TABLE IF NOT EXISTS cleaned_event_logs (
    id SERIAL PRIMARY KEY,

    source_table VARCHAR(150),
    process_type VARCHAR(150),

    normalized_case_id TEXT,
    normalized_activity TEXT,

    event_timestamp TIMESTAMPTZ NULL,

    record_data JSONB NOT NULL,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


CREATE INDEX IF NOT EXISTS idx_cleaned_event_logs_case_id
ON cleaned_event_logs (normalized_case_id);


CREATE INDEX IF NOT EXISTS idx_cleaned_event_logs_process_type
ON cleaned_event_logs (process_type);


CREATE INDEX IF NOT EXISTS idx_cleaned_event_logs_activity
ON cleaned_event_logs (normalized_activity);


CREATE TABLE IF NOT EXISTS ai_ready_cases (
    id SERIAL PRIMARY KEY,

    case_id TEXT NOT NULL,
    process_type VARCHAR(150),

    case_summary TEXT,
    case_json JSONB NOT NULL,

    total_events INTEGER,
    start_timestamp TIMESTAMPTZ NULL,
    end_timestamp TIMESTAMPTZ NULL,

    embedding_status VARCHAR(50) DEFAULT 'pending',
    qdrant_point_id TEXT NULL,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


CREATE INDEX IF NOT EXISTS idx_ai_ready_cases_case_id
ON ai_ready_cases (case_id);


CREATE INDEX IF NOT EXISTS idx_ai_ready_cases_process_type
ON ai_ready_cases (process_type);


CREATE TABLE IF NOT EXISTS transformation_logs (
    id SERIAL PRIMARY KEY,

    pipeline_stage VARCHAR(150),
    source_database VARCHAR(150),
    target_database VARCHAR(150),
    source_table VARCHAR(150),

    total_input_records INTEGER,
    total_output_records INTEGER,
    status VARCHAR(50),

    message TEXT,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS sync_state (
    source_table VARCHAR(150) PRIMARY KEY,
    last_synced_source_id BIGINT NOT NULL DEFAULT 0,
    last_synced_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_ready_documents (
    id SERIAL PRIMARY KEY,

    document_id TEXT UNIQUE NOT NULL,
    document_type VARCHAR(100),
    document_name TEXT,

    source_file_path TEXT,
    extracted_text TEXT,
    text_for_ai TEXT,
    document_json JSONB NOT NULL,

    embedding_status VARCHAR(50) DEFAULT 'pending',
    qdrant_point_id TEXT NULL,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


CREATE INDEX IF NOT EXISTS idx_ai_ready_documents_document_type
ON ai_ready_documents (document_type);


CREATE INDEX IF NOT EXISTS idx_ai_ready_documents_embedding_status
ON ai_ready_documents (embedding_status);

"""


# ============================================================
# Main
# ============================================================

def main():
    print("\nCreating AI-native database schema...")
    print(f"Target database: {AI_DB_NAME}")

    with engine.begin() as connection:
        connection.execute(text(CREATE_SCHEMA_SQL))

    print("\nAI-native database schema created successfully.")
    print("\nCreated tables:")
    print("  - cleaned_event_logs")
    print("  - ai_ready_cases")
    print("  - transformation_logs")
    print("  - ai_ready_documents")
    print("  - sync_state")

if __name__ == "__main__":
    main()
