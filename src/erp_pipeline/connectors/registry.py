"""Connector registry / factory.

Lets calling code write::

    connector = ConnectorRegistry.create(settings)

instead of an ``if postgres... elif mysql... elif sqlserver... elif mongo...``
chain scattered across the codebase.

Laziness (Step 15)
-------------------
Each built-in entry is registered as a zero-argument loader function, not as
an already-imported connector class. ``_load_mysql()`` (for example) imports
``erp_pipeline.connectors.mysql`` only when actually called, and that module
in turn only imports ``pymysql`` lazily inside its own methods (see
``mysql.py``). The net effect: importing ``erp_pipeline.connectors`` -
including this registry - never touches ``pymysql``, ``pyodbc`` or
``pymongo`` at all, so none of them being installed can break PostgreSQL
support or anything else in the package.
"""

from __future__ import annotations

from typing import Callable

from erp_pipeline.connectors.base import BaseSourceConnector
from erp_pipeline.connectors.config import ConnectionSettings
from erp_pipeline.connectors.errors import ConnectorConfigurationError
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.schemas.source_models import SourceSystem

ConnectorLoader = Callable[[], type[BaseSourceConnector]]


class ConnectorRegistry:
    """Dispatches ``SourceType`` to the matching connector implementation."""

    _loaders: dict[SourceType, ConnectorLoader] = {}

    @classmethod
    def register(cls, source_type: SourceType, loader: ConnectorLoader) -> None:
        """Register (or replace) the connector loader for one SourceType."""
        cls._loaders[SourceType.from_value(source_type)] = loader

    @classmethod
    def unregister(cls, source_type: SourceType) -> None:
        cls._loaders.pop(SourceType.from_value(source_type), None)

    @classmethod
    def registered_source_types(cls) -> tuple[SourceType, ...]:
        return tuple(sorted(cls._loaders, key=lambda item: item.value))

    @classmethod
    def is_registered(cls, source_type: SourceType) -> bool:
        return SourceType.from_value(source_type) in cls._loaders

    @classmethod
    def create(
        cls,
        settings: ConnectionSettings,
        source_system: SourceSystem | None = None,
    ) -> BaseSourceConnector:
        """Create the connector matching ``settings.source_type``.

        If ``source_system`` is supplied, compatibility is checked first
        (Step 19) - a mismatched id or technology is rejected before any
        connector is constructed, let alone before any network activity.

        Raises ``ConnectorConfigurationError`` for an unregistered/unknown
        source type. Never silently falls back to a different connector.
        """
        if not isinstance(settings, ConnectionSettings):
            raise ConnectorConfigurationError(
                f"ConnectorRegistry.create() requires a ConnectionSettings "
                f"instance, got {type(settings).__name__}."
            )

        if source_system is not None:
            settings.require_compatible_source_system(source_system)

        loader = cls._loaders.get(settings.source_type)

        if loader is None:
            supported = ", ".join(t.value for t in cls.registered_source_types())
            raise ConnectorConfigurationError(
                f"No connector is registered for source_type="
                f"{settings.source_type.value!r}. Registered types: "
                f"{supported or '(none)'}."
            )

        connector_class = loader()
        return connector_class(settings)


def _load_postgresql() -> type[BaseSourceConnector]:
    from erp_pipeline.connectors.postgresql import PostgreSQLConnector

    return PostgreSQLConnector


def _load_mysql() -> type[BaseSourceConnector]:
    from erp_pipeline.connectors.mysql import MySQLConnector

    return MySQLConnector


def _load_sqlserver() -> type[BaseSourceConnector]:
    from erp_pipeline.connectors.sqlserver import SQLServerConnector

    return SQLServerConnector


def _load_mongodb() -> type[BaseSourceConnector]:
    from erp_pipeline.connectors.mongodb import MongoDBConnector

    return MongoDBConnector


ConnectorRegistry.register(SourceType.POSTGRESQL, _load_postgresql)
ConnectorRegistry.register(SourceType.MYSQL, _load_mysql)
ConnectorRegistry.register(SourceType.SQL_SERVER, _load_sqlserver)
ConnectorRegistry.register(SourceType.MONGODB, _load_mongodb)


__all__ = ["ConnectorRegistry", "ConnectorLoader"]
