"""A search must resolve only its candidate IDs, regardless of corpus size."""
from dataclasses import replace

from erp_pipeline.storage.state import InMemoryTierStateStore
from tests.erp_pipeline.storage.conftest import make_metadata


def test_batch_lookup_is_deduplicated_and_handles_empty_and_unknown_ids():
    state = InMemoryTierStateStore()
    first = make_metadata()
    state.save(first)
    state.save(replace(first, representation_id="unrelated", vector_id="unrelated-vector"))
    assert state.load_many_by_vector_ids([]) == ()
    result = state.load_many_by_vector_ids([first.vector_id, first.vector_id, "missing"])
    assert result == (first,)


def test_merge_uses_batch_lookup_and_ignores_orphan_vectors():
    from erp_pipeline.storage.hybrid_store import HybridVectorStore, SearchHit
    from erp_pipeline.storage.migration import TierSet
    from erp_pipeline.storage.models import StorageTier
    state = InMemoryTierStateStore()
    first = make_metadata()
    state.save(first)
    state.list_all = lambda *args: (_ for _ in ()).throw(AssertionError("full scan"))
    store = HybridVectorStore(TierSet(), state)
    hits = store._merge([
        SearchHit(None, first.vector_id, .8, StorageTier.HOT),
        SearchHit(None, "orphan", .99, StorageTier.HOT),
    ], 5)
    assert len(hits) == 1 and hits[0].representation_id == first.representation_id


def test_stale_physical_candidate_does_not_consume_the_requested_top_k():
    from erp_pipeline.storage.hybrid_store import HybridVectorStore
    from erp_pipeline.storage.migration import TierSet
    from erp_pipeline.storage.models import StorageTier
    class Tier:
        dimension = 4
        def search(self, vector, limit=5):
            return [("stale-vector", .99), ("current-vector", .8)][:limit]
    state = InMemoryTierStateStore()
    first = make_metadata()
    state.save(replace(first, vector_id="stale-vector", is_current=False))
    state.save(replace(first, representation_id="current", vector_id="current-vector"))
    result = HybridVectorStore(TierSet(hot=Tier()), state).search([.1, .2, .3, .4], limit=1)
    assert len(result.hits) == 1 and result.hits[0].representation_id == "current"
