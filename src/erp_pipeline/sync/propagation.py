"""Downstream propagation contracts: canonical, representation, embedding, vector.

WHY THESE ARE INTERFACES
------------------------
The generic sync engine must not know that this project happens to use
PostgreSQL for canonical storage and Qdrant for vectors. It must not import
``bpi2020`` either (Step 76). So every downstream stage is a narrow protocol
with an in-memory implementation for tests, and real systems arrive through
adapters that live outside this package.

That is also what keeps Phase 10 inside its boundary: it proves incremental
propagation reaches the vector layer WITHOUT designing the Phase 11/12
embedding and hybrid-tier architecture.

THE AI-READY REPRESENTATION
---------------------------
``AIRepresentation`` is deliberately small - six fields, exactly what Step 20
asks for. It is not a new knowledge model competing with ``CanonicalRecord``.
The two differ in a way that matters here: a ``CanonicalRecord`` is ONE source
record with a full provenance envelope, while a representation is an
AGGREGATE - a process case built from many events - that must be comparable by
hash before anyone decides whether it is worth embedding. An adapter converts
freely between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from erp_pipeline.schemas.canonical_models import CanonicalRecord
from erp_pipeline.sync.hashing import representation_content_hash, vector_id_for


# ============================================================
# AI-ready representation (Step 20)
# ============================================================

@dataclass(frozen=True)
class AIRepresentation:
    """One unit of content an embedding model would be asked to represent."""

    representation_id: str
    entity_type: str
    text_for_ai: str | None = None
    content: Mapping[str, Any] = field(default_factory=dict)
    #: Canonical record ids this representation was built from.
    source_record_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: Supplied by a builder, or computed on demand. Never a runtime value.
    content_hash: str | None = None

    def compute_hash(self) -> str:
        return representation_content_hash(
            self.representation_id, self.text_for_ai, self.content
        )

    def resolved_hash(self) -> str:
        return self.content_hash or self.compute_hash()

    @property
    def vector_id(self) -> str:
        """Stable across every update to this representation (Step 25)."""
        return vector_id_for(self.representation_id)

    def to_dict(self) -> dict[str, Any]:
        """Privacy-safe: identity, hash and counts - never the content."""
        return {
            "representation_id": self.representation_id,
            "entity_type": self.entity_type,
            "content_hash": self.resolved_hash(),
            "vector_id": self.vector_id,
            "source_record_count": len(self.source_record_ids),
            "has_text": self.text_for_ai is not None,
        }


@dataclass(frozen=True)
class EmbeddingResult:
    """What an embedding updater produced."""

    representation_id: str
    content_hash: str
    #: The vector itself. Optional: an updater that writes straight to a store
    #: may return only the fact that it succeeded.
    vector: tuple[float, ...] | None = None
    model_id: str | None = None
    dimensions: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "representation_id": self.representation_id,
            "content_hash": self.content_hash,
            "model_id": self.model_id,
            "dimensions": self.dimensions or (
                len(self.vector) if self.vector else None
            ),
        }


# ============================================================
# Protocols
# ============================================================

@runtime_checkable
class CanonicalRecordStore(Protocol):
    """Where canonical records live (Step 15).

    ``upsert`` must be IDEMPOTENT: the same record processed twice produces one
    stored record, not two. That is what lets the sync engine offer
    at-least-once delivery without at-least-once duplication.
    """

    def upsert(self, record: CanonicalRecord) -> bool:
        """Store or replace. Returns True when stored content changed."""
        ...  # pragma: no cover - protocol declaration

    def delete(self, record_id: str) -> bool:
        """Remove. Returns True when something was removed."""
        ...  # pragma: no cover - protocol declaration

    def get(self, record_id: str) -> CanonicalRecord | None:
        ...  # pragma: no cover - protocol declaration


@runtime_checkable
class AffectedRepresentationResolver(Protocol):
    """Which AI-ready representations one change affects (Steps 18, 19).

    This is the heart of "no full rebuild". A changed event affects ONE case,
    not all of them, and only something that understands the domain can say
    which. So the generic coordinator asks, rather than assuming - and no BPI
    process-case semantics leak into it.
    """

    def resolve_affected(
        self, change: Any, record: CanonicalRecord | None
    ) -> tuple[str, ...]:
        ...  # pragma: no cover - protocol declaration


@runtime_checkable
class AIRepresentationBuilder(Protocol):
    """Rebuilds ONE representation by key (Step 19).

    Keyed and singular on purpose: a builder that could only rebuild everything
    would make the affected-set analysis pointless.
    """

    def rebuild(self, key: str) -> AIRepresentation | None:
        ...  # pragma: no cover - protocol declaration


@runtime_checkable
class RepresentationHashLedger(Protocol):
    """Remembers the content hash last embedded for each representation.

    Without a DURABLE record of what was last embedded, "skip when unchanged"
    cannot work across runs - the engine would have nothing to compare against
    and would re-embed everything every time, which is the full rebuild this
    phase exists to eliminate.

    In this repository the BPI equivalent already exists as the
    ``ai_ready_cases.content_hash`` column, so an adapter maps onto it rather
    than introducing a second source of truth.
    """

    def get_hash(self, representation_id: str) -> str | None:
        ...  # pragma: no cover - protocol declaration

    def set_hash(self, representation_id: str, content_hash: str) -> None:
        ...  # pragma: no cover - protocol declaration

    def forget(self, representation_id: str) -> None:
        ...  # pragma: no cover - protocol declaration


class InMemoryHashLedger:
    """Hash ledger held in a dictionary."""

    def __init__(self, hashes: Mapping[str, str] | None = None) -> None:
        self._hashes: dict[str, str] = dict(hashes or {})

    def get_hash(self, representation_id: str) -> str | None:
        return self._hashes.get(representation_id)

    def set_hash(self, representation_id: str, content_hash: str) -> None:
        self._hashes[representation_id] = content_hash

    def forget(self, representation_id: str) -> None:
        self._hashes.pop(representation_id, None)

    def __len__(self) -> int:
        return len(self._hashes)


@runtime_checkable
class EmbeddingUpdater(Protocol):
    """Turns a representation into an embedding (Step 23).

    Narrow by design. Phase 11 owns model selection, batching and tiering; all
    Phase 10 needs is "embed this one thing, and tell me you did".
    """

    def embed(self, representation: AIRepresentation) -> EmbeddingResult:
        ...  # pragma: no cover - protocol declaration


@runtime_checkable
class VectorRecordStore(Protocol):
    """Where vectors live (Step 24)."""

    def upsert(
        self, representation: AIRepresentation, embedding: EmbeddingResult
    ) -> bool:
        ...  # pragma: no cover - protocol declaration

    def delete(self, vector_id: str) -> bool:
        ...  # pragma: no cover - protocol declaration


# ============================================================
# In-memory implementations (tests, and the reference semantics)
# ============================================================

class InMemoryCanonicalStore:
    """Idempotent canonical store keyed by ``record_id`` (Step 16).

    Also the executable specification of what an adapter must do: replace by
    identity, report whether content actually changed, and never accumulate a
    second row for the same record.
    """

    def __init__(self) -> None:
        self._records: dict[str, CanonicalRecord] = {}
        self.upsert_calls = 0
        self.delete_calls = 0

    def upsert(self, record: CanonicalRecord) -> bool:
        self.upsert_calls += 1
        existing = self._records.get(record.record_id)
        self._records[record.record_id] = record
        if existing is None:
            return True
        return existing.content_hash != record.content_hash

    def delete(self, record_id: str) -> bool:
        self.delete_calls += 1
        return self._records.pop(record_id, None) is not None

    def get(self, record_id: str) -> CanonicalRecord | None:
        return self._records.get(record_id)

    def __len__(self) -> int:
        return len(self._records)

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))


class StaticAffectedResolver:
    """Resolves affected representations from a declared mapping.

    Useful when the relationship is a simple lookup - which is the common case
    for a per-record representation, where a canonical record IS its own
    representation.
    """

    def __init__(
        self,
        mapping: Mapping[str, Iterable[str]] | None = None,
        default_to_record_id: bool = True,
    ) -> None:
        self._mapping = {k: tuple(v) for k, v in (mapping or {}).items()}
        self._default_to_record_id = default_to_record_id
        self.calls = 0

    def resolve_affected(
        self, change: Any, record: CanonicalRecord | None
    ) -> tuple[str, ...]:
        self.calls += 1
        key = getattr(change, "record_key", None)

        if key is not None and key in self._mapping:
            return self._mapping[key]

        if record is not None and record.record_id in self._mapping:
            return self._mapping[record.record_id]

        if self._default_to_record_id and record is not None:
            return (record.record_id,)

        return ()


class CountingEmbeddingUpdater:
    """Deterministic fake embedder that counts invocations (Step 23).

    The count is the proof: "only the affected representation was embedded" is
    only a claim until something has counted the calls.

    The vector is a deterministic function of the content hash, so a test can
    assert that identical content produces an identical vector without any
    model, any download and any network.
    """

    def __init__(self, dimensions: int = 8, model_id: str = "fake-deterministic") -> None:
        self.dimensions = dimensions
        self.model_id = model_id
        self.calls = 0
        self.embedded_ids: list[str] = []

    def embed(self, representation: AIRepresentation) -> EmbeddingResult:
        self.calls += 1
        self.embedded_ids.append(representation.representation_id)

        digest = representation.resolved_hash()
        vector = tuple(
            int(digest[index * 2 : index * 2 + 2], 16) / 255.0
            for index in range(self.dimensions)
        )

        return EmbeddingResult(
            representation_id=representation.representation_id,
            content_hash=digest,
            vector=vector,
            model_id=self.model_id,
            dimensions=self.dimensions,
        )


class InMemoryVectorStore:
    """Vector store keyed by the STABLE vector id (Step 25).

    Writing the same representation twice replaces one entry; it never creates
    a second. Counters make "one vector update, not 33,000" checkable.
    """

    def __init__(self) -> None:
        self._vectors: dict[str, dict[str, Any]] = {}
        self.upsert_calls = 0
        self.delete_calls = 0

    def upsert(
        self, representation: AIRepresentation, embedding: EmbeddingResult
    ) -> bool:
        self.upsert_calls += 1
        vector_id = representation.vector_id
        existed = vector_id in self._vectors

        self._vectors[vector_id] = {
            "representation_id": representation.representation_id,
            "entity_type": representation.entity_type,
            "content_hash": embedding.content_hash,
            "vector": embedding.vector,
            "model_id": embedding.model_id,
        }

        return not existed

    def delete(self, vector_id: str) -> bool:
        self.delete_calls += 1
        return self._vectors.pop(vector_id, None) is not None

    def get(self, vector_id: str) -> dict[str, Any] | None:
        return self._vectors.get(vector_id)

    def __len__(self) -> int:
        return len(self._vectors)

    @property
    def vector_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._vectors))


class DictRepresentationBuilder:
    """Builds representations from an in-memory table of aggregates.

    Counts rebuilds, so a test can assert the minimal affected set was rebuilt
    rather than the whole corpus (Step 26).
    """

    def __init__(
        self, aggregates: Mapping[str, AIRepresentation] | None = None
    ) -> None:
        self._aggregates: dict[str, AIRepresentation] = dict(aggregates or {})
        self.rebuild_calls = 0
        self.rebuilt_keys: list[str] = []

    def set(self, representation: AIRepresentation) -> None:
        self._aggregates[representation.representation_id] = representation

    def remove(self, key: str) -> None:
        self._aggregates.pop(key, None)

    def rebuild(self, key: str) -> AIRepresentation | None:
        self.rebuild_calls += 1
        self.rebuilt_keys.append(key)
        return self._aggregates.get(key)


class FailingStage:
    """Test double that fails a named stage a configured number of times.

    Used to prove retry safety (Steps 31, 64, 65) without corrupting a real
    store: the failure is injected, the checkpoint behaviour is real.
    """

    def __init__(self, delegate: Any, fail_times: int = 1) -> None:
        self._delegate = delegate
        self._remaining = fail_times
        self.attempts = 0

    def _maybe_fail(self) -> None:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("injected downstream failure")

    def embed(self, representation: AIRepresentation) -> EmbeddingResult:
        self._maybe_fail()
        return self._delegate.embed(representation)

    def upsert(self, *args: Any, **kwargs: Any) -> bool:
        self._maybe_fail()
        return self._delegate.upsert(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        self._maybe_fail()
        return self._delegate.delete(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


__all__ = [
    "AIRepresentation",
    "EmbeddingResult",
    "CanonicalRecordStore",
    "AffectedRepresentationResolver",
    "AIRepresentationBuilder",
    "EmbeddingUpdater",
    "VectorRecordStore",
    "RepresentationHashLedger",
    "InMemoryHashLedger",
    "InMemoryCanonicalStore",
    "StaticAffectedResolver",
    "CountingEmbeddingUpdater",
    "InMemoryVectorStore",
    "DictRepresentationBuilder",
    "FailingStage",
]
