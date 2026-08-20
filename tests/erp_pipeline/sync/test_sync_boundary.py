"""Boundary, privacy and determinism for the generic sync core.

Steps 71-74, 76. The generic engine must be independent of the frozen
prototype, of any one vector database, and of the network - and it must never
put a business value into a report.
"""

from __future__ import annotations

import ast
import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from erp_pipeline.sync import (
    AIRepresentation,
    SyncOptions,
    vector_id_for,
)

from tests.erp_pipeline.sync.conftest import (
    BASE_TIME,
    SECRET_ACCOUNT,
    SECRET_CUSTOMER,
    SECRET_EMAIL,
    Harness,
    invoice_row,
)

PACKAGE = Path("src/erp_pipeline/sync")
SOURCES = sorted(PACKAGE.rglob("*.py"))
ALL_SECRETS = (SECRET_CUSTOMER, SECRET_ACCOUNT, SECRET_EMAIL)


def _code_text() -> str:
    """Package code with docstrings and comments stripped.

    A vocabulary ban must be checked against what the code DOES. This package's
    own docstrings say "no bpi2020" and "no Qdrant", and a naive substring scan
    would flag the very sentences documenting the guarantee.
    """
    parts: list[str] = []

    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())

        parts.append(ast.unparse(ast.fix_missing_locations(tree)))

    return "\n".join(parts)


def _imported_modules() -> set[str]:
    modules: set[str] = set()

    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
                modules.add(node.module)

    return modules


# ============================================================
# No BPI dependency (Step 76)
# ============================================================

def test_the_generic_core_never_imports_bpi2020():
    for module in _imported_modules():
        assert not module.startswith("bpi2020")
        assert not module.startswith("src.bpi2020")


def test_no_sync_module_mentions_the_prototype_package():
    assert "bpi2020" not in _code_text()


# ============================================================
# No vector-database coupling (Step 24)
# ============================================================

def test_the_generic_core_imports_no_vector_database():
    forbidden = {"qdrant_client", "chromadb", "pinecone", "weaviate", "faiss"}

    assert _imported_modules() & forbidden == set()


def test_no_vector_database_vocabulary_appears_in_the_code():
    text = _code_text().lower()

    for marker in ("qdrant", "pointstruct", "collection_name", "vectorparams"):
        assert marker not in text, marker


# ============================================================
# No network, no LLM (Steps 72, 73)
# ============================================================

FORBIDDEN_MODULES = {
    "requests", "httpx", "aiohttp", "urllib", "urllib3", "socket",
    "openai", "anthropic", "cohere", "mistralai", "ollama",
    "transformers", "sentence_transformers", "torch",
}


def test_the_generic_core_imports_no_network_or_llm_module():
    assert _imported_modules() & FORBIDDEN_MODULES == set()


def test_no_endpoint_execution_vocabulary_exists():
    """Step 72: Phase 10 monitors specifications, it does not call them."""
    text = _code_text().lower()

    for marker in ("requests.get", "requests.post", ".json()", "soap", "wsdl"):
        assert marker not in text, marker


def test_no_llm_client_is_constructed():
    text = _code_text().lower()

    for marker in ("openai", "anthropic", "gpt-", "claude-", "completion("):
        assert marker not in text, marker


def test_drift_classification_uses_no_model():
    """Step 73: schema drift is deterministic, not inferred."""
    text = Path("src/erp_pipeline/sync/drift.py").read_text(encoding="utf-8").lower()

    for marker in ("embedding", "predict", "model.", "llm"):
        assert marker not in text, marker


# ============================================================
# Privacy (Step 71)
# ============================================================

def _secret_harness() -> Harness:
    rows = [
        invoice_row(
            index,
            customer_id=SECRET_CUSTOMER,
            amount="100.00",
            account=SECRET_ACCOUNT,
            email=SECRET_EMAIL,
        )
        for index in range(1, 6)
    ]
    harness = Harness(rows=rows)
    harness.catch_up()
    harness.reset_counters()
    return harness


def test_legitimate_values_do_reach_the_canonical_record():
    harness = _secret_harness()

    record = harness.canonical.get("erp:erp_pg:invoice:inv-001")

    assert record.normalized_data["customer_id"] == SECRET_CUSTOMER


def test_no_secret_reaches_the_run_summary():
    harness = _secret_harness()
    harness.source.add(
        invoice_row(6, customer_id=SECRET_CUSTOMER, offset_seconds=500)
    )

    payload = json.dumps(harness.run().to_dict(), default=str)

    for secret in ALL_SECRETS:
        assert secret not in payload


