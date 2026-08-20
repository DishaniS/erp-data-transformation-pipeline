"""Generic vector-store handoff, and a configurable Qdrant adapter.

PHASE 11 HANDS OFF; PHASE 12 ROUTES (Step 31)
---------------------------------------------
Nothing here decides WHERE a vector should live. There is no hot/warm/cold
scoring, no sensitivity-based routing, no tier migration and no archival - those
are Phase 12's, and a static test asserts their absence. This module's whole job
is: given an embedding, put it somewhere, under a stable identity.

CONFIGURABLE COLLECTION (Step 32)
---------------------------------
``bpi2020_erp_knowledge`` is not hard-coded anywhere in this package. The
collection is a constructor argument, because a generic engine that knew one
deployment's collection name would not be generic.

DIMENSION IS CHECKED BEFORE THE WRITE (Step 38)
-----------------------------------------------
A dimension mismatch produces a typed ``EmbeddingDimensionError`` naming the
model and the collection, rather than an opaque driver error several layers
away from the cause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from erp_pipeline.ai.errors import EmbeddingDimensionError, VectorStoreError
from erp_pipeline.ai.hashing import vector_id_for
from erp_pipeline.ai.models import EmbeddingRecord
from erp_pipeline.sync.propagation import AIRepresentation


@runtime_checkable
class VectorStore(Protocol):
    """Where embeddings are handed off to."""

    def upsert_embedding(
        self,
        record: EmbeddingRecord,
        representation: AIRepresentation | None = None,
    ) -> bool:
        ...  # pragma: no cover - protocol declaration

    def delete_embedding(self, vector_id: str) -> bool:
        ...  # pragma: no cover - protocol declaration

    def get_metadata(self, vector_id: str) -> dict[str, Any] | None:
        ...  # pragma: no cover - protocol declaration


def build_vector_payload(
    record: EmbeddingRecord,
    representation: AIRepresentation | None = None,
    include_text: bool = False,
) -> dict[str, Any]:
    """Safe structural payload for a stored vector (Step 37).

    Identity, provenance and model facts - the things needed to trace a vector
    back to its source and to decide later whether it is current. The AI text
    is NOT included by default: it would double the storage and turn the index
    into a second copy of the corpus.
    """
    payload: dict[str, Any] = {
        "representation_id": record.representation_id,
        "entity_type": record.entity_type,
        "content_hash": record.content_hash,
        "model_id": record.model_id,
        "dimension": record.dimension,
        "engine_version": record.engine_version,
    }

    if representation is not None:
        payload["source_record_ids"] = list(representation.source_record_ids)

        for key in (
            "canonical_record_id",
            "source_system_id",
            "source_type",
            "source_entity",
            "sensitivity",
            "document_id",
            "chunk_index",
            "page_start",
            "page_end",
        ):
            value = representation.metadata.get(key)
            if value is not None:
                payload[key] = value

        if include_text and representation.text_for_ai:
            payload["text_for_ai"] = representation.text_for_ai

    return payload


class InMemoryEmbeddingStore:
    """A vector store in a dictionary, keyed by the stable vector id.

    The reference semantics an adapter must match: writing the same
    representation twice replaces one entry rather than accumulating two.
    """

    def __init__(self) -> None:
        self._vectors: dict[str, dict[str, Any]] = {}
        self.upsert_calls = 0
        self.delete_calls = 0

    def upsert_embedding(
        self,
        record: EmbeddingRecord,
        representation: AIRepresentation | None = None,
    ) -> bool:
        self.upsert_calls += 1
        vector_id = vector_id_for(record.representation_id)
        existed = vector_id in self._vectors

        self._vectors[vector_id] = {
            "vector": list(record.vector or ()),
            "payload": build_vector_payload(record, representation),
        }

        return not existed

    def delete_embedding(self, vector_id: str) -> bool:
        self.delete_calls += 1
        return self._vectors.pop(vector_id, None) is not None

    def get_metadata(self, vector_id: str) -> dict[str, Any] | None:
        entry = self._vectors.get(vector_id)
        return dict(entry["payload"]) if entry else None

    def __len__(self) -> int:
        return len(self._vectors)

    @property
    def vector_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._vectors))


@dataclass
class QdrantVectorStore:
    """A generic Qdrant adapter with a configurable collection.

    ``collection_name`` is REQUIRED. Defaulting it to a deployment's collection
    would make an accidental write to production data one forgotten argument
    away.
    """

    client: Any
    collection_name: str
    dimension: int | None = None
    include_text: bool = False
    upsert_calls: int = 0
    delete_calls: int = 0

    def ensure_collection(self, dimension: int, recreate: bool = False) -> None:
        """Create the collection if it does not exist.

        ``recreate`` is off by default and never used implicitly: silently
        recreating a collection would discard every vector in it.
        """
        try:
            from qdrant_client.models import Distance, VectorParams
        except ImportError as exc:  # pragma: no cover - dependency present here
            raise VectorStoreError(
                "qdrant-client is not installed"
            ) from exc

        existing = {
            item.name for item in self.client.get_collections().collections
        }

        if self.collection_name in existing and not recreate:
            self.dimension = self.dimension or dimension
            return

        if self.collection_name in existing and recreate:
            self.client.delete_collection(self.collection_name)

        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=VectorParams(
                size=dimension, distance=Distance.COSINE
            ),
        )

        self.dimension = dimension

    def collection_dimension(self) -> int | None:
        """The dimension the collection is actually configured with."""
        try:
            info = self.client.get_collection(self.collection_name)
        except Exception:  # noqa: BLE001 - absent collection is not an error here
            return None

        params = getattr(
            getattr(getattr(info, "config", None), "params", None), "vectors", None
        )

        return getattr(params, "size", None)

    def point_count(self) -> int:
        try:
            return int(
                self.client.get_collection(self.collection_name).points_count or 0
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                f"could not read the point count of collection "
                f"{self.collection_name!r} ({type(exc).__name__})"
            ) from exc

    def upsert_embedding(
        self,
        record: EmbeddingRecord,
        representation: AIRepresentation | None = None,
    ) -> bool:
        from qdrant_client.models import PointStruct

        if record.vector is None:
            raise VectorStoreError(
                f"embedding {record.embedding_id!r} carries no vector to store"
            )

        expected = self.dimension or self.collection_dimension()

        if expected is not None and len(record.vector) != expected:
            raise EmbeddingDimensionError(
                f"refusing to write a {len(record.vector)}-dimensional vector "
                f"into collection {self.collection_name!r}, which is configured "
                f"for {expected}",
                expected=expected,
                actual=len(record.vector),
            )

        self.upsert_calls += 1
        point_id = vector_id_for(record.representation_id)

        try:
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=list(record.vector),
                        payload=build_vector_payload(
                            record, representation, self.include_text
                        ),
                    )
                ],
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                f"upsert into collection {self.collection_name!r} failed "
                f"({type(exc).__name__})"
            ) from exc

        return True

    def delete_embedding(self, vector_id: str) -> bool:
        self.delete_calls += 1

        try:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=[vector_id],
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                f"delete from collection {self.collection_name!r} failed "
                f"({type(exc).__name__})"
            ) from exc

        return True

    def get_metadata(self, vector_id: str) -> dict[str, Any] | None:
        try:
            points = self.client.retrieve(
                collection_name=self.collection_name,
                ids=[vector_id],
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                f"retrieve from collection {self.collection_name!r} failed "
                f"({type(exc).__name__})"
            ) from exc

        if not points:
            return None

        return dict(points[0].payload or {})


__all__ = [
    "VectorStore",
    "build_vector_payload",
    "InMemoryEmbeddingStore",
    "QdrantVectorStore",
]
