"""Pure result/value types returned by the connector framework.

Nothing in this module performs I/O. These are plain dataclasses describing
the *shape* of a connection test result, a capability report, and source
metadata - the connectors in this package populate them from a live driver
call, but the types themselves have no driver dependency.

None of these types is a Phase 1 contract and none is persisted by the schema
catalog. They exist only for the duration of a connector session.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from erp_pipeline.schemas.enums import SourceType


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime.

    A local copy of the same helper ``erp_pipeline.schemas.serialization``
    provides, kept here so ``connectors`` does not need to import
    ``schemas.serialization`` just for a clock function.
    """
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ConnectorCapabilities:
    """What the underlying source TECHNOLOGY is capable of.

    This is a statement about the database technology, never about what
    ``erp_pipeline`` has implemented so far. PostgreSQL genuinely supports
    schema discovery as a technology; Phase 3 does not perform schema
    discovery. A capability flag being ``True`` here must never be read as
    "Phase 3 implements this."
    """

    source_type: SourceType
    relational: bool
    document_database: bool
    supports_transactions: bool
    supports_namespaces: bool
    supports_primary_keys: bool
    supports_foreign_keys: bool
    supports_nested_documents: bool
    supports_server_version: bool
    supports_read_only_session: bool
    supports_incremental_key_extraction: bool
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_type"] = self.source_type.value
        return payload


@dataclass(frozen=True)
class ConnectionTestResult:
    """The outcome of a successful ``BaseSourceConnector.test_connection()``.

    Only ever constructed for a successful test. An operational failure
    raises a typed ``ConnectorError`` instead of producing one of these with
    ``success=False`` - see ``base.BaseSourceConnector.test_connection`` for
    why a single consistent representation was chosen over two.

    Never carries a password, token, or raw credential URL.
    """

    success: bool
    source_system_id: str
    source_type: SourceType
    database_name: str
    server_version: str | None
    latency_ms: float
    message: str
    checked_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "source_system_id": self.source_system_id,
            "source_type": self.source_type.value,
            "database_name": self.database_name,
            "server_version": self.server_version,
            "latency_ms": round(self.latency_ms, 3),
            "message": self.message,
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(frozen=True)
class SourceMetadata:
    """Minimal, safe, non-schema metadata about a connected source.

    Deliberately shallow: a database/server name and version, which
    connector class and driver produced this, and its capability report.
    Nothing here enumerates tables, collections, columns or relationships -
    that begins in Phase 4/5.
    """

    source_system_id: str
    source_type: SourceType
    database_name: str
    server_vendor: str
    server_version: str | None
    connector_implementation: str
    driver_name: str | None
    driver_version: str | None
    capabilities: ConnectorCapabilities
    checked_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_system_id": self.source_system_id,
            "source_type": self.source_type.value,
            "database_name": self.database_name,
            "server_vendor": self.server_vendor,
            "server_version": self.server_version,
            "connector_implementation": self.connector_implementation,
            "driver_name": self.driver_name,
            "driver_version": self.driver_version,
            "capabilities": self.capabilities.to_dict(),
            "checked_at": self.checked_at.isoformat(),
        }


__all__ = [
    "utc_now",
    "ConnectorCapabilities",
    "ConnectionTestResult",
    "SourceMetadata",
]
