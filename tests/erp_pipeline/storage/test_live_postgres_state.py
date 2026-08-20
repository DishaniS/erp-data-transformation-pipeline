"""Live PostgreSQL proof that tier state is genuinely durable.

Everything here runs in a throwaway schema named `erp_phase12_live_<token>`,
created by the test and dropped afterwards. No production table, no baseline
table and no existing schema is read or written.

Durability is the whole point of this store, and durability cannot be proved
in-process: the tests below deliberately discard the Python objects and read
the rows back through a brand-new store instance.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from erp_pipeline.storage.cold_tier import (
    ColdArchiveTier,
    StaticKeyProvider,
    generate_key,
)
from erp_pipeline.storage.errors import ConcurrencyConflictError
from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import TierSet
from erp_pipeline.storage.models import (
    StorageTier,
    TierTransition,
    TransitionReason,
    make_transition_id,
)
from erp_pipeline.storage.state import (
    PostgresTierStateStore,
    bootstrap_storage_schema,
)

from .conftest import make_embedding, make_metadata

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def engine():
    """A live PostgreSQL engine, or a skip that names why it was unavailable."""
    pytest.importorskip("sqlalchemy", reason="sqlalchemy is not installed")
    pytest.importorskip("psycopg2", reason="psycopg2 is not installed")

    import sqlalchemy as sa

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # pragma: no cover - dotenv is optional
        pass

    host = os.getenv("AI_DB_HOST", "localhost")
    port = os.getenv("AI_DB_PORT", "5432")
    name = os.getenv("AI_DB_NAME")
    user = os.getenv("AI_DB_USER")
    password = os.getenv("AI_DB_PASSWORD")

    if not (name and user):
        pytest.skip("AI_DB_* connection settings are not configured")

    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"

    try:
        candidate = sa.create_engine(url)
        with candidate.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as error:  # pragma: no cover - environment dependent
        pytest.skip(f"live PostgreSQL unreachable at {host}:{port}: {error!r}")

    return candidate


@pytest.fixture
def schema(engine):
    """An isolated schema per test, dropped afterwards.

    Isolation is not politeness here - bootstrapping into the real
    `erp_vector_storage` namespace would mean a test run silently created
    production tables.
    """
    import sqlalchemy as sa

    name = f"erp_phase12_live_{uuid.uuid4().hex[:10]}"
    bootstrap_storage_schema(engine, schema=name)

    try:
        yield name
    finally:
        with engine.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))


@pytest.fixture
def store(engine, schema) -> PostgresTierStateStore:
    return PostgresTierStateStore(engine, schema=schema)


# ----------------------------------------------------------------------
# Step 9 - bootstrap and the basic durable operations
# ----------------------------------------------------------------------


def test_bootstrap_creates_the_three_tables(engine, schema):
    import sqlalchemy as sa

    with engine.connect() as connection:
        found = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema"
                ),
                {"schema": schema},
            )
        }

    assert {"vector_storage_state", "vector_tier_transitions", "vector_access_stats"} <= found


def test_bootstrap_is_idempotent(engine, schema):
    """A second run must not fail; startup should be safe to repeat."""
    bootstrap_storage_schema(engine, schema=schema)
    bootstrap_storage_schema(engine, schema=schema)


def test_state_insert_and_read(store: PostgresTierStateStore):
    metadata = make_metadata(now=NOW)
    store.save(metadata)

    loaded = store.load(metadata.representation_id)

    assert loaded is not None
    assert loaded.representation_id == metadata.representation_id
    assert loaded.current_tier is metadata.current_tier
    assert loaded.embedding_id == metadata.embedding_id
    assert loaded.vector_id == metadata.vector_id
    assert loaded.dimension == metadata.dimension


def test_state_update_and_version_increment(store: PostgresTierStateStore):
    original = store.save(make_metadata(now=NOW))
    moved = original.with_tier(StorageTier.WARM)
    store.save(moved)

    loaded = store.load(original.representation_id)

    assert loaded.current_tier is StorageTier.WARM
    assert loaded.version == original.version + 1


def test_access_statistics_persist(store: PostgresTierStateStore):
    metadata = store.save(make_metadata(access_count=0, now=NOW))

    for _ in range(3):
        store.record_access(metadata.representation_id)

    loaded = store.load(metadata.representation_id)

    assert loaded.access_count == 3
    assert loaded.last_accessed_at is not None


def test_transitions_persist(store: PostgresTierStateStore):
    metadata = store.save(make_metadata(now=NOW))

    store.record_transition(
        TierTransition(
            transition_id=make_transition_id(
                metadata.representation_id, StorageTier.WARM, NOW
            ),
            representation_id=metadata.representation_id,
            vector_id=metadata.vector_id,
            from_tier=StorageTier.HOT,
            to_tier=StorageTier.WARM,
            reason=TransitionReason.AGE_DEMOTION,
            policy_id="erp_hybrid_default",
            policy_version="1.0",
            occurred_at=NOW,
            succeeded=True,
        )
    )

    history = store.transitions_for(metadata.representation_id)

    assert len(history) == 1
    assert history[0].from_tier is StorageTier.HOT
    assert history[0].to_tier is StorageTier.WARM
    assert history[0].succeeded is True


# ----------------------------------------------------------------------
# Step 10 - optimistic concurrency, proved against the real database
# ----------------------------------------------------------------------


def test_stale_write_is_refused_and_the_row_is_not_overwritten(
    store: PostgresTierStateStore,
):
    """N -> N+1 succeeds; a second writer still holding N must be refused."""
    original = store.save(make_metadata(now=NOW))
    assert original.version == 0

    winner = original.with_tier(StorageTier.WARM)
    store.save(winner, expected_version=original.version)

    assert store.load(original.representation_id).version == 1

    loser = original.with_tier(StorageTier.COLD)

    with pytest.raises(ConcurrencyConflictError) as caught:
        store.save(loser, expected_version=original.version)

    assert caught.value.expected_version == 0
    assert caught.value.actual_version == 1

    # The winner's write must still be the one in the database.
    surviving = store.load(original.representation_id)

    assert surviving.current_tier is StorageTier.WARM
    assert surviving.version == 1


# ----------------------------------------------------------------------
# Step 11 - restart persistence
# ----------------------------------------------------------------------


def test_state_survives_the_store_instance_being_destroyed(engine, schema):
    """Process-local state would pass every in-memory test and still be wrong."""
    metadata = make_metadata(now=NOW)

    first = PostgresTierStateStore(engine, schema=schema)
    first.save(metadata.with_tier(StorageTier.WARM))
    del first

    # A brand-new store object, as if the process had restarted.
    second = PostgresTierStateStore(engine, schema=schema)
    loaded = second.load(metadata.representation_id)

    assert loaded is not None
    assert loaded.current_tier is StorageTier.WARM


# ----------------------------------------------------------------------
# Step 12 - a live migration whose state outlives the store
# ----------------------------------------------------------------------


def test_a_live_migration_persists_its_tier_and_its_audit(
    engine, schema, qdrant_client, tmp_path: Path
):
    """Qdrant holds the vector, PostgreSQL holds the truth about where it is."""
    from erp_pipeline.storage.hot_tier import QdrantHotTier
    from erp_pipeline.storage.warm_tier import QdrantWarmTier

    from .conftest import TEST_COLLECTION_PREFIX

    token = uuid.uuid4().hex[:8]
    hot_name = f"{TEST_COLLECTION_PREFIX}pg_hot_{token}"
    warm_name = f"{TEST_COLLECTION_PREFIX}pg_warm_{token}"

    hot = QdrantHotTier(qdrant_client, hot_name, 8)
    warm = QdrantWarmTier(qdrant_client, warm_name, 8)
    hot.ensure_collection(recreate=True)
    warm.ensure_collection(recreate=True)

    cold = ColdArchiveTier(tmp_path / "cold", StaticKeyProvider(generate_key()))
    tiers = TierSet(hot=hot, warm=warm, cold=cold)

    try:
        state = PostgresTierStateStore(engine, schema=schema)
        store = HybridVectorStore(tiers, state)

        record = make_embedding(seed=4)
        store.store(record, override=StorageTier.HOT, override_reason="setup")

        assert hot.exists(record.representation_id)

        store.migrate(record.representation_id, StorageTier.WARM)

        # Qdrant: exactly one tier holds it.
        assert hot.exists(record.representation_id) is False
        assert warm.exists(record.representation_id) is True

        # PostgreSQL: the tier and the audit trail are both recorded.
        persisted = state.load(record.representation_id)
        assert persisted.current_tier is StorageTier.WARM

        moves = [
            t
            for t in state.transitions_for(record.representation_id)
            if t.to_tier is StorageTier.WARM and t.succeeded
        ]
        assert moves, "the HOT -> WARM transition was not persisted"
        assert moves[-1].from_tier is StorageTier.HOT

        # Restart: a new store instance must still agree.
        del store, state
        reopened = PostgresTierStateStore(engine, schema=schema)

        assert reopened.load(record.representation_id).current_tier is StorageTier.WARM

    finally:
        for name in (hot_name, warm_name):
            try:
                qdrant_client.delete_collection(name)
            except Exception:
                pass
