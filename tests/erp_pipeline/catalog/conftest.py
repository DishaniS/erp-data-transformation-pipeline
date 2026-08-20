"""Fixtures for live-PostgreSQL catalog tests.

Every test in this package that touches the database uses source_system_id
values prefixed ``__pytest_catalog__`` and cleans up its own rows explicitly
at teardown (Task 31's "isolated test source IDs with explicit cleanup").
Nothing here truncates a shared table or otherwise risks Phase 0/Phase 1
research data - the catalog schema (``erp_catalog``) is entirely separate
from the BPI application tables in ``public`` regardless.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from erp_pipeline.catalog.config import CatalogDatabaseSettings
from erp_pipeline.catalog.exceptions import CatalogConnectionError
from erp_pipeline.catalog.repository import CatalogRepository
from erp_pipeline.catalog.schema import bootstrap_catalog
from erp_pipeline.catalog.service import SchemaCatalogService

TEST_PREFIX = "pytest_catalog"


@pytest.fixture(scope="session")
def catalog_engine():
    try:
        settings = CatalogDatabaseSettings.from_env()
        engine = settings.create_engine(connect_args={"connect_timeout": 10})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - this is the availability probe
        pytest.skip(f"Catalog PostgreSQL unavailable: {exc}")

    report = bootstrap_catalog(engine)
    if not report.is_complete:
        pytest.skip(f"Catalog bootstrap incomplete: {report.render()}")

    yield engine
    engine.dispose()


@pytest.fixture()
def repository(catalog_engine) -> CatalogRepository:
    return CatalogRepository(catalog_engine)


@pytest.fixture()
def service(repository) -> SchemaCatalogService:
    return SchemaCatalogService(repository)


@pytest.fixture()
def unique_id():
    """A short, unique, normalized-identifier-safe suffix for this test run."""
    return uuid.uuid4().hex[:10]


@pytest.fixture()
def cleanup(catalog_engine):
    """Register catalog rows for teardown, keyed by table + primary key.

    Usage: ``cleanup.schema_id("my_schema")`` / ``cleanup.source_system_id(...)``
    / ``cleanup.mapping_id(...)``. Cleanup runs even if the test fails, and
    respects FK order (children before parents).
    """

    class _Cleanup:
        def __init__(self) -> None:
            self.schema_ids: list[str] = []
            self.source_system_ids: list[str] = []
            self.mapping_ids: list[str] = []

        def schema_id(self, value: str) -> str:
            self.schema_ids.append(value)
            return value

        def source_system_id(self, value: str) -> str:
            self.source_system_ids.append(value)
            return value

        def mapping_id(self, value: str) -> str:
            self.mapping_ids.append(value)
            return value

    tracker = _Cleanup()
    yield tracker

    with catalog_engine.begin() as connection:
        for mapping_id in tracker.mapping_ids:
            connection.execute(
                text("DELETE FROM erp_catalog.field_mappings WHERE mapping_id = :id"),
                {"id": mapping_id},
            )
            connection.execute(
                text("DELETE FROM erp_catalog.mapping_profiles WHERE mapping_id = :id"),
                {"id": mapping_id},
            )
        for schema_id in tracker.schema_ids:
            connection.execute(
                text("DELETE FROM erp_catalog.source_fields WHERE schema_id = :id"),
                {"id": schema_id},
            )
            connection.execute(
                text("DELETE FROM erp_catalog.source_entities WHERE schema_id = :id"),
                {"id": schema_id},
            )
            connection.execute(
                text("DELETE FROM erp_catalog.source_relationships WHERE schema_id = :id"),
                {"id": schema_id},
            )
            connection.execute(
                text("DELETE FROM erp_catalog.schema_snapshots WHERE schema_id = :id"),
                {"id": schema_id},
            )
        for source_system_id in tracker.source_system_ids:
            connection.execute(
                text(
                    "DELETE FROM erp_catalog.source_systems WHERE source_system_id = :id"
                ),
                {"id": source_system_id},
            )
