"""Controlled errors raised by the AI-ready knowledge and embedding engine.

PRIVACY
-------
No exception here carries AI-ready text or a vector. Messages name
representation ids, entity types, model ids, dimensions and counts - never the
business content being embedded. An embedding pipeline runs over real ERP data,
so a traceback that leaked a case summary would undo the privacy work of every
earlier phase.

RAISED VERSUS RECORDED
----------------------
A single representation that cannot be embedded is not an exception under the
default policy - it is an expected outcome, recorded as a FAILED
``EmbeddingRecord`` so the batch continues. Exceptions are for problems no
amount of trying different representations will fix: an unavailable model, a
collection whose dimension disagrees with the model, a chunking configuration
that cannot produce chunks.
"""

from __future__ import annotations


class AIError(Exception):
    """Base class for every error this package raises."""


class EmbeddingError(AIError):
    """A representation could not be embedded."""

    def __init__(self, message: str, representation_id: str | None = None) -> None:
        super().__init__(message)
        self.representation_id = representation_id


class EmbeddingModelUnavailableError(AIError):
    """The embedding model could not be loaded.

    Deliberately loud rather than silently falling back to a remote API or a
    random vector: an embedding produced by something other than the declared
    model would poison the index in a way nothing downstream could detect.
    """


class EmbeddingDimensionError(AIError):
    """A vector's dimension does not match what was expected.

    Raised before a vector reaches a store, so the failure names the model and
    the collection rather than surfacing as an opaque driver error (Step 38).
    """

    def __init__(
        self,
        message: str,
        expected: int | None = None,
        actual: int | None = None,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class EmptyAIContentError(AIError):
    """There is no content worth embedding.

    An embedding of an empty string is a valid vector pointing nowhere
    meaningful; storing one would silently pollute retrieval, so empty content
    is reported instead (Step 27).
    """

    def __init__(self, message: str, representation_id: str | None = None) -> None:
        super().__init__(message)
        self.representation_id = representation_id


class ChunkingError(AIError):
    """A document could not be split into chunks."""


class VectorStoreError(AIError):
    """A vector store operation failed."""


class AIConfigurationError(AIError):
    """The engine was configured in a way that cannot work.

    Overlap larger than the chunk size, a batch size of zero, a negative
    dimension. Raised at construction, before any content is processed.
    """


__all__ = [
    "AIError",
    "EmbeddingError",
    "EmbeddingModelUnavailableError",
    "EmbeddingDimensionError",
    "EmptyAIContentError",
    "ChunkingError",
    "VectorStoreError",
    "AIConfigurationError",
]
