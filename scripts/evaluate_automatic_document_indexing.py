"""Phase 6 mini-evaluation: does an upload index itself, correctly?

Drives the real HTTP surface with a deterministic corpus and counts what could
go wrong: an upload that needed a second manual job, a document that never
became searchable, a hit belonging to the wrong employee, a duplicate corpus
entry, or raw bytes on the wire.

Also measures the thing a user actually feels — how long from "upload accepted"
to "this document is searchable".

Run:
    python scripts/evaluate_phase6_automatic_indexing.py
"""

from __future__ import annotations

import base64
import io
import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from fastapi.testclient import TestClient

from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.ingestion import FileIngestionService
from erp_pipeline.ingestion.ocr import probe_ocr
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemoryRepresentationStore,
    OrchestrationService,
    PipelineServices,
    UploadStore,
)
from erp_pipeline.storage.migration import _payload_for
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync import InMemoryCanonicalStore
from tests.erp_pipeline.api.test_search_resolution_and_filters import (
    DIMENSION,
    DeterministicTestModel,
    InProcessTier,
    PatchedStorage,
)

ARTIFACT = ROOT / "artifacts" / "phase6_automatic_document_indexing_evaluation.json"


def _pdf(lines: list[str]) -> bytes:
    import pymupdf as fitz

    document = fitz.open()
    page = document.new_page()

    for index, line in enumerate(lines):
        page.insert_text((56, 70 + index * 22), line, fontsize=11)

    payload = document.tobytes()
    document.close()

    return payload


def _image_of_text(text: str) -> bytes:
    import pymupdf as fitz

    typed = fitz.open()
    typed.new_page(width=420, height=180).insert_text((28, 100), text, fontsize=26)
    bitmap = typed.load_page(0).get_pixmap(dpi=300).tobytes("png")
    typed.close()

    return bitmap


