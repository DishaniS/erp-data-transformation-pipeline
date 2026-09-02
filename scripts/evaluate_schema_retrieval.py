"""Phase 7 mini-evaluation: can the system answer questions about its own structure?

Builds a deterministic four-system schema corpus, runs a fixed query set with
known target entities, and reports Recall@1, Recall@3 and MRR alongside the
safety gates.

The query set is FIXED BEFORE the run and is not edited afterwards. Queries
that fail are reported as failures; no vocabulary is tuned to recover them,
because a retrieval number produced by adjusting the corpus until the queries
pass measures nothing.

Uses the real 384-dimensional embedding model. A deterministic stand-in would
make every recall figure meaningless.

Run:
    python scripts/evaluate_phase7_schema_retrieval.py
"""

from __future__ import annotations

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

from erp_pipeline.ai.embedding import SentenceTransformerModel
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemoryRepresentationStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    RelationshipType,
    SchemaOrigin,
)
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
)
from erp_pipeline.storage.migration import _payload_for
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync import InMemoryCanonicalStore
from tests.erp_pipeline.api.test_search_resolution_and_filters import (
    InProcessTier,
    PatchedStorage,
)

ARTIFACT = ROOT / "artifacts" / "phase7_schema_retrieval_evaluation.json"

TEXT = FieldDataType.STRING
NUM = FieldDataType.DECIMAL
INT = FieldDataType.INTEGER
BIN = FieldDataType.BINARY
DATE = FieldDataType.DATE
BOOL = FieldDataType.BOOLEAN

#: Row values planted nowhere - asserted absent from every representation.
BUSINESS_VALUES = (
    "EMP002", "Nimal Silva", "INV-204", "45000.00", "250000",
    "SUP-88", "MCH-091", "Colombo",
)


def field(name, source_type, normalized, **kwargs):
    return SourceField(
        source_name=name, normalized_name=name, source_data_type=source_type,
        normalized_data_type=normalized, **kwargs,
    )


def key(name, source_type=None):
    return field(
        name, source_type or "VARCHAR(30)", TEXT,
        is_primary_key=True, nullable=False, required=True,
    )


def entity(entity_id, name, fields, kind=EntityKind.TABLE, primary=()):
    return SourceEntity(
        entity_id=entity_id, source_name=name, normalized_name=name,
        entity_kind=kind, fields=tuple(fields), primary_key_fields=tuple(primary),
    )


def schema(schema_id, system, name, entities, relationships=(), database=None):
    return SourceSchema(
        schema_id=schema_id, source_system_id=system, schema_name=name,
        origin=SchemaOrigin.DISCOVERED, entities=tuple(entities),
        relationships=tuple(relationships),
        metadata={"database": database} if database else {},
        schema_hash=f"hash_{schema_id}",
    )


