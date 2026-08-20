"""SQL Server connector: mocked connection-test behavior and dependency handling.

No live SQL Server instance is required for any test in this file. Two
independent "missing dependency" cases are covered: the `pyodbc` package
itself absent, and `pyodbc` present but the configured ODBC driver name not
registered with the system driver manager.
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
from erp_pipeline.connectors.sqlserver import DEFAULT_ODBC_DRIVER, SQLServerConnector
from erp_pipeline.schemas.enums import SourceType


def _fake_settings(**overrides):
    defaults = dict(
        source_system_id="mock_mssql",
        source_type=SourceType.SQL_SERVER,
        host="localhost", port=1433, database="db",
        username="app_user", password="fake-password",
    )
    defaults.update(overrides)
    return ConnectionSettings(**defaults)


# ============================================================
# 21: mocked connection-test behavior
# ============================================================

def test_sqlserver_test_connection_uses_mocked_engine():
    connector = SQLServerConnector(_fake_settings())

    fake_connection = mock.MagicMock()
    fake_connection.execute.return_value.scalar.return_value = (
        "Microsoft SQL Server 2022 (mock)"
    )
    fake_engine = mock.MagicMock()
    fake_engine.connect.return_value.__enter__.return_value = fake_connection

    with mock.patch("pyodbc.drivers", return_value=[DEFAULT_ODBC_DRIVER]):
        with mock.patch("sqlalchemy.create_engine", return_value=fake_engine):
            result = connector.test_connection()

    assert result.success is True
    assert "SQL Server" in result.server_version
    connector.close()


def test_sqlserver_source_metadata_uses_mocked_engine():
    connector = SQLServerConnector(_fake_settings())

    fake_connection = mock.MagicMock()
    fake_connection.execute.return_value.scalar.return_value = "Microsoft SQL Server (mock)"
    fake_engine = mock.MagicMock()
    fake_engine.connect.return_value.__enter__.return_value = fake_connection

    with mock.patch("pyodbc.drivers", return_value=[DEFAULT_ODBC_DRIVER]):
        with mock.patch("sqlalchemy.create_engine", return_value=fake_engine):
            metadata = connector.get_source_metadata()

    assert metadata.server_vendor == "sql_server"
    assert metadata.connector_implementation == "SQLServerConnector"
    assert metadata.capabilities.relational is True
    assert metadata.capabilities.supports_namespaces is True  # SQL Server has schemas
    connector.close()


# ============================================================
# 24: authentication error normalized safely (mocked)
# ============================================================

def test_sqlserver_authentication_failure_is_normalized_and_password_not_leaked():
    connector = SQLServerConnector(_fake_settings(password="super-secret-value"))

    driver_error = Exception(
        "('28000', \"[28000] [Microsoft][ODBC Driver 18 for SQL Server]"
        "Login failed for user 'app_user'.\")"
    )

    with mock.patch("pyodbc.drivers", return_value=[DEFAULT_ODBC_DRIVER]):
        with mock.patch("sqlalchemy.create_engine", side_effect=driver_error):
            with pytest.raises(ConnectorAuthenticationError) as excinfo:
                connector.test_connection()

    assert "super-secret-value" not in str(excinfo.value)
    connector.close()


# ============================================================
# 23: timeout normalized correctly (mocked)
# ============================================================

def test_sqlserver_timeout_is_normalized():
    connector = SQLServerConnector(_fake_settings())

    with mock.patch("pyodbc.drivers", return_value=[DEFAULT_ODBC_DRIVER]):
        with mock.patch(
            "sqlalchemy.create_engine",
            side_effect=Exception("HYT00 Login timeout expired"),
        ):
            with pytest.raises(ConnectorTimeoutError):
                connector.test_connection()

    connector.close()


# ============================================================
# 18: missing dependency - two distinct cases
# ============================================================

def test_sqlserver_missing_pyodbc_package_raises_dependency_error():
    connector = SQLServerConnector(_fake_settings())

    with mock.patch.dict(sys.modules, {"pyodbc": None}):
        with pytest.raises(ConnectorDependencyError, match="pyodbc"):
            connector.test_connection()

    connector.close()


def test_sqlserver_missing_odbc_driver_raises_dependency_error_not_stack_trace():
    """pyodbc IS installed, but the configured ODBC driver name is not
    registered with the system driver manager - a genuinely live, real check
    against this machine's actual pyodbc.drivers() list."""
    connector = SQLServerConnector(
        _fake_settings(driver_options={"driver": "ODBC Driver 99 for SQL Server (fake)"})
    )

    with pytest.raises(ConnectorDependencyError, match="not registered"):
        connector.test_connection()

    connector.close()


def test_sqlserver_driver_name_is_configurable_not_hardcoded():
    connector = SQLServerConnector(
        _fake_settings(driver_options={"driver": "Custom Driver Name"})
    )
    assert connector._configured_driver_name() == "Custom Driver Name"
    connector.close()


def test_sqlserver_default_driver_used_when_unspecified():
    connector = SQLServerConnector(_fake_settings())
    assert connector._configured_driver_name() == DEFAULT_ODBC_DRIVER
    connector.close()


def test_sqlserver_connector_module_imports_without_pyodbc_installed():
    with mock.patch.dict(sys.modules, {"pyodbc": None}):
        sys.modules.pop("erp_pipeline.connectors.sqlserver", None)
        import importlib

        module = importlib.import_module("erp_pipeline.connectors.sqlserver")
        assert hasattr(module, "SQLServerConnector")


def test_sqlserver_capabilities_available_even_without_driver():
    connector = SQLServerConnector(_fake_settings())
    with mock.patch.dict(sys.modules, {"pyodbc": None}):
        capabilities = connector.get_capabilities()
    assert capabilities.relational is True
    connector.close()
