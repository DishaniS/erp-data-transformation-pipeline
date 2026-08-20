"""Runtime connection settings for the connector framework.

``ConnectionSettings`` is deliberately NOT ``SourceSystem``.

    SourceSystem (erp_pipeline.schemas)
        describes a source LOGICALLY: an id, a name, a technology, an
        environment label. Safe to serialize, log, diff, and persist in the
        schema catalog. Never carries a credential - the model itself refuses
        to construct with one.

    ConnectionSettings (this module)
        describes HOW TO REACH a source AT RUNTIME: host, port, credentials,
        TLS options, timeout. Never persisted anywhere, never logged in full,
        never accepted by ``CatalogRepository``.

A ``ConnectionSettings`` object is constructed fresh for each run from
whatever configuration source the caller chooses (environment variables for a
demo/test, a secrets manager in production, ...). This module does not read
environment variables itself, unlike ``erp_pipeline.catalog.config`` - the
whole point of a runtime settings object is that the framework can hold many
of them at once, one per source system, which a fixed set of environment
variable names cannot express.

Security
--------
* ``password`` is excluded from the dataclass-generated ``repr()``
  (``field(repr=False)``), and this class defines no ``__str__``, so
  ``str(settings)`` falls back to the same redacted ``repr()``.
* ``sanitized()`` returns an explicit, safe dictionary for logging/display -
  the only sanctioned way to render a ``ConnectionSettings`` as text.
* ``driver_options`` and ``metadata`` are scanned for credential-shaped keys
  using the same denylist ``erp_pipeline.schemas`` uses for ``SourceSystem``
  metadata, so a second secret cannot be smuggled in through a side channel.
* Nothing in this module builds or exposes a raw connection URL string with
  the password inlined; individual connectors use
  ``sqlalchemy.URL.render_as_string(hide_password=True)`` wherever a URL
  needs to be shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from erp_pipeline.connectors.errors import ConnectorConfigurationError, ConnectorTypeMismatchError
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.schemas.source_models import SourceSystem
from erp_pipeline.schemas.validation import (
    ValidationError,
    reject_secret_keys,
    require_identifier,
    require_json_object,
    require_text,
)

# Default network timeout applied when a caller does not specify one.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10

# Reasonable bounds. Not a claim about any particular driver's own limits -
# just enough to reject an obviously wrong value (a typo, a negative number,
# a port that cannot exist) before any network activity is attempted.
_MIN_PORT = 1
_MAX_PORT = 65535
_MIN_TIMEOUT_SECONDS = 1
_MAX_TIMEOUT_SECONDS = 3600


def _require_port(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConnectorConfigurationError(
            f"{field_name} must be an integer, got {type(value).__name__}."
        )
    if not (_MIN_PORT <= value <= _MAX_PORT):
        raise ConnectorConfigurationError(
            f"{field_name} must be between {_MIN_PORT} and {_MAX_PORT}, got {value}."
        )
    return value


def _require_timeout(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConnectorConfigurationError(
            f"{field_name} must be an integer, got {type(value).__name__}."
        )
    if not (_MIN_TIMEOUT_SECONDS <= value <= _MAX_TIMEOUT_SECONDS):
        raise ConnectorConfigurationError(
            f"{field_name} must be between {_MIN_TIMEOUT_SECONDS} and "
            f"{_MAX_TIMEOUT_SECONDS} seconds, got {value}."
        )
    return value


@dataclass
class ConnectionSettings:
    """Runtime connectivity information for one source system.

    One shape covers all four supported technologies rather than four
    near-duplicate classes: relational fields (``host``/``port``/``database``/
    ``username``/``password``) apply directly to PostgreSQL, MySQL and SQL
    Server; MongoDB additionally uses ``auth_database`` when its credential
    database differs from the database being connected to.

    ``driver_options`` carries technology-specific, non-secret knobs - for
    example ``{"driver": "ODBC Driver 18 for SQL Server"}`` for SQL Server, or
    ``{"charset": "utf8mb4"}`` for MySQL. It is validated exactly like
    ``metadata``: JSON-safe, no credential-shaped key.
    """

    source_system_id: str
    source_type: SourceType
    host: str
    port: int
    database: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ssl_enabled: bool = False
    ssl_mode: str | None = None
    connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS
    auth_database: str | None = None
    driver_options: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            self.source_system_id = require_identifier(
                self.source_system_id, "ConnectionSettings.source_system_id"
            )
            self.source_type = SourceType.from_value(self.source_type)
            self.host = require_text(self.host, "ConnectionSettings.host")
            self.database = require_text(self.database, "ConnectionSettings.database")
        except ValidationError as exc:
            raise ConnectorConfigurationError(str(exc)) from exc

        self.port = _require_port(self.port, "ConnectionSettings.port")
        self.connect_timeout_seconds = _require_timeout(
            self.connect_timeout_seconds, "ConnectionSettings.connect_timeout_seconds"
        )

        if self.username is not None:
            try:
                self.username = require_text(self.username, "ConnectionSettings.username")
            except ValidationError as exc:
                raise ConnectorConfigurationError(str(exc)) from exc

        if self.password is not None and not isinstance(self.password, str):
            raise ConnectorConfigurationError(
                "ConnectionSettings.password must be a string when provided, got "
                f"{type(self.password).__name__}."
            )

        if self.ssl_mode is not None:
            try:
                self.ssl_mode = require_text(self.ssl_mode, "ConnectionSettings.ssl_mode")
            except ValidationError as exc:
                raise ConnectorConfigurationError(str(exc)) from exc

        if self.auth_database is not None:
            try:
                self.auth_database = require_text(
                    self.auth_database, "ConnectionSettings.auth_database"
                )
            except ValidationError as exc:
                raise ConnectorConfigurationError(str(exc)) from exc

        try:
            self.driver_options = require_json_object(
                self.driver_options, "ConnectionSettings.driver_options"
            )
            reject_secret_keys(self.driver_options, "ConnectionSettings.driver_options")
            self.metadata = require_json_object(self.metadata, "ConnectionSettings.metadata")
            reject_secret_keys(self.metadata, "ConnectionSettings.metadata")
        except ValidationError as exc:
            raise ConnectorConfigurationError(str(exc)) from exc

    # ------------------------------------------------------------
    # Safe display
    # ------------------------------------------------------------

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        # No custom __repr__ is defined: the dataclass-generated one already
        # excludes `password` via field(repr=False). Routing __str__ through
        # it keeps exactly one code path responsible for what this object
        # ever renders as text, so there is only one place to keep safe.
        return repr(self)

    def sanitized(self) -> dict[str, Any]:
        """A safe dictionary for logging or display.

        ``password`` is reported only as present/absent, never by value.
        """
        return {
            "source_system_id": self.source_system_id,
            "source_type": self.source_type.value,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "username": self.username,
            "password_set": self.password is not None,
            "ssl_enabled": self.ssl_enabled,
            "ssl_mode": self.ssl_mode,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "auth_database": self.auth_database,
            "driver_options": dict(self.driver_options),
            "metadata": dict(self.metadata),
        }

    # ------------------------------------------------------------
    # SourceSystem compatibility (Step 19)
    # ------------------------------------------------------------

    def matches_source_system(self, source_system: SourceSystem) -> bool:
        """True when this settings object could be used to connect to
        ``source_system`` - same id, same declared technology."""
        return (
            self.source_system_id == source_system.source_system_id
            and self.source_type == source_system.source_type
        )

    def require_compatible_source_system(self, source_system: SourceSystem) -> None:
        """Raise ``ConnectorTypeMismatchError`` unless this settings object is
        compatible with ``source_system``.

        This never mutates ``source_system`` and never persists anything -
        it is a pure compatibility check run before connecting.
        """
        if not isinstance(source_system, SourceSystem):
            raise ConnectorTypeMismatchError(
                f"Expected a SourceSystem, got {type(source_system).__name__}."
            )

        if self.source_system_id != source_system.source_system_id:
            raise ConnectorTypeMismatchError(
                f"ConnectionSettings.source_system_id {self.source_system_id!r} does "
                f"not match SourceSystem.source_system_id "
                f"{source_system.source_system_id!r}."
            )

        if self.source_type != source_system.source_type:
            raise ConnectorTypeMismatchError(
                f"ConnectionSettings.source_type {self.source_type.value!r} does not "
                f"match SourceSystem.source_type {source_system.source_type.value!r} "
                f"for source_system_id {self.source_system_id!r}."
            )


__all__ = [
    "ConnectionSettings",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
]
