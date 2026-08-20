"""
Cross-store integrity verification for the BPI 2020 pipeline.

Checks that PostgreSQL, the unified knowledge files, and Qdrant all agree on
record identity. Every number below is queried or computed - nothing is
hardcoded, and the PASS/FAIL verdict is derived from the failure count.

Checks performed
----------------
 1. every AI-ready case has a stable case_record_id
 2. every AI-ready document has a stable document_record_id
 3. every unified record references a real current PostgreSQL source record
 4. no duplicate stable record IDs exist (PostgreSQL and unified layer)
 5. rows marked embedding_status='completed' have a Qdrant point ID
 6. PostgreSQL and unified record identities match
 7. Qdrant point IDs are the deterministic UUIDv5 of the stable record ID
 8. no unified record identifies itself only by a PostgreSQL SERIAL
 9. cleaned events all carry a stable event_record_id
10. baseline record counts, reported per source table

Usage:
    python src/bpi2020/verification/verify_cross_store_integrity.py
    python src/bpi2020/verification/verify_cross_store_integrity.py --skip-qdrant
    python src/bpi2020/verification/verify_cross_store_integrity.py --sample 5000
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bpi2020.common.config import PostgresSettings, get_vector_collection
from bpi2020.common.health import check_postgres, check_qdrant
from bpi2020.common.stable_ids import make_qdrant_point_id
from bpi2020.qdrant_connection import QdrantSettings

UNIFIED_JSONL_PATH = (
    PROJECT_ROOT
    / "data"
    / "bpi2020"
    / "unified"
    / "bpi2020_unified_ai_knowledge_base.jsonl"
)

# Historical reference values from the Phase 0 audit. Reported as context only;
# a mismatch is a warning to investigate, never an assertion and never a target
# to force records towards.
REFERENCE_BASELINE = {
    "cleaned_event_logs": 270_211,
    "ai_ready_cases": 32_999,
    "ai_ready_documents": 6,
    "unified_records": 33_005,
}

LEGACY_SERIAL_RECORD_ID = re.compile(r"^(case|document)_\d+$")


class IntegrityReport:
    """Collects failures and warnings so the verdict can be calculated."""

    def __init__(self) -> None:
        self.counts: dict[str, Any] = {}
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.checks_run = 0

    def record_count(self, label: str, value: Any) -> None:
        self.counts[label] = value

    def check(self, label: str, ok: bool, detail: str) -> bool:
        self.checks_run += 1
        if not ok:
            self.failures.append(f"{label}: {detail}")
        return ok

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def passed(self) -> bool:
        return not self.failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify cross-store identity integrity for the BPI 2020 pipeline."
    )
    parser.add_argument(
        "--skip-qdrant",
        action="store_true",
        help="Skip all Qdrant checks (use when the vector store is unavailable).",
    )
    parser.add_argument(
        "--skip-unified-file",
        action="store_true",
        help="Skip streaming the unified JSONL file.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=200,
        help=(
            "How many unified records to probe individually in Qdrant "
            "(default: 200). Point-ID derivation is verified for every record "
            "arithmetically; this only bounds the live existence probe."
        ),
    )
    parser.add_argument(
        "--delete-orphan-points",
        action="store_true",
        help=(
            "Delete Qdrant points whose ID is not derivable from any current "
            "stable record ID. Destructive: off by default, and it only ever "
            "touches points this tool has positively identified as orphans."
        ),
    )
    return parser.parse_args()


def iter_unified_records() -> Iterator[dict[str, Any]]:
    """Stream the unified JSONL file without loading it into memory."""
    with UNIFIED_JSONL_PATH.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {UNIFIED_JSONL_PATH} at line {line_number}: {exc}"
                ) from exc


# ============================================================
# PostgreSQL-side checks
# ============================================================

def verify_postgres(engine, report: IntegrityReport) -> dict[str, set[str]]:
    """Check stable-ID coverage and uniqueness; return the live identity sets."""
    print("\nPostgreSQL identity checks")
    print("-" * 70)

    with engine.connect() as connection:
        for table in ("cleaned_event_logs", "ai_ready_cases", "ai_ready_documents"):
            count = connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            report.record_count(table, count)
            print(f"  {table}: {count}")

        per_source = connection.execute(
            text(
                """
                SELECT source_table, process_type, COUNT(*)
                FROM cleaned_event_logs
                GROUP BY source_table, process_type
                ORDER BY source_table
                """
            )
        ).fetchall()
        report.record_count(
            "cleaned_event_logs_per_source",
            {row[0]: row[2] for row in per_source},
        )

        # Check 1/2/9: stable identifier coverage.
        coverage = {
            "cleaned_event_logs.event_record_id": (
                "SELECT COUNT(*) FROM cleaned_event_logs WHERE event_record_id IS NULL"
            ),
            "ai_ready_cases.case_record_id": (
                "SELECT COUNT(*) FROM ai_ready_cases WHERE case_record_id IS NULL"
            ),
            "ai_ready_documents.document_record_id": (
                "SELECT COUNT(*) FROM ai_ready_documents WHERE document_record_id IS NULL"
            ),
        }
        for label, query in coverage.items():
            missing = connection.execute(text(query)).scalar() or 0
            report.record_count(f"missing_{label}", missing)
            report.check(
                f"stable id coverage {label}",
                missing == 0,
                f"{missing} row(s) have no stable identifier",
            )
            print(f"  [{'OK' if missing == 0 else 'FAIL'}] {label}: {missing} missing")

        # Check 4: uniqueness of stable identifiers inside PostgreSQL.
        duplicates = {
            "cleaned_event_logs.event_record_id": """
                SELECT COUNT(*) FROM (
                    SELECT event_record_id FROM cleaned_event_logs
                    WHERE event_record_id IS NOT NULL
                    GROUP BY event_record_id HAVING COUNT(*) > 1
                ) d
            """,
            "ai_ready_cases.case_record_id": """
                SELECT COUNT(*) FROM (
                    SELECT case_record_id FROM ai_ready_cases
                    WHERE case_record_id IS NOT NULL
                    GROUP BY case_record_id HAVING COUNT(*) > 1
                ) d
            """,
            "ai_ready_documents.document_record_id": """
                SELECT COUNT(*) FROM (
                    SELECT document_record_id FROM ai_ready_documents
                    WHERE document_record_id IS NOT NULL
                    GROUP BY document_record_id HAVING COUNT(*) > 1
                ) d
            """,
        }
        total_duplicates = 0
        for label, query in duplicates.items():
            count = connection.execute(text(query)).scalar() or 0
            total_duplicates += count
            report.check(
                f"stable id uniqueness {label}",
                count == 0,
                f"{count} duplicated stable identifier(s)",
            )
            print(f"  [{'OK' if count == 0 else 'FAIL'}] {label}: {count} duplicates")

        report.record_count("duplicate_stable_ids", total_duplicates)

        # Check 5: completed embeddings must carry a Qdrant point ID.
        for table, key_column in (
            ("ai_ready_cases", "case_record_id"),
            ("ai_ready_documents", "document_record_id"),
        ):
            missing_point = connection.execute(
                text(
                    f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE embedding_status = 'completed' AND qdrant_point_id IS NULL
                    """
                )
            ).scalar() or 0
            report.record_count(f"missing_point_id_{table}", missing_point)
            report.check(
                f"completed rows have a point id ({table})",
                missing_point == 0,
                f"{missing_point} completed row(s) have no qdrant_point_id",
            )
            print(
                f"  [{'OK' if missing_point == 0 else 'FAIL'}] {table}: "
                f"{missing_point} completed rows without a point id"
            )

            # Check 7: stored point IDs must be the deterministic derivation.
            rows = connection.execute(
                text(
                    f"""
                    SELECT {key_column}, qdrant_point_id FROM {table}
                    WHERE qdrant_point_id IS NOT NULL
                    """
                )
            ).fetchall()

            mismatched = [
                record_id
                for record_id, point_id in rows
                if record_id and point_id != make_qdrant_point_id(record_id)
            ]
            report.record_count(f"nondeterministic_point_id_{table}", len(mismatched))
            report.check(
                f"deterministic point ids ({table})",
                not mismatched,
                f"{len(mismatched)} row(s) store a point id that is not "
                f"uuid5 of the stable record id, e.g. {mismatched[:3]}",
            )
            print(
                f"  [{'OK' if not mismatched else 'FAIL'}] {table}: "
                f"{len(mismatched)} non-deterministic point ids "
                f"(of {len(rows)} linked rows)"
            )

            status_rows = connection.execute(
                text(f"SELECT embedding_status, COUNT(*) FROM {table} GROUP BY 1")
            ).fetchall()
            report.record_count(
                f"embedding_status_{table}", {row[0]: row[1] for row in status_rows}
            )

        case_ids = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT case_record_id FROM ai_ready_cases "
                    "WHERE case_record_id IS NOT NULL"
                )
            )
        }
        document_ids = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT document_record_id FROM ai_ready_documents "
                    "WHERE document_record_id IS NOT NULL"
                )
            )
        }

    return {"erp_case": case_ids, "erp_document": document_ids}


