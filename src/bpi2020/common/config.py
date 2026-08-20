"""Canonical environment configuration for the BPI 2020 pipeline.

Before Phase 0 the same PostgreSQL server was addressed through three different
variable prefixes (``BPI_OLD_DB_*``, ``OLD_DB_*``, ``AI_DB_*``). ``OLD_DB_*``
was not present in ``.env`` at all, so two scripts silently fell back to a
hardcoded password baked into the source.

This module defines one canonical name per setting and keeps the historical
names working as deprecated fallbacks:

* canonical value present            -> used
* only a legacy value present        -> used, with a deprecation warning
* both present and equal             -> used, no warning
* both present and DIFFERENT         -> canonical wins, loud warning

Values are never printed. Only variable names, hosts, ports and database names
appear in diagnostics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import URL

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Canonical name -> ordered legacy fallbacks (highest priority first).
LEGACY_ALIASES: dict[str, tuple[str, ...]] = {
    "ERP_SOURCE_DB_HOST": ("BPI_OLD_DB_HOST", "OLD_DB_HOST"),
    "ERP_SOURCE_DB_PORT": ("BPI_OLD_DB_PORT", "OLD_DB_PORT"),
    "ERP_SOURCE_DB_NAME": ("BPI_OLD_DB_NAME", "OLD_DB_NAME"),
    "ERP_SOURCE_DB_USER": ("BPI_OLD_DB_USER", "OLD_DB_USER"),
    "ERP_SOURCE_DB_PASSWORD": ("BPI_OLD_DB_PASSWORD", "OLD_DB_PASSWORD"),
    "PIPELINE_DB_HOST": ("AI_DB_HOST",),
    "PIPELINE_DB_PORT": ("AI_DB_PORT",),
    "PIPELINE_DB_NAME": ("AI_DB_NAME",),
    "PIPELINE_DB_USER": ("AI_DB_USER",),
    "PIPELINE_DB_PASSWORD": ("AI_DB_PASSWORD",),
    "VECTOR_DB_URL": ("QDRANT_URL",),
    "VECTOR_DB_API_KEY": ("QDRANT_API_KEY",),
    "VECTOR_DB_HOST": ("QDRANT_HOST",),
    "VECTOR_DB_PORT": ("QDRANT_PORT",),
    "VECTOR_DB_TIMEOUT_SECONDS": ("QDRANT_TIMEOUT_SECONDS",),
    "VECTOR_DB_RECREATE_COLLECTION": ("QDRANT_RECREATE_COLLECTION",),
    "VECTOR_COLLECTION": ("QDRANT_COLLECTION",),
    "EMBEDDING_MODEL_ID": ("EMBEDDING_MODEL",),
    "TESSERACT_CMD": ("TESSERACT_PATH",),
}

# Variables whose values must never be echoed, even in a conflict warning.
SECRET_SETTINGS = frozenset(
    {
        "ERP_SOURCE_DB_PASSWORD",
        "PIPELINE_DB_PASSWORD",
        "VECTOR_DB_API_KEY",
    }
)

_env_loaded = False
_warned: set[str] = set()


class ConfigurationError(RuntimeError):
    """Raised when a required setting is missing or unusable."""


def load_project_env() -> None:
    """Load ``.env`` from the project root exactly once."""
    global _env_loaded
    if not _env_loaded:
        load_dotenv(PROJECT_ROOT / ".env")
        _env_loaded = True


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"CONFIG WARNING: {message}")


def get_setting(
    canonical: str,
    default: str | None = None,
    required: bool = False,
) -> str | None:
    """Resolve one setting by canonical name, honouring deprecated aliases."""
    load_project_env()

    def read(name: str) -> str | None:
        raw = os.getenv(name)
        if raw is None:
            return None
        raw = raw.strip()
        return raw or None

    canonical_value = read(canonical)
    aliases = LEGACY_ALIASES.get(canonical, ())

    legacy_name: str | None = None
    legacy_value: str | None = None
    for alias in aliases:
        value = read(alias)
        if value is not None:
            legacy_name, legacy_value = alias, value
            break

    if canonical_value is not None:
        if legacy_value is not None and legacy_value != canonical_value:
            detail = (
                "values differ"
                if canonical in SECRET_SETTINGS
                else f"legacy value {legacy_value!r} ignored"
            )
            _warn_once(
                f"conflict:{canonical}",
                f"{canonical} and deprecated {legacy_name} are both set and {detail}. "
                f"Using {canonical}. Remove {legacy_name} from .env.",
            )
        return canonical_value

    if legacy_value is not None:
        _warn_once(
            f"deprecated:{legacy_name}",
            f"{legacy_name} is deprecated. Rename it to {canonical} in .env.",
        )
        return legacy_value

    if required:
        alias_hint = f" (deprecated aliases: {', '.join(aliases)})" if aliases else ""
        raise ConfigurationError(
            f"Required setting {canonical} is not set in .env{alias_hint}. "
            "Copy .env.example to .env and fill it in."
        )

    return default


def get_int_setting(canonical: str, default: int) -> int:
    value = get_setting(canonical)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(
            f"{canonical} must be an integer, got {value!r}."
        ) from exc


def get_bool_setting(canonical: str, default: bool = False) -> bool:
    value = get_setting(canonical)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PostgresSettings:
    """Connection settings for one PostgreSQL database."""

    label: str
    host: str
    port: int
    database: str
    user: str
    password: str

    @classmethod
    def _from_prefix(cls, label: str, prefix: str, default_database: str) -> "PostgresSettings":
        return cls(
            label=label,
            host=get_setting(f"{prefix}_HOST", "localhost") or "localhost",
            port=get_int_setting(f"{prefix}_PORT", 5432),
            database=get_setting(f"{prefix}_NAME", default_database) or default_database,
            user=get_setting(f"{prefix}_USER", "postgres") or "postgres",
            password=get_setting(f"{prefix}_PASSWORD", required=True) or "",
        )

    @classmethod
    def erp_source(cls) -> "PostgresSettings":
        """Simulated legacy ERP database (``bpi2020_old_erp_db``)."""
        return cls._from_prefix("ERP source DB", "ERP_SOURCE_DB", "bpi2020_old_erp_db")

    @classmethod
    def pipeline(cls) -> "PostgresSettings":
        """AI-native pipeline database (``erp_ai_native_db``)."""
        return cls._from_prefix("Pipeline DB", "PIPELINE_DB", "erp_ai_native_db")

    @property
    def safe_target(self) -> str:
        """Display value that never contains credentials."""
        return f"{self.host}:{self.port}/{self.database}"

    def url(self) -> URL:
        """Build a SQLAlchemy URL. ``URL.create`` escapes the password safely."""
        return URL.create(
            drivername="postgresql+psycopg2",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
        )

    def create_engine(self, **kwargs):
        from sqlalchemy import create_engine

        kwargs.setdefault("pool_pre_ping", True)
        return create_engine(self.url(), **kwargs)


@dataclass(frozen=True)
class EmbeddingSettings:
    """Embedding model configuration."""

    model_id: str
    batch_size: int

    @classmethod
    def from_env(cls) -> "EmbeddingSettings":
        return cls(
            model_id=get_setting(
                "EMBEDDING_MODEL_ID",
                "sentence-transformers/all-MiniLM-L6-v2",
            )
            or "sentence-transformers/all-MiniLM-L6-v2",
            batch_size=get_int_setting("EMBEDDING_BATCH_SIZE", 64),
        )


def get_vector_collection() -> str:
    return get_setting("VECTOR_COLLECTION", "bpi2020_erp_knowledge") or "bpi2020_erp_knowledge"


def get_tesseract_cmd() -> str | None:
    return get_setting("TESSERACT_CMD")
