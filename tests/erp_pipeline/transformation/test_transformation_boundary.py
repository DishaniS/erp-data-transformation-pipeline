"""Privacy, offline boundary, determinism and provenance.

Steps 61-67, 74-76. These are the tests that make the phase's safety claims
checkable rather than merely stated, and they are deliberately static where a
static proof is possible: an assertion about what the package IMPORTS cannot be
defeated by a code path a runtime test happens not to exercise.
"""

from __future__ import annotations

import ast
import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from erp_pipeline.schemas.canonical_models import CanonicalRecord
from erp_pipeline.schemas.enums import FieldDataType as T, SourceType
from erp_pipeline.schemas.run_models import DataQualityIssue, TransformationRun
from erp_pipeline.transformation import (
    IssueCode,
    SourceRecord,
    TransformationOptions,
    TransformationService,
    transform_record,
    transform_records,
)

from tests.erp_pipeline.transformation.conftest import (
    SECRET_ACCOUNT,
    SECRET_CUSTOMER,
    SECRET_EMAIL,
    customer_profile,
    invoice_profile,
    make_mapping,
    make_profile,
)

PACKAGE = Path("src/erp_pipeline/transformation")
SOURCES = sorted(PACKAGE.rglob("*.py"))
ALL_SECRETS = (SECRET_CUSTOMER, SECRET_ACCOUNT, SECRET_EMAIL)


def _module_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in SOURCES)


def _code_text() -> str:
    """The package's CODE with docstrings and comments removed.

    A vocabulary ban has to be checked against what the code DOES, not against
    prose. This module's own docstrings say things like "no embeddings" and
    "never imports bpi2020", and a naive substring scan would flag the very
    sentences documenting the guarantee - proving nothing either way.
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
    """Every module name imported anywhere in the package."""
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
# No BPI dependency (Step 62)
# ============================================================

def test_the_package_never_imports_bpi2020():
    for module in _imported_modules():
        assert not module.startswith("bpi2020")
        assert not module.startswith("src.bpi2020")


def test_no_source_file_mentions_the_bpi_package():
    assert "bpi2020" not in _code_text()


# ============================================================
# No network or AI (Steps 63, 65)
# ============================================================

FORBIDDEN_MODULES = {
    "requests", "httpx", "aiohttp", "urllib", "urllib3", "http", "socket",
    "ftplib", "smtplib", "telnetlib", "xmlrpc",
    "openai", "anthropic", "google", "cohere", "mistralai", "ollama",
    "transformers", "sentence_transformers", "torch", "tensorflow",
    "qdrant_client", "chromadb", "pinecone", "weaviate", "faiss",
    "sqlalchemy", "psycopg", "psycopg2", "pymongo", "pymysql", "pyodbc",
}


def test_the_package_imports_no_network_or_ai_module():
    offenders = _imported_modules() & FORBIDDEN_MODULES

    assert offenders == set()


def test_importing_the_package_loads_no_network_or_ai_module():
    """Static import lists can miss a lazy import inside a function."""
    import subprocess
    import sys

    code = (
        "import sys; import erp_pipeline.transformation as t; "
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        f"{sorted(FORBIDDEN_MODULES)!r}); print(bad)"
    )

    output = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path.cwd()),
        env={"PYTHONPATH": "src", "PATH": ""},
    )

    assert output.returncode == 0, output.stderr
    assert output.stdout.strip() == "[]"


def test_no_embedding_or_vector_vocabulary_exists():
    text = _code_text().lower()

    for marker in (
        "embedding", "embed(", "qdrant", "vector_store", "vectorstore",
        "cosine", "sentence-transformer", "faiss", "pinecone",
    ):
        assert marker not in text, marker


def test_no_llm_client_is_constructed():
    text = _code_text().lower()

    for marker in ("openai", "anthropic", "gpt-", "claude-", "completion("):
        assert marker not in text, marker


# ============================================================
# No database writes (Step 64)
# ============================================================

def test_the_package_performs_no_database_access():
    text = _code_text().lower()

    for marker in ("execute(", "commit()", "cursor(", "session(", "insert into"):
        assert marker not in text, marker


def test_the_package_opens_no_file_for_writing():
    """Transformation is in-memory; persistence is a later phase."""
    offenders: list[str] = []

    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "open":
                    offenders.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("write_text", "write_bytes", "mkdir"):
                    offenders.append(f"{path}:{node.lineno}")

    assert offenders == []


def test_a_transformation_returns_records_rather_than_storing_them(pg_context):
    summary = transform_records(
        [
            SourceRecord.from_mapping(
                {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}
            )
        ],
        invoice_profile(),
        pg_context,
    )

    assert isinstance(summary.successful_records[0], CanonicalRecord)


# ============================================================
# Privacy sentinels (Steps 61, 44)
# ============================================================

def _secret_profile():
    return invoice_profile(
        "secret.profile", "erp_a", "fin_invoice",
        fields=(
            ("inv_no", "invoice_id", T.STRING),
            ("cust_no", "customer_id", T.STRING),
            ("total_amt", "amount", T.DECIMAL),
        ),
    )


def _secret_record(amount: str = "2500.50") -> SourceRecord:
    return SourceRecord.from_mapping(
        {
            "inv_no": SECRET_ACCOUNT,
            "cust_no": SECRET_CUSTOMER,
            "total_amt": amount,
            "contact": SECRET_EMAIL,
        },
        ordinal=1,
    )


def test_legitimate_source_data_does_reach_the_canonical_record(pg_context):
    """The sentinels are business data; carrying them is the engine's job."""
    result = transform_record(_secret_record(), _secret_profile(), context=pg_context)

    assert result.record.normalized_data["customer_id"] == SECRET_CUSTOMER


