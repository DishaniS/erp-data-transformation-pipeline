"""Optional profiling: aggregates only, budgeted, and provably value-free.

The most important tests here are the privacy ones: profiling must never
capture a value out of the source data.
"""

import dataclasses
import json

import pytest
from sqlalchemy import types as sqltypes

from erp_pipeline.discovery.models import ColumnProfile, DiscoveryOptions, ProfilingSummary, TableProfile
from erp_pipeline.discovery.profiling import profile_schema
from erp_pipeline.discovery.relational import discover_schema

from tests.erp_pipeline.discovery.fakes import FakeInspector, FakeRelationalConnector, column


SECRET_VALUES = [
    "john@example.com",
    "Jane Customer",
    "hunter2",
    "INV-SECRET-001",
    "4111111111111111",
]


class _RecordingConnection:
    """Captures every SQL statement executed and returns canned aggregates."""

    def __init__(self, results=None, fail_on=None):
        self.statements: list[str] = []
        self._results = results or {}
        self._fail_on = fail_on or ()

        class _Preparer:
            def quote(self, name):
                return f'"{name}"'

        class _Dialect:
            name = "postgresql"
            identifier_preparer = _Preparer()

        self.dialect = _Dialect()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)

        for marker in self._fail_on:
            if marker in sql:
                raise RuntimeError(f"simulated failure for {marker}")

        return _Result(self._result_for(sql))

    def _result_for(self, sql: str):
        upper = sql.upper()
        if "COUNT(*)" in upper:
            return (self._results.get("row_count", 1000),)
        if "COUNT(DISTINCT" in upper:
            return (self._results.get("distinct", 976),)
        if "COUNT(" in upper:
            return (self._results.get("non_null", 976),)
        if "MIN(LENGTH" in upper or "MIN(LEN" in upper:
            return self._results.get("length", (5, 74, 22.5))
        if "MIN(" in upper:
            return self._results.get("minmax", (10, 5000))
        return (None,)


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar(self):
        return self._row[0] if self._row else None

    def first(self):
        return self._row


class _ProfilingConnector(FakeRelationalConnector):
    def __init__(self, inspector, connection):
        super().__init__(inspector)
        self._connection = connection

    def _open_readonly_connection(self):
        return self._connection


def _schema_and_connector(connection, columns=None):
    inspector = FakeInspector(
        tables={"public": ["customer"]},
        columns={
            ("public", "customer"): columns
            or [
                column("id", sqltypes.Integer(), nullable=False),
                column("email", sqltypes.String(255)),
                column("balance", sqltypes.Numeric(12, 2)),
            ]
        },
        primary_keys={("public", "customer"): {"constrained_columns": ["id"]}},
    )
    connector = _ProfilingConnector(inspector, connection)
    schema = discover_schema(connector)
    return schema, connector


# ============================================================
# Off by default
# ============================================================

def test_profiling_is_disabled_by_default():
    assert DiscoveryOptions().profiling_enabled is False


def test_disabled_profiling_runs_no_queries():
    connection = _RecordingConnection()
    schema, connector = _schema_and_connector(connection)

    summary = profile_schema(connector, schema, DiscoveryOptions())

    assert summary.enabled is False
    assert connection.statements == []
    connector.close()


def test_discovery_service_does_not_profile_unless_enabled():
    from erp_pipeline.discovery.service import RelationalDiscoveryService

    connection = _RecordingConnection()
    schema, connector = _schema_and_connector(connection)

    result = RelationalDiscoveryService().discover(connector)

    assert result.profiling.enabled is False
    assert connection.statements == []
    connector.close()


# ============================================================
# Aggregate statistics
# ============================================================

