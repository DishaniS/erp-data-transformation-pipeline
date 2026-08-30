"""HOT tier: full-precision vectors in Qdrant, optimized for latency.

WHAT MAKES IT HOT
-----------------
Vectors stay in RAM at full float32 precision, with no quantization and no
on-disk offload. That is the whole configuration - and it is what the WARM tier
is measured against, so it must not drift.

Phase 11's ``vector_id_for`` supplies the point id, so a vector keeps the same
logical identity here as it has in every other tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from erp_pipeline.ai.hashing import vector_id_for
from erp_pipeline.ai.models import EmbeddingRecord
from erp_pipeline.storage.errors import (
    TierUnavailableError,
    VectorIdentityMismatchError,
)
from erp_pipeline.storage.payload_indexes import ensure_payload_indexes
from erp_pipeline.schemas.search_fields import RESERVED_PAYLOAD_FIELDS
from erp_pipeline.storage.models import (
    MeasurementKind,
    StorageFootprint,
    StorageTier,
    TierHealth,
)

#: Bytes per float32 component, used for the footprint proxy.
FLOAT32_BYTES = 4


@dataclass
class QdrantHotTier:
    """Full-precision Qdrant storage."""

    client: Any
    collection_name: str
    dimension: int
    upsert_calls: int = 0
    delete_calls: int = 0
    search_calls: int = 0

    tier: StorageTier = field(default=StorageTier.HOT, init=False)

    # ------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------

    def ensure_collection(self, recreate: bool = False) -> None:
        """Create the HOT collection with NO quantization and NO on-disk."""
        from qdrant_client import models as M

        existing = {c.name for c in self.client.get_collections().collections}

        if self.collection_name in existing:
            if not recreate:
                # The collection is already here, but it may predate payload
                # indexing - which is the state every already-deployed cluster
                # is in. Ensure them and return; nothing is recreated.
                ensure_payload_indexes(self.client, self.collection_name)

                return
            self.client.delete_collection(self.collection_name)

        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=M.VectorParams(
                size=self.dimension,
                distance=M.Distance.COSINE,
                # Explicit, not defaulted: this is the control condition for
                # the WARM comparison, so both switches are stated.
                on_disk=False,
            ),
        )

        # Filtered search on managed Qdrant REQUIRES payload indexes; without
        # them every filtered query is a 400. Idempotent, and never recreates
        # the collection just to add one.
        ensure_payload_indexes(self.client, self.collection_name)

    def live_configuration(self) -> dict[str, Any]:
        """The configuration the SERVER reports, not what we asked for."""
        info = self.client.get_collection(self.collection_name)
        vectors = info.config.params.vectors

        return {
            "collection": self.collection_name,
            "size": getattr(vectors, "size", None),
            "distance": str(getattr(vectors, "distance", None)),
            "on_disk": getattr(vectors, "on_disk", None),
            "quantization": _describe_quantization(info),
            "points_count": info.points_count,
        }

    def health(self) -> TierHealth:
        try:
            configuration = self.live_configuration()
        except Exception as exc:  # noqa: BLE001 - availability probe
            return TierHealth(
                tier=StorageTier.HOT,
                available=False,
                detail=f"{type(exc).__name__}",
            )

        return TierHealth(
            tier=StorageTier.HOT,
            available=True,
            record_count=configuration.get("points_count"),
            configuration=configuration,
        )

    # ------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------

    def upsert(
        self, record: EmbeddingRecord, payload: Mapping[str, Any] | None = None
    ) -> str:
        from qdrant_client import models as M

        if record.vector is None:
            raise TierUnavailableError(
                f"embedding {record.embedding_id!r} carries no vector",
                tier=StorageTier.HOT.value,
            )

        if len(record.vector) != self.dimension:
            raise VectorIdentityMismatchError(
                f"HOT collection {self.collection_name!r} expects "
                f"{self.dimension} dimensions, got {len(record.vector)}"
            )

        point_id = vector_id_for(record.representation_id)
        self.upsert_calls += 1
        dynamic_fields = set(payload or {}) - RESERVED_PAYLOAD_FIELDS
        known = set(getattr(self, "_dynamic_payload_indexes", set()))
        missing = tuple(sorted(dynamic_fields - known))
        if missing:
            report = ensure_payload_indexes(
                self.client, self.collection_name, field_names=missing
            )
            known.update(report["created"])
            known.update(report["already_present"])
            self._dynamic_payload_indexes = known

        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                M.PointStruct(
                    id=point_id,
                    vector=list(record.vector),
                    payload=dict(payload or {}),
                )
            ],
            wait=True,
        )

        return point_id

    def get_vector(self, representation_id: str) -> tuple[float, ...] | None:
        point_id = vector_id_for(representation_id)

        points = self.client.retrieve(
            collection_name=self.collection_name,
            ids=[point_id],
            with_vectors=True,
        )

        if not points:
            return None

        return tuple(float(value) for value in (points[0].vector or ()))

    def exists(self, representation_id: str) -> bool:
        return bool(
            self.client.retrieve(
                collection_name=self.collection_name,
                ids=[vector_id_for(representation_id)],
            )
        )

    def delete(self, representation_id: str) -> bool:
        self.delete_calls += 1

        self.client.delete(
            collection_name=self.collection_name,
            points_selector=[vector_id_for(representation_id)],
            wait=True,
        )

        return True

    def search(
        self,
        vector: Sequence[float],
        limit: int = 5,
        query_filter: Any | None = None,
    ) -> list[tuple[str, float]]:
        """Return ``(point_id, score)`` pairs, highest score first.

        ``query_filter`` is a Qdrant ``Filter`` applied SERVER-SIDE, so the
        constraint narrows the ANN search itself rather than discarding
        results afterwards. Over-fetching and post-filtering would silently
        return fewer than ``limit`` matches whenever the filter is selective.
        """
        self.search_calls += 1

        results = self.client.query_points(
            collection_name=self.collection_name,
            query=list(vector),
            limit=limit,
            with_payload=False,
            query_filter=query_filter,
        ).points

        return [(str(point.id), float(point.score)) for point in results]

    def fetch(
        self,
        query_filter: Any,
        limit: int = 100,
    ) -> list[tuple[str, Mapping[str, Any]]]:
        """Return ``(point_id, payload)`` pairs matching a filter - no vector.

        Uses Qdrant's ``scroll``, which is satisfied by the payload index on
        the filtered fields. This is identity/metadata retrieval, not
        similarity search: there is no query vector, so no ANN graph is
        walked and no relevance score is computed - the filter alone decides
        membership, exactly as it narrows ``search()``'s candidate set, just
        without a ranking on top of it.
        """
        points, _ = self.client.scroll(
            collection_name=self.collection_name,
            scroll_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )

        return [(str(point.id), dict(point.payload or {})) for point in points]

    def count(self) -> int:
        return int(
            self.client.get_collection(self.collection_name).points_count or 0
        )

    # ------------------------------------------------------------
    # Footprint (Step 59)
    # ------------------------------------------------------------

    def footprint(self) -> StorageFootprint:
        """Vector payload size.

        Marked PROXY, not MEASURED, and deliberately so: the Qdrant client
        exposes point counts and configuration, not the collection's physical
        bytes on disk. ``count x dimension x 4`` is the exact size of the raw
        float32 vector payload, which is the quantity the WARM comparison is
        actually about - but it excludes index structures and segment overhead,
        so calling it "physical storage" would be false.
        """
        count = self.count()
        per_record = self.dimension * FLOAT32_BYTES

        return StorageFootprint(
            tier=StorageTier.HOT,
            record_count=count,
            bytes_total=float(count * per_record),
            bytes_per_record=float(per_record),
            kind=MeasurementKind.PROXY,
            method=(
                "points_count x dimension x 4 bytes (float32 vector payload); "
                "excludes HNSW index and segment overhead, which the client "
                "does not expose"
            ),
            detail={
                "dimension": self.dimension,
                "bytes_per_component": FLOAT32_BYTES,
                "quantized": False,
                "on_disk": False,
            },
        )


def _describe_quantization(info: Any) -> dict[str, Any] | None:
    """Read quantization from wherever this server version reports it.

    Qdrant accepts quantization either inside ``VectorParams`` or at collection
    level, and reports it back in the corresponding place. Checking one only
    would make a genuinely quantized collection look unquantized - which is the
    exact false claim Step 17 warns against.
    """
    vectors = getattr(getattr(info.config, "params", None), "vectors", None)
    candidate = getattr(vectors, "quantization_config", None)

    if candidate is None:
        candidate = getattr(info.config, "quantization_config", None)

    if candidate is None:
        return None

    scalar = getattr(candidate, "scalar", None)

    if scalar is not None:
        return {
            "kind": "scalar",
            "type": str(getattr(scalar, "type", None)),
            "quantile": getattr(scalar, "quantile", None),
            "always_ram": getattr(scalar, "always_ram", None),
        }

    return {"kind": type(candidate).__name__}


__all__ = [
    "FLOAT32_BYTES",
    "QdrantHotTier",
]
