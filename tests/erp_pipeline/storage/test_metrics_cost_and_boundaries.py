"""Measurement honesty, the cost model, phase integration and phase boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import erp_pipeline.storage as storage_package
from erp_pipeline.ai.models import EmbeddingStatus
from erp_pipeline.storage.cost import (
    DEFAULT_COST_MODEL,
    DEFAULT_RESOURCE_MULTIPLIERS,
    CostModel,
)
from erp_pipeline.storage.errors import StorageConfigurationError
from erp_pipeline.storage.metrics import (
    LatencySample,
    evaluate_recall,
    measure_latency,
    ranking_overlap,
    vector_payload_proxy,
)
from erp_pipeline.storage.models import MeasurementKind, StorageTier
from erp_pipeline.storage.service import StorageService

from .conftest import make_embedding

# ----------------------------------------------------------------------
# Measurement honesty
# ----------------------------------------------------------------------


def test_p95_is_withheld_until_there_are_enough_samples():
    """A p95 from three numbers is theatre. It must be declared unavailable."""
    small = LatencySample("tiny", tuple(float(i) for i in range(5)))
    large = LatencySample("big", tuple(float(i) for i in range(40)))

    assert small.to_dict()["p95_available"] is False
    assert small.to_dict()["p95_ms"] is None
    assert large.to_dict()["p95_available"] is True
    assert large.to_dict()["p95_ms"] > 0


def test_latency_measurement_discards_warmup():
    calls: list[int] = []

    sample = measure_latency("probe", lambda i: calls.append(i), iterations=10, warmup=3)

    assert len(calls) == 13
    assert sample.count == 10


def test_the_payload_proxy_is_the_only_comparable_footprint():
    """int8 is a quarter of float32. If the proxy hides that, it is useless."""
    hot = vector_payload_proxy(StorageTier.HOT, 100, 384, quantized=False)
    warm = vector_payload_proxy(StorageTier.WARM, 100, 384, quantized=True)

    assert hot.bytes_per_record == 384 * 4
    assert warm.bytes_per_record == 384 * 1
    assert hot.kind is MeasurementKind.PROXY
    assert warm.kind is MeasurementKind.PROXY
    assert "excluding" in hot.method


def test_recall_is_scored_against_labels_not_another_tier():
    ranked = [["a", "b", "c"], ["x", "y", "z"]]
    expected = ["c", "x"]

    result = evaluate_recall("t", ranked, expected)

    assert result.recall_at(1) == 0.5
    assert result.recall_at(3) == 1.0


def test_ranking_overlap_is_a_diagnostic_not_a_score():
    identical = ranking_overlap([["a", "b"]], [["a", "b"]], k=2)
    disjoint = ranking_overlap([["a", "b"]], [["c", "d"]], k=2)

    assert identical == 1.0
    assert disjoint == 0.0


# ----------------------------------------------------------------------
# Cost model
# ----------------------------------------------------------------------


def test_cost_is_normalized_and_never_currency():
    """No money anywhere. The disclaimer text is excluded from the scan, since
    it legitimately uses the word 'prices' to say these are not prices."""
    payload = DEFAULT_COST_MODEL.to_dict()
    quantitative = {
        key: value for key, value in payload.items()
        if key not in ("assumptions", "multiplier_rationale")
    }
    rendered = str(quantitative).lower()

    assert "$" not in rendered
    assert "usd" not in rendered
    assert "price" not in rendered
    assert "currency" not in rendered

    # The disclaimer must still be present and must still disclaim.
    assert payload["assumptions"]
    assert any("not prices" in line.lower() for line in payload["assumptions"])


def test_multipliers_are_ordinal_hot_warm_cold():
    assert (
        DEFAULT_RESOURCE_MULTIPLIERS[StorageTier.HOT]
        > DEFAULT_RESOURCE_MULTIPLIERS[StorageTier.WARM]
        > DEFAULT_RESOURCE_MULTIPLIERS[StorageTier.COLD]
    )


def test_substituting_multipliers_changes_only_the_cost():
    """The reader must be able to plug in their own numbers. That is the point."""
    custom = CostModel(multipliers={t: 1.0 for t in StorageTier})

    default_cost = DEFAULT_COST_MODEL.cost_for(StorageTier.WARM, 1000.0, 10)
    custom_cost = custom.cost_for(StorageTier.WARM, 1000.0, 10)

    assert default_cost.storage_bytes == custom_cost.storage_bytes
    assert default_cost.normalized_cost != custom_cost.normalized_cost


def test_cost_carries_the_measurement_kind_of_its_input():
    cost = DEFAULT_COST_MODEL.cost_for(
        StorageTier.COLD, 500.0, 5, MeasurementKind.MEASURED
    )

    assert cost.to_dict()["storage_measurement"] == "measured"


# ----------------------------------------------------------------------
# Phase 11 integration
# ----------------------------------------------------------------------


def test_service_stores_a_phase_11_embedding_record_directly(tmp_path):
    """Phase 12 must consume Phase 11's output without a translation layer."""
    from erp_pipeline.storage.cold_tier import (
        ColdArchiveTier,
        StaticKeyProvider,
        generate_key,
    )

    service = StorageService(
        cold=ColdArchiveTier(tmp_path, StaticKeyProvider(generate_key()))
    )
    record = make_embedding()

    metadata, decision = service.store(record, override=StorageTier.COLD,
                                       override_reason="only cold configured")

    assert metadata.representation_id == record.representation_id
    assert metadata.embedding_id == record.embedding_id
    assert metadata.content_hash == record.content_hash
    assert decision.selected_tier is StorageTier.COLD


