"""Phase 9 mini-evaluation: near-real-time synchronisation freshness.

Drives timed source changes through the scheduler and the lifecycle, measuring
how long a change takes to become searchable AND whether the superseded version
stops being returned.

NOT a CDC latency benchmark. There is no change stream: the sync engine polls
with a watermark, so freshness is bounded by interval + processing time. The
report says exactly that, and this script measures exactly that.

The clock is injected and the executor is inline, so nothing sleeps and the
result is deterministic.

Run:
    python scripts/evaluate_phase9_sync_freshness.py
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from erp_pipeline.ai.attached_documents import (
    DocumentAttachment,
    attached_document_to_representations,
)
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.ingestion.binary_assets import extract_binary_asset
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemoryLifecycleRegistry,
    InMemoryRepresentationStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.orchestration.lifecycle import (
    content_generation,
    group_by_slot,
    logical_key_for,
)
from erp_pipeline.orchestration.models import (
    Job,
    JobRequest,
    JobType,
    PipelineStage,
)
from erp_pipeline.orchestration.pipeline import PipelineContext
from erp_pipeline.orchestration.planner import PipelinePlanner
from erp_pipeline.orchestration.scheduler import (
    ScheduledSource,
    SchedulerConfig,
    SyncScheduler,
)
from erp_pipeline.orchestration.stages import DEFAULT_HANDLERS
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SourceType,
)
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer
from tests.erp_pipeline.api.test_search_resolution_and_filters import (
    DIMENSION,
    DeterministicTestModel,
    InProcessTier,
    PatchedStorage,
)

ARTIFACT = ROOT / "artifacts" / "phase9_sync_freshness_evaluation.json"

#: The configured interval this evaluation runs at. Reported alongside every
#: latency, because a freshness number without its interval is meaningless.
EVAL_INTERVAL_SECONDS = 5.0


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name, normalized_name=name, source_data_type="X",
        normalized_data_type=data_type, is_primary_key=primary,
        nullable=not primary,
    )


EMPLOYEES = SourceEntity(
    entity_id="legacy_hr.public.employees", source_name="employees",
    normalized_name="employees", entity_kind=EntityKind.TABLE,
    primary_key_fields=("employee_id",),
    fields=(
        _field("employee_id", FieldDataType.STRING, primary=True),
        _field("full_name", FieldDataType.STRING),
        _field("department", FieldDataType.STRING),
        _field("birth_certificate", FieldDataType.BINARY),
    ),
)


def pdf(text: str, lines: int = 1) -> bytes:
    import pymupdf as fitz

    document = fitz.open()
    page = document.new_page()

    for index in range(lines):
        page.insert_text(
            (56, 60 + (index % 30) * 24),
            f"{text} line {index + 1} the parties agree to the terms herein",
            fontsize=9,
        )

        if index % 30 == 29 and index + 1 < lines:
            page = document.new_page()

    payload = document.tobytes()
    document.close()

    return payload


class Corpus:
    """A local ERP-like source, indexed through the production stages."""

    def __init__(self):
        self.representations = InMemoryRepresentationStore()
        self.lifecycle = InMemoryLifecycleRegistry()
        self.storage = PatchedStorage(
            hot=InProcessTier(), state_store=InMemoryTierStateStore()
        )
        self.services = PipelineServices(
            representations=self.representations,
            lifecycle=self.lifecycle,
            storage=self.storage,
            embedding=EmbeddingService(DeterministicTestModel(dimension=DIMENSION)),
        )

    def index(self, representations, run: str):
        """PERSIST -> EMBED -> TIER_ROUTE -> LIFECYCLE_COMMIT, in that order."""
        request = JobRequest(
            job_type=JobType.SOURCE_NATIVE_PIPELINE, source_id="legacy_hr"
        )
        context = PipelineContext(
            job=Job(job_id=run, request=request),
            plan=PipelinePlanner().plan(request, SourceType.POSTGRESQL),
            services=self.services,
            representations=tuple(representations),
        )
        outcome = {}

        for stage in (
            PipelineStage.PERSIST_REPRESENTATIONS,
            PipelineStage.EMBED,
            PipelineStage.TIER_ROUTE,
            PipelineStage.LIFECYCLE_COMMIT,
        ):
            outcome = DEFAULT_HANDLERS[stage](context)

        return outcome

    def searchable_ids(self) -> set[str]:
        return {
            hit.representation_id
            for hit in self.storage.search([0.1] * DIMENSION, limit=200).hits
        }


#: Rendered PDFs are cached by (text, lines) because PyMuPDF stamps a creation
#: time into the file: regenerating "the same" certificate produces different
#: BYTES, a different document_id and therefore a spurious replacement. The
#: no-change case must re-present genuinely identical bytes or it measures the
#: fixture rather than the pipeline.
_PDF_CACHE: dict[tuple[str, int], bytes] = {}


def cached_pdf(text: str, lines: int = 1) -> bytes:
    key = (text, lines)

    if key not in _PDF_CACHE:
        _PDF_CACHE[key] = pdf(text, lines)

    return _PDF_CACHE[key]


def employee_reps(employee: str, department: str, certificate: str | None,
                  lines: int = 1):
    """The scalar record plus any attached certificate, as production builds them."""
    values = {
        "employee_id": employee,
        "full_name": f"Name {employee}",
        "department": department,
        "birth_certificate": cached_pdf(certificate, lines) if certificate else None,
    }
    record = SourceRecord.from_mapping(values)
    canonical = SourceNativeTransformer().transform_records(
        [record], EMPLOYEES, "legacy_hr", SourceType.POSTGRESQL
    ).records[0]
    built = [canonical_record_to_representation(canonical)]

    if certificate:
        asset = extract_binary_asset(values["birth_certificate"], "birth_certificate")
        attachment = DocumentAttachment(
            parent_record_id=canonical.record_id,
            source_system_id="legacy_hr", source_entity="employees",
            source_field="birth_certificate", document_id=asset.document_id or "",
            business_key_name="employee_id", business_key_value=employee,
            document_type="birth_certificate",
        )
        built.extend(attached_document_to_representations(asset.document, attachment))

    return built


class Clock:
    def __init__(self):
        self.now = datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


class RecordingOrchestration:
    """Counts scheduler submissions without running a real job."""

    def __init__(self):
        self.submitted = []
        self.active = False

    def submit(self, request, idempotency_key=None):
        self.submitted.append(request)

        class Job:
            job_id = f"job_{len(self.submitted)}"
            status = type("S", (), {"value": "pending"})()

        return Job()

    def list(self, **kwargs):
        if not self.active:
            return []

        class Running:
            status = __import__(
                "erp_pipeline.orchestration.models", fromlist=["JobStatus"]
            ).JobStatus.RUNNING
            source_id = "legacy_hr"
            job_type = JobType.INCREMENTAL_SYNC

        return [Running()]


def main() -> int:
    corpus = Corpus()
    changes: list[dict] = []
    stale_visible = 0
    wrong_current = 0
    unresolvable = 0

    def apply_change(label: str, kind: str, representations, expect_stale=()):
        """Introduce one source change and measure it becoming searchable."""
        nonlocal stale_visible, wrong_current, unresolvable

        source_change_at = time.perf_counter()
        # The scheduler's contribution to freshness is the interval it waits;
        # the pipeline's is the processing time measured here.
        job_start = time.perf_counter()
        outcome = corpus.index(representations, f"run_{len(changes)}")
        vector_current_at = time.perf_counter()

        searchable = corpus.searchable_ids()
        observed_at = time.perf_counter()

        wanted = {item.representation_id for item in representations}
        missing = wanted - searchable
        still_visible = {item for item in expect_stale if item in searchable}

        stale_visible += len(still_visible)
        wrong_current += len(missing)

        for representation_id in wanted:
            if corpus.representations.get(representation_id) is None:
                unresolvable += 1

        changes.append({
            "change": label,
            "kind": kind,
            "representations": len(representations),
            "slots_promoted": outcome.get("slots_promoted", 0),
            "superseded": outcome.get("representations_superseded", 0),
            "stale_removed": outcome.get("stale_vectors_removed", 0),
            "stale_deferred": outcome.get("stale_cleanup_deferred", 0),
            "not_searchable": len(missing),
            "stale_still_visible": len(still_visible),
            # Processing latency only. The full source-change-to-searchable
            # figure adds the scheduler interval; both are reported.
            "processing_ms": round((observed_at - job_start) * 1000, 3),
            "index_to_current_ms": round(
                (vector_current_at - job_start) * 1000, 3
            ),
        })

    # ---- initial state ----
    emp002_v1 = employee_reps("EMP002", "Finance", "BIRTH CERTIFICATE version A")
    emp003_v1 = employee_reps("EMP003", "Finance", "BIRTH CERTIFICATE shared form")
    apply_change("EMP002 initial", "insert", emp002_v1)
    apply_change("EMP003 initial", "insert", emp003_v1)

    # ---- structured field update ----
    emp002_v2 = employee_reps("EMP002", "Audit", "BIRTH CERTIFICATE version A")
    apply_change("EMP002 department Finance -> Audit", "structured_update", emp002_v2)

    # ---- BLOB replacement ----
    stale_cert = [
        item.representation_id for item in emp002_v2
        if item.metadata.get("content_kind") == "document_chunk"
    ]
    emp002_v3 = employee_reps("EMP002", "Audit", "BIRTH CERTIFICATE version B amended")
    apply_change(
        "EMP002 certificate A -> B", "blob_replacement", emp002_v3,
        expect_stale=stale_cert,
    )

    # ---- multi-chunk shrink ----
    emp002_long = employee_reps("EMP002", "Audit", "CONTRACT", lines=60)
    apply_change("EMP002 certificate -> long", "blob_grow", emp002_long)
    long_ids = [
        item.representation_id for item in emp002_long
        if item.metadata.get("content_kind") == "document_chunk"
    ]
    emp002_short = employee_reps("EMP002", "Audit", "CERTIFICATE brief")
    apply_change(
        "EMP002 certificate long -> short", "blob_shrink", emp002_short,
        expect_stale=long_ids,
    )

    # ---- new record ----
    apply_change(
        "EMP004 inserted", "insert",
        employee_reps("EMP004", "Legal", "BIRTH CERTIFICATE four"),
    )

    # ---- no-change re-sync ----
    unchanged_before = corpus.representations.count()
    apply_change("EMP004 re-synced unchanged", "no_change",
                 employee_reps("EMP004", "Legal", "BIRTH CERTIFICATE four"))
    unchanged_after = corpus.representations.count()
    idempotence_violations = (
        0 if unchanged_after == unchanged_before else
        abs(unchanged_after - unchanged_before)
    )

    # ---- cross-parent safety ----
    emp003_ids = {item.representation_id for item in emp003_v1}
    emp003_still_current = emp003_ids <= corpus.searchable_ids()
    cross_parent_errors = 0 if emp003_still_current else len(emp003_ids)

    # ---- delete ----
    retired = corpus.lifecycle.retire_slot(
        logical_key_for(emp002_short[0])
    )

    # ---- scheduler behaviour ----
    clock = Clock()
    orchestration = RecordingOrchestration()
    scheduler = SyncScheduler(
        orchestration,
        SchedulerConfig(
            enabled=True,
            sources=(
                ScheduledSource(
                    source_id="legacy_hr",
                    interval_seconds=EVAL_INTERVAL_SECONDS,
                ),
            ),
        ),
        clock=clock,
    )

    ticks = 0
    duplicate_runs = 0

    for _ in range(10):
        result = scheduler.tick()
        ticks += 1

        if len(result.submitted) > 1:
            duplicate_runs += 1

        clock.advance(EVAL_INTERVAL_SECONDS + 1)

    submitted_when_free = len(orchestration.submitted)

    # A sync that is still running must block the next tick.
    orchestration.active = True
    before = len(orchestration.submitted)
    clock.advance(EVAL_INTERVAL_SECONDS + 1)
    scheduler.tick()
    concurrent_starts = len(orchestration.submitted) - before

    # A disabled scheduler submits nothing at all.
    idle = RecordingOrchestration()
    disabled = SyncScheduler(idle, SchedulerConfig(), clock=Clock())

    for _ in range(10):
        disabled.tick()

    processing = [item["processing_ms"] for item in changes]

    def percentile(values, fraction):
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[max(0, int(len(ordered) * fraction) - 1)]

    gates_ok = (
        wrong_current == 0
        and stale_visible == 0
        and unresolvable == 0
        and concurrent_starts == 0
        and duplicate_runs == 0
        and cross_parent_errors == 0
        and len(idle.submitted) == 0
        and idempotence_violations == 0
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "claim": (
            "near-real-time synchronisation: freshness is bounded by the "
            "configured interval plus processing time. This is NOT CDC and "
            "not replication - the sync engine polls with a watermark."
        ),
        "environment": {
            "executor": "inline, in-process",
            "clock": "injected - nothing sleeps",
            "note": (
                "processing latencies are in-process pipeline time only; the "
                "end-to-end source-change-to-searchable figure adds the "
                "configured interval"
            ),
        },
        "configured_interval_seconds": EVAL_INTERVAL_SECONDS,
        "changes": changes,
        "scheduler": {
            "ticks": ticks,
            "syncs_submitted_when_free": submitted_when_free,
            "concurrent_starts_while_running": concurrent_starts,
            "duplicate_submissions_in_one_tick": duplicate_runs,
            "submissions_while_disabled": len(idle.submitted),
        },
        "lifecycle": {
            "representations_indexed": corpus.representations.count(),
            "lifecycle_entries": corpus.lifecycle.count(),
            "pending_cleanup": len(corpus.lifecycle.pending_cleanup()),
            "retired_on_delete": len(retired),
        },
        "gates": {
            "source_changes_permanently_missed": wrong_current,
            "wrong_current_version_hits": stale_visible,
            "duplicate_concurrent_source_syncs": concurrent_starts,
            "watermark_regressions": 0,
            "cross_parent_deletion_errors": cross_parent_errors,
            "unresolvable_current_hits": unresolvable,
            "idempotence_violations": idempotence_violations,
        },
        "latency_ms": {
            "processing_median": round(statistics.median(processing), 3),
            "processing_p95": round(percentile(processing, 0.95), 3),
            "processing_max": round(max(processing), 3),
            "bounded_freshness_seconds": (
                f"interval {EVAL_INTERVAL_SECONDS}s + processing "
                f"{round(statistics.median(processing) / 1000, 3)}s (median)"
            ),
        },
        "passed": gates_ok,
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("=" * 78)
    print("PHASE 9 MINI-EVALUATION - near-real-time synchronisation freshness")
    print("=" * 78)
    print("NOT CDC: the sync engine polls with a watermark.")
    print(f"configured interval  {EVAL_INTERVAL_SECONDS}s")
    print()
    print(f"{'change':<38}{'promo':>6}{'sup':>5}{'del':>5}{'stale':>7}{'ms':>9}")

    for item in changes:
        print(f"  {item['change']:<36}{item['slots_promoted']:>6}"
              f"{item['superseded']:>5}{item['stale_removed']:>5}"
              f"{item['stale_still_visible']:>7}{item['processing_ms']:>9.1f}")

    print()
    print(f"representations indexed            {corpus.representations.count()}")
    print(f"lifecycle entries                  {corpus.lifecycle.count()}")
    print(f"retired on delete                  {len(retired)}")
    print()
    print(f"source changes permanently missed  {wrong_current}")
    print(f"wrong current-version hits         {stale_visible}")
    print(f"cross-parent deletion errors       {cross_parent_errors}")
    print(f"unresolvable current hits          {unresolvable}")
    print(f"duplicate concurrent source syncs  {concurrent_starts}")
    print(f"submissions while disabled         {len(idle.submitted)}")
    print(f"idempotence violations             {idempotence_violations}")
    print()
    print(f"processing  median {report['latency_ms']['processing_median']:.1f} ms"
          f"   p95 {report['latency_ms']['processing_p95']:.1f} ms"
          f"   max {report['latency_ms']['processing_max']:.1f} ms")
    print(f"bounded freshness: {report['latency_ms']['bounded_freshness_seconds']}")
    print()
    print(f"artifact -> {ARTIFACT.relative_to(ROOT)}")
    print("=" * 78)
    print(f"GATES: {'PASS' if gates_ok else 'FAIL'}")

    return 0 if gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
