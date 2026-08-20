"""Shared SQLAlchemy-based lifecycle for the relational connectors.

PostgreSQL, MySQL and SQL Server all follow the same shape: build a
SQLAlchemy URL, create an engine lazily, open a short-lived connection to run
``SELECT 1`` plus a best-effort vendor version query, time it, and dispose the
engine on close. This module holds that shared shape once; each vendor module
supplies only what genuinely differs (URL construction, driver import,
version query text, capability report).

Design choice: SQLAlchemy Core, no ORM (Phase 2 already established this
project's stance - see ``erp_pipeline/catalog/repository.py``). Phase 3 needs
even less than Core's query building: a connection, one fixed diagnostic
query, and clean disposal. No table reflection, no ORM session, and
certainly no execution of caller-supplied SQL.
"""

from __future__ import annotations

import time
from types import ModuleType
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from erp_pipeline.connectors.base import BaseSourceConnector
from erp_pipeline.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConnectionError,
    ConnectorDependencyError,
    ConnectorTimeoutError,
    redact_text,
)
from erp_pipeline.connectors.models import ConnectionTestResult, ConnectorCapabilities, SourceMetadata, utc_now

# Substrings (already lower-cased) that mark a driver error as an
# authentication failure across psycopg2/pymysql/pyodbc's differing message
# formats. Matching is deliberately broad - a false positive just means an
# unrelated failure is reported as ConnectorAuthenticationError instead of
# ConnectorConnectionError, which is a much smaller problem than the reverse.
_AUTH_FAILURE_MARKERS = (
    "password authentication failed",
    "access denied",
    "authentication failed",
    "login failed",
    "invalid authorization",
    "auth failed",
    "not authorized",
)

_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
)


def classify_relational_error(exc: Exception) -> Exception:
    """Map a SQLAlchemy/DBAPI exception to a typed ConnectorError.

    Text-based classification is inherently heuristic across three different
    DBAPI drivers with three different error-message vocabularies. Where the
    classification is uncertain the exception becomes a
    ConnectorConnectionError - a generic "could not connect" is always safe;
    a wrong AUTH/TIMEOUT label would be actively misleading.
    """
    message = redact_text(str(exc))
    lowered = message.lower()

    if isinstance(exc, TimeoutError) or any(marker in lowered for marker in _TIMEOUT_MARKERS):
        return ConnectorTimeoutError(f"Connection timed out: {message}")

    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return ConnectorAuthenticationError(
            "Authentication failed - the source rejected the supplied "
            f"credentials: {message}"
        )

    return ConnectorConnectionError(f"Could not connect: {message}")


