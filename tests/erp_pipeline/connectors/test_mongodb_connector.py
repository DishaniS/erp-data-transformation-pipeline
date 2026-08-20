"""MongoDB connector: mocked ping behavior and dependency handling.

No live MongoDB server is required for any test in this file.
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
from erp_pipeline.connectors.mongodb import MongoDBConnector
from erp_pipeline.schemas.enums import SourceType


def _fake_settings(**overrides):
    defaults = dict(
        source_system_id="mock_mongo",
        source_type=SourceType.MONGODB,
        host="localhost", port=27017, database="db",
        username="app_user", password="fake-password",
    )
    defaults.update(overrides)
    return ConnectionSettings(**defaults)


def _install_fake_pymongo_client(connector, ping_result=None, ping_error=None, version="7.0.0-mock"):
    """Replace connector._driver_module with a fake pymongo-like module whose
    MongoClient returns a MagicMock client, wired to succeed or fail."""
    fake_client = mock.MagicMock()
    if ping_error is not None:
        fake_client.admin.command.side_effect = ping_error
    else:
        fake_client.admin.command.return_value = ping_result or {"ok": 1.0}
    fake_client.server_info.return_value = {"version": version}

    fake_module = mock.MagicMock()
    fake_module.__name__ = "pymongo"
    fake_module.version = "4.17.0-mock"
    fake_module.MongoClient.return_value = fake_client

    connector._driver_module = fake_module
    return fake_client


# ============================================================
# 22: mocked ping behavior
# ============================================================

def test_mongodb_test_connection_uses_mocked_ping():
    connector = MongoDBConnector(_fake_settings())
    _install_fake_pymongo_client(connector)

    result = connector.test_connection()

    assert result.success is True
    assert result.server_version == "7.0.0-mock"
    connector.close()


def test_mongodb_source_metadata_uses_mocked_client():
    connector = MongoDBConnector(_fake_settings())
    _install_fake_pymongo_client(connector)

    metadata = connector.get_source_metadata()

    assert metadata.server_vendor == "mongodb"
    assert metadata.connector_implementation == "MongoDBConnector"
    assert metadata.driver_name == "pymongo"
    assert metadata.capabilities.document_database is True
    assert metadata.capabilities.relational is False
    connector.close()


def test_mongodb_ping_command_is_the_only_admin_call():
    """Proves the connectivity check uses the documented safe `ping` command
    and nothing else administrative or destructive."""
    connector = MongoDBConnector(_fake_settings())
    fake_client = _install_fake_pymongo_client(connector)

    connector.test_connection()

    fake_client.admin.command.assert_called_once_with("ping")
    connector.close()


# ============================================================
# 24: authentication error normalized safely (mocked)
# ============================================================

def test_mongodb_authentication_failure_is_normalized_and_password_not_leaked():
    import pymongo.errors as mongo_errors

    connector = MongoDBConnector(_fake_settings(password="super-secret-value"))
    auth_error = mongo_errors.OperationFailure("Authentication failed.", code=18)
    _install_fake_pymongo_client(connector, ping_error=auth_error)

    with pytest.raises(ConnectorAuthenticationError) as excinfo:
        connector.test_connection()

    assert "super-secret-value" not in str(excinfo.value)
    connector.close()


# ============================================================
# 23: timeout normalized correctly (mocked)
# ============================================================

def test_mongodb_timeout_is_normalized():
    import pymongo.errors as mongo_errors

    connector = MongoDBConnector(_fake_settings())
    _install_fake_pymongo_client(
        connector, ping_error=mongo_errors.ServerSelectionTimeoutError("no servers")
    )

    with pytest.raises(ConnectorTimeoutError):
        connector.test_connection()

    connector.close()


# ============================================================
# 18: missing optional driver produces ConnectorDependencyError
# ============================================================

def test_mongodb_missing_driver_raises_dependency_error():
    connector = MongoDBConnector(_fake_settings())

    with mock.patch.dict(sys.modules, {"pymongo": None}):
        with pytest.raises(ConnectorDependencyError, match="pymongo"):
            connector.test_connection()

    connector.close()


def test_mongodb_connector_module_imports_without_pymongo_installed():
    with mock.patch.dict(sys.modules, {"pymongo": None}):
        sys.modules.pop("erp_pipeline.connectors.mongodb", None)
        import importlib

        module = importlib.import_module("erp_pipeline.connectors.mongodb")
        assert hasattr(module, "MongoDBConnector")


def test_mongodb_capabilities_available_even_without_driver():
    connector = MongoDBConnector(_fake_settings())
    with mock.patch.dict(sys.modules, {"pymongo": None}):
        capabilities = connector.get_capabilities()
    assert capabilities.document_database is True
    connector.close()


# ============================================================
# Scope: no sampling, no schema inference
# ============================================================

def test_mongodb_connector_does_not_expose_sampling_methods():
    forbidden = {"sample_documents", "list_collections", "infer_schema", "get_collections"}
    public_methods = {
        name for name in dir(MongoDBConnector) if not name.startswith("_")
    }
    assert not (public_methods & forbidden)
