"""Similarity and retrieval sanity evaluation (Steps 45-48).

WHAT THIS IS, AND IS NOT
------------------------
A regression and research SANITY benchmark on a small synthetic ERP corpus. It
is not evidence of production retrieval quality, and this module says so rather
than letting a good-looking number imply it.

Its real job is to catch the failures that would otherwise be invisible: a
representation builder that stops including business content, a model swapped
for one that does not understand the domain, a normalization bug that flattens
every similarity toward the same value. Those show up here immediately.

NO SELF-LABELLING
-----------------
Expected answers are hand-declared by the caller. Generating labels from the
model and then measuring the model against them would measure nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from erp_pipeline.ai.embedding import EmbeddingModel, cosine_similarity
from erp_pipeline.ai.models import EmbeddingStatus
from erp_pipeline.sync.propagation import AIRepresentation


@dataclass(frozen=True)
class SimilarityPair:
    """One hand-declared expectation about two pieces of content."""

    label: str
    left: str
    right: str
    #: True when the pair is expected to be semantically related.
    related: bool


@dataclass(frozen=True)
class SimilarityReport:
    """Measured similarities, related versus unrelated."""

    related: tuple[tuple[str, float], ...] = ()
    unrelated: tuple[tuple[str, float], ...] = ()

    @property
    def mean_related(self) -> float:
        if not self.related:
            return 0.0
        return round(sum(score for _, score in self.related) / len(self.related), 6)

    @property
    def mean_unrelated(self) -> float:
        if not self.unrelated:
            return 0.0
        return round(
            sum(score for _, score in self.unrelated) / len(self.unrelated), 6
        )

    @property
    def separation(self) -> float:
        """How far apart the two groups are. Positive is the expected direction."""
        return round(self.mean_related - self.mean_unrelated, 6)

    @property
    def min_related(self) -> float:
        return round(min((s for _, s in self.related), default=0.0), 6)

    @property
    def max_unrelated(self) -> float:
        return round(max((s for _, s in self.unrelated), default=0.0), 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "related_count": len(self.related),
            "unrelated_count": len(self.unrelated),
            "mean_related": self.mean_related,
            "mean_unrelated": self.mean_unrelated,
            "separation": self.separation,
            "min_related": self.min_related,
            "max_unrelated": self.max_unrelated,
        }


def evaluate_similarity(
    model: EmbeddingModel, pairs: Sequence[SimilarityPair]
) -> SimilarityReport:
    """Measure cosine similarity for each hand-declared pair."""
    texts: list[str] = []
    for pair in pairs:
        texts.extend((pair.left, pair.right))

    vectors = model.encode(texts, batch_size=32)

    related: list[tuple[str, float]] = []
    unrelated: list[tuple[str, float]] = []

    for index, pair in enumerate(pairs):
        score = round(
            cosine_similarity(vectors[index * 2], vectors[index * 2 + 1]), 6
        )
        (related if pair.related else unrelated).append((pair.label, score))

    return SimilarityReport(related=tuple(related), unrelated=tuple(unrelated))


@dataclass(frozen=True)
class RetrievalQuery:
    """A query and the representation id a human says should answer it."""

    query: str
    expected_representation_id: str


@dataclass(frozen=True)
class RetrievalReport:
    """Measured top-1 and top-3 behaviour over a labelled corpus."""

    corpus_size: int
    query_count: int
    top1_hits: int
    top3_hits: int
    results: tuple[tuple[str, str, int], ...] = ()

    @property
    def top1_accuracy(self) -> float:
        if self.query_count <= 0:
            return 0.0
        return round(self.top1_hits / self.query_count, 6)

    @property
    def top3_hit_rate(self) -> float:
        if self.query_count <= 0:
            return 0.0
        return round(self.top3_hits / self.query_count, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_size": self.corpus_size,
            "query_count": self.query_count,
            "top1_hits": self.top1_hits,
            "top3_hits": self.top3_hits,
            "top1_accuracy": self.top1_accuracy,
            "top3_hit_rate": self.top3_hit_rate,
        }


def evaluate_retrieval(
    model: EmbeddingModel,
    corpus: Sequence[AIRepresentation],
    queries: Sequence[RetrievalQuery],
) -> RetrievalReport:
    """Rank the corpus against each query by cosine similarity.

    Ties break by representation id, so a corpus containing two identical texts
    ranks reproducibly rather than by dictionary order.
    """
    corpus_vectors = model.encode(
        [(item.text_for_ai or "") for item in corpus], batch_size=32
    )
    query_vectors = model.encode([q.query for q in queries], batch_size=32)

    top1 = 0
    top3 = 0
    results: list[tuple[str, str, int]] = []

    for query, query_vector in zip(queries, query_vectors):
        scored = sorted(
            (
                (-cosine_similarity(query_vector, vector), item.representation_id)
                for item, vector in zip(corpus, corpus_vectors)
            )
        )
        ranked = [representation_id for _, representation_id in scored]

        try:
            rank = ranked.index(query.expected_representation_id) + 1
        except ValueError:  # pragma: no cover - a label naming an absent record
            rank = len(ranked) + 1

        if rank == 1:
            top1 += 1
        if rank <= 3:
            top3 += 1

        results.append((query.query, query.expected_representation_id, rank))

    return RetrievalReport(
        corpus_size=len(corpus),
        query_count=len(queries),
        top1_hits=top1,
        top3_hits=top3,
        results=tuple(results),
    )


__all__ = [
    "SimilarityPair",
    "SimilarityReport",
    "evaluate_similarity",
    "RetrievalQuery",
    "RetrievalReport",
    "evaluate_retrieval",
]
