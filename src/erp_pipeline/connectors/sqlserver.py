"""SQL Server connector.

Uses ``pyodbc`` via SQLAlchemy's ``mssql+pyodbc`` dialect. Two independent
things can be missing, and this connector distinguishes them:

1. the ``pyodbc`` Python package itself
2. the named ODBC driver registered with the system's ODBC driver manager
   (e.g. "ODBC Driver 18 for SQL Server") - installing the Python package
   does NOT install this; it is a separate system component.

Either missing dependency raises ``ConnectorDependencyError`` with an
actionable message rather than a low-level ``pyodbc.InterfaceError`` stack
trace. The driver name is configurable via
``ConnectionSettings.driver_options["driver"]`` rather than hardcoded, so a
deployment with a different installed driver version is not locked out.
"""

from __future__ import annotations

from erp_pipeline.connectors.errors import ConnectorDependencyError
from erp_pipeline.connectors.models import ConnectorCapabilities
from erp_pipeline.connectors.relational import SQLAlchemyRelationalConnector
from erp_pipeline.schemas.enums import SourceType

#: Used when ConnectionSettings.driver_options does not specify one.
DEFAULT_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"


class SQLServerConnector(SQLAlchemyRelationalConnector):
    """Connector for SQL Server sources."""

    SUPPORTED_SOURCE_TYPE = SourceType.SQL_SERVER
    SERVER_VENDOR = "sql_server"
    SERVER_VERSION_QUERY = "SELECT @@VERSION"

    def _configured_driver_name(self) -> str:
        return str(self._settings.driver_options.get("driver", DEFAULT_ODBC_DRIVER))

    def _ensure_driver_available(self) -> None:
        if self._driver_module is not None:
            return

        try:
            import pyodbc
        except ImportError as exc:
            raise ConnectorDependencyError(
                "The 'pyodbc' package is required to connect to SQL Server "
                "sources but is not installed. Install it with: "
                "pip install pyodbc"
            ) from exc

        driver_name = self._configured_driver_name()
        try:
            available_drivers = pyodbc.drivers()
        except Exception as exc:
            raise ConnectorDependencyError(
                "Could not enumerate system ODBC drivers via pyodbc. Ensure "
                "the ODBC Driver Manager is installed and accessible."
            ) from exc

        if driver_name not in available_drivers:
            raise ConnectorDependencyError(
                f"The ODBC driver {driver_name!r} is not registered with this "
                f"system's ODBC driver manager. Installed drivers: "
                f"{available_drivers}. Install the Microsoft ODBC Driver for "
                "SQL Server, or set ConnectionSettings.driver_options['driver'] "
                "to the name of an installed driver."
            )

        self._driver_module = pyodbc

    def _build_url(self):
        from sqlalchemy import URL

        return URL.create(
            drivername="mssql+pyodbc",
            username=self._settings.username,
            password=self._settings.password,
            host=self._settings.host,
            port=self._settings.port,
            database=self._settings.database,
            query={"driver": self._configured_driver_name()},
        )

    def _connect_args(self) -> dict:
        connect_args: dict = {"timeout": self._settings.connect_timeout_seconds}

        if self._settings.ssl_enabled:
            connect_args["Encrypt"] = "yes"

        return connect_args

    def _capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_type=SourceType.SQL_SERVER,
            relational=True,
            document_database=False,
            supports_transactions=True,
            supports_namespaces=True,  # SQL Server schemas within a database
            supports_primary_keys=True,
            supports_foreign_keys=True,
            supports_nested_documents=False,
            supports_server_version=True,
            supports_read_only_session=True,
            supports_incremental_key_extraction=True,  # IDENTITY / ROWVERSION
            notes=(
                "Technology capability report only. Phase 3 implements "
                "connectivity, testing and metadata reporting - not schema "
                "discovery, which is Phase 4."
            ),
        )


__all__ = ["SQLServerConnector", "DEFAULT_ODBC_DRIVER"]
