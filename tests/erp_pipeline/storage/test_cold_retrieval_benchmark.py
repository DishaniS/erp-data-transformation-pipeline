"""COLD is retrieval-evaluated the same way HOT and WARM are.

A lossless round trip proves the bytes came back. It does not prove the tier
retrieves the right record, because retrieval also depends on the index, the
distance metric and the query. Those are different properties, so COLD gets a
measured Recall@k rather than an inferred one.

The corpus here is small so the suite stays fast; the full 500-vector, 40-query
evaluation lives in `scripts/benchmark_tiered_storage.py` and its artifact is
checked for completeness at the bottom of this file.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from erp_pipeline.storage.benchmark import build_corpus, build_queries
from erp_pipeline.storage.cold_tier import (
    ColdArchiveTier,
    StaticKeyProvider,
    generate_key,
)
from erp_pipeline.storage.hot_tier import QdrantHotTier
from erp_pipeline.storage.metrics import (
    evaluate_recall,
    measure_latency,
    ranking_overlap,
)
from erp_pipeline.storage.models import StorageTier

from .conftest import TEST_COLLECTION_PREFIX, make_embedding, make_vector

ARTIFACT = (
    Path(__file__).resolve().parents[3] / "artifacts" / "tiered_storage_benchmark.json"
)

DIMENSION = 16
CORPUS = 24
QUERIES = 8


def _distinct_vector(index: int, dimension: int = DIMENSION) -> tuple[float, ...]:
    """A one-hot-dominant unit vector, unique per index.

    The shared `make_vector` helper collides at this corpus size and dimension
    (24 seeds produce 17 distinct vectors), and duplicate vectors tie in the
    ranking, which would make Recall@1 measure the fixture rather than the tier.
    """
    raw = [0.05] * dimension
    raw[index % dimension] = 1.0 + (index // dimension)
    norm = sum(value * value for value in raw) ** 0.5

    return tuple(value / norm for value in raw)


def _seeded_records(count: int = CORPUS):
    return [
        make_embedding(
            representation_id=f"ai:invoice:cold-bench-{index:03d}",
            dimension=DIMENSION,
            vector=_distinct_vector(index),
        )
        for index in range(count)
    ]


@pytest.fixture
def cold_corpus(tmp_path: Path):
    tier = ColdArchiveTier(tmp_path / "cold", StaticKeyProvider(generate_key()))
    records = _seeded_records()

    for record in records:
        tier.archive(record)

    return tier, records


@pytest.fixture
def rehydrated_index(qdrant_client, cold_corpus):
    """The real cold search path: archives -> temp collection -> queries."""
    tier, records = cold_corpus
    name = f"{TEST_COLLECTION_PREFIX}coldbench_{uuid.uuid4().hex[:8]}"
    index = QdrantHotTier(qdrant_client, name, DIMENSION)
    index.ensure_collection(recreate=True)

    try:
        for record in records:
            index.upsert(tier.rehydrate(record.representation_id))

        yield index, tier, records
    finally:
        try:
            qdrant_client.delete_collection(name)
        except Exception:
            pass


def test_the_whole_cold_corpus_rehydrates(rehydrated_index):
    """A partial rehydration would quietly deflate every recall number."""
    index, _, records = rehydrated_index

    assert index.count() == len(records)


def test_cold_recall_is_measured_not_inferred(rehydrated_index):
    """Each archived record must be retrievable by its own vector."""
    index, _, records = rehydrated_index

    from erp_pipeline.sync.hashing import vector_id_for

    rankings = [
        [point for point, _ in index.search(record.vector, limit=5)]
        for record in records
    ]
    # Ground truth is the record's own deterministic point id - the same
    # identity function the tier used when writing it.
    expected = [vector_id_for(record.representation_id) for record in records]

    result = evaluate_recall("cold", rankings, expected)

    assert result.recall_at(1) == 1.0
    assert result.recall_at(3) == 1.0
    assert result.recall_at(5) == 1.0


def test_cold_ranking_matches_hot_on_the_same_corpus(qdrant_client, cold_corpus):
    """Full-precision rehydration should reproduce HOT's ranking exactly.

    Asserted rather than assumed - if archiving ever became lossy, this is the
    test that would notice.
    """
    tier, records = cold_corpus
    token = uuid.uuid4().hex[:8]
    hot_name = f"{TEST_COLLECTION_PREFIX}coldcmp_hot_{token}"
    cold_name = f"{TEST_COLLECTION_PREFIX}coldcmp_cold_{token}"

    hot = QdrantHotTier(qdrant_client, hot_name, DIMENSION)
    cold_index = QdrantHotTier(qdrant_client, cold_name, DIMENSION)
    hot.ensure_collection(recreate=True)
    cold_index.ensure_collection(recreate=True)

    try:
        for record in records:
            hot.upsert(record)
            cold_index.upsert(tier.rehydrate(record.representation_id))

        queries = [make_vector(index + 200, DIMENSION) for index in range(QUERIES)]

        hot_rankings = [[p for p, _ in hot.search(q, limit=5)] for q in queries]
        cold_rankings = [[p for p, _ in cold_index.search(q, limit=5)] for q in queries]

        assert ranking_overlap(hot_rankings, cold_rankings, k=5) == 1.0
    finally:
        for name in (hot_name, cold_name):
            try:
                qdrant_client.delete_collection(name)
            except Exception:
                pass


def test_cold_rehydration_and_search_are_timed_separately(rehydrated_index):
    """Mixing preparation into per-query latency would flatter or damn COLD.

    They are different costs with different shapes: rehydration is paid once
    for the corpus, the query cost is paid per query.
    """
    index, tier, records = rehydrated_index

    rehydration = measure_latency(
        "cold_rehydrate_one",
        lambda i: tier.rehydrate(records[i % len(records)].representation_id),
        iterations=10,
        warmup=2,
    )
    search = measure_latency(
        "cold_post_rehydration_search",
        lambda i: index.search(records[i % len(records)].vector, limit=5),
        iterations=10,
        warmup=2,
    )

    assert rehydration.median_ms > 0
    assert search.median_ms > 0
    assert rehydration.to_dict()["label"] != search.to_dict()["label"]


def test_the_vector_roundtrip_proof_is_kept_alongside_recall(cold_corpus):
    """Numerical integrity and retrieval quality are both reported, separately."""
    tier, records = cold_corpus

    worst = max(
        max(
            abs(a - b)
            for a, b in zip(tier.rehydrate(record.representation_id).vector, record.vector)
        )
        for record in records
    )

    assert worst == 0.0


# ----------------------------------------------------------------------
# The published artifact must actually contain the COLD evaluation
# ----------------------------------------------------------------------


@pytest.mark.skipif(not ARTIFACT.exists(), reason="benchmark artifact not generated yet")
def test_artifact_reports_cold_recall():
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    cold = payload["recall"]["cold"]

    for key in ("recall_at_1", "recall_at_3", "recall_at_5"):
        assert isinstance(cold[key], (int, float))

    assert isinstance(payload["recall"]["cold_hot_top5_overlap"], (int, float))
    assert payload["recall"]["cold_vector_roundtrip"]["lossless"] is True


@pytest.mark.skipif(not ARTIFACT.exists(), reason="benchmark artifact not generated yet")
def test_artifact_reports_cold_rehydration_and_search_latency():
    latency = json.loads(ARTIFACT.read_text(encoding="utf-8"))["latency"]
    rehydration = latency["cold_rehydration"]

    for key in (
        "archive_read_time_ms",
        "decrypt_decompress_deserialize_time_ms",
        "temporary_index_population_time_ms",
        "rehydration_total_ms",
        "rehydration_per_record_ms",
    ):
        assert rehydration[key] >= 0

    assert latency["cold_post_rehydration_search"]["median_ms"] > 0
    assert latency["total_cold_access_latency"]["definition"]


@pytest.mark.skipif(not ARTIFACT.exists(), reason="benchmark artifact not generated yet")
def test_artifact_uses_the_same_dataset_and_queries_for_every_tier():
    """The comparison is only meaningful if all three saw identical inputs."""
    dataset = json.loads(ARTIFACT.read_text(encoding="utf-8"))["dataset"]

    assert dataset["size"] == 500
    assert dataset["query_count"] == 40

    for tier in ("hot", "warm", "cold"):
        assert dataset["per_tier"][tier]["dataset_size"] == 500
        assert dataset["per_tier"][tier]["query_count"] == 40


@pytest.mark.skipif(not ARTIFACT.exists(), reason="benchmark artifact not generated yet")
def test_artifact_still_separates_measured_proxy_and_estimated():
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    safety = payload["claim_safety"]

    assert safety["measured"]
    assert safety["proxy"]
    assert safety["estimated"]
    assert any("monetary" in claim.lower() for claim in safety["not_claimed"])