def test_no_secret_reaches_a_quality_issue(pg_context):
    summary = transform_records(
        [_secret_record("hello")], _secret_profile(), pg_context
    )

    for issue in summary.issues:
        rendered = json.dumps(issue.to_json_dict(), default=str)
        for secret in ALL_SECRETS:
            assert secret not in rendered


def test_no_secret_reaches_a_rejection_report(pg_context):
    summary = transform_records(
        [_secret_record("hello")], _secret_profile(), pg_context
    )

    rendered = json.dumps(
        [item.to_dict() for item in summary.rejected_records], default=str
    )

    for secret in ALL_SECRETS:
        assert secret not in rendered


def test_no_secret_reaches_the_run_summary(pg_context):
    summary = transform_records(
        [_secret_record("hello")], _secret_profile(), pg_context
    )

    rendered = json.dumps(summary.to_dict(), default=str)

    for secret in ALL_SECRETS:
        assert secret not in rendered


def test_no_secret_reaches_the_transformation_run(pg_context):
    summary = transform_records(
        [_secret_record("hello")], _secret_profile(), pg_context
    )

    rendered = json.dumps(summary.run.to_json_dict(), default=str)

    for secret in ALL_SECRETS:
        assert secret not in rendered


def test_no_secret_reaches_a_quality_object_repr(pg_context):
    summary = transform_records(
        [_secret_record("hello")], _secret_profile(), pg_context
    )

    for issue in summary.issues:
        for secret in ALL_SECRETS:
            assert secret not in repr(issue)


def test_nothing_is_logged_during_transformation(pg_context, caplog):
    with caplog.at_level(logging.DEBUG):
        transform_records([_secret_record("hello")], _secret_profile(), pg_context)

    assert caplog.records == []


def test_the_rejection_report_keeps_the_record_in_memory_only(pg_context):
    """Available for remediation, never serialized by default (Step 34)."""
    summary = transform_records(
        [_secret_record("hello")], _secret_profile(), pg_context
    )
    rejected = summary.rejected_records[0]

    assert rejected.source_record is not None
    assert SECRET_CUSTOMER not in json.dumps(rejected.to_dict(), default=str)


def test_retaining_the_source_record_can_be_switched_off(pg_context):
    options = TransformationOptions(retain_source_on_rejection=False)

    summary = TransformationService(options=options).transform_records(
        [_secret_record("hello")], _secret_profile(), pg_context
    )

    assert summary.rejected_records[0].source_record is None


