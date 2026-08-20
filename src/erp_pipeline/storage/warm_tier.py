"""WARM tier: Qdrant with int8 scalar quantization and on-disk vectors.

WHAT MAKES IT WARM (Step 16)
----------------------------
Two real Qdrant features, not a rebranded copy of the HOT collection:

    on_disk=True                  vectors live on disk, not in RAM
    ScalarQuantization(INT8)      each float32 component is stored as int8

That is a genuine ~4x reduction in the stored vector payload, applied by the
SERVER. Nothing here rounds floats in Python and calls it quantization.

VERIFIED, NOT ASSUMED (Step 17)
-------------------------------
``live_configuration()`` reads the configuration back from the server and
``quantization_verified()`` returns True only when the server itself reports a
scalar quantizer. A collection that silently failed to apply quantization would
otherwise be indistinguishable from one that applied it, and every footprint
number downstream would be a lie.

THE TRADE-OFF IS MEASURED, NOT ASSERTED (Step 18)
-------------------------------------------------
int8 quantization is lossy. Whether it changes ranking is an empirical question
about this corpus and this model, so the benchmark measures recall against the
HOT baseline rather than assuming they match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from erp_pipeline.ai.hashing import vector_id_for
from erp_pipeline.ai.models import EmbeddingRecord
from erp_pipeline.storage.errors import (
    StorageConfigurationError,
    TierUnavailableError,
    VectorIdentityMismatchError,
)
from erp_pipeline.storage.hot_tier import FLOAT32_BYTES, _describe_quantization
from erp_pipeline.storage.models import (
    MeasurementKind,
    StorageFootprint,
    StorageTier,
    TierHealth,
)

#: Bytes per component once int8 scalar quantization is applied.
INT8_BYTES = 1


@dataclass
class QdrantWarmTier:
    """Quantized, on-disk Qdrant storage."""

    client: Any
    collection_name: str
    dimension: int
    quantile: float = 0.99
    #: Keep the quantized vectors in RAM for speed while the full vectors stay
    #: on disk. False here so WARM is genuinely lower-resource than HOT; the
    #: knob is exposed because it is the main WARM latency/footprint dial.
    always_ram: bool = False
    upsert_calls: int = 0
    delete_calls: int = 0
    search_calls: int = 0

    tier: StorageTier = field(default=StorageTier.WARM, init=False)

    def __post_init__(self) -> None:
        if not 0.5 <= self.quantile <= 1.0:
            raise StorageConfigurationError(
                f"quantile must be in [0.5, 1.0], got {self.quantile}."
            )

    # ------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------

    def ensure_collection(self, recreate: bool = False) -> None:
        from qdrant_client import models as M

        existing = {c.name for c in self.client.get_collections().collections}

        if self.collection_name in existing:
            if not recreate:
                return
            self.client.delete_collection(self.collection_name)

        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=M.VectorParams(
                size=self.dimension,
                distance=M.Distance.COSINE,
                on_disk=True,
                quantization_config=M.ScalarQuantization(
                    scalar=M.ScalarQuantizationConfig(
                        type=M.ScalarType.INT8,
                        quantile=self.quantile,
                        always_ram=self.always_ram,
                    )
                ),
            ),
        )

    def live_configuration(self) -> dict[str, Any]:
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

    def quantization_verified(self) -> bool:
        """True only when the SERVER reports a scalar quantizer.

        This is the guard behind every "WARM is quantized" claim in the
        benchmark. If Qdrant ever silently ignored the configuration, this
        returns False and the report says so instead of inventing a 4x saving.
        """
        configuration = self.live_configuration()
        quantization = configuration.get("quantization")

        return bool(quantization) and quantization.get("kind") == "scalar"

    def health(self) -> TierHealth:
        try:
            configuration = self.live_configuration()
        except Exception as exc:  # noqa: BLE001 - availability probe
            return TierHealth(
                tier=StorageTier.WARM,
                available=False,
                detail=f"{type(exc).__name__}",
            )

        return TierHealth(
            tier=StorageTier.WARM,
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
                tier=StorageTier.WARM.value,
            )

        if len(record.vector) != self.dimension:
            raise VectorIdentityMismatchError(
                f"WARM collection {self.collection_name!r} expects "
                f"{self.dimension} dimensions, got {len(record.vector)}"
            )

        point_id = vector_id_for(record.representation_id)
        self.upsert_calls += 1

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
        """Read the stored vector back.

        Note this returns the FULL-precision vector: Qdrant keeps the original
        alongside the quantized copy and uses the quantized one for search.
        A round-trip through WARM therefore does not degrade the vector - only
        retrieval ranking can differ, which is what the recall measurement is
        for.
        """
        points = self.client.retrieve(
            collection_name=self.collection_name,
            ids=[vector_id_for(representation_id)],
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
        self, vector: Sequence[float], limit: int = 5
    ) -> list[tuple[str, float]]:
        self.search_calls += 1

        results = self.client.query_points(
            collection_name=self.collection_name,
            query=list(vector),
            limit=limit,
            with_payload=False,
        ).points

        return [(str(point.id), float(point.score)) for point in results]

    def count(self) -> int:
        return int(
            self.client.get_collection(self.collection_name).points_count or 0
        )

    # ------------------------------------------------------------
    # Footprint
    # ------------------------------------------------------------

    def footprint(self) -> StorageFootprint:
        """Quantized vector payload size.

        PROXY for the same reason as HOT, and computed the same way so the two
        are comparable. The per-component size is 1 byte instead of 4 ONLY when
        the server confirms quantization; otherwise the honest number is the
        float32 one.
        """
        count = self.count()
        quantized = self.quantization_verified()
        per_component = INT8_BYTES if quantized else FLOAT32_BYTES
        per_record = self.dimension * per_component

        return StorageFootprint(
            tier=StorageTier.WARM,
            record_count=count,
            bytes_total=float(count * per_record),
            bytes_per_record=float(per_record),
            kind=MeasurementKind.PROXY,
            method=(
                "points_count x dimension x "
                f"{per_component} bytes "
                f"({'int8 quantized, server-verified' if quantized else 'float32 - quantization NOT confirmed by the server'}); "
                "the searchable payload, excluding the original vectors Qdrant "
                "also retains on disk and excluding index overhead"
            ),
            detail={
                "dimension": self.dimension,
                "bytes_per_component": per_component,
                "quantized": quantized,
                "quantile": self.quantile,
                "always_ram": self.always_ram,
                "on_disk": True,
            },
        )


__all__ = [
    "INT8_BYTES",
    "QdrantWarmTier",
]
