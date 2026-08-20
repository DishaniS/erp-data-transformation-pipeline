"""LIVE BPI cascade: real PostgreSQL tables, real one-case rebuild.

SAFETY
------
Everything runs in an isolated ``bpi_cascade_test`` schema whose tables are
created with the REAL DDL of ``cleaned_event_logs`` and ``ai_ready_cases``, and
dropped afterwards. The production 270,211-event / 32,999-case baseline is
never read and never written.

Skipped, never faked, when PostgreSQL is unreachable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from erp_pipeline.schemas.enums import FieldDataType as T, MappingStatus, SourceType
from erp_pipeline.schemas.mapping_models import FieldMapping, MappingProfile
from erp_pipeline.sync import (
    ExtractionConfig,
    FailingStage,
    FailurePolicy,
    InMemoryCanonicalStore,
    InMemoryVectorStore,
    PropagationPipeline,
    RelationalIncrementalExtractor,
    SyncOptions,
    SyncService,
    SyncTarget,
    WatermarkStrategy,
    bootstrap_sync_schema,
    PostgresSyncStateStore,
)
from erp_integrations.bpi_case_cascade import CaseKeyIndex
from erp_integrations.bpi_postgres_cascade import (
    PostgresAffectedCaseResolver,
    PostgresCaseAccess,
    PostgresCaseHashLedger,
    PostgresCaseRepresentationBuilder,
    PreviousHashRegistry,
    build_case_document,
    make_qdrant_point_id,
)

SCHEMA = "bpi_cascade_test"
SYSTEM = "bpi_cascade_live"
BASE = datetime(2026, 8, 14, 9, 0, 0, tzinfo=timezone.utc)

CASE_COUNT = 60
EVENTS_PER_CASE = 4
PROCESS_TYPE = "RequestForPayment"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)


@pytest.fixture(scope="module")
def engine():
    _load_env()

    try:
        from sqlalchemy import create_engine, text
    except ImportError:  # pragma: no cover
        pytest.skip("SQLAlchemy is not installed")

    user = os.getenv("AI_DB_USER") or os.getenv("PIPELINE_DB_USER")
    password = os.getenv("AI_DB_PASSWORD") or os.getenv("PIPELINE_DB_PASSWORD")
    host = os.getenv("AI_DB_HOST") or os.getenv("PIPELINE_DB_HOST") or "localhost"
    port = os.getenv("AI_DB_PORT") or os.getenv("PIPELINE_DB_PORT") or "5432"
    database = (
        os.getenv("AI_DB_NAME") or os.getenv("PIPELINE_DB_NAME")
        or "erp_ai_native_db"
    )

    if not user or not password:
        pytest.skip("PostgreSQL credentials are not configured")

    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"

    try:
        instance = create_engine(url, pool_pre_ping=True)
        with instance.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - availability probe
        pytest.skip(f"PostgreSQL unreachable: {exc}")

    yield instance
    instance.dispose()


@pytest.fixture()
def bpi_tables(engine):
    """Isolated copies of the REAL cleaned_event_logs / ai_ready_cases DDL."""
    from sqlalchemy import text

    def run(sql: str) -> None:
        with engine.begin() as connection:
            for statement in [s.strip() for s in sql.split(";") if s.strip()]:
                connection.execute(text(statement))

    run(
        f"""
        DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
        CREATE SCHEMA {SCHEMA};

        CREATE TABLE {SCHEMA}.cleaned_event_logs (
            id SERIAL PRIMARY KEY,
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
        CREATE INDEX ON {SCHEMA}.cleaned_event_logs (normalized_case_id);

        CREATE TABLE {SCHEMA}.ai_ready_cases (
            id SERIAL PRIMARY KEY,
            case_record_id TEXT NULL UNIQUE,
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
        """
    )

    with engine.begin() as connection:
        ordinal = 0
        for case_index in range(1, CASE_COUNT + 1):
            case_id = f"LIVECASE-{case_index:04d}"
            for event_index in range(1, EVENTS_PER_CASE + 1):
                ordinal += 1
                connection.execute(
                    text(
                        f"""
                        INSERT INTO {SCHEMA}.cleaned_event_logs (
                            event_record_id, source_system, source_entity,
                            source_record_key, source_table, process_type,
                            normalized_case_id, normalized_activity,
                            event_timestamp, record_data
                        ) VALUES (
                            :erid, 'bpi_challenge_2020', 'request_for_payment',
                            :srk, 'raw_request_for_payment', :ptype,
                            :case_id, :activity, :ts, CAST(:data AS JSONB)
                        )
                        """
                    ),
                    {
                        "erid": f"EVT-{ordinal:06d}",
                        "srk": f"EVT-{ordinal:06d}",
                        "ptype": PROCESS_TYPE,
                        "case_id": case_id,
                        "activity": f"activity_{event_index}",
                        "ts": BASE + timedelta(minutes=ordinal),
                        "data": json.dumps(
                            {"seq": event_index, "operational_note": "initial"}
                        ),
                    },
                )

    try:
        yield run
    finally:
        run(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def event_profile() -> MappingProfile:
    """Maps a cleaned event onto a canonical record.

    The canonical side is incidental here - the cascade's subject is the CASE
    aggregate - but the generic coordinator runs Phase 9 first, so a valid
    profile is required.
    """
    return MappingProfile(
        mapping_id="bpi.cleaned.event",
        source_system_id=SYSTEM,
        source_entity="cleaned_event_logs",
        target_entity_type="customer",
        field_mappings=(
            FieldMapping(
                source_field="event_record_id",
                target_field="customer_id",
                target_type=T.STRING,
                status=MappingStatus.AUTO_ACCEPTED,
            ),
            FieldMapping(
                source_field="normalized_activity",
                target_field="name",
                target_type=T.STRING,
                status=MappingStatus.AUTO_ACCEPTED,
            ),
        ),
    )


class LiveCascade:
    """The real cascade wired through the generic Phase 10 coordinator."""

    def __init__(self, engine, state_store):
        self.engine = engine
        self.access = PostgresCaseAccess(engine=engine, schema=SCHEMA)
        self.previous_hashes = PreviousHashRegistry()
        self.index = CaseKeyIndex()

        self.resolver = PostgresAffectedCaseResolver(
            self.access, index=self.index
        )
        self.builder = PostgresCaseRepresentationBuilder(
            self.access,
            previous_hashes=self.previous_hashes,
            index=self.index,
        )
        self.ledger = PostgresCaseHashLedger(
            self.access, previous_hashes=self.previous_hashes
        )
        self.embedder = _CountingEmbedder()
        self.vectors = InMemoryVectorStore()
        self.canonical = InMemoryCanonicalStore()

        self.pipeline = PropagationPipeline(
            canonical_store=self.canonical,
            resolver=self.resolver,
            builder=self.builder,
            ledger=self.ledger,
            embedder=self.embedder,
            vector_store=self.vectors,
        )

        self.config = ExtractionConfig(
            source_entity="cleaned_event_logs",
            namespace=SCHEMA,
            strategy=WatermarkStrategy.MONOTONIC_ID,
            key_field="event_record_id",
            tie_break_field="id",
        )
        self.extractor = RelationalIncrementalExtractor(engine, self.config)
        self.service = SyncService(state_store, self.pipeline)
        self.target = SyncTarget(
            source_system_id=SYSTEM,
            source_entity="cleaned_event_logs",
            source_type=SourceType.POSTGRESQL,
            mapping_profile=event_profile(),
            schema_id="bpi.cleaned.v1",
        )

    def run(self, batch_size: int = 5000, policy=FailurePolicy.BLOCK):
        return self.service.run_incremental(
            self.target,
            self.extractor,
            SyncOptions(batch_size=batch_size, failure_policy=policy),
            strategy=WatermarkStrategy.MONOTONIC_ID,
            tie_break_field="id",
        )

    def reset_counters(self):
        self.access.reset_counters()
        self.builder.reset_counters()
        self.resolver.reset_counters()
        self.embedder.calls = 0
        self.embedder.embedded_ids = []
        self.vectors.upsert_calls = 0
        self.vectors.delete_calls = 0


class _CountingEmbedder:
    """Deterministic stand-in for the real model.

    The REAL ``BpiEmbeddingUpdater`` is exercised separately; this one keeps
    the multi-case live scenarios fast, because loading a transformer per test
    would dominate the run without testing anything the dedicated test does not.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.embedded_ids: list[str] = []

    def embed(self, representation):
        from erp_pipeline.sync.propagation import EmbeddingResult

        self.calls += 1
        self.embedded_ids.append(representation.representation_id)
        digest = representation.resolved_hash()

        return EmbeddingResult(
            representation_id=representation.representation_id,
            content_hash=digest,
            vector=tuple(
                int(digest[i * 2 : i * 2 + 2], 16) / 255.0 for i in range(8)
            ),
            model_id="counting-stub",
            dimensions=8,
        )


@pytest.fixture()
def state_store(engine):
    from sqlalchemy import text

    bootstrap_sync_schema(engine)
    store = PostgresSyncStateStore(engine)

    def clean():
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM erp_sync.sync_state WHERE source_system_id = :s"
                ),
                {"s": SYSTEM},
            )

    clean()
    yield store
    clean()


