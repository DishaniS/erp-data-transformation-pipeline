"""The public Phase 11 entry point: representations in, embeddings out.

    service = EmbeddingService(model)

    record   = service.embed_one(representation)
    summary  = service.embed_many(representations)          # iterable, batched
    summary  = service.embed_and_store(representations, store)

WHAT DECIDES WHETHER TO EMBED (Step 24)
---------------------------------------
Three inputs, in this order::

    force               -> embed
    no previous record  -> embed
    content_hash moved  -> embed
    model changed       -> embed          (even when the content is identical)
    otherwise           -> SKIPPED_UNCHANGED

The model check matters and is easy to forget: a vector produced by a different
model is not comparable with the rest of the index, however unchanged its source
content is.

STREAMING (Step 23)
-------------------
``embed_many`` consumes an ITERABLE and materializes only one batch at a time.
A 33,000-case corpus is processed in ``batch_size`` slices, never as one list.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

from erp_pipeline.ai.embedding import (
    DEFAULT_MODEL_ID,
    EmbeddingModel,
    ModelFingerprint,
    SentenceTransformerModel,
)
from erp_pipeline.ai.errors import (
    AIError,
    EmbeddingDimensionError,
    EmbeddingError,
)
from erp_pipeline.ai.models import (
    DEFAULT_EMBEDDING_OPTIONS,
    AI_ENGINE_VERSION,
    EmbeddingFailurePolicy,
    EmbeddingOptions,
    EmbeddingRecord,
    EmbeddingRunSummary,
    EmbeddingStatus,
    make_embedding_id,
)
from erp_pipeline.sync.propagation import AIRepresentation


def _batched(
    items: Iterable[AIRepresentation], size: int
) -> Iterator[list[AIRepresentation]]:
    """Yield fixed-size batches without materializing the whole input."""
    batch: list[AIRepresentation] = []

    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []

    if batch:
        yield batch


class EmbeddingService:
    """Turns AI-ready representations into embeddings, in batches."""

    def __init__(
        self,
        model: EmbeddingModel | None = None,
        options: EmbeddingOptions | None = None,
    ) -> None:
        self._model = model or SentenceTransformerModel(DEFAULT_MODEL_ID)
        self._options = options or DEFAULT_EMBEDDING_OPTIONS

    @property
    def model(self) -> EmbeddingModel:
        return self._model

    @property
    def options(self) -> EmbeddingOptions:
        return self._options

    @property
    def model_id(self) -> str:
        return self._model.model_id

    @property
    def dimension(self) -> int:
        return self._model.dimension

    def fingerprint(self) -> ModelFingerprint:
        builder = getattr(self._model, "fingerprint", None)

        if callable(builder):
            return builder()

        return ModelFingerprint(
            model_id=self._model.model_id, dimension=self._model.dimension
        )

    # ------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------

    def is_current(
        self,
        representation: AIRepresentation,
        previous: EmbeddingRecord | Mapping[str, Any] | None,
    ) -> bool:
        """Whether an existing embedding still represents this content.

        Accepts either a record or a plain mapping, so a caller holding rows
        from its own store does not have to reconstruct model objects.
        """
        if previous is None or self._options.force:
            return False

        if isinstance(previous, EmbeddingRecord):
            previous_hash = previous.content_hash
            previous_model = previous.model_id
        else:
            previous_hash = previous.get("content_hash")
            previous_model = previous.get("model_id")

        if previous_hash != representation.resolved_hash():
            return False

        # A different model means a different vector space (Step 50).
        if previous_model != self._model.model_id:
            return False

        return True

    # ------------------------------------------------------------
    # One representation
    # ------------------------------------------------------------

    def embed_one(
        self,
        representation: AIRepresentation,
        previous: EmbeddingRecord | Mapping[str, Any] | None = None,
    ) -> EmbeddingRecord:
        """Embed a single representation, or explain why it was not embedded."""
        return self._finish_batch([representation], [previous])[0]

    # ------------------------------------------------------------
    # Many representations
    # ------------------------------------------------------------

    def embed_many(
        self,
        representations: Iterable[AIRepresentation],
        previous: Mapping[str, Any] | None = None,
        store: Any = None,
    ) -> EmbeddingRunSummary:
        """Embed an iterable, batching through the model's own encoder."""
        started = time.monotonic()
        lookup = previous or {}

        records: list[EmbeddingRecord] = []
        vectors_upserted = 0
        vectors_failed = 0

        for batch in _batched(representations, self._options.batch_size):
            existing = [lookup.get(item.representation_id) for item in batch]
            produced = self._finish_batch(batch, existing)
            records.extend(produced)

            if store is None:
                continue

            for record, representation in zip(produced, batch):
                if not record.status.produced_vector:
                    continue
                try:
                    store.upsert_embedding(record, representation)
                    vectors_upserted += 1
                except Exception:  # noqa: BLE001 - counted, never swallowed silently
                    vectors_failed += 1
                    if self._options.failure_policy is (
                        EmbeddingFailurePolicy.FAIL_FAST
                    ):
                        raise

        duration = round(time.monotonic() - started, 6)

        return EmbeddingRunSummary(
            model_id=self._model.model_id,
            dimension=self._model.dimension,
            batch_size=self._options.batch_size,
            representations_read=len(records),
            embeddings_generated=sum(
                1 for r in records if r.status is EmbeddingStatus.GENERATED
            ),
            embeddings_skipped=sum(
                1 for r in records if r.status is EmbeddingStatus.SKIPPED_UNCHANGED
            ),
            embeddings_failed=sum(
                1 for r in records if r.status is EmbeddingStatus.FAILED
            ),
            embeddings_empty=sum(
                1 for r in records if r.status is EmbeddingStatus.EMPTY_CONTENT
            ),
            vectors_upserted=vectors_upserted,
            vectors_failed=vectors_failed,
            duration_seconds=duration,
            records=tuple(records),
        )

    def embed_and_store(
        self,
        representations: Iterable[AIRepresentation],
        store: Any,
        previous: Mapping[str, Any] | None = None,
    ) -> EmbeddingRunSummary:
        """Embed and hand every produced vector to a store."""
        return self.embed_many(representations, previous=previous, store=store)

    # ------------------------------------------------------------
    # Batch mechanics
    # ------------------------------------------------------------

    def _finish_batch(
        self,
        batch: Sequence[AIRepresentation],
        previous: Sequence[Any],
    ) -> list[EmbeddingRecord]:
        """Classify a batch, encode only what needs it, and assemble records."""
        results: list[EmbeddingRecord | None] = [None] * len(batch)
        to_encode: list[int] = []

        for index, representation in enumerate(batch):
            text = (representation.text_for_ai or "").strip()

            if len(text) < max(1, self._options.min_content_characters):
                # An embedding of nothing is a valid vector pointing nowhere;
                # storing one would quietly pollute retrieval (Step 27).
                results[index] = self._record(
                    representation,
                    EmbeddingStatus.EMPTY_CONTENT,
                    reason="the representation has no AI-ready text to embed",
                )
                continue

            if self.is_current(representation, previous[index]):
                results[index] = self._record(
                    representation,
                    EmbeddingStatus.SKIPPED_UNCHANGED,
                    reason="content hash and model are unchanged",
                )
                continue

            to_encode.append(index)

        if to_encode:
            texts = [
                (batch[index].text_for_ai or "").strip() for index in to_encode
            ]

            try:
                vectors = self._model.encode(
                    texts, batch_size=self._options.batch_size
                )
            except Exception as exc:  # noqa: BLE001
                if self._options.failure_policy is EmbeddingFailurePolicy.FAIL_FAST:
                    raise EmbeddingError(
                        f"embedding a batch of {len(texts)} representation(s) "
                        f"failed ({type(exc).__name__})"
                    ) from exc

                for index in to_encode:
                    results[index] = self._record(
                        batch[index],
                        EmbeddingStatus.FAILED,
                        reason=f"model encode failed ({type(exc).__name__})",
                    )
                vectors = []

            for offset, index in enumerate(to_encode):
                if offset >= len(vectors):
                    break

                vector = vectors[offset]

                if len(vector) != self._model.dimension:
                    message = (
                        f"expected {self._model.dimension} dimensions, got "
                        f"{len(vector)}"
                    )
                    if self._options.failure_policy is (
                        EmbeddingFailurePolicy.FAIL_FAST
                    ):
                        raise EmbeddingDimensionError(
                            message,
                            expected=self._model.dimension,
                            actual=len(vector),
                        )
                    results[index] = self._record(
                        batch[index], EmbeddingStatus.FAILED, reason=message
                    )
                    continue

                results[index] = self._record(
                    batch[index], EmbeddingStatus.GENERATED, vector=vector
                )

        return [
            item
            if item is not None
            else self._record(
                batch[position],
                EmbeddingStatus.FAILED,
                reason="the model returned no vector for this representation",
            )
            for position, item in enumerate(results)
        ]

    def _record(
        self,
        representation: AIRepresentation,
        status: EmbeddingStatus,
        vector: Sequence[float] | None = None,
        reason: str | None = None,
    ) -> EmbeddingRecord:
        return EmbeddingRecord(
            embedding_id=make_embedding_id(
                representation.representation_id, self._model.model_id
            ),
            representation_id=representation.representation_id,
            entity_type=representation.entity_type,
            content_hash=representation.resolved_hash(),
            model_id=self._model.model_id,
            dimension=self._model.dimension,
            status=status,
            vector=tuple(float(value) for value in vector) if vector else None,
            reason=reason,
            metadata={"engine_version": AI_ENGINE_VERSION},
        )


__all__ = [
    "EmbeddingService",
]
