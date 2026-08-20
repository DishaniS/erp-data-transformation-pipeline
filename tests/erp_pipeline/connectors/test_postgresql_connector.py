"""PostgreSQL connector: live proof (Step 24) plus mocked structural tests."""

from unittest import mock

import pytest

from erp_pipeline.connectors.config import ConnectionSettings
from erp_pipeline.connectors.errors import ConnectorAuthenticationError, ConnectorTimeoutError
from erp_pipeline.connectors.models import ConnectionTestResult, SourceMetadata
from erp_pipeline.connectors.postgresql import PostgreSQLConnector
from erp_pipeline.schemas.enums import SourceType


# ============================================================
# 19: live PostgreSQL connection-test behavior
# ============================================================

def test_live_postgresql_test_connection(live_postgresql_settings):
    """Uses Phase 3's NEW PostgreSQLConnector against the real BPI source DB.

    Read-only: SELECT 1 and a version() query only. Never writes.
    """
    with PostgreSQLConnector(live_postgresql_settings) as connector:
        result = connector.test_connection()

        assert isinstance(result, ConnectionTestResult)
        assert result.success is True
        assert result.source_system_id == "finance_erp_pg"
        assert result.source_type is SourceType.POSTGRESQL
        assert result.database_name == live_postgresql_settings.database
        assert result.server_version is not None
        assert "PostgreSQL" in result.server_version
        assert result.latency_ms > 0

    assert connector.is_closed is True


def test_live_postgresql_source_metadata(live_postgresql_settings):
    with PostgreSQLConnector(live_postgresql_settings) as connector:
        metadata = connector.get_source_metadata()

        assert isinstance(metadata, SourceMetadata)
        assert metadata.server_vendor == "postgresql"
        assert metadata.connector_implementation == "PostgreSQLConnector"
        assert metadata.driver_name == "psycopg2"
        assert metadata.driver_version is not None
        assert metadata.capabilities.relational is True

        # No credential ever appears in the serialized metadata.
        import json

        serialized = json.dumps(metadata.to_dict())
        assert "password" not in serialized.lower()


def test_live_postgresql_capabilities_report(live_postgresql_settings):
    with PostgreSQLConnector(live_postgresql_settings) as connector:
        capabilities = connector.get_capabilities()
        assert capabilities.relational is True
        assert capabilities.document_database is False
        assert capabilities.supports_transactions is True
        assert capabilities.supports_server_version is True


# ============================================================
# Mocked structural tests (no live server required)
# ============================================================

def _fake_settings():
    return ConnectionSettings(
        source_system_id="mock_pg",
        source_type=SourceType.POSTGRESQL,
        host="localhost", port=5432, database="db",
        username="app_user", password="fake-password",
    )


def test_postgresql_test_connection_uses_mocked_engine():
    connector = PostgreSQLConnector(_fake_settings())

    fake_connection = mock.MagicMock()
    fake_connection.execute.return_value.scalar.return_value = "PostgreSQL 16.0 (mock)"
    fake_engine = mock.MagicMock()
    fake_engine.connect.return_value.__enter__.return_value = fake_connection

    with mock.patch("sqlalchemy.create_engine", return_value=fake_engine):
        result = connector.test_connection()

    assert result.success is True
    assert result.server_version == "PostgreSQL 16.0 (mock)"
    connector.close()


def test_postgresql_authentication_failure_is_normalized():
    connector = PostgreSQLConnector(_fake_settings())

    with mock.patch(
        "sqlalchemy.create_engine",
        side_effect=Exception('connection to server failed: FATAL:  password authentication failed for user "app_user"'),
    ):
        with pytest.raises(ConnectorAuthenticationError) as excinfo:
            connector.test_connection()

    assert "fake-password" not in str(excinfo.value)
    connector.close()


def test_postgresql_timeout_is_normalized():
    connector = PostgreSQLConnector(_fake_settings())

    with mock.patch(
        "sqlalchemy.create_engine",
        side_effect=Exception("connection to server timed out"),
    ):
        with pytest.raises(ConnectorTimeoutError):
            connector.test_connection()

    connector.close()


def test_postgresql_url_never_includes_raw_password_in_str():
    connector = PostgreSQLConnector(_fake_settings())
    url = connector._build_url()

    assert "fake-password" not in str(url)
    assert "fake-password" not in repr(url)
    connector.close()
