"""Fixtures for live-database discovery tests.

Two distinct live targets, kept deliberately separate:

``bpi_source_connector``
    The real BPI source database, used READ-ONLY to prove discovery works
    against a genuine pre-existing ERP-shaped source. Never modified.

``probe_schema``
    A throwaway schema created inside the PIPELINE database for tests that
    need controlled DDL (composite keys, FK shapes, a structural change).
    Created and dropped by the fixture; the BPI source is never touched by it.

Both skip - never fail, never fake - when PostgreSQL is unreachable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(_project_root() / ".env")


def _settings(source_system_id: str, database_env: str, default_database: str,
              host_env: str, port_env: str, user_env: str, password_env: str):
    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.schemas.enums import SourceType

    password = os.getenv(password_env)
    if not password:
        pytest.skip(f"{password_env} is not configured in .env")

    return ConnectionSettings(
        source_system_id=source_system_id,
        source_type=SourceType.POSTGRESQL,
        host=os.getenv(host_env, "localhost"),
        port=int(os.getenv(port_env, "5432")),
        database=os.getenv(database_env, default_database),
        username=os.getenv(user_env, "postgres"),
        password=password,
        connect_timeout_seconds=10,
    )


@pytest.fixture(scope="session")
def bpi_source_connector():
    """READ-ONLY connector to the real BPI source database."""
    _load_env()
    from erp_pipeline.connectors.postgresql import PostgreSQLConnector

    settings = _settings(
        source_system_id="finance_erp_pg",
        database_env="BPI_OLD_DB_NAME", default_database="bpi2020_old_erp_db",
        host_env="BPI_OLD_DB_HOST", port_env="BPI_OLD_DB_PORT",
        user_env="BPI_OLD_DB_USER", password_env="BPI_OLD_DB_PASSWORD",
    )

    connector = PostgreSQLConnector(settings)
    try:
        connector.test_connection()
    except Exception as exc:  # noqa: BLE001 - availability probe
        connector.close()
        pytest.skip(f"BPI source PostgreSQL unreachable: {exc}")

    yield connector
    connector.close()


@pytest.fixture(scope="session")
def sakila_connector():
    """READ-ONLY connector to the live MySQL Sakila database.

    Credentials come from ``MYSQL_SAKILA_PASSWORD`` in ``.env`` (gitignored);
    the password is never printed by these tests. The account used
    (``read_only``) holds only ``SELECT, SHOW VIEW`` on ``sakila``, so a write
    is impossible at the database level regardless of what the code does.

    Skips - never fails, never fakes - when the password is unset or the
    server is unreachable.
    """
    _load_env()
    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.connectors.mysql import MySQLConnector
    from erp_pipeline.schemas.enums import SourceType

    password = os.getenv("MYSQL_SAKILA_PASSWORD")
    if not password:
        pytest.skip("MYSQL_SAKILA_PASSWORD is not configured in .env")

    settings = ConnectionSettings(
        source_system_id="mysql_sakila",
        source_type=SourceType.MYSQL,
        host=os.getenv("MYSQL_SAKILA_HOST", "localhost"),
        port=int(os.getenv("MYSQL_SAKILA_PORT", "3306")),
        database=os.getenv("MYSQL_SAKILA_DB", "sakila"),
        username=os.getenv("MYSQL_SAKILA_USER", "read_only"),
        password=password,
        connect_timeout_seconds=10,
    )

    connector = MySQLConnector(settings)
    try:
        connector.test_connection()
    except Exception as exc:  # noqa: BLE001 - availability probe
        connector.close()
        pytest.skip(f"Sakila MySQL unreachable: {exc}")

    yield connector
    connector.close()


@pytest.fixture(scope="session")
def pipeline_connector():
    """Connector to the pipeline database, where probe schemas are created."""
    _load_env()
    from erp_pipeline.connectors.postgresql import PostgreSQLConnector

    settings = _settings(
        source_system_id="discovery_probe_pg",
        database_env="AI_DB_NAME", default_database="erp_ai_native_db",
        host_env="AI_DB_HOST", port_env="AI_DB_PORT",
        user_env="AI_DB_USER", password_env="AI_DB_PASSWORD",
    )

    connector = PostgreSQLConnector(settings)
    try:
        connector.test_connection()
    except Exception as exc:  # noqa: BLE001 - availability probe
        connector.close()
        pytest.skip(f"Pipeline PostgreSQL unreachable: {exc}")

    yield connector
    connector.close()


@pytest.fixture()
def probe_schema(pipeline_connector):
    """Create an isolated schema for DDL-dependent tests, then drop it.

    All CREATE/ALTER/DROP in this project's discovery test suite lives here,
    in test setup - never in the discovery package itself.
    """
    from sqlalchemy import text

    schema_name = "erp_disc_test"
    engine = pipeline_connector._sqlalchemy_engine  # noqa: SLF001 - test setup

    def run(statements: str) -> None:
        with engine.begin() as connection:
            for statement in [s.strip() for s in statements.split(";") if s.strip()]:
                connection.execute(text(statement))

    run(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE; CREATE SCHEMA {schema_name}")

    try:
        yield schema_name, run
    finally:
        run(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
