"""Generic ERP source connector framework.

Where ``erp_pipeline.schemas`` defines the pure contracts and
``erp_pipeline.catalog`` persists them, this package is the connection
boundary to a live ERP source: PostgreSQL, MySQL, SQL Server, or MongoDB.

Phase 3 scope
-------------
This package proves the framework can safely create the correct connector,
connect to a source, verify it, identify basic non-sensitive source metadata,
report capabilities, and close cleanly - nothing more. No table/column/
relationship discovery, no document sampling, no mapping, no ETL. See
``docs/source_connectors.md``.

Importing this package is always safe
--------------------------------------
``import erp_pipeline.connectors`` never requires ``pymysql``, ``pyodbc``, or
``pymongo`` to be installed. Only the individual vendor modules
(``connectors.mysql``, ``connectors.sqlserver``, ``connectors.mongodb``)
import their driver, and even then lazily, inside a method - not at module
import time. Attempting to actually connect with a missing driver raises
``ConnectorDependencyError`` with an actionable message.

This package depends on ``erp_pipeline.schemas`` (for ``SourceType`` and
``SourceSystem``) and on SQLAlchemy, which the project already requires. It
never imports a dataset-specific module.
"""

from __future__ import annotations

from erp_pipeline.connectors.base import BaseSourceConnector
from erp_pipeline.connectors.config import ConnectionSettings, DEFAULT_CONNECT_TIMEOUT_SECONDS
from erp_pipeline.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorClosedError,
    ConnectorConfigurationError,
    ConnectorConnectionError,
    ConnectorDependencyError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorTypeMismatchError,
    redact_text,
)
from erp_pipeline.connectors.models import ConnectionTestResult, ConnectorCapabilities, SourceMetadata
from erp_pipeline.connectors.registry import ConnectorRegistry

__all__ = [
    "BaseSourceConnector",
    "ConnectionSettings",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "ConnectorError",
    "ConnectorConfigurationError",
    "ConnectorDependencyError",
    "ConnectorConnectionError",
    "ConnectorAuthenticationError",
    "ConnectorTimeoutError",
    "ConnectorTypeMismatchError",
    "ConnectorClosedError",
    "redact_text",
    "ConnectionTestResult",
    "ConnectorCapabilities",
    "SourceMetadata",
    "ConnectorRegistry",
]
