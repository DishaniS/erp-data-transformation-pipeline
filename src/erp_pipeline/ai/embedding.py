"""The embedding model abstraction and its local implementation.

    EmbeddingModel (protocol)
        ├── SentenceTransformerModel   the real local model, loaded once
        └── DeterministicTestModel     no download, no network, reproducible

WHY AN ABSTRACTION (Step 15)
----------------------------
Nothing in the rest of the pipeline imports ``SentenceTransformer``. The service,
the vector adapters and the evaluation code all speak to ``EmbeddingModel``, so
swapping the model - which Phase 12 and any later research run will want to do -
touches one class rather than every call site.

LOCAL ONLY (Step 55)
--------------------
There is no OpenAI, Gemini, Anthropic or remote Hugging Face inference path
here, and no code that would silently fall back to one. If the local model
cannot be loaded, that is reported as
``EmbeddingModelUnavailableError`` - a run that quietly switched to a remote
service would ship ERP content off the machine.

DIMENSION IS MEASURED, NOT ASSUMED (Step 18)
--------------------------------------------
``all-MiniLM-L6-v2`` is 384-dimensional, but this module asks the loaded model
and then verifies against the first vector it actually produces. A hard-coded
constant that drifted from reality would be discovered only when a vector store
rejected a write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from erp_pipeline.ai.errors import (
    EmbeddingDimensionError,
    EmbeddingModelUnavailableError,
)

#: The model the BPI pipeline already uses and has verified. Reused rather than
#: replaced (Step 16) so Phase 11 vectors are comparable with existing ones.
DEFAULT_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"


@runtime_checkable
class EmbeddingModel(Protocol):
    """Anything that can turn text into vectors."""

    @property
    def model_id(self) -> str:
        ...  # pragma: no cover - protocol declaration

    @property
    def dimension(self) -> int:
        ...  # pragma: no cover - protocol declaration

    def encode(
        self, texts: Sequence[str], batch_size: int = 32
    ) -> list[tuple[float, ...]]:
        ...  # pragma: no cover - protocol declaration


@dataclass
class ModelFingerprint:
    """Enough metadata to know which model produced a vector (Step 49).

    ``library_version`` is populated only when it can actually be read.
    Reporting a revision that is not genuinely known would be worse than
    reporting none, because it would look like provenance while being a guess.
    """

    model_id: str
    dimension: int
    library_version: str | None = None
    normalizes_output: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "dimension": self.dimension,
            "library_version": self.library_version,
            "normalizes_output": self.normalizes_output,
        }

    def identity(self) -> str:
        """What "the same model" means for re-embedding decisions."""
        return f"{self.model_id}@{self.dimension}"


class SentenceTransformerModel:
    """The real local sentence-transformers model.

    Loaded LAZILY and exactly ONCE per instance (Step 17). Loading a
    transformer costs seconds; doing it per record would dominate a run and
    make every measured latency meaningless. ``load_count`` exists so a test can
    prove the reuse rather than assume it.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        normalize: bool = False,
        device: str | None = None,
    ) -> None:
        self._model_id = model_id
        self._normalize = normalize
        self._device = device
        self._model: Any = None
        self._dimension: int | None = None
        self._normalized_output: bool | None = None
        self.load_count = 0
        self.encode_calls = 0

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - dependency present here
            raise EmbeddingModelUnavailableError(
                "sentence-transformers is not installed, and this engine will "
                "not fall back to a remote embedding service."
            ) from exc

        try:
            kwargs = {"device": self._device} if self._device else {}
            self._model = SentenceTransformer(self._model_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            raise EmbeddingModelUnavailableError(
                f"the local embedding model {self._model_id!r} could not be "
                f"loaded ({type(exc).__name__}). No remote fallback is "
                "attempted."
            ) from exc

        self.load_count += 1

        return self._model

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            model = self._load()

            # sentence-transformers renamed this; support both rather than
            # pinning a version, and fall back to measuring an actual vector
            # if neither accessor exists (Step 18: measured, not assumed).
            declared = None
            for accessor in ("get_embedding_dimension",
                             "get_sentence_embedding_dimension"):
                getter = getattr(model, accessor, None)
                if callable(getter):
                    declared = getter()
                    break

            if declared is None:
                declared = len(
                    model.encode(
                        ["dimension probe"],
                        show_progress_bar=False,
                        convert_to_numpy=True,
                    )[0]
                )

            self._dimension = int(declared)

        return self._dimension

    def output_is_normalized(self, tolerance: float = 1e-3) -> bool:
        """Whether this model already returns unit-length vectors.

        MEASURED, not assumed (Step 52). ``all-MiniLM-L6-v2`` ships a
        ``Normalize`` module in its sentence-transformers pipeline, so its
        output norm is ~1.0 even with ``normalize_embeddings=False`` - which is
        the opposite of what the flag name suggests. Probing once and caching
        the answer is the only way to state this correctly for an arbitrary
        model.
        """
        if self._normalized_output is None:
            vector = self.encode(["normalization probe"])[0]
            norm = sum(value * value for value in vector) ** 0.5
            self._normalized_output = abs(norm - 1.0) <= tolerance

        return self._normalized_output

    def fingerprint(self, probe_normalization: bool = True) -> ModelFingerprint:
        version: str | None = None

        try:
            import sentence_transformers

            version = getattr(sentence_transformers, "__version__", None)
        except Exception:  # noqa: BLE001 - metadata is best-effort
            version = None

        return ModelFingerprint(
            model_id=self._model_id,
            dimension=self.dimension,
            library_version=version,
            normalizes_output=(
                self.output_is_normalized() if probe_normalization else None
            ),
        )

    def encode(
        self, texts: Sequence[str], batch_size: int = 32
    ) -> list[tuple[float, ...]]:
        """Encode a batch through the model's own batching (Step 21)."""
        if not texts:
            return []

        model = self._load()
        self.encode_calls += 1

        vectors = model.encode(
            list(texts),
            batch_size=batch_size,
            show_progress_bar=False,
            normalize_embeddings=self._normalize,
            convert_to_numpy=True,
        )

        encoded = [tuple(float(value) for value in vector) for vector in vectors]

        expected = self.dimension
        for vector in encoded:
            if len(vector) != expected:
                raise EmbeddingDimensionError(
                    f"model {self._model_id!r} returned a {len(vector)}-"
                    f"dimensional vector where {expected} was declared",
                    expected=expected,
                    actual=len(vector),
                )

        return encoded


class DeterministicTestModel:
    """A reproducible model that needs no download and no network.

    Not a mock of the real model's SEMANTICS - it makes no claim about
    similarity. It exists so the service, batching, skip logic, counters and
    vector plumbing can be tested exhaustively and fast, while the real model is
    exercised separately where semantics actually matter.
    """

    def __init__(self, model_id: str = "deterministic-test", dimension: int = 16) -> None:
        self._model_id = model_id
        self._dimension = dimension
        self.load_count = 1
        self.encode_calls = 0
        self.encoded_batch_sizes: list[int] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def fingerprint(self) -> ModelFingerprint:
        return ModelFingerprint(
            model_id=self._model_id,
            dimension=self._dimension,
            library_version=None,
            normalizes_output=False,
        )

    def encode(
        self, texts: Sequence[str], batch_size: int = 32
    ) -> list[tuple[float, ...]]:
        import hashlib

        self.encode_calls += 1
        self.encoded_batch_sizes.append(len(texts))

        vectors: list[tuple[float, ...]] = []

        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            vectors.append(
                tuple(
                    int(digest[index * 2 : index * 2 + 2], 16) / 255.0
                    for index in range(self._dimension)
                )
            )

        return vectors


def cosine_similarity(
    left: Sequence[float], right: Sequence[float]
) -> float:
    """Cosine similarity, normalizing here rather than assuming it (Step 52).

    MEASURED BEHAVIOUR: ``all-MiniLM-L6-v2`` already returns L2-normalized
    vectors - its sentence-transformers pipeline ends in a ``Normalize``
    module, so the output norm is ~1.0 even with ``normalize_embeddings=False``.
    Dividing by the norms here is therefore a no-op for that model.

    It is kept anyway, deliberately. The division costs nothing measurable, it
    makes this function correct for models that do NOT normalize, and it means
    the definition of "cosine" lives in one visible place rather than depending
    on a property of whichever model happens to be plugged in. What is avoided
    is asking the MODEL to normalize a second time, which is why
    ``SentenceTransformerModel.normalize`` defaults to False.
    """
    if len(left) != len(right):
        raise EmbeddingDimensionError(
            f"cannot compare a {len(left)}-dimensional vector with a "
            f"{len(right)}-dimensional one",
            expected=len(left),
            actual=len(right),
        )

    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5

    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0

    return dot / (left_norm * right_norm)


__all__ = [
    "DEFAULT_MODEL_ID",
    "EmbeddingModel",
    "ModelFingerprint",
    "SentenceTransformerModel",
    "DeterministicTestModel",
    "cosine_similarity",
]
