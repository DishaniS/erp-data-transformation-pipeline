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
from erp_pipeline.schemas.search_fields import filter_value_token
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

CARRIED_IDENTITY_KEYS: tuple[str, ...] = (
    "canonical_record_id",
    "source_system_id",
    "source_type",
    "source_entity",
    "record_key",
    "filter_attributes",
    "sensitivity",
    "document_id",
    "record_type",
    "content_kind",
    "parent_record_id",
    "source_field",
    "business_key_name",
    "business_key_value",
    "document_type",
    "page_start",
    "page_end",
    "chunk_index",
    "schema_name",
    "entity_kind",
    "schema_id",
    "schema_version",
    "entity_id",
    "schema_chunk_index",
)


def _carried_identity(
    representation: AIRepresentation, *, filter_token_secret: str | None
) -> dict[str, Any]:
    """The identity subset of a representation's metadata, absent keys omitted.

    ``filter_attributes`` gets special handling: it is the ONE carried key
    whose values are arbitrary ERP business content (a department name, a
    status, whatever the source schema declared filterable), not a system
    identifier. Every one of those values is tokenized here - the single
    chokepoint every representation passes through on its way to
    ``EmbeddingRecord.metadata`` and, downstream, the Qdrant payload -
    rather than in the three independent places that first assemble
    ``filter_attributes`` (``ai.representation``, ``orchestration.multimodal``,
    ``transformation.source_native``). One chokepoint means one place that
    can leak a raw value, and this is it: with no secret configured,
    ``filter_attributes`` is DROPPED rather than stored in the clear - a
    missing dynamic filter is a capability gap; a leaked department name in
    a vector database is a data-protection incident.
    """
    metadata = representation.metadata or {}

    carried = {
        key: metadata[key]
        for key in CARRIED_IDENTITY_KEYS
        if key != "filter_attributes" and metadata.get(key) is not None
    }

    raw_filters = metadata.get("filter_attributes")

    if isinstance(raw_filters, Mapping) and raw_filters:
        if filter_token_secret:
            source_system_id = str(metadata.get("source_system_id") or "")
            source_entity = str(metadata.get("source_entity") or "")

            def token(field_name: str, value: Any) -> str:
                return filter_value_token(
                    filter_token_secret,
                    source_system_id=source_system_id,
                    source_entity=source_entity,
                    field_name=field_name,
                    value=value,
                )

            carried["filter_attributes"] = {
                field_name: (
                    # A multi-valued field: SearchFilters.matches() checks
                    # membership item by item, so each item is tokenized on
                    # its own rather than the whole list becoming one token.
                    [token(field_name, item) for item in value]
                    if isinstance(value, Sequence) and not isinstance(value, (str, bytes))
                    else token(field_name, value)
                )
                for field_name, value in raw_filters.items()
            }
        # else: no secret configured - filter_attributes is omitted, not
        # stored in the clear. See the docstring above.

    return carried


class EmbeddingService:
    """Turns AI-ready representations into embeddings, in batches."""

    def __init__(
        self,
        model: EmbeddingModel | None = None,
        options: EmbeddingOptions | None = None,
        filter_token_secret: str | None = None,
    ) -> None:
        self._model = model or SentenceTransformerModel(DEFAULT_MODEL_ID)
        self._options = options or DEFAULT_EMBEDDING_OPTIONS
        #: Keys dynamic filter values into an HMAC token before they ever
        #: reach a vector payload. ``None`` is a valid, deliberate value -
        #: not a misconfiguration - and means dynamic filtering is simply
        #: unavailable: see ``_carried_identity``, which omits
        #: ``filter_attributes`` entirely rather than storing it in the
        #: clear when this is unset.
        self._filter_token_secret = filter_token_secret

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

    def tokenize_filter_value(
        self, *, source_system_id: str, source_entity: str, field_name: str, value: Any
    ) -> str | None:
        """The SAME token ingestion would have produced for this value, or
        ``None`` when no filter-token secret is configured.

        This is the ONLY way outside code reaches the filter-token secret -
        by asking this method to use it, never by reading it out. A caller
        (``GET /v1/search`` building a dynamic filter) gets back exactly
        what got written into the Qdrant payload for the same
        ``(source_system_id, source_entity, field_name, value)``, so an
        equality match against the payload finds the record - without this
        service, or anything else, ever handling the raw secret string.

        ``None`` is a real answer, not an error: it means dynamic filtering
        is unavailable in this deployment. The caller decides what that
        means for a request rather than this method fabricating a
        placeholder that would silently match nothing, or worse, something
        it should not.
        """
        if not self._filter_token_secret:
            return None

        return filter_value_token(
            self._filter_token_secret,
            source_system_id=source_system_id,
            source_entity=source_entity,
            field_name=field_name,
            value=value,
        )

    def fingerprint(self) -> ModelFingerprint:
        builder = getattr(self._model, "fingerprint", None)

        if callable(builder):
            return builder()

        return ModelFingerprint(
            model_id=self._model.model_id, dimension=self._model.dimension
        )


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

    def embed_one(
        self,
        representation: AIRepresentation,
        previous: EmbeddingRecord | Mapping[str, Any] | None = None,
    ) -> EmbeddingRecord:
        """Embed a single representation, or explain why it was not embedded."""
        return self._finish_batch([representation], [previous])[0]

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
            metadata={
                "engine_version": AI_ENGINE_VERSION,
                **_carried_identity(
                    representation, filter_token_secret=self._filter_token_secret
                ),
            },
        )


__all__ = [
    "EmbeddingService",
]