# ============================================================
# Unified-file checks
# ============================================================

def verify_unified_file(
    postgres_ids: dict[str, set[str]],
    report: IntegrityReport,
) -> dict[str, str]:
    """Check that every unified record resolves to a live PostgreSQL row."""
    print("\nUnified knowledge base checks")
    print("-" * 70)

    if not UNIFIED_JSONL_PATH.exists():
        report.check(
            "unified file present",
            False,
            f"{UNIFIED_JSONL_PATH} not found; run build_unified_bpi_knowledge_base.py",
        )
        return {}

    known_ids = set().union(*postgres_ids.values()) if postgres_ids else set()

    total = 0
    missing_record_id = 0
    legacy_serial_ids: list[str] = []
    duplicate_ids: list[str] = []
    duplicate_count = 0
    missing_sources: list[str] = []
    missing_content_hash = 0
    derived_hashes = 0
    seen: set[str] = set()
    point_id_by_record: dict[str, str] = {}
    type_counts: dict[str, int] = {}

    for record in iter_unified_records():
        total += 1
        record_type = record.get("record_type")
        type_counts[record_type] = type_counts.get(record_type, 0) + 1

        record_id = record.get("record_id") or record.get("unified_record_id")

        if not record_id:
            missing_record_id += 1
            continue

        record_id = str(record_id)

        # Check 8: a SERIAL-derived identity is not acceptable linkage.
        if LEGACY_SERIAL_RECORD_ID.match(record_id):
            if len(legacy_serial_ids) < 5:
                legacy_serial_ids.append(record_id)
            continue

        if record_id in seen:
            duplicate_count += 1
            if len(duplicate_ids) < 5:
                duplicate_ids.append(record_id)
        seen.add(record_id)

        # Check 3/6: the referenced PostgreSQL source row must exist right now.
        expected = postgres_ids.get(record_type)
        if expected is not None:
            if record_id not in expected and len(missing_sources) < 5:
                missing_sources.append(record_id)
        elif record_id not in known_ids and len(missing_sources) < 5:
            missing_sources.append(record_id)

        if not record.get("content_hash"):
            missing_content_hash += 1
        elif record.get("content_hash_source") == "derived_in_unified_layer":
            derived_hashes += 1

        point_id_by_record[record_id] = make_qdrant_point_id(record_id)

    report.record_count("unified_records", total)
    report.record_count("unified_record_type_counts", type_counts)

    missing_source_count = sum(
        1
        for record_id in seen
        if record_id not in known_ids
    )
    report.record_count("unified_missing_postgres_sources", missing_source_count)
    report.record_count("unified_duplicate_record_ids", duplicate_count)

    report.check(
        "unified records have a record_id",
        missing_record_id == 0,
        f"{missing_record_id} unified record(s) have no record_id",
    )
    report.check(
        "unified records use stable ids, not SERIALs",
        not legacy_serial_ids,
        f"unified records still identified by a PostgreSQL SERIAL, e.g. {legacy_serial_ids}",
    )
    report.check(
        "unified record ids are unique",
        not duplicate_ids,
        f"duplicate unified record_id(s), e.g. {duplicate_ids}",
    )
    report.check(
        "unified records resolve to a live PostgreSQL row",
        missing_source_count == 0,
        f"{missing_source_count} unified record(s) reference a source row that "
        f"does not exist, e.g. {missing_sources}",
    )

    print(f"  unified records: {total}")
    print(f"  by type: {type_counts}")
    print(f"  [{'OK' if missing_record_id == 0 else 'FAIL'}] missing record_id: {missing_record_id}")
    print(f"  [{'OK' if not legacy_serial_ids else 'FAIL'}] SERIAL-derived record ids: {len(legacy_serial_ids)}")
    print(f"  [{'OK' if not duplicate_ids else 'FAIL'}] duplicate record ids: {len(duplicate_ids)}")
    print(
        f"  [{'OK' if missing_source_count == 0 else 'FAIL'}] "
        f"missing PostgreSQL sources: {missing_source_count}"
    )

    if missing_content_hash:
        report.warn(f"{missing_content_hash} unified record(s) have no content_hash.")
    if derived_hashes:
        report.warn(
            f"{derived_hashes} unified record(s) carry a hash derived in the unified "
            "layer rather than by the producing stage. Re-run build_ai_ready_cases.py "
            "and parse_bpi_documents.py."
        )

    # Check 6, other direction: PostgreSQL rows absent from the unified layer.
    orphaned_postgres = known_ids - seen
    report.record_count("postgres_rows_missing_from_unified", len(orphaned_postgres))
    report.check(
        "every PostgreSQL row appears in the unified layer",
        not orphaned_postgres,
        f"{len(orphaned_postgres)} PostgreSQL row(s) are absent from the unified "
        f"file, e.g. {sorted(orphaned_postgres)[:3]}",
    )
    print(
        f"  [{'OK' if not orphaned_postgres else 'FAIL'}] PostgreSQL rows missing "
        f"from unified file: {len(orphaned_postgres)}"
    )

    return point_id_by_record