def test_value_diagnostics_are_off_by_default(pg_context):
    summary = transform_records(
        [_secret_record("hello")], _secret_profile(), pg_context
    )

    assert all(
        issue.original_value_summary is None for issue in summary.issues
    )


def test_even_opted_in_diagnostics_report_shape_not_content(pg_context):
    """The opt-in buys type and length; it can never leak the value."""
    options = TransformationOptions(include_value_diagnostics=True)

    summary = TransformationService(options=options).transform_records(
        [_secret_record("hello")], _secret_profile(), pg_context
    )

    summaries = [
        issue.original_value_summary
        for issue in summary.issues
        if issue.original_value_summary
    ]

    assert summaries
    for item in summaries:
        assert "redacted" in item
        for secret in ALL_SECRETS:
            assert secret not in item


def test_no_exception_message_carries_a_value():
    """Step 45: errors name fields and rules, never data."""
    from erp_pipeline.transformation import convert

    result = convert(SECRET_CUSTOMER, T.DECIMAL, TransformationOptions())

    assert not result.ok
    assert SECRET_CUSTOMER not in (result.reason or "")


def test_transformation_needs_no_values_it_was_not_given(pg_context):
    """Schema-shaped input only; nothing is fetched from anywhere."""
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert result.is_transformed


# ============================================================
# Determinism (Step 66)
# ============================================================

def _determinism_records():
    return [
        SourceRecord.from_mapping(
            {"inv_no": f"INV-{i}", "cust_no": "C001", "total_amt": "10.00"},
            ordinal=i,
        )
        for i in range(1, 4)
    ] + [
        SourceRecord.from_mapping(
            {"inv_no": "INV-9", "cust_no": "C001", "total_amt": "hello"},
            ordinal=9,
        )
    ]


def test_repeated_transformation_produces_identical_records(pg_context):
    first = transform_records(_determinism_records(), invoice_profile(), pg_context)
    second = transform_records(_determinism_records(), invoice_profile(), pg_context)

    assert [
        record.normalized_data for record in first.successful_records
    ] == [record.normalized_data for record in second.successful_records]


def test_repeated_transformation_produces_identical_identities(pg_context):
    first = transform_records(_determinism_records(), invoice_profile(), pg_context)
    second = transform_records(_determinism_records(), invoice_profile(), pg_context)

    assert [r.record_id for r in first.successful_records] == [
        r.record_id for r in second.successful_records
    ]


def test_repeated_transformation_produces_identical_content_hashes(pg_context):
    first = transform_records(_determinism_records(), invoice_profile(), pg_context)
    second = transform_records(_determinism_records(), invoice_profile(), pg_context)

    assert [r.content_hash for r in first.successful_records] == [
        r.content_hash for r in second.successful_records
    ]


def test_repeated_transformation_produces_identical_issue_codes(pg_context):
    first = transform_records(_determinism_records(), invoice_profile(), pg_context)
    second = transform_records(_determinism_records(), invoice_profile(), pg_context)

    assert first.issue_codes() == second.issue_codes()


def test_repeated_transformation_produces_identical_outcomes(pg_context):
    first = transform_records(_determinism_records(), invoice_profile(), pg_context)
    second = transform_records(_determinism_records(), invoice_profile(), pg_context)

    assert (
        first.records_transformed,
        first.records_failed,
        first.records_skipped,
    ) == (
        second.records_transformed,
        second.records_failed,
        second.records_skipped,
    )


def test_field_ordering_is_stable(pg_context):
    first = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )
    second = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert list(first.record.normalized_data) == list(second.record.normalized_data)


def test_only_operational_timings_differ_between_runs(pg_context):
    """Timestamps and duration may move; content may not."""
    first = transform_records(_determinism_records(), invoice_profile(), pg_context)
    second = transform_records(_determinism_records(), invoice_profile(), pg_context)

    assert first.run.run_id == second.run.run_id
    assert first.run.records_read == second.run.records_read


def test_the_engine_uses_no_randomness():
    modules = _imported_modules()

    assert "random" not in modules
    assert "secrets" not in modules
    assert "uuid" not in modules


