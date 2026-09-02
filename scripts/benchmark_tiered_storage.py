"""Run the Phase 12 hybrid tiered storage benchmark against live infrastructure.

    python scripts/run_phase12_benchmark.py

Requires a reachable Qdrant. Uses its OWN isolated collections, prefixed
``erp_phase12_bench_``, and deletes them at the end. The production BPI
collection is never opened, read or written.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qdrant_client import QdrantClient  # noqa: E402

from erp_pipeline.ai.embedding import SentenceTransformerModel  # noqa: E402
from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus  # noqa: E402
from erp_pipeline.storage.benchmark import (  # noqa: E402
    build_corpus,
    build_queries,
    write_artifact,
)
from erp_pipeline.storage.cold_tier import (  # noqa: E402
    ColdArchiveTier,
    StaticKeyProvider,
    generate_key,
)
from erp_pipeline.storage.cost import DEFAULT_COST_MODEL  # noqa: E402
from erp_pipeline.storage.hot_tier import QdrantHotTier  # noqa: E402
from erp_pipeline.storage.metrics import (  # noqa: E402
    LatencySample,
    evaluate_recall,
    measure_latency,
    ranking_overlap,
    vector_payload_proxy,
)
from erp_pipeline.storage.models import (  # noqa: E402
    STORAGE_ENGINE_VERSION,
    BusinessCriticality,
    LatencyRequirement,
    MeasurementKind,
    StorageLocation,
    StorageRecordMetadata,
    StorageTier,
    TransitionReason,
)
from erp_pipeline.storage.state import InMemoryTierStateStore  # noqa: E402
from erp_pipeline.storage.storage_policy import (  # noqa: E402
    DEFAULT_POLICY,
    DEFAULT_TIER_LOCATIONS,
)
from erp_pipeline.storage.vector_router import StoragePolicyRouter  # noqa: E402
from erp_pipeline.storage.warm_tier import QdrantWarmTier  # noqa: E402
from erp_pipeline.schemas.enums import SensitivityLevel  # noqa: E402

PREFIX = "erp_phase12_bench_"
DIMENSION = 384


def log(message: str) -> None:
    print(message, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6333)
    parser.add_argument("--corpus-size", type=int, default=500)
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--movement", type=int, default=100)
    parser.add_argument(
        "--output", default=str(ROOT / "artifacts" / "phase12_storage_benchmark.json")
    )
    args = parser.parse_args()

    started_at = datetime.now(timezone.utc)
    client = QdrantClient(host=args.host, port=args.port, timeout=120)
    client.get_collections()

    # ------------------------------------------------------------------
    # Corpus: real MiniLM embeddings, one corpus shared by all three tiers
    # ------------------------------------------------------------------
    log(f"[1/8] Embedding a {args.corpus_size}-record ERP corpus with MiniLM ...")
    corpus = build_corpus(args.corpus_size)
    queries = build_queries(corpus, args.queries)

    model = SentenceTransformerModel()
    fingerprint = model.fingerprint(probe_normalization=True)

    embed_started = time.perf_counter()
    vectors = model.encode([record.text for record in corpus])
    embed_seconds = time.perf_counter() - embed_started
    query_vectors = model.encode([query.query for query in queries])

    log(
        f"      {len(vectors)} vectors, dim={len(vectors[0])}, "
        f"{embed_seconds:.1f}s, normalized={fingerprint.normalizes_output}"
    )

    hot_name = f"{PREFIX}hot"
    warm_name = f"{PREFIX}warm"
    cold_root = Path(tempfile.mkdtemp(prefix="erp_phase12_bench_cold_"))
    created: list[str] = []

    try:
        # --------------------------------------------------------------
        # Load the SAME corpus into each tier
        # --------------------------------------------------------------
        log("[2/8] Loading the identical corpus into HOT, WARM and COLD ...")
        hot = QdrantHotTier(client, hot_name, DIMENSION)
        hot.ensure_collection(recreate=True)
        created.append(hot_name)

        warm = QdrantWarmTier(client, warm_name, DIMENSION)
        warm.ensure_collection(recreate=True)
        created.append(warm_name)

        cold = ColdArchiveTier(cold_root, StaticKeyProvider(generate_key()))

        now = datetime.now(timezone.utc)
        metadata_by_id: dict[str, StorageRecordMetadata] = {}

        embeddings: dict[str, EmbeddingRecord] = {}

        for record, vector in zip(corpus, vectors):
            embedding = EmbeddingRecord(
                embedding_id=f"emb:{record.representation_id}",
                representation_id=record.representation_id,
                content_hash=f"hash-{record.ordinal}",
                model_id=fingerprint.model_id,
                dimension=DIMENSION,
                status=EmbeddingStatus.GENERATED,
                entity_type=record.entity_type,
                vector=tuple(vector),
            )
            embeddings[record.representation_id] = embedding

            payload = {"text": record.text, "entity_type": record.entity_type}
            point_id = hot.upsert(embedding, payload)
            warm.upsert(embedding, payload)
            cold.archive(embedding, payload)

            metadata_by_id[record.representation_id] = StorageRecordMetadata(
                representation_id=record.representation_id,
                embedding_id=embedding.embedding_id,
                vector_id=str(point_id),
                entity_type=record.entity_type,
                dimension=DIMENSION,
                model_id=fingerprint.model_id,
                content_hash=f"hash-{record.ordinal}",
                current_tier=StorageTier.HOT,
                created_at=now - timedelta(days=record.ordinal % 400),
                last_accessed_at=now - timedelta(days=record.ordinal % 200),
                access_count=record.ordinal % 30,
                sensitivity=(
                    SensitivityLevel.RESTRICTED
                    if record.ordinal % 11 == 0
                    else SensitivityLevel.INTERNAL
                ),
                business_criticality=list(BusinessCriticality)[record.ordinal % 4],
                latency_requirement=list(LatencyRequirement)[record.ordinal % 3],
            )

        warm_quantization = warm.quantization_verified()
        hot_config = hot.live_configuration()
        warm_config = warm.live_configuration()

        log(
            f"      hot: {hot.count()} pts on_disk={hot_config['on_disk']} "
            f"quantization={hot_config['quantization']}"
        )
        log(
            f"      warm: {warm.count()} pts on_disk={warm_config['on_disk']} "
            f"quantization={warm_config['quantization']} verified={warm_quantization}"
        )
        log(f"      cold: {cold.count()} archives, {cold.total_bytes()} bytes measured")

        # --------------------------------------------------------------
        # Latency, same queries, each tier
        # --------------------------------------------------------------
        log(f"[3/8] Timing {len(queries)} queries per tier ...")

        def hot_query(index: int) -> None:
            hot.search(query_vectors[index % len(query_vectors)], limit=5)

        def warm_query(index: int) -> None:
            warm.search(query_vectors[index % len(query_vectors)], limit=5)

        cold_ids = [record.representation_id for record in corpus]

        def cold_query(index: int) -> None:
            cold.rehydrate(cold_ids[index % len(cold_ids)])

        hot_latency = measure_latency("hot_search", hot_query, iterations=len(queries))
        warm_latency = measure_latency("warm_search", warm_query, iterations=len(queries))
        cold_latency = measure_latency(
            "cold_single_rehydration", cold_query, iterations=len(queries)
        )

        for sample in (hot_latency, warm_latency, cold_latency):
            log(
                f"      {sample.label:26} median={sample.median_ms:8.3f} ms  "
                f"p95={sample.p95_ms:8.3f} ms  n={sample.count}"
            )

        # --------------------------------------------------------------
        # Recall against hand-declared labels
        # --------------------------------------------------------------
        log("[4/8] Scoring Recall@1/3/5 against declared labels ...")
        expected = [query.expected_representation_id for query in queries]

        # The tiers rank by Qdrant point id; recall is declared against
        # representation ids, so the ids must be mapped back before comparing.
        # Comparing the two directly would score every query a miss.
        point_to_representation = {
            metadata.vector_id: representation_id
            for representation_id, metadata in metadata_by_id.items()
        }

        def rank(tier: Any, vector: Sequence[float]) -> list[str]:
            return [
                point_to_representation.get(point_id, point_id)
                for point_id, _ in tier.search(vector, limit=5)
            ]

        hot_rankings = [rank(hot, qv) for qv in query_vectors]
        warm_rankings = [rank(warm, qv) for qv in query_vectors]

        hot_recall = evaluate_recall("hot", hot_rankings, expected)
        warm_recall = evaluate_recall("warm", warm_rankings, expected)
        overlap = ranking_overlap(hot_rankings, warm_rankings, k=5)

        # --------------------------------------------------------------
        # COLD deep search: rehydrate the archive corpus into an isolated
        # temporary collection and run the SAME queries against it.
        #
        # Encrypted archives are never searched in place. The pipeline is
        # read -> decrypt/decompress/deserialize -> populate a throwaway index
        # -> query -> destroy the index.
        # --------------------------------------------------------------
        log("[4b/8] Rehydrating the cold corpus and searching it ...")
        cold_search_name = f"{PREFIX}cold_rehydrated"
        count = len(corpus)

        # Warm the OS page cache first. Without this the first pass pays cold
        # disk I/O and the second does not, which would make the difference
        # between them an artefact of caching rather than a measure of work.
        for record in corpus:
            cold.path_for(record.representation_id).read_bytes()

        # (1) Pure archive I/O: read every archive's bytes off disk.
        read_started = time.perf_counter()
        archive_bytes = sum(
            len(cold.path_for(record.representation_id).read_bytes())
            for record in corpus
        )
        archive_read_seconds = time.perf_counter() - read_started

        # (2) Full rehydration: read + decrypt + decompress + deserialize.
        rehydrate_started = time.perf_counter()
        rehydrated = [cold.rehydrate(record.representation_id) for record in corpus]
        rehydrate_seconds = time.perf_counter() - rehydrate_started

        # (3) Populate the temporary index with the restored vectors.
        cold_search_tier = QdrantHotTier(client, cold_search_name, DIMENSION)
        cold_search_tier.ensure_collection(recreate=True)
        created.append(cold_search_name)

        populate_started = time.perf_counter()
        for restored in rehydrated:
            cold_search_tier.upsert(restored)
        populate_seconds = time.perf_counter() - populate_started

        rehydration_total_seconds = rehydrate_seconds + populate_seconds

        log(
            f"      archive read {archive_read_seconds * 1000:.1f} ms "
            f"({archive_bytes} bytes) | rehydrate "
            f"{rehydrate_seconds * 1000:.1f} ms | index populate "
            f"{populate_seconds * 1000:.1f} ms"
        )
        log(
            f"      rehydration_total = {rehydration_total_seconds * 1000:.1f} ms "
            f"for {len(rehydrated)} records "
            f"({rehydration_total_seconds * 1000 / len(rehydrated):.3f} ms/record)"
        )

        assert cold_search_tier.count() == count, "cold corpus did not fully rehydrate"

        # (4) The SAME 40 queries, now against the rehydrated index.
        def cold_search_query(index: int) -> None:
            cold_search_tier.search(query_vectors[index % len(query_vectors)], limit=5)

        cold_search_latency = measure_latency(
            "cold_post_rehydration_search",
            cold_search_query,
            iterations=len(queries),
        )

        cold_rankings = [rank(cold_search_tier, qv) for qv in query_vectors]
        cold_recall = evaluate_recall("cold", cold_rankings, expected)
        cold_hot_overlap = ranking_overlap(hot_rankings, cold_rankings, k=5)

        log(
            f"      cold R@1={cold_recall.recall_at(1):.3f} "
            f"R@3={cold_recall.recall_at(3):.3f} R@5={cold_recall.recall_at(5):.3f} "
            f"| overlap vs hot = {cold_hot_overlap:.3f}"
        )
        log(
            f"      cold post-rehydration search median="
            f"{cold_search_latency.median_ms:.3f} ms "
            f"p95={cold_search_latency.p95_ms:.3f} ms"
        )

        log(
            f"      hot  R@1={hot_recall.recall_at(1):.3f} "
            f"R@3={hot_recall.recall_at(3):.3f} R@5={hot_recall.recall_at(5):.3f}"
        )
        log(
            f"      warm R@1={warm_recall.recall_at(1):.3f} "
            f"R@3={warm_recall.recall_at(3):.3f} R@5={warm_recall.recall_at(5):.3f}"
        )
        log(f"      hot/warm top-5 ranking overlap = {overlap:.3f}")

        # --------------------------------------------------------------
        # Cold retrieval fidelity: exact, not approximate
        # --------------------------------------------------------------
        log("[5/8] Verifying cold rehydration fidelity ...")
        deviations = []
        for record, vector in list(zip(corpus, vectors))[:50]:
            restored = cold.rehydrate(record.representation_id)
            deviations.append(
                max(abs(a - b) for a, b in zip(restored.vector, vector))
            )
        cold_max_deviation = max(deviations)
        log(f"      max component deviation over 50 archives = {cold_max_deviation:.3e}")

        # --------------------------------------------------------------
        # Footprint and cost
        # --------------------------------------------------------------
        log("[6/8] Measuring footprint and computing the cost proxy ...")
        footprints = {
            StorageTier.HOT: vector_payload_proxy(
                StorageTier.HOT, count, DIMENSION, quantized=False
            ),
            StorageTier.WARM: vector_payload_proxy(
                StorageTier.WARM, count, DIMENSION, quantized=True
            ),
            StorageTier.COLD: vector_payload_proxy(
                StorageTier.COLD, count, DIMENSION, quantized=False
            ),
        }
        cold_measured = cold.footprint()

        costs = {
            tier: DEFAULT_COST_MODEL.cost_for(
                tier, fp.bytes_total, count, MeasurementKind.PROXY
            )
            for tier, fp in footprints.items()
        }
        relative = DEFAULT_COST_MODEL.relative_to_hot(costs)

        for tier, fp in footprints.items():
            log(
                f"      {tier.value:5} proxy {fp.bytes_per_record:7.1f} B/rec  "
                f"total {fp.bytes_total:12.0f} B  "
                f"cost x{relative.get(tier.value, 0):.4f} of hot"
            )
        log(
            f"      cold ARCHIVE measured: {cold_measured.bytes_total:.0f} B total, "
            f"{cold_measured.bytes_per_record:.1f} B/rec "
            "(different scope - includes metadata, header, nonce and GCM tag)"
        )

        # --------------------------------------------------------------
        # Routing distribution over every context, plus the hard invariant
        # --------------------------------------------------------------
        log(f"[7/8] Routing all {count} contexts through the policy ...")
        router = StoragePolicyRouter(DEFAULT_POLICY)
        distribution = {tier.value: 0 for tier in StorageTier}
        reason_codes: dict[str, int] = {}
        restricted_total = 0
        restricted_on_premises = 0

        for metadata in metadata_by_id.values():
            decision = router.route(metadata.to_context(now=now), now=now)
            distribution[decision.selected_tier.value] += 1
            reason_codes[decision.reason_code.value] = (
                reason_codes.get(decision.reason_code.value, 0) + 1
            )

            if metadata.sensitivity is SensitivityLevel.RESTRICTED:
                restricted_total += 1
                if (
                    DEFAULT_TIER_LOCATIONS[decision.selected_tier]
                    is StorageLocation.ON_PREMISES
                ):
                    restricted_on_premises += 1

        log(f"      distribution: {distribution}")
        log(f"      reason codes: {reason_codes}")
        log(
            f"      RESTRICTED invariant (default topology): "
            f"{restricted_on_premises}/{restricted_total} on-premises"
        )

        if restricted_total and restricted_on_premises != restricted_total:
            raise SystemExit("FATAL: a RESTRICTED record was routed to an external tier")

        # Under the default topology every tier is on-premises, so the
        # constraint is satisfied trivially and proves nothing. The real test is
        # a topology where a tier IS external: the prohibition must then remove
        # that tier from the candidate set no matter how cheap it scores.
        external_policy = replace(
            DEFAULT_POLICY,
            tier_locations={
                StorageTier.HOT: StorageLocation.ON_PREMISES,
                StorageTier.WARM: StorageLocation.ON_PREMISES,
                StorageTier.COLD: StorageLocation.EXTERNAL,
            },
        )
        external_router = StoragePolicyRouter(external_policy)
        external_distribution = {tier.value: 0 for tier in StorageTier}
        external_restricted_cold = 0
        non_restricted_cold = 0

        for metadata in metadata_by_id.values():
            decision = external_router.route(metadata.to_context(now=now), now=now)
            external_distribution[decision.selected_tier.value] += 1

            if decision.selected_tier is StorageTier.COLD:
                if metadata.sensitivity is SensitivityLevel.RESTRICTED:
                    external_restricted_cold += 1
                else:
                    non_restricted_cold += 1

        log(
            f"      RESTRICTED invariant (COLD external): "
            f"{external_restricted_cold} restricted records in the external tier "
            f"(must be 0); {non_restricted_cold} non-restricted records still "
            "allowed there"
        )

        if external_restricted_cold:
            raise SystemExit(
                "FATAL: the on-premises-only constraint failed to exclude an "
                "external tier"
            )

        if not non_restricted_cold:
            raise SystemExit(
                "FATAL: no record reached the external tier, so the constraint "
                "was never actually exercised"
            )

        # --------------------------------------------------------------
        # Movement benchmark
        # --------------------------------------------------------------
        log(f"[8/8] Timing tier movement for {args.movement} vectors ...")
        movement_ids = [record.representation_id for record in corpus[: args.movement]]

        demote_started = time.perf_counter()
        for representation_id in movement_ids:
            cold.archive(embeddings[representation_id])
        demote_seconds = time.perf_counter() - demote_started

        promote_started = time.perf_counter()
        for representation_id in movement_ids:
            warm.upsert(cold.rehydrate(representation_id))
        promote_seconds = time.perf_counter() - promote_started

        log(
            f"      warm->cold {demote_seconds * 1000 / len(movement_ids):.3f} ms/vector | "
            f"cold->warm {promote_seconds * 1000 / len(movement_ids):.3f} ms/vector"
        )

        # --------------------------------------------------------------
        # Artifact
        # --------------------------------------------------------------
        artifact = {
            "phase": 12,
            "artifact": "phase12_storage_benchmark",
            "storage_engine_version": STORAGE_ENGINE_VERSION,
            "generated_at": started_at.isoformat(),
            "duration_seconds": round(
                (datetime.now(timezone.utc) - started_at).total_seconds(), 2
            ),
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "qdrant_host": f"{args.host}:{args.port}",
                "collections_used": created,
                "production_collections_touched": [],
                "external_services": [],
                "llm_calls": 0,
            },
            "embedding_model": fingerprint.to_dict(),
            "corpus": {
                "record_count": count,
                "entity_types": sorted({r.entity_type for r in corpus}),
                "dimension": DIMENSION,
                "embedding_seconds": round(embed_seconds, 2),
                "identical_corpus_in_every_tier": True,
                "vectors_are_real_model_output": True,
            },
            "dataset": {
                "size": count,
                "query_count": len(queries),
                "identical_across_tiers": True,
                "per_tier": {
                    tier: {"dataset_size": count, "query_count": len(queries)}
                    for tier in ("hot", "warm", "cold")
                },
            },
            "queries": {
                "count": len(queries),
                "ground_truth": "hand-declared labels, not another tier's ranking",
                "examples": [
                    {
                        "query": q.query,
                        "expected": q.expected_representation_id,
                    }
                    for q in queries[:5]
                ],
            },
            "tier_configuration": {
                "hot": hot_config,
                "warm": warm_config,
                "warm_quantization_server_verified": warm_quantization,
                "cold": {
                    "compression": "gzip level 9",
                    "encryption": "AES-256-GCM",
                    "format_version": "1.0",
                    "key_source": "injected provider; never persisted beside archives",
                },
            },
            "latency": {
                "hot_search": hot_latency.to_dict(),
                "warm_search": warm_latency.to_dict(),
                "cold_single_rehydration": cold_latency.to_dict(),
                "cold_post_rehydration_search": cold_search_latency.to_dict(),
                "cold_rehydration": {
                    "archive_records": count,
                    "archive_bytes_read": archive_bytes,
                    "archive_read_time_ms": round(archive_read_seconds * 1000, 4),
                    "decrypt_decompress_deserialize_time_ms": round(
                        max(0.0, rehydrate_seconds - archive_read_seconds) * 1000, 4
                    ),
                    "decrypt_time_measurement": "DERIVED",
                    "decrypt_time_method": (
                        "full rehydrate elapsed minus the pure archive-read "
                        "elapsed over the same 500 files. The page cache is "
                        "warmed by an untimed pass beforehand so both terms see "
                        "the same cache state and the difference reflects "
                        "decrypt/decompress/deserialize work rather than I/O "
                        "asymmetry"
                    ),
                    "full_rehydrate_time_ms": round(rehydrate_seconds * 1000, 4),
                    "temporary_index_population_time_ms": round(
                        populate_seconds * 1000, 4
                    ),
                    "rehydration_total_ms": round(
                        rehydration_total_seconds * 1000, 4
                    ),
                    "rehydration_per_record_ms": round(
                        rehydration_total_seconds * 1000 / count, 4
                    ),
                    "definition": (
                        "rehydration_total_ms = full rehydrate (read + decrypt + "
                        "decompress + deserialize) + temporary index population"
                    ),
                },
                "total_cold_access_latency": {
                    "definition": (
                        "one-time rehydration_total_ms + per-query "
                        "cold_post_rehydration_search median; the first term is "
                        "amortised across every query served from the "
                        "rehydrated index and is NOT paid per query"
                    ),
                    "one_time_preparation_ms": round(
                        rehydration_total_seconds * 1000, 4
                    ),
                    "per_query_after_preparation_ms": cold_search_latency.median_ms,
                    "first_query_ms": round(
                        rehydration_total_seconds * 1000
                        + cold_search_latency.median_ms,
                        4,
                    ),
                },
                "note": (
                    "cold_single_rehydration is one record fetched by id, NOT a "
                    "similarity search. cold_post_rehydration_search IS a real "
                    "similarity search over the same 500 vectors and the same 40 "
                    "queries, but it is only reachable after paying the one-time "
                    "rehydration cost, so it is not equivalent to an "
                    "always-online ANN query"
                ),
            },
            "recall": {
                "hot": hot_recall.to_dict(),
                "warm": warm_recall.to_dict(),
                "hot_warm_top5_overlap": overlap,
                "overlap_note": (
                    "a diagnostic of how much int8 quantization perturbed the "
                    "ranking; it is not a quality metric"
                ),
                "cold": cold_recall.to_dict(),
                "cold_hot_top5_overlap": cold_hot_overlap,
                "cold_retrieval_model": (
                    "the encrypted archive corpus is rehydrated into an isolated "
                    "temporary Qdrant collection and the same 40 queries are run "
                    "against it; encrypted files are never searched in place"
                ),
                "cold_vector_roundtrip": {
                    "max_component_deviation": cold_max_deviation,
                    "lossless": cold_max_deviation == 0.0,
                    "note": (
                        "numerical integrity of serialize -> compress -> encrypt "
                        "-> decrypt -> decompress -> deserialize. This is a "
                        "separate property from retrieval quality and does not "
                        "substitute for the measured recall above"
                    ),
                },
            },
            "footprint": {
                "comparable_proxy": {
                    tier.value: fp.to_dict() for tier, fp in footprints.items()
                },
                "cold_archive_measured": cold_measured.to_dict(),
                "comparability_warning": (
                    "comparable_proxy counts vector components only and is the ONLY "
                    "cross-tier comparable figure. cold_archive_measured covers a "
                    "different scope (header, nonce, GCM tag, compressed metadata) "
                    "and must never be subtracted from or divided by the proxy."
                ),
            },
            "cost": {
                "model": DEFAULT_COST_MODEL.to_dict(),
                "per_tier": {tier.value: cost.to_dict() for tier, cost in costs.items()},
                "relative_to_hot": relative,
                "disclaimer": (
                    "normalized units, not currency; the multipliers are stated "
                    "assumptions a reader may replace"
                ),
            },
            "routing": {
                "contexts_evaluated": count,
                "distribution": distribution,
                "reason_codes": reason_codes,
                "policy_id": DEFAULT_POLICY.policy_id,
                "policy_version": DEFAULT_POLICY.version,
                "restricted_records": restricted_total,
                "restricted_kept_on_premises": restricted_on_premises,
                "restricted_invariant_held": restricted_on_premises == restricted_total,
            },
            "movement": {
                "vector_count": len(movement_ids),
                "warm_to_cold_ms_per_vector": round(
                    demote_seconds * 1000 / len(movement_ids), 4
                ),
                "cold_to_warm_ms_per_vector": round(
                    promote_seconds * 1000 / len(movement_ids), 4
                ),
                "warm_to_cold_total_seconds": round(demote_seconds, 3),
                "cold_to_warm_total_seconds": round(promote_seconds, 3),
            },
            "claim_safety": {
                "measured": [
                    "cold archive bytes on disk",
                    "cold compression ratio",
                    "all latency samples",
                    "recall against declared labels",
                    "warm int8 quantization read back from the server",
                    "cold rehydration fidelity",
                ],
                "proxy": [
                    "hot, warm and cold vector payload bytes (formula stated)",
                ],
                "estimated": [
                    "cost resource multipliers (experimental assumptions)",
                ],
                "not_claimed": [
                    "monetary savings",
                    "production-scale performance",
                    "generalization beyond this corpus and this model",
                    "that cold latency is comparable to hot or warm search latency",
                ],
            },
        }

        path = write_artifact(artifact, Path(args.output))
        log("")
        log(f"Benchmark artifact written: {path}")

        return 0

    finally:
        for name in created:
            try:
                client.delete_collection(name)
            except Exception:  # pragma: no cover - cleanup must never mask a result
                pass
        shutil.rmtree(cold_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
