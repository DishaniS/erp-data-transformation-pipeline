"""BaseSourceConnector contract, ConnectorRegistry dispatch, and lifecycle."""

import inspect

import pytest

from erp_pipeline.connectors.base import BaseSourceConnector
from erp_pipeline.connectors.config import ConnectionSettings
from erp_pipeline.connectors.errors import ConnectorClosedError, ConnectorConfigurationError, ConnectorTypeMismatchError
from erp_pipeline.connectors.models import ConnectionTestResult, ConnectorCapabilities, SourceMetadata
from erp_pipeline.connectors.mongodb import MongoDBConnector
from erp_pipeline.connectors.mysql import MySQLConnector
from erp_pipeline.connectors.postgresql import PostgreSQLConnector
from erp_pipeline.connectors.registry import ConnectorRegistry
from erp_pipeline.connectors.sqlserver import SQLServerConnector
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.schemas.source_models import SourceSystem


def _settings(source_type, **overrides):
    defaults = dict(
        source_system_id="probe_sys",
        source_type=source_type,
        host="localhost",
        port={"postgresql": 5432, "mysql": 3306, "sql_server": 1433, "mongodb": 27017}[
            source_type.value if hasattr(source_type, "value") else source_type
        ],
        database="db",
        username="app_user",
        password="fake-password",
    )
    defaults.update(overrides)
    return ConnectionSettings(**defaults)


# ============================================================
# 12-15: registry dispatch
# ============================================================

def test_registry_dispatches_postgresql():
    connector = ConnectorRegistry.create(_settings(SourceType.POSTGRESQL))
    assert isinstance(connector, PostgreSQLConnector)
    connector.close()


def test_registry_dispatches_mysql():
    connector = ConnectorRegistry.create(_settings(SourceType.MYSQL))
    assert isinstance(connector, MySQLConnector)
    connector.close()


def test_registry_dispatches_sqlserver():
    connector = ConnectorRegistry.create(_settings(SourceType.SQL_SERVER))
    assert isinstance(connector, SQLServerConnector)
    connector.close()


def test_registry_dispatches_mongodb():
    connector = ConnectorRegistry.create(_settings(SourceType.MONGODB))
    assert isinstance(connector, MongoDBConnector)
    connector.close()


def test_registry_reports_all_four_registered_types():
    registered = {t.value for t in ConnectorRegistry.registered_source_types()}
    assert registered == {"postgresql", "mysql", "sql_server", "mongodb"}


def test_registry_rejects_unregistered_type_clearly():
    ConnectorRegistry.unregister(SourceType.CSV) if ConnectorRegistry.is_registered(SourceType.CSV) else None
    settings = ConnectionSettings(
        source_system_id="sys", source_type=SourceType.CSV, host="n/a", port=1, database="n/a"
    )
    with pytest.raises(ConnectorConfigurationError, match="No connector is registered"):
        ConnectorRegistry.create(settings)


def test_registry_does_not_silently_fall_back():
    """Registering nothing for a type must fail loudly, not default to some
    other connector."""

    class _Dummy:
        pass

    saved = dict(ConnectorRegistry._loaders)
    try:
        ConnectorRegistry._loaders.clear()
        with pytest.raises(ConnectorConfigurationError):
            ConnectorRegistry.create(_settings(SourceType.POSTGRESQL))
    finally:
        ConnectorRegistry._loaders.clear()
        ConnectorRegistry._loaders.update(saved)


# ============================================================
# 16: connector/source type mismatch (Step 4)
# ============================================================

def test_postgresql_connector_rejects_mongodb_settings():
    settings = _settings(SourceType.MONGODB)
    with pytest.raises(ConnectorTypeMismatchError, match="postgresql"):
        PostgreSQLConnector(settings)


def test_mysql_connector_rejects_postgresql_settings():
    settings = _settings(SourceType.POSTGRESQL)
    with pytest.raises(ConnectorTypeMismatchError):
        MySQLConnector(settings)


def test_sqlserver_connector_rejects_mysql_settings():
    settings = _settings(SourceType.MYSQL)
    with pytest.raises(ConnectorTypeMismatchError):
        SQLServerConnector(settings)


def test_mongodb_connector_rejects_sqlserver_settings():
    settings = _settings(SourceType.SQL_SERVER)
    with pytest.raises(ConnectorTypeMismatchError):
        MongoDBConnector(settings)


def test_base_constructor_rejects_non_connection_settings():
    with pytest.raises(ConnectorTypeMismatchError, match="ConnectionSettings"):
        PostgreSQLConnector({"source_type": "postgresql"})


def test_registry_create_with_incompatible_source_system_is_rejected():
    source_system = SourceSystem(
        source_system_id="probe_sys", name="Probe", source_type=SourceType.MONGODB
    )
    settings = _settings(SourceType.POSTGRESQL)
    with pytest.raises(ConnectorTypeMismatchError):
        ConnectorRegistry.create(settings, source_system=source_system)


