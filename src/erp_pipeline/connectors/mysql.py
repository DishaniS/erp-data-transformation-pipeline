"""MySQL connector.

Uses ``PyMySQL`` - a pure-Python DBAPI driver, chosen specifically because it
needs no compiled system dependency, so ``pip install pymysql`` alone is
enough to exercise the real driver in unit tests without a system MySQL
client library. The import happens lazily inside ``_ensure_driver_available``
so importing this module (or ``erp_pipeline.connectors`` as a whole) never
fails just because PyMySQL is not installed - see Step 15 / Step 18 in the
Phase 3 tests for the proof.
"""

from __future__ import annotations

from erp_pipeline.connectors.errors import ConnectorDependencyError
from erp_pipeline.connectors.models import ConnectorCapabilities
from erp_pipeline.connectors.relational import SQLAlchemyRelationalConnector
from erp_pipeline.schemas.enums import SourceType


class MySQLConnector(SQLAlchemyRelationalConnector):
    """Connector for MySQL sources."""

    SUPPORTED_SOURCE_TYPE = SourceType.MYSQL
    SERVER_VENDOR = "mysql"
    SERVER_VERSION_QUERY = "SELECT VERSION()"

    def _ensure_driver_available(self) -> None:
        if self._driver_module is not None:
            return

        try:
            import pymysql
        except ImportError as exc:
            raise ConnectorDependencyError(
                "The 'pymysql' package is required to connect to MySQL sources "
                "but is not installed. Install it with: pip install PyMySQL"
            ) from exc

        self._driver_module = pymysql

    def _build_url(self):
        from sqlalchemy import URL

        return URL.create(
            drivername="mysql+pymysql",
            username=self._settings.username,
            password=self._settings.password,
            host=self._settings.host,
            port=self._settings.port,
            database=self._settings.database,
            query=(
                {"charset": str(self._settings.driver_options["charset"])}
                if "charset" in self._settings.driver_options
                else {}
            ),
        )

    def _connect_args(self) -> dict:
        connect_args: dict = {"connect_timeout": self._settings.connect_timeout_seconds}

        if self._settings.ssl_enabled:
            # PyMySQL accepts an `ssl` dict; an empty dict requests an
            # encrypted connection without pinning specific certificate
            # files, which are a deployment-specific concern outside Phase 3.
            connect_args["ssl"] = {}

        return connect_args

    def _capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_type=SourceType.MYSQL,
            relational=True,
            document_database=False,
            supports_transactions=True,
            # In MySQL a "schema" IS the database - there is no separate
            # namespace layer the way PostgreSQL/SQL Server have schemas
            # nested inside a database.
            supports_namespaces=False,
            supports_primary_keys=True,
            supports_foreign_keys=True,
            supports_nested_documents=False,
            supports_server_version=True,
            supports_read_only_session=True,
            supports_incremental_key_extraction=True,  # AUTO_INCREMENT
            notes=(
                "Technology capability report only. Phase 3 implements "
                "connectivity, testing and metadata reporting - not schema "
                "discovery, which is Phase 4."
            ),
        )


__all__ = ["MySQLConnector"]
