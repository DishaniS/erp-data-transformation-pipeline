"""Adapters letting Phase 10 drive the Phase 11 engine (Step 58).

Phase 10's incremental cascade talks to two narrow protocols::

    EmbeddingUpdater.embed(representation) -> EmbeddingResult
    VectorRecordStore.upsert(representation, embedding) / .delete(vector_id)

These adapters satisfy both using the generic Phase 11 engine, so the cascade
gets a real model and a real vector store WITHOUT importing
``sentence_transformers`` or ``qdrant_client`` anywhere - which is exactly the
independence Phase 10 was built for.

Nothing in Phase 10 changes. The adapters are additive: an existing deployment
keeps whatever updater it already has, and can move to this one when it wants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from erp_pipeline.ai.errors import EmbeddingError
from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.sync.propagation import AIRepresentation, EmbeddingResult


@dataclass
class Phase11EmbeddingUpdater:
    """Satisfies Phase 10's ``EmbeddingUpdater`` using the Phase 11 service.

    Phase 10 has already decided the representation needs embedding by the time
    it calls this - it compares hashes itself - so this adapter forces the
    embedding rather than second-guessing that decision with its own skip check.
    Two independent skip policies would eventually disagree.
    """

    service: EmbeddingService
    calls: int = 0
    embedded_ids: list[str] = field(default_factory=list)

    def embed(self, representation: AIRepresentation) -> EmbeddingResult:
        self.calls += 1
        self.embedded_ids.append(representation.representation_id)

        record = self.service.embed_one(representation, previous=None)

        if record.status is not EmbeddingStatus.GENERATED:
            raise EmbeddingError(
                f"embedding produced no vector ({record.status.value}): "
                f"{record.reason}",
                representation_id=representation.representation_id,
            )

        return EmbeddingResult(
            representation_id=record.representation_id,
            content_hash=record.content_hash,
            vector=record.vector,
            model_id=record.model_id,
            dimensions=record.dimension,
        )


@dataclass
class Phase11VectorRecordStore:
    """Satisfies Phase 10's ``VectorRecordStore`` using a Phase 11 store."""

    store: Any
    upsert_calls: int = 0
    delete_calls: int = 0

    def upsert(
        self, representation: AIRepresentation, embedding: EmbeddingResult
    ) -> bool:
        self.upsert_calls += 1

        record = EmbeddingRecord(
            embedding_id=f"phase10.{representation.representation_id}",
            representation_id=representation.representation_id,
            entity_type=representation.entity_type,
            content_hash=embedding.content_hash,
            model_id=embedding.model_id or "unknown",
            dimension=embedding.dimensions or len(embedding.vector or ()),
            status=EmbeddingStatus.GENERATED,
            vector=embedding.vector,
        )

        return bool(self.store.upsert_embedding(record, representation))

    def delete(self, vector_id: str) -> bool:
        self.delete_calls += 1
        return bool(self.store.delete_embedding(vector_id))


__all__ = [
    "Phase11EmbeddingUpdater",
    "Phase11VectorRecordStore",
]