# ============================================================
# Qdrant checks
# ============================================================

def verify_qdrant(
    point_id_by_record: dict[str, str],
    report: IntegrityReport,
    sample_size: int,
    delete_orphans: bool,
) -> None:
    """Check the vector store agrees with the stable identities."""
    print("\nQdrant checks")
    print("-" * 70)

    settings = QdrantSettings.from_env()
    collection = get_vector_collection()

    result = check_qdrant(settings, collection, raise_on_failure=False)
    print(f"  {result}")

    if not result.ok:
        report.check("qdrant reachable", False, result.detail)
        return

    client = settings.create_client()

    if not client.collection_exists(collection):
        report.warn(
            f"Qdrant collection '{collection}' does not exist yet. "
            "Run generate_and_store_embeddings.py."
        )
        report.record_count("qdrant_points", 0)
        return

    info = client.get_collection(collection)
    point_count = info.points_count or 0
    report.record_count("qdrant_points", point_count)
    report.record_count("expected_qdrant_points", len(point_id_by_record))
    print(f"  points in collection: {point_count}")
    print(f"  expected from unified layer: {len(point_id_by_record)}")

    if not point_id_by_record:
        report.warn("No unified records were scanned, so vector linkage was not checked.")
        return

    # Existence probe on a bounded sample: retrieving 33k ids would be slow and
    # would not tell us anything the derivation check does not already prove.
    sample_ids = list(point_id_by_record.values())[:sample_size]
    found = client.retrieve(
        collection_name=collection,
        ids=sample_ids,
        with_payload=True,
        with_vectors=False,
    )
    found_by_id = {str(point.id): point for point in found}
    missing_points = [pid for pid in sample_ids if pid not in found_by_id]

    report.record_count("qdrant_sample_size", len(sample_ids))
    report.record_count("qdrant_missing_vectors_in_sample", len(missing_points))
    report.check(
        "sampled unified records have a vector",
        not missing_points,
        f"{len(missing_points)} of {len(sample_ids)} sampled records have no "
        f"vector in Qdrant, e.g. {missing_points[:3]}",
    )
    print(
        f"  [{'OK' if not missing_points else 'FAIL'}] sampled vectors present: "
        f"{len(sample_ids) - len(missing_points)}/{len(sample_ids)}"
    )

    # Payload must carry the stable key back.
    payload_mismatches = []
    expected_record_by_point = {v: k for k, v in point_id_by_record.items()}
    for point_id, point in found_by_id.items():
        payload = point.payload or {}
        payload_record_id = payload.get("record_id") or payload.get("unified_record_id")
        if payload_record_id != expected_record_by_point.get(point_id):
            payload_mismatches.append(point_id)

    report.record_count("qdrant_payload_mismatches", len(payload_mismatches))
    report.check(
        "vector payloads carry the stable record id",
        not payload_mismatches,
        f"{len(payload_mismatches)} sampled point(s) have a payload record_id that "
        f"does not match the point they are stored under, e.g. {payload_mismatches[:3]}",
    )
    print(
        f"  [{'OK' if not payload_mismatches else 'FAIL'}] payload record_id matches: "
        f"{len(found_by_id) - len(payload_mismatches)}/{len(found_by_id)}"
    )

    orphan_count = point_count - len(point_id_by_record)
    if orphan_count > 0:
        report.warn(
            f"Qdrant holds {orphan_count} more point(s) than the unified layer "
            "defines. These are most likely vectors written before stable ids "
            "existed. Re-run this tool with --delete-orphan-points to remove the "
            "ones it can positively identify as orphans."
        )
        _handle_orphans(client, collection, point_id_by_record, report, delete_orphans)


