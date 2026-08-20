"""Supplemental AI models: chunks, embedding records, options, run summary.

WHAT IS REUSED, NOT REDEFINED
-----------------------------
``AIRepresentation`` already exists, in ``erp_pipeline.sync.propagation``. It
carries exactly what Step 3 asks for - representation id, entity type, content,
content hash, source record ids, metadata - and Phase 10's skip-if-unchanged
logic is built on it. Defining a second, near-identical "AIReadyRepresentation"
here would fork the content-hash convention that decides whether an embedding is
regenerated, which is the one thing Phase 11 must not do (Step 9).

So Phase 11 reuses it and adds only what genuinely does not exist yet:

``DocumentChunk``      a bounded, page-traceable slice of an extracted document
``EmbeddingRecord``    the outcome of embedding one representation
``EmbeddingStatus``    generated / skipped / failed / empty
``EmbeddingOptions``   model, batching, policy
``EmbeddingRunSummary``counters over a batch

PRIVACY
-------
``EmbeddingRecord`` holds a vector, and ``DocumentChunk`` holds document text.
Neither appears in ``to_dict()`` or ``__repr__`` by default: a run summary or a
log line must never become a copy of the corpus or of the index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.schemas.identity import hash_json_payload, normalize_identifier

#: Version of the representation-building and embedding behaviour. Recorded on
#: every embedding record so a vector produced under different rules is
#: traceable.
AI_ENGINE_VERSION = "1.0"

#: Prefix for AI-ready representation identities, keeping them distinct from
#: canonical ``erp:`` ids and from Phase 0's ``case:`` / ``event:`` ids.
AI_ID_PREFIX = "ai"


# ============================================================
# Document chunks (Steps 11-14)
# ============================================================

@dataclass(frozen=True)
class DocumentChunk:
    """One bounded slice of an extracted document.

    ``page_start`` / ``page_end`` preserve the Phase 6 page provenance
    (Step 13). A retrieval answer that cannot say which page it came from is
    much less useful, and the information is unrecoverable once the pages are
    merged away.

    ``text`` is content and is excluded from ``to_dict()`` by default, on the
    same terms Phase 6 applies to page text.
    """

    document_id: str
    chunk_id: str
    chunk_index: int
    text: str
    page_start: int
    page_end: int
    char_count: int = 0
    content_hash: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def spans_pages(self) -> bool:
        return self.page_end > self.page_start

    def to_dict(self, include_text: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "char_count": self.char_count,
            "content_hash": self.content_hash,
        }

        if include_text:
            payload["text"] = self.text

        return payload

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"DocumentChunk(chunk_id={self.chunk_id!r}, "
            f"index={self.chunk_index}, pages={self.page_start}-{self.page_end}, "
            f"chars={self.char_count})"
        )


@dataclass(frozen=True)
class ChunkingConfig:
    """Deterministic chunking rules (Step 12).

    Character-based rather than token-based, and openly so: the sentence
    transformer's tokenizer is available but its limits differ per model, and a
    transparent character budget that a reader can verify by counting is worth
    more here than a tokenizer-derived one they cannot. ``max_characters`` is
    chosen well inside the model's 256-token window for ordinary prose.
    """

    max_characters: int = 800
    overlap_characters: int = 100
    min_characters: int = 40
    #: Prefer breaking at a paragraph or sentence boundary within this many
    #: characters of the hard limit, so chunks end at readable places.
    boundary_search_window: int = 200
    version: str = "1.0"

    def __post_init__(self) -> None:
        from erp_pipeline.ai.errors import AIConfigurationError

        if self.max_characters < 1:
            raise AIConfigurationError(
                f"max_characters must be at least 1, got {self.max_characters}."
            )

        if self.overlap_characters < 0:
            raise AIConfigurationError(
                "overlap_characters must not be negative, got "
                f"{self.overlap_characters}."
            )

        if self.overlap_characters >= self.max_characters:
            raise AIConfigurationError(
                f"overlap_characters ({self.overlap_characters}) must be smaller "
                f"than max_characters ({self.max_characters}); an overlap at or "
                "beyond the chunk size cannot advance and would loop forever."
            )

        if self.min_characters < 0:
            raise AIConfigurationError(
                "min_characters must not be negative."
            )

    def fingerprint(self) -> str:
        """Part of chunk identity, so re-chunking under new rules is visible."""
        return (
            f"chunk@{self.version}/max={self.max_characters}"
            f"/ov={self.overlap_characters}/min={self.min_characters}"
        )


# ============================================================
# Representation building options (Steps 6, 7, 29)
# ============================================================

#: Canonical/operational keys that describe HOW a record was produced rather
#: than WHAT it says. Excluded from embedding text and from the content hash by
#: default (Step 6): a mapping-engine version tells a retrieval model nothing,
#: and including it would make every engine upgrade look like a content change.
DEFAULT_OPERATIONAL_KEYS: frozenset[str] = frozenset(
    {
        "mapping_id",
        "transformation_engine_version",
        "transformation_config",
        "canonical_model_identity",
        "validation_profile_version",
        "rules_applied",
        "sync_run_id",
        "run_id",
        "created_at",
        "updated_at",
        "extracted_at",
        "processed_at",
        "synced_at",
        "duration",
        "duration_seconds",
        "embedding_status",
        "qdrant_point_id",
        "content_hash",
        "schema_version",
        "model_version",
        "applied_to_data",
        "watermark",
        "version",
        "revision",
        "source_record_id",
    }
)


@dataclass(frozen=True)
class RepresentationConfig:
    """How a canonical record becomes AI-ready text and structure."""

    #: Keys treated as operational and kept out of the embedding text.
    operational_keys: frozenset[str] = DEFAULT_OPERATIONAL_KEYS
    #: Include the entity type as a leading line. Cheap, and it measurably helps
    #: distinguish an invoice from a purchase order with similar wording.
    include_entity_header: bool = True
    #: Include selected provenance facts (source system, entity) as text.
    include_source_context: bool = True
    #: Hard cap on generated text. Records are bounded rather than truncated by
    #: the model silently (Step 28).
    max_characters: int = 4000
    #: Label style for field names: ``invoice_id`` -> ``Invoice Id``.
    humanize_field_names: bool = True
    version: str = "1.0"

    def fingerprint(self) -> str:
        return (
            f"repr@{self.version}/header={int(self.include_entity_header)}"
            f"/src={int(self.include_source_context)}/max={self.max_characters}"
            f"/human={int(self.humanize_field_names)}"
        )


# ============================================================
# Embedding outcome (Steps 19, 25)
# ============================================================

class EmbeddingStatus(str, Enum):
    """What happened to one representation."""

    #: A vector was produced.
    GENERATED = "generated"
    #: Content and model are unchanged since the recorded embedding.
    SKIPPED_UNCHANGED = "skipped_unchanged"
    #: There was nothing meaningful to embed.
    EMPTY_CONTENT = "empty_content"
    #: Embedding was attempted and failed.
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def produced_vector(self) -> bool:
        return self is EmbeddingStatus.GENERATED


@dataclass(frozen=True)
class EmbeddingRecord:
    """The outcome of embedding one representation (Step 19).

    ``vector`` is deliberately absent from ``to_dict()`` and ``__repr__``. A run
    over 33,000 cases would otherwise produce a log containing the entire index,
    which is both unusable and a leak.
    """

    embedding_id: str
    representation_id: str
    content_hash: str
    model_id: str
    dimension: int
    status: EmbeddingStatus
    entity_type: str | None = None
    vector: tuple[float, ...] | None = None
    #: Safe reason when the status is FAILED or EMPTY_CONTENT.
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    engine_version: str = AI_ENGINE_VERSION

    @property
    def has_vector(self) -> bool:
        return self.vector is not None

    def to_dict(self, include_vector: bool = False) -> dict[str, Any]:
        """Privacy-safe by default (Step 54)."""
        payload: dict[str, Any] = {
            "embedding_id": self.embedding_id,
            "representation_id": self.representation_id,
            "entity_type": self.entity_type,
            "content_hash": self.content_hash,
            "model_id": self.model_id,
            "dimension": self.dimension,
            "status": self.status.value,
            "reason": self.reason,
            "engine_version": self.engine_version,
            "has_vector": self.has_vector,
        }

        if include_vector and self.vector is not None:
            payload["vector"] = list(self.vector)

        return payload

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"EmbeddingRecord(id={self.embedding_id!r}, "
            f"status={self.status.value}, model={self.model_id!r}, "
            f"dim={self.dimension}, has_vector={self.has_vector})"
        )


def make_representation_id(entity_type: str, stable_key: str) -> str:
    """``ai:{entity_type}:{stable_key}`` (Step 4).

    Deterministic, and distinct from canonical ``erp:`` ids so a representation
    and the record it projects can coexist in one store without collision.
    """
    return (
        f"{AI_ID_PREFIX}:{normalize_identifier(entity_type)}"
        f":{normalize_identifier(stable_key)}"
    )


def make_embedding_id(representation_id: str, model_id: str) -> str:
    """Deterministic embedding identity (Step 20).

    Derived from the representation AND the model, so re-embedding the same
    representation with the same model updates one logical embedding, while a
    model change produces a distinguishable one. Never a random UUID, and never
    content-derived - the content changing must UPDATE this embedding, not mint
    a new one.
    """
    return normalize_identifier(
        "emb."
        + hash_json_payload(
            {"representation": representation_id, "model": model_id}
        )[:20]
    )


# ============================================================
# Engine options (Steps 21, 22, 24, 26)
# ============================================================

class EmbeddingFailurePolicy(str, Enum):
    """What a batch does when one representation fails."""

    #: Record the failure and keep going. The default: one malformed record
    #: should not cost a 33,000-record run.
    CONTINUE = "continue"
    #: Stop at the first failure.
    FAIL_FAST = "fail_fast"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class EmbeddingOptions:
    """Everything that changes what the embedding service produces."""

    #: Passed to the model's own batch encoder. Matches the existing BPI
    #: pipeline's default so throughput characteristics stay comparable.
    batch_size: int = 64
    failure_policy: EmbeddingFailurePolicy = EmbeddingFailurePolicy.CONTINUE
    #: Re-embed even when content and model are unchanged. Off, because
    #: skipping unchanged content is the point of the hash (Step 24).
    force: bool = False
    #: Refuse to embed content shorter than this after stripping.
    min_content_characters: int = 1
    representation: RepresentationConfig = field(
        default_factory=RepresentationConfig
    )

    def __post_init__(self) -> None:
        from erp_pipeline.ai.errors import AIConfigurationError

        if self.batch_size < 1:
            raise AIConfigurationError(
                f"batch_size must be at least 1, got {self.batch_size}."
            )

        if self.min_content_characters < 0:
            raise AIConfigurationError(
                "min_content_characters must not be negative."
            )

    def fingerprint(self) -> str:
        return (
            f"emb@{AI_ENGINE_VERSION}/batch={self.batch_size}"
            f"/fail={self.failure_policy.value}/force={int(self.force)}"
            f"/min={self.min_content_characters}"
            f"/{self.representation.fingerprint()}"
        )


DEFAULT_EMBEDDING_OPTIONS = EmbeddingOptions()


# ============================================================
# Run summary (Steps 48, 57)
# ============================================================

@dataclass(frozen=True)
class EmbeddingRunSummary:
    """Counters over one embedding batch.

    INVARIANT (Step 57), asserted by test::

        representations_read ==
            embeddings_generated + embeddings_skipped
            + embeddings_failed + embeddings_empty
    """

    model_id: str
    dimension: int
    representations_read: int = 0
    embeddings_generated: int = 0
    embeddings_skipped: int = 0
    embeddings_failed: int = 0
    embeddings_empty: int = 0
    vectors_upserted: int = 0
    vectors_failed: int = 0
    duration_seconds: float = 0.0
    batch_size: int = 0
    records: tuple[EmbeddingRecord, ...] = ()

    @property
    def counters_balance(self) -> bool:
        return self.representations_read == (
            self.embeddings_generated
            + self.embeddings_skipped
            + self.embeddings_failed
            + self.embeddings_empty
        )

    @property
    def throughput_per_second(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return round(self.embeddings_generated / self.duration_seconds, 3)

    @property
    def average_latency_ms(self) -> float:
        if self.embeddings_generated <= 0:
            return 0.0
        return round(
            (self.duration_seconds / self.embeddings_generated) * 1000.0, 3
        )

    def records_with(self, status: EmbeddingStatus) -> tuple[EmbeddingRecord, ...]:
        return tuple(item for item in self.records if item.status is status)

    def to_dict(self) -> dict[str, Any]:
        """Privacy-safe metrics: counts, ids and model facts only."""
        return {
            "model_id": self.model_id,
            "dimension": self.dimension,
            "batch_size": self.batch_size,
            "representations_read": self.representations_read,
            "embeddings_generated": self.embeddings_generated,
            "embeddings_skipped": self.embeddings_skipped,
            "embeddings_failed": self.embeddings_failed,
            "embeddings_empty": self.embeddings_empty,
            "vectors_upserted": self.vectors_upserted,
            "vectors_failed": self.vectors_failed,
            "duration_seconds": self.duration_seconds,
            "throughput_per_second": self.throughput_per_second,
            "average_latency_ms": self.average_latency_ms,
            "counters_balance": self.counters_balance,
        }


__all__ = [
    "AI_ENGINE_VERSION",
    "AI_ID_PREFIX",
    "DocumentChunk",
    "ChunkingConfig",
    "DEFAULT_OPERATIONAL_KEYS",
    "RepresentationConfig",
    "EmbeddingStatus",
    "EmbeddingRecord",
    "EmbeddingFailurePolicy",
    "EmbeddingOptions",
    "DEFAULT_EMBEDDING_OPTIONS",
    "EmbeddingRunSummary",
    "make_representation_id",
    "make_embedding_id",
]
