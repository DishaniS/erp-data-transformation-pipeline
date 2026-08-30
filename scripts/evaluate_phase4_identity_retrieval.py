"""Phase 4 mini-evaluation: can retrieval name the ERP record it found?

Builds a deterministic corpus, runs identity-filtered queries against it, and
counts the ways the answer could be wrong: a hit belonging to a different
employee, a different document type, or a different content kind. Also measures
what a filtered search costs.

This is a technical mini-evaluation, not the final research experiment. It
asserts nothing; the pass/fail judgement belongs to the report that reads it.

Run:
    python scripts/evaluate_phase4_identity_retrieval.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from erp_pipeline.ai.attached_documents import (
    DocumentAttachment,
    attached_document_to_representations,
)
from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import _carried_identity
from erp_pipeline.ingestion.binary_assets import extract_binary_asset
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SensitivityLevel,
    SourceType,
)
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.storage.filters import (
    FILTERABLE_FIELDS,
    PROVENANCE_ONLY_FIELDS,
    InvalidFilterValueError,
    SearchFilters,
    UnknownFilterFieldError,
)
from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import TierSet, _payload_for
from erp_pipeline.storage.models import StorageTier
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync.hashing import vector_id_for
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

ARTIFACT = ROOT / "artifacts" / "phase4_identity_retrieval_evaluation.json"
VECTOR = [0.1, 0.2, 0.3, 0.4]


class FilterAwareTier:
    """A tier honouring a Qdrant-style ``must`` filter, as the real one does."""

    dimension = len(VECTOR)

    def __init__(self) -> None:
        self.points: list[tuple[str, dict]] = []

    def upsert(self, record, payload=None):
        self.points.append(
            (vector_id_for(record.representation_id), dict(payload or {}))
        )
        return True

    def get_vector(self, representation_id):
        return tuple(VECTOR)

    def exists(self, representation_id):
        return True

    def delete(self, representation_id):
        return True

    def count(self):
        return len(self.points)

    def search(self, vector, limit=5, query_filter=None):
        results = []

        for vector_id, payload in self.points:
            if query_filter is not None and not all(
                payload.get(condition.key) == condition.match.value
                for condition in query_filter.must
            ):
                continue

            results.append((vector_id, 0.9))

        return results[:limit]


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type="X",
        normalized_data_type=data_type,
        is_primary_key=primary,
        nullable=not primary,
    )


def _pdf(lines: list[str]) -> bytes:
    import pymupdf as fitz

    document = fitz.open()
    page = document.new_page()

    for index, line in enumerate(lines):
        page.insert_text((72, 96 + index * 20), line, fontsize=11)

    payload = document.tobytes()
    document.close()

    return payload


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (240, 120), "white").save(buffer, "PNG")

    return buffer.getvalue()


EMPLOYEES = SourceEntity(
    entity_id="hr.employees",
    source_name="employees",
    normalized_name="employees",
    entity_kind=EntityKind.TABLE,
    primary_key_fields=("employee_id",),
    fields=(
        _field("employee_id", FieldDataType.STRING, primary=True),
        _field("full_name", FieldDataType.STRING),
        _field("department", FieldDataType.STRING),
        _field("birth_certificate", FieldDataType.BINARY),
        _field("employment_contract", FieldDataType.BINARY),
        _field("profile_photo", FieldDataType.BINARY),
    ),
)

WAREHOUSE_STOCK = SourceEntity(
    entity_id="wms.warehouse_stock",
    source_name="warehouse_stock",
    normalized_name="warehouse_stock",
    entity_kind=EntityKind.TABLE,
    primary_key_fields=("warehouse_id", "product_id"),
    fields=(
        _field("warehouse_id", FieldDataType.STRING, primary=True),
        _field("product_id", FieldDataType.STRING, primary=True),
        _field("quantity", FieldDataType.INTEGER),
    ),
)


class Index:
    """The corpus, indexed through the production write path."""

    def __init__(self, tier: str = "hot") -> None:
        self.state = InMemoryTierStateStore()
        self.hot = FilterAwareTier() if tier == "hot" else None
        self.warm = FilterAwareTier() if tier == "warm" else None
        self.store = HybridVectorStore(
            TierSet(hot=self.hot, warm=self.warm), self.state
        )
        self.indexed = 0

    def _embed(self, representation) -> EmbeddingRecord:
        return EmbeddingRecord(
            embedding_id=f"emb.{representation.representation_id}",
            representation_id=representation.representation_id,
            entity_type=representation.entity_type,
            content_hash=representation.content_hash or "h",
            model_id="eval-model",
            dimension=len(VECTOR),
            status=EmbeddingStatus.GENERATED,
            vector=tuple(VECTOR),
            metadata=_carried_identity(representation),
        )

    def add(self, representation) -> None:
        override = (
            StorageTier.WARM if self.hot is None and self.warm is not None else None
        )
        self.store.store(
            self._embed(representation),
            sensitivity=SensitivityLevel.INTERNAL,
            override=override,
            override_reason="tier pinned for the evaluation" if override else None,
        )
        self.indexed += 1

    def add_employee(self, employee_id: str, name: str, **blobs) -> None:
        rows = [
            SourceRecord.from_mapping(
                {
                    "employee_id": employee_id,
                    "full_name": name,
                    "department": "Finance",
                    **blobs,
                }
            )
        ]
        canonical = SourceNativeTransformer().transform_records(
            rows, EMPLOYEES, "legacy_hr", SourceType.POSTGRESQL
        ).records[0]

        self.add(canonical_record_to_representation(canonical))

        for field_name, payload in blobs.items():
            if payload is None:
                continue

            asset = extract_binary_asset(payload, field_name)

            if not asset.succeeded:
                continue

            attachment = DocumentAttachment(
                parent_record_id=canonical.record_id,
                source_system_id="legacy_hr",
                source_entity="employees",
                source_field=field_name,
                document_id=asset.document_id or "",
                business_key_name="employee_id",
                business_key_value=employee_id,
                document_type=field_name,
                media_type=asset.media_type,
            )

            for representation in attached_document_to_representations(
                asset.document, attachment
            ):
                self.add(representation)

    def add_stock(self, warehouse: str, product: str, quantity: str) -> None:
        rows = [
            SourceRecord.from_mapping(
                {
                    "warehouse_id": warehouse,
                    "product_id": product,
                    "quantity": quantity,
                }
            )
        ]
        canonical = SourceNativeTransformer().transform_records(
            rows, WAREHOUSE_STOCK, "legacy_wms", SourceType.POSTGRESQL
        ).records[0]

        self.add(canonical_record_to_representation(canonical))

    def find(self, **filters):
        return self.store.search(
            VECTOR, limit=100, filters=SearchFilters.from_mapping(filters)
        ).hits


def build(tier: str = "hot") -> Index:
    shared = _pdf(
        ["BIRTH CERTIFICATE", "Registrar General, Colombo", "Serial: BC-4471"]
    )
    index = Index(tier)

    index.add_employee(
        "EMP001",
        "Sunil Bandara",
        birth_certificate=_pdf(["BIRTH CERTIFICATE", "Serial: BC-0001"]),
    )
    index.add_employee(
        "EMP002",
        "Nimal Silva",
        birth_certificate=shared,
        employment_contract=_pdf(["EMPLOYMENT CONTRACT", "Senior Accountant"]),
        profile_photo=_png(),
    )
    # The same certificate bytes as EMP002 - the Phase 3 collision case.
    index.add_employee("EMP003", "Amal Perera", birth_certificate=shared)
    index.add_stock("WH-1", "P-77", "5")
    index.add_stock("WH-2", "P-77", "9")

    return index


def main() -> int:
    index = build("hot")
    warm = build("warm")

    # ``SearchFilters.to_qdrant_filter`` imports the Qdrant client lazily, so
    # the FIRST filtered query pays for that import - ~2 seconds, and nothing
    # to do with retrieval. Timing it would put a number in the report that
    # describes an import rather than a search.
    index.find(business_key_value="__warmup__")

    queries: list[dict] = []
    wrong_identity = wrong_type = kind_leak = 0
    incomplete_provenance = 0

    def run(name: str, expect_key, expect_type, expect_kind, **filters):
        nonlocal wrong_identity, wrong_type, kind_leak, incomplete_provenance

        started = time.perf_counter()
        hits = index.find(**filters)
        elapsed = (time.perf_counter() - started) * 1000

        bad_identity = [
            h for h in hits
            if expect_key is not None and h.state.business_key_value != expect_key
        ]
        bad_type = [
            h for h in hits
            if expect_type is not None and h.state.document_type != expect_type
        ]
        bad_kind = [
            h for h in hits
            if expect_kind is not None and h.state.content_kind != expect_kind
        ]

        wrong_identity += len(bad_identity)
        wrong_type += len(bad_type)
        kind_leak += len(bad_kind)

        # Every document hit must be able to say where it came from.
        for hit in hits:
            if hit.state.content_kind != "document_chunk":
                continue

            if any(
                getattr(hit.state, name_) is None
                for name_ in (
                    "parent_record_id", "source_field", "document_type",
                    "document_id", "page_start", "chunk_index",
                )
            ):
                incomplete_provenance += 1

        queries.append(
            {
                "name": name,
                "filters": filters,
                "hits": len(hits),
                "wrong_identity": len(bad_identity),
                "wrong_document_type": len(bad_type),
                "wrong_content_kind": len(bad_kind),
                "latency_ms": round(elapsed, 4),
            }
        )

        return hits

    # -- identity --
    run("emp002 certificate chunks", "EMP002", "birth_certificate", "document_chunk",
        business_key_name="employee_id", business_key_value="EMP002",
        document_type="birth_certificate", content_kind="document_chunk")
    run("emp003 certificate chunks", "EMP003", "birth_certificate", "document_chunk",
        business_key_value="EMP003", document_type="birth_certificate",
        content_kind="document_chunk")
    run("emp001 certificate chunks", "EMP001", "birth_certificate", "document_chunk",
        business_key_value="EMP001", document_type="birth_certificate",
        content_kind="document_chunk")
    run("emp002 everything", "EMP002", None, None, business_key_value="EMP002")

    # -- document type --
    run("emp002 contract", "EMP002", "employment_contract", "document_chunk",
        business_key_value="EMP002", document_type="employment_contract",
        content_kind="document_chunk")
    run("emp002 photo", "EMP002", "profile_photo", "document_chunk",
        business_key_value="EMP002", document_type="profile_photo",
        content_kind="document_chunk")

    # -- content kind --
    run("emp002 structured only", "EMP002", None, "structured_record",
        business_key_value="EMP002", content_kind="structured_record")
    run("emp002 documents only", "EMP002", None, "document_chunk",
        business_key_value="EMP002", content_kind="document_chunk")

    # -- source field, independent of document type --
    run("emp002 by source field", "EMP002", None, "document_chunk",
        business_key_value="EMP002", source_field="birth_certificate",
        content_kind="document_chunk")

    # -- parent record --
    run("emp002 by parent record", "EMP002", None, None,
        parent_record_id="erp:legacy_hr:employees:emp002")

    # -- composite key --
    run("composite warehouse key", "WH-1|P-77", None, "structured_record",
        business_key_value="WH-1|P-77")

    # -- the original five --
    run("legacy filter: entity_type", None, None, None, entity_type="employees")
    run("legacy filter: source_system", None, None, None, source_system_id="legacy_hr")
    run("legacy filter: sensitivity", None, None, None, sensitivity="internal")

    # ---- HOT / WARM parity ----
    parity_failures = 0

    for query in (
        {"business_key_value": "EMP002", "content_kind": "document_chunk"},
        {"business_key_value": "EMP003", "document_type": "birth_certificate"},
        {"content_kind": "structured_record"},
        {"business_key_value": "WH-1|P-77"},
    ):
        def signature(source: Index):
            return sorted(
                (
                    h.state.business_key_value,
                    h.state.content_kind,
                    h.state.document_type,
                    h.state.chunk_index,
                )
                for h in source.find(**query)
            )

        if signature(index) != signature(warm):
            parity_failures += 1

    # ---- unknown filters ----
    refused = accepted = 0

    for bad in (
        {"employee_ssn": "x"}, {"salary": "1"}, {"text_for_ai": "x"},
        {"page_start": 1}, {"chunk_index": 0}, {"content_kind": "schema"},
        {"content_kind": "nonsense"},
    ):
        try:
            SearchFilters.from_mapping(bad)
            accepted += 1
        except (UnknownFilterFieldError, InvalidFilterValueError):
            refused += 1

    # ---- raw content in the payload ----
    surface = json.dumps(
        [_payload_for(state) for state in index.state.list_all()], default=str
    )
    leaks = [
        marker
        for marker in (
            "BIRTH CERTIFICATE", "EMPLOYMENT CONTRACT", "Registrar",
            "Nimal Silva", "%PDF", "JVBERi0x", "iVBORw0KGgo", "text_for_ai",
        )
        if marker in surface
    ]

    latencies = sorted(q["latency_ms"] for q in queries)
    percentile95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]

    gates_ok = (
        wrong_identity == 0
        and wrong_type == 0
        and kind_leak == 0
        and not leaks
        and accepted == 0
        and parity_failures == 0
        and incomplete_provenance == 0
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "representations_indexed": index.indexed,
            "employees": 3,
            "composite_key_records": 2,
            "shared_certificate_employees": ["EMP002", "EMP003"],
        },
        "contract": {
            "filterable_fields": list(FILTERABLE_FIELDS),
            "provenance_only_fields": list(PROVENANCE_ONLY_FIELDS),
        },
        "queries": queries,
        "gates": {
            "queries_attempted": len(queries),
            "wrong_identity_matches": wrong_identity,
            "wrong_document_type_matches": wrong_type,
            "content_kind_leakage": kind_leak,
            "incomplete_provenance": incomplete_provenance,
            "hot_warm_parity_failures": parity_failures,
            "unknown_filters_accepted": accepted,
            "unknown_filters_refused": refused,
            "raw_content_leakage": len(leaks),
            "leak_markers": leaks,
        },
        "latency_ms": {
            "median": round(statistics.median(latencies), 4),
            "p95": round(percentile95, 4),
            "max": round(latencies[-1], 4),
        },
        "passed": gates_ok,
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("=" * 68)
    print("PHASE 4 MINI-EVALUATION - identity-aware retrieval")
    print("=" * 68)
    print(f"representations indexed  {index.indexed}")
    print(f"filterable fields        {len(FILTERABLE_FIELDS)}")
    print(f"queries attempted        {len(queries)}")
    print()
    print(f"{'query':<32}{'hits':>6}{'bad-id':>8}{'bad-type':>10}{'ms':>9}")

    for query in queries:
        print(
            f"  {query['name']:<30}{query['hits']:>6}"
            f"{query['wrong_identity']:>8}{query['wrong_document_type']:>10}"
            f"{query['latency_ms']:>9.3f}"
        )

    print()
    print(f"wrong-identity matches      {wrong_identity}")
    print(f"wrong-document-type matches {wrong_type}")
    print(f"content-kind leakage        {kind_leak}")
    print(f"incomplete provenance       {incomplete_provenance}")
    print(f"HOT/WARM parity failures    {parity_failures}")
    print(f"unknown filters refused     {refused}  (accepted {accepted})")
    print(f"raw-content leakage         {len(leaks)} {leaks or ''}")
    print()
    print(f"latency  median {report['latency_ms']['median']:.3f} ms   "
          f"p95 {report['latency_ms']['p95']:.3f} ms")
    print()
    print(f"artifact -> {ARTIFACT.relative_to(ROOT)}")
    print("=" * 68)
    print(f"GATES: {'PASS' if gates_ok else 'FAIL'}")

    return 0 if gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
