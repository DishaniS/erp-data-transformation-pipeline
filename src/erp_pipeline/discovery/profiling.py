"""Optional, safe, aggregate-only column and table profiling.

PRIVACY RULE - the reason this module is written the way it is
---------------------------------------------------------------
Profiling never reads a value out of the source. Every query it issues is an
aggregate that collapses many rows into a count, a bound, or a length:

    SELECT COUNT(*)                     row count
    SELECT COUNT(col)                   non-null count -> null count/percentage
    SELECT COUNT(DISTINCT col)          distinct count
    SELECT MIN(col), MAX(col)           numeric bounds only
    SELECT MIN/MAX/AVG(LENGTH(col))     string length statistics

There is no ``SELECT col FROM ...``, no ``LIMIT n`` row fetch, and no field on
``ColumnProfile`` capable of holding a value. ``numeric_min``/``numeric_max``
are applied to numeric columns only - never to text, binary, or temporal
columns - so a min/max can never surface an email address, a customer name,
or an invoice description.

Profiling is OFF by default, is never required for structural discovery, and
can never fail a discovery run: individual failures are recorded on the
result and the pass is marked partial.

READ-ONLY: this module issues SELECT aggregates exclusively. No DDL, no DML.
"""

from __future__ import annotations

from typing import Any, Sequence

from erp_pipeline.connectors.errors import redact_text
from erp_pipeline.discovery.errors import ProfilingBudgetExceeded
from erp_pipeline.discovery.models import (
    ColumnProfile,
    DiscoveryOptions,
    ProfilingSummary,
    TableProfile,
)
from erp_pipeline.schemas.enums import FieldDataType
from erp_pipeline.schemas.source_models import SourceEntity, SourceSchema

#: Types whose MIN/MAX are safe to report as bounds. Deliberately numeric
#: only - a MIN/MAX over a text column would return actual stored content.
_NUMERIC_TYPES = frozenset({FieldDataType.INTEGER, FieldDataType.DECIMAL})

#: Types eligible for length statistics. Lengths are counts, not content.
_LENGTH_TYPES = frozenset({FieldDataType.STRING})


class _Budget:
    """Tracks the profiling query allowance and whether it was exhausted."""

    def __init__(self, max_queries: int, strict: bool) -> None:
        self._max_queries = max_queries
        self._strict = strict
        self.executed = 0
        self.exhausted = False

    def take(self, count: int = 1) -> bool:
        if self.executed + count > self._max_queries:
            self.exhausted = True
            if self._strict:
                raise ProfilingBudgetExceeded(
                    f"Profiling budget of {self._max_queries} queries exhausted."
                )
            return False

        self.executed += count
        return True


def profile_schema(
    connector: Any,
    schema: SourceSchema,
    options: DiscoveryOptions,
) -> ProfilingSummary:
    """Profile a discovered schema, respecting the configured budget."""
    if not options.profiling_enabled:
        return ProfilingSummary(enabled=False)

    budget = _Budget(options.max_profiling_queries, options.strict_budget)
    notes: list[str] = []
    profiles: list[TableProfile] = []
    partial = False

    entities = list(schema.entities)
    if len(entities) > options.max_profiled_tables:
        partial = True
        notes.append(
            f"Profiled the first {options.max_profiled_tables} of {len(entities)} "
            "tables; raise max_profiled_tables to cover more."
        )
        entities = entities[: options.max_profiled_tables]

    try:
        connection_context = connector._open_readonly_connection()  # noqa: SLF001 - sanctioned seam
    except Exception as exc:
        return ProfilingSummary(
            enabled=True,
            partial=True,
            notes=(f"Could not open a profiling connection: {redact_text(str(exc))}",),
        )

    try:
        with connection_context as connection:
            preparer = _identifier_preparer(connection)

            for entity in entities:
                if budget.exhausted:
                    partial = True
                    break

                profiles.append(
                    _profile_entity(connection, preparer, entity, options, budget)
                )
    except ProfilingBudgetExceeded:
        raise
    except Exception as exc:
        partial = True
        notes.append(f"Profiling stopped early: {redact_text(str(exc))}")

    if budget.exhausted:
        partial = True
        notes.append(
            f"Query budget of {options.max_profiling_queries} exhausted; "
            "results are partial."
        )

    return ProfilingSummary(
        enabled=True,
        partial=partial,
        tables_profiled=len(profiles),
        queries_executed=budget.executed,
        budget_exhausted=budget.exhausted,
        notes=tuple(notes),
        tables=tuple(profiles),
    )


def _identifier_preparer(connection: Any):
    """Dialect-aware identifier quoter.

    Table and column names come from the database's own catalog, but they are
    still quoted through the dialect's preparer rather than interpolated raw -
    correct for names with mixed case, spaces, or reserved words, and it keeps
    the query construction free of naive string concatenation.
    """
    return connection.dialect.identifier_preparer


