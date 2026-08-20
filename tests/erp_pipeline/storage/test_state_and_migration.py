"""Tier state, the migration engine, monitoring and the hybrid facade.

These tests use in-memory tiers and an in-memory state store so every migration
path can be exercised, including the failure paths that a live server will not
produce on demand.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.cold_tier import (
    ColdArchiveTier,
    StaticKeyProvider,
    generate_key,
)
from erp_pipeline.storage.errors import (
    ConcurrencyConflictError,
    MigrationError,
    StorageError,
    VectorIdentityMismatchError,
)
from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import MigrationEngine, TierSet
from erp_pipeline.storage.models import (
    StorageTier,
    TierTransition,
    TransitionReason,
    make_transition_id,
)
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.storage.storage_policy import DEFAULT_POLICY
from erp_pipeline.storage.tier_monitor import TierMonitor
from erp_pipeline.storage.vector_router import StoragePolicyRouter

from .conftest import DIMENSION, make_embedding, make_metadata, make_vector

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class FakeVectorTier:
    """An in-memory stand-in with the same surface as the Qdrant tiers.

    Needed because some required behaviours - a write that fails midway, for
    instance - cannot be provoked reliably against a real server.
    """

    def __init__(self, tier: StorageTier, dimension: int = DIMENSION) -> None:
        self.tier = tier
        self.dimension = dimension
        self.records: dict[str, EmbeddingRecord] = {}
        self.payloads: dict[str, dict] = {}
        self.fail_on_upsert = False
        self.upsert_calls = 0
        self.delete_calls = 0

    def upsert(self, record: EmbeddingRecord, payload=None) -> str:
        self.upsert_calls += 1

        if self.fail_on_upsert:
            raise StorageError(f"simulated {self.tier.value} write failure")

        self.records[record.representation_id] = record
        self.payloads[record.representation_id] = dict(payload or {})

        return "00000000-0000-0000-0000-000000000001"

    def get_vector(self, representation_id: str):
        record = self.records.get(representation_id)
        return record.vector if record else None

    def get_record(self, representation_id: str) -> EmbeddingRecord | None:
        return self.records.get(representation_id)

    def stored_payload(self, representation_id: str) -> dict:
        return self.payloads.get(representation_id, {})

    def exists(self, representation_id: str) -> bool:
        return representation_id in self.records

    def delete(self, representation_id: str) -> bool:
        self.delete_calls += 1
        self.payloads.pop(representation_id, None)
        return self.records.pop(representation_id, None) is not None

    def count(self) -> int:
        return len(self.records)

    def search(self, vector, limit: int = 5):
        return [(rid, 1.0) for rid in list(self.records)[:limit]]


@pytest.fixture
def tiers(tmp_path: Path) -> TierSet:
    return TierSet(
        hot=FakeVectorTier(StorageTier.HOT),
        warm=FakeVectorTier(StorageTier.WARM),
        cold=ColdArchiveTier(tmp_path / "cold", StaticKeyProvider(generate_key())),
    )


@pytest.fixture
def state() -> InMemoryTierStateStore:
    return InMemoryTierStateStore()


@pytest.fixture
def store(tiers: TierSet, state: InMemoryTierStateStore) -> HybridVectorStore:
    return HybridVectorStore(tiers, state, router=StoragePolicyRouter(DEFAULT_POLICY))


# ----------------------------------------------------------------------
# State persistence and concurrency
# ----------------------------------------------------------------------


def test_state_round_trips(state: InMemoryTierStateStore):
    metadata = make_metadata(now=NOW)
    saved = state.save(metadata)

    loaded = state.load(metadata.representation_id)

    assert loaded is not None
    assert loaded.representation_id == metadata.representation_id
    assert loaded.current_tier is metadata.current_tier
    assert loaded.version == saved.version


def test_the_version_advances_when_the_tier_changes(state: InMemoryTierStateStore):
    """The version rides on the value object; the store persists and checks it."""
    first = state.save(make_metadata(now=NOW))
    second = state.save(first.with_tier(StorageTier.WARM))

    assert second.version == first.version + 1
    assert state.load(first.representation_id).version == second.version


def test_stale_write_is_rejected(state: InMemoryTierStateStore):
    """Two writers, one stale. The loser must be told, not silently overwritten."""
    original = state.save(make_metadata(now=NOW))
    state.save(original.with_tier(StorageTier.WARM))  # another writer wins

    # The loser still holds the pre-move snapshot and its stale version.
    with pytest.raises(ConcurrencyConflictError):
        state.save(
            original.with_tier(StorageTier.COLD), expected_version=original.version
        )


def test_access_statistics_accumulate(state: InMemoryTierStateStore):
    metadata = state.save(make_metadata(access_count=0, now=NOW))

    before = metadata.last_accessed_at

    for _ in range(3):
        state.record_access(metadata.representation_id)

    updated = state.load(metadata.representation_id)

    assert updated.access_count == 3
    assert updated.last_accessed_at > before


def test_transitions_are_recorded_in_order(state: InMemoryTierStateStore):
    metadata = state.save(make_metadata(now=NOW))

    for index, (source, target) in enumerate(
        ((StorageTier.HOT, StorageTier.WARM), (StorageTier.WARM, StorageTier.COLD))
    ):
        state.record_transition(
            TierTransition(
                transition_id=make_transition_id(
                    metadata.representation_id, target, NOW + timedelta(days=index)
                ),
                representation_id=metadata.representation_id,
                vector_id=metadata.vector_id,
                from_tier=source,
                to_tier=target,
                reason=TransitionReason.AGE_DEMOTION,
                policy_id=DEFAULT_POLICY.policy_id,
                policy_version=DEFAULT_POLICY.version,
                occurred_at=NOW + timedelta(days=index),
                succeeded=True,
            )
        )

    history = state.transitions_for(metadata.representation_id)

    assert [t.to_tier for t in history] == [StorageTier.WARM, StorageTier.COLD]


# ----------------------------------------------------------------------
# Migration: every path, plus the failure and idempotency cases
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,destination",
    [
        (StorageTier.HOT, StorageTier.WARM),
        (StorageTier.WARM, StorageTier.COLD),
        (StorageTier.COLD, StorageTier.WARM),
        (StorageTier.WARM, StorageTier.HOT),
        (StorageTier.HOT, StorageTier.COLD),
        (StorageTier.COLD, StorageTier.HOT),
    ],
)
def test_every_migration_path_preserves_identity_and_vector(
    store: HybridVectorStore, tiers: TierSet, source: StorageTier, destination: StorageTier
):
    record = make_embedding(seed=5)
    metadata, _ = store.store(record, override=source, override_reason="test setup")

    assert metadata.current_tier is source

    moved, transition = store.migrate(record.representation_id, destination)

    assert transition.succeeded
    assert moved.current_tier is destination
    assert moved.representation_id == metadata.representation_id
    assert moved.embedding_id == metadata.embedding_id
    assert moved.vector_id == metadata.vector_id

    restored = store.get(record.representation_id)

    assert restored.vector == pytest.approx(record.vector, abs=1e-6)


def test_source_copy_is_removed_so_there_is_one_authority(
    store: HybridVectorStore, tiers: TierSet
):
    """Two live copies means two answers to 'where does this vector live'."""
    record = make_embedding()
    store.store(record, override=StorageTier.HOT, override_reason="setup")
    store.migrate(record.representation_id, StorageTier.WARM)

    assert not tiers.hot.exists(record.representation_id)
    assert tiers.warm.exists(record.representation_id)


def test_failed_migration_leaves_the_source_intact(
    store: HybridVectorStore, tiers: TierSet
):
    """The cardinal rule: never delete the only copy before the new one lands."""
    record = make_embedding()
    store.store(record, override=StorageTier.HOT, override_reason="setup")
    tiers.warm.fail_on_upsert = True

    with pytest.raises(MigrationError) as caught:
        store.migrate(record.representation_id, StorageTier.WARM)

    assert caught.value.source_intact is True
    assert tiers.hot.exists(record.representation_id)
    assert store.metadata_for(record.representation_id).current_tier is StorageTier.HOT


def test_failed_migration_is_recorded_as_a_failure(
    store: HybridVectorStore, tiers: TierSet, state: InMemoryTierStateStore
):
    record = make_embedding()
    store.store(record, override=StorageTier.HOT, override_reason="setup")
    tiers.warm.fail_on_upsert = True

    with pytest.raises(MigrationError):
        store.migrate(record.representation_id, StorageTier.WARM)

    failures = [
        t for t in state.transitions_for(record.representation_id) if not t.succeeded
    ]

    assert failures
    assert failures[-1].to_tier is StorageTier.WARM


def test_migrating_to_the_current_tier_is_a_no_op(
    store: HybridVectorStore, tiers: TierSet
):
    """Re-running a plan must not rewrite data that is already in place."""
    record = make_embedding()
    store.store(record, override=StorageTier.WARM, override_reason="setup")
    before = tiers.warm.upsert_calls

    moved, transition = store.migrate(record.representation_id, StorageTier.WARM)

    assert moved.current_tier is StorageTier.WARM
    assert tiers.warm.upsert_calls == before
    assert transition is None or transition.succeeded


def test_concurrent_migration_is_rejected(store: HybridVectorStore):
    """Two schedulers must not both move the same record."""
    record = make_embedding()
    metadata, _ = store.store(record, override=StorageTier.HOT, override_reason="setup")

    store.migrate(record.representation_id, StorageTier.WARM)

    with pytest.raises((ConcurrencyConflictError, MigrationError)):
        store.migration_engine.migrate(
            metadata,  # a stale snapshot from before the first move
            StorageTier.COLD,
            expected_version=metadata.version,
        )


def test_a_destination_storing_the_wrong_shape_is_detected(
    store: HybridVectorStore, tiers: TierSet
):
    """WARM is deliberately lossy, so verification there is structural.

    Comparing components exactly against an int8 tier would fail on correct
    data, so the check asserts the vector is present and the right shape. A
    destination that silently stored a different dimension must still be caught.
    """
    record = make_embedding(seed=3)
    store.store(record, override=StorageTier.HOT, override_reason="setup")

    truncated = replace(record, vector=make_vector(3, DIMENSION - 2))
    original_upsert = tiers.warm.upsert

    tiers.warm.upsert = lambda rec, payload=None: original_upsert(truncated, payload)

    with pytest.raises((VectorIdentityMismatchError, MigrationError)):
        store.migrate(record.representation_id, StorageTier.WARM)


def test_cold_verification_compares_the_actual_components(
    store: HybridVectorStore, tiers: TierSet
):
    """COLD is lossless, so its verification decrypts and compares values.

    This is the strongest check in the engine and the reason a corrupt archive
    can never cause the source copy to be deleted.
    """
    record = make_embedding(seed=4)
    store.store(record, override=StorageTier.HOT, override_reason="setup")

    original_archive = tiers.cold.archive
    wrong = replace(record, vector=make_vector(77))

    tiers.cold.archive = lambda rec, payload=None: original_archive(wrong, payload)

    with pytest.raises((VectorIdentityMismatchError, MigrationError)):
        store.migrate(record.representation_id, StorageTier.COLD)

    assert tiers.hot.exists(record.representation_id)


# ----------------------------------------------------------------------
# Hybrid search
# ----------------------------------------------------------------------


def test_search_covers_hot_and_warm_but_not_cold_by_default(store: HybridVectorStore):
    """Cold has no index; searching it silently would hide a huge cost."""
    store.store(make_embedding(representation_id="ai:invoice:a", seed=1),
                override=StorageTier.HOT, override_reason="setup")
    store.store(make_embedding(representation_id="ai:invoice:b", seed=2),
                override=StorageTier.WARM, override_reason="setup")
    store.store(make_embedding(representation_id="ai:invoice:c", seed=3),
                override=StorageTier.COLD, override_reason="setup")

    result = store.search(make_vector(1), limit=10)

    assert set(result.tiers_searched) == {StorageTier.HOT, StorageTier.WARM}
    assert "ai:invoice:c" not in {hit.representation_id for hit in result.hits}


def test_search_does_not_return_the_same_record_twice(store: HybridVectorStore):
    store.store(make_embedding(representation_id="ai:invoice:a", seed=1),
                override=StorageTier.HOT, override_reason="setup")

    ids = [hit.representation_id for hit in store.search(make_vector(1), limit=10).hits]

    assert len(ids) == len(set(ids))


def test_search_results_carry_their_tier(store: HybridVectorStore):
    store.store(make_embedding(representation_id="ai:invoice:a", seed=1),
                override=StorageTier.WARM, override_reason="setup")

    hits = store.search(make_vector(1), limit=5).hits

    assert all(hit.tier in (StorageTier.HOT, StorageTier.WARM) for hit in hits)


# ----------------------------------------------------------------------
# Monitoring: evaluate, plan, dry run, execute
# ----------------------------------------------------------------------


def test_dry_run_plans_without_moving_anything(
    store: HybridVectorStore, tiers: TierSet, state: InMemoryTierStateStore
):
    """A plan you cannot inspect before it runs is not a plan."""
    monitor = TierMonitor(state, store.migration_engine, StoragePolicyRouter(DEFAULT_POLICY))
    record = make_embedding()
    store.store(record, override=StorageTier.HOT, override_reason="setup")

    stale = replace(
        state.load(record.representation_id),
        created_at=NOW - timedelta(days=900),
        last_accessed_at=NOW - timedelta(days=900),
        tier_since=NOW - timedelta(days=900),
        access_count=0,
    )
    state.save(stale)

    plan = monitor.plan_migrations(now=NOW)
    result = monitor.execute_migrations(plan=plan, dry_run=True, now=NOW)

    assert plan.migrations
    assert result.plan.dry_run is True
    assert result.succeeded == 0
    assert tiers.hot.exists(record.representation_id)
    assert state.load(record.representation_id).current_tier is StorageTier.HOT


def test_distribution_counts_every_tier(store: HybridVectorStore, state: InMemoryTierStateStore):
    monitor = TierMonitor(state, store.migration_engine, StoragePolicyRouter(DEFAULT_POLICY))

    for tier in (StorageTier.HOT, StorageTier.WARM, StorageTier.COLD):
        store.store(
            make_embedding(representation_id=f"ai:invoice:{tier.value}", seed=1),
            override=tier,
            override_reason="setup",
        )

    distribution = monitor.distribution()

    assert distribution[StorageTier.HOT.value] == 1
    assert distribution[StorageTier.WARM.value] == 1
    assert distribution[StorageTier.COLD.value] == 1


# ----------------------------------------------------------------------
# Rehydration and deletion
# ----------------------------------------------------------------------


def test_rehydration_brings_a_cold_record_back_unchanged(store: HybridVectorStore):
    record = make_embedding(seed=11)
    store.store(record, override=StorageTier.COLD, override_reason="setup")

    metadata, _ = store.rehydrate(record.representation_id, StorageTier.WARM)

    assert metadata.current_tier is StorageTier.WARM
    assert store.get(record.representation_id).vector == pytest.approx(
        record.vector, abs=1e-6
    )


def test_delete_removes_the_record_and_its_state(
    store: HybridVectorStore, state: InMemoryTierStateStore
):
    record = make_embedding()
    store.store(record, override=StorageTier.HOT, override_reason="setup")

    assert store.delete(record.representation_id) is True
    assert state.load(record.representation_id) is None
    assert store.get(record.representation_id) is None


def test_delete_respects_legal_hold_unless_forced(
    store: HybridVectorStore, state: InMemoryTierStateStore
):
    """Retention that a plain delete can bypass is not retention."""
    from erp_pipeline.storage.errors import RetentionProtectedError

    record = make_embedding()
    store.store(record, override=StorageTier.HOT, override_reason="setup", legal_hold=True)

    with pytest.raises(RetentionProtectedError):
        store.delete(record.representation_id)

    assert store.delete(record.representation_id, force=True) is True