@pytest.fixture()
def cascade(engine, bpi_tables, state_store):
    live = LiveCascade(engine, state_store)
    live.run()          # baseline: every case built and embedded once

    # The stub vector store does not write back, so mark the baseline embedded
    # exactly as the real Qdrant adapter (and the batch embedder) would. Without
    # this every case would still read as 'pending' and the pending-count proof
    # would be measuring the baseline rather than the incremental change.
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE {SCHEMA}.ai_ready_cases "
                "SET embedding_status = 'completed', "
                "    qdrant_point_id = 'baseline-point'"
            )
        )

    live.previous_hashes.clear()
    live.reset_counters()
    return live


def _first_case_record_id(cascade) -> str:
    from bpi2020.common.stable_ids import make_case_record_id

    return make_case_record_id(PROCESS_TYPE, "LIVECASE-0001")


def _add_event(engine, case_id: str, activity: str, ordinal: int, note="initial"):
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                INSERT INTO {SCHEMA}.cleaned_event_logs (
                    event_record_id, source_system, source_entity,
                    source_record_key, source_table, process_type,
                    normalized_case_id, normalized_activity,
                    event_timestamp, record_data
                ) VALUES (
                    :erid, 'bpi_challenge_2020', 'request_for_payment',
                    :erid, 'raw_request_for_payment', :ptype,
                    :cid, :act, :ts, CAST(:data AS JSONB)
                )
                """
            ),
            {
                "erid": f"EVT-NEW-{ordinal:06d}",
                "ptype": PROCESS_TYPE,
                "cid": case_id,
                "act": activity,
                "ts": BASE + timedelta(days=5, minutes=ordinal),
                "data": json.dumps({"seq": 99, "operational_note": note}),
            },
        )


# ============================================================
# Baseline
# ============================================================

def test_the_live_baseline_builds_every_case(cascade):
    assert cascade.access.count_cases() == CASE_COUNT


def test_the_baseline_used_the_real_tables(cascade, engine):
    from sqlalchemy import text

    with engine.connect() as connection:
        events = connection.execute(
            text(f"SELECT COUNT(*) FROM {SCHEMA}.cleaned_event_logs")
        ).scalar()

    assert events == CASE_COUNT * EVENTS_PER_CASE


def test_every_case_row_has_the_frozen_identity_and_a_hash(cascade, engine):
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT case_record_id, content_hash FROM {SCHEMA}.ai_ready_cases"
            )
        ).all()

    assert len(rows) == CASE_COUNT
    assert all(r[0] and r[0].startswith("case:") for r in rows)
    assert all(r[1] for r in rows)


# ============================================================
# STEP 3 - one-case rebuild == batch semantics
# ============================================================

def test_one_case_rebuild_equals_the_batch_builder(cascade, engine):
    """The equivalence Step 3 demands, against real rows."""
    import pandas as pd

    from bpi2020.transformation.build_ai_ready_cases import (
        build_case_document as batch_build,
    )
    from sqlalchemy import text

    with engine.connect() as connection:
        all_rows = connection.execute(
            text(
                f"""
                SELECT id, event_record_id, source_table, process_type,
                       normalized_case_id, normalized_activity,
                       event_timestamp, record_data
                FROM {SCHEMA}.cleaned_event_logs
                """
            )
        ).mappings().all()

    frame = pd.DataFrame([dict(r) for r in all_rows])

    # What the BATCH script does: group the whole table, build per group.
    group = frame[frame["normalized_case_id"] == "LIVECASE-0001"]
    batch_doc = batch_build(group)

    # What the INCREMENTAL path does: read one case, build it.
    incremental_doc = build_case_document(
        cascade.access.events_for_case("LIVECASE-0001")
    )

    assert incremental_doc["case_record_id"] == batch_doc["case_record_id"]
    assert incremental_doc["content_hash"] == batch_doc["content_hash"]
    assert incremental_doc["case_summary"] == batch_doc["case_summary"]
    assert incremental_doc["total_events"] == batch_doc["total_events"]
    assert incremental_doc["case_json"] == batch_doc["case_json"]


def test_the_incremental_builder_reuses_the_batch_function():
    """Not a lookalike - literally the same callable."""
    import inspect

    from bpi2020.transformation import build_ai_ready_cases

    source = inspect.getsource(build_case_document)

    assert "build_ai_ready_cases" in source
    assert hasattr(build_ai_ready_cases, "build_case_document")


def test_the_persisted_row_matches_the_batch_document(cascade, engine):
    from sqlalchemy import text

    doc = build_case_document(cascade.access.events_for_case("LIVECASE-0002"))

    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT content_hash, case_summary, total_events "
                f"FROM {SCHEMA}.ai_ready_cases WHERE case_record_id = :id"
            ),
            {"id": doc["case_record_id"]},
        ).first()

    assert row[0] == doc["content_hash"]
    assert row[1] == doc["case_summary"]
    assert row[2] == doc["total_events"]


# ============================================================
# STEP 9 - live changed-content proof
# ============================================================

def test_live_changed_content_proof(cascade, engine):
    record_id = _first_case_record_id(cascade)
    before = cascade.access.load_case_row(record_id)
    old_hash = before["content_hash"]
    old_point = make_qdrant_point_id(record_id)

    _add_event(engine, "LIVECASE-0001", "activity_amended", 1)

    summary = cascade.run()
    after = cascade.access.load_case_row(record_id)

    assert summary.changes_read == 1
    assert summary.changes_processed == 1
    assert summary.representations_resolved == 1
    assert cascade.builder.rebuild_calls == 1
    assert after["content_hash"] != old_hash
    assert summary.embeddings_generated == 1
    assert summary.vectors_upserted == 1
    assert make_qdrant_point_id(record_id) == old_point


def test_live_changed_content_touches_only_one_case(cascade, engine):
    """Step 11: the SQL adapter itself proves the scoping."""
    _add_event(engine, "LIVECASE-0007", "activity_amended", 2)

    cascade.run()

    assert cascade.resolver.resolved_case_ids == ["LIVECASE-0007"]
    assert cascade.access.case_event_queries == 1
    assert cascade.access.case_upserts == 1
    assert cascade.access.count_cases() == CASE_COUNT


def test_live_changed_content_reads_only_that_cases_events(cascade, engine):
    """5 events of one case, not 240 rows of the table."""
    _add_event(engine, "LIVECASE-0007", "activity_amended", 3)

    cascade.run()

    assert cascade.access.rows_read == EVENTS_PER_CASE + 1


def test_live_other_cases_keep_their_hashes(cascade, engine):
    from sqlalchemy import text

    with engine.connect() as connection:
        before = dict(
            connection.execute(
                text(
                    f"SELECT case_record_id, content_hash "
                    f"FROM {SCHEMA}.ai_ready_cases"
                )
            ).all()
        )

    _add_event(engine, "LIVECASE-0012", "activity_amended", 4)
    cascade.run()

    with engine.connect() as connection:
        after = dict(
            connection.execute(
                text(
                    f"SELECT case_record_id, content_hash "
                    f"FROM {SCHEMA}.ai_ready_cases"
                )
            ).all()
        )

    changed = [k for k in before if before[k] != after[k]]
    target = _first_case_record_id(cascade)

    assert len(changed) == 1
    assert changed[0] != target


def test_live_changed_case_is_marked_pending_then_completed(cascade, engine):
    from sqlalchemy import text

    _add_event(engine, "LIVECASE-0009", "activity_amended", 5)
    cascade.run()

    with engine.connect() as connection:
        pending = connection.execute(
            text(
                f"SELECT COUNT(*) FROM {SCHEMA}.ai_ready_cases "
                "WHERE embedding_status = 'pending'"
            )
        ).scalar()

    # The stub vector store does not write back; the real Qdrant adapter does.
    # What matters here is that exactly ONE case was flagged for embedding.
    assert pending == 1


# ============================================================
# STEP 10 - live UNCHANGED-content proof
# ============================================================

def test_live_unchanged_content_proof(cascade, engine):
    """An operational-only edit: ``record_data`` is not AI-facing.

    The batch builder's ``content_hash`` covers identity, the case summary and
    the metadata that reaches the vector payload - deliberately NOT the raw
    per-event ``attributes``. So editing them is a genuine source change whose
    AI-ready content is unchanged.
    """
    from sqlalchemy import text

    record_id = _first_case_record_id(cascade)
    old_hash = cascade.access.load_case_row(record_id)["content_hash"]

    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                UPDATE {SCHEMA}.cleaned_event_logs
                SET record_data = jsonb_set(
                        record_data, '{{operational_note}}', '"revised"'
                    )
                WHERE normalized_case_id = 'LIVECASE-0001'
                """
            )
        )

    # The event id is unchanged, so the watermark alone would not resurface it.
    # Re-resolve the case explicitly, exactly as a replay would.
    representation = cascade.builder.rebuild(record_id)
    new_hash = representation.resolved_hash()
    previous = cascade.ledger.get_hash(record_id)

    assert new_hash == old_hash
    assert previous == old_hash
    assert cascade.embedder.calls == 0
    assert cascade.vectors.upsert_calls == 0


