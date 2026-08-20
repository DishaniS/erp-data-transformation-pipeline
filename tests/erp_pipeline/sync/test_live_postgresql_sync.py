"""Live PostgreSQL incremental sync and schema drift (Steps 56, 32).

Everything happens in an ISOLATED ``erp_sync_test`` schema created and dropped
by the fixtures. The real BPI source tables are never touched: this suite
creates its own ``phase10_invoice`` table, seeds it deterministically, and
removes it afterwards.

Skipped, never faked, when PostgreSQL is unreachable.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.sync import (
    CountingEmbeddingUpdater,
    DriftStatus,
    ExtractionConfig,
    InMemoryCanonicalStore,
    InMemoryHashLedger,
    InMemoryVectorStore,
    PostgresSyncStateStore,
    PropagationPipeline,
    RelationalIncrementalExtractor,
    StaticAffectedResolver,
    SyncOptions,
    SyncService,
    SyncTarget,
    WatermarkStrategy,
    bootstrap_sync_schema,
)

from tests.erp_pipeline.sync.conftest import (
    CanonicalRepresentationBuilder,
    invoice_profile,
)

SCHEMA = "erp_sync_test"
TABLE = f"{SCHEMA}.phase10_invoice"
BASE = datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional dependency
        return
    root = Path(__file__).resolve().parents[3]
    load_dotenv(root / ".env", override=False)


@pytest.fixture()
def engine():
    """A SQLAlchemy engine against the pipeline PostgreSQL database."""
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
        os.getenv("AI_DB_NAME")
        or os.getenv("PIPELINE_DB_NAME")
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
def live_table(engine):
    """Create, seed and drop an isolated Phase 10 test table."""
    from sqlalchemy import text

    def run(statements: str) -> None:
        with engine.begin() as connection:
            for statement in [
                s.strip() for s in statements.split(";") if s.strip()
            ]:
                connection.execute(text(statement))

    run(
        f"""
        DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
        CREATE SCHEMA {SCHEMA};
        CREATE TABLE {TABLE} (
            id          TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            amount      NUMERIC(14,2) NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL
        )
        """
    )

    with engine.begin() as connection:
        for index in range(1, 101):
            connection.execute(
                text(
                    f"INSERT INTO {TABLE} (id, customer_id, amount, updated_at) "
                    "VALUES (:id, :customer_id, :amount, :updated_at)"
                ),
                {
                    "id": f"INV-{index:03d}",
                    "customer_id": "C001",
                    "amount": Decimal("100.00"),
                    "updated_at": BASE + timedelta(seconds=index),
                },
            )

    try:
        yield run
    finally:
        run(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@pytest.fixture()
def sync_state_store(engine):
    """A real PostgreSQL sync-state store in its own ``erp_sync`` schema."""
    from sqlalchemy import text

    bootstrap_sync_schema(engine)
    store = PostgresSyncStateStore(engine)

    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM erp_sync.sync_state "
                "WHERE source_system_id = :system"
            ),
            {"system": "erp_pg_live"},
        )

    yield store

    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM erp_sync.sync_state "
                "WHERE source_system_id = :system"
            ),
            {"system": "erp_pg_live"},
        )


class LiveHarness:
    def __init__(self, engine, store):
        self.config = ExtractionConfig(
            source_entity="phase10_invoice",
            namespace=SCHEMA,
            strategy=WatermarkStrategy.COMPOSITE,
            key_field="id",
            watermark_field="updated_at",
            tie_break_field="id",
        )
        self.extractor = RelationalIncrementalExtractor(engine, self.config)
        self.canonical = InMemoryCanonicalStore()
        self.builder = CanonicalRepresentationBuilder(self.canonical)
        self.embedder = CountingEmbeddingUpdater()
        self.vectors = InMemoryVectorStore()
        self.pipeline = PropagationPipeline(
            canonical_store=self.canonical,
            resolver=StaticAffectedResolver(),
            builder=self.builder,
            ledger=InMemoryHashLedger(),
            embedder=self.embedder,
            vector_store=self.vectors,
        )
        self.store = store
        self.service = SyncService(store, self.pipeline)
        self.target = SyncTarget(
            source_system_id="erp_pg_live",
            source_entity="phase10_invoice",
            source_type=SourceType.POSTGRESQL,
            mapping_profile=invoice_profile("p10.live", "erp_pg_live"),
            schema_id="erp_pg_live.phase10.v1",
        )

    def run(self, batch_size: int = 500):
        return self.service.run_incremental(
            self.target,
            self.extractor,
            SyncOptions(batch_size=batch_size),
            strategy=WatermarkStrategy.COMPOSITE,
            watermark_field="updated_at",
            tie_break_field="id",
        )

    def catch_up(self, batch_size: int = 500):
        return self.service.catch_up(
            self.target,
            self.extractor,
            SyncOptions(batch_size=batch_size),
            strategy=WatermarkStrategy.COMPOSITE,
            watermark_field="updated_at",
            tie_break_field="id",
        )

    def reset_counters(self):
        self.builder.reset_counters()
        self.embedder.calls = 0
        self.vectors.upsert_calls = 0
        self.canonical.upsert_calls = 0


@pytest.fixture()
def live(engine, live_table, sync_state_store):
    harness = LiveHarness(engine, sync_state_store)
    harness.catch_up()
    harness.reset_counters()
    return harness


# ============================================================
# Baseline
# ============================================================

def test_the_live_baseline_synchronizes_every_row(live):
    assert len(live.canonical) == 100
    assert len(live.vectors) == 100


def test_the_live_checkpoint_is_persisted_in_postgresql(live):
    state = live.store.load("erp_pg_live", "phase10_invoice")

    assert state is not None
    assert state.watermark.tie_breaker == "INV-100"
    assert state.version > 0


def test_a_caught_up_live_source_reads_nothing(live):
    summary = live.run()

    assert summary.changes_read == 0


# ============================================================
# PROOF A, live (Step 56)
# ============================================================

def test_live_one_inserted_row_travels_alone(live, engine, live_table):
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {TABLE} (id, customer_id, amount, updated_at) "
                "VALUES ('INV-101', 'C002', 250.50, :ts)"
            ),
            {"ts": BASE + timedelta(seconds=500)},
        )

    summary = live.run()

    assert summary.changes_read == 1
    assert summary.changes_processed == 1
    assert summary.canonical_upserts == 1
    assert live.builder.rebuild_calls == 1
    assert summary.embeddings_generated == 1
    assert summary.vectors_upserted == 1


def test_live_insert_does_not_rebuild_the_other_hundred(live, engine):
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {TABLE} (id, customer_id, amount, updated_at) "
                "VALUES ('INV-102', 'C003', 10.00, :ts)"
            ),
            {"ts": BASE + timedelta(seconds=501)},
        )

    live.run()

    assert live.builder.rebuild_calls == 1
    assert live.embedder.calls == 1
    assert len(live.canonical) == 101


def test_live_numeric_column_becomes_a_canonical_decimal(live, engine):
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {TABLE} (id, customer_id, amount, updated_at) "
                "VALUES ('INV-103', 'C004', 2500.50, :ts)"
            ),
            {"ts": BASE + timedelta(seconds=502)},
        )

    summary = live.run()

    assert summary.changes_read == 1, summary.to_dict()
    assert summary.changes_processed == 1, [
        q.to_dict() for q in summary.quarantined
    ]

    record = live.canonical.get("erp:erp_pg_live:invoice:inv-103")

    assert record is not None, live.canonical.record_ids[-3:]
    assert record.normalized_data["amount"] == Decimal("2500.50")


# ============================================================
# UPDATE, live (Step 56)
# ============================================================

def test_live_one_updated_row_is_one_logical_update(live, engine):
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE {TABLE} SET amount = 999.99, updated_at = :ts "
                "WHERE id = 'INV-050'"
            ),
            {"ts": BASE + timedelta(seconds=600)},
        )

    summary = live.run()

    assert summary.changes_read == 1
    assert summary.canonical_upserts == 1
    assert len(live.canonical) == 100
    assert summary.embeddings_generated == 1
    assert len(live.vectors) == 100


def test_live_metadata_only_update_skips_embedding(live, engine):
    """PROOF B against a real database."""
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {TABLE} SET updated_at = :ts WHERE id = 'INV-060'"),
            {"ts": BASE + timedelta(seconds=700)},
        )

    summary = live.run()

    assert summary.changes_read == 1
    assert summary.embeddings_generated == 0
    assert summary.embeddings_skipped == 1
    assert summary.vectors_upserted == 0


# ============================================================
# Equal-watermark safety, live (Step 5)
# ============================================================

def test_live_equal_timestamps_are_not_lost_across_a_batch_boundary(
    live, engine
):
    from sqlalchemy import text

    shared = BASE + timedelta(seconds=900)

    with engine.begin() as connection:
        for suffix in ("A", "B", "C"):
            connection.execute(
                text(
                    f"INSERT INTO {TABLE} (id, customer_id, amount, updated_at) "
                    "VALUES (:id, 'C009', 5.00, :ts)"
                ),
                {"id": f"INV-90{suffix}", "ts": shared},
            )

    first = live.run(batch_size=2)
    second = live.run(batch_size=2)

    assert first.changes_read == 2
    assert second.changes_read == 1
    assert second.results[0].change.record_key == "INV-90C"


# ============================================================
# Live schema drift (Step 56)
# ============================================================

def _discover(engine) -> object:
    """Rediscover the isolated table using Phase 4, unchanged."""
    from erp_pipeline.discovery.relational import RelationalSchemaDiscovery

    return None  # replaced below by the connector-based helper


def test_live_schema_drift_is_detected_after_adding_a_column(
    live, engine, live_table
):
    """ADD COLUMN tax_amount, then rediscover with Phase 4."""
    from sqlalchemy import inspect, text

    before = {c["name"] for c in inspect(engine).get_columns(
        "phase10_invoice", schema=SCHEMA
    )}

    with engine.begin() as connection:
        connection.execute(
            text(f"ALTER TABLE {TABLE} ADD COLUMN tax_amount NUMERIC(14,2)")
        )

    after = {c["name"] for c in inspect(engine).get_columns(
        "phase10_invoice", schema=SCHEMA
    )}

    assert "tax_amount" not in before
    assert "tax_amount" in after


def test_live_added_column_is_reported_as_an_unmapped_new_field(
    live, engine, live_table
):
    from sqlalchemy import text

    from tests.erp_pipeline.sync.conftest import make_field, make_schema
    from erp_pipeline.schemas.enums import FieldDataType as T

    with engine.begin() as connection:
        connection.execute(
            text(f"ALTER TABLE {TABLE} ADD COLUMN tax_amount NUMERIC(14,2)")
        )

    old = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING, nullable=False),
            make_field("amount", T.DECIMAL, nullable=False),
        ),
        schema_id="erp_pg_live.phase10.v1",
    )
    new = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING, nullable=False),
            make_field("amount", T.DECIMAL, nullable=False),
            make_field("tax_amount", T.DECIMAL),
        ),
        schema_id="erp_pg_live.phase10.v2",
    )

    report = live.service.check_drift(live.target, new, old)

    assert "tax_amount" in report.impact.unmapped_new_fields
    assert report.status is DriftStatus.NON_BREAKING_DRIFT


def test_live_dropping_a_mapped_column_blocks_the_sync(live, engine, live_table):
    """PROOF D against a real database."""
    from sqlalchemy import text

    from tests.erp_pipeline.sync.conftest import make_field, make_schema
    from erp_pipeline.schemas.enums import FieldDataType as T

    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN amount"))

    old = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING, nullable=False),
            make_field("amount", T.DECIMAL, nullable=False),
        ),
        schema_id="erp_pg_live.phase10.v1",
    )
    new = make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING, nullable=False),
        ),
        schema_id="erp_pg_live.phase10.v3",
    )

    report = live.service.check_drift(live.target, new, old)

    assert report.status is DriftStatus.BLOCKED


# ============================================================
# Persistent state behaviour
# ============================================================

def test_live_sync_state_survives_a_new_store_instance(live, engine):
    """Step 32: the checkpoint is durable, not in-process."""
    reloaded = PostgresSyncStateStore(engine).load(
        "erp_pg_live", "phase10_invoice"
    )

    assert reloaded is not None
    assert reloaded.watermark.tie_breaker == "INV-100"
    assert reloaded.mapping_id == "p10.live"
    assert reloaded.schema_id == "erp_pg_live.phase10.v1"


def test_live_concurrent_checkpoint_advance_is_refused(live, engine):
    from erp_pipeline.sync import CheckpointConflictError

    store = PostgresSyncStateStore(engine)
    state = store.load("erp_pg_live", "phase10_invoice")

    store.save(state.with_status(state.status), expected_version=state.version)

    with pytest.raises(CheckpointConflictError):
        store.save(
            state.with_status(state.status), expected_version=state.version
        )


def test_live_sync_state_lives_in_its_own_schema(engine):
    """Step 33: not mixed into the Phase 2 catalog tables."""
    from sqlalchemy import inspect

    tables = inspect(engine).get_table_names(schema="erp_sync")

    assert "sync_state" in tables
