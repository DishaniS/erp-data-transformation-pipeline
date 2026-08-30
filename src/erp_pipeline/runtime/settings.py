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

#: Keys a dynamic (catalog-driven) filter value into an HMAC token before it
#: ever reaches a Qdrant payload - see ``ai.service.EmbeddingService`` and
#: ``schemas.search_fields.filter_value_token``. Read directly from the
#: environment at the point of use, the same as ``COLD_KEY_VARIABLE``: the
#: key itself never travels through a settings object.
FILTER_TOKEN_KEY_VARIABLE = "ERP_FILTER_TOKEN_KEY"


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


#: Qdrant deployment targets. ``cloud`` addresses a managed cluster by URL and
#: requires an API key; ``local`` addresses a development instance by host and
#: port. Local is never chosen implicitly when cloud settings are present.
CLOUD_MODE = "cloud"
LOCAL_MODE = "local"


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
    #: What the deployment MEANT to connect to. ``None`` infers it; an explicit
    #: value is what makes local Qdrant a deliberate choice rather than the
    #: thing you get when configuration silently fails to arrive.
    declared_mode: str | None = None

    @classmethod
    def from_environment(cls) -> "QdrantSettings":
        return cls(
            declared_mode=(_env("ERP_QDRANT_MODE") or "").strip().lower() or None,
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

    @property
    def deployment(self) -> str:
        """``cloud`` or ``local``.

        Inferred when not declared: supplying a URL or an API key is only ever
        an attempt to reach a managed cluster, so it is read as cloud intent.
        That inference is what stops a typo in either variable from quietly
        becoming a localhost connection.
        """
        if self.declared_mode in {CLOUD_MODE, LOCAL_MODE}:
            return self.declared_mode

        return CLOUD_MODE if (self.url or self.api_key) else LOCAL_MODE

    def validate(self) -> None:
        """Refuse to start on a half-configured cluster.

        Called before the client is built, so a cloud deployment missing its
        URL or key FAILS rather than connecting to whatever happens to be
        listening on localhost. The message names the variables and never
        contains the key itself.
        """
        if not self.enabled:
            return

        if self.declared_mode is not None and self.declared_mode not in {
            CLOUD_MODE,
            LOCAL_MODE,
        }:
            raise ConfigurationError(
                f"ERP_QDRANT_MODE={self.declared_mode!r} is not valid; "
                f"expected {CLOUD_MODE!r} or {LOCAL_MODE!r}"
            )

        if self.deployment != CLOUD_MODE:
            return

        missing = []

        if not self.url:
            missing.append("ERP_QDRANT_URL")

        if not self.api_key:
            missing.append("ERP_QDRANT_API_KEY")

        if missing:
            raise ConfigurationError(
                "Qdrant Cloud is selected but "
                + " and ".join(missing)
                + (" are" if len(missing) > 1 else " is")
                + " not set. Vectors are NOT written to localhost as a "
                "fallback. Set the missing variable, or set "
                f"ERP_QDRANT_MODE={LOCAL_MODE} to use a local Qdrant on "
                f"{self.host}:{self.port} deliberately."
            )

    def describe(self) -> dict[str, Any]:
        return {
            "mode": "url" if self.uses_url else "host_port",
            "deployment": self.deployment,
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


#: Tier deployment locations. The enum values are the ones
#: ``erp_pipeline.storage.models.StorageLocation`` already defines:
#: ``on_premises`` and ``external``.
STORAGE_LOCATION_VARIABLES: Mapping[str, str] = {
    "hot": "ERP_STORAGE_HOT_LOCATION",
    "warm": "ERP_STORAGE_WARM_LOCATION",
    "cold": "ERP_STORAGE_COLD_LOCATION",
}


@dataclass(frozen=True)
class StorageLocationSettings:
    """Where each tier's data PHYSICALLY resides in this deployment.

    WHY THIS EXISTS
    ---------------
    The storage policy restricts RESTRICTED data to on-premises tiers, and the
    router genuinely enforces it. But the location map was a code-level constant
    declaring all three tiers on-premises, written when that was true. Once HOT
    and WARM moved to managed Qdrant the constraint kept being enforced against
    a map that no longer described reality, so it excluded nothing.

    A compliance control that reads a stale constant is worse than no control:
    it reports success while delivering nothing.

    THE DEFAULTS ARE INFERRED, NOT ASSUMED
    --------------------------------------
    HOT and WARM default to their FACTUAL location, taken from the Qdrant
    deployment mode the system already knows. A cluster addressed by URL with an
    API key is not on-premises, and no operator should have to remember to say
    so a second time.

    COLD cannot be inferred - a filesystem path looks identical whether it is a
    local disk or a mounted cloud share - so it defaults to ``on_premises`` and
    a deployment on cloud storage MUST declare it. Azure Files is cloud storage.
    """

    hot: str = "on_premises"
    warm: str = "on_premises"
    cold: str = "on_premises"

    @classmethod
    def from_environment(cls, qdrant: "QdrantSettings | None" = None) -> "StorageLocationSettings":
        # HOT and WARM live wherever Qdrant lives. Inferring this is the
        # difference between a control that works and one that has to be
        # remembered.
        inferred = (
            "external"
            if qdrant is not None and qdrant.deployment == CLOUD_MODE
            else "on_premises"
        )

        return cls(
            hot=(_env(STORAGE_LOCATION_VARIABLES["hot"]) or inferred).strip().lower(),
            warm=(_env(STORAGE_LOCATION_VARIABLES["warm"]) or inferred).strip().lower(),
            cold=(
                _env(STORAGE_LOCATION_VARIABLES["cold"]) or "on_premises"
            ).strip().lower(),
        )

    def validate(self) -> None:
        """Refuse an unrecognised location rather than guessing at it.

        Guessing here would mean guessing whether restricted data may be
        stored somewhere, which is not a guess worth making.
        """
        from erp_pipeline.storage.models import StorageLocation

        allowed = {location.value for location in StorageLocation}

        for tier, variable in STORAGE_LOCATION_VARIABLES.items():
            value = getattr(self, tier)

            if value not in allowed:
                raise ConfigurationError(
                    f"{variable}={value!r} is not a valid storage location; "
                    f"expected one of: {', '.join(sorted(allowed))}"
                )

    def as_tier_map(self) -> dict[Any, Any]:
        """The mapping ``StoragePolicy.tier_locations`` expects."""
        from erp_pipeline.storage.models import StorageLocation, StorageTier

        self.validate()

        return {
            StorageTier.HOT: StorageLocation(self.hot),
            StorageTier.WARM: StorageLocation(self.warm),
            StorageTier.COLD: StorageLocation(self.cold),
        }

    def describe(self) -> dict[str, Any]:
        return {"hot": self.hot, "warm": self.warm, "cold": self.cold}


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
    #: Where each tier physically lives. Drives the restricted-data
    #: constraint in StoragePolicy.
    storage_locations: StorageLocationSettings = field(
        default_factory=StorageLocationSettings
    )

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

        # Resolved first: HOT/WARM locations are inferred from it.
        _qdrant = QdrantSettings.from_environment()

        return cls(
            api=ApiSettings.from_environment(),
            database=DatabaseSettings.from_environment(),
            qdrant=_qdrant,
            cold=ColdSettings.from_environment(),
            storage_locations=StorageLocationSettings.from_environment(_qdrant),
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

        try:
            self.storage_locations.validate()
        except ConfigurationError as error:
            problems.append(str(error))

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
            "storage_locations": self.storage_locations.describe(),
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