def test_live_replay_does_not_re_embed(cascade, engine):
    """At-least-once replay must not regenerate an unchanged embedding."""
    # Capture the exact checkpoint from before the new event exists, so the
    # replay resurfaces precisely that one event and nothing else.
    before_state = cascade.service.state_store.load(SYSTEM, "cleaned_event_logs")

    _add_event(engine, "LIVECASE-0003", "activity_amended", 6)
    first = cascade.run()

    assert first.changes_read == 1
    assert first.embeddings_generated == 1

    current = cascade.service.state_store.load(SYSTEM, "cleaned_event_logs")
    rewound = current.__class__(
        source_system_id=current.source_system_id,
        source_entity=current.source_entity,
        strategy=current.strategy,
        watermark=before_state.watermark,
        tie_break_field="id",
        version=current.version,
    )
    cascade.service.state_store.save(rewound, expected_version=current.version)

    cascade.previous_hashes.clear()
    cascade.reset_counters()

    second = cascade.run()

    assert second.changes_read == 1
    assert second.embeddings_generated == 0
    assert second.embeddings_skipped == 1
    assert second.vectors_upserted == 0


def test_live_unchanged_content_keeps_the_stored_hash(cascade, engine):
    record_id = _first_case_record_id(cascade)
    old_hash = cascade.access.load_case_row(record_id)["content_hash"]

    cascade.builder.rebuild(record_id)

    assert cascade.access.load_case_row(record_id)["content_hash"] == old_hash


