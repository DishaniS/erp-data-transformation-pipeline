"""MySQL connector: mocked connection-test behavior and dependency handling.

No live MySQL server is required for any test in this file.
"""

import sys
from unittest import mock

import pytest

from erp_pipeline.connectors.config import ConnectionSettings
from erp_pipeline.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorDependencyError,
    ConnectorTimeoutError,
)
from erp_pipeline.connectors.mysql import MySQLConnector
from erp_pipeline.schemas.enums import SourceType


def _fake_settings(**overrides):
    defaults = dict(
        source_system_id="mock_mysql",
        source_type=SourceType.MYSQL,
        host="localhost", port=3306, database="db",
        username="app_user", password="fake-password",
    )
    defaults.update(overrides)
    return ConnectionSettings(**defaults)


# ============================================================
# 20: mocked connection-test behavior
# ============================================================

def test_mysql_test_connection_uses_mocked_engine():
    connector = MySQLConnector(_fake_settings())

    fake_connection = mock.MagicMock()
    fake_connection.execute.return_value.scalar.return_value = "8.0.36-mock"
    fake_engine = mock.MagicMock()
    fake_engine.connect.return_value.__enter__.return_value = fake_connection

    with mock.patch("sqlalchemy.create_engine", return_value=fake_engine):
        result = connector.test_connection()

    assert result.success is True
    assert result.server_version == "8.0.36-mock"
    connector.close()


def test_mysql_source_metadata_uses_mocked_engine():
    connector = MySQLConnector(_fake_settings())

    fake_connection = mock.MagicMock()
    fake_connection.execute.return_value.scalar.return_value = "8.0.36-mock"
    fake_engine = mock.MagicMock()
    fake_engine.connect.return_value.__enter__.return_value = fake_connection

    with mock.patch("sqlalchemy.create_engine", return_value=fake_engine):
        metadata = connector.get_source_metadata()

    assert metadata.server_vendor == "mysql"
    assert metadata.connector_implementation == "MySQLConnector"
    assert metadata.capabilities.relational is True
    assert metadata.capabilities.supports_namespaces is False  # MySQL: schema == database
    connector.close()


# ============================================================
# 24: authentication error normalized safely (mocked)
# ============================================================

def test_mysql_authentication_failure_is_normalized_and_password_not_leaked():
    connector = MySQLConnector(_fake_settings(password="super-secret-value"))

    driver_error = Exception(
        "(pymysql.err.OperationalError) (1045, \"Access denied for user "
        "'app_user'@'localhost' (using password: YES)\")"
    )

    with mock.patch("sqlalchemy.create_engine", side_effect=driver_error):
        with pytest.raises(ConnectorAuthenticationError) as excinfo:
            connector.test_connection()

    assert "super-secret-value" not in str(excinfo.value)
    connector.close()


# ============================================================
# 23: timeout normalized correctly (mocked)
# ============================================================

def test_mysql_timeout_is_normalized():
    connector = MySQLConnector(_fake_settings())

    with mock.patch(
        "sqlalchemy.create_engine",
        side_effect=Exception("(2003, 'Connection timeout')"),
    ):
        with pytest.raises(ConnectorTimeoutError):
            connector.test_connection()

    connector.close()


# ============================================================
# 18: missing optional driver produces ConnectorDependencyError
# ============================================================

def test_mysql_missing_driver_raises_dependency_error():
    connector = MySQLConnector(_fake_settings())

    # Simulates `pymysql` not being installed: `import pymysql` inside
    # _ensure_driver_available raises ImportError, exactly as it would on a
    # machine without the package. No real uninstall required.
    with mock.patch.dict(sys.modules, {"pymysql": None}):
        with pytest.raises(ConnectorDependencyError, match="pymysql"):
            connector.test_connection()

    connector.close()


def test_mysql_connector_module_imports_without_pymysql_installed():
    """import erp_pipeline.connectors.mysql itself must never require pymysql
    at import time - only using the connector should."""
    with mock.patch.dict(sys.modules, {"pymysql": None}):
        # Force a fresh import of the connector module under the simulated
        # missing-dependency condition.
        sys.modules.pop("erp_pipeline.connectors.mysql", None)
        import importlib

        module = importlib.import_module("erp_pipeline.connectors.mysql")
        assert hasattr(module, "MySQLConnector")


def test_mysql_capabilities_available_even_without_driver():
    """Capability reporting is static and must not require the driver."""
    connector = MySQLConnector(_fake_settings())
    with mock.patch.dict(sys.modules, {"pymysql": None}):
        capabilities = connector.get_capabilities()
    assert capabilities.relational is True
    connector.close()
