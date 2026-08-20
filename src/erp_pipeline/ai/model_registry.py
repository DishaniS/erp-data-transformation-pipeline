"""A small registry of available embedding models.

Exists so a research run can select a model by NAME - in configuration, in a
test, on a command line - without the selecting code importing
``sentence_transformers``. That is the same reason the ``EmbeddingModel``
protocol exists, applied one level up.

Deliberately tiny. A plugin system with entry points would be more general and
would buy nothing here: there are two models, one real and one deterministic
stand-in, and adding a third is a one-line registration.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from erp_pipeline.ai.embedding import (
    DEFAULT_MODEL_ID,
    DeterministicTestModel,
    EmbeddingModel,
    SentenceTransformerModel,
)
from erp_pipeline.ai.errors import EmbeddingModelUnavailableError

#: Name -> factory. Local models only; nothing here reaches a network service.
_REGISTRY: dict[str, Callable[..., EmbeddingModel]] = {
    "minilm": lambda **kwargs: SentenceTransformerModel(
        kwargs.pop("model_id", DEFAULT_MODEL_ID), **kwargs
    ),
    DEFAULT_MODEL_ID: lambda **kwargs: SentenceTransformerModel(
        DEFAULT_MODEL_ID, **kwargs
    ),
    "deterministic-test": lambda **kwargs: DeterministicTestModel(**kwargs),
}


def available_models() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def register_model(name: str, factory: Callable[..., EmbeddingModel]) -> None:
    """Add a model factory. Overwriting an existing name is refused."""
    if name in _REGISTRY:
        raise EmbeddingModelUnavailableError(
            f"a model named {name!r} is already registered; pick another name "
            "rather than silently replacing it"
        )

    _REGISTRY[name] = factory


def create_model(name: str = "minilm", **kwargs: Any) -> EmbeddingModel:
    """Build a model by name."""
    factory = _REGISTRY.get(name)

    if factory is None:
        raise EmbeddingModelUnavailableError(
            f"unknown embedding model {name!r}. Registered: "
            f"{', '.join(available_models())}."
        )

    return factory(**kwargs)


__all__ = [
    "available_models",
    "register_model",
    "create_model",
]
