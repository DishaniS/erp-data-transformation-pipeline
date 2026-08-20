"""Phase 0 live integration checks.

These touch real services and are skipped - not failed - when a dependency is
unavailable, so the unit suite stays runnable anywhere. Keep them cheap: they
assert on identity and linkage, never on reprocessing the full dataset.

Run only these:
    pytest tests/test_pipeline_integration.py -v
"""

import pytest
from sqlalchemy import text

from bpi2020.common.config import ConfigurationError, PostgresSettings, get_vector_collection
from bpi2020.common.health import DependencyUnavailableError, check_postgres, check_qdrant
from bpi2020.common.stable_ids import make_qdrant_point_id
from bpi2020.qdrant_connection import QdrantSettings


@pytest.fixture(scope="module")
def pipeline_engine():
    try:
        settings = PostgresSettings.pipeline()
        check_postgres(settings, required_tables=("ai_ready_cases", "ai_ready_documents"))
    except (ConfigurationError, DependencyUnavailableError) as exc:
        pytest.skip(f"Pipeline PostgreSQL unavailable: {exc}")

    engine = settings.create_engine()
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def qdrant_client():
    try:
        settings = QdrantSettings.from_env()
        result = check_qdrant(settings, get_vector_collection(), raise_on_failure=False)
        if not result.ok:
            pytest.skip(f"Qdrant unavailable: {result.detail}")
    except ConfigurationError as exc:
        pytest.skip(f"Qdrant not configured: {exc}")

    return settings.create_client()


# ============================================================
# Schema-level identity guarantees
# ============================================================