def test_row_count_null_percentage_and_numeric_bounds():
    connection = _RecordingConnection(
        results={"row_count": 1000, "non_null": 976, "minmax": (10, 5000)}
    )
    schema, connector = _schema_and_connector(connection)

    summary = profile_schema(
        connector,
        schema,
        DiscoveryOptions(
            profiling_enabled=True,
            profile_row_counts=True,
            profile_null_percentage=True,
            profile_numeric_min_max=True,
        ),
    )

    table = summary.tables[0]
    assert table.row_count == 1000

    email = next(c for c in table.columns if c.column_name == "email")
    assert email.null_count == 24
    assert email.null_percentage == 2.4

    balance = next(c for c in table.columns if c.column_name == "balance")
    assert balance.numeric_min == 10.0
    assert balance.numeric_max == 5000.0
    connector.close()


def test_distinct_count_when_requested():
    connection = _RecordingConnection(results={"distinct": 976})
    schema, connector = _schema_and_connector(connection)

    summary = profile_schema(
        connector, schema,
        DiscoveryOptions(profiling_enabled=True, profile_distinct_count=True),
    )

    email = next(c for c in summary.tables[0].columns if c.column_name == "email")
    assert email.distinct_count == 976
    connector.close()


def test_length_stats_only_apply_to_string_columns():
    connection = _RecordingConnection(results={"length": (5, 74, 22.5)})
    schema, connector = _schema_and_connector(connection)

    summary = profile_schema(
        connector, schema,
        DiscoveryOptions(
            profiling_enabled=True,
            profile_length_stats=True,
            profile_row_counts=False,
            profile_null_percentage=False,
            profile_numeric_min_max=False,
        ),
    )

    profiles = {c.column_name: c for c in summary.tables[0].columns}
    assert profiles["email"].max_length == 74
    assert profiles["email"].min_length == 5
    assert profiles["email"].average_length == 22.5
    # A numeric column gets no length statistics.
    assert "balance" not in profiles or profiles["balance"].max_length is None
    connector.close()


def test_numeric_bounds_never_requested_for_text_columns():
    """MIN/MAX over a text column would return actual stored content."""
    connection = _RecordingConnection()
    schema, connector = _schema_and_connector(connection)

    profile_schema(
        connector, schema,
        DiscoveryOptions(profiling_enabled=True, profile_numeric_min_max=True),
    )

    minmax_statements = [s for s in connection.statements if "MIN(" in s.upper()]
    for statement in minmax_statements:
        assert '"email"' not in statement, (
            f"MIN/MAX was issued against a text column: {statement}"
        )
    connector.close()


# ============================================================
# PRIVACY: no sample values, ever
# ============================================================

def test_only_aggregate_sql_is_ever_issued():
    connection = _RecordingConnection()
    schema, connector = _schema_and_connector(connection)

    profile_schema(
        connector, schema,
        DiscoveryOptions(
            profiling_enabled=True,
            profile_distinct_count=True,
            profile_length_stats=True,
        ),
    )

    assert connection.statements, "expected profiling to issue queries"
    for statement in connection.statements:
        upper = statement.upper()
        assert any(
            aggregate in upper for aggregate in ("COUNT(", "MIN(", "MAX(", "AVG(")
        ), f"non-aggregate query issued: {statement}"
        assert "LIMIT" not in upper, f"row-fetching query issued: {statement}"
        assert "SELECT *" not in upper, f"row-dumping query issued: {statement}"
    connector.close()


def test_column_profile_has_no_field_capable_of_holding_a_value():
    """Structural proof: there is nowhere to put a sample value."""
    field_names = {f.name for f in dataclasses.fields(ColumnProfile)}
    forbidden = {
        "sample", "samples", "sample_values", "values", "examples",
        "most_common", "mode", "top_values", "first_value", "preview",
    }
    assert not (field_names & forbidden)


def test_table_profile_has_no_row_data_field():
    field_names = {f.name for f in dataclasses.fields(TableProfile)}
    forbidden = {"rows", "sample_rows", "data", "records", "preview"}
    assert not (field_names & forbidden)


def test_serialized_profile_contains_no_source_values():
    """End-to-end: even if the source held secrets, none can appear."""
    connection = _RecordingConnection(
        results={"row_count": 1000, "non_null": 976, "minmax": (10, 5000)}
    )
    schema, connector = _schema_and_connector(connection)

    summary = profile_schema(
        connector, schema,
        DiscoveryOptions(
            profiling_enabled=True,
            profile_distinct_count=True,
            profile_length_stats=True,
        ),
    )

    serialized = json.dumps(summary.to_dict())
    for secret in SECRET_VALUES:
        assert secret not in serialized
    connector.close()