def test_no_secret_reaches_a_quarantine_report():
    harness = _secret_harness()
    harness.source.add(
        invoice_row(
            6, customer_id=SECRET_CUSTOMER, amount="hello", offset_seconds=500
        )
    )

    summary = harness.run(
        SyncOptions(batch_size=10, failure_policy="quarantine")
    )
    payload = json.dumps([q.to_dict() for q in summary.quarantined], default=str)

    for secret in ALL_SECRETS:
        assert secret not in payload


def test_no_secret_reaches_the_persisted_sync_state():
    harness = _secret_harness()

    payload = json.dumps(harness.state.to_dict(), default=str)

    for secret in ALL_SECRETS:
        assert secret not in payload


def test_a_source_change_does_not_serialize_its_payload():
    """The raw record is in memory for Phase 9, never in a report."""
    harness = _secret_harness()
    harness.source.add(
        invoice_row(6, customer_id=SECRET_CUSTOMER, offset_seconds=500)
    )

    summary = harness.run()
    payload = json.dumps(summary.results[0].change.to_dict(), default=str)

    assert SECRET_CUSTOMER not in payload
    assert summary.results[0].change.payload is not None


def test_nothing_is_logged_during_a_sync(caplog):
    harness = _secret_harness()
    harness.source.add(
        invoice_row(6, customer_id=SECRET_CUSTOMER, offset_seconds=500)
    )

    with caplog.at_level(logging.DEBUG):
        harness.run()

    assert caplog.records == []


def test_a_watermark_description_carries_no_business_value():
    harness = _secret_harness()

    assert SECRET_CUSTOMER not in harness.state.watermark.describe()


def test_a_representation_serializes_without_its_content():
    representation = AIRepresentation(
        representation_id="r1",
        entity_type="invoice",
        text_for_ai=SECRET_EMAIL,
        content={"customer_id": SECRET_CUSTOMER},
    )

    payload = json.dumps(representation.to_dict(), default=str)

    for secret in ALL_SECRETS:
        assert secret not in payload


# ============================================================
# Determinism (Step 74)
# ============================================================

def test_the_same_change_produces_the_same_canonical_identity():
    first = Harness(rows=[invoice_row(1)])
    second = Harness(rows=[invoice_row(1)])

    first.catch_up()
    second.catch_up()

    assert first.canonical.record_ids == second.canonical.record_ids


def test_the_same_content_produces_the_same_hash():
    first = Harness(rows=[invoice_row(1)])
    second = Harness(rows=[invoice_row(1)])

    first.catch_up()
    second.catch_up()

    key = first.canonical.record_ids[0]

    assert (
        first.builder.rebuild(key).resolved_hash()
        == second.builder.rebuild(key).resolved_hash()
    )


def test_vector_identity_is_deterministic_and_not_random():
    assert vector_id_for("erp:erp_pg:invoice:inv-001") == vector_id_for(
        "erp:erp_pg:invoice:inv-001"
    )


def test_vector_identity_differs_per_representation():
    assert vector_id_for("a") != vector_id_for("b")


def test_the_engine_uses_no_randomness():
    modules = _imported_modules()

    assert "random" not in modules
    assert "secrets" not in modules
    assert "uuid" not in modules


def test_a_repeated_run_produces_identical_counters():
    def run_once():
        harness = Harness(rows=[invoice_row(i) for i in range(1, 11)])
        harness.catch_up()
        harness.reset_counters()
        harness.source.add(invoice_row(11, offset_seconds=500))
        return harness.run().to_dict()

    first = run_once()
    second = run_once()

    for key in first:
        if key in ("duration_seconds",):
            continue
        assert first[key] == second[key], key


# ============================================================
# No database writes outside the sync state store (Step 64 of Phase 9,
# carried forward)
# ============================================================

def test_the_coordinator_writes_to_no_store_it_was_not_given():
    """Every downstream write goes through an injected interface."""
    text = Path("src/erp_pipeline/sync/coordinator.py").read_text(encoding="utf-8")

    for marker in ("create_engine", "psycopg", "sqlalchemy"):
        assert marker not in text


def test_only_the_state_store_touches_sql():
    """Sync state is the only thing this package persists itself."""
    offenders = []

    for path in SOURCES:
        if path.name in ("state.py", "extractor.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if "CREATE TABLE" in text or "INSERT INTO" in text:
            offenders.append(path.name)

    assert offenders == []
