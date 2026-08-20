"""MongoDB connector.

Uses ``pymongo``, imported lazily so importing ``erp_pipeline.connectors``
never requires it to be installed. Not a ``SQLAlchemyRelationalConnector``
subclass - MongoDB has its own client/session model with no SQL dialect - so
this connector implements ``BaseSourceConnector`` directly.

Explicitly out of scope here: document sampling, schema inference, collection
enumeration as ``SourceEntity`` objects. Those belong to Phase 5, which reaches
the server exclusively through the ``create_database_handle()`` seam this
module exposes - the connector still owns the client, its credentials and its
lifecycle.
"""

from __future__ import annotations

import time
from typing import Any

from erp_pipeline.connectors.base import BaseSourceConnector
from erp_pipeline.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConnectionError,
    ConnectorDependencyError,
    ConnectorTimeoutError,
    redact_text,
)
from erp_pipeline.connectors.models import ConnectionTestResult, ConnectorCapabilities, SourceMetadata, utc_now
from erp_pipeline.schemas.enums import SourceType


def _classify_mongo_error(exc: Exception) -> Exception:
    """Map a pymongo exception to a typed ConnectorError.

    Imports pymongo's error classes lazily - this function is only ever
    called after a successful driver import, so pymongo is guaranteed
    available by the time it runs.
    """
    import pymongo.errors as mongo_errors

    message = redact_text(str(exc))

    if isinstance(exc, mongo_errors.OperationFailure):
        # OperationFailure.code 18 is MongoDB's AuthenticationFailed code;
        # fall back to text matching for older/other auth-related codes.
        if getattr(exc, "code", None) == 18 or "auth" in message.lower():
            return ConnectorAuthenticationError(
                f"Authentication failed - the source rejected the supplied "
                f"credentials: {message}"
            )
        return ConnectorConnectionError(f"Could not connect: {message}")

    if isinstance(exc, mongo_errors.ServerSelectionTimeoutError):
        return ConnectorTimeoutError(f"Connection timed out: {message}")

    if isinstance(exc, (mongo_errors.ConnectionFailure, mongo_errors.PyMongoError)):
        return ConnectorConnectionError(f"Could not connect: {message}")

    return ConnectorConnectionError(f"Could not connect: {message}")


class MongoDBConnector(BaseSourceConnector):
    """Connector for MongoDB sources."""

    SUPPORTED_SOURCE_TYPE = SourceType.MONGODB

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._client = None
        self._driver_module = None

    def _ensure_driver_available(self) -> None:
        if self._driver_module is not None:
            return

        try:
            import pymongo
        except ImportError as exc:
            raise ConnectorDependencyError(
                "The 'pymongo' package is required to connect to MongoDB "
                "sources but is not installed. Install it with: "
                "pip install pymongo"
            ) from exc

        self._driver_module = pymongo

    def _build_connection_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self._settings.host,
            "port": self._settings.port,
            "serverSelectionTimeoutMS": self._settings.connect_timeout_seconds * 1000,
            "connectTimeoutMS": self._settings.connect_timeout_seconds * 1000,
        }

        if self._settings.username is not None:
            kwargs["username"] = self._settings.username
        if self._settings.password is not None:
            kwargs["password"] = self._settings.password
        if self._settings.auth_database is not None:
            kwargs["authSource"] = self._settings.auth_database
        if self._settings.ssl_enabled:
            kwargs["tls"] = True

        return kwargs

    @property
    def _mongo_client(self):
        self._require_open()

        if self._client is None:
            self._ensure_driver_available()

            try:
                self._client = self._driver_module.MongoClient(
                    **self._build_connection_kwargs()
                )
            except Exception as exc:
                raise _classify_mongo_error(exc) from exc

        return self._client

    def test_connection(self) -> ConnectionTestResult:
        self._require_open()

        started = time.monotonic()
        try:
            # `ping` is the documented safe, read-only way to verify
            # connectivity and authentication without touching any data.
            self._mongo_client.admin.command("ping")
            server_version = self._safe_server_version()
        except ConnectorDependencyError:
            raise
        except Exception as exc:
            raise _classify_mongo_error(exc) from exc

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

    def _safe_server_version(self) -> str | None:
        """Best-effort version string via the standard `buildInfo`/`server_info`
        administrative command. Never fails the caller if it errors."""
        try:
            info = self._mongo_client.server_info()
            return str(info.get("version"))
        except Exception:
            return None

    def get_source_metadata(self) -> SourceMetadata:
        self._require_open()

        try:
            server_version = self._safe_server_version()
        except ConnectorDependencyError:
            raise
        except Exception as exc:
            raise _classify_mongo_error(exc) from exc

        driver_version = getattr(self._driver_module, "version", None)

        return SourceMetadata(
            source_system_id=self.source_system_id,
            source_type=self.source_type,
            database_name=self._settings.database,
            server_vendor="mongodb",
            server_version=server_version,
            connector_implementation=type(self).__name__,
            driver_name=getattr(self._driver_module, "__name__", None),
            driver_version=str(driver_version) if driver_version else None,
            capabilities=self.get_capabilities(),
            checked_at=utc_now(),
        )

    # ------------------------------------------------------------
    # Document-store access seam (added in Phase 5)
    # ------------------------------------------------------------

    def create_database_handle(self):
        """Return the ``pymongo.database.Database`` this connector is bound to.

        The counterpart of ``SQLAlchemyRelationalConnector.create_inspector()``:
        the single sanctioned seam through which ``erp_pipeline.discovery``
        reads collection metadata and samples documents, so Phase 5 reuses this
        connector's validated settings, credentials and pooled client instead
        of constructing a second ``MongoClient`` of its own.

        Unlike an SQLAlchemy ``Inspector``, a ``Database`` object is not
        read-only by construction - MongoDB exposes reads and writes through
        the same handle. The read-only guarantee is therefore enforced one
        level up, in the discovery package: its inference modules call only
        ``list_collections``/``find``/``estimated_document_count``, which
        ``tests/erp_pipeline/discovery/test_mongodb_read_only_safety.py``
        verifies by AST inspection. Granting the connector a read-only MongoDB
        role remains the stronger, recommended control.
        """
        self._require_open()

        try:
            return self._mongo_client[self._settings.database]
        except ConnectorDependencyError:
            raise
        except Exception as exc:
            raise _classify_mongo_error(exc) from exc

    def get_capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_type=SourceType.MONGODB,
            relational=False,
            document_database=True,
            # Multi-document ACID transactions are supported on replica
            # sets/sharded clusters since MongoDB 4.0/4.2 - a technology
            # capability, not something Phase 3 uses.
            supports_transactions=True,
            supports_namespaces=True,  # database.collection namespacing
            # MongoDB has an implicit primary key (`_id`) on every document,
            # not a declared multi-column primary key the way SQL does.
            supports_primary_keys=True,
            supports_foreign_keys=False,
            supports_nested_documents=True,
            supports_server_version=True,
            supports_read_only_session=True,
            # ObjectId embeds a creation timestamp, and most collections
            # carry a monotonically increasing _id, both usable as an
            # incremental extraction key by a future phase.
            supports_incremental_key_extraction=True,
            notes=(
                "Technology capability report only. Phase 3 implements "
                "connectivity, testing and metadata reporting - not document "
                "sampling or schema inference, which is Phase 5."
            ),
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._closed = True


__all__ = ["MongoDBConnector"]
