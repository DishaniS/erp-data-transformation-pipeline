"""PostgreSQL configuration for the schema catalog's physical storage.

Scoped to exactly what the catalog needs, and no wider. It implements the
canonical-name-with-deprecated-fallback pattern this repository uses for every
setting, so an older variable keeps working while printing a warning.

Logical vs physical
--------------------
The catalog's *logical* model is the Phase 1 contracts (``SourceSystem``,
``SourceSchema``, ...). PostgreSQL is only the *physical* engine chosen to
store them, addressed here via the project's existing pipeline database. A
later phase could swap the physical engine without touching
``erp_pipeline.schemas`` at all - only this module and ``repository.py`` would
change.

Canonical variable names
-------------------------
    PIPELINE_DB_HOST
    PIPELINE_DB_PORT
    PIPELINE_DB_NAME
    PIPELINE_DB_USER
    PIPELINE_DB_PASSWORD

``AI_DB_*`` is accepted as a deprecated fallback (the same legacy name Phase 0
documents in ``.env.example``). Rules, matching Phase 0's contract:

* canonical value present            -> used
* only the legacy value present      -> used, with a deprecation warning
* both present and equal             -> used, no warning
* both present and DIFFERENT         -> canonical wins, loud warning

Values are never printed, anywhere. Only host, port and database name appear
in diagnostics (``CatalogDatabaseSettings.safe_target``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from erp_pipeline.catalog.exceptions import CatalogConfigurationError

# Fixed PostgreSQL schema namespace for all catalog tables. Never taken from
# user input or interpolated - see the security note in repository.py.
CATALOG_SCHEMA_NAME = "erp_catalog"

_CANONICAL_PREFIX = "PIPELINE_DB"
_LEGACY_PREFIX = "AI_DB"

_env_loaded = False
_warned: set[str] = set()


def _project_root() -> Path:
    # src/erp_pipeline/catalog/config.py -> project root is four parents up.
    return Path(__file__).resolve().parents[3]


def _load_project_env() -> None:
    """Load ``.env`` from the project root exactly once.

    Imported lazily so importing this module never requires ``python-dotenv``
    to be installed unless a catalog operation is actually attempted -
    consistent with Phase 1's stance that ``erp_pipeline.schemas`` needs no
    third-party package. ``erp_pipeline.catalog`` is explicitly allowed to
    depend on the project's existing dependencies (Task 32), and
    ``python-dotenv`` is already one of them.
    """
    global _env_loaded
    if _env_loaded:
        return

    from dotenv import load_dotenv

    load_dotenv(_project_root() / ".env")
    _env_loaded = True


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"CATALOG CONFIG WARNING: {message}")


def _read_setting(suffix: str, default: str | None = None, required: bool = False) -> str | None:
    """Resolve one ``PIPELINE_DB_*`` setting, honouring the ``AI_DB_*`` fallback."""
    _load_project_env()

    canonical_name = f"{_CANONICAL_PREFIX}_{suffix}"
    legacy_name = f"{_LEGACY_PREFIX}_{suffix}"
    is_secret = suffix == "PASSWORD"

    def read(name: str) -> str | None:
        raw = os.getenv(name)
        if raw is None:
            return None
        raw = raw.strip()
        return raw or None

    canonical_value = read(canonical_name)
    legacy_value = read(legacy_name)

    if canonical_value is not None:
        if legacy_value is not None and legacy_value != canonical_value:
            detail = "values differ" if is_secret else f"legacy value {legacy_value!r} ignored"
            _warn_once(
                f"conflict:{canonical_name}",
                f"{canonical_name} and deprecated {legacy_name} are both set and "
                f"{detail}. Using {canonical_name}. Remove {legacy_name} from .env.",
            )
        return canonical_value

    if legacy_value is not None:
        _warn_once(
            f"deprecated:{legacy_name}",
            f"{legacy_name} is deprecated for the schema catalog. Rename it to "
            f"{canonical_name} in .env.",
        )
        return legacy_value

    if required:
        raise CatalogConfigurationError(
            f"Required setting {canonical_name} is not set in .env (deprecated "
            f"alias: {legacy_name}). Copy .env.example to .env and fill it in."
        )

    return default


@dataclass(frozen=True)
class CatalogDatabaseSettings:
    """Connection settings for the catalog's physical PostgreSQL storage."""

    host: str
    port: int
    database: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> "CatalogDatabaseSettings":
        port_raw = _read_setting("PORT", "5432")
        try:
            port = int(port_raw) if port_raw is not None else 5432
        except ValueError as exc:
            raise CatalogConfigurationError(
                f"{_CANONICAL_PREFIX}_PORT must be an integer, got {port_raw!r}."
            ) from exc

        return cls(
            host=_read_setting("HOST", "localhost") or "localhost",
            port=port,
            database=_read_setting("NAME", "erp_ai_native_db") or "erp_ai_native_db",
            user=_read_setting("USER", "postgres") or "postgres",
            password=_read_setting("PASSWORD", required=True) or "",
        )

    @property
    def safe_target(self) -> str:
        """Display value that never contains credentials."""
        return f"{self.host}:{self.port}/{self.database}"

    def url(self):
        """Build a SQLAlchemy URL. ``URL.create`` escapes the password safely."""
        from sqlalchemy import URL

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


__all__ = [
    "CATALOG_SCHEMA_NAME",
    "CatalogDatabaseSettings",
]
