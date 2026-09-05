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

import inspect
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
from erp_pipeline.storage.filters import NO_FILTERS, SearchFilters
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
    """One result, with the tier it came from and the record it resolves to."""

    representation_id: str | None
    vector_id: str
    score: float
    tier: StorageTier
    #: The canonical record this hit resolves to, carried forward from storage
    #: state. ``None`` means the state row genuinely has no canonical
    #: reference - a vector stored before the field existed, or one derived
    #: from no canonical record. Never guessed from the representation id.
    canonical_record_id: str | None = None
    entity_type: str | None = None
    #: The authoritative state row for this vector, attached during merging.
    #:
    #: ``_merge`` has already batch-loaded it to re-check filters, so handing
    #: it to the caller costs nothing and saves the API a second lookup PER
    #: HIT. Deliberately excluded from ``to_dict()``: it is an internal
    #: reference, not part of the wire shape.
    #:
    #: ``None`` means no state row was found - the hit is reported with what
    #: the tier itself knew rather than being dropped.
    state: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "representation_id": self.representation_id,
            "canonical_record_id": self.canonical_record_id,
            "entity_type": self.entity_type,
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


def _tier_search(
    backend: Any,
    vector: Sequence[float],
    limit: int,
    query_filter: Any | None,
) -> list[tuple[str, float]]:
    """Search one tier, passing a server-side filter when it accepts one.

    Tolerant by design: an in-memory or third-party tier implementation that
    predates filtering still works unfiltered rather than raising a
    ``TypeError``. When a filter IS requested and the backend cannot apply it,
    that is not silently ignored - the caller's filter is re-checked against
    tier state during the merge, so a non-matching hit is dropped there.

    Support is decided by INSPECTING the signature rather than by catching
    ``TypeError`` from the call. Catching would also swallow a genuine
    ``TypeError`` raised inside the backend's own body and then silently retry
    it unfiltered, turning a real bug into a quiet behaviour change.
    """
    if query_filter is None:
        return list(backend.search(vector, limit=limit))

    if _accepts_query_filter(backend.search):
        return list(backend.search(vector, limit=limit, query_filter=query_filter))

    return list(backend.search(vector, limit=limit))


def _tier_fetch(
    backend: Any, query_filter: Any, limit: int
) -> list[tuple[str, Mapping[str, Any]]]:
    """Filter-only fetch on one tier, or empty when the backend cannot.

    Tolerant by design, matching ``_tier_search``: a tier implementation that
    predates identity-only retrieval (every existing in-memory test double)
    contributes nothing here rather than raising ``AttributeError``.
    """
    fetch = getattr(backend, "fetch", None)

    if fetch is None:
        return []

    return list(fetch(query_filter, limit=limit))


def _tier_is_empty(backend: Any) -> bool:
    """Whether a tier provably holds nothing, when that can be asked at all.

    Tolerant by design: a backend with no ``count()`` (an exotic third-party
    tier, or a test double that predates it) is treated as non-empty rather
    than raising - the caller still queries it and takes whatever answer it
    gives, exactly as before this check existed. A backend whose ``count()``
    itself fails is treated the same way, so a transient error here cannot
    turn into a silently skipped tier.
    """
    count = getattr(backend, "count", None)

    if count is None:
        return False

    try:
        return int(count()) == 0
    except Exception:  # noqa: BLE001 - err toward querying, not skipping
        return False


