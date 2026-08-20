"""A Phase 10 content update must reach a record wherever it currently lives.

Phase 10 decides that a record changed and re-embeds it. Phase 12 must apply
that update to the record's actual tier - including WARM and COLD - without
losing the update, stranding a stale copy, or resurrecting the vector in a tier
policy had moved it out of.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import TierSet
from erp_pipeline.storage.models import StorageTier
from erp_pipeline.storage.state import InMemoryTierStateStore

from .conftest import make_embedding, make_vector
from .test_state_and_migration import FakeVectorTier

from erp_pipeline.storage.cold_tier import (
    ColdArchiveTier,
    StaticKeyProvider,
    generate_key,
)


@pytest.fixture
def tiers(tmp_path: Path) -> TierSet:
    return TierSet(
        hot=FakeVectorTier(StorageTier.HOT),
        warm=FakeVectorTier(StorageTier.WARM),
        cold=ColdArchiveTier(tmp_path / "cold", StaticKeyProvider(generate_key())),
    )


@pytest.fixture
def store(tiers: TierSet) -> HybridVectorStore:
    return HybridVectorStore(tiers, InMemoryTierStateStore())


@pytest.mark.parametrize("tier", [StorageTier.WARM, StorageTier.COLD])
def test_an_update_reaches_a_record_that_is_not_in_hot(
    store: HybridVectorStore, tier: StorageTier
):
    """The update must land in the tier the record actually occupies."""
    original = make_embedding(seed=1, content_hash="hash-v1")
    store.store(original, override=tier, override_reason="setup")

    updated = replace(
        original, vector=make_vector(42), content_hash="hash-v2"
    )
    metadata, _ = store.store(updated, override=tier, override_reason="stay put")

    assert metadata.current_tier is tier
    assert metadata.content_hash == "hash-v2"

    restored = store.get(original.representation_id)

    assert restored.vector == pytest.approx(updated.vector, abs=1e-6)


@pytest.mark.parametrize("tier", [StorageTier.WARM, StorageTier.COLD])
def test_an_update_preserves_identity(store: HybridVectorStore, tier: StorageTier):
    """An update changes content, never the record's identity."""
    original = make_embedding(seed=1, content_hash="hash-v1")
    first, _ = store.store(original, override=tier, override_reason="setup")

    updated = replace(original, vector=make_vector(42), content_hash="hash-v2")
    second, _ = store.store(updated, override=tier, override_reason="stay put")

    assert second.representation_id == first.representation_id
    assert second.embedding_id == first.embedding_id
    assert second.vector_id == first.vector_id


def test_an_update_does_not_reset_the_records_history(store: HybridVectorStore):
    """Access history is evidence for tiering. An update must not erase it."""
    original = make_embedding(content_hash="hash-v1")
    store.store(original, override=StorageTier.WARM, override_reason="setup")

    for _ in range(4):
        store.state.record_access(original.representation_id)

    before = store.metadata_for(original.representation_id)

    updated = replace(original, vector=make_vector(9), content_hash="hash-v2")
    store.store(updated, override=StorageTier.WARM, override_reason="stay put")

    after = store.metadata_for(original.representation_id)

    assert after.access_count == before.access_count
    assert after.created_at == before.created_at


def test_an_update_that_moves_tier_leaves_no_stale_copy_behind(
    store: HybridVectorStore, tiers: TierSet
):
    """If an update relocates a record, the old tier must not keep serving it.

    Two live copies means a search can return the pre-update vector, which is
    exactly the corruption Phase 10's re-embedding exists to prevent.
    """
    original = make_embedding(content_hash="hash-v1")
    store.store(original, override=StorageTier.WARM, override_reason="setup")

    assert tiers.warm.exists(original.representation_id)

    updated = replace(original, vector=make_vector(9), content_hash="hash-v2")
    metadata, _ = store.store(
        updated, override=StorageTier.HOT, override_reason="promoted on update"
    )

    assert metadata.current_tier is StorageTier.HOT
    assert tiers.hot.exists(original.representation_id)
    assert not tiers.warm.exists(original.representation_id)
