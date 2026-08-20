"""One engine, one database, many schemas.

WHY A SINGLE ENGINE
-------------------
The catalog, sync state, canonical records, jobs and vector tier state are five
different concerns, but they are one database. Giving each its own engine would
multiply connection pools for no benefit and make it possible for them to point
at different servers - which is a class of bug nobody enjoys diagnosing.

Each concern keeps its own PostgreSQL *schema*, so responsibilities stay
separate without the data being scattered.
"""

from __future__ import annotations

import logging
from typing import Any

from erp_pipeline.runtime.settings import ConfigurationError, DatabaseSettings

LOGGER = logging.getLogger("erp_pipeline.runtime.database")

#: Every generic schema this application owns, with the module that owns its
#: DDL. Nothing here re-declares a table - the DDL stays with its module.
OWNED_SCHEMAS = (
    "erp_catalog",
    "erp_sync",
    "erp_vector_storage",
    "erp_orchestration",
    "erp_runtime",
)


def build_pipeline_engine(settings: DatabaseSettings, **kwargs: Any) -> Any:
    """Create the shared SQLAlchemy engine for the AI-native database.

    ``pool_pre_ping`` matters here: a research machine suspends, PostgreSQL
    drops the connection, and without it the next request fails with a stale
    socket rather than transparently reconnecting.
    """
    if not settings.configured:
        raise ConfigurationError(
            "the AI-native PostgreSQL connection is not configured; set "
            "PIPELINE_DB_NAME, PIPELINE_DB_USER and PIPELINE_DB_PASSWORD"
        )

    import sqlalchemy as sa

    options = {"pool_pre_ping": True, "future": True}
    options.update(kwargs)

    # The URL embeds the password, so it is built here and never logged.
    return sa.create_engine(settings.url(), **options)


def check_connection(engine: Any) -> tuple[bool, str | None]:
    """Cheap liveness probe for readiness. Never raises."""
    try:
        import sqlalchemy as sa

        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))

        return True, None
    except Exception as error:  # noqa: BLE001 - readiness must not raise
        # The exception text can contain a DSN, so only the type escapes.
        return False, type(error).__name__


def existing_schemas(engine: Any) -> tuple[str, ...]:
    import sqlalchemy as sa

    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name = ANY(:names)"
            ),
            {"names": list(OWNED_SCHEMAS)},
        ).all()

    return tuple(sorted(row[0] for row in rows))


__all__ = [
    "OWNED_SCHEMAS",
    "build_pipeline_engine",
    "check_connection",
    "existing_schemas",
]
