"""Registered sources: how to reach a system, never how to authenticate to it.

WHAT IS STORED
--------------
Structural connection metadata - type, host, port, database, username - plus a
``credential_ref`` naming a secret held elsewhere.

WHAT IS NOT
-----------
The password. Not in this object, not in the catalog, not in a job row, not in
a log line, not in an API response. ``ConnectionSettings`` are assembled at the
moment a connection is opened and discarded with it, which is the rule Phase 3
established and this module exists to preserve.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from erp_pipeline.connectors import ConnectionSettings
from erp_pipeline.orchestration.errors import SourceNotFoundError
from erp_pipeline.orchestration.secrets import SecretProvider
from erp_pipeline.schemas.enums import SourceType

_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


def normalize_source_id(name: str) -> str:
    """A stable, lowercase identifier derived from the supplied name."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", (name or "").strip().lower()).strip("_-")

    return cleaned[:63] or f"src_{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class RegisteredSource:
    """A source the API knows how to reach."""

    source_id: str
    name: str
    source_type: SourceType
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    #: A NAME, resolved through a SecretProvider at connect time. Never a value.
    credential_ref: str | None = None
    auth_database: str | None = None
    ssl_enabled: bool = False
    description: str | None = None
    #: Free-form structural metadata. Filtered on write: see `SourceRegistry`.
    metadata: Mapping[str, Any] = field(default_factory=dict)
    registered_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict[str, Any]:
        """Safe for an API response.

        There is no password field to omit, which is the point: it was never
        stored, so it cannot be leaked by forgetting to exclude it here.
        """
        return {
            "source_id": self.source_id,
            "name": self.name,
            "source_type": self.source_type.value,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "username": self.username,
            "credential_ref": self.credential_ref,
            "ssl_enabled": self.ssl_enabled,
            "description": self.description,
            "metadata": dict(self.metadata),
            "registered_at": self.registered_at.isoformat(),
        }

    def connection_settings(
        self, secrets: SecretProvider | None = None
    ) -> ConnectionSettings:
        """Build runtime settings, resolving the credential only now.

        The returned object is short-lived by design: it is handed to a
        connector, used, and dropped. It is never persisted or serialized.
        """
        password = None

        if self.credential_ref and secrets is not None:
            password = secrets.resolve(self.credential_ref)

        return ConnectionSettings(
            source_system_id=self.source_id,
            source_type=self.source_type,
            host=self.host,
            port=self.port,
            database=self.database,
            username=self.username,
            password=password,
            ssl_enabled=self.ssl_enabled,
            auth_database=self.auth_database,
        )


#: Keys refused in free-form metadata. A caller who puts a password in a
#: metadata bag would otherwise get it persisted and echoed back.
_FORBIDDEN_METADATA = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "credentials",
    "connection_string",
    "dsn",
    "uri",
    "url",
}


def scrub_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop anything that looks like a credential.

    Belt and braces: the DTO has no password field, but metadata is an open
    dictionary and open dictionaries are where secrets end up.
    """
    if not metadata:
        return {}

    return {
        key: value
        for key, value in metadata.items()
        if key.lower() not in _FORBIDDEN_METADATA
        and not any(marker in key.lower() for marker in ("password", "secret", "token"))
    }


class SourceRegistry:
    """In-process registry of sources the API may act on."""

    def __init__(self) -> None:
        self._sources: dict[str, RegisteredSource] = {}

    def register(self, source: RegisteredSource) -> RegisteredSource:
        cleaned = RegisteredSource(
            source_id=source.source_id,
            name=source.name,
            source_type=source.source_type,
            host=source.host,
            port=source.port,
            database=source.database,
            username=source.username,
            credential_ref=source.credential_ref,
            auth_database=source.auth_database,
            ssl_enabled=source.ssl_enabled,
            description=source.description,
            metadata=scrub_metadata(source.metadata),
            registered_at=source.registered_at,
        )
        self._sources[cleaned.source_id] = cleaned

        return cleaned

    def get(self, source_id: str) -> RegisteredSource:
        source = self._sources.get(source_id)

        if source is None:
            raise SourceNotFoundError(
                f"source {source_id!r} is not registered", source_id=source_id
            )

        return source

    def find(self, source_id: str) -> RegisteredSource | None:
        return self._sources.get(source_id)

    def list(self, limit: int = 100, offset: int = 0) -> tuple[RegisteredSource, ...]:
        ordered = sorted(self._sources.values(), key=lambda s: s.registered_at)

        return tuple(ordered[offset : offset + limit])

    def __len__(self) -> int:
        return len(self._sources)


__all__ = [
    "RegisteredSource",
    "SourceRegistry",
    "normalize_source_id",
    "scrub_metadata",
]