class SQLAlchemyRelationalConnector(BaseSourceConnector):
    """Common lifecycle for PostgreSQL / MySQL / SQL Server connectors.

    Subclasses must set:
        SUPPORTED_SOURCE_TYPE   (inherited requirement from BaseSourceConnector)
        SERVER_VENDOR           short vendor label, e.g. "postgresql"
        SERVER_VERSION_QUERY    a single safe read-only SQL statement

    and implement:
        _ensure_driver_available()   raise ConnectorDependencyError if the
                                      DBAPI driver (or, for SQL Server, the
                                      configured ODBC driver) is unavailable.
                                      Must set self._driver_module on success.
        _build_url()                 -> sqlalchemy.URL, never printed raw
        _connect_args()               -> dict passed to create_engine(connect_args=...)
        _capabilities()               -> ConnectorCapabilities
    """

    SERVER_VENDOR: str = "relational"
    SERVER_VERSION_QUERY: str = "SELECT 1"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._engine: Engine | None = None
        self._driver_module: ModuleType | None = None

    # ------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------

    def _ensure_driver_available(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _build_url(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def _connect_args(self) -> dict[str, Any]:
        return {}

    def _capabilities(self) -> ConnectorCapabilities:  # pragma: no cover - overridden
        raise NotImplementedError

    # ------------------------------------------------------------
    # Engine lifecycle
    # ------------------------------------------------------------

    @property
    def _sqlalchemy_engine(self) -> Engine:
        self._require_open()

        if self._engine is None:
            self._ensure_driver_available()
            from sqlalchemy import create_engine

            url = self._build_url()
            try:
                self._engine = create_engine(
                    url,
                    connect_args=self._connect_args(),
                    pool_pre_ping=True,
                )
            except Exception as exc:
                raise classify_relational_error(exc) from exc

        return self._engine

    def _query_server_version(self, connection) -> str | None:
        """Best-effort version string. Never fails the caller if it errors."""
        try:
            return str(connection.execute(text(self.SERVER_VERSION_QUERY)).scalar())
        except Exception:
            return None

    # ------------------------------------------------------------
    # Metadata introspection seam (added in Phase 4)
    # ------------------------------------------------------------

    def create_inspector(self):
        """Return a SQLAlchemy ``Inspector`` bound to this connector's engine.

        This is the sanctioned seam for ``erp_pipeline.discovery`` so schema
        discovery reuses the connector's configured, validated, pooled engine
        instead of opening its own connection to the source.

        It does NOT widen Phase 3's "this is not a remote SQL execution tool"
        boundary: an ``Inspector`` reads catalog metadata only and offers no
        way to run caller-supplied SQL.
        """
        self._require_open()

        from sqlalchemy import inspect as sqlalchemy_inspect

        try:
            return sqlalchemy_inspect(self._sqlalchemy_engine)
        except ConnectorDependencyError:
            raise
        except Exception as exc:
            raise classify_relational_error(exc) from exc

    def _open_readonly_connection(self):
        """Open a short-lived connection for read-only aggregate queries.

        Deliberately protected, not public: it exists only for Phase 4's
        optional profiling, which issues COUNT/MIN/MAX-style aggregates over
        discovered columns. Keeping it off the public contract preserves the
        Phase 3 guarantee that no *public* connector method lets an external
        caller execute arbitrary SQL.
        """
        self._require_open()

        try:
            return self._sqlalchemy_engine.connect()
        except ConnectorDependencyError:
            raise
        except Exception as exc:
            raise classify_relational_error(exc) from exc

    # ------------------------------------------------------------
    # Public contract
    # ------------------------------------------------------------

    def test_connection(self) -> ConnectionTestResult:
        self._require_open()

        started = time.monotonic()
        try:
            with self._sqlalchemy_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                server_version = self._query_server_version(connection)
        except ConnectorDependencyError:
            raise
        except Exception as exc:
            raise classify_relational_error(exc) from exc

        latency_ms = (time.monotonic() - started) * 1000

        return ConnectionTestResult(
            success=True,
            source_system_id=self.source_system_id,
            source_type=self.source_type,
            database_name=self._settings.database,
            server_version=server_version,
            latency_ms=latency_ms,
            message="Connection succeeded.",
            checked_at=utc_now(),
        )

    def get_source_metadata(self) -> SourceMetadata:
        self._require_open()

        try:
            with self._sqlalchemy_engine.connect() as connection:
                server_version = self._query_server_version(connection)
        except ConnectorDependencyError:
            raise
        except Exception as exc:
            raise classify_relational_error(exc) from exc

        driver_name = getattr(self._driver_module, "__name__", None)
        driver_version = getattr(self._driver_module, "__version__", None)

        return SourceMetadata(
            source_system_id=self.source_system_id,
            source_type=self.source_type,
            database_name=self._settings.database,
            server_vendor=self.SERVER_VENDOR,
            server_version=server_version,
            connector_implementation=type(self).__name__,
            driver_name=driver_name,
            driver_version=str(driver_version) if driver_version else None,
            capabilities=self._capabilities(),
            checked_at=utc_now(),
        )

    def get_capabilities(self) -> ConnectorCapabilities:
        return self._capabilities()

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
        self._closed = True


__all__ = ["SQLAlchemyRelationalConnector", "classify_relational_error"]
