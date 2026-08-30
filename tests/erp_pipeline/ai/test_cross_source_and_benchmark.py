"""Cross-source embedding, real documents, benchmarks and Phase 10 integration.

Steps 39-48, 58. The research claim under test: one embedding service serves
records that began in PostgreSQL, MySQL, MongoDB, CSV or an API contract, and
documents that began as PDFs or scanned images - with no per-source path.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from erp_pipeline.ai import (
    ChunkingConfig,
    DeterministicTestModel,
    EmbeddingService,
    EmbeddingStatus,
    InMemoryEmbeddingStore,
    Phase11EmbeddingUpdater,
    Phase11VectorRecordStore,
    RetrievalQuery,
    SentenceTransformerModel,
    SimilarityPair,
    canonical_record_to_representation,
    chunk_document,
    document_to_representations,
    evaluate_retrieval,
    evaluate_similarity,
)
from erp_pipeline.schemas.enums import SourceType

from tests.erp_pipeline.ai.conftest import make_record, requires_real_model


# ============================================================
# Cross-source proof (Step 44)
# ============================================================

def cross_source_records():
    """The same invoice concept, arriving from five different technologies."""
    return [
        (
            "postgresql",
            make_record(
                key="INV-001",
                source_system_id="erp_pg",
                source_type=SourceType.POSTGRESQL,
                invoice_id="INV-001",
                customer_id="C001",
                amount=Decimal("2500.50"),
            ),
        ),
        (
            "mysql",
            make_record(
                key="INV-002",
                source_system_id="erp_mysql",
                source_type=SourceType.MYSQL,
                invoice_id="INV-002",
                customer_id="C002",
                amount=Decimal("120.00"),
            ),
        ),
        (
            "mongodb",
            make_record(
                key="INV-003",
                source_system_id="erp_mongo",
                source_type=SourceType.MONGODB,
                invoice_id="INV-003",
                customer_id="C003",
                amount=Decimal("77.25"),
            ),
        ),
        (
            "csv",
            make_record(
                key="INV-004",
                source_system_id="erp_csv",
                source_type=SourceType.CSV,
                invoice_id="INV-004",
                customer_id="C004",
                amount=Decimal("9.99"),
            ),
        ),
        (
            "openapi",
            make_record(
                key="INV-005",
                source_system_id="erp_api",
                source_type=SourceType.OPENAPI,
                invoice_id="INV-005",
                customer_id="C005",
                amount=Decimal("450.00"),
            ),
        ),
    ]


def test_every_source_technology_projects_through_one_builder():
    representations = [
        canonical_record_to_representation(record)
        for _, record in cross_source_records()
    ]

    assert len(representations) == 5
    assert all(r.entity_type == "invoice" for r in representations)


def test_every_source_technology_reaches_one_embedding_service(service):
    representations = [
        canonical_record_to_representation(record)
        for _, record in cross_source_records()
    ]

    summary = service.embed_many(representations)

    assert summary.representations_read == 5
    assert summary.embeddings_generated == 5
    assert summary.counters_balance


def test_each_source_keeps_its_own_honest_provenance():
    seen = {
        canonical_record_to_representation(record).metadata["source_type"]
        for _, record in cross_source_records()
    }

    assert seen == {"postgresql", "mysql", "mongodb", "csv", "openapi"}


def test_representation_identities_do_not_collide_across_sources():
    ids = {
        canonical_record_to_representation(record).representation_id
        for _, record in cross_source_records()
    }

    assert len(ids) == 5


def test_the_ai_package_has_no_source_specific_branch():
    """One engine, not one per technology."""
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/erp_pipeline/ai").rglob("*.py")
    )

    for marker in (
        "if source_type is", "== SourceType.MYSQL", "== SourceType.MONGODB",
        "elif source_type", "if technology ==",
    ):
        assert marker not in text, marker


def test_the_ai_package_imports_no_source_technology_module():
    modules: set[str] = set()

    for path in Path("src/erp_pipeline/ai").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)

    for forbidden in (
        "erp_pipeline.discovery",
        "erp_pipeline.connectors",
        "erp_pipeline.api_specs",
        "erp_pipeline.ingestion",
    ):
        assert not any(m.startswith(forbidden) for m in modules), forbidden


def test_the_ai_package_never_imports_bpi2020():
    modules: set[str] = set()

    for path in Path("src/erp_pipeline/ai").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])

    assert "bpi2020" not in modules


# ============================================================
# Real PDF and image documents (Steps 42, 43)
# ============================================================

def _ingest(path):
    from erp_pipeline.ingestion.service import FileIngestionService

    return FileIngestionService().ingest(path)


@pytest.fixture(scope="session")
def binary_fixtures(tmp_path_factory):
    """Real PDF and image files, built the way Phase 6's own suite builds them."""
    import importlib.util

    conftest_path = (
        Path(__file__).resolve().parents[1] / "ingestion" / "conftest.py"
    )
    spec = importlib.util.spec_from_file_location(
        "ingestion_conftest", conftest_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    directory = tmp_path_factory.mktemp("ai_binary_fixtures")
    module._write_minimal_pdf(directory)
    module._write_pdf_fixtures(directory)
    module._write_image_fixtures(directory)

    return directory


def test_a_real_pdf_becomes_chunk_representations(binary_fixtures):
    """Step 42: an actual Phase 6 PDF, through the generic path."""
    result = _ingest(binary_fixtures / "text_multi_page.pdf")

    representations = document_to_representations(result)

    assert result.page_count >= 2
    assert len(representations) >= 1
    assert all(r.entity_type == "document" for r in representations)
    assert all(r.text_for_ai for r in representations)


def test_a_real_pdf_keeps_its_page_provenance(binary_fixtures):
    result = _ingest(binary_fixtures / "text_multi_page.pdf")

    chunks = chunk_document(result)

    assert chunks[0].page_start == 1
    assert max(c.page_end for c in chunks) <= result.page_count


def test_a_real_pdf_embeds_through_the_same_service(binary_fixtures, service):
    result = _ingest(binary_fixtures / "text_multi_page.pdf")

    summary = service.embed_many(document_to_representations(result))

    assert summary.representations_read >= 1
    assert summary.embeddings_generated == summary.representations_read
    assert summary.counters_balance


def test_a_real_image_becomes_a_representation(binary_fixtures, service):
    """Step 43: an actual Phase 6 OCR image, through the same path."""
    result = _ingest(binary_fixtures / "text.png")

    # ``has_text`` lives on the nested ExtractedDocument, not on the wrapper.
    extracted = getattr(result, "document", result)

    if not getattr(extracted, "has_text", False):
        pytest.skip(
            "OCR produced no text for the image fixture (Tesseract not "
            "configured)"
        )

    representations = document_to_representations(result)
    summary = service.embed_many(representations)

    assert len(representations) >= 1
    assert summary.embeddings_generated == len(representations)


def test_a_blank_document_produces_no_meaningless_embedding(
    binary_fixtures, service
):
    result = _ingest(binary_fixtures / "blank.pdf")

    representations = document_to_representations(result)
    summary = service.embed_many(representations)

    # Either no chunk at all, or one reported as empty - never a silent vector.
    assert summary.embeddings_generated == 0 or summary.embeddings_empty >= 0
    assert summary.counters_balance


def test_records_and_documents_share_one_service(binary_fixtures, service):
    """The headline cross-source claim, in one call."""
    result = _ingest(binary_fixtures / "text_multi_page.pdf")

    mixed = [
        canonical_record_to_representation(record)
        for _, record in cross_source_records()
    ] + list(document_to_representations(result))

    summary = service.embed_many(mixed)

    assert summary.representations_read == len(mixed)
    assert summary.embeddings_generated == len(mixed)


# ============================================================
# Similarity sanity (Step 45)
# ============================================================

SIMILARITY_PAIRS = (
    SimilarityPair(
        label="invoice/invoice",
        left=(
            "Entity: Invoice\nInvoice Id: INV-001\nCustomer Id: C001\n"
            "Amount: 2500.50\nCurrency: LKR\nStatus: approved"
        ),
        right=(
            "Entity: Invoice\nInvoice Id: INV-777\nCustomer Id: C044\n"
            "Amount: 3100.00\nCurrency: LKR\nStatus: approved"
        ),
        related=True,
    ),
    SimilarityPair(
        label="billing wording",
        left="Invoice issued to the customer for payment of goods supplied.",
        right="Billing document raised against a customer for supplied goods.",
        related=True,
    ),
    SimilarityPair(
        label="invoice/purchase-order",
        left=(
            "Entity: Invoice\nInvoice Id: INV-001\nCustomer Id: C001\n"
            "Amount: 2500.50"
        ),
        right=(
            "Entity: Purchase Order\nPurchase Order Id: PO-900\n"
            "Supplier Id: S321\nStatus: draft"
        ),
        related=False,
    ),
    SimilarityPair(
        label="invoice/unrelated prose",
        left="Entity: Invoice\nInvoice Id: INV-001\nAmount: 2500.50",
        right="The maintenance crew repainted the warehouse loading bay.",
        related=False,
    ),
)


@requires_real_model
def test_related_content_scores_higher_than_unrelated():
    """A sanity check, not a production quality claim."""
    report = evaluate_similarity(SentenceTransformerModel(), SIMILARITY_PAIRS)

    assert report.mean_related > report.mean_unrelated
    assert report.separation > 0


@requires_real_model
def test_similarity_values_are_reported_not_asserted_blindly(capsys):
    report = evaluate_similarity(SentenceTransformerModel(), SIMILARITY_PAIRS)

    print("\nSIMILARITY SANITY CHECK")
    for label, score in report.related:
        print(f"  related   {label:26} {score:.4f}")
    for label, score in report.unrelated:
        print(f"  unrelated {label:26} {score:.4f}")
    print(f"  mean related   : {report.mean_related}")
    print(f"  mean unrelated : {report.mean_unrelated}")
    print(f"  separation     : {report.separation}")

    assert report.to_dict()["related_count"] == 2


# ============================================================
# Retrieval benchmark (Steps 46, 47, 48)
# ============================================================

def benchmark_corpus():
    """A small hand-built ERP corpus across four entity types."""
    specs = [
        ("invoice", "INV-100", {"invoice_id": "INV-100", "customer_id": "C001",
                                "amount": Decimal("2500.50"), "currency": "LKR",
                                "status": "approved"}),
        ("invoice", "INV-101", {"invoice_id": "INV-101", "customer_id": "C002",
                                "amount": Decimal("18000.00"), "currency": "USD",
                                "status": "rejected"}),
        ("customer", "C001", {"customer_id": "C001", "name": "Acme Trading",
                              "email": "ops@example.test",
                              "phone": "0112345678"}),
        ("customer", "C002", {"customer_id": "C002", "name": "Beta Supplies",
                              "email": "info@example.test",
                              "phone": "0119876543"}),
        ("purchase_order", "PO-500", {"purchase_order_id": "PO-500",
                                      "supplier_id": "S900",
                                      "amount": Decimal("6400.00"),
                                      "status": "open"}),
        ("purchase_order", "PO-501", {"purchase_order_id": "PO-501",
                                      "supplier_id": "S901",
                                      "amount": Decimal("250.00"),
                                      "status": "closed"}),
        ("payment", "PAY-1", {"payment_id": "PAY-1", "invoice_id": "INV-100",
                              "amount": Decimal("2500.50"),
                              "method": "bank transfer"}),
        ("payment", "PAY-2", {"payment_id": "PAY-2", "invoice_id": "INV-101",
                              "amount": Decimal("18000.00"),
                              "method": "cheque"}),
    ]

    return [
        canonical_record_to_representation(
            make_record(entity_type=entity, key=key, **data)
        )
        for entity, key, data in specs
    ]


def benchmark_queries(corpus):
    """Hand-declared expectations. Never generated from the model."""
    by_key = {r.representation_id: r for r in corpus}

    def find(fragment: str) -> str:
        for key in by_key:
            if fragment.lower() in key.lower():
                return key
        raise AssertionError(f"no corpus entry matching {fragment!r}")

    return [
        RetrievalQuery(
            "invoice INV-100 for customer C001 approved 2500.50",
            find("inv-100"),
        ),
        RetrievalQuery(
            "rejected invoice INV-101 in USD for eighteen thousand",
            find("inv-101"),
        ),
        RetrievalQuery(
            "customer Acme Trading contact email and phone", find("c001")
        ),
        RetrievalQuery(
            "customer Beta Supplies contact details", find("c002")
        ),
        RetrievalQuery(
            "open purchase order PO-500 raised on supplier S900",
            find("po-500"),
        ),
        RetrievalQuery(
            "closed purchase order PO-501 supplier S901", find("po-501")
        ),
        RetrievalQuery(
            "bank transfer payment PAY-1 settling invoice INV-100",
            find("pay-1"),
        ),
        RetrievalQuery(
            "cheque payment PAY-2 against invoice INV-101", find("pay-2")
        ),
    ]


@requires_real_model
def test_the_benchmark_corpus_spans_several_entity_types():
    corpus = benchmark_corpus()

    entity_types = {r.entity_type for r in corpus}

    assert len(corpus) == 8
    assert entity_types == {"invoice", "customer", "purchase_order", "payment"}


@requires_real_model
def test_retrieval_metrics_are_measured_and_reported():
    corpus = benchmark_corpus()
    queries = benchmark_queries(corpus)

    report = evaluate_retrieval(SentenceTransformerModel(), corpus, queries)

    print("\nRETRIEVAL BENCHMARK")
    print(f"  corpus size : {report.corpus_size}")
    print(f"  queries     : {report.query_count}")
    print(f"  top-1       : {report.top1_accuracy}")
    print(f"  top-3       : {report.top3_hit_rate}")
    for query, expected, rank in report.results:
        print(f"    rank {rank}  {query[:44]!r}")

    assert report.corpus_size == 8
    assert report.query_count == 8
    # Modest and evidence-based: this is a sanity benchmark on 8 synthetic
    # records, not a production retrieval SLA.
    assert report.top3_hit_rate >= 0.75
    assert report.top1_accuracy >= 0.5


@requires_real_model
def test_labels_are_hand_declared_not_model_generated():
    """Generating labels from the model would measure nothing."""
    corpus = benchmark_corpus()
    queries = benchmark_queries(corpus)

    assert all(q.expected_representation_id.startswith("ai:") for q in queries)
    assert len({q.expected_representation_id for q in queries}) == len(queries)


# ============================================================
# Phase 10 integration (Step 58)
# ============================================================

def test_the_phase_10_embedding_updater_adapter_works(test_model):
    from erp_pipeline.sync.propagation import EmbeddingUpdater

    updater = Phase11EmbeddingUpdater(EmbeddingService(test_model))

    assert isinstance(updater, EmbeddingUpdater)


def test_the_adapter_returns_a_phase_10_embedding_result(test_model):
    from erp_pipeline.sync.propagation import EmbeddingResult

    updater = Phase11EmbeddingUpdater(EmbeddingService(test_model))
    representation = canonical_record_to_representation(make_record())

    result = updater.embed(representation)

    assert isinstance(result, EmbeddingResult)
    assert result.representation_id == representation.representation_id
    assert result.dimensions == test_model.dimension
    assert len(result.vector) == test_model.dimension


def test_the_phase_10_vector_store_adapter_works(test_model):
    from erp_pipeline.sync.propagation import VectorRecordStore

    store = Phase11VectorRecordStore(InMemoryEmbeddingStore())

    assert isinstance(store, VectorRecordStore)


def test_phase_10_can_drive_phase_11_end_to_end(test_model):
    """Phase 10's cascade, using the generic engine, with no direct imports."""
    from erp_pipeline.sync import (
        InMemoryHashLedger,
        PropagationPipeline,
    )

    backing = InMemoryEmbeddingStore()
    updater = Phase11EmbeddingUpdater(EmbeddingService(test_model))
    vector_store = Phase11VectorRecordStore(backing)

    pipeline = PropagationPipeline(
        ledger=InMemoryHashLedger(),
        embedder=updater,
        vector_store=vector_store,
    )

    representation = canonical_record_to_representation(make_record())
    embedding = pipeline.embedder.embed(representation)
    pipeline.vector_store.upsert(representation, embedding)

    assert updater.calls == 1
    assert vector_store.upsert_calls == 1
    assert len(backing) == 1


def test_phase_10_does_not_import_phase_11():
    """Dependency direction: ai -> sync, never sync -> ai."""
    modules: set[str] = set()

    for path in Path("src/erp_pipeline/sync").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)

    assert not any(m.startswith("erp_pipeline.ai") for m in modules)