# ============================================================
# Budget and failure handling
# ============================================================

def test_table_budget_marks_result_partial_without_failing():
    connection = _RecordingConnection()
    inspector = FakeInspector(
        tables={"public": ["t1", "t2", "t3"]},
        columns={
            ("public", name): [column("a", sqltypes.Integer())] for name in ("t1", "t2", "t3")
        },
    )
    connector = _ProfilingConnector(inspector, connection)
    schema = discover_schema(connector)

    summary = profile_schema(
        connector, schema,
        DiscoveryOptions(profiling_enabled=True, max_profiled_tables=2),
    )

    assert summary.partial is True
    assert summary.tables_profiled == 2
    assert any("max_profiled_tables" in note for note in summary.notes)
    connector.close()


def test_query_budget_stops_profiling_and_marks_partial():
    connection = _RecordingConnection()
    schema, connector = _schema_and_connector(connection)

    summary = profile_schema(
        connector, schema,
        DiscoveryOptions(
            profiling_enabled=True,
            profile_distinct_count=True,
            max_profiling_queries=2,
        ),
    )

    assert summary.budget_exhausted is True
    assert summary.partial is True
    assert summary.queries_executed <= 2
    connector.close()


def test_strict_budget_raises_when_requested():
    from erp_pipeline.discovery.errors import ProfilingBudgetExceeded

    connection = _RecordingConnection()
    schema, connector = _schema_and_connector(connection)

    with pytest.raises(ProfilingBudgetExceeded):
        profile_schema(
            connector, schema,
            DiscoveryOptions(
                profiling_enabled=True,
                profile_distinct_count=True,
                max_profiling_queries=1,
                strict_budget=True,
            ),
        )
    connector.close()


def test_profiling_failure_is_recorded_not_raised():
    connection = _RecordingConnection(fail_on=("COUNT(*)",))
    schema, connector = _schema_and_connector(connection)

    summary = profile_schema(
        connector, schema, DiscoveryOptions(profiling_enabled=True)
    )

    assert summary.enabled is True
    assert summary.tables[0].error is not None
    connector.close()


def test_profiling_connection_failure_does_not_raise():
    class _BrokenConnector(FakeRelationalConnector):
        def _open_readonly_connection(self):
            raise RuntimeError("connection unavailable")

    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={("public", "t"): [column("a", sqltypes.Integer())]},
    )
    connector = _BrokenConnector(inspector)
    schema = discover_schema(connector)

    summary = profile_schema(connector, schema, DiscoveryOptions(profiling_enabled=True))

    assert summary.enabled is True
    assert summary.partial is True
    assert summary.notes
    connector.close()


def test_profiling_never_alters_the_structural_hash():
    """Profiling is supplemental: enabling it must not change the schema."""
    from erp_pipeline.discovery.service import RelationalDiscoveryService

    connection_a = _RecordingConnection()
    schema_a, connector_a = _schema_and_connector(connection_a)
    hash_without = schema_a.compute_schema_hash()
    connector_a.close()

    connection_b = _RecordingConnection()
    inspector = FakeInspector(
        tables={"public": ["customer"]},
        columns={
            ("public", "customer"): [
                column("id", sqltypes.Integer(), nullable=False),
                column("email", sqltypes.String(255)),
                column("balance", sqltypes.Numeric(12, 2)),
            ]
        },
        primary_keys={("public", "customer"): {"constrained_columns": ["id"]}},
    )
    connector_b = _ProfilingConnector(inspector, connection_b)
    result = RelationalDiscoveryService(
        DiscoveryOptions(profiling_enabled=True)
    ).discover(connector_b)

    assert result.profiling.enabled is True
    assert result.schema.compute_schema_hash() == hash_without
    connector_b.close()