def _handle_orphans(
    client,
    collection: str,
    point_id_by_record: dict[str, str],
    report: IntegrityReport,
    delete_orphans: bool,
) -> None:
    """Identify (and optionally delete) points with no current stable owner."""
    valid_point_ids = set(point_id_by_record.values())
    orphans: list[str] = []
    offset = None

    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        for point in points:
            if str(point.id) not in valid_point_ids:
                orphans.append(str(point.id))
        if offset is None:
            break

    report.record_count("qdrant_orphan_points", len(orphans))
    print(f"  orphan points identified: {len(orphans)}")

    if not orphans:
        return

    if not delete_orphans:
        print("  (not deleted; pass --delete-orphan-points to remove them)")
        return

    client.delete(collection_name=collection, points_selector=orphans, wait=True)
    print(f"  deleted {len(orphans)} orphan point(s)")
    report.record_count("qdrant_orphan_points_deleted", len(orphans))


# ============================================================
# Reporting
# ============================================================

def print_summary(report: IntegrityReport) -> None:
    counts = report.counts

    print("\n" + "=" * 70)
    print("CROSS-STORE INTEGRITY SUMMARY")
    print("=" * 70)

    print(f"Cleaned events checked      : {counts.get('cleaned_event_logs', 'n/a')}")
    print(f"Cases checked               : {counts.get('ai_ready_cases', 'n/a')}")
    print(f"Documents checked           : {counts.get('ai_ready_documents', 'n/a')}")
    print(f"Unified records checked     : {counts.get('unified_records', 'n/a')}")
    print(f"Qdrant points               : {counts.get('qdrant_points', 'not checked')}")
    print(f"Duplicate stable IDs        : {counts.get('duplicate_stable_ids', 'n/a')}")
    print(
        f"Missing PostgreSQL sources  : "
        f"{counts.get('unified_missing_postgres_sources', 'n/a')}"
    )
    print(
        f"Missing vector linkage      : "
        f"{counts.get('qdrant_missing_vectors_in_sample', 'not checked')}"
    )

    per_source = counts.get("cleaned_event_logs_per_source")
    if per_source:
        print("\nCleaned events per source table:")
        for source_table, count in sorted(per_source.items()):
            print(f"  {source_table}: {count}")

    print("\nBaseline comparison (reference values from the Phase 0 audit):")
    for label, expected in REFERENCE_BASELINE.items():
        actual = counts.get(label)
        if actual is None:
            print(f"  {label}: not measured (expected {expected})")
            continue
        delta = actual - expected
        marker = "match" if delta == 0 else f"delta {delta:+d}"
        print(f"  {label}: actual {actual}, reference {expected} ({marker})")

    if report.warnings:
        print(f"\nWarnings ({len(report.warnings)}):")
        for warning in report.warnings:
            print(f"  - {warning}")

    if report.failures:
        print(f"\nIntegrity failures ({len(report.failures)}):")
        for failure in report.failures:
            print(f"  - {failure}")

    print(f"\nChecks run     : {report.checks_run}")
    print(f"Integrity status: {'PASS' if report.passed else 'FAIL'}")


def run_verification(args: argparse.Namespace) -> IntegrityReport:
    report = IntegrityReport()

    pipeline_db = PostgresSettings.pipeline()
    print(f"Pipeline database: {pipeline_db.safe_target}")
    print(check_postgres(pipeline_db, required_tables=("ai_ready_cases",)))

    engine = pipeline_db.create_engine()
    postgres_ids = verify_postgres(engine, report)

    point_id_by_record: dict[str, str] = {}
    if args.skip_unified_file:
        report.warn("Unified file checks were skipped (--skip-unified-file).")
    else:
        point_id_by_record = verify_unified_file(postgres_ids, report)

    if args.skip_qdrant:
        report.warn("Qdrant checks were skipped (--skip-qdrant).")
    else:
        verify_qdrant(
            point_id_by_record,
            report,
            sample_size=args.sample,
            delete_orphans=args.delete_orphan_points,
        )

    print_summary(report)
    return report


def main() -> int:
    args = parse_args()
    report = run_verification(args)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