def build_corpus() -> list[SourceSchema]:
    """Four systems, 24 entities, mixed dialects and deliberate decoys."""
    hr = schema(
        "sch_hr", "legacy_hr", "public", [
            entity("legacy_hr.public.employees", "employees", [
                key("employee_id", "VARCHAR(20)"), field("full_name", "VARCHAR(200)", TEXT),
                field("department_id", "INTEGER", INT), field("hired_on", "DATE", DATE),
                field("birth_certificate", "BYTEA", BIN),
                field("employment_contract", "BYTEA", BIN),
            ], primary=("employee_id",)),
            entity("legacy_hr.public.employee_notes", "employee_notes", [
                key("note_id", "INTEGER"), field("employee_id", "VARCHAR(20)", TEXT),
                field("note_text", "TEXT", TEXT), field("created_on", "DATE", DATE),
            ], primary=("note_id",)),
            entity("legacy_hr.public.employee_training", "employee_training", [
                key("training_id", "INTEGER"), field("employee_id", "VARCHAR(20)", TEXT),
                field("course_name", "VARCHAR(200)", TEXT),
                field("completed_on", "DATE", DATE), field("passed", "BOOLEAN", BOOL),
            ], primary=("training_id",)),
            entity("legacy_hr.public.birth_records", "birth_records", [
                key("record_id", "INTEGER"), field("registered_on", "DATE", DATE),
                field("registrar_office", "VARCHAR(200)", TEXT),
            ], primary=("record_id",)),
            entity("legacy_hr.public.document_archive", "document_archive", [
                key("archive_id", "INTEGER"), field("archived_on", "DATE", DATE),
                field("retention_years", "INTEGER", INT),
            ], primary=("archive_id",)),
            entity("legacy_hr.public.departments", "departments", [
                key("department_id", "INTEGER"),
                field("department_name", "VARCHAR(100)", TEXT),
                field("cost_centre", "VARCHAR(30)", TEXT),
            ], primary=("department_id",)),
            entity("legacy_hr.public.leave_requests", "leave_requests", [
                key("leave_id", "INTEGER"), field("employee_id", "VARCHAR(20)", TEXT),
                field("start_date", "DATE", DATE), field("end_date", "DATE", DATE),
                field("approval_status", "VARCHAR(20)", TEXT),
            ], primary=("leave_id",)),
        ], [
            SourceRelationship(
                relationship_id="fk_emp_dept",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="employees", to_entity="departments",
                from_fields=("department_id",), to_fields=("department_id",)),
        ], database="hrdb")

    finance = schema(
        "sch_fin", "finance_erp", "sales", [
            entity("finance_erp.sales.invoices", "invoices", [
                key("inv_no"), field("cust_ref", "VARCHAR(30)", TEXT),
                field("total_amt", "DECIMAL(14,2)", NUM),
                field("curr", "CHAR(3)", TEXT),
                field("approval_status", "VARCHAR(20)", TEXT),
                field("issued_on", "DATE", DATE),
            ], primary=("inv_no",)),
            entity("finance_erp.sales.invoice_lines", "invoice_lines", [
                key("line_id", "INTEGER"), field("inv_no", "VARCHAR(30)", TEXT),
                field("product_code", "VARCHAR(30)", TEXT),
                field("quantity", "INTEGER", INT),
                field("line_total", "DECIMAL(14,2)", NUM),
            ], primary=("line_id",)),
            entity("finance_erp.sales.customers", "customers", [
                key("cust_ref"), field("customer_name", "VARCHAR(200)", TEXT),
                field("email_addr", "VARCHAR(200)", TEXT),
                field("credit_limit", "DECIMAL(14,2)", NUM),
            ], primary=("cust_ref",)),
            entity("finance_erp.sales.suppliers", "suppliers", [
                key("supplier_id", "VARCHAR(20)"),
                field("supplier_name", "VARCHAR(200)", TEXT),
                field("tax_id", "VARCHAR(30)", TEXT),
            ], primary=("supplier_id",)),
            entity("finance_erp.sales.purchase_orders", "purchase_orders", [
                key("po_number"), field("supplier_id", "VARCHAR(20)", TEXT),
                field("ordered_on", "DATE", DATE),
                field("po_total", "DECIMAL(14,2)", NUM),
            ], primary=("po_number",)),
            entity("finance_erp.sales.payments", "payments", [
                key("payment_id", "INTEGER"), field("inv_no", "VARCHAR(30)", TEXT),
                field("paid_amount", "DECIMAL(14,2)", NUM),
                field("paid_on", "DATE", DATE),
            ], primary=("payment_id",)),
            entity("finance_erp.sales.tax_rates", "tax_rates", [
                key("tax_code", "VARCHAR(10)"),
                field("rate_percent", "DECIMAL(5,2)", NUM),
            ], primary=("tax_code",)),
        ], [
            SourceRelationship(
                relationship_id="fk_po_supplier",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="purchase_orders", to_entity="suppliers",
                from_fields=("supplier_id",), to_fields=("supplier_id",)),
            SourceRelationship(
                relationship_id="fk_line_invoice",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="invoice_lines", to_entity="invoices",
                from_fields=("inv_no",), to_fields=("inv_no",)),
        ], database="findb")

    # MySQL dialect, and a SECOND employees table.
    payroll = schema(
        "sch_pay", "legacy_payroll", "payroll", [
            entity("legacy_payroll.payroll.employees", "employees", [
                key("emp_no", "VARCHAR(20)"),
                field("gross_pay", "DECIMAL(12,2)", NUM),
                field("tax_code", "VARCHAR(10)", TEXT),
                field("bank_account", "VARCHAR(40)", TEXT),
            ], primary=("emp_no",)),
            entity("legacy_payroll.payroll.payslips", "payslips", [
                key("payslip_id", "INTEGER"), field("emp_no", "VARCHAR(20)", TEXT),
                field("period_end", "DATE", DATE),
                field("payslip_pdf", "LONGBLOB", BIN),
            ], primary=("payslip_id",)),
            entity("legacy_payroll.payroll.deductions", "deductions", [
                key("deduction_id", "INTEGER"), field("emp_no", "VARCHAR(20)", TEXT),
                field("deduction_type", "VARCHAR(50)", TEXT),
                field("amount", "DECIMAL(12,2)", NUM),
            ], primary=("deduction_id",)),
            entity("legacy_payroll.payroll.pay_grades", "pay_grades", [
                key("grade_code", "VARCHAR(10)"),
                field("min_salary", "DECIMAL(12,2)", NUM),
                field("max_salary", "DECIMAL(12,2)", NUM),
            ], primary=("grade_code",)),
        ], database="paydb")

    # MongoDB + SQL Server dialects.
    ops = schema(
        "sch_ops", "plant_ops", "operations", [
            entity("plant_ops.operations.machines", "machines", [
                key("machine_code", "NVARCHAR(20)"),
                field("machine_name", "NVARCHAR(200)", TEXT),
                field("installed_on", "DATE", DATE),
                field("manual_scan", "VARBINARY(MAX)", BIN),
            ], primary=("machine_code",)),
            entity("plant_ops.operations.maintenance_logs", "maintenance_logs", [
                key("log_id", "INTEGER"), field("machine_code", "NVARCHAR(20)", TEXT),
                field("performed_on", "DATE", DATE),
                field("technician", "NVARCHAR(200)", TEXT),
            ], primary=("log_id",)),
            entity("plant_ops.operations.sensor_readings", "sensor_readings",
                   [key("reading_id", "ObjectId"),
                    field("machine_code", "string", TEXT),
                    field("temperature", "double", NUM),
                    field("recorded_at", "date", DATE),
                    field("tags", "array<string>", FieldDataType.ARRAY, is_array=True)],
                   kind=EntityKind.COLLECTION, primary=("reading_id",)),
            entity("plant_ops.operations.inspection_reports", "inspection_reports",
                   [key("report_id", "ObjectId"),
                    field("machine_code", "string", TEXT),
                    field("report_file", "binData", BIN),
                    field("summary", "string", TEXT)],
                   kind=EntityKind.COLLECTION, primary=("report_id",)),
            entity("plant_ops.operations.warehouse_stock", "warehouse_stock", [
                field("warehouse_id", "NVARCHAR(10)", TEXT, is_primary_key=True,
                      nullable=False, required=True),
                field("product_id", "NVARCHAR(10)", TEXT, is_primary_key=True,
                      nullable=False, required=True),
                field("quantity", "INTEGER", INT),
            ], primary=("warehouse_id", "product_id")),
            entity("plant_ops.operations.spare_parts", "spare_parts", [
                key("part_number", "NVARCHAR(30)"),
                field("part_name", "NVARCHAR(200)", TEXT),
                field("unit_cost", "DECIMAL(12,2)", NUM),
            ], primary=("part_number",)),
        ], [
            SourceRelationship(
                relationship_id="fk_log_machine",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="maintenance_logs", to_entity="machines",
                from_fields=("machine_code",), to_fields=("machine_code",)),
        ], database="opsdb")

    return [hr, finance, payroll, ops]