def test_registry_create_with_compatible_source_system_succeeds():
    source_system = SourceSystem(
        source_system_id="probe_sys", name="Probe", source_type=SourceType.POSTGRESQL
    )
    settings = _settings(SourceType.POSTGRESQL)
    connector = ConnectorRegistry.create(settings, source_system=source_system)
    assert isinstance(connector, PostgreSQLConnector)
    connector.close()


# ============================================================
# Base contract surface
# ============================================================

def test_base_connector_is_abstract():
    with pytest.raises(TypeError):
        BaseSourceConnector(_settings(SourceType.POSTGRESQL))


@pytest.mark.parametrize(
    "connector_class, source_type",
    [
        (PostgreSQLConnector, SourceType.POSTGRESQL),
        (MySQLConnector, SourceType.MYSQL),
        (SQLServerConnector, SourceType.SQL_SERVER),
        (MongoDBConnector, SourceType.MONGODB),
    ],
)
def test_every_connector_exposes_the_required_public_methods(connector_class, source_type):
    connector = connector_class(_settings(source_type))
    try:
        assert hasattr(connector, "test_connection")
        assert hasattr(connector, "get_source_metadata")
        assert hasattr(connector, "get_capabilities")
        assert hasattr(connector, "close")
        assert hasattr(connector, "__enter__")
        assert hasattr(connector, "__exit__")

        capabilities = connector.get_capabilities()
        assert isinstance(capabilities, ConnectorCapabilities)
        assert capabilities.source_type is source_type
    finally:
        connector.close()


def test_get_capabilities_does_not_require_an_open_connection_and_survives_close():
    connector = PostgreSQLConnector(_settings(SourceType.POSTGRESQL))
    connector.close()
    # Must NOT raise ConnectorClosedError - capabilities are static.
    capabilities = connector.get_capabilities()
    assert isinstance(capabilities, ConnectorCapabilities)


# ============================================================
# 28: no arbitrary public write/execute interface
# ============================================================

FORBIDDEN_PUBLIC_METHOD_NAMES = {
    "execute_sql", "execute", "execute_query", "run_sql", "run_query", "query",
    "cursor", "insert", "update", "delete", "drop", "alter", "create_table",
}


@pytest.mark.parametrize(
    "connector_class",
    [PostgreSQLConnector, MySQLConnector, SQLServerConnector, MongoDBConnector],
)
def test_no_connector_exposes_an_arbitrary_execute_method(connector_class):
    public_methods = {
        name
        for name, _ in inspect.getmembers(connector_class, predicate=callable)
        if not name.startswith("_")
    }
    overlap = public_methods & FORBIDDEN_PUBLIC_METHOD_NAMES
    assert not overlap, f"{connector_class.__name__} exposes forbidden methods: {overlap}"


def test_base_connector_contract_itself_has_no_execute_method():
    public_methods = {
        name
        for name, _ in inspect.getmembers(BaseSourceConnector, predicate=callable)
        if not name.startswith("_")
    }
    assert not (public_methods & FORBIDDEN_PUBLIC_METHOD_NAMES)


# ============================================================
# 29: no Phase 4/5 schema discovery methods present
# ============================================================

FORBIDDEN_DISCOVERY_METHOD_NAMES = {
    "discover_schema", "list_tables", "list_collections", "get_columns",
    "get_primary_keys", "get_foreign_keys", "sample_documents", "infer_schema",
    "get_relationships", "profile_field",
}


@pytest.mark.parametrize(
    "connector_class",
    [PostgreSQLConnector, MySQLConnector, SQLServerConnector, MongoDBConnector],
)
def test_no_connector_implements_schema_discovery(connector_class):
    public_methods = {
        name
        for name, _ in inspect.getmembers(connector_class, predicate=callable)
        if not name.startswith("_")
    }
    overlap = public_methods & FORBIDDEN_DISCOVERY_METHOD_NAMES
    assert not overlap, f"{connector_class.__name__} exposes discovery methods: {overlap}"


# ============================================================
# 25-27: resource lifecycle
# ============================================================

def test_context_manager_closes_resources():
    settings = _settings(SourceType.POSTGRESQL)
    with ConnectorRegistry.create(settings) as connector:
        assert connector.is_closed is False
    assert connector.is_closed is True


def test_repeated_close_is_safe():
    connector = PostgreSQLConnector(_settings(SourceType.POSTGRESQL))
    connector.close()
    connector.close()
    connector.close()  # must not raise
    assert connector.is_closed is True


@pytest.mark.parametrize(
    "connector_class, source_type",
    [
        (PostgreSQLConnector, SourceType.POSTGRESQL),
        (MySQLConnector, SourceType.MYSQL),
        (SQLServerConnector, SourceType.SQL_SERVER),
        (MongoDBConnector, SourceType.MONGODB),
    ],
)
def test_operation_after_closure_raises_connector_closed_error(connector_class, source_type):
    connector = connector_class(_settings(source_type))
    connector.close()

    with pytest.raises(ConnectorClosedError):
        connector.test_connection()

    with pytest.raises(ConnectorClosedError):
        connector.get_source_metadata()


def test_entering_a_closed_connector_raises():
    connector = PostgreSQLConnector(_settings(SourceType.POSTGRESQL))
    connector.close()
    with pytest.raises(ConnectorClosedError):
        with connector:
            pass