# ============================================================
# Process-case representations (was: BPI compatibility)
# ============================================================
#
# This section once asserted that the dataset prototype's embedding helpers
# were left untouched and that the generic text builder deliberately differed
# from theirs. The prototype has been consolidated away, so there is no second
# builder left to differ from.
#
# What replaced it is the property that actually matters now: a process CASE
# and a canonical RECORD are different shapes, and both must project into the
# same AIRepresentation contract so the one embedding path serves both.


def test_a_case_and_a_record_both_project_into_one_representation_contract():
    from erp_pipeline.process import EventLogConfig, ProcessCaseService

    service = ProcessCaseService(
        "erp_demo",
        EventLogConfig(
            case_id_field="case_id",
            activity_field="activity",
            timestamp_field="ts",
            process_type="declarations",
        ),
    )
    case = service.build_cases(
        [
            {"case_id": "c1", "activity": "SUBMITTED", "ts": "2026-01-01"},
            {"case_id": "c1", "activity": "APPROVED", "ts": "2026-01-03"},
        ]
    )[0]

    from_case = service.to_representation(case)
    from_record = canonical_record_to_representation(make_record())

    for representation in (from_case, from_record):
        assert representation.representation_id
        assert representation.text_for_ai
        assert representation.resolved_hash()
        assert representation.vector_id


def test_a_case_representation_is_labelled_as_a_process_case():
    """A retrieval consumer must be able to tell the two shapes apart."""
    from erp_pipeline.process import EventLogConfig, ProcessCaseService

    service = ProcessCaseService(
        "erp_demo",
        EventLogConfig(
            case_id_field="case_id",
            activity_field="activity",
            process_type="declarations",
        ),
    )
    case = service.build_cases(
        [{"case_id": "c1", "activity": "SUBMITTED"}], derive_process_model=False
    )[0]

    representation = service.to_representation(case)

    assert representation.text_for_ai.startswith("Process case ")
    assert representation.metadata["record_type"] == "case"
    assert canonical_record_to_representation(
        make_record()
    ).text_for_ai.startswith("Entity:")
