"""
Create and migrate the AI-native database schema for cleaned BPI 2020 ERP data.

This script creates the AI-native tables inside erp_ai_native_db.
The old database bpi2020_old_erp_db remains unchanged.

It is safe to run repeatedly. On an existing populated database it performs an
additive migration:

1. adds the stable cross-layer identifier columns,
2. backfills them for existing rows from business identity only,
3. adds the unique constraints that keep them trustworthy.

No existing row is deleted and no SERIAL primary key is changed.

Stable identifier columns (see bpi2020.common.stable_ids):
    cleaned_event_logs.event_record_id      event:{system}:{entity}:{row_key}
    ai_ready_cases.case_record_id           case:{process_type}:{case_id}
    ai_ready_documents.document_record_id   document:{document_id}

content_hash is intentionally NOT backfilled here. It is written by the stage
that produces the AI-facing content (build_ai_ready_cases.py,
parse_bpi_documents.py) so the hashing rule lives in exactly one place.
"""

import sys
from pathlib import Path

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bpi2020.common.config import PostgresSettings
from bpi2020.common.health import check_postgres
from bpi2020.common.stable_ids import (
    SOURCE_SYSTEM,
    StableIdError,
    make_case_record_id,
    make_document_record_id,
    make_event_record_id,
)


BACKFILL_BATCH_SIZE = 5000


# ============================================================
# SQL schema
# ============================================================

CREATE_SCHEMA_SQL = """

CREATE TABLE IF NOT EXISTS cleaned_event_logs (
    id SERIAL PRIMARY KEY,

    -- Deterministic cross-layer identity. The SERIAL above is internal only.
    event_record_id TEXT NULL,
    source_system VARCHAR(100) NULL,
    source_entity VARCHAR(150) NULL,
    source_record_key TEXT NULL,

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

    -- Authoritative cross-layer key. Files and Qdrant reference this, never id.
    case_record_id TEXT NULL,
    content_hash TEXT NULL,

    case_id TEXT NOT NULL,
    process_type VARCHAR(150),

    case_summary TEXT,
    case_json JSONB NOT NULL,

    total_events INTEGER,
    start_timestamp TIMESTAMPTZ NULL,
    end_timestamp TIMESTAMPTZ NULL,

    embedding_status VARCHAR(50) DEFAULT 'pending',
    qdrant_point_id TEXT NULL,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


CREATE INDEX IF NOT EXISTS idx_ai_ready_cases_case_id
ON ai_ready_cases (case_id);


CREATE INDEX IF NOT EXISTS idx_ai_ready_cases_process_type
ON ai_ready_cases (process_type);


CREATE INDEX IF NOT EXISTS idx_ai_ready_cases_embedding_status
ON ai_ready_cases (embedding_status);


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

    -- Authoritative cross-layer key, derived from the content-hashed document_id.
    document_record_id TEXT NULL,
    content_hash TEXT NULL,

    document_id TEXT UNIQUE NOT NULL,
    document_type VARCHAR(100),
    document_name TEXT,

    source_file_path TEXT,
    extracted_text TEXT,
    text_for_ai TEXT,
    document_json JSONB NOT NULL,

    embedding_status VARCHAR(50) DEFAULT 'pending',
    qdrant_point_id TEXT NULL,

    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


CREATE INDEX IF NOT EXISTS idx_ai_ready_documents_document_type
ON ai_ready_documents (document_type);


CREATE INDEX IF NOT EXISTS idx_ai_ready_documents_embedding_status
ON ai_ready_documents (embedding_status);

"""


# Additive migration for databases created before Phase 0.
ADD_STABLE_ID_COLUMNS_SQL = """
ALTER TABLE cleaned_event_logs ADD COLUMN IF NOT EXISTS event_record_id TEXT NULL;
ALTER TABLE cleaned_event_logs ADD COLUMN IF NOT EXISTS source_system VARCHAR(100) NULL;
ALTER TABLE cleaned_event_logs ADD COLUMN IF NOT EXISTS source_entity VARCHAR(150) NULL;
ALTER TABLE cleaned_event_logs ADD COLUMN IF NOT EXISTS source_record_key TEXT NULL;

ALTER TABLE ai_ready_cases ADD COLUMN IF NOT EXISTS case_record_id TEXT NULL;
ALTER TABLE ai_ready_cases ADD COLUMN IF NOT EXISTS content_hash TEXT NULL;
ALTER TABLE ai_ready_cases ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE ai_ready_documents ADD COLUMN IF NOT EXISTS document_record_id TEXT NULL;
ALTER TABLE ai_ready_documents ADD COLUMN IF NOT EXISTS content_hash TEXT NULL;
ALTER TABLE ai_ready_documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;
"""


