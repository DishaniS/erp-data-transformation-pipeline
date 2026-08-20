"""Runtime configuration for a deployed instance.

WHY THIS EXISTS SEPARATELY FROM ``ApiSettings``
-----------------------------------------------
``ApiSettings`` describes the HTTP surface. This describes the *infrastructure*
the application binds to: which PostgreSQL holds canonical state, where Qdrant
lives, where cold archives are written. Keeping them apart means a test can
vary one without dragging in the other.

EVERY VALUE COMES FROM THE ENVIRONMENT
--------------------------------------
Nothing here is hard-coded to a machine. Defaults are loopback-friendly for
local development, and `validate()` refuses configurations that would be
unsafe or non-functional rather than failing later in a request.

Secrets (the DB password, the Qdrant API key, the cold key) are read but never
rendered: ``__repr__`` and ``describe()`` redact them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from erp_pipeline.api.config import ApiSettings

#: Canonical prefix for the AI-native database, matching Phase 2's catalog
#: configuration so one database serves every generic schema.
PIPELINE_DB_PREFIX = "PIPELINE_DB"
LEGACY_DB_PREFIX = "AI_DB"

COLD_KEY_VARIABLE = "ERP_COLD_ARCHIVE_KEY"


def _env(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)

    if raw is None:
        return default

    raw = raw.strip()

    return raw or default


def _db_setting(suffix: str, default: str | None = None) -> str | None:
    """Read ``PIPELINE_DB_*`` with the documented ``AI_DB_*`` fallback.

    Phase 2 already established this pair. Introducing a third name for the
    same database would guarantee that two of them eventually disagree.
    """
    return _env(f"{PIPELINE_DB_PREFIX}_{suffix}") or _env(
        f"{LEGACY_DB_PREFIX}_{suffix}", default
    )


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name)

    if raw is None:
        return default

    return raw.lower() in {"1", "true", "yes", "on"}


class ConfigurationError(RuntimeError):
    """Raised when configuration is missing, contradictory or unsafe.

    Deliberately raised at startup rather than on first use: a service that
    boots and then fails every request is harder to diagnose than one that
    refuses to boot and says why.
    """


@dataclass(frozen=True)
class DatabaseSettings:
    """The AI-native PostgreSQL that holds all generic runtime state."""

    host: str = "localhost"
    port: int = 5432
    database: str = "erp_ai_native_db"
    user: str = "postgres"
    password: str | None = None

    @classmethod
    def from_environment(cls) -> "DatabaseSettings":
        return cls(
            host=_db_setting("HOST", "localhost") or "localhost",
            port=int(_db_setting("PORT", "5432") or 5432),
            database=_db_setting("NAME", "erp_ai_native_db") or "erp_ai_native_db",
            user=_db_setting("USER", "postgres") or "postgres",
            password=_db_setting("PASSWORD"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.database and self.user and self.password)

    def url(self) -> str:
        """Build a SQLAlchemy URL. Never log the result - it embeds the password."""
        from sqlalchemy.engine import URL

        return URL.create(
            drivername="postgresql+psycopg2",
            username=self.user,
            password=self.password or "",
            host=self.host,
            port=self.port,
            database=self.database,
        ).render_as_string(hide_password=False)

    def describe(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "password": "[REDACTED]" if self.password else "[MISSING]",
        }

    def __repr__(self) -> str:
        return (
            f"DatabaseSettings(host={self.host!r}, port={self.port}, "
            f"database={self.database!r}, user={self.user!r}, "
            "password=<redacted>)"
        )


@dataclass(frozen=True)
class QdrantSettings:
    """Generic vector-store configuration.

    Deliberately does NOT reuse the BPI ``VECTOR_DB_*`` variables: those belong
    to the BPI prototype and point at its production collection. Generic code
    that read them would inherit a BPI deployment by accident.
    """

    url: str | None = None
    host: str = "localhost"
    port: int = 6333
    api_key: str | None = None
    hot_collection: str = "erp_vectors_hot"
    warm_collection: str = "erp_vectors_warm"
    dimension: int = 384
    timeout_seconds: int = 60
    enabled: bool = True

    @classmethod
    def from_environment(cls) -> "QdrantSettings":
        return cls(
            url=_env("ERP_QDRANT_URL"),
            host=_env("ERP_QDRANT_HOST", "localhost") or "localhost",
            port=int(_env("ERP_QDRANT_PORT", "6333") or 6333),
            api_key=_env("ERP_QDRANT_API_KEY"),
            hot_collection=_env("ERP_QDRANT_HOT_COLLECTION", "erp_vectors_hot")
            or "erp_vectors_hot",
            warm_collection=_env("ERP_QDRANT_WARM_COLLECTION", "erp_vectors_warm")
            or "erp_vectors_warm",
            dimension=int(_env("ERP_QDRANT_DIMENSION", "384") or 384),
            timeout_seconds=int(_env("ERP_QDRANT_TIMEOUT_SECONDS", "60") or 60),
            enabled=_flag("ERP_QDRANT_ENABLED", True),
        )

    @property
    def uses_url(self) -> bool:
        return bool(self.url)

    def describe(self) -> dict[str, Any]:
        return {
            "mode": "url" if self.uses_url else "host_port",
            "url": self.url if self.uses_url else None,
            "host": None if self.uses_url else self.host,
            "port": None if self.uses_url else self.port,
            "api_key": "[REDACTED]" if self.api_key else "[NOT SET]",
            "hot_collection": self.hot_collection,
            "warm_collection": self.warm_collection,
            "dimension": self.dimension,
            "enabled": self.enabled,
        }

    def __repr__(self) -> str:
        target = self.url if self.uses_url else f"{self.host}:{self.port}"

        return (
            f"QdrantSettings(target={target!r}, "
            f"hot={self.hot_collection!r}, warm={self.warm_collection!r}, "
            "api_key=<redacted>)"
        )


@dataclass(frozen=True)
class ColdSettings:
    """Encrypted archive configuration.

    The directory is configurable because a real deployment puts archives on a
    backed-up volume, not beside the code. The key is read from the
    environment by Phase 12's own provider and never travels through here.
    """

    enabled: bool = True
    directory: Path = field(default_factory=lambda: Path("var/cold-archive"))

    @classmethod
    def from_environment(cls) -> "ColdSettings":
        return cls(
            enabled=_flag("ERP_COLD_ENABLED", True),
            directory=Path(
                _env("ERP_COLD_ARCHIVE_DIR", "var/cold-archive") or "var/cold-archive"
            ),
        )

    @property
    def key_present(self) -> bool:
        return bool(os.environ.get(COLD_KEY_VARIABLE))

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "directory": str(self.directory),
            "key_variable": COLD_KEY_VARIABLE,
            "key": "[REDACTED]" if self.key_present else "[MISSING]",
        }


@dataclass(frozen=True)
class RuntimeSettings:
    """Everything a deployed instance needs to assemble itself."""

    api: ApiSettings = field(default_factory=ApiSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    qdrant: QdrantSettings = field(default_factory=QdrantSettings)
    cold: ColdSettings = field(default_factory=ColdSettings)

    #: When true, startup creates any missing owned schema. Safe for local and
    #: demo use; an operator running a managed database will want it off and
    #: will run the bootstrap command explicitly instead.
    bootstrap_on_startup: bool = True

    #: Loading the model is deferred until something actually embeds, so a
    #: machine with no model cache can still serve everything else.
    embedding_enabled: bool = True

    executor_workers: int = 2

    #: Escape hatch for deliberately binding a non-loopback address without a
    #: key. Named to be uncomfortable to type on purpose.
    allow_insecure_bind: bool = False

    @classmethod
    def from_environment(cls, load_dotenv: bool = True) -> "RuntimeSettings":
        if load_dotenv:
            _load_project_env()

        return cls(
            api=ApiSettings.from_environment(),
            database=DatabaseSettings.from_environment(),
            qdrant=QdrantSettings.from_environment(),
            cold=ColdSettings.from_environment(),
            bootstrap_on_startup=_flag("ERP_BOOTSTRAP_ON_STARTUP", True),
            embedding_enabled=_flag("ERP_EMBEDDING_ENABLED", True),
            executor_workers=int(_env("ERP_EXECUTOR_WORKERS", "2") or 2),
            allow_insecure_bind=_flag("ERP_ALLOW_INSECURE_BIND", False),
        )

    # ------------------------------------------------------------------

    def validate(self) -> tuple[str, ...]:
        """Return the reasons this configuration cannot be deployed.

        Returning a list rather than raising on the first problem means an
        operator fixes everything in one pass instead of one restart per typo.
        """
        problems: list[str] = []

        if not self.database.configured:
            missing = []

            if not self.database.database:
                missing.append(f"{PIPELINE_DB_PREFIX}_NAME")
            if not self.database.user:
                missing.append(f"{PIPELINE_DB_PREFIX}_USER")
            if not self.database.password:
                missing.append(f"{PIPELINE_DB_PREFIX}_PASSWORD")

            problems.append(
                "the AI-native PostgreSQL connection is incomplete; set "
                + ", ".join(missing)
            )

        # An unauthenticated service on a routable address is the single most
        # dangerous misconfiguration this system can have, so it is refused.
        if self._binds_externally() and not self.api.auth_enabled:
            if not self.allow_insecure_bind:
                problems.append(
                    f"the API is configured to bind {self.api.host!r}, which is "
                    "reachable from the network, but ERP_API_KEY is not set. "
                    "Set ERP_API_KEY, bind 127.0.0.1, or set "
                    "ERP_ALLOW_INSECURE_BIND=true if this is a deliberate "
                    "isolated experiment."
                )

        if self.cold.enabled and not self.cold.key_present:
            problems.append(
                f"the cold tier is enabled but {COLD_KEY_VARIABLE} is not set. "
                "Archives are never written unencrypted, so cold storage "
                "cannot start. Set the key or set ERP_COLD_ENABLED=false."
            )

        if not self.api.upload_dir:
            problems.append("ERP_API_UPLOAD_DIR resolves to an empty path")

        return tuple(problems)

    def _binds_externally(self) -> bool:
        host = (self.api.host or "").strip().lower()

        return host not in {"127.0.0.1", "localhost", "::1", ""}

    def require_valid(self) -> None:
        problems = self.validate()

        if problems:
            raise ConfigurationError(
                "the runtime configuration is not deployable:\n  - "
                + "\n  - ".join(problems)
            )

    def describe(self) -> dict[str, Any]:
        """A safe, fully redacted view for logs and the readiness endpoint."""
        return {
            "api": {
                "host": self.api.host,
                "port": self.api.port,
                "auth_enabled": self.api.auth_enabled,
                "api_key": "[REDACTED]" if self.api.auth_enabled else "[NOT SET]",
                "cors_origins": list(self.api.cors_origins),
                "upload_dir": str(self.api.upload_dir),
                "max_upload_bytes": self.api.max_upload_bytes,
            },
            "database": self.database.describe(),
            "qdrant": self.qdrant.describe(),
            "cold": self.cold.describe(),
            "bootstrap_on_startup": self.bootstrap_on_startup,
            "embedding_enabled": self.embedding_enabled,
            "executor_workers": self.executor_workers,
        }


def _load_project_env() -> None:
    """Load ``.env`` if python-dotenv is available. Never overrides real env."""
    try:
        from dotenv import load_dotenv
    except Exception:  # pragma: no cover - dotenv is optional
        return

    load_dotenv(override=False)


__all__ = [
    "PIPELINE_DB_PREFIX",
    "LEGACY_DB_PREFIX",
    "COLD_KEY_VARIABLE",
    "ConfigurationError",
    "DatabaseSettings",
    "QdrantSettings",
    "ColdSettings",
    "RuntimeSettings",
]
