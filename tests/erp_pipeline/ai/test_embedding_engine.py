"""The embedding model abstraction, service, batching and skip policy.

Steps 15-28, 48-57. The load-bearing tests here are the SKIP decisions: getting
them wrong in one direction re-embeds the corpus every run, and in the other
leaves the index permanently stale.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from erp_pipeline.ai import (
    DEFAULT_MODEL_ID,
    AIRepresentation,
    DeterministicTestModel,
    EmbeddingFailurePolicy,
    EmbeddingModel,
    EmbeddingOptions,
    EmbeddingRecord,
    EmbeddingService,
    EmbeddingStatus,
    ModelFingerprint,
    SentenceTransformerModel,
    available_models,
    canonical_record_to_representation,
    cosine_similarity,
    create_model,
    make_embedding_id,
)
from erp_pipeline.ai.errors import (
    AIConfigurationError,
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingModelUnavailableError,
)

from tests.erp_pipeline.ai.conftest import (
    SECRET_CUSTOMER,
    make_record,
    requires_real_model,
)


def representation(key: str = "r1", text: str = "Invoice INV-001 amount 2500.50"):
    return AIRepresentation(
        representation_id=key,
        entity_type="invoice",
        text_for_ai=text,
        content={"invoice_id": "INV-001"},
    )


# ============================================================
# Model abstraction (Step 15)
# ============================================================

def test_the_test_model_satisfies_the_protocol(test_model):
    assert isinstance(test_model, EmbeddingModel)


def test_the_real_model_satisfies_the_protocol():
    assert isinstance(SentenceTransformerModel(), EmbeddingModel)


def test_only_the_model_module_imports_sentence_transformers():
    """Step 15: the rest of the pipeline speaks to the abstraction."""
    import ast
    from pathlib import Path

    offenders = []

    for path in Path("src/erp_pipeline/ai").rglob("*.py"):
        if path.name in ("embedding.py", "model_registry.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] == "sentence_transformers" for n in names):
                offenders.append(path.name)

    assert offenders == []


def test_the_registry_lists_local_models_only():
    assert "minilm" in available_models()
    assert "deterministic-test" in available_models()


def test_a_model_can_be_created_by_name():
    model = create_model("deterministic-test")

    assert isinstance(model, EmbeddingModel)


def test_an_unknown_model_name_is_refused():
    with pytest.raises(EmbeddingModelUnavailableError):
        create_model("gpt-embedding-3-large")


# ============================================================
# The real model (Steps 16, 17, 18)
# ============================================================

@requires_real_model
def test_the_real_model_reports_its_measured_dimension():
    model = SentenceTransformerModel()

    assert model.dimension == 384


@requires_real_model
def test_the_real_model_is_the_one_bpi_already_uses():
    assert DEFAULT_MODEL_ID == "sentence-transformers/all-MiniLM-L6-v2"


@requires_real_model
def test_the_real_model_loads_once_not_per_record():
    """Step 17: loading a transformer per record would dominate any run."""
    model = SentenceTransformerModel()
    service = EmbeddingService(model)

    for index in range(5):
        service.embed_one(representation(f"r{index}"))

    assert model.load_count == 1


@requires_real_model
def test_the_real_model_produces_vectors_of_the_declared_dimension():
    model = SentenceTransformerModel()

    vectors = model.encode(["one", "two"])

    assert len(vectors) == 2
    assert all(len(vector) == model.dimension for vector in vectors)


@requires_real_model
def test_the_real_model_is_deterministic_within_tolerance():
    """Step 51: numerically equivalent, not necessarily byte-identical."""
    model = SentenceTransformerModel()

    first = model.encode(["ERP invoice for customer C001"])[0]
    second = model.encode(["ERP invoice for customer C001"])[0]

    assert all(abs(a - b) < 1e-6 for a, b in zip(first, second))


@requires_real_model
def test_the_real_model_already_returns_normalized_vectors():
    """Step 52: MEASURED, and it contradicts the flag name.

    ``normalize_embeddings`` defaults to False, yet the norm is ~1.0 - the
    model's own pipeline ends in a Normalize module. Asserting the opposite
    would have been an assumption that happened to be wrong.
    """
    model = SentenceTransformerModel()

    vector = model.encode(["ERP invoice"])[0]
    norm = sum(value * value for value in vector) ** 0.5

    assert abs(norm - 1.0) < 1e-3
    assert model.output_is_normalized() is True
    assert model.fingerprint().normalizes_output is True


@requires_real_model
def test_cosine_is_not_applied_twice_by_the_model():
    """We never ask the model to normalize again on top of its own pipeline."""
    model = SentenceTransformerModel()

    assert model._normalize is False


@requires_real_model
def test_the_model_fingerprint_records_what_is_known():
    fingerprint = SentenceTransformerModel().fingerprint()

    assert fingerprint.model_id == DEFAULT_MODEL_ID
    assert fingerprint.dimension == 384
    assert isinstance(fingerprint.to_dict(), dict)


def test_a_fingerprint_does_not_invent_a_revision():
    """Step 49: an unknown revision is reported as unknown."""
    fingerprint = ModelFingerprint(model_id="x", dimension=8)

    assert fingerprint.library_version is None


# ============================================================
# Embedding identity (Step 20)
# ============================================================

def test_embedding_identity_is_deterministic():
    assert make_embedding_id("ai:invoice:1", "m") == make_embedding_id(
        "ai:invoice:1", "m"
    )


def test_embedding_identity_changes_with_the_model():
    assert make_embedding_id("ai:invoice:1", "m1") != make_embedding_id(
        "ai:invoice:1", "m2"
    )


def test_embedding_identity_is_not_content_derived(service):
    """Changed content must UPDATE the embedding, not mint a new one."""
    first = service.embed_one(representation("r1", "text one"))
    second = service.embed_one(representation("r1", "text two"))

    assert first.embedding_id == second.embedding_id
    assert first.content_hash != second.content_hash


# ============================================================
# Batching (Steps 21, 22, 23)
# ============================================================

def test_a_batch_is_encoded_in_one_call(test_model):
    service = EmbeddingService(test_model, EmbeddingOptions(batch_size=64))

    service.embed_many(representation(f"r{i}", f"text {i}") for i in range(10))

    assert test_model.encode_calls == 1
    assert test_model.encoded_batch_sizes == [10]


def test_the_batch_size_bounds_each_encode_call(test_model):
    service = EmbeddingService(test_model, EmbeddingOptions(batch_size=4))

    service.embed_many(representation(f"r{i}", f"text {i}") for i in range(10))

    assert test_model.encoded_batch_sizes == [4, 4, 2]


def test_a_generator_is_accepted_without_materializing_it(test_model):
    pulled = []

    def stream():
        for index in range(6):
            pulled.append(index)
            yield representation(f"r{index}", f"text {index}")

    service = EmbeddingService(test_model, EmbeddingOptions(batch_size=2))
    summary = service.embed_many(stream())

    assert summary.representations_read == 6
    assert len(pulled) == 6


def test_a_zero_batch_size_is_refused():
    with pytest.raises(AIConfigurationError):
        EmbeddingOptions(batch_size=0)


# ============================================================
# Skip and re-embed policy (Steps 24, 50)
# ============================================================

def test_unchanged_content_and_model_is_skipped(service):
    item = representation("r1")
    first = service.embed_one(item)

    second = service.embed_one(item, previous=first)

    assert first.status is EmbeddingStatus.GENERATED
    assert second.status is EmbeddingStatus.SKIPPED_UNCHANGED
    assert second.vector is None


def test_changed_content_re_embeds(service):
    first = service.embed_one(representation("r1", "original"))

    second = service.embed_one(representation("r1", "amended"), previous=first)

    assert second.status is EmbeddingStatus.GENERATED
    assert second.content_hash != first.content_hash


def test_a_changed_model_re_embeds_identical_content():
    """Step 50: a vector from another model is not comparable."""
    item = representation("r1")
    old = EmbeddingService(DeterministicTestModel("model-a")).embed_one(item)

    service = EmbeddingService(DeterministicTestModel("model-b"))
    fresh = service.embed_one(item, previous=old)

    assert old.content_hash == fresh.content_hash
    assert fresh.status is EmbeddingStatus.GENERATED


def test_force_overrides_the_skip(service):
    item = representation("r1")
    first = service.embed_one(item)

    forced = EmbeddingService(
        service.model, EmbeddingOptions(force=True)
    ).embed_one(item, previous=first)

    assert forced.status is EmbeddingStatus.GENERATED


def test_is_current_accepts_a_plain_mapping(service):
    item = representation("r1")

    assert service.is_current(
        item,
        {"content_hash": item.resolved_hash(), "model_id": service.model_id},
    )


def test_is_current_rejects_an_unknown_previous(service):
    assert not service.is_current(representation("r1"), None)


def test_the_skip_decision_matches_phase_10s_hash(service):
    """Phase 9/10/11 must agree about what "unchanged" means."""
    from erp_pipeline.sync.hashing import representation_content_hash

    record = make_record()
    projected = canonical_record_to_representation(record)

    assert projected.resolved_hash() == representation_content_hash(
        projected.representation_id,
        text_for_ai=projected.text_for_ai,
        content=projected.content,
    )


# ============================================================
# Empty content and failures (Steps 26, 27)
# ============================================================

def test_empty_content_is_reported_not_embedded(service):
    record = service.embed_one(representation("r1", ""))

    assert record.status is EmbeddingStatus.EMPTY_CONTENT
    assert record.vector is None
    assert record.reason


def test_whitespace_only_content_is_reported(service):
    assert service.embed_one(
        representation("r1", "   \n  ")
    ).status is EmbeddingStatus.EMPTY_CONTENT


def test_empty_content_does_not_stop_the_batch(test_model):
    service = EmbeddingService(test_model)

    summary = service.embed_many(
        [representation("r1", "good"), representation("r2", ""),
         representation("r3", "also good")]
    )

    assert summary.embeddings_generated == 2
    assert summary.embeddings_empty == 1


class _ExplodingModel:
    model_id = "exploding"
    dimension = 8

    def encode(self, texts, batch_size=32):
        raise RuntimeError("synthetic model failure")


def test_a_model_failure_is_recorded_under_continue():
    service = EmbeddingService(_ExplodingModel())

    summary = service.embed_many([representation("r1"), representation("r2")])

    assert summary.embeddings_failed == 2
    assert summary.embeddings_generated == 0


def test_a_model_failure_raises_under_fail_fast():
    service = EmbeddingService(
        _ExplodingModel(),
        EmbeddingOptions(failure_policy=EmbeddingFailurePolicy.FAIL_FAST),
    )

    with pytest.raises(EmbeddingError):
        service.embed_many([representation("r1")])


class _WrongDimensionModel:
    model_id = "wrong-dim"
    dimension = 8

    def encode(self, texts, batch_size=32):
        return [tuple([0.1] * 4) for _ in texts]


def test_a_dimension_mismatch_is_caught_by_the_service():
    service = EmbeddingService(_WrongDimensionModel())

    summary = service.embed_many([representation("r1")])

    assert summary.embeddings_failed == 1
    assert "dimension" in (summary.records[0].reason or "").lower()


def test_a_dimension_mismatch_raises_under_fail_fast():
    service = EmbeddingService(
        _WrongDimensionModel(),
        EmbeddingOptions(failure_policy=EmbeddingFailurePolicy.FAIL_FAST),
    )

    with pytest.raises(EmbeddingDimensionError):
        service.embed_many([representation("r1")])


def test_a_failure_reason_carries_no_business_content():
    service = EmbeddingService(_ExplodingModel())

    summary = service.embed_many(
        [representation("r1", f"Customer {SECRET_CUSTOMER}")]
    )

    assert SECRET_CUSTOMER not in (summary.records[0].reason or "")


# ============================================================
# Counters (Steps 48, 57)
# ============================================================

def test_the_counter_invariant_holds(test_model):
    service = EmbeddingService(test_model)
    first = service.embed_one(representation("r1"))

    summary = service.embed_many(
        [representation("r1"), representation("r2"), representation("r3", "")],
        previous={"r1": first},
    )

    assert summary.representations_read == 3
    assert summary.embeddings_generated == 1
    assert summary.embeddings_skipped == 1
    assert summary.embeddings_empty == 1
    assert summary.counters_balance


def test_the_summary_reports_the_required_metrics(test_model):
    summary = EmbeddingService(test_model).embed_many(
        [representation(f"r{i}") for i in range(3)]
    )

    payload = summary.to_dict()

    for key in (
        "model_id", "dimension", "batch_size", "representations_read",
        "embeddings_generated", "embeddings_skipped", "embeddings_failed",
        "duration_seconds", "throughput_per_second", "average_latency_ms",
        "counters_balance",
    ):
        assert key in payload


def test_an_empty_batch_is_safe(test_model):
    summary = EmbeddingService(test_model).embed_many([])

    assert summary.representations_read == 0
    assert summary.throughput_per_second == 0.0
    assert summary.average_latency_ms == 0.0
    assert summary.counters_balance


# ============================================================
# Privacy (Step 54)
# ============================================================

def test_a_record_repr_carries_no_vector_or_text(service):
    record = service.embed_one(representation("r1", SECRET_CUSTOMER))

    assert SECRET_CUSTOMER not in repr(record)
    assert "vector" not in repr(record).lower() or "has_vector" in repr(record)


def test_a_record_serialization_omits_the_vector_by_default(service):
    record = service.embed_one(representation("r1"))

    assert "vector" not in record.to_dict()
    assert record.to_dict()["has_vector"] is True
    assert "vector" in record.to_dict(include_vector=True)


def test_a_run_summary_carries_no_text_or_vector(service):
    import json

    summary = service.embed_many([representation("r1", SECRET_CUSTOMER)])
    payload = json.dumps(summary.to_dict(), default=str)

    assert SECRET_CUSTOMER not in payload
    # Counter KEYS legitimately mention vectors; actual float payloads must not
    # appear, so look for the bracketed list a serialized vector would produce.
    assert '"vector"' not in payload
    assert "[0." not in payload


def test_nothing_is_logged_during_embedding(service, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        service.embed_many([representation("r1", SECRET_CUSTOMER)])

    assert caplog.records == []


# ============================================================
# No external AI API (Step 55)
# ============================================================

def test_the_package_imports_no_remote_ai_client():
    import ast
    from pathlib import Path

    forbidden = {
        "openai", "anthropic", "cohere", "google", "mistralai", "ollama",
        "requests", "httpx", "aiohttp", "urllib",
    }
    modules: set[str] = set()

    for path in Path("src/erp_pipeline/ai").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])

    assert modules & forbidden == set()


def test_no_remote_inference_vocabulary_exists():
    from pathlib import Path

    text = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in Path("src/erp_pipeline/ai").rglob("*.py")
    )

    for marker in ("api_key", "api.openai", "inference-api", "bearer "):
        assert marker not in text, marker


# ============================================================
# Cosine (Step 52)
# ============================================================

def test_cosine_of_identical_vectors_is_one():
    assert abs(cosine_similarity((1.0, 2.0, 3.0), (1.0, 2.0, 3.0)) - 1.0) < 1e-9


def test_cosine_of_orthogonal_vectors_is_zero():
    assert abs(cosine_similarity((1.0, 0.0), (0.0, 1.0))) < 1e-9


def test_cosine_normalizes_unnormalized_input():
    assert abs(cosine_similarity((2.0, 0.0), (5.0, 0.0)) - 1.0) < 1e-9


def test_cosine_refuses_mismatched_dimensions():
    with pytest.raises(EmbeddingDimensionError):
        cosine_similarity((1.0, 2.0), (1.0, 2.0, 3.0))


def test_a_zero_vector_gives_zero_not_a_crash():
    assert cosine_similarity((0.0, 0.0), (1.0, 1.0)) == 0.0
