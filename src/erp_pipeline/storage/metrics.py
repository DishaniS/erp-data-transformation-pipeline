"""Measurement helpers that never blur how a number was obtained.

THE ONE RULE (Steps 24, 25, 26, 59)
-----------------------------------
Every quantity carries a ``MeasurementKind``:

    MEASURED    read from the filesystem or the store itself
    PROXY       computed from measured inputs by a stated formula
    ESTIMATED   an assumption

The cold tier's bytes are MEASURED - they are real files. The Qdrant tiers'
bytes are PROXY, because the client exposes point counts and configuration but
not a collection's physical size on disk.

WHY THE FOOTPRINTS ARE NOT DIRECTLY COMPARABLE
----------------------------------------------
This is the trap the benchmark has to avoid. The HOT/WARM proxy counts ONLY the
vector payload - 384 floats. The COLD measurement counts a whole file: header,
nonce, GCM tag, the vector AND its metadata payload. Comparing 4653 measured
cold bytes against 1536 proxy hot bytes and concluding "cold is bigger" would be
comparing different content scopes.

So ``vector_payload_proxy()`` gives all three tiers a like-for-like number, and
the full artifact size is reported separately and labelled as such.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from erp_pipeline.storage.models import (
    MeasurementKind,
    StorageFootprint,
    StorageTier,
)

#: float32 and int8 component sizes, used by the like-for-like proxy.
FLOAT32_BYTES = 4
INT8_BYTES = 1


@dataclass(frozen=True)
class LatencySample:
    """Timing over repeated measurements (Step 33)."""

    label: str
    samples_ms: tuple[float, ...]

    @property
    def count(self) -> int:
        return len(self.samples_ms)

    @property
    def median_ms(self) -> float:
        if not self.samples_ms:
            return 0.0
        return round(statistics.median(self.samples_ms), 4)

    @property
    def mean_ms(self) -> float:
        if not self.samples_ms:
            return 0.0
        return round(statistics.fmean(self.samples_ms), 4)

    @property
    def p95_ms(self) -> float:
        """95th percentile, or ``None``-like 0.0 when the sample is too small.

        Reporting a p95 from three measurements would be theatre, so it is only
        computed once there are enough samples for the number to mean anything.
        """
        if len(self.samples_ms) < 20:
            return 0.0

        ordered = sorted(self.samples_ms)
        index = max(0, int(round(0.95 * (len(ordered) - 1))))

        return round(ordered[index], 4)

    @property
    def min_ms(self) -> float:
        return round(min(self.samples_ms), 4) if self.samples_ms else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "measurements": self.count,
            "median_ms": self.median_ms,
            "mean_ms": self.mean_ms,
            "p95_ms": self.p95_ms if self.count >= 20 else None,
            "p95_available": self.count >= 20,
            "min_ms": self.min_ms,
        }


def measure_latency(
    label: str,
    operation: Callable[[int], Any],
    iterations: int = 30,
    warmup: int = 3,
) -> LatencySample:
    """Time an operation repeatedly, discarding warm-up runs.

    Warm-up matters: the first Qdrant query pays connection and cache costs
    that have nothing to do with the tier being measured, and including it
    would make whichever tier ran first look slowest.
    """
    for index in range(max(0, warmup)):
        operation(index)

    samples: list[float] = []

    for index in range(iterations):
        started = time.perf_counter()
        operation(index)
        samples.append((time.perf_counter() - started) * 1000.0)

    return LatencySample(label=label, samples_ms=tuple(samples))


def vector_payload_proxy(
    tier: StorageTier, record_count: int, dimension: int, quantized: bool
) -> StorageFootprint:
    """Like-for-like vector payload size across all three tiers.

    The ONLY footprint number that may be compared directly between tiers,
    because it measures the same thing in each: the stored vector components.
    COLD keeps full float32 inside its archive, so its proxy equals HOT's - the
    cold saving is compression, which the archive measurement captures instead.
    """
    per_component = INT8_BYTES if quantized else FLOAT32_BYTES
    per_record = dimension * per_component

    return StorageFootprint(
        tier=tier,
        record_count=record_count,
        bytes_total=float(record_count * per_record),
        bytes_per_record=float(per_record),
        kind=MeasurementKind.PROXY,
        method=(
            f"VECTOR_PAYLOAD_PROXY: record_count x {dimension} x "
            f"{per_component} bytes "
            f"({'int8' if quantized else 'float32'}); vector components only, "
            "excluding index structures, payloads and container overhead"
        ),
        detail={
            "dimension": dimension,
            "bytes_per_component": per_component,
            "quantized": quantized,
            "scope": "vector components only",
        },
    )


@dataclass(frozen=True)
class RecallResult:
    """Recall at several cut-offs against hand-declared labels."""

    label: str
    query_count: int
    hits_at: Mapping[int, int] = field(default_factory=dict)

    def recall_at(self, k: int) -> float:
        if self.query_count <= 0:
            return 0.0
        return round(self.hits_at.get(k, 0) / self.query_count, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "query_count": self.query_count,
            "recall_at_1": self.recall_at(1),
            "recall_at_3": self.recall_at(3),
            "recall_at_5": self.recall_at(5),
        }


def evaluate_recall(
    label: str,
    ranked_results: Sequence[Sequence[str]],
    expected: Sequence[str],
    cutoffs: Sequence[int] = (1, 3, 5),
) -> RecallResult:
    """Recall against LABELS, never against another tier's ranking.

    Using HOT's output as ground truth would measure agreement, not quality,
    and would make HOT perfect by definition.
    """
    hits = {k: 0 for k in cutoffs}

    for ranked, target in zip(ranked_results, expected):
        for k in cutoffs:
            if target in list(ranked)[:k]:
                hits[k] += 1

    return RecallResult(
        label=label, query_count=len(expected), hits_at=hits
    )


def ranking_overlap(
    left: Sequence[Sequence[str]], right: Sequence[Sequence[str]], k: int = 5
) -> float:
    """Fraction of top-k results two tiers agree on.

    A DIAGNOSTIC, not a quality metric: it says how much quantization changed
    the ranking, which is useful, but it says nothing about whether either
    ranking is correct.
    """
    if not left:
        return 0.0

    totals = []

    for a, b in zip(left, right):
        top_a, top_b = set(list(a)[:k]), set(list(b)[:k])
        if not top_a:
            continue
        totals.append(len(top_a & top_b) / len(top_a))

    return round(sum(totals) / len(totals), 6) if totals else 0.0


__all__ = [
    "FLOAT32_BYTES",
    "INT8_BYTES",
    "LatencySample",
    "measure_latency",
    "vector_payload_proxy",
    "RecallResult",
    "evaluate_recall",
    "ranking_overlap",
]