def _accepts_query_filter(search: Any) -> bool:
    """Whether a tier's ``search`` declares a ``query_filter`` parameter."""
    try:
        parameters = inspect.signature(search).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False

    if "query_filter" in parameters:
        return True

    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


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
        canonical_record_id: str | None = None,
        source_system_id: str | None = None,
        source_entity: str | None = None,
        document_id: str | None = None,
    ) -> tuple[StorageRecordMetadata, RoutingDecision]:
        """Route, write, verify, then persist state - in that order.

        State is written LAST and only after the tier write is verified. A
        state row claiming a vector is somewhere it is not would be worse than
        no row at all: every later read and migration would trust it.

        The identity arguments are carried FORWARD, never reconstructed. An
        explicit argument wins; otherwise the value is read from the embedding
        record's own metadata, which the AI layer populated from the
        representation. Neither path parses a representation id.
        """
        existing = self._state.load(record.representation_id)
        now = datetime.now(timezone.utc)

        carried = dict(getattr(record, "metadata", None) or {})

        def identity(explicit: str | None, key: str) -> str | None:
            if explicit is not None:
                return explicit

            value = carried.get(key)

            if value is not None:
                return str(value)

            # Preserve what a previous write already established rather than
            # blanking it because this particular call did not carry it.
            return getattr(existing, key, None) if existing else None

        def ordinal(key: str) -> int | None:
            """The integer form of a provenance value.

            Separate from ``identity`` because page and chunk numbers are
            integers everywhere they are stored, and stringifying them here
            would put ``"0"`` in a column typed INTEGER and a payload field a
            reader expects to compare numerically. A value that will not
            convert is dropped rather than guessed at.
            """
            value = carried.get(key)

            if value is None:
                return getattr(existing, key, None) if existing else None

            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        dynamic = carried.get("filter_attributes")
        filter_attributes = (
            dict(dynamic)
            if isinstance(dynamic, Mapping)
            else dict(getattr(existing, "filter_attributes", {}) or {})
        )

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
            canonical_record_id=identity(
                canonical_record_id, "canonical_record_id"
            ),
            source_system_id=identity(source_system_id, "source_system_id"),
            source_entity=identity(source_entity, "source_entity"),
            record_key=identity(None, "record_key"),
            document_id=identity(document_id, "document_id"),
            # Phase 4 identity and provenance, carried on exactly the same
            # terms: explicit argument wins, then the embedding record's own
            # metadata, then whatever a previous write established.
            content_kind=identity(None, "content_kind"),
            parent_record_id=identity(None, "parent_record_id"),
            source_field=identity(None, "source_field"),
            business_key_name=identity(None, "business_key_name"),
            business_key_value=identity(None, "business_key_value"),
            filter_attributes=filter_attributes,
            document_type=identity(None, "document_type"),
            schema_name=identity(None, "schema_name"),
            entity_kind=identity(None, "entity_kind"),
            schema_id=identity(None, "schema_id"),
            schema_version=identity(None, "schema_version"),
            entity_id=identity(None, "entity_id"),
            schema_chunk_index=ordinal("schema_chunk_index"),
            page_start=ordinal("page_start"),
            page_end=ordinal("page_end"),
            chunk_index=ordinal("chunk_index"),
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
        filters: SearchFilters | None = None,
    ) -> SearchResult:
        """HOT + WARM by default; COLD only via rehydration.

        ``filters`` constrain every tier with the SAME semantics: pushed into
        Qdrant for HOT and WARM, applied to tier state before rehydration for
        COLD. A query must not mean one thing online and another in the
        archive.
        """
        resolved = filters or NO_FILTERS
        query_filter = resolved.to_qdrant_filter()

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

            if query_filter is not None and _tier_is_empty(backend):
                # An empty tier cannot match a filtered query, and Qdrant
                # Cloud requires a payload index to even ATTEMPT one - a tier
                # that has never received a write for the filtered field
                # will not have it, and would 400 rather than return no
                # results. Skipping is both correct (nothing here matches)
                # and what avoids that error.
                continue

            searched.append(tier)

            for vector_id, score in _tier_search(backend, vector, limit, query_filter):
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
                self._search_cold(vector, limit, resolved)
            )
            raw.extend(cold_hits)
            query_seconds += cold_query
            searched.append(StorageTier.COLD)

        hits = self._merge(raw, limit, resolved)

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

    def fetch(self, filters: SearchFilters, limit: int = 10) -> SearchResult:
        """Exact identity/metadata retrieval - no query vector, no ranking.

        For a caller that supplies exact filters but no query text: the
        filter alone determines membership, through the SAME server-side
        Qdrant filter ``search()`` uses (``SearchFilters.to_qdrant_filter``),
        via ``scroll`` rather than an ANN query. Qdrant's payload index
        answers this directly - it is never a full collection scan.

        HOT and WARM only. COLD has no index to scroll without rehydrating
        first, which a plain identity lookup should not have to pay for; a
        caller that needs an archived record by exact key still has
        ``search(..., include_cold=True)`` with a query available to it.

        Refuses an empty filter: fetching "the first N points of the
        collection" with no constraint at all is not identity retrieval, it
        is an unscoped dump - exactly what this method exists to avoid.
        """
        if filters.is_empty:
            raise StorageConfigurationError(
                "fetch() requires at least one filter; an unfiltered fetch "
                "would return an arbitrary slice of the collection rather "
                "than an identified record"
            )

        query_filter = filters.to_qdrant_filter()
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

            if _tier_is_empty(backend):
                # fetch() is always filtered (empty filters are refused
                # above), so the same reasoning as search() applies: an
                # empty tier cannot match, and may not even have an index
                # for the filtered field yet.
                continue

            searched.append(tier)

            for vector_id, _payload in _tier_fetch(backend, query_filter, limit):
                raw.append(
                    SearchHit(
                        representation_id=None,
                        vector_id=vector_id,
                        # No query vector means nothing was scored against it.
                        # 1.0 reads as "exact match", which is what a filter
                        # hit is - never a fabricated similarity.
                        score=1.0,
                        tier=tier,
                    )
                )

        hits = self._merge(raw, limit, filters)

        return SearchResult(
            hits=hits,
            tiers_searched=tuple(searched),
            query_seconds=round(time.monotonic() - started, 6),
        )

    def _merge(
        self,
        raw: Sequence[SearchHit],
        limit: int,
        filters: "SearchFilters | None" = None,
    ) -> tuple[SearchHit, ...]:
        """Deduplicate by logical vector id, preferring the authoritative tier.

        An interrupted migration can leave one vector in two tiers. Returning
        it twice would corrupt any ranking built on these results, so the copy
        from the tier the STATE says is authoritative wins; failing that, the
        higher score does.

        This is also where each hit is enriched with the canonical record it
        resolves to, taken from tier state. The vector payload carries the
        same value, but state is the authority, and reading it here means a
        vector stored before the field existed reports ``None`` rather than a
        fabricated id.

        ``filters`` are re-checked against state as a backstop. The tiers
        already filtered server-side; this catches a vector whose payload and
        state disagree, which would otherwise leak a non-matching hit.
        """
        resolved_filters = filters or NO_FILTERS
        by_vector: dict[str, SearchHit] = {}
        state = self._state_by_vector({hit.vector_id for hit in raw})

        for hit in raw:
            metadata = state.get(hit.vector_id)

            if metadata is not None and not resolved_filters.matches(metadata):
                continue

            # Phase 9: PostgreSQL is authoritative about which version is
            # current. A superseded vector still physically present - because
            # its delete failed, or has not run yet - must never be returned as
            # though it were the answer.
            if metadata is not None and getattr(metadata, "is_current", True) is False:
                continue

            enriched = SearchHit(
                representation_id=(
                    metadata.representation_id
                    if metadata
                    else hit.representation_id
                ),
                vector_id=hit.vector_id,
                score=hit.score,
                tier=hit.tier,
                canonical_record_id=(
                    metadata.canonical_record_id if metadata else None
                ),
                entity_type=metadata.entity_type if metadata else None,
                state=metadata,
            )

            incumbent = by_vector.get(hit.vector_id)

            if incumbent is None:
                by_vector[hit.vector_id] = enriched
                continue

            official = metadata.current_tier if metadata else None

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

    def _state_by_vector(
        self, vector_ids: set[str]
    ) -> dict[str, StorageRecordMetadata]:
        """Map vector id -> its authoritative tier-state row.

        Replaces the narrower ``_authoritative_tiers``: hits now need the
        canonical reference and the entity type as well as the official tier,
        and one lookup should serve all of them.
        """
        return {
            metadata.vector_id: metadata
            for metadata in self._state.list_all()
            if metadata.vector_id in vector_ids
        }

    def _search_cold(
        self,
        vector: Sequence[float],
        limit: int,
        filters: "SearchFilters | None" = None,
    ) -> tuple[list[SearchHit], float, float, int]:
        """Rehydrate cold vectors into a temporary collection, then query it.

        This is the honest implementation of "cold search": encrypted archives
        have no index, so the only way to search them is to restore them first.
        The rehydration cost is returned separately so a benchmark can report
        it rather than bury it inside a query time.

        Filters are applied to tier STATE before anything is rehydrated. That
        keeps archive semantics identical to the online tiers, and it means a
        filtered-out archive is never decrypted at all - the cheapest possible
        way to honour a filter here.
        """
        cold = self._tiers.cold
        hot = self._tiers.hot

        if cold is None or hot is None:
            return [], 0.0, 0.0, 0

        resolved = filters or NO_FILTERS
        cold_records = [
            metadata
            for metadata in self._state.list_all(StorageTier.COLD)
            if resolved.matches(metadata)
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

            by_representation = {
                metadata.representation_id: metadata
                for metadata in cold_records
            }

            hits = []

            for point in results:
                representation_id = (point.payload or {}).get(
                    "representation_id"
                )
                metadata = by_representation.get(representation_id)

                hits.append(
                    SearchHit(
                        representation_id=representation_id,
                        vector_id=str(point.id),
                        score=float(point.score),
                        tier=StorageTier.COLD,
                        canonical_record_id=(
                            metadata.canonical_record_id if metadata else None
                        ),
                        entity_type=metadata.entity_type if metadata else None,
                    )
                )

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