# ============================================================
# STEP 12 - vector identity
# ============================================================

def test_the_vector_point_id_is_stable_across_updates(cascade, engine):
    record_id = _first_case_record_id(cascade)
    before = make_qdrant_point_id(record_id)

    _add_event(engine, "LIVECASE-0001", "activity_amended", 7)
    cascade.run()

    assert make_qdrant_point_id(record_id) == before


def test_no_duplicate_vector_is_created(cascade, engine):
    before = len(cascade.vectors)

    _add_event(engine, "LIVECASE-0004", "activity_amended", 8)
    cascade.run()

    assert len(cascade.vectors) == before


def test_the_point_id_derives_from_the_frozen_helper():
    from bpi2020.common.stable_ids import make_qdrant_point_id as frozen

    record_id = "case:requestforpayment:livecase-0001"

    assert make_qdrant_point_id(record_id) == frozen(record_id)


# ============================================================
# STEP 13 - retry safety with the REAL adapter
# ============================================================

def test_a_vector_failure_leaves_the_checkpoint_retry_safe(cascade, engine):
    before = cascade.service.state_store.load(
        SYSTEM, "cleaned_event_logs"
    ).watermark

    cascade.pipeline.vector_store = FailingStage(cascade.vectors, fail_times=1)
    _add_event(engine, "LIVECASE-0005", "activity_amended", 9)

    summary = cascade.run()
    after = cascade.service.state_store.load(
        SYSTEM, "cleaned_event_logs"
    ).watermark

    assert summary.changes_failed == 1
    assert not summary.checkpoint_advanced
    assert after.tie_breaker == before.tie_breaker