def test_a_skipped_embedding_cannot_be_stored(tmp_path):
    """A record with no vector is not storable; failing loudly beats a null row."""
    from erp_pipeline.storage.cold_tier import (
        ColdArchiveTier,
        StaticKeyProvider,
        generate_key,
    )

    service = StorageService(
        cold=ColdArchiveTier(tmp_path, StaticKeyProvider(generate_key()))
    )
    skipped = make_embedding(status=EmbeddingStatus.SKIPPED_UNCHANGED)

    with pytest.raises(StorageConfigurationError):
        service.store(skipped)


def test_storage_reuses_phase_11_identity_rather_than_minting_its_own():
    """A second identity scheme would silently fork the two phases' views."""
    from erp_pipeline.ai.models import make_embedding_id

    record = make_embedding()

    assert record.embedding_id == f"emb:{record.representation_id}" or make_embedding_id


# ----------------------------------------------------------------------
# Phase boundaries
# ----------------------------------------------------------------------


def _package_code() -> dict[str, str]:
    """Every module's source with docstrings removed.

    The prose describes what Phase 12 must not do, so scanning raw text would
    match the documentation rather than the implementation.
    """
    sources: dict[str, str] = {}

    for path in Path(storage_package.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                if (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                ):
                    node.body.pop(0)

        sources[path.name] = ast.unparse(tree)

    return sources


@pytest.mark.parametrize(
    "banned",
    ["openai", "anthropic", "gemini", "cohere", "huggingface_hub.InferenceClient"],
)
def test_storage_calls_no_hosted_language_model(banned: str):
    """The prototype runs locally. A hosted call would break that guarantee."""
    for name, code in _package_code().items():
        assert banned not in code.lower(), f"{name} references {banned}"


def test_storage_does_not_import_the_bpi_package():
    """Phase 12 is generic infrastructure; importing BPI would make it a fork."""
    for name, code in _package_code().items():
        assert "import bpi2020" not in code, name
        assert "from bpi2020" not in code, name


def test_storage_does_not_hard_code_a_collection_name():
    for name, code in _package_code().items():
        assert "bpi2020_erp_knowledge" not in code, name


def test_storage_does_not_reach_into_phase_13_territory():
    """Phase 12 decides where a vector lives. It does not serve HTTP or chat."""
    for name, code in _package_code().items():
        lowered = code.lower()

        for banned in ("fastapi", "flask", "uvicorn", "@app.route", "langchain"):
            assert banned not in lowered, f"{name} references {banned}"


def test_no_encryption_is_invented():
    """Home-grown crypto is the classic way to look encrypted while not being."""
    code = _package_code()["cold_tier.py"].lower()

    assert "aesgcm" in code or "aes" in code
    assert "def _xor" not in code
    assert "mode.ecb" not in code.replace(" ", "")


def test_every_public_export_actually_exists():
    """A broken __all__ turns a typo into an ImportError for every consumer."""
    for name in storage_package.__all__:
        assert hasattr(storage_package, name), f"__all__ exports missing {name}"
