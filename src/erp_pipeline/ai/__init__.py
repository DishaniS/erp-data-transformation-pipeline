"""AI-ready knowledge and generic embedding for the ERP pipeline (Phase 11).

    CanonicalRecord                    ExtractedDocument (PDF / image OCR)
          │                                     │
          ▼                                     ▼
    deterministic text                  page-traced chunks
    + structured payload                        │
          │                                     ▼
          └──────────────┬──────────── AIRepresentation (Phase 10's, reused)
                         ▼
                   content_hash
                         ▼
                  EmbeddingService          batched, streaming
                         ▼
                   EmbeddingRecord
                         ▼
                    VectorStore            handoff only - Phase 12 routes

THE RESEARCH CLAIM
------------------
One embedding subsystem serves records that originated in PostgreSQL, MySQL,
SQL Server, MongoDB, CSV, an OpenAPI/Postman contract, a PDF or a scanned
image. By the time content reaches this package the source technology has been
abstracted away by Phases 4-10, so there is no per-source embedding path and no
branch on where anything came from.

WHAT IS REUSED
--------------
``AIRepresentation``, ``representation_content_hash`` and ``vector_id_for`` are
Phase 10's, imported rather than redefined. Phase 10's skip-if-unchanged logic
depends on that hash, and a second formula would break it silently.

BOUNDARIES
----------
Local model only - no OpenAI, Gemini, Anthropic or remote inference API, and no
code path that could fall back to one. No hot/warm/cold routing, no tier
scoring, no migration and no archival: those are Phase 12's, and a static test
asserts their absence here.
"""

from __future__ import annotations

from erp_pipeline.ai.chunking import (
    PAGE_SEPARATOR,
    chunk_document,
    chunk_text,
    chunk_to_representation,
    document_to_representations,
)
from erp_pipeline.ai.embedding import (
    DEFAULT_MODEL_ID,
    DeterministicTestModel,
    EmbeddingModel,
    ModelFingerprint,
    SentenceTransformerModel,
    cosine_similarity,
)
from erp_pipeline.ai.errors import (
    AIConfigurationError,
    AIError,
    ChunkingError,
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingModelUnavailableError,
    EmptyAIContentError,
    VectorStoreError,
)
from erp_pipeline.ai.evaluation import (
    RetrievalQuery,
    RetrievalReport,
    SimilarityPair,
    SimilarityReport,
    evaluate_retrieval,
    evaluate_similarity,
)
from erp_pipeline.ai.hashing import (
    chunk_content_hash,
    make_chunk_id,
    representation_content_hash,
    strip_volatile,
    vector_id_for,
)
from erp_pipeline.ai.integration import (
    Phase11EmbeddingUpdater,
    Phase11VectorRecordStore,
)
from erp_pipeline.ai.model_registry import (
    available_models,
    create_model,
    register_model,
)
from erp_pipeline.ai.models import (
    AI_ENGINE_VERSION,
    AI_ID_PREFIX,
    DEFAULT_EMBEDDING_OPTIONS,
    DEFAULT_OPERATIONAL_KEYS,
    ChunkingConfig,
    DocumentChunk,
    EmbeddingFailurePolicy,
    EmbeddingOptions,
    EmbeddingRecord,
    EmbeddingRunSummary,
    EmbeddingStatus,
    RepresentationConfig,
    make_embedding_id,
    make_representation_id,
)
from erp_pipeline.ai.representation import (
    build_text,
    canonical_record_to_representation,
    canonical_records_to_representations,
    flatten,
    format_value,
    humanize,
)
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.ai.vector import (
    InMemoryEmbeddingStore,
    QdrantVectorStore,
    VectorStore,
    build_vector_payload,
)

#: Re-exported from Phase 10 so callers have one obvious import site, while the
#: definition stays in exactly one place.
from erp_pipeline.sync.propagation import AIRepresentation, EmbeddingResult

__all__ = [
    # errors
    "AIError",
    "AIConfigurationError",
    "ChunkingError",
    "EmbeddingError",
    "EmbeddingDimensionError",
    "EmbeddingModelUnavailableError",
    "EmptyAIContentError",
    "VectorStoreError",
    # representation
    "AIRepresentation",
    "AI_ENGINE_VERSION",
    "AI_ID_PREFIX",
    "RepresentationConfig",
    "DEFAULT_OPERATIONAL_KEYS",
    "make_representation_id",
    "canonical_record_to_representation",
    "canonical_records_to_representations",
    "build_text",
    "flatten",
    "format_value",
    "humanize",
    # chunking
    "ChunkingConfig",
    "DocumentChunk",
    "PAGE_SEPARATOR",
    "chunk_text",
    "chunk_document",
    "chunk_to_representation",
    "document_to_representations",
    # hashing
    "representation_content_hash",
    "chunk_content_hash",
    "make_chunk_id",
    "strip_volatile",
    "vector_id_for",
    # model
    "DEFAULT_MODEL_ID",
    "EmbeddingModel",
    "ModelFingerprint",
    "SentenceTransformerModel",
    "DeterministicTestModel",
    "cosine_similarity",
    "available_models",
    "create_model",
    "register_model",
    # embedding
    "EmbeddingStatus",
    "EmbeddingRecord",
    "EmbeddingResult",
    "EmbeddingOptions",
    "EmbeddingFailurePolicy",
    "DEFAULT_EMBEDDING_OPTIONS",
    "EmbeddingRunSummary",
    "EmbeddingService",
    "make_embedding_id",
    # vector
    "VectorStore",
    "InMemoryEmbeddingStore",
    "QdrantVectorStore",
    "build_vector_payload",
    # Phase 10 integration
    "Phase11EmbeddingUpdater",
    "Phase11VectorRecordStore",
    # evaluation
    "SimilarityPair",
    "SimilarityReport",
    "evaluate_similarity",
    "RetrievalQuery",
    "RetrievalReport",
    "evaluate_retrieval",
]