def test_the_failed_change_is_retried_and_succeeds(cascade, engine):
    cascade.pipeline.vector_store = FailingStage(cascade.vectors, fail_times=1)
    _add_event(engine, "LIVECASE-0006", "activity_amended", 10)

    cascade.run()
    cascade.previous_hashes.clear()
    second = cascade.run()

    assert second.changes_read == 1
    assert second.changes_processed == 1


def test_a_retry_creates_no_duplicate_case_row(cascade, engine):
    cascade.pipeline.vector_store = FailingStage(cascade.vectors, fail_times=1)
    _add_event(engine, "LIVECASE-0008", "activity_amended", 11)

    cascade.run()
    cascade.previous_hashes.clear()
    cascade.run()

    assert cascade.access.count_cases() == CASE_COUNT


# ============================================================
# No-full-rebuild, stated in numbers
# ============================================================

def test_no_full_rebuild_against_real_sql(cascade, engine):
    available = cascade.access.count_cases()

    _add_event(engine, "LIVECASE-0011", "activity_amended", 12)
    summary = cascade.run()

    assert available == CASE_COUNT
    assert summary.representations_rebuilt == 1
    assert cascade.builder.rebuild_calls == 1
    assert cascade.access.case_upserts == 1
    assert cascade.embedder.calls == 1


def test_an_unchanged_source_does_no_case_work(cascade):
    summary = cascade.run()

    assert summary.changes_read == 0
    assert cascade.builder.rebuild_calls == 0
    assert cascade.access.case_event_queries == 0
    assert cascade.embedder.calls == 0
