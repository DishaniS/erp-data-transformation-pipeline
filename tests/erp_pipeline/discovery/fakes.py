"""In-memory fakes so structural discovery is testable without a live server.

``FakeInspector`` implements exactly the SQLAlchemy ``Inspector`` surface the
discovery algorithm uses, returning the same shapes SQLAlchemy returns. This
lets MySQL and SQL Server discovery logic be exercised deterministically on a
machine with no MySQL/SQL Server instance, and lets edge cases (no PK,
composite FK, cross-schema reference, failing metadata call) be constructed
precisely.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from erp_pipeline.connectors.config import ConnectionSettings
from erp_pipeline.connectors.relational import SQLAlchemyRelationalConnector
from erp_pipeline.connectors.models import ConnectorCapabilities
from erp_pipeline.schemas.enums import SourceType


class FakeDialect:
    def __init__(self, name: str = "postgresql") -> None:
        self.name = name


class FakeInspector:
    """Mimics the subset of sqlalchemy.Inspector that discovery calls."""

    def __init__(
        self,
        schema_names: Sequence[str] = ("public",),
        tables: Mapping[str | None, Sequence[str]] | None = None,
        views: Mapping[str | None, Sequence[str]] | None = None,
        columns: Mapping[tuple[str | None, str], Sequence[dict[str, Any]]] | None = None,
        primary_keys: Mapping[tuple[str | None, str], dict[str, Any]] | None = None,
        foreign_keys: Mapping[tuple[str | None, str], Sequence[dict[str, Any]]] | None = None,
        unique_constraints: Mapping[tuple[str | None, str], Sequence[dict[str, Any]]] | None = None,
        indexes: Mapping[tuple[str | None, str], Sequence[dict[str, Any]]] | None = None,
        table_comments: Mapping[tuple[str | None, str], dict[str, Any]] | None = None,
        dialect_name: str = "postgresql",
        failing_methods: Sequence[str] = (),
    ) -> None:
        self._schema_names = list(schema_names)
        self._tables = dict(tables or {})
        self._views = dict(views or {})
        self._columns = dict(columns or {})
        self._primary_keys = dict(primary_keys or {})
        self._foreign_keys = dict(foreign_keys or {})
        self._unique_constraints = dict(unique_constraints or {})
        self._indexes = dict(indexes or {})
        self._table_comments = dict(table_comments or {})
        self._failing = set(failing_methods)
        self.dialect = FakeDialect(dialect_name)

    def _maybe_fail(self, method: str) -> None:
        if method in self._failing:
            raise RuntimeError(f"simulated failure in {method}")

    def get_schema_names(self):
        self._maybe_fail("get_schema_names")
        return list(self._schema_names)

    def get_table_names(self, schema=None):
        self._maybe_fail("get_table_names")
        return list(self._tables.get(schema, []))

    def get_view_names(self, schema=None):
        self._maybe_fail("get_view_names")
        return list(self._views.get(schema, []))

    def get_columns(self, table_name, schema=None):
        self._maybe_fail("get_columns")
        return [dict(c) for c in self._columns.get((schema, table_name), [])]

    def get_pk_constraint(self, table_name, schema=None):
        self._maybe_fail("get_pk_constraint")
        return dict(self._primary_keys.get((schema, table_name), {}))

    def get_foreign_keys(self, table_name, schema=None):
        self._maybe_fail("get_foreign_keys")
        return [dict(fk) for fk in self._foreign_keys.get((schema, table_name), [])]

    def get_unique_constraints(self, table_name, schema=None):
        self._maybe_fail("get_unique_constraints")
        return [dict(u) for u in self._unique_constraints.get((schema, table_name), [])]

    def get_indexes(self, table_name, schema=None):
        self._maybe_fail("get_indexes")
        return [dict(i) for i in self._indexes.get((schema, table_name), [])]

    def get_table_comment(self, table_name, schema=None):
        self._maybe_fail("get_table_comment")
        return dict(self._table_comments.get((schema, table_name), {}))


class FakeRelationalConnector(SQLAlchemyRelationalConnector):
    """A real connector subclass wired to a FakeInspector.

    Subclasses the genuine Phase 3 base so discovery exercises the same
    type-checking, lifecycle and ``create_inspector`` seam it would use
    against a live database - only the Inspector itself is substituted.
    """

    SERVER_VENDOR = "fake"

    def __init__(
        self,
        inspector: FakeInspector,
        source_type: SourceType = SourceType.POSTGRESQL,
        source_system_id: str = "fake_sys",
        database: str = "fake_db",
    ) -> None:
        type(self).SUPPORTED_SOURCE_TYPE = source_type
        settings = ConnectionSettings(
            source_system_id=source_system_id,
            source_type=source_type,
            host="localhost",
            port=5432,
            database=database,
            username="app_user",
            password="fake-password",
        )
        super().__init__(settings)
        self._inspector = inspector

    def create_inspector(self):
        self._require_open()
        return self._inspector

    def _ensure_driver_available(self) -> None:  # pragma: no cover - not used
        self._driver_module = None

    def _build_url(self):  # pragma: no cover - not used
        raise NotImplementedError

    def _capabilities(self) -> ConnectorCapabilities:  # pragma: no cover - not used
        raise NotImplementedError


def column(
    name: str,
    type_object: Any,
    nullable: bool = True,
    default: Any = None,
    comment: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a column dict shaped exactly like Inspector.get_columns()."""
    payload: dict[str, Any] = {
        "name": name,
        "type": type_object,
        "nullable": nullable,
        "default": default,
    }
    if comment is not None:
        payload["comment"] = comment
    payload.update(extra)
    return payload
