"""Live Qdrant tests. These prove the tiers differ in fact, not in intent.

Every collection here is created by the test, prefixed and deleted afterwards.
The production BPI collection is never named, opened, read or written.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from erp_pipeline.storage.cold_tier import (
    ColdArchiveTier,
    StaticKeyProvider,
    generate_key,
)
from erp_pipeline.storage.hot_tier import QdrantHotTier
from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import TierSet
from erp_pipeline.storage.models import MeasurementKind, StorageTier
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.storage.warm_tier import QdrantWarmTier

from .conftest import TEST_COLLECTION_PREFIX, make_embedding

DIMENSION = 32


@pytest.fixture
def live_names():
    token = uuid.uuid4().hex[:8]

    return (
        f"{TEST_COLLECTION_PREFIX}hot_{token}",
        f"{TEST_COLLECTION_PREFIX}warm_{token}",
    )


@pytest.fixture
def live_tiers(qdrant_client, live_names, tmp_path: Path):
    hot_name, warm_name = live_names
    hot = QdrantHotTier(qdrant_client, hot_name, DIMENSION)
    warm = QdrantWarmTier(qdrant_client, warm_name, DIMENSION)
    hot.ensure_collection(recreate=True)
    warm.ensure_collection(recreate=True)

    cold = ColdArchiveTier(tmp_path / "cold", StaticKeyProvider(generate_key()))

    try:
        yield TierSet(hot=hot, warm=warm, cold=cold)
    finally:
        for name in (hot_name, warm_name):
            try:
                qdrant_client.delete_collection(name)
            except Exception:
                pass


def test_hot_and_warm_are_configured_differently_on_the_server(live_tiers: TierSet):
    """If the server reports identical settings, there is only one tier."""
    hot = live_tiers.hot.live_configuration()
    warm = live_tiers.warm.live_configuration()

    assert hot["on_disk"] is False
    assert hot["quantization"] is None

    assert warm["on_disk"] is True
    assert warm["quantization"] is not None
    assert live_tiers.warm.quantization_verified() is True


def test_warm_quantization_is_int8_read_back_from_the_server(live_tiers: TierSet):
    quantization = live_tiers.warm.live_configuration()["quantization"]

    assert quantization["kind"] == "scalar"
    assert "INT8" in str(quantization["type"]).upper()


def test_a_vector_survives_a_full_hot_warm_cold_hot_round_trip(live_tiers: TierSet):
    """Critical proof: identity and values are stable across every tier."""
    store = HybridVectorStore(live_tiers, InMemoryTierStateStore())
    record = make_embedding(seed=6, dimension=DIMENSION)

    metadata, _ = store.store(record, override=StorageTier.HOT, override_reason="setup")
    identity = (metadata.representation_id, metadata.embedding_id, metadata.vector_id)

    for destination in (StorageTier.WARM, StorageTier.COLD, StorageTier.HOT):
        metadata, transition = store.migrate(record.representation_id, destination)

        assert transition.succeeded
        assert metadata.current_tier is destination
        assert (
            metadata.representation_id,
            metadata.embedding_id,
            metadata.vector_id,
        ) == identity

    restored = store.get(record.representation_id)

    # float32 storage costs a little precision; a real corruption would not be
    # anywhere near this tolerance.
    assert restored.vector == pytest.approx(record.vector, abs=1e-6)


def test_only_one_tier_holds_the_record_after_a_migration(live_tiers: TierSet):
    store = HybridVectorStore(live_tiers, InMemoryTierStateStore())
    record = make_embedding(seed=2, dimension=DIMENSION)

    store.store(record, override=StorageTier.HOT, override_reason="setup")
    store.migrate(record.representation_id, StorageTier.WARM)

    assert live_tiers.hot.exists(record.representation_id) is False
    assert live_tiers.warm.exists(record.representation_id) is True
    assert live_tiers.cold.exists(record.representation_id) is False


def test_hybrid_search_finds_records_in_both_live_tiers(live_tiers: TierSet):
    store = HybridVectorStore(live_tiers, InMemoryTierStateStore())

    hot_record = make_embedding(representation_id="ai:invoice:live-hot", seed=1,
                                dimension=DIMENSION)
    warm_record = make_embedding(representation_id="ai:invoice:live-warm", seed=2,
                                 dimension=DIMENSION)

    store.store(hot_record, override=StorageTier.HOT, override_reason="setup")
    store.store(warm_record, override=StorageTier.WARM, override_reason="setup")

    result = store.search(hot_record.vector, limit=10)
    found = {hit.representation_id for hit in result.hits}

    assert "ai:invoice:live-hot" in found
    assert "ai:invoice:live-warm" in found
    assert set(result.tiers_searched) == {StorageTier.HOT, StorageTier.WARM}


def test_cold_deep_search_is_opt_in_and_leaves_no_collection_behind(
    live_tiers: TierSet, qdrant_client
):
    """Deep search builds a temporary index; leaking it would be a slow disaster."""
    store = HybridVectorStore(live_tiers, InMemoryTierStateStore())
    record = make_embedding(representation_id="ai:invoice:live-cold", seed=9,
                            dimension=DIMENSION)
    store.store(record, override=StorageTier.COLD, override_reason="setup")

    before = {c.name for c in qdrant_client.get_collections().collections}

    shallow = store.search(record.vector, limit=5)
    assert "ai:invoice:live-cold" not in {h.representation_id for h in shallow.hits}

    deep = store.search(record.vector, limit=5, include_cold=True)

    assert "ai:invoice:live-cold" in {h.representation_id for h in deep.hits}
    assert StorageTier.COLD in deep.tiers_searched

    after = {c.name for c in qdrant_client.get_collections().collections}
    assert after == before


def test_footprints_report_how_they_were_obtained(live_tiers: TierSet):
    """A proxy presented as a measurement is the easiest way to mislead."""
    record = make_embedding(dimension=DIMENSION)
    live_tiers.hot.upsert(record)
    live_tiers.cold.archive(record)

    assert live_tiers.hot.footprint().kind is MeasurementKind.PROXY
    assert live_tiers.cold.footprint().kind is MeasurementKind.MEASURED


def test_health_reports_each_live_tier(live_tiers: TierSet):
    store = HybridVectorStore(live_tiers, InMemoryTierStateStore())
    health = store.health()

    assert health["hot"].available is True
    assert health["warm"].available is True
    assert health["cold"].available is True


def test_the_production_collection_is_never_referenced_by_the_package():
    """Phase 12 is generic. A hard-coded BPI collection would make it BPI-specific."""
    import ast
    from pathlib import Path

    import erp_pipeline.storage as package

    for path in Path(package.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # Strip docstrings: the prose legitimately explains what is banned.
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                if (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                ):
                    node.body.pop(0)

        code = ast.unparse(tree)

        assert "bpi2020_erp_knowledge" not in code, path.name
        assert "import bpi2020" not in code, path.name