#: FIXED BEFORE THE RUN. Not edited afterwards.
#: (category, query, expected source system, expected entity, filters)
QUERIES = [
    # -- field-location --
    ("field", "Which ERP table contains employee birth certificates?",
     "legacy_hr", "employees", {}),
    ("field", "Where is the invoice total amount stored?",
     "finance_erp", "invoices", {}),
    ("field", "Which table holds the customer credit limit?",
     "finance_erp", "customers", {}),
    ("field", "Where are bank account details for payroll kept?",
     "legacy_payroll", "employees", {}),
    ("field", "Which collection stores machine inspection report files?",
     "plant_ops", "inspection_reports", {}),
    ("field", "Where is the spare part unit cost recorded?",
     "plant_ops", "spare_parts", {}),
    # -- entity-purpose --
    ("entity", "table of departments and cost centres", "legacy_hr", "departments", {}),
    ("entity", "employee leave and absence approvals", "legacy_hr", "leave_requests", {}),
    ("entity", "supplier tax identification records", "finance_erp", "suppliers", {}),
    ("entity", "machine maintenance history", "plant_ops", "maintenance_logs", {}),
    ("entity", "payslip documents for each pay period",
     "legacy_payroll", "payslips", {}),
    ("entity", "warehouse stock quantities per product",
     "plant_ops", "warehouse_stock", {}),
    # -- datatype --
    ("datatype", "Which employee field stores binary document data?",
     "legacy_hr", "employees", {}),
    ("datatype", "table with a VARBINARY column for scanned manuals",
     "plant_ops", "machines", {}),
    ("datatype", "decimal columns holding monetary amounts on invoices",
     "finance_erp", "invoices", {}),
    ("datatype", "array field on a mongo collection", "plant_ops", "sensor_readings", {}),
    # -- relationship --
    ("relationship", "How are purchase orders related to suppliers?",
     "finance_erp", "purchase_orders", {}),
    ("relationship", "foreign key from employees to departments",
     "legacy_hr", "employees", {}),
    ("relationship", "which table links invoice lines back to invoices",
     "finance_erp", "invoice_lines", {}),
    # -- cross-source, exact-filtered --
    ("cross_source", "employees table", "legacy_hr", "employees",
     {"source_system_id": "legacy_hr"}),
    ("cross_source", "employees table", "legacy_payroll", "employees",
     {"source_system_id": "legacy_payroll"}),
    ("cross_source", "invoice records", "finance_erp", "invoices",
     {"schema_name": "sales"}),
]


