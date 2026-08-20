"""Domain error hierarchy for the connector framework.

Every connector operation that can fail raises one of these instead of
letting a raw driver/DBAPI exception cross the public API. A caller that
catches ``ConnectorError`` has caught every connector failure; a caller that
wants driver-level detail can inspect ``__cause__``, which is always
preserved via ``raise ... from exc`` at the point the underlying error was
translated.

No message constructed here may ever include a password or a connection URL
with embedded credentials - see ``errors.redact_text`` and its use in
``relational.py`` / the individual connector modules.
"""

from __future__ import annotations

import re

# Matches "scheme://user:password@host" style URLs so any driver-produced
# text that happens to embed a DSN can be safely redacted before it reaches
# an error message, a log line, or a raised exception. Applied defensively
# everywhere driver-originated text is surfaced, even though the drivers this
# framework targets do not normally embed credentials in their own messages.
_CREDENTIAL_URL_PATTERN = re.compile(r"([a-zA-Z][\w+]*://[^:/\s]+):[^@/\s]+@")


def redact_text(text: str) -> str:
    """Replace any embedded ``user:password@`` fragment with ``user:***@``."""
    if not text:
        return text
    return _CREDENTIAL_URL_PATTERN.sub(r"\1:***@", text)


class ConnectorError(Exception):
    """Base class for every connector-layer error."""


class ConnectorConfigurationError(ConnectorError):
    """Raised when connection settings are missing, invalid, or unsafe.

    Covers blank identifiers, out-of-range ports/timeouts, and settings that
    fail structural validation before any network activity is attempted.
    """


class ConnectorDependencyError(ConnectorError):
    """Raised when a required optional driver package or system component
    is unavailable.

    Examples: ``pymysql`` not installed, ``pyodbc`` installed but the
    configured ODBC driver name is not registered with the system driver
    manager, ``pymongo`` not installed. The message names the missing
    dependency and how to supply it; it never claims a live connection was
    attempted.
    """


class ConnectorConnectionError(ConnectorError):
    """Raised when a connection attempt fails for a reason other than
    authentication or timeout (host unreachable, DNS failure, TLS
    negotiation failure, refused connection, unrecognized driver error)."""


class ConnectorAuthenticationError(ConnectorError):
    """Raised when the source rejects the supplied credentials.

    The message states that authentication failed; it never contains the
    password that was rejected.
    """


class ConnectorTimeoutError(ConnectorError):
    """Raised when a connection or operation exceeds its configured timeout."""


class ConnectorTypeMismatchError(ConnectorError):
    """Raised when a connector implementation does not match the requested
    ``SourceType``.

    Example: constructing ``PostgreSQLConnector`` with settings whose
    ``source_type`` is ``SourceType.MONGODB``, or validating
    ``ConnectionSettings`` against a ``SourceSystem`` that names a different
    ``source_system_id`` or ``source_type``.
    """


class ConnectorClosedError(ConnectorError):
    """Raised when an operation is attempted on a connector that has already
    been closed.

    ``close()`` itself is exempt from this - calling ``close()`` repeatedly
    is always safe and never raises.
    """


__all__ = [
    "redact_text",
    "ConnectorError",
    "ConnectorConfigurationError",
    "ConnectorDependencyError",
    "ConnectorConnectionError",
    "ConnectorAuthenticationError",
    "ConnectorTimeoutError",
    "ConnectorTypeMismatchError",
    "ConnectorClosedError",
]
