"""Phase 5 mini-evaluation: can every search hit be turned back into its text?

Builds a deterministic corpus, searches it with identity filters, resolves every
hit through the representation store, and counts the ways the answer could be
wrong: an unresolvable hit, the wrong text, the wrong employee, the wrong
document type, or a chunk whose provenance does not match what it returned.

The store is exercised on a REAL on-disk database rather than a dict, because a
dictionary would pass every assertion here and still lose the corpus on restart
- which is the defect being fixed.

Run:
    python scripts/evaluate_phase5_representation_resolution.py
"""

from __future__ import annotations

import base64
import io
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

import sqlalchemy as sa

from erp_pipeline.ai.attached_documents import (
    DocumentAttachment,
    attached_document_to_representations,
)
from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import _carried_identity
from erp_pipeline.ingestion.binary_assets import extract_binary_asset
from erp_pipeline.ingestion.ocr import probe_ocr
from erp_pipeline.orchestration.representation_store import (
    PostgresRepresentationStore,
    create_representations_sql,
)
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SensitivityLevel,
    SourceType,
)
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.storage.filters import SearchFilters
from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import TierSet, _payload_for
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync.hashing import vector_id_for
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

ARTIFACT = ROOT / "artifacts" / "phase5_representation_resolution_evaluation.json"
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


def _pdf(lines: list[str], pages: int = 1) -> bytes:
    import pymupdf as fitz

    document = fitz.open()

    for page_number in range(pages):
        page = document.new_page()

        for index, line in enumerate(lines):
            page.insert_text(
                (56, 66 + index * 22), f"{line} [page {page_number + 1}]", fontsize=10
            )

    payload = document.tobytes()
    document.close()

    return payload


def _long_pdf() -> bytes:
    """Long enough to produce several chunks."""
    import pymupdf as fitz

    document = fitz.open()

    for page_number in range(3):
        page = document.new_page()

        for line in range(30):
            page.insert_text(
                (56, 60 + line * 24),
                f"CONTRACT PAGE {page_number + 1} CLAUSE {line + 1} "
                "the parties agree to the terms set out herein",
                fontsize=9,
            )

    payload = document.tobytes()
    document.close()

    return payload


def _certificate_image() -> bytes:
    """A picture of certificate text - the OCR path."""
    import pymupdf as fitz

    typed = fitz.open()
    typed.new_page(width=420, height=200).insert_text(
        (30, 100), "BIRTH CERTIFICATE", fontsize=26
    )
    bitmap = typed.load_page(0).get_pixmap(dpi=300).tobytes("png")
    typed.close()

    return bitmap


def _blank_png() -> bytes:
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


class Corpus:
    """Indexed through the production write path, into a real database."""

    def __init__(self, workspace: Path) -> None:
        runtime_file = workspace / "erp_runtime.sqlite"
        main_file = workspace / "main.sqlite"

        def connect():
            engine = sa.create_engine(f"sqlite:///{main_file}")

            @sa.event.listens_for(engine, "connect")
            def _attach(dbapi_connection, _record):  # noqa: ANN001
                dbapi_connection.execute(
                    f"ATTACH DATABASE '{runtime_file}' AS erp_runtime"
                )

            return engine

        self._connect = connect
        engine = connect()

        with engine.begin() as connection:
            connection.execute(sa.text(create_representations_sql()))

        self.representations = PostgresRepresentationStore(engine)
        self.state = InMemoryTierStateStore()
        self.hot = FilterAwareTier()
        self.store = HybridVectorStore(TierSet(hot=self.hot), self.state)
        self.raw_blobs: list[bytes] = []
        self.expected: dict[str, dict] = {}

    def reconnect(self) -> PostgresRepresentationStore:
        """A fresh engine on the same files - a restart."""
        return PostgresRepresentationStore(self._connect())

    def _index(self, representation) -> None:
        # The pipeline's order: persist FIRST, then make it searchable.
        self.representations.upsert(representation)
        self.store.store(
            EmbeddingRecord(
                embedding_id=f"emb.{representation.representation_id}",
                representation_id=representation.representation_id,
                entity_type=representation.entity_type,
                content_hash=representation.resolved_hash(),
                model_id="eval-model",
                dimension=len(VECTOR),
                status=EmbeddingStatus.GENERATED,
                vector=tuple(VECTOR),
                metadata=_carried_identity(representation),
            ),
            sensitivity=SensitivityLevel.INTERNAL,
        )
        self.expected[representation.representation_id] = {
            "text": representation.text_for_ai,
            "parent": representation.metadata.get("parent_record_id")
            or representation.metadata.get("canonical_record_id"),
            "business_key_value": representation.metadata.get("business_key_value"),
            "document_type": representation.metadata.get("document_type"),
            "content_kind": representation.metadata.get("content_kind"),
            "page_start": representation.metadata.get("page_start"),
            "chunk_index": representation.metadata.get("chunk_index"),
        }

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

        self._index(canonical_record_to_representation(canonical))

        for field_name, payload in blobs.items():
            if payload is None:
                continue

            self.raw_blobs.append(payload)
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
                self._index(representation)

    def find(self, **filters):
        return self.store.search(
            VECTOR, limit=100, filters=SearchFilters.from_mapping(filters)
        ).hits


