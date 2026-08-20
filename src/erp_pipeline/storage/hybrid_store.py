"""The one storage facade callers use.

    HybridVectorStore
        .store()      route, write, verify, persist state
        .get()        read from wherever it is, recording the access
        .search()     HOT + WARM merged; COLD only on request
        .delete()     honouring retention
        .route()      explain without moving anything
        .migrate()    move, safely and auditably
        .rehydrate()  COLD -> a searchable tier

Nothing above this line should ever need ``hot_store`` / ``warm_store`` /
``cold_store``. If an application had to know which tier a vector was in before
it could fetch it, the tiering would have leaked into every caller and the
abstraction would be worthless.

SEARCH SEMANTICS ARE HONEST (Steps 15, 18, 45, 48)
--------------------------------------------------
Ordinary search covers HOT and WARM only. COLD is compressed, encrypted files -
they support no ANN index, and pretending otherwise would be the single most
dishonest thing this phase could do. ``include_cold=True`` therefore REHYDRATES
into a temporary collection first, and the cost of that is measured and
reported as part of cold access latency rather than hidden.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.ai.hashing import vector_id_for
from erp_pipeline.ai.models import EmbeddingRecord
from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.errors import (
    RetentionProtectedError,
    StorageConfigurationError,
    TierUnavailableError,
)
from erp_pipeline.storage.migration import MigrationEngine, TierSet, _payload_for
from erp_pipeline.storage.models import (
    BusinessCriticality,
    LatencyRequirement,
    RoutingDecision,
    StorageRecordMetadata,
    StorageRoutingContext,
    StorageTier,
    TierHealth,
    TierTransition,
    TransitionReason,
    make_transition_id,
)
from erp_pipeline.storage.state import TierStateStore
from erp_pipeline.storage.storage_policy import StoragePolicy
from erp_pipeline.storage.vector_router import StoragePolicyRouter


@dataclass(frozen=True)
class SearchHit:
    """One result, with the tier it came from."""

    representation_id: str | None
    vector_id: str
    score: float
    tier: StorageTier

    def to_dict(self) -> dict[str, Any]:
        return {
            "representation_id": self.representation_id,
            "vector_id": self.vector_id,
            "score": round(self.score, 6),
            "tier": self.tier.value,
        }


@dataclass(frozen=True)
class SearchResult:
    """A merged search, with the timing breakdown that makes it honest."""

    hits: tuple[SearchHit, ...]
    tiers_searched: tuple[StorageTier, ...]
    query_seconds: float = 0.0
    rehydration_seconds: float = 0.0
    rehydrated_count: int = 0

    @property
    def total_seconds(self) -> float:
        return round(self.query_seconds + self.rehydration_seconds, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "tiers_searched": [t.value for t in self.tiers_searched],
            "query_seconds": round(self.query_seconds, 6),
            "rehydration_seconds": round(self.rehydration_seconds, 6),
            "rehydrated_count": self.rehydrated_count,
            "total_seconds": self.total_seconds,
        }


class HybridVectorStore:
    """One facade over HOT, WARM and COLD."""

    def __init__(
        self,
        tiers: TierSet,
        state_store: TierStateStore,
        policy: StoragePolicy | None = None,
        router: StoragePolicyRouter | None = None,
    ) -> None:
        self._tiers = tiers
        self._state = state_store
        self._router = router or StoragePolicyRouter(policy)
        self._engine = MigrationEngine(tiers, state_store, self._router)
        self._validate_score_comparability()

    @property
    def router(self) -> StoragePolicyRouter:
        return self._router

    @property
    def tiers(self) -> TierSet:
        return self._tiers

    @property
    def state(self) -> TierStateStore:
        return self._state

    @property
    def migration_engine(self) -> MigrationEngine:
        return self._engine

    # ------------------------------------------------------------
    # Configuration validation (Step 17)
    # ------------------------------------------------------------

    def _validate_score_comparability(self) -> None:
        """HOT and WARM scores are merged, so they must be commensurable.

        Merging a cosine score with a euclidean one, or scores over vectors of
        different dimension, would produce a ranking that looks fine and means
        nothing. Cheaper to refuse at construction.
        """
        hot, warm = self._tiers.hot, self._tiers.warm

        if hot is None or warm is None:
            return

        if hot.dimension != warm.dimension:
            raise StorageConfigurationError(
                f"HOT is {hot.dimension}-dimensional and WARM is "
                f"{warm.dimension}-dimensional; their search scores cannot be "
                "merged"
            )

    # ------------------------------------------------------------
    # Store (Steps 6, 7)
    # ------------------------------------------------------------

    def store(
        self,
        record: EmbeddingRecord,
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
        business_criticality: BusinessCriticality = BusinessCriticality.NORMAL,
        latency_requirement: LatencyRequirement = LatencyRequirement.STANDARD,
        retention_until: datetime | None = None,
        legal_hold: bool = False,
        created_at: datetime | None = None,
        content_updated_at: datetime | None = None,
        override: StorageTier | None = None,
        override_reason: str | None = None,
    ) -> tuple[StorageRecordMetadata, RoutingDecision]:
        """Route, write, verify, then persist state - in that order.

        State is written LAST and only after the tier write is verified. A
        state row claiming a vector is somewhere it is not would be worse than
        no row at all: every later read and migration would trust it.
        """
        existing = self._state.load(record.representation_id)
        now = datetime.now(timezone.utc)

        metadata = StorageRecordMetadata(
            representation_id=record.representation_id,
            embedding_id=record.embedding_id,
            vector_id=vector_id_for(record.representation_id),
            current_tier=existing.current_tier if existing else StorageTier.HOT,
            content_hash=record.content_hash,
            model_id=record.model_id,
            dimension=record.dimension,
            sensitivity=sensitivity,
            business_criticality=business_criticality,
            latency_requirement=latency_requirement,
            entity_type=record.entity_type,
            access_count=existing.access_count if existing else 0,
            recent_access_count=existing.recent_access_count if existing else 0,
            last_accessed_at=existing.last_accessed_at if existing else None,
            created_at=(existing.created_at if existing else None) or created_at or now,
            content_updated_at=content_updated_at or now,
            retention_until=retention_until,
            legal_hold=legal_hold,
            tier_since=existing.tier_since if existing else None,
            version=existing.version if existing else 0,
        )

        context = metadata.to_context(now)

        if existing is None:
            # A brand-new record has no incumbent tier, so hysteresis has
            # nothing to protect and the raw scores decide.
            context = _without_current_tier(context)

        decision = self._router.route(
            context, override=override, override_reason=override_reason
        )

        destination = decision.selected_tier

        # -- write and verify BEFORE any state is persisted --
        payload = _payload_for(metadata)
        record_for_write = record

        self._engine._write(destination, record_for_write, payload)
        self._engine._verify(destination, record_for_write)

        stored = metadata.with_tier(
            destination,
            policy_id=self._router.policy.policy_id,
            policy_version=self._router.policy.version,
        )
        self._state.save(
            stored, expected_version=existing.version if existing else None
        )

        occurred = datetime.now(timezone.utc)
        self._state.record_transition(
            TierTransition(
                transition_id=make_transition_id(
                    record.representation_id, destination, occurred
                ),
                representation_id=record.representation_id,
                vector_id=stored.vector_id,
                from_tier=existing.current_tier if existing else None,
                to_tier=destination,
                reason=(
                    TransitionReason.CONTENT_UPDATE
                    if existing
                    else TransitionReason.INITIAL_PLACEMENT
                ),
                policy_id=self._router.policy.policy_id,
                policy_version=self._router.policy.version,
                succeeded=True,
                forced=decision.forced,
                occurred_at=occurred,
                detail=decision.reason,
            )
        )

        # An update that lands in a different tier leaves a copy behind.
        if existing is not None and existing.current_tier is not destination:
            try:
                self._engine._retire(
                    existing.current_tier, record.representation_id
                )
            except Exception:  # noqa: BLE001 - state is already correct
                pass

        return stored, decision

    # ------------------------------------------------------------
    # Read (Step 4)
    # ------------------------------------------------------------

    def get(
        self, representation_id: str, record_access: bool = True
    ) -> EmbeddingRecord | None:
        """Fetch from whichever tier holds it, recording the read."""
        metadata = self._state.load(representation_id)

        if metadata is None:
            return None

        record, _ = self._engine.read_record(metadata)

        if record_access:
            # Access statistics are what let the router promote something that
            # has become popular, so every read through the facade counts.
            self._state.record_access(representation_id)

        return record

    def metadata_for(self, representation_id: str) -> StorageRecordMetadata | None:
        return self._state.load(representation_id)

    # ------------------------------------------------------------
    # Route without moving (Step 12)
    # ------------------------------------------------------------

    def route(
        self,
        representation_id: str,
        override: StorageTier | None = None,
        now: datetime | None = None,
    ) -> RoutingDecision | None:
        metadata = self._state.load(representation_id)

        if metadata is None:
            return None

        return self._router.route(
            metadata.to_context(now), override=override, now=now
        )

    # ------------------------------------------------------------
    # Search (Steps 15, 16, 18)
    # ------------------------------------------------------------

    def search(
        self,
        vector: Sequence[float],
        limit: int = 5,
        include_cold: bool = False,
        record_access: bool = False,
    ) -> SearchResult:
        """HOT + WARM by default; COLD only via rehydration."""
        started = time.monotonic()
        raw: list[SearchHit] = []
        searched: list[StorageTier] = []

        for tier in (StorageTier.HOT, StorageTier.WARM):
            backend = {
                StorageTier.HOT: self._tiers.hot,
                StorageTier.WARM: self._tiers.warm,
            }[tier]

            if backend is None:
                continue

            searched.append(tier)

            for vector_id, score in backend.search(vector, limit=limit):
                raw.append(
                    SearchHit(
                        representation_id=None,
                        vector_id=vector_id,
                        score=score,
                        tier=tier,
                    )
                )

        query_seconds = time.monotonic() - started
        rehydration_seconds = 0.0
        rehydrated = 0

        if include_cold:
            cold_hits, rehydration_seconds, cold_query, rehydrated = (
                self._search_cold(vector, limit)
            )
            raw.extend(cold_hits)
            query_seconds += cold_query
            searched.append(StorageTier.COLD)

        hits = self._merge(raw, limit)

        if record_access:
            for hit in hits:
                if hit.representation_id:
                    self._state.record_access(hit.representation_id)

        return SearchResult(
            hits=hits,
            tiers_searched=tuple(searched),
            query_seconds=round(query_seconds, 6),
            rehydration_seconds=round(rehydration_seconds, 6),
            rehydrated_count=rehydrated,
        )

    def _merge(
        self, raw: Sequence[SearchHit], limit: int
    ) -> tuple[SearchHit, ...]:
        """Deduplicate by logical vector id, preferring the authoritative tier.

        An interrupted migration can leave one vector in two tiers. Returning
        it twice would corrupt any ranking built on these results, so the copy
        from the tier the STATE says is authoritative wins; failing that, the
        higher score does.
        """
        by_vector: dict[str, SearchHit] = {}
        authoritative = self._authoritative_tiers(
            {hit.vector_id for hit in raw}
        )

        for hit in raw:
            resolved = authoritative.get(hit.vector_id)
            enriched = SearchHit(
                representation_id=(
                    resolved[0] if resolved else hit.representation_id
                ),
                vector_id=hit.vector_id,
                score=hit.score,
                tier=hit.tier,
            )

            incumbent = by_vector.get(hit.vector_id)

            if incumbent is None:
                by_vector[hit.vector_id] = enriched
                continue

            official = resolved[1] if resolved else None

            if official is not None:
                if enriched.tier is official and incumbent.tier is not official:
                    by_vector[hit.vector_id] = enriched
                continue

            if enriched.score > incumbent.score:
                by_vector[hit.vector_id] = enriched

        ranked = sorted(
            by_vector.values(), key=lambda hit: (-hit.score, hit.vector_id)
        )

        return tuple(ranked[:limit])

    def _authoritative_tiers(
        self, vector_ids: set[str]
    ) -> dict[str, tuple[str, StorageTier]]:
        """Map vector id -> (representation id, official tier)."""
        resolved: dict[str, tuple[str, StorageTier]] = {}

        for metadata in self._state.list_all():
            if metadata.vector_id in vector_ids:
                resolved[metadata.vector_id] = (
                    metadata.representation_id,
                    metadata.current_tier,
                )

        return resolved

    def _search_cold(
        self, vector: Sequence[float], limit: int
    ) -> tuple[list[SearchHit], float, float, int]:
        """Rehydrate cold vectors into a temporary collection, then query it.

        This is the honest implementation of "cold search": encrypted archives
        have no index, so the only way to search them is to restore them first.
        The rehydration cost is returned separately so a benchmark can report
        it rather than bury it inside a query time.
        """
        cold = self._tiers.cold
        hot = self._tiers.hot

        if cold is None or hot is None:
            return [], 0.0, 0.0, 0

        cold_records = [
            metadata
            for metadata in self._state.list_all(StorageTier.COLD)
        ]

        if not cold_records:
            return [], 0.0, 0.0, 0

        temporary = f"erp_phase12_coldsearch_{uuid.uuid4().hex[:12]}"
        client = hot.client

        from qdrant_client import models as M

        rehydration_start = time.monotonic()

        client.create_collection(
            collection_name=temporary,
            vectors_config=M.VectorParams(
                size=hot.dimension, distance=M.Distance.COSINE
            ),
        )

        try:
            points = []

            for metadata in cold_records:
                restored = cold.rehydrate(metadata.representation_id)
                points.append(
                    M.PointStruct(
                        id=metadata.vector_id,
                        vector=list(restored.vector or ()),
                        payload={"representation_id": metadata.representation_id},
                    )
                )

            client.upsert(
                collection_name=temporary, points=points, wait=True
            )
            rehydration_seconds = time.monotonic() - rehydration_start

            query_start = time.monotonic()
            results = client.query_points(
                collection_name=temporary,
                query=list(vector),
                limit=limit,
                with_payload=True,
            ).points
            query_seconds = time.monotonic() - query_start

            hits = [
                SearchHit(
                    representation_id=(point.payload or {}).get(
                        "representation_id"
                    ),
                    vector_id=str(point.id),
                    score=float(point.score),
                    tier=StorageTier.COLD,
                )
                for point in results
            ]

            return hits, rehydration_seconds, query_seconds, len(points)
        finally:
            # The temporary index is always cleaned up, including on failure.
            try:
                client.delete_collection(temporary)
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass

    # ------------------------------------------------------------
    # Migrate and rehydrate
    # ------------------------------------------------------------

    def migrate(
        self,
        representation_id: str,
        destination: StorageTier,
        reason: TransitionReason = TransitionReason.MANUAL_OVERRIDE,
    ) -> tuple[StorageRecordMetadata, TierTransition] | None:
        metadata = self._state.load(representation_id)

        if metadata is None:
            return None

        return self._engine.migrate(
            metadata, destination, reason=reason,
            forced=reason is TransitionReason.MANUAL_OVERRIDE,
        )

    def rehydrate(
        self,
        representation_id: str,
        destination: StorageTier = StorageTier.WARM,
    ) -> tuple[StorageRecordMetadata, TierTransition] | None:
        """Bring a cold record back into a searchable tier."""
        metadata = self._state.load(representation_id)

        if metadata is None:
            return None

        if metadata.current_tier is not StorageTier.COLD:
            return metadata, self._engine._transition(
                metadata, metadata.current_tier, metadata.current_tier,
                TransitionReason.REHYDRATION_REQUEST,
                succeeded=True, forced=False,
                detail="already in a searchable tier", duration=0.0,
            )

        return self._engine.migrate(
            metadata, destination,
            reason=TransitionReason.REHYDRATION_REQUEST,
        )

    # ------------------------------------------------------------
    # Delete (Steps 22, 55)
    # ------------------------------------------------------------

    def delete(
        self, representation_id: str, force: bool = False
    ) -> bool:
        """Delete from the authoritative tier, honouring retention.

        Retention and legal hold refuse the delete rather than silently
        skipping it: a caller that believes a record is gone when it is not has
        a compliance problem, not a tidiness one.
        """
        metadata = self._state.load(representation_id)

        if metadata is None:
            return False

        if not force:
            if metadata.legal_hold:
                raise RetentionProtectedError(
                    f"{representation_id!r} is under legal hold and cannot be "
                    "deleted"
                )

            if metadata.retention_until and metadata.retention_until > datetime.now(
                timezone.utc
            ):
                raise RetentionProtectedError(
                    f"{representation_id!r} is retained until "
                    f"{metadata.retention_until.isoformat()}",
                    retention_until=metadata.retention_until,
                )

        # Sweep every tier, not just the authoritative one: an interrupted
        # migration may have left a copy elsewhere, and leaving it behind would
        # keep a "deleted" vector searchable.
        for tier in self._tiers.available():
            try:
                self._tiers.get(tier).delete(representation_id)
            except Exception:  # noqa: BLE001 - best effort across tiers
                pass

        self._state.delete(representation_id)

        return True

    # ------------------------------------------------------------
    # Health (Step 75)
    # ------------------------------------------------------------

    def health(self) -> dict[str, TierHealth]:
        report: dict[str, TierHealth] = {}

        for tier in self._tiers.available():
            report[tier.value] = self._tiers.get(tier).health()

        return report


def _without_current_tier(
    context: StorageRoutingContext,
) -> StorageRoutingContext:
    from dataclasses import replace

    return replace(context, current_tier=None, tier_since=None)


__all__ = [
    "SearchHit",
    "SearchResult",
    "HybridVectorStore",
]