# Created only after backfill so that a duplicate surfaces as a loud failure.
# PostgreSQL unique indexes permit multiple NULLs, so partially migrated rows
# do not block index creation.
ADD_STABLE_ID_CONSTRAINTS_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_cleaned_event_logs_event_record_id
ON cleaned_event_logs (event_record_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_ready_cases_case_record_id
ON ai_ready_cases (case_record_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_ready_cases_natural_key
ON ai_ready_cases (process_type, case_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_ready_documents_document_record_id
ON ai_ready_documents (document_record_id);
"""


# ============================================================
# Backfill
# ============================================================

def _apply_updates(connection, sql: str, rows: list[dict]) -> int:
    """Apply one executemany batch and return the number of parameter sets."""
    if not rows:
        return 0
    connection.execute(text(sql), rows)
    return len(rows)


def backfill_event_record_ids(engine) -> dict:
    """Populate cleaned_event_logs.event_record_id for pre-Phase-0 rows."""
    select_sql = text(
        """
        SELECT id, source_table, record_data->>'source_row_id' AS source_row_id
        FROM cleaned_event_logs
        WHERE event_record_id IS NULL
        ORDER BY id
        """
    )
    update_sql = """
        UPDATE cleaned_event_logs
        SET event_record_id = :event_record_id,
            source_system = :source_system,
            source_entity = :source_entity,
            source_record_key = :source_record_key
        WHERE id = :id
    """

    updated = 0
    skipped = 0

    with engine.connect() as connection:
        pending = connection.execute(select_sql).fetchall()

    if not pending:
        return {"updated": 0, "skipped": 0}

    print(f"   cleaned_event_logs rows needing event_record_id: {len(pending)}")

    batch: list[dict] = []
    with engine.begin() as connection:
        for row in pending:
            row_id, source_table, source_row_id = row

            # source_row_id is assigned deterministically by the CSV importer
            # from CSV row order, so it is reproducible across re-imports.
            if source_table is None or source_row_id is None:
                skipped += 1
                continue

            try:
                event_record_id = make_event_record_id(source_table, source_row_id)
            except StableIdError:
                skipped += 1
                continue

            batch.append(
                {
                    "id": row_id,
                    "event_record_id": event_record_id,
                    "source_system": SOURCE_SYSTEM,
                    "source_entity": source_table,
                    "source_record_key": str(source_row_id),
                }
            )

            if len(batch) >= BACKFILL_BATCH_SIZE:
                updated += _apply_updates(connection, update_sql, batch)
                print(f"      backfilled {updated}/{len(pending)} events...")
                batch = []

        updated += _apply_updates(connection, update_sql, batch)

    return {"updated": updated, "skipped": skipped}


def backfill_case_record_ids(engine) -> dict:
    """Populate ai_ready_cases.case_record_id for pre-Phase-0 rows."""
    select_sql = text(
        """
        SELECT id, process_type, case_id
        FROM ai_ready_cases
        WHERE case_record_id IS NULL
        ORDER BY id
        """
    )
    update_sql = """
        UPDATE ai_ready_cases
        SET case_record_id = :case_record_id
        WHERE id = :id
    """

    with engine.connect() as connection:
        pending = connection.execute(select_sql).fetchall()

    if not pending:
        return {"updated": 0, "skipped": 0}

    print(f"   ai_ready_cases rows needing case_record_id: {len(pending)}")

    updated = 0
    skipped = 0
    batch: list[dict] = []

    with engine.begin() as connection:
        for row_id, process_type, case_id in pending:
            try:
                case_record_id = make_case_record_id(process_type, case_id)
            except StableIdError:
                skipped += 1
                continue

            batch.append({"id": row_id, "case_record_id": case_record_id})

            if len(batch) >= BACKFILL_BATCH_SIZE:
                updated += _apply_updates(connection, update_sql, batch)
                print(f"      backfilled {updated}/{len(pending)} cases...")
                batch = []

        updated += _apply_updates(connection, update_sql, batch)

    return {"updated": updated, "skipped": skipped}


def backfill_document_record_ids(engine) -> dict:
    """Populate ai_ready_documents.document_record_id for pre-Phase-0 rows."""
    select_sql = text(
        """
        SELECT id, document_id
        FROM ai_ready_documents
        WHERE document_record_id IS NULL
        ORDER BY id
        """
    )
    update_sql = """
        UPDATE ai_ready_documents
        SET document_record_id = :document_record_id
        WHERE id = :id
    """

    with engine.connect() as connection:
        pending = connection.execute(select_sql).fetchall()

    if not pending:
        return {"updated": 0, "skipped": 0}

    print(f"   ai_ready_documents rows needing document_record_id: {len(pending)}")

    updated = 0
    skipped = 0
    batch: list[dict] = []

    with engine.begin() as connection:
        for row_id, document_id in pending:
            try:
                document_record_id = make_document_record_id(document_id)
            except StableIdError:
                skipped += 1
                continue

            batch.append({"id": row_id, "document_record_id": document_record_id})

        updated += _apply_updates(connection, update_sql, batch)

    return {"updated": updated, "skipped": skipped}


def report_missing_stable_ids(engine) -> int:
    """Report rows that still lack a stable identifier after backfill."""
    checks = {
        "cleaned_event_logs.event_record_id": "SELECT COUNT(*) FROM cleaned_event_logs WHERE event_record_id IS NULL",
        "ai_ready_cases.case_record_id": "SELECT COUNT(*) FROM ai_ready_cases WHERE case_record_id IS NULL",
        "ai_ready_documents.document_record_id": "SELECT COUNT(*) FROM ai_ready_documents WHERE document_record_id IS NULL",
    }

    total_missing = 0

    with engine.connect() as connection:
        for label, query in checks.items():
            missing = connection.execute(text(query)).scalar() or 0
            total_missing += missing
            status = "OK" if missing == 0 else "MISSING"
            print(f"   [{status}] {label}: {missing} rows without a stable id")

    return total_missing


# ============================================================
# Main
# ============================================================

def main():
    settings = PostgresSettings.pipeline()

    print("\nCreating / migrating AI-native database schema...")
    print(f"Target database: {settings.safe_target}")

    print(f"\n{check_postgres(settings)}")

    engine = settings.create_engine()

    print("\nApplying base schema...")
    with engine.begin() as connection:
        connection.execute(text(CREATE_SCHEMA_SQL))

    print("Adding stable cross-layer identifier columns...")
    with engine.begin() as connection:
        connection.execute(text(ADD_STABLE_ID_COLUMNS_SQL))

    print("\nBackfilling stable identifiers for existing rows...")
    event_result = backfill_event_record_ids(engine)
    case_result = backfill_case_record_ids(engine)
    document_result = backfill_document_record_ids(engine)

    print(
        f"   events   : updated {event_result['updated']}, skipped {event_result['skipped']}"
    )
    print(
        f"   cases    : updated {case_result['updated']}, skipped {case_result['skipped']}"
    )
    print(
        f"   documents: updated {document_result['updated']}, skipped {document_result['skipped']}"
    )

    print("\nStable identifier coverage:")
    total_missing = report_missing_stable_ids(engine)

    print("\nAdding unique constraints on stable identifiers...")
    try:
        with engine.begin() as connection:
            connection.execute(text(ADD_STABLE_ID_CONSTRAINTS_SQL))
    except Exception as exc:
        raise RuntimeError(
            "Failed to create the stable-identifier unique indexes. This means "
            "duplicate stable identifiers exist and must be resolved before the "
            "pipeline can guarantee cross-store integrity.\n"
            f"Underlying error: {exc}"
        ) from exc

    print("\nAI-native database schema is up to date.")
    print("\nTables:")
    for table_name in (
        "cleaned_event_logs",
        "ai_ready_cases",
        "ai_ready_documents",
        "transformation_logs",
        "sync_state",
    ):
        print(f"  - {table_name}")

    if total_missing:
        print(
            f"\nWARNING: {total_missing} row(s) still have no stable identifier. "
            "Re-run the producing pipeline stage for those rows."
        )


if __name__ == "__main__":
    main()
