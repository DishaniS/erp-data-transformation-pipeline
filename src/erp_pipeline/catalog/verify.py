"""Catalog integrity verification.

Usage:
    python -m erp_pipeline.catalog.verify

Checks performed
-----------------
1. PostgreSQL connectivity to the catalog database
2. every required ``erp_catalog`` table exists
3. source system count
4. schema snapshot count
5. orphan entity / field / relationship rows (should be impossible given the
   foreign keys in ``schema.py``, but verified directly rather than assumed)
6. duplicate catalog versions within a (source_system_id, schema_name) scope
   (should be impossible given the unique constraint, verified directly)
7. duplicate schema hashes within a scope (would mean the idempotent-save
   logic in ``repository.save_schema_snapshot`` failed to deduplicate)
8. latest-snapshot resolution is unambiguous for every scope present

Never touches Qdrant, never prints a credential. Exits non-zero on any
integrity failure so it is usable as a CI gate.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Engine

from erp_pipeline.catalog.config import CATALOG_SCHEMA_NAME, CatalogDatabaseSettings
from erp_pipeline.catalog.schema import ALL_TABLE_NAMES


@dataclass
class VerificationReport:
    counts: dict[str, object] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    checks_run: int = 0

    def record(self, label: str, value: object) -> None:
        self.counts[label] = value

    def check(self, label: str, ok: bool, detail: str) -> bool:
        self.checks_run += 1
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {label}: {detail}")
        if not ok:
            self.failures.append(f"{label}: {detail}")
        return ok

    @property
    def passed(self) -> bool:
        return not self.failures


def verify_catalog(engine: Engine) -> VerificationReport:
    report = VerificationReport()

    print("Catalog connectivity")
    print("-" * 70)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        report.check("PostgreSQL connectivity", True, "reachable")
    except Exception as exc:  # noqa: BLE001 - deliberately broad, this is the connectivity probe
        report.check(
            "PostgreSQL connectivity",
            False,
            f"unreachable ({type(exc).__name__}: {str(exc).splitlines()[0][:200]})",
        )
        return report  # nothing else can be checked without a connection

    print("\nSchema and tables")
    print("-" * 70)
    with engine.connect() as connection:
        existing_tables = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema"
                ),
                {"schema": CATALOG_SCHEMA_NAME},
            )
        }

    missing_tables = [name for name in ALL_TABLE_NAMES if name not in existing_tables]
    report.check(
        f"schema '{CATALOG_SCHEMA_NAME}' has all required tables",
        not missing_tables,
        "all present" if not missing_tables else f"missing: {missing_tables}",
    )
    if missing_tables:
        return report  # counts below assume the tables exist

    print("\nCounts")
    print("-" * 70)
    with engine.connect() as connection:
        for table in ALL_TABLE_NAMES:
            count = connection.execute(
                text(f"SELECT COUNT(*) FROM {CATALOG_SCHEMA_NAME}.{table}")
            ).scalar()
            report.record(table, count)
            print(f"  {table}: {count}")

    print("\nOrphan rows")
    print("-" * 70)
    with engine.connect() as connection:
        orphan_queries = {
            "source_entities without a schema_snapshots row": f"""
                SELECT COUNT(*) FROM {CATALOG_SCHEMA_NAME}.source_entities e
                WHERE NOT EXISTS (
                    SELECT 1 FROM {CATALOG_SCHEMA_NAME}.schema_snapshots s
                    WHERE s.schema_id = e.schema_id
                )
            """,
            "source_fields without a source_entities row": f"""
                SELECT COUNT(*) FROM {CATALOG_SCHEMA_NAME}.source_fields f
                WHERE NOT EXISTS (
                    SELECT 1 FROM {CATALOG_SCHEMA_NAME}.source_entities e
                    WHERE e.schema_id = f.schema_id AND e.entity_id = f.entity_id
                )
            """,
            "source_relationships without a schema_snapshots row": f"""
                SELECT COUNT(*) FROM {CATALOG_SCHEMA_NAME}.source_relationships r
                WHERE NOT EXISTS (
                    SELECT 1 FROM {CATALOG_SCHEMA_NAME}.schema_snapshots s
                    WHERE s.schema_id = r.schema_id
                )
            """,
            "mapping_profiles without a source_systems row": f"""
                SELECT COUNT(*) FROM {CATALOG_SCHEMA_NAME}.mapping_profiles m
                WHERE NOT EXISTS (
                    SELECT 1 FROM {CATALOG_SCHEMA_NAME}.source_systems s
                    WHERE s.source_system_id = m.source_system_id
                )
            """,
            "field_mappings without a mapping_profiles row": f"""
                SELECT COUNT(*) FROM {CATALOG_SCHEMA_NAME}.field_mappings fm
                WHERE NOT EXISTS (
                    SELECT 1 FROM {CATALOG_SCHEMA_NAME}.mapping_profiles m
                    WHERE m.mapping_id = fm.mapping_id
                )
            """,
        }
        for label, query in orphan_queries.items():
            count = connection.execute(text(query)).scalar() or 0
            report.record(f"orphan:{label}", count)
            report.check(label, count == 0, f"{count} orphan row(s)")

    print("\nVersion sequence integrity")
    print("-" * 70)
    with engine.connect() as connection:
        duplicate_versions = connection.execute(
            text(
                f"""
                SELECT source_system_id, schema_name, catalog_version, COUNT(*)
                FROM {CATALOG_SCHEMA_NAME}.schema_snapshots
                GROUP BY source_system_id, schema_name, catalog_version
                HAVING COUNT(*) > 1
                """
            )
        ).fetchall()
        report.record("duplicate_catalog_versions", len(duplicate_versions))
        report.check(
            "no duplicate catalog versions within a scope",
            not duplicate_versions,
            "none found" if not duplicate_versions else f"{len(duplicate_versions)} duplicate(s)",
        )

        duplicate_hashes = connection.execute(
            text(
                f"""
                SELECT source_system_id, schema_name, schema_hash, COUNT(*)
                FROM {CATALOG_SCHEMA_NAME}.schema_snapshots
                GROUP BY source_system_id, schema_name, schema_hash
                HAVING COUNT(*) > 1
                """
            )
        ).fetchall()
        report.record("duplicate_schema_hashes_in_scope", len(duplicate_hashes))
        report.check(
            "no duplicate schema hashes within a scope",
            not duplicate_hashes,
            (
                "none found"
                if not duplicate_hashes
                else f"{len(duplicate_hashes)} scope(s) with a hash reused across "
                "versions - the idempotent-save dedup path may have failed"
            ),
        )

        scopes = connection.execute(
            text(
                f"""
                SELECT DISTINCT source_system_id, schema_name
                FROM {CATALOG_SCHEMA_NAME}.schema_snapshots
                """
            )
        ).fetchall()

        unresolvable_scopes = []
        for source_system_id, schema_name in scopes:
            latest_rows = connection.execute(
                text(
                    f"""
                    SELECT schema_id FROM {CATALOG_SCHEMA_NAME}.schema_snapshots
                    WHERE source_system_id = :sid AND schema_name = :name
                      AND catalog_version = (
                          SELECT MAX(catalog_version)
                          FROM {CATALOG_SCHEMA_NAME}.schema_snapshots
                          WHERE source_system_id = :sid AND schema_name = :name
                      )
                    """
                ),
                {"sid": source_system_id, "name": schema_name},
            ).fetchall()
            if len(latest_rows) != 1:
                unresolvable_scopes.append((source_system_id, schema_name, len(latest_rows)))

        report.record("scopes_checked", len(scopes))
        report.record("unresolvable_latest_scopes", len(unresolvable_scopes))
        report.check(
            "latest snapshot resolves unambiguously for every scope",
            not unresolvable_scopes,
            f"{len(scopes)} scope(s) checked"
            if not unresolvable_scopes
            else f"ambiguous: {unresolvable_scopes}",
        )

    print("\n" + "=" * 70)
    print("CATALOG INTEGRITY SUMMARY")
    print("=" * 70)
    for label in ALL_TABLE_NAMES:
        print(f"{label}: {report.counts.get(label, 'n/a')}")
    print(f"Checks run: {report.checks_run}")
    print(f"Integrity status: {'PASS' if report.passed else 'FAIL'}")

    return report


def main() -> int:
    settings = CatalogDatabaseSettings.from_env()
    print(f"Catalog database: {settings.safe_target}")
    print(f"Catalog schema  : {CATALOG_SCHEMA_NAME}\n")

    engine = settings.create_engine()
    try:
        report = verify_catalog(engine)
    finally:
        engine.dispose()

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["VerificationReport", "verify_catalog"]
