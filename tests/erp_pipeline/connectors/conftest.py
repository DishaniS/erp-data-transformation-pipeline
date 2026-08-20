"""Fixtures for connector tests.

Live PostgreSQL settings are built from the same ``BPI_OLD_DB_*`` /
``ERP_SOURCE_DB_*`` configuration Phase 0 already uses for the BPI source
database, so this test suite needs no new environment variables. If that
database is unreachable, tests depending on ``live_postgresql_settings`` are
skipped, never failed and never faked.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def live_postgresql_settings():
    from dotenv import load_dotenv

    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.schemas.enums import SourceType

    load_dotenv(_project_root() / ".env")

    password = os.getenv("BPI_OLD_DB_PASSWORD") or os.getenv("ERP_SOURCE_DB_PASSWORD")
    if not password:
        pytest.skip("BPI source PostgreSQL credentials are not configured in .env")

    settings = ConnectionSettings(
        source_system_id="finance_erp_pg",
        source_type=SourceType.POSTGRESQL,
        host=os.getenv("BPI_OLD_DB_HOST", os.getenv("ERP_SOURCE_DB_HOST", "localhost")),
        port=int(os.getenv("BPI_OLD_DB_PORT", os.getenv("ERP_SOURCE_DB_PORT", "5432"))),
        database=os.getenv("BPI_OLD_DB_NAME", os.getenv("ERP_SOURCE_DB_NAME", "bpi2020_old_erp_db")),
        username=os.getenv("BPI_OLD_DB_USER", os.getenv("ERP_SOURCE_DB_USER", "postgres")),
        password=password,
        connect_timeout_seconds=10,
    )

    # Probe once; skip the whole session's live tests if genuinely unreachable
    # rather than letting each test fail with a connection error.
    from erp_pipeline.connectors.postgresql import PostgreSQLConnector

    probe = PostgreSQLConnector(settings)
    try:
        probe.test_connection()
    except Exception as exc:  # noqa: BLE001 - this is the availability probe
        pytest.skip(f"BPI source PostgreSQL unreachable: {exc}")
    finally:
        probe.close()

    return settings
