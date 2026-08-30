"""The Phase 11 -> Phase 12 entry point.

    EmbeddingService  ->  EmbeddingRecord  ->  StorageService.store()  ->  tier

A caller holding a Phase 11 ``EmbeddingRecord`` should be able to store it in
one call, without constructing a router, a tier set or a routing context by
hand. Requiring that assembly would push tiering knowledge back into every
caller, which is exactly what the facade exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.cold_tier import ColdArchiveTier
from erp_pipeline.storage.hot_tier import QdrantHotTier
from erp_pipeline.storage.filters import SearchFilters
from erp_pipeline.storage.hybrid_store import HybridVectorStore, SearchResult
from erp_pipeline.storage.migration import MigrationEngine, TierSet
from erp_pipeline.storage.models import (
    BusinessCriticality,
    LatencyRequirement,
    MigrationPlan,
    MigrationResult,
    RoutingDecision,
    StorageRecordMetadata,
    StorageTier,
    TransitionReason,
)
from erp_pipeline.storage.state import InMemoryTierStateStore, TierStateStore
from erp_pipeline.storage.storage_policy import StoragePolicy
from erp_pipeline.storage.tier_monitor import TierMonitor
from erp_pipeline.storage.vector_router import StoragePolicyRouter
from erp_pipeline.storage.warm_tier import QdrantWarmTier


@dataclass(frozen=True)
class StorageProfile:
    """The routing attributes that come from ERP policy, not from the vector.

    Bundled so a caller sets them once per class of record rather than passing
    six arguments to every store call. None of it can be inferred from the
    embedding, which is exactly why it has to be supplied.
    """

    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    business_criticality: BusinessCriticality = BusinessCriticality.NORMAL
    latency_requirement: LatencyRequirement = LatencyRequirement.STANDARD
    retention_until: datetime | None = None
    legal_hold: bool = False

    @classmethod
    def from_metadata(
        cls,
        metadata: Mapping[str, Any] | None,
        base: "StorageProfile | None" = None,
    ) -> "StorageProfile":
        """Build a profile from a record's own carried metadata.

        This is how a real record's ``sensitivity`` reaches the router. The
        canonical record declares it, the representation carries it, the
        embedding carries it forward, and this turns it into the routing
        attribute the policy acts on.

        Owned by the storage layer on purpose: orchestration supplies accurate
        metadata and calls this, but never decides a tier. Anything the
        metadata does not declare falls back to ``base`` - so a caller can
        still set criticality or a legal hold per class of record while the
        sensitivity comes from the data.

        An unrecognized sensitivity value is REFUSED rather than quietly
        downgraded to the default: silently treating an unknown label as
        ``INTERNAL`` is exactly how restricted data ends up in the wrong tier.
        """
        resolved = base or DEFAULT_PROFILE
        values = dict(metadata or {})

        raw = values.get("sensitivity")

        if raw is None:
            return resolved

        sensitivity = SensitivityLevel.from_value(raw)

        return replace(resolved, sensitivity=sensitivity)


DEFAULT_PROFILE = StorageProfile()


class StorageService:
    """One object wiring Phase 11 embeddings to the hybrid tiered store."""

    def __init__(
        self,
        hot: QdrantHotTier | None = None,
        warm: QdrantWarmTier | None = None,
        cold: ColdArchiveTier | None = None,
        state_store: TierStateStore | None = None,
        policy: StoragePolicy | None = None,
    ) -> None:
        self._tiers = TierSet(hot=hot, warm=warm, cold=cold)
        # `is not None`, NOT `or`. Phase 11: `InMemoryTierStateStore` defines
        # `__len__`, so an EMPTY one is falsy - and an empty one is exactly
        # what a caller passes at startup. `or` therefore discarded the
        # caller's store and substituted a private one, silently: writes went
        # somewhere the caller could not see, and nothing survived a restart.
        # Found by the restart contract test, which is the only thing that
        # looks at the store again after the process is rebuilt.
        self._state = (
            state_store if state_store is not None else InMemoryTierStateStore()
        )
        self._router = StoragePolicyRouter(policy)
        self._store = HybridVectorStore(
            self._tiers, self._state, router=self._router
        )
        self._monitor = TierMonitor(
            self._state, self._store.migration_engine, self._router
        )

    # -- accessors --

    @property
    def store_facade(self) -> HybridVectorStore:
        return self._store

    @property
    def monitor(self) -> TierMonitor:
        return self._monitor

    @property
    def router(self) -> StoragePolicyRouter:
        return self._router

    @property
    def state(self) -> TierStateStore:
        return self._state

    @property
    def tiers(self) -> TierSet:
        return self._tiers

    # ------------------------------------------------------------
    # Phase 11 integration (Step 19)
    # ------------------------------------------------------------

    def store(
        self,
        record: EmbeddingRecord,
        profile: StorageProfile | None = None,
        override: StorageTier | None = None,
        override_reason: str | None = None,
        created_at: datetime | None = None,
    ) -> tuple[StorageRecordMetadata, RoutingDecision]:
        """Store one Phase 11 embedding, routed by policy."""
        if record.status is not EmbeddingStatus.GENERATED or record.vector is None:
            from erp_pipeline.storage.errors import StorageConfigurationError

            raise StorageConfigurationError(
                f"embedding {record.embedding_id!r} has status "
                f"{record.status.value!r} and no vector to store"
            )

        # When no profile is supplied, take the routing attributes the record
        # itself declares rather than defaulting everything to INTERNAL. A
        # caller that passes a profile still wins - it is the explicit choice.
        resolved = profile or StorageProfile.from_metadata(
            getattr(record, "metadata", None)
        )

        return self._store.store(
            record,
            sensitivity=resolved.sensitivity,
            business_criticality=resolved.business_criticality,
            latency_requirement=resolved.latency_requirement,
            retention_until=resolved.retention_until,
            legal_hold=resolved.legal_hold,
            created_at=created_at,
            override=override,
            override_reason=override_reason,
        )

    def store_many(
        self,
        records: Iterable[EmbeddingRecord],
        profile: StorageProfile | None = None,
    ) -> tuple[tuple[StorageRecordMetadata, RoutingDecision], ...]:
        return tuple(self.store(record, profile) for record in records)

    # ------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------

    def get(self, representation_id: str) -> EmbeddingRecord | None:
        return self._store.get(representation_id)

    def search(
        self,
        vector: Sequence[float],
        limit: int = 5,
        include_cold: bool = False,
        filters: SearchFilters | None = None,
    ) -> SearchResult:
        return self._store.search(
            vector, limit=limit, include_cold=include_cold, filters=filters
        )

    def fetch(self, filters: SearchFilters, limit: int = 10) -> SearchResult:
        """Exact identity/metadata retrieval - no query vector, no ranking."""
        return self._store.fetch(filters, limit=limit)

    def delete(self, representation_id: str, force: bool = False) -> bool:
        return self._store.delete(representation_id, force=force)

    # ------------------------------------------------------------
    # Tiering
    # ------------------------------------------------------------

    def route(self, representation_id: str) -> RoutingDecision | None:
        return self._store.route(representation_id)

    def migrate(
        self,
        representation_id: str,
        destination: StorageTier,
        reason: TransitionReason = TransitionReason.MANUAL_OVERRIDE,
    ) -> Any:
        return self._store.migrate(representation_id, destination, reason)

    def rehydrate(
        self,
        representation_id: str,
        destination: StorageTier = StorageTier.WARM,
    ) -> Any:
        return self._store.rehydrate(representation_id, destination)

    def plan_migrations(self, **kwargs: Any) -> MigrationPlan:
        return self._monitor.plan_migrations(**kwargs)

    def execute_migrations(self, **kwargs: Any) -> MigrationResult:
        return self._monitor.execute_migrations(**kwargs)

    def health(self) -> dict[str, Any]:
        return {
            name: health.to_dict()
            for name, health in self._store.health().items()
        }

    def distribution(self) -> dict[str, int]:
        return self._monitor.distribution()


__all__ = [
    "StorageProfile",
    "DEFAULT_PROFILE",
    "StorageService",
]