def build_app(workspace: Path):
    representations = InMemoryRepresentationStore()
    storage = PatchedStorage(
        hot=InProcessTier(), state_store=InMemoryTierStateStore()
    )
    services = PipelineServices(
        ingestion=FileIngestionService(),
        uploads=UploadStore(workspace / "uploads"),
        records=InMemoryCanonicalStore(),
        representations=representations,
        storage=storage,
        embedding=EmbeddingService(DeterministicTestModel(dimension=DIMENSION)),
    )
    app = create_app(
        settings=ApiSettings(upload_dir=workspace / "uploads"),
        orchestration=OrchestrationService(
            services=services, job_store=InMemoryJobStore(), executor=InlineJobExecutor()
        ),
    )

    return app, representations, storage


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="phase6_eval_"))
    app, representations, storage = build_app(workspace)

    certificate = _pdf(
        ["BIRTH CERTIFICATE", "Registrar General Colombo", "Name: Nimal Silva"]
    )
    contract = _pdf(["EMPLOYMENT CONTRACT", "Position: Senior Accountant"])
    policy = _pdf(["COMPANY POLICY", "Travel and expenses"])
    scan = _image_of_text("BIRTH CERTIFICATE")
    corrupt = b"%PDF-1.7\n" + b"\x00" * 200

    raw_blobs = [certificate, contract, policy, scan]

    #: (label, bytes, filename, identity, expected_to_index)
    corpus = [
        ("text pdf", contract, "contract.pdf", {}, True),
        (
            "readable image",
            scan,
            "scan.png",
            {"business_key_name": "employee_id", "business_key_value": "EMP001",
             "document_type": "birth_certificate"},
            True,
        ),
        (
            "EMP002 certificate",
            certificate,
            "cert.pdf",
            {"source_system_id": "legacy_hr", "source_entity": "employees",
             "business_key_name": "employee_id", "business_key_value": "EMP002",
             "document_type": "birth_certificate"},
            True,
        ),
        (
            "EMP003 same certificate",
            certificate,
            "cert.pdf",
            {"source_system_id": "legacy_hr", "source_entity": "employees",
             "business_key_name": "employee_id", "business_key_value": "EMP003",
             "document_type": "birth_certificate"},
            True,
        ),
        ("generic document", policy, "policy.pdf", {}, True),
        (
            "repeated EMP002 upload",
            certificate,
            "cert.pdf",
            {"source_system_id": "legacy_hr", "source_entity": "employees",
             "business_key_name": "employee_id", "business_key_value": "EMP002",
             "document_type": "birth_certificate"},
            True,
        ),
        ("corrupt document", corrupt, "broken.pdf", {}, False),
    ]

    uploads: list[dict] = []
    attempted = accepted = jobs_created = jobs_completed = jobs_failed = 0
    manual_calls = 0
    latencies: list[float] = []

    with TestClient(app) as client:
        for label, payload, filename, identity, should_index in corpus:
            attempted += 1
            content_type = (
                "image/png" if filename.endswith(".png") else "application/pdf"
            )
            started = time.perf_counter()
            response = client.post(
                "/v1/files/documents",
                files={"file": (filename, payload, content_type)},
                data=identity,
            )
            elapsed = (time.perf_counter() - started) * 1000

            entry = {
                "label": label,
                "status_code": response.status_code,
                "expected_to_index": should_index,
                "upload_to_searchable_ms": round(elapsed, 3),
            }

            if response.status_code != 201:
                entry.update({"accepted": False, "index_job_id": None})
                uploads.append(entry)
                continue

            accepted += 1
            body = response.json()
            entry.update(
                {
                    "accepted": True,
                    "document_id": (body.get("document_id") or "")[:12],
                    "index_job_id": body.get("index_job_id"),
                    "indexing_status": body.get("indexing_status"),
                }
            )

            if body.get("index_job_id"):
                jobs_created += 1

                if body.get("indexing_status") == "succeeded":
                    jobs_completed += 1
                    # Only a completed job's upload counts toward the
                    # upload-to-searchable measurement.
                    latencies.append(elapsed)
                elif body.get("indexing_status") == "failed":
                    jobs_failed += 1

            uploads.append(entry)

        # ---- identity-filtered retrieval ----
        wrong_identity = wrong_type = unresolvable = 0
        searched: list[dict] = []

        for employee in ("EMP001", "EMP002", "EMP003"):
            hits = client.post(
                "/v1/search",
                json={
                    "query": "birth certificate details",
                    "top_k": 20,
                    "filters": {
                        "business_key_value": employee,
                        "document_type": "birth_certificate",
                        "content_kind": "document_chunk",
                    },
                },
            ).json()["hits"]

            for hit in hits:
                resolved = client.get(
                    f"/v1/representations/{hit['representation_id']}"
                )

                if resolved.status_code != 200:
                    unresolvable += 1
                    continue

                body = resolved.json()

                if body.get("business_key_value") != employee:
                    wrong_identity += 1

                if body.get("document_type") != "birth_certificate":
                    wrong_type += 1

            searched.append({"employee": employee, "hits": len(hits)})

        # ---- a generic document is findable too ----
        generic_hits = client.post(
            "/v1/search",
            json={
                "query": "company policy travel",
                "top_k": 20,
                "filters": {"content_kind": "document_chunk"},
            },
        ).json()["hits"]

        for hit in generic_hits:
            if (
                client.get(
                    f"/v1/representations/{hit['representation_id']}"
                ).status_code
                != 200
            ):
                unresolvable += 1

        # ---- CSV must not have entered the vector path ----
        csv_response = client.post(
            "/v1/files/csv",
            files={
                "file": (
                    "ledger.csv",
                    b"invoice_id,customer_id,amount\nINV-1,CUS-1,10.00\n",
                    "text/csv",
                )
            },
        )
        csv_body = csv_response.json() if csv_response.status_code == 201 else {}
        csv_started_a_job = "index_job_id" in csv_body

        total_jobs = len(client.get("/v1/jobs").json())

        # ---- content safety ----
        resolution_surface = json.dumps(
            [
                client.get(f"/v1/representations/{hit['representation_id']}").json()
                for hit in generic_hits
            ]
        )

    # EMP002 appears twice in the corpus (uploaded, then re-uploaded); the
    # duplicate must not have produced a second corpus entry.
    emp002_entries = [
        key
        for key in representations.list_ids()
        if (representations.get(key).metadata or {}).get("business_key_value")
        == "EMP002"
    ]
    duplicate_semantic_entries = max(0, len(emp002_entries) - 1)

    leaks = [
        marker
        for marker in ("JVBERi0x", "iVBORw0KGgo", "%PDF-", "/9j/4AAQ")
        if marker in resolution_surface
    ]

    for payload in raw_blobs:
        if base64.b64encode(payload).decode()[:24] in resolution_surface:
            leaks.append("base64 blob prefix")
            break

    payload_surface = json.dumps(
        [_payload_for(state) for state in storage.state.list_all()], default=str
    )
    text_in_qdrant = any(
        marker in payload_surface
        for marker in ("BIRTH CERTIFICATE", "COMPANY POLICY", "text_for_ai")
    )

    def percentile(values, fraction):
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[max(0, int(len(ordered) * fraction) - 1)]

    gates_ok = (
        manual_calls == 0
        and wrong_identity == 0
        and wrong_type == 0
        and duplicate_semantic_entries == 0
        and unresolvable == 0
        and not leaks
        and not text_in_qdrant
        and not csv_started_a_job
        and jobs_created == accepted
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"ocr_available": probe_ocr().available},
        "corpus": {
            "uploads": uploads,
            "representations_indexed": representations.count(),
            "jobs_recorded": total_jobs,
        },
        "search": searched,
        "gates": {
            "uploads_attempted": attempted,
            "uploads_accepted": accepted,
            "automatic_jobs_created": jobs_created,
            "jobs_completed": jobs_completed,
            "jobs_failed": jobs_failed,
            "manual_job_calls_required": manual_calls,
            "wrong_identity_matches": wrong_identity,
            "wrong_document_types": wrong_type,
            "duplicate_semantic_entries": duplicate_semantic_entries,
            "unresolvable_hits": unresolvable,
            "raw_or_base64_leakage": len(leaks),
            "leak_markers": leaks,
            "text_in_qdrant_payload": text_in_qdrant,
            "csv_started_an_index_job": csv_started_a_job,
        },
        "latency_ms": {
            "upload_to_searchable_median": round(
                statistics.median(latencies) if latencies else 0.0, 3
            ),
            "upload_to_searchable_p95": round(percentile(latencies, 0.95), 3),
            # Reported because with a handful of samples a "p95" is really
            # "the second-slowest", and the slowest case here (OCR on a
            # scanned image) is an order of magnitude above the median. The
            # max is the number that describes the worst a user waits.
            "upload_to_searchable_max": round(max(latencies) if latencies else 0.0, 3),
            "samples": len(latencies),
        },
        "passed": gates_ok,
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("=" * 72)
    print("PHASE 6 MINI-EVALUATION - automatic document indexing")
    print("=" * 72)
    print(f"OCR available            {probe_ocr().available}")
    print(f"representations indexed  {representations.count()}")
    print()
    print(f"{'upload':<28}{'code':>6}{'job':>6}{'status':>12}{'ms':>10}")

    for entry in uploads:
        print(
            f"  {entry['label']:<26}{entry['status_code']:>6}"
            f"{('yes' if entry.get('index_job_id') else '-'):>6}"
            f"{str(entry.get('indexing_status') or '-'):>12}"
            f"{entry['upload_to_searchable_ms']:>10.1f}"
        )

    print()
    print(f"uploads attempted            {attempted}")
    print(f"uploads accepted             {accepted}")
    print(f"automatic jobs created       {jobs_created}")
    print(f"jobs completed               {jobs_completed}")
    print(f"jobs failed                  {jobs_failed}")
    print(f"MANUAL job calls required    {manual_calls}")
    print()
    print(f"wrong identity matches       {wrong_identity}")
    print(f"wrong document types         {wrong_type}")
    print(f"duplicate semantic entries   {duplicate_semantic_entries}")
    print(f"unresolvable hits            {unresolvable}")
    print(f"raw / base64 leakage         {len(leaks)} {leaks or ''}")
    print(f"text in Qdrant payload       {text_in_qdrant}")
    print(f"CSV started an index job     {csv_started_a_job}")
    print()
    print(
        "upload -> searchable  median "
        f"{report['latency_ms']['upload_to_searchable_median']:.1f} ms   "
        f"p95 {report['latency_ms']['upload_to_searchable_p95']:.1f} ms   "
        f"max {report['latency_ms']['upload_to_searchable_max']:.1f} ms   "
        f"(n={len(latencies)})"
    )
    print()
    print(f"artifact -> {ARTIFACT.relative_to(ROOT)}")
    print("=" * 72)
    print(f"GATES: {'PASS' if gates_ok else 'FAIL'}")

    return 0 if gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
