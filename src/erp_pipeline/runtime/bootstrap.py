"""Idempotent schema bootstrap.

WHAT THIS DOES NOT DO
---------------------
It declares no DDL of its own. Every schema is created by the module that owns
it - Phase 2 owns ``erp_catalog``, Phase 10 owns ``erp_sync``, and so on. This
module only calls those helpers in order and reports what it found.

Duplicating the DDL here would create a second definition that silently drifts
from the first.

SAFETY
------
Nothing is ever dropped. Every helper is ``CREATE ... IF NOT EXISTS``, so
running this twice is a no-op, and running it against a populated database
leaves the data alone.

    python -m erp_pipeline.runtime.bootstrap
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

from erp_pipeline.runtime.database import (
    OWNED_SCHEMAS,
    build_pipeline_engine,
    existing_schemas,
)
from erp_pipeline.runtime.settings import RuntimeSettings

LOGGER = logging.getLogger("erp_pipeline.runtime.bootstrap")


@dataclass(frozen=True)
class SchemaResult:
    schema: str
    owner: str
    created: bool
    detail: str | None = None
    succeeded: bool = True


@dataclass(frozen=True)
class BootstrapResult:
    results: tuple[SchemaResult, ...] = ()
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return all(result.succeeded for result in self.results)

    @property
    def created(self) -> tuple[str, ...]:
        return tuple(r.schema for r in self.results if r.created)

    def render(self) -> str:
        lines = [f"schemas before: {', '.join(self.before) or '(none)'}"]

        for result in self.results:
            state = (
                "CREATED"
                if result.created
                else ("OK" if result.succeeded else "FAILED")
            )
            suffix = f"  [{result.detail}]" if result.detail else ""
            lines.append(f"  {state:8} {result.schema:22} (owner: {result.owner}){suffix}")

        lines.append(f"schemas after:  {', '.join(self.after) or '(none)'}")

        return "\n".join(lines)


def bootstrap_all(engine: Any) -> BootstrapResult:
    """Create every generic schema this application owns. Idempotent."""
    from erp_pipeline.catalog import bootstrap_catalog
    from erp_pipeline.orchestration import (
        bootstrap_orchestration_schema,
        bootstrap_record_schema,
    )
    from erp_pipeline.storage import bootstrap_storage_schema
    from erp_pipeline.sync import bootstrap_sync_schema

    before = existing_schemas(engine)
    results: list[SchemaResult] = []

    steps: Sequence[tuple[str, str, Any]] = (
        ("erp_catalog", "Phase 2 schema catalog", bootstrap_catalog),
        ("erp_sync", "Phase 10 incremental sync", bootstrap_sync_schema),
        ("erp_vector_storage", "Phase 12 tier state", bootstrap_storage_schema),
        ("erp_orchestration", "Phase 13 jobs", bootstrap_orchestration_schema),
        ("erp_runtime", "Phase 13 canonical records", bootstrap_record_schema),
    )

    for schema, owner, helper in steps:
        was_present = schema in before

        try:
            outcome = helper(engine)
            detail = None

            # Phase 2's helper returns a report rather than None.
            if outcome is not None and hasattr(outcome, "tables_missing"):
                missing = tuple(outcome.tables_missing)
                detail = (
                    f"tables still missing: {', '.join(missing)}" if missing else None
                )

            results.append(
                SchemaResult(
                    schema=schema,
                    owner=owner,
                    created=not was_present,
                    detail=detail,
                    succeeded=True,
                )
            )
        except Exception as error:  # noqa: BLE001 - reported, not raised
            results.append(
                SchemaResult(
                    schema=schema,
                    owner=owner,
                    created=False,
                    detail=f"{type(error).__name__}",
                    succeeded=False,
                )
            )
            LOGGER.exception("bootstrap failed for %s", schema)

    return BootstrapResult(
        results=tuple(results), before=before, after=existing_schemas(engine)
    )


def verify_all(engine: Any) -> tuple[str, ...]:
    """Return the owned schemas that are still missing. Creates nothing."""
    present = set(existing_schemas(engine))

    return tuple(schema for schema in OWNED_SCHEMAS if schema not in present)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m erp_pipeline.runtime.bootstrap",
        description=(
            "Create the generic PostgreSQL schemas this application owns. "
            "Idempotent; never drops anything."
        ),
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="report missing schemas without creating anything",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = RuntimeSettings.from_environment()

    if not settings.database.configured:
        print(
            "PostgreSQL is not configured. Set PIPELINE_DB_NAME, "
            "PIPELINE_DB_USER and PIPELINE_DB_PASSWORD (see .env.example).",
            file=sys.stderr,
        )

        return 2

    described = settings.database.describe()
    print(
        f"database: {described['user']}@{described['host']}:{described['port']}"
        f"/{described['database']}  password={described['password']}"
    )

    engine = build_pipeline_engine(settings.database)

    if args.verify_only:
        missing = verify_all(engine)

        if missing:
            print(f"MISSING: {', '.join(missing)}")

            return 1

        print("all owned schemas are present")

        return 0

    result = bootstrap_all(engine)
    print(result.render())

    if not result.ok:
        print("bootstrap did not complete cleanly", file=sys.stderr)

        return 1

    print("bootstrap complete")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SchemaResult",
    "BootstrapResult",
    "bootstrap_all",
    "verify_all",
    "main",
]
