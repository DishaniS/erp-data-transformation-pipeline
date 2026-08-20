"""ConnectionSettings: validation, security (redaction), and compatibility."""

import pytest

from erp_pipeline.connectors.config import ConnectionSettings
from erp_pipeline.connectors.errors import ConnectorConfigurationError, ConnectorTypeMismatchError
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.schemas.source_models import SourceSystem

FAKE_PASSWORD = "N0t@RealPassword!123"


# ============================================================
# 1-4: valid settings for every supported technology
# ============================================================

def test_valid_postgresql_settings():
    settings = ConnectionSettings(
        source_system_id="finance_erp_pg",
        source_type=SourceType.POSTGRESQL,
        host="localhost",
        port=5432,
        database="finance_erp",
        username="app_user",
        password=FAKE_PASSWORD,
    )
    assert settings.source_type is SourceType.POSTGRESQL
    assert settings.port == 5432


def test_valid_mysql_settings():
    settings = ConnectionSettings(
        source_system_id="ops_erp_mysql",
        source_type=SourceType.MYSQL,
        host="localhost",
        port=3306,
        database="ops_erp",
        username="app_user",
        password=FAKE_PASSWORD,
        driver_options={"charset": "utf8mb4"},
    )
    assert settings.source_type is SourceType.MYSQL
    assert settings.driver_options["charset"] == "utf8mb4"


def test_valid_sqlserver_settings():
    settings = ConnectionSettings(
        source_system_id="corp_erp_mssql",
        source_type=SourceType.SQL_SERVER,
        host="localhost",
        port=1433,
        database="corp_erp",
        username="app_user",
        password=FAKE_PASSWORD,
        driver_options={"driver": "ODBC Driver 18 for SQL Server"},
    )
    assert settings.source_type is SourceType.SQL_SERVER
    assert settings.driver_options["driver"] == "ODBC Driver 18 for SQL Server"


def test_valid_mongodb_settings():
    settings = ConnectionSettings(
        source_system_id="billing_erp_mongo",
        source_type=SourceType.MONGODB,
        host="localhost",
        port=27017,
        database="billing",
        username="app_user",
        password=FAKE_PASSWORD,
        auth_database="admin",
        ssl_enabled=True,
    )
    assert settings.source_type is SourceType.MONGODB
    assert settings.auth_database == "admin"
    assert settings.ssl_enabled is True


# ============================================================
# 5-7: structural rejection
# ============================================================

def test_blank_source_system_id_rejected():
    with pytest.raises(ConnectorConfigurationError, match="blank"):
        ConnectionSettings(
            source_system_id="   ",
            source_type=SourceType.POSTGRESQL,
            host="localhost",
            port=5432,
            database="db",
        )


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 999999])
def test_invalid_port_rejected(bad_port):
    with pytest.raises(ConnectorConfigurationError, match="port"):
        ConnectionSettings(
            source_system_id="sys",
            source_type=SourceType.POSTGRESQL,
            host="localhost",
            port=bad_port,
            database="db",
        )


@pytest.mark.parametrize("bad_timeout", [0, -5, 999999])
def test_invalid_timeout_rejected(bad_timeout):
    with pytest.raises(ConnectorConfigurationError, match="timeout"):
        ConnectionSettings(
            source_system_id="sys",
            source_type=SourceType.POSTGRESQL,
            host="localhost",
            port=5432,
            database="db",
            connect_timeout_seconds=bad_timeout,
        )


def test_unknown_source_type_rejected():
    with pytest.raises(ValueError, match="not a valid SourceType"):
        ConnectionSettings(
            source_system_id="sys",
            source_type="oracle",
            host="localhost",
            port=1521,
            database="db",
        )


def test_non_integer_port_rejected():
    with pytest.raises(ConnectorConfigurationError, match="integer"):
        ConnectionSettings(
            source_system_id="sys",
            source_type=SourceType.POSTGRESQL,
            host="localhost",
            port="5432",
            database="db",
        )


# ============================================================
# 8-10: password never leaks through repr/str/sanitized
# ============================================================

def _all_settings():
    return [
        ConnectionSettings(
            source_system_id="finance_erp_pg",
            source_type=SourceType.POSTGRESQL,
            host="localhost", port=5432, database="db",
            username="app_user", password=FAKE_PASSWORD,
        ),
        ConnectionSettings(
            source_system_id="ops_erp_mysql",
            source_type=SourceType.MYSQL,
            host="localhost", port=3306, database="db",
            username="app_user", password=FAKE_PASSWORD,
        ),
        ConnectionSettings(
            source_system_id="corp_erp_mssql",
            source_type=SourceType.SQL_SERVER,
            host="localhost", port=1433, database="db",
            username="app_user", password=FAKE_PASSWORD,
        ),
        ConnectionSettings(
            source_system_id="billing_erp_mongo",
            source_type=SourceType.MONGODB,
            host="localhost", port=27017, database="db",
            username="app_user", password=FAKE_PASSWORD,
        ),
    ]


