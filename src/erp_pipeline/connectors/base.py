"""The base connector contract every source technology implements.

Deliberately small: ``test_connection``, ``get_source_metadata``,
``get_capabilities``, ``close``, plus context-manager lifecycle. Nothing
about schema discovery, and - importantly - no general
``execute_sql``/``execute``/``run_query`` method. This framework is a
connection boundary, not a remote query execution tool; a caller who needs to
run something against the source has stepped outside what Phase 3 offers on
purpose (see ``docs/source_connectors.md``).
"""

from __future__ import annotations

import abc
from typing import ClassVar

from erp_pipeline.connectors.config import ConnectionSettings
from erp_pipeline.connectors.errors import ConnectorClosedError, ConnectorTypeMismatchError
from erp_pipeline.connectors.models import ConnectionTestResult, ConnectorCapabilities, SourceMetadata
from erp_pipeline.schemas.enums import SourceType


class BaseSourceConnector(abc.ABC):
    """Abstract base for every source connector.

    Construction validates that ``settings.source_type`` matches the
    connector's own ``SUPPORTED_SOURCE_TYPE`` (Step 4) - a caller can never
    end up with a ``PostgreSQLConnector`` silently attached to MongoDB
    settings. Concrete subclasses set ``SUPPORTED_SOURCE_TYPE`` as a class
    attribute.
    """

    #: The single SourceType this connector implementation supports. Must be
    #: overridden by every concrete subclass.
    SUPPORTED_SOURCE_TYPE: ClassVar[SourceType]

    def __init__(self, settings: ConnectionSettings) -> None:
        if not isinstance(settings, ConnectionSettings):
            raise ConnectorTypeMismatchError(
                f"{type(self).__name__} requires a ConnectionSettings instance, "
                f"got {type(settings).__name__}."
            )

        if settings.source_type != self.SUPPORTED_SOURCE_TYPE:
            raise ConnectorTypeMismatchError(
                f"{type(self).__name__} only supports "
                f"{self.SUPPORTED_SOURCE_TYPE.value!r}, but settings declare "
                f"source_type={settings.source_type.value!r} for "
                f"source_system_id={settings.source_system_id!r}."
            )

        self._settings = settings
        self._closed = False

    # ------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------

    @property
    def source_system_id(self) -> str:
        return self._settings.source_system_id

    @property
    def source_type(self) -> SourceType:
        return self._settings.source_type

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------
    # Public contract
    # ------------------------------------------------------------

    @abc.abstractmethod
    def test_connection(self) -> ConnectionTestResult:
        """Verify connectivity and return a safe, structured result.

        Must raise a ``ConnectorError`` subclass on any operational failure
        rather than returning a "failed" result object - see
        ``docs/source_connectors.md`` for why this framework picked one
        consistent representation instead of two.
        """

    @abc.abstractmethod
    def get_source_metadata(self) -> SourceMetadata:
        """Return minimal, safe, non-schema metadata about the source.

        Never enumerates tables, collections, columns, or relationships.
        """

    @abc.abstractmethod
    def get_capabilities(self) -> ConnectorCapabilities:
        """Return this technology's capability report.

        Pure and static: does not require an open connection and does not
        raise ``ConnectorClosedError`` even after ``close()``.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Release any held resources.

        Must be safe to call more than once (idempotent) and must never
        raise merely because it was already closed.
        """

    # ------------------------------------------------------------
    # Lifecycle helpers shared by every subclass
    # ------------------------------------------------------------

    def _require_open(self) -> None:
        """Guard for methods that need a live session.

        ``get_capabilities()`` deliberately does not call this - capability
        reporting is static technology information, not a live operation.
        """
        if self._closed:
            raise ConnectorClosedError(
                f"{type(self).__name__} for source_system_id="
                f"{self.source_system_id!r} is closed. Create a new connector "
                "to reconnect."
            )

    def __enter__(self) -> "BaseSourceConnector":
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:
        status = "closed" if self._closed else "open"
        return (
            f"{type(self).__name__}(source_system_id={self.source_system_id!r}, "
            f"source_type={self.source_type.value!r}, status={status!r})"
        )


__all__ = ["BaseSourceConnector"]
