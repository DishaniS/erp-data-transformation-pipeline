"""Shared Qdrant connection configuration for ingestion and retrieval."""

from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import QdrantClient

from bpi2020.common.config import get_int_setting, get_setting


@dataclass(frozen=True)
class QdrantSettings:
    """Vector database settings with a local-server fallback.

    Canonical variables are ``VECTOR_DB_URL`` / ``VECTOR_DB_API_KEY``; the
    original ``QDRANT_*`` names still work as deprecated aliases (see
    ``bpi2020.common.config.LEGACY_ALIASES``).
    """

    url: str | None
    api_key: str | None
    host: str
    port: int
    timeout_seconds: int

    @classmethod
    def from_env(cls) -> "QdrantSettings":
        return cls(
            url=get_setting("VECTOR_DB_URL"),
            api_key=get_setting("VECTOR_DB_API_KEY"),
            host=get_setting("VECTOR_DB_HOST", "localhost") or "localhost",
            port=get_int_setting("VECTOR_DB_PORT", 6333),
            timeout_seconds=get_int_setting("VECTOR_DB_TIMEOUT_SECONDS", 60),
        )

    @property
    def target(self) -> str:
        """Return a safe display value that never includes credentials."""
        return self.url or f"http://{self.host}:{self.port}"

    def create_client(self) -> QdrantClient:
        if self.url:
            return QdrantClient(
                url=self.url,
                api_key=self.api_key,
                timeout=self.timeout_seconds,
            )

        return QdrantClient(
            host=self.host,
            port=self.port,
            api_key=self.api_key,
            timeout=self.timeout_seconds,
        )
