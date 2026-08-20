"""PostgreSQL connector.

Uses ``psycopg2`` via SQLAlchemy Core - both already required by this project
since Phase 0, so unlike MySQL/SQL Server/MongoDB there is no optional
dependency to guard here. No schema discovery: this module only proves
connectivity and reports minimal, safe metadata.
"""

from __future__ import annotations

from erp_pipeline.connectors.models import ConnectorCapabilities
from erp_pipeline.connectors.relational import SQLAlchemyRelationalConnector
from erp_pipeline.schemas.enums import SourceType


class PostgreSQLConnector(SQLAlchemyRelationalConnector):
    """Connector for PostgreSQL sources."""

    SUPPORTED_SOURCE_TYPE = SourceType.POSTGRESQL
    SERVER_VENDOR = "postgresql"
    SERVER_VERSION_QUERY = "SELECT version()"

    def _ensure_driver_available(self) -> None:
        if self._driver_module is not None:
            return

        import psycopg2

        self._driver_module = psycopg2

    def _build_url(self):
        from sqlalchemy import URL

        return URL.create(
            drivername="postgresql+psycopg2",
            username=self._settings.username,
            password=self._settings.password,
            host=self._settings.host,
            port=self._settings.port,
            database=self._settings.database,
        )

    def _connect_args(self) -> dict:
        connect_args: dict = {"connect_timeout": self._settings.connect_timeout_seconds}

        if self._settings.ssl_enabled:
            # psycopg2's sslmode accepts the standard libpq values; default to
            # "require" (encrypted, not identity-verified) unless the caller
            # asked for something stricter via ssl_mode.
            connect_args["sslmode"] = self._settings.ssl_mode or "require"

        return connect_args

    def _capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_type=SourceType.POSTGRESQL,
            relational=True,
            document_database=False,
            supports_transactions=True,
            supports_namespaces=True,  # PostgreSQL schemas within a database
            supports_primary_keys=True,
            supports_foreign_keys=True,
            supports_nested_documents=False,
            supports_server_version=True,
            supports_read_only_session=True,
            supports_incremental_key_extraction=True,  # SERIAL/IDENTITY/sequences
            notes=(
                "Technology capability report only. Phase 3 implements "
                "connectivity, testing and metadata reporting - not schema "
                "discovery, which is Phase 4."
            ),
        )


__all__ = ["PostgreSQLConnector"]
