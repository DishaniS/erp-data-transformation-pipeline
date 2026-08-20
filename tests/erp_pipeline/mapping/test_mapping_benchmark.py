"""LABELLED MAPPING BENCHMARK (Steps 50, 51, 52).

The expected mappings below were written BY HAND, before running the engine
against them, from what each source field means in ERP terms. They are not
engine output relabelled as ground truth - that would measure only whether the
engine agrees with itself.

Where the engine and a label disagreed during development, each case was
examined individually and the label was changed only when the engine's answer
was demonstrably the better reading of the data (the notes in
``test_cross_source_mapping.py`` record two such cases). Everything else was
left as a genuine miss.

``EXPECTED_UNMAPPED`` matters as much as the positive labels: a benchmark that
only rewards coverage would push the engine toward guessing, which is the
failure mode this phase exists to avoid.

Metrics reported (Step 51):

    top-1 accuracy        best candidate == expected target
    top-3 recall          expected target anywhere in the top 3
    auto-selection precision   of what it chose automatically, how much was right
    automatic coverage    fraction of labelled fields it chose automatically
    ambiguity rate        fraction it declined as too close to call
    unmapped rate         fraction it found no home for
    correct-refusal rate  of fields that SHOULD be unmapped, how many were

The thresholds asserted at the end are deliberately modest. This is a research
benchmark on a small synthetic corpus, not a production SLA, and the test
exists to catch regressions and to make the real numbers visible - not to
manufacture a flattering figure.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from erp_pipeline.mapping import FieldOutcome, MappingService
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SchemaOrigin,
    SourceType,
)

from tests.erp_pipeline.mapping.conftest import make_entity, make_field, make_schema

T = FieldDataType


@dataclass(frozen=True)
class Label:
    """One hand-declared expectation."""

    source_style: str
    entity: str
    field: str
    data_type: FieldDataType
    #: Qualified canonical target, or None when the field SHOULD stay unmapped.
    expected: str | None
    path: tuple[str, ...] | None = None


# ============================================================
# The labelled corpus - written by hand
# ============================================================

LABELS: tuple[Label, ...] = (
    # ---------- PostgreSQL: snake_case, fully typed ----------
    Label("postgresql", "fin_invoice", "invoice_no", T.STRING, "invoice.invoice_id"),
    Label("postgresql", "fin_invoice", "customer_ref", T.STRING, "invoice.customer_id"),
    Label("postgresql", "fin_invoice", "total_amount", T.DECIMAL, "invoice.amount"),
    Label("postgresql", "fin_invoice", "currency_code", T.STRING, "invoice.currency"),
    Label("postgresql", "fin_invoice", "approval_status", T.STRING, "invoice.status"),
    Label("postgresql", "fin_invoice", "issue_date", T.DATE, "invoice.issued_on"),
    Label("postgresql", "fin_invoice", "row_version", T.INTEGER, None),
    Label("postgresql", "fin_invoice", "etl_batch_id", T.INTEGER, None),
    Label("postgresql", "fin_customer", "customer_id", T.STRING,
          "customer.customer_id"),
    Label("postgresql", "fin_customer", "customer_name", T.STRING, "customer.name"),
    Label("postgresql", "fin_customer", "email", T.STRING, "customer.email"),
    Label("postgresql", "fin_customer", "phone_number", T.STRING, "customer.phone"),
    Label("postgresql", "fin_customer", "internal_notes", T.STRING, None),

    # ---------- MySQL: camelCase columns ----------
    Label("mysql", "invoices", "invoiceId", T.STRING, "invoice.invoice_id"),
    Label("mysql", "invoices", "customerId", T.STRING, "invoice.customer_id"),
    Label("mysql", "invoices", "total", T.DECIMAL, "invoice.amount"),
    Label("mysql", "invoices", "status", T.STRING, "invoice.status"),
    Label("mysql", "invoices", "currency", T.STRING, "invoice.currency"),
    Label("mysql", "invoices", "issuedAt", T.DATETIME, "invoice.issued_on"),
    Label("mysql", "invoices", "syncFlag", T.BOOLEAN, None),
    Label("mysql", "customers", "customerId", T.STRING, "customer.customer_id"),
    Label("mysql", "customers", "fullName", T.STRING, "customer.name"),
    Label("mysql", "customers", "email_address", T.STRING, "customer.email"),
    Label("mysql", "customers", "mobile", T.STRING, "customer.phone"),

    # ---------- MongoDB: nested paths ----------
    Label("mongodb", "invoices", "invoice", T.STRING, "invoice.invoice_id"),
    Label("mongodb", "invoices", "id", T.STRING, "invoice.customer_id",
          path=("customer",)),
    Label("mongodb", "invoices", "email", T.STRING, "customer.email",
          path=("customer", "contact")),
    Label("mongodb", "invoices", "phone", T.STRING, "customer.phone",
          path=("customer", "contact")),
    Label("mongodb", "invoices", "total", T.DECIMAL, "invoice.amount",
          path=("financial",)),
    Label("mongodb", "invoices", "currency", T.STRING, "invoice.currency",
          path=("financial",)),
    Label("mongodb", "invoices", "status", T.STRING, "invoice.status"),
    Label("mongodb", "invoices", "shard_key", T.STRING, None),
    Label("mongodb", "customers", "customer_no", T.STRING,
          "customer.customer_id"),
    Label("mongodb", "customers", "name", T.STRING, "customer.name",
          path=("profile",)),

    # ---------- CSV: abbreviated headers ----------
    Label("csv", "invoice_export", "inv_no", T.STRING, "invoice.invoice_id"),
    Label("csv", "invoice_export", "cust_no", T.STRING, "invoice.customer_id"),
    Label("csv", "invoice_export", "total_amt", T.DECIMAL, "invoice.amount"),
    Label("csv", "invoice_export", "ccy", T.STRING, "invoice.currency"),
    Label("csv", "invoice_export", "stat", T.STRING, "invoice.status"),
    Label("csv", "invoice_export", "invoice_date", T.DATE, "invoice.issued_on"),
    Label("csv", "invoice_export", "col_17", T.STRING, None),
    Label("csv", "customer_export", "cust_no", T.STRING, "customer.customer_id"),
    Label("csv", "customer_export", "cust_name", T.STRING, "customer.name"),
    Label("csv", "customer_export", "email_addr", T.STRING, "customer.email"),
    Label("csv", "customer_export", "tel", T.STRING, "customer.phone"),

    # ---------- OpenAPI: camelCase, declared types ----------
    Label("openapi", "Invoice", "invoiceId", T.STRING, "invoice.invoice_id"),
    Label("openapi", "Invoice", "customerId", T.STRING, "invoice.customer_id"),
    Label("openapi", "Invoice", "totalAmount", T.DECIMAL, "invoice.amount"),
    Label("openapi", "Invoice", "currency", T.STRING, "invoice.currency"),
    Label("openapi", "Invoice", "status", T.STRING, "invoice.status"),
    Label("openapi", "Invoice", "issuedOn", T.DATE, "invoice.issued_on"),
    Label("openapi", "Invoice", "etag", T.STRING, None),
    Label("openapi", "Customer", "customerId", T.STRING, "customer.customer_id"),
    Label("openapi", "Customer", "displayName", T.STRING, "customer.name"),
    Label("openapi", "Customer", "email", T.STRING, "customer.email",
          path=("contact",)),
    Label("openapi", "Customer", "phone", T.STRING, "customer.phone",
          path=("contact",)),

    # ---------- Postman: inferred from examples ----------
    Label("postman", "Get Invoice_response_200", "invoice_id", T.STRING,
          "invoice.invoice_id"),
    Label("postman", "Get Invoice_response_200", "customer_id", T.STRING,
          "invoice.customer_id"),
    Label("postman", "Get Invoice_response_200", "amount", T.INTEGER,
          "invoice.amount"),
    Label("postman", "Get Invoice_response_200", "emailAddress", T.STRING,
          "customer.email"),
    Label("postman", "Get Invoice_response_200", "invoiceStatus", T.STRING,
          "invoice.status"),
    Label("postman", "Get Invoice_response_200", "_debug_trace", T.STRING, None),
    Label("postman", "Create Customer_request", "customerNumber", T.STRING,
          "customer.customer_id"),
    Label("postman", "Create Customer_request", "companyName", T.STRING,
          "customer.name"),

    # ---------- Purchase orders, across styles ----------
    Label("postgresql", "purchase_orders", "po_no", T.STRING,
          "purchase_order.purchase_order_id"),
    Label("postgresql", "purchase_orders", "supplier_no", T.STRING,
          "purchase_order.supplier_id"),
    Label("postgresql", "purchase_orders", "order_total", T.DECIMAL,
          "purchase_order.amount"),
    Label("postgresql", "purchase_orders", "order_status", T.STRING,
          "purchase_order.status"),
)

_SOURCE_TYPES = {
    "postgresql": (SourceType.POSTGRESQL, SchemaOrigin.DISCOVERED, EntityKind.TABLE),
    "mysql": (SourceType.MYSQL, SchemaOrigin.DISCOVERED, EntityKind.TABLE),
    "mongodb": (SourceType.MONGODB, SchemaOrigin.INFERRED, EntityKind.COLLECTION),
    "csv": (SourceType.CSV, SchemaOrigin.INFERRED, EntityKind.DATASET),
    "openapi": (SourceType.OPENAPI, SchemaOrigin.API_SPEC, EntityKind.API_SCHEMA),
    "postman": (SourceType.POSTMAN, SchemaOrigin.INFERRED, EntityKind.API_SCHEMA),
}


def _build_schemas() -> dict[str, object]:
    """Turn the labelled corpus into one ``SourceSchema`` per source style."""
    grouped: dict[str, dict[str, list]] = {}

    for label in LABELS:
        grouped.setdefault(label.source_style, {}).setdefault(
            label.entity, []
        ).append(label)

    schemas = {}
    for style, entities in grouped.items():
        source_type, origin, kind = _SOURCE_TYPES[style]
        schemas[style] = make_schema(
            f"bench_{style}", source_type, origin,
            entities=tuple(
                make_entity(
                    entity_name,
                    tuple(
                        make_field(item.field, item.data_type, path=item.path)
                        for item in items
                    ),
                    kind=kind,
                )
                for entity_name, items in entities.items()
            ),
        )

    return schemas


@dataclass
class BenchmarkMetrics:
    labelled: int
    positive_labels: int
    negative_labels: int
    top1_correct: int
    top3_correct: int
    auto_selected: int
    auto_correct: int
    ambiguous: int
    unmapped: int
    correct_refusals: int
    #: Labels whose source spelling is NOT declared as an alias of the expected
    #: target, and how many of those the engine still got right. This is the
    #: honest generalization measure: the alias registry and these labels were
    #: written by the same author, so overall accuracy partly measures
    #: internal consistency. These fields had to be resolved by normalization,
    #: tokenization and context alone.
    alias_independent: int = 0
    alias_independent_correct: int = 0

    @property
    def top1_accuracy(self) -> float:
        return round(self.top1_correct / self.positive_labels, 4)

    @property
    def top3_recall(self) -> float:
        return round(self.top3_correct / self.positive_labels, 4)

    @property
    def auto_precision(self) -> float:
        if not self.auto_selected:
            return 0.0
        return round(self.auto_correct / self.auto_selected, 4)

    @property
    def auto_coverage(self) -> float:
        return round(self.auto_selected / self.labelled, 4)

    @property
    def ambiguity_rate(self) -> float:
        return round(self.ambiguous / self.labelled, 4)

    @property
    def unmapped_rate(self) -> float:
        return round(self.unmapped / self.labelled, 4)

    @property
    def correct_refusal_rate(self) -> float:
        if not self.negative_labels:
            return 1.0
        return round(self.correct_refusals / self.negative_labels, 4)

    @property
    def alias_independent_accuracy(self) -> float:
        if not self.alias_independent:
            return 0.0
        return round(self.alias_independent_correct / self.alias_independent, 4)

    def render(self) -> str:
        return (
            f"labelled mappings      : {self.labelled} "
            f"({self.positive_labels} positive, {self.negative_labels} negative)\n"
            f"top-1 accuracy         : {self.top1_accuracy}\n"
            f"top-3 recall           : {self.top3_recall}\n"
            f"auto-selection precision: {self.auto_precision} "
            f"({self.auto_correct}/{self.auto_selected})\n"
            f"automatic coverage     : {self.auto_coverage}\n"
            f"ambiguity rate         : {self.ambiguity_rate}\n"
            f"unmapped rate          : {self.unmapped_rate}\n"
            f"correct refusal rate   : {self.correct_refusal_rate}\n"
            f"alias-independent top-1: {self.alias_independent_accuracy} "
            f"({self.alias_independent_correct}/{self.alias_independent} labels "
            f"the alias registry never declared)"
        )


def evaluate() -> BenchmarkMetrics:
    """Run the engine over the labelled corpus and measure it."""
    service = MappingService()
    schemas = _build_schemas()

    by_key = {
        (label.source_style, label.entity, _path_of(label)): label
        for label in LABELS
    }

    metrics = BenchmarkMetrics(
        labelled=len(LABELS),
        positive_labels=sum(1 for item in LABELS if item.expected is not None),
        negative_labels=sum(1 for item in LABELS if item.expected is None),
        top1_correct=0, top3_correct=0, auto_selected=0, auto_correct=0,
        ambiguous=0, unmapped=0, correct_refusals=0,
    )

    alias_index = service.engine.alias_index

    for style, schema in schemas.items():
        result = service.generate(schema, validate=False)

        for decision in result.decisions:
            label = by_key.get((style, decision.source_entity, decision.source_field))
            if label is None:  # pragma: no cover - corpus/schema mismatch guard
                continue

            candidates = [item.qualified_target for item in decision.candidates]

            if label.expected is not None:
                correct_top1 = candidates[:1] == [label.expected]

                if correct_top1:
                    metrics.top1_correct += 1
                if label.expected in candidates[:3]:
                    metrics.top3_correct += 1

                if not _is_declared_alias(alias_index, label):
                    metrics.alias_independent += 1
                    if correct_top1:
                        metrics.alias_independent_correct += 1

            if decision.outcome is FieldOutcome.AUTO_SELECTED:
                metrics.auto_selected += 1
                if decision.selected.qualified_target == label.expected:
                    metrics.auto_correct += 1
            elif decision.outcome is FieldOutcome.AMBIGUOUS:
                metrics.ambiguous += 1
            elif decision.outcome is FieldOutcome.UNMAPPED:
                metrics.unmapped += 1
                if label.expected is None:
                    metrics.correct_refusals += 1

            if label.expected is None and decision.outcome in (
                FieldOutcome.REVIEW_REQUIRED, FieldOutcome.AMBIGUOUS,
            ):
                # Not selected, so not a wrong mapping - counted as a refusal
                # too, just a less decisive one.
                metrics.correct_refusals += 1

    return metrics


def _path_of(label: Label) -> str:
    return ".".join(list(label.path or ()) + [label.field])


def _is_declared_alias(alias_index, label: Label) -> bool:
    """Whether the registry explicitly declares this spelling for this target.

    Used to separate "the engine knew because someone told it" from "the
    engine worked it out", which is the only part of the score that says
    anything about unseen sources.
    """
    if label.expected is None:
        return False

    entity_type, field_name = label.expected.split(".", 1)
    declared = set(alias_index.declared_aliases_for(entity_type, field_name))
    declared.add(field_name)

    return label.field in declared or _path_of(label) in declared


@pytest.fixture(scope="module")
def metrics() -> BenchmarkMetrics:
    return evaluate()


# ============================================================
# The benchmark
# ============================================================

def test_the_corpus_is_large_and_hand_labelled(metrics):
    assert metrics.labelled >= 50
    assert metrics.negative_labels >= 8, (
        "a benchmark with no negative labels rewards guessing"
    )


def test_benchmark_metrics_are_reported(metrics, capsys):
    """Prints the actual numbers. Run with -s to see them."""
    with capsys.disabled():
        print("\n" + "=" * 60)
        print("PHASE 8 MAPPING BENCHMARK")
        print("=" * 60)
        print(metrics.render())
        print("=" * 60)


def test_top1_accuracy(metrics):
    assert metrics.top1_accuracy >= 0.80, metrics.render()


def test_top3_recall_exceeds_top1(metrics):
    assert metrics.top3_recall >= metrics.top1_accuracy
    assert metrics.top3_recall >= 0.85, metrics.render()


def test_auto_selection_precision_is_high(metrics):
    """Step 52: precision matters far more than coverage. An engine that
    auto-selects little but is right when it does is doing its job."""
    assert metrics.auto_precision >= 0.90, metrics.render()


def test_the_engine_refuses_fields_that_have_no_target(metrics):
    """Negative labels must not be silently mapped."""
    assert metrics.correct_refusal_rate >= 0.90, metrics.render()


def test_generalization_beyond_the_declared_aliases(metrics):
    """The honest half of the benchmark.

    The alias registry and these labels share an author, so overall accuracy
    partly measures internal consistency. This subset - fields whose spelling
    the registry never declares - had to be resolved by normalization,
    tokenization, type and context alone, and is the number that says
    something about an unseen ERP.
    """
    assert metrics.alias_independent >= 15, (
        "too few alias-independent labels for the measure to mean anything"
    )
    assert metrics.alias_independent_accuracy >= 0.75, metrics.render()


def test_coverage_is_reported_without_being_forced(metrics):
    """No assertion that coverage is high - only that it is measured, and that
    the engine has not simply mapped everything."""
    assert 0.0 < metrics.auto_coverage < 1.0, metrics.render()


def test_ambiguous_fields_are_never_auto_selected():
    """The single most important negative property: the engine must not
    silently resolve a coin toss."""
    service = MappingService()

    for schema in _build_schemas().values():
        result = service.generate(schema, validate=False)

        for decision in result.decisions:
            if decision.ambiguity is not None:
                assert decision.selected is None, decision.source_field
                assert decision.outcome is FieldOutcome.AMBIGUOUS


def test_no_auto_selection_has_an_incompatible_type():
    """A type conflict must veto automatic selection everywhere in the corpus,
    not just in the unit tests."""
    service = MappingService()

    for schema in _build_schemas().values():
        result = service.generate(schema, validate=False)

        for decision in result.decisions:
            if decision.outcome is FieldOutcome.AUTO_SELECTED:
                assert not decision.selected.has_type_conflict, decision.source_field


def test_the_benchmark_is_deterministic():
    """Two runs of the whole corpus produce identical metrics."""
    first = evaluate()
    second = evaluate()

    assert first.render() == second.render()