def _qualified_table(preparer: Any, entity: SourceEntity) -> str:
    quoted_table = preparer.quote(entity.source_name)
    if entity.namespace:
        return f"{preparer.quote(entity.namespace)}.{quoted_table}"
    return quoted_table


def _profile_entity(
    connection: Any,
    preparer: Any,
    entity: SourceEntity,
    options: DiscoveryOptions,
    budget: _Budget,
) -> TableProfile:
    from sqlalchemy import text as sql_text

    table_reference = _qualified_table(preparer, entity)
    row_count: int | None = None
    column_profiles: list[ColumnProfile] = []

    if options.profile_row_counts and budget.take():
        try:
            row_count = int(
                connection.execute(
                    sql_text(f"SELECT COUNT(*) FROM {table_reference}")
                ).scalar()
                or 0
            )
        except ProfilingBudgetExceeded:
            # Strict-budget signal: must reach the caller, not be recorded as
            # a per-table error.
            raise
        except Exception as exc:
            return TableProfile(
                entity_name=entity.normalized_name,
                error=redact_text(str(exc)),
            )

    for field in entity.fields:
        if budget.exhausted:
            break

        profile = _profile_column(
            connection=connection,
            preparer=preparer,
            table_reference=table_reference,
            field=field,
            row_count=row_count,
            options=options,
            budget=budget,
        )
        if profile is not None:
            column_profiles.append(profile)

    return TableProfile(
        entity_name=entity.normalized_name,
        row_count=row_count,
        columns=tuple(column_profiles),
    )


def _profile_column(
    connection: Any,
    preparer: Any,
    table_reference: str,
    field: Any,
    row_count: int | None,
    options: DiscoveryOptions,
    budget: _Budget,
) -> ColumnProfile | None:
    from sqlalchemy import text as sql_text

    column_reference = preparer.quote(field.source_name)

    null_count: int | None = None
    null_percentage: float | None = None
    distinct_count: int | None = None
    numeric_min: float | None = None
    numeric_max: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    average_length: float | None = None
    error: str | None = None

    requested_anything = False

    try:
        if options.profile_null_percentage and row_count is not None:
            if budget.take():
                requested_anything = True
                non_null = int(
                    connection.execute(
                        sql_text(
                            f"SELECT COUNT({column_reference}) FROM {table_reference}"
                        )
                    ).scalar()
                    or 0
                )
                null_count = max(row_count - non_null, 0)
                null_percentage = (
                    round((null_count / row_count) * 100, 4) if row_count else 0.0
                )

        if options.profile_distinct_count and budget.take():
            requested_anything = True
            distinct_count = int(
                connection.execute(
                    sql_text(
                        f"SELECT COUNT(DISTINCT {column_reference}) FROM {table_reference}"
                    )
                ).scalar()
                or 0
            )

        if (
            options.profile_numeric_min_max
            and field.normalized_data_type in _NUMERIC_TYPES
            and budget.take()
        ):
            requested_anything = True
            row = connection.execute(
                sql_text(
                    f"SELECT MIN({column_reference}), MAX({column_reference}) "
                    f"FROM {table_reference}"
                )
            ).first()
            if row is not None:
                numeric_min = float(row[0]) if row[0] is not None else None
                numeric_max = float(row[1]) if row[1] is not None else None

        if (
            options.profile_length_stats
            and field.normalized_data_type in _LENGTH_TYPES
            and budget.take()
        ):
            requested_anything = True
            length_function = _length_function(connection)
            row = connection.execute(
                sql_text(
                    f"SELECT MIN({length_function}({column_reference})), "
                    f"MAX({length_function}({column_reference})), "
                    f"AVG({length_function}({column_reference})) "
                    f"FROM {table_reference}"
                )
            ).first()
            if row is not None:
                min_length = int(row[0]) if row[0] is not None else None
                max_length = int(row[1]) if row[1] is not None else None
                average_length = round(float(row[2]), 4) if row[2] is not None else None

    except ProfilingBudgetExceeded:
        # Strict-budget signal: must propagate to the caller rather than being
        # recorded as an ordinary per-column profiling error.
        raise
    except Exception as exc:
        error = redact_text(str(exc))

    if not requested_anything and error is None:
        return None

    return ColumnProfile(
        column_name=field.normalized_name,
        null_count=null_count,
        null_percentage=null_percentage,
        distinct_count=distinct_count,
        numeric_min=numeric_min,
        numeric_max=numeric_max,
        min_length=min_length,
        max_length=max_length,
        average_length=average_length,
        error=error,
    )


def _length_function(connection: Any) -> str:
    """The dialect's string-length function.

    SQL Server spells it ``LEN``; PostgreSQL and MySQL use ``LENGTH``. This
    is one of the few genuinely engine-specific details in Phase 4, isolated
    to this single helper.
    """
    dialect_name = getattr(connection.dialect, "name", "") or ""
    return "LEN" if dialect_name.startswith("mssql") else "LENGTH"


__all__ = ["profile_schema"]