def test_record_identity_is_derived_from_the_business_key(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "INV-001", "cust_no": "C001", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert result.record.record_id == "erp:erp_a:invoice:inv-001"


def test_record_identity_carries_no_timestamp(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "INV-001", "cust_no": "C001", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert "2026" not in result.record.record_id
    assert result.record.record_id.count(":") == 3


def test_a_record_without_a_business_key_is_reported(pg_context):
    """No identity is better than an invented one."""
    profile = make_profile(
        "keyless.profile",
        [make_mapping("cust_name", "name", T.STRING)],
        target_entity_type="customer",
    )

    result = transform_record(
        SourceRecord.from_mapping({"cust_name": "Acme"}),
        profile,
        context=pg_context,
    )

    assert not result.is_transformed


# ============================================================
# Traceability (Steps 46, 74, 75)
# ============================================================

def test_a_record_records_which_mapping_produced_it(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert result.record.metadata["mapping_id"] == invoice_profile().mapping_id


def test_a_record_records_the_engine_and_config_version(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert result.record.metadata["transformation_engine_version"]
    assert result.record.metadata["transformation_config"]
    assert result.record.metadata["canonical_model_identity"]


def test_a_record_stays_traceable_to_its_source(pg_context):
    record = SourceRecord.from_mapping(
        {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}, ordinal=42
    )

    result = transform_record(record, invoice_profile(), context=pg_context)

    assert result.record.source.source_system_id == "erp_a"
    assert result.record.source.source_entity == "fin_invoice"
    assert result.record.provenance.original_record_id == "42"


def test_audit_metadata_lists_rules_by_name_not_by_value(pg_context):
    from erp_pipeline.schemas.enums import TransformationOperation
    from erp_pipeline.schemas.mapping_models import TransformationRule

    profile = make_profile(
        "audit.profile",
        [
            make_mapping("inv_no", "invoice_id", T.STRING),
            make_mapping("cust_no", "customer_id", T.STRING),
            make_mapping(
                "total_amt",
                "amount",
                T.DECIMAL,
                transformations=(
                    TransformationRule(operation=TransformationOperation.TRIM),
                ),
            ),
        ],
        source_entity="fin_invoice",
        target_entity_type="invoice",
    )

    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": SECRET_CUSTOMER, "total_amt": " 1.00 "}
        ),
        profile,
        context=pg_context,
    )

    applied = result.record.metadata["rules_applied"]

    assert applied == ["amount:trim"]
    assert SECRET_CUSTOMER not in json.dumps(applied)


def test_no_before_and_after_values_are_stored_in_audit_metadata(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": SECRET_ACCOUNT, "cust_no": "C", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert SECRET_ACCOUNT not in json.dumps(
        dict(result.record.metadata), default=str
    )


# ============================================================
# Frozen contracts are reused, not replaced
# ============================================================

def test_the_output_is_the_frozen_canonical_record(pg_context):
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "1.00"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    assert type(result.record) is CanonicalRecord


def test_no_competing_record_model_is_exported():
    import erp_pipeline.transformation as package

    for name in package.__all__:
        assert "CanonicalRecord" not in name or name == "CanonicalRecord"
        assert not name.startswith("Universal")
        assert not name.startswith("Transformed")


def test_issues_are_the_frozen_contract(pg_context):
    summary = transform_records(
        [
            SourceRecord.from_mapping(
                {"inv_no": "I", "cust_no": "C", "total_amt": "hello"}
            )
        ],
        invoice_profile(),
        pg_context,
    )

    assert all(isinstance(issue, DataQualityIssue) for issue in summary.issues)


def test_a_canonical_record_serializes_with_decimal_precision(pg_context):
    """Money must survive serialization as a string, not a float."""
    result = transform_record(
        SourceRecord.from_mapping(
            {"inv_no": "I", "cust_no": "C", "total_amt": "2500.50"}
        ),
        invoice_profile(),
        context=pg_context,
    )

    payload = result.record.to_json_dict()

    assert payload["normalized_data"]["amount"] == "2500.50"