def main() -> int:
    import tempfile

    workspace = Path(tempfile.mkdtemp(prefix="phase5_eval_"))
    corpus = Corpus(workspace)

    shared_certificate = _pdf(
        ["BIRTH CERTIFICATE", "Registrar General Colombo", "Serial BC-4471"]
    )

    corpus.add_employee(
        "EMP001", "Sunil Bandara", birth_certificate=_pdf(["BIRTH CERTIFICATE One"])
    )
    corpus.add_employee(
        "EMP002",
        "Nimal Silva",
        birth_certificate=_certificate_image(),
        employment_contract=_pdf(["EMPLOYMENT CONTRACT", "Senior Accountant"]),
        profile_photo=_blank_png(),
    )
    corpus.add_employee("EMP003", "Amal Perera", birth_certificate=shared_certificate)
    # EMP003's certificate bytes reused on a fourth employee.
    corpus.add_employee("EMP004", "Kamala Fernando", birth_certificate=shared_certificate)
    corpus.add_employee("EMP009", "Multi Page", employment_contract=_long_pdf())

    # Warm the lazy qdrant-client import so it is not timed as retrieval.
    corpus.find(business_key_value="__warmup__")
    corpus.representations.get("__warmup__")

    queries: list[dict] = []
    attempted = resolved = 0
    unresolvable = wrong_text = wrong_parent = wrong_type = provenance_mismatch = 0
    lookup_times: list[float] = []
    combined_times: list[float] = []

    def run(name: str, **filters):
        nonlocal attempted, resolved, unresolvable
        nonlocal wrong_text, wrong_parent, wrong_type, provenance_mismatch

        started = time.perf_counter()
        hits = corpus.find(**filters)
        search_ms = (time.perf_counter() - started) * 1000

        local_bad = {"unresolvable": 0, "text": 0, "parent": 0, "type": 0, "prov": 0}

        for hit in hits:
            attempted += 1
            expected = corpus.expected[hit.representation_id]

            lookup_started = time.perf_counter()
            stored = corpus.representations.get(hit.representation_id)
            lookup_ms = (time.perf_counter() - lookup_started) * 1000
            lookup_times.append(lookup_ms)
            combined_times.append(search_ms + lookup_ms)

            if stored is None:
                unresolvable += 1
                local_bad["unresolvable"] += 1
                continue

            resolved += 1

            if stored.text_for_ai != expected["text"]:
                wrong_text += 1
                local_bad["text"] += 1

            parent = stored.metadata.get("parent_record_id") or stored.metadata.get(
                "canonical_record_id"
            )

            if parent != expected["parent"]:
                wrong_parent += 1
                local_bad["parent"] += 1

            if stored.metadata.get("document_type") != expected["document_type"]:
                wrong_type += 1
                local_bad["type"] += 1

            if (
                stored.metadata.get("page_start") != expected["page_start"]
                or stored.metadata.get("chunk_index") != expected["chunk_index"]
            ):
                provenance_mismatch += 1
                local_bad["prov"] += 1

        queries.append(
            {
                "name": name,
                "filters": filters,
                "hits": len(hits),
                "search_ms": round(search_ms, 4),
                **{f"wrong_{key}": value for key, value in local_bad.items()},
            }
        )

        return hits

    run("emp002 certificate", business_key_value="EMP002",
        document_type="birth_certificate", content_kind="document_chunk")
    run("emp002 contract", business_key_value="EMP002",
        document_type="employment_contract", content_kind="document_chunk")
    run("emp002 structured", business_key_value="EMP002",
        content_kind="structured_record")
    run("emp002 everything", business_key_value="EMP002")
    run("emp003 certificate", business_key_value="EMP003",
        document_type="birth_certificate")
    run("emp004 shared certificate", business_key_value="EMP004",
        document_type="birth_certificate")
    run("emp009 multi-chunk contract", business_key_value="EMP009",
        content_kind="document_chunk")
    run("all document chunks", content_kind="document_chunk")
    run("all structured records", content_kind="structured_record")
    run("whole corpus")

    # ---- shared document: same text, different association ----
    shared_hits = [
        corpus.representations.get(hit.representation_id)
        for employee in ("EMP003", "EMP004")
        for hit in corpus.find(
            business_key_value=employee, document_type="birth_certificate"
        )
    ]
    association_collapse = 0

    if len(shared_hits) == 2:
        first, second = shared_hits

        if first.text_for_ai != second.text_for_ai:
            association_collapse += 1  # should be identical - same document
        if first.metadata["parent_record_id"] == second.metadata["parent_record_id"]:
            association_collapse += 1  # must NOT be identical

    # ---- multi-chunk: no chunk returns another chunk's text ----
    chunk_hits = corpus.find(business_key_value="EMP009", content_kind="document_chunk")
    chunk_texts = [
        corpus.representations.get(hit.representation_id).text_for_ai
        for hit in chunk_hits
    ]
    chunk_crosstalk = len(chunk_texts) - len(set(chunk_texts))

    # ---- durability ----
    after_restart = corpus.reconnect()
    sample = next(iter(corpus.expected))
    survived = after_restart.get(sample) is not None

    # ---- leakage ----
    stored_surface = json.dumps(
        [
            {
                "text": corpus.representations.get(key).text_for_ai,
                "metadata": dict(corpus.representations.get(key).metadata),
            }
            for key in corpus.expected
        ],
        default=str,
    )
    binary_leaks = [
        name
        for name, marker in (
            ("base64 blob prefix", None),
            ("base64 pdf header", "JVBERi0x"),
            ("base64 png header", "iVBORw0KGgo"),
            ("base64 jpeg header", "/9j/4AAQ"),
            ("raw pdf header", "%PDF-"),
        )
        if marker is not None and marker in stored_surface
    ]

    for payload in corpus.raw_blobs:
        if base64.b64encode(payload).decode()[:24] in stored_surface:
            binary_leaks.append("base64 blob prefix")
            break

    # ---- the vector payload must still carry no text ----
    payload_surface = json.dumps(
        [_payload_for(state) for state in corpus.state.list_all()], default=str
    )
    qdrant_text_leak = any(
        marker in payload_surface
        for marker in ("BIRTH CERTIFICATE", "EMPLOYMENT CONTRACT", "text_for_ai")
    )

    ocr = probe_ocr()
    gates_ok = (
        unresolvable == 0
        and wrong_text == 0
        and wrong_parent == 0
        and wrong_type == 0
        and provenance_mismatch == 0
        and not binary_leaks
        and not qdrant_text_leak
        and association_collapse == 0
        and chunk_crosstalk == 0
        and survived
    )

    def percentile(values, fraction):
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[max(0, int(len(ordered) * fraction) - 1)]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"ocr_available": ocr.available},
        "corpus": {
            "representations_indexed": len(corpus.expected),
            "employees": 5,
            "shared_certificate_employees": ["EMP003", "EMP004"],
            "store": "sqlite (erp_runtime attached), production SQL unchanged",
        },
        "queries": queries,
        "gates": {
            "search_hits_attempted": attempted,
            "search_hits_resolved": resolved,
            "unresolvable_hits": unresolvable,
            "wrong_text_resolutions": wrong_text,
            "wrong_parent_identities": wrong_parent,
            "wrong_document_types": wrong_type,
            "chunk_provenance_mismatches": provenance_mismatch,
            "association_collapse": association_collapse,
            "chunk_crosstalk": chunk_crosstalk,
            "raw_binary_leakage": len(binary_leaks),
            "leak_markers": binary_leaks,
            "text_in_qdrant_payload": qdrant_text_leak,
            "survived_restart": survived,
        },
        "latency_ms": {
            "representation_lookup_median": round(
                statistics.median(lookup_times) if lookup_times else 0.0, 4
            ),
            "representation_lookup_p95": round(percentile(lookup_times, 0.95), 4),
            "search_and_resolve_median": round(
                statistics.median(combined_times) if combined_times else 0.0, 4
            ),
            "search_and_resolve_p95": round(percentile(combined_times, 0.95), 4),
        },
        "passed": gates_ok,
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("=" * 70)
    print("PHASE 5 MINI-EVALUATION - representation content resolution")
    print("=" * 70)
    print(f"representations indexed  {len(corpus.expected)}")
    print(f"OCR available            {ocr.available}")
    print()
    print(f"{'query':<34}{'hits':>6}{'unres':>7}{'badtxt':>8}{'ms':>9}")

    for query in queries:
        print(
            f"  {query['name']:<32}{query['hits']:>6}"
            f"{query['wrong_unresolvable']:>7}{query['wrong_text']:>8}"
            f"{query['search_ms']:>9.3f}"
        )

    print()
    print(f"search hits attempted        {attempted}")
    print(f"search hits resolved         {resolved}")
    print(f"unresolvable hits            {unresolvable}")
    print(f"wrong text resolutions       {wrong_text}")
    print(f"wrong parent identities      {wrong_parent}")
    print(f"wrong document types         {wrong_type}")
    print(f"chunk provenance mismatches  {provenance_mismatch}")
    print(f"association collapse         {association_collapse}")
    print(f"chunk crosstalk              {chunk_crosstalk}")
    print(f"raw binary / base64 leakage  {len(binary_leaks)} {binary_leaks or ''}")
    print(f"text in Qdrant payload       {qdrant_text_leak}")
    print(f"survived restart             {survived}")
    print()
    print(
        "representation lookup  median "
        f"{report['latency_ms']['representation_lookup_median']:.3f} ms   "
        f"p95 {report['latency_ms']['representation_lookup_p95']:.3f} ms"
    )
    print(
        "search + resolve       median "
        f"{report['latency_ms']['search_and_resolve_median']:.3f} ms   "
        f"p95 {report['latency_ms']['search_and_resolve_p95']:.3f} ms"
    )
    print()
    print(f"artifact -> {ARTIFACT.relative_to(ROOT)}")
    print("=" * 70)
    print(f"GATES: {'PASS' if gates_ok else 'FAIL'}")

    return 0 if gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