@pytest.mark.parametrize("settings", _all_settings())
def test_password_absent_from_repr(settings):
    assert FAKE_PASSWORD not in repr(settings)


@pytest.mark.parametrize("settings", _all_settings())
def test_password_absent_from_str(settings):
    assert FAKE_PASSWORD not in str(settings)


@pytest.mark.parametrize("settings", _all_settings())
def test_sanitized_hides_password(settings):
    safe = settings.sanitized()
    assert "password" not in safe
    assert safe["password_set"] is True
    assert FAKE_PASSWORD not in str(safe)


def test_sanitized_reports_password_unset_when_none():
    settings = ConnectionSettings(
        source_system_id="sys",
        source_type=SourceType.POSTGRESQL,
        host="localhost", port=5432, database="db",
    )
    assert settings.sanitized()["password_set"] is False


def test_password_field_excluded_from_dataclass_fields_repr_mechanism():
    """Structural proof: the dataclass field itself is repr=False, not just
    that this particular value happens not to collide with output text."""
    import dataclasses

    password_field = next(
        f for f in dataclasses.fields(ConnectionSettings) if f.name == "password"
    )
    assert password_field.repr is False


# ============================================================
# Secret-shaped keys rejected in driver_options / metadata
# ============================================================

def test_driver_options_rejects_credential_shaped_key():
    with pytest.raises(ConnectorConfigurationError, match="credentials"):
        ConnectionSettings(
            source_system_id="sys",
            source_type=SourceType.POSTGRESQL,
            host="localhost", port=5432, database="db",
            driver_options={"api_key": "shhh"},
        )


def test_metadata_rejects_credential_shaped_key():
    with pytest.raises(ConnectorConfigurationError, match="credentials"):
        ConnectionSettings(
            source_system_id="sys",
            source_type=SourceType.POSTGRESQL,
            host="localhost", port=5432, database="db",
            metadata={"connection_string": "postgresql://u:p@h/d"},
        )


# ============================================================
# 17: SourceSystem / ConnectionSettings compatibility (Step 19)
# ============================================================

def test_settings_compatible_with_matching_source_system():
    source_system = SourceSystem(
        source_system_id="finance_erp",
        name="Finance ERP",
        source_type=SourceType.POSTGRESQL,
    )
    settings = ConnectionSettings(
        source_system_id="finance_erp",
        source_type=SourceType.POSTGRESQL,
        host="localhost", port=5432, database="db",
    )
    assert settings.matches_source_system(source_system) is True
    settings.require_compatible_source_system(source_system)  # must not raise


def test_settings_incompatible_source_type_rejected():
    source_system = SourceSystem(
        source_system_id="finance_erp",
        name="Finance ERP",
        source_type=SourceType.POSTGRESQL,
    )
    settings = ConnectionSettings(
        source_system_id="finance_erp",
        source_type=SourceType.MONGODB,
        host="localhost", port=27017, database="db",
    )
    assert settings.matches_source_system(source_system) is False
    with pytest.raises(ConnectorTypeMismatchError, match="source_type"):
        settings.require_compatible_source_system(source_system)


def test_settings_incompatible_source_system_id_rejected():
    source_system = SourceSystem(
        source_system_id="finance_erp",
        name="Finance ERP",
        source_type=SourceType.POSTGRESQL,
    )
    settings = ConnectionSettings(
        source_system_id="other_erp",
        source_type=SourceType.POSTGRESQL,
        host="localhost", port=5432, database="db",
    )
    with pytest.raises(ConnectorTypeMismatchError, match="source_system_id"):
        settings.require_compatible_source_system(source_system)


def test_require_compatible_source_system_rejects_non_source_system():
    settings = ConnectionSettings(
        source_system_id="finance_erp",
        source_type=SourceType.POSTGRESQL,
        host="localhost", port=5432, database="db",
    )
    with pytest.raises(ConnectorTypeMismatchError, match="SourceSystem"):
        settings.require_compatible_source_system({"not": "a SourceSystem"})


# ============================================================
# Task 32: no credential fields were added to SourceSystem
# ============================================================

def test_source_system_gained_no_credential_fields():
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(SourceSystem)}
    forbidden = {
        "password", "secret", "token", "api_key", "connection_string",
        "connection_url", "credential", "private_key",
    }
    assert not (field_names & forbidden)