@pytest.mark.parametrize(
    "table, column",
    [
        ("cleaned_event_logs", "event_record_id"),
        ("ai_ready_cases", "case_record_id"),
        ("ai_ready_documents", "document_record_id"),
    ],
)
def test_stable_id_column_exists(pipeline_engine, table, column):
    with pipeline_engine.connect() as connection:
        exists = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name = :table AND column_name = :column
                """
            ),
            {"table": table, "column": column},
        ).scalar()

    assert exists == 1, f"{table}.{column} is missing; run create_ai_native_db_schema.py"


@pytest.mark.parametrize(
    "index_name",
    [
        "uq_cleaned_event_logs_event_record_id",
        "uq_ai_ready_cases_case_record_id",
        "uq_ai_ready_cases_natural_key",
        "uq_ai_ready_documents_document_record_id",
    ],
)
def test_unique_index_exists(pipeline_engine, index_name):
    with pipeline_engine.connect() as connection:
        exists = connection.execute(
            text("SELECT COUNT(*) FROM pg_indexes WHERE indexname = :name"),
            {"name": index_name},
        ).scalar()

    assert exists == 1, f"unique index {index_name} is missing"


@pytest.mark.parametrize(
    "table, column",
    [
        ("cleaned_event_logs", "event_record_id"),
        ("ai_ready_cases", "case_record_id"),
        ("ai_ready_documents", "document_record_id"),
    ],
)
def test_no_row_lacks_a_stable_id(pipeline_engine, table, column):
    with pipeline_engine.connect() as connection:
        missing = connection.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL")
        ).scalar()

    assert missing == 0, f"{missing} row(s) in {table} have no {column}"


# ============================================================
# Rebuild idempotency
# ============================================================

def test_case_upsert_is_idempotent(pipeline_engine):
    """
    Re-applying the exact case UPSERT must not create a row, must not move the
    SERIAL, and must not invalidate the embedding of unchanged content.

    The whole thing runs inside a rolled-back transaction, so the live table is
    left untouched.
    """
    from bpi2020.transformation.build_ai_ready_cases import UPSERT_BATCH_SIZE  # noqa: F401

    upsert_sql = text(
        """
        INSERT INTO ai_ready_cases (
            case_record_id, content_hash, case_id, process_type,
            case_summary, case_json, total_events, embedding_status, updated_at
        )
        VALUES (
            :case_record_id, :content_hash, :case_id, :process_type,
            :case_summary, CAST(:case_json AS JSONB), :total_events,
            'pending', CURRENT_TIMESTAMP
        )
        ON CONFLICT (case_record_id)
        DO UPDATE SET
            case_summary = EXCLUDED.case_summary,
            content_hash = EXCLUDED.content_hash,
            updated_at = CURRENT_TIMESTAMP,
            embedding_status = CASE
                WHEN ai_ready_cases.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN 'pending'
                ELSE ai_ready_cases.embedding_status
            END
        """
    )

    params = {
        "case_record_id": "case:__pytest__:idempotency_probe",
        "content_hash": "hash-v1",
        "case_id": "__pytest__ probe",
        "process_type": "__pytest__",
        "case_summary": "probe summary",
        "case_json": "{}",
        "total_events": 1,
    }

    connection = pipeline_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(upsert_sql, params)
        first = connection.execute(
            text(
                "SELECT id, content_hash, embedding_status FROM ai_ready_cases "
                "WHERE case_record_id = :rid"
            ),
            {"rid": params["case_record_id"]},
        ).fetchone()

        # Pretend the row was embedded.
        connection.execute(
            text(
                "UPDATE ai_ready_cases SET embedding_status = 'completed', "
                "qdrant_point_id = :pid WHERE case_record_id = :rid"
            ),
            {
                "pid": make_qdrant_point_id(params["case_record_id"]),
                "rid": params["case_record_id"],
            },
        )

        # Rerun with identical content.
        connection.execute(upsert_sql, params)
        second = connection.execute(
            text(
                "SELECT id, content_hash, embedding_status FROM ai_ready_cases "
                "WHERE case_record_id = :rid"
            ),
            {"rid": params["case_record_id"]},
        ).fetchone()

        row_count = connection.execute(
            text("SELECT COUNT(*) FROM ai_ready_cases WHERE case_record_id = :rid"),
            {"rid": params["case_record_id"]},
        ).scalar()

        assert row_count == 1, "an identical rerun created a duplicate row"
        assert second.id == first.id, "the SERIAL moved on an identical rerun"
        assert second.embedding_status == "completed", (
            "unchanged content invalidated an existing embedding"
        )

        # Now change the content: the embedding must be invalidated.
        connection.execute(upsert_sql, dict(params, content_hash="hash-v2"))
        third = connection.execute(
            text(
                "SELECT id, embedding_status FROM ai_ready_cases "
                "WHERE case_record_id = :rid"
            ),
            {"rid": params["case_record_id"]},
        ).fetchone()

        assert third.id == first.id, "changed content moved the SERIAL"
        assert third.embedding_status == "pending", (
            "changed content did not invalidate the embedding"
        )
    finally:
        transaction.rollback()
        connection.close()


# ============================================================
# Cross-store linkage
# ============================================================

def test_stored_point_ids_are_deterministically_derived(pipeline_engine):
    """Every linked row must store uuid5 of its own stable key."""
    queries = {
        "ai_ready_cases": "case_record_id",
        "ai_ready_documents": "document_record_id",
    }

    for table, column in queries.items():
        with pipeline_engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT {column}, qdrant_point_id FROM {table} "
                    "WHERE qdrant_point_id IS NOT NULL LIMIT 500"
                )
            ).fetchall()

        mismatched = [
            record_id
            for record_id, point_id in rows
            if point_id != make_qdrant_point_id(record_id)
        ]

        assert not mismatched, (
            f"{table} stores point ids not derived from the stable key: {mismatched[:3]}"
        )


def test_completed_rows_have_a_point_id(pipeline_engine):
    for table in ("ai_ready_cases", "ai_ready_documents"):
        with pipeline_engine.connect() as connection:
            missing = connection.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} "
                    "WHERE embedding_status = 'completed' AND qdrant_point_id IS NULL"
                )
            ).scalar()

        assert missing == 0, f"{missing} completed row(s) in {table} have no point id"


def test_missing_source_record_raises_instead_of_reporting_success(pipeline_engine):
    """
    A record_id with no PostgreSQL row must abort the run, not pass silently.

    This is the exact failure the old "WHERE id = :source_record_id" hid: it
    matched zero rows and the job still printed success. The UPDATE runs inside
    a transaction that rolls back, so nothing is written.
    """
    from bpi2020.embeddings import generate_and_store_embeddings as stage

    record = {
        "record_id": "case:__pytest__:definitely_not_present",
        "record_type": "erp_case",
        "source_record_id": 999_999_999,
    }
    point_id = make_qdrant_point_id(record["record_id"])

    with pytest.raises(stage.EmbeddingLinkageError, match="EMBEDDING_SOURCE_RECORD_NOT_FOUND"):
        stage.update_postgres_embedding_status([record], [point_id])


def test_embedded_cases_resolve_in_qdrant(pipeline_engine, qdrant_client):
    """A sample of embedded cases must exist in Qdrant under the derived ID."""
    collection = get_vector_collection()

    if not qdrant_client.collection_exists(collection):
        pytest.skip(f"Qdrant collection '{collection}' does not exist yet")

    with pipeline_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT case_record_id FROM ai_ready_cases "
                "WHERE embedding_status = 'completed' "
                "ORDER BY case_record_id LIMIT 25"
            )
        ).fetchall()

    if not rows:
        pytest.skip("no completed case embeddings to verify")

    record_ids = [row[0] for row in rows]
    point_ids = [make_qdrant_point_id(record_id) for record_id in record_ids]

    found = qdrant_client.retrieve(
        collection_name=collection,
        ids=point_ids,
        with_payload=True,
        with_vectors=False,
    )
    found_ids = {str(point.id) for point in found}

    missing = [pid for pid in point_ids if pid not in found_ids]
    assert not missing, f"{len(missing)} embedded case(s) have no vector in Qdrant"

    for point in found:
        payload = point.payload or {}
        payload_id = payload.get("record_id") or payload.get("unified_record_id")
        assert payload_id in record_ids, (
            "a vector payload does not carry the stable record id it is stored under"
        )