class Harness:
    def __init__(self, workspace: Path, embedding):
        class Tier(InProcessTier):
            dimension = embedding.dimension

        self.representations = InMemoryRepresentationStore()
        self.storage = PatchedStorage(
            hot=Tier(), state_store=InMemoryTierStateStore()
        )
        self.services = PipelineServices(
            records=InMemoryCanonicalStore(),
            representations=self.representations,
            storage=self.storage,
            embedding=embedding,
        )
        self.orchestration = OrchestrationService(
            services=self.services, job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        )
        self.app = create_app(
            settings=ApiSettings(upload_dir=workspace / "uploads"),
            orchestration=self.orchestration,
        )

    def index(self, schema):
        self.services.schema_cache[schema.schema_id] = schema

        return self.orchestration.index_schema(schema.schema_id)


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="phase7_eval_"))
    embedding = EmbeddingService(SentenceTransformerModel())
    harness = Harness(workspace, embedding)
    corpus = build_corpus()

    index_times: list[float] = []
    entity_count = field_count = 0

    for schema_obj in corpus:
        entity_count += len(schema_obj.entities)
        field_count += sum(len(e.fields) for e in schema_obj.entities)
        started = time.perf_counter()
        job_id, status, error = harness.index(schema_obj)
        index_times.append((time.perf_counter() - started) * 1000)

        if error or status != "succeeded":
            print(f"indexing failed for {schema_obj.schema_id}: {error or status}")
            return 1

    results: list[dict] = []
    reciprocal: list[float] = []
    hit_at_1 = hit_at_3 = 0
    wrong_source = unresolvable = 0
    query_times: list[float] = []
    resolve_times: list[float] = []

    with TestClient(harness.app) as client:
        # Warm the model/HTTP path so the first query is not timed as retrieval.
        client.post("/v1/search", json={"query": "warmup", "top_k": 1,
                                        "filters": {"content_kind": "schema"}})

        for category, query, want_system, want_entity, filters in QUERIES:
            payload = {"query": query, "top_k": 5,
                       "filters": {"content_kind": "schema", **filters}}
            started = time.perf_counter()
            hits = client.post("/v1/search", json=payload).json()["hits"]
            query_times.append((time.perf_counter() - started) * 1000)

            ranked: list[tuple[str, str]] = []

            for hit in hits:
                started = time.perf_counter()
                response = client.get(
                    f"/v1/representations/{hit['representation_id']}"
                )
                resolve_times.append((time.perf_counter() - started) * 1000)

                if response.status_code != 200:
                    unresolvable += 1
                    continue

                body = response.json()
                ranked.append((body["source_system_id"], body["source_entity"]))

                if filters.get("source_system_id") and body[
                    "source_system_id"
                ] != filters["source_system_id"]:
                    wrong_source += 1

                if filters.get("schema_name") and body["schema_name"] != filters[
                    "schema_name"
                ]:
                    wrong_source += 1

            target = (want_system, want_entity)
            rank = ranked.index(target) + 1 if target in ranked else 0

            if rank == 1:
                hit_at_1 += 1
            if 1 <= rank <= 3:
                hit_at_3 += 1

            reciprocal.append(1.0 / rank if rank else 0.0)
            results.append({
                "category": category, "query": query,
                "expected": f"{want_system}.{want_entity}",
                "filters": filters, "rank": rank or None,
                "top_3": [f"{s}.{e}" for s, e in ranked[:3]],
            })

        # ---- the other content kinds are untouched ----
        other_kinds = {
            kind: len(client.post("/v1/search", json={
                "query": "employee", "top_k": 20,
                "filters": {"content_kind": kind},
            }).json()["hits"])
            for kind in ("structured_record", "document_chunk")
        }

        refused = client.post("/v1/search", json={
            "query": "x", "filters": {"content_kind": "schema_table"},
        }).status_code

    # ---- safety gates ----
    stored = [
        harness.representations.get(key)
        for key in harness.representations.list_ids()
    ]
    text_surface = json.dumps([item.text_for_ai for item in stored])
    leaked = [value for value in BUSINESS_VALUES if value in text_surface]

    payload_surface = json.dumps(
        [_payload_for(state) for state in harness.storage.state.list_all()],
        default=str,
    )
    schema_text_in_qdrant = (
        "Content Kind: ERP Schema" in payload_surface
        or "Source Type:" in payload_surface
    )

    ids = [item.representation_id for item in stored]
    duplicates = len(ids) - len(set(ids))

    # Re-indexing must not accumulate.
    before = harness.representations.count()
    for schema_obj in corpus:
        harness.index(schema_obj)
    after_reindex = harness.representations.count()

    total = len(QUERIES)
    gates_ok = (
        not leaked
        and unresolvable == 0
        and duplicates == 0
        and wrong_source == 0
        and not schema_text_in_qdrant
        and before == after_reindex
        and refused == 422
        and other_kinds["structured_record"] == 0
        and other_kinds["document_chunk"] == 0
    )

    def percentile(values, fraction):
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[max(0, int(len(ordered) * fraction) - 1)]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "embedding_model": embedding.model_id,
            "dimension": embedding.dimension,
            "vector_store": "in-process tier (NOT a real Qdrant server)",
        },
        "corpus": {
            "source_systems": len(corpus),
            "entities": entity_count,
            "fields": field_count,
            "representations": harness.representations.count(),
        },
        "retrieval": {
            "queries": total,
            "recall_at_1": round(hit_at_1 / total, 4),
            "recall_at_3": round(hit_at_3 / total, 4),
            "mrr": round(sum(reciprocal) / total, 4),
            "results": results,
        },
        "gates": {
            "business_value_leakage": len(leaked),
            "leaked_values": leaked,
            "unresolvable_schema_hits": unresolvable,
            "duplicate_current_representations": duplicates,
            "wrong_source_under_exact_filter": wrong_source,
            "schema_text_in_qdrant": schema_text_in_qdrant,
            "representations_before_reindex": before,
            "representations_after_reindex": after_reindex,
            "undefined_content_kind_status": refused,
            "other_content_kind_hits": other_kinds,
        },
        "latency_ms": {
            "schema_index_per_source_median": round(
                statistics.median(index_times), 3
            ),
            "schema_query_median": round(statistics.median(query_times), 3),
            "schema_query_p95": round(percentile(query_times, 0.95), 3),
            "representation_resolve_median": round(
                statistics.median(resolve_times), 3
            ),
        },
        "passed": gates_ok,
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("=" * 74)
    print("PHASE 7 MINI-EVALUATION - semantic schema retrieval")
    print("=" * 74)
    print(f"source systems {len(corpus)}   entities {entity_count}   "
          f"fields {field_count}   representations {harness.representations.count()}")
    print(f"model {embedding.model_id} ({embedding.dimension}d)")
    print()
    print(f"{'category':<14}{'rank':>5}  query")

    for item in results:
        mark = "  " if item["rank"] == 1 else ("~ " if item["rank"] else "X ")
        print(f"  {item['category']:<12}{str(item['rank'] or '-'):>5}  "
              f"{mark}{item['query'][:52]}")

    print()
    print(f"Recall@1  {report['retrieval']['recall_at_1']:.3f}")
    print(f"Recall@3  {report['retrieval']['recall_at_3']:.3f}")
    print(f"MRR       {report['retrieval']['mrr']:.3f}")
    print()
    print(f"business-value leakage            {len(leaked)} {leaked or ''}")
    print(f"unresolvable schema hits          {unresolvable}")
    print(f"duplicate current representations {duplicates}")
    print(f"wrong-source under exact filter   {wrong_source}")
    print(f"schema text in Qdrant payload     {schema_text_in_qdrant}")
    print(f"reindex changed count             {before} -> {after_reindex}")
    print(f"other content kinds reachable     {other_kinds}")
    print()
    print(f"index per source  median {report['latency_ms']['schema_index_per_source_median']:.1f} ms")
    print(f"schema query      median {report['latency_ms']['schema_query_median']:.1f} ms   "
          f"p95 {report['latency_ms']['schema_query_p95']:.1f} ms")
    print(f"resolve           median {report['latency_ms']['representation_resolve_median']:.1f} ms")
    print()
    print(f"artifact -> {ARTIFACT.relative_to(ROOT)}")
    print("=" * 74)
    print(f"GATES: {'PASS' if gates_ok else 'FAIL'}")

    return 0 if gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
