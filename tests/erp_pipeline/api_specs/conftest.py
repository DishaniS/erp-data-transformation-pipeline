"""Fixtures for Phase 7 API specification tests.

Every specification fixture is a real committed file under
``tests/fixtures/api_specs/`` - JSON and YAML are text, so they stay
reviewable in a diff, which matters more here than anywhere else in the
project: the fixtures encode exactly which OpenAPI and Postman constructs the
parser claims to handle.

All content is synthetic. The ``SECRET_*`` sentinels exist so the privacy
tests can prove those values never reach a schema, a warning, a log or the
catalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: Synthetic secrets planted in the fixtures. Every one of these must be
#: absent from every serialized output.
SECRET_BEARER = "SECRET_BEARER_928311"
SECRET_API_KEY = "SECRET_API_KEY_88391"
SECRET_BASIC_PASSWORD = "SECRET_BASIC_PASSWORD_99281"
SECRET_COOKIE = "SECRET_COOKIE_44120"
SECRET_CUSTOMER = "SECRET_CUSTOMER_NAME_31007"
SECRET_IBAN = "SECRET_IBAN_55231"
SECRET_OPENAPI_EXAMPLE = "SECRET_EXAMPLE_VALUE_50021"
SECRET_QUERY_KEY = "SECRET_QUERY_KEY_71122"
SECRET_HTML_BODY = "SECRET_HTML_BODY_10011"

SECRETS: tuple[str, ...] = (
    SECRET_BEARER,
    SECRET_API_KEY,
    SECRET_BASIC_PASSWORD,
    SECRET_COOKIE,
    SECRET_CUSTOMER,
    SECRET_IBAN,
    SECRET_OPENAPI_EXAMPLE,
    SECRET_QUERY_KEY,
    SECRET_HTML_BODY,
)


@pytest.fixture(scope="session")
def spec_fixtures() -> Path:
    """Directory of committed specification fixture files."""
    directory = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "api_specs"

    if not directory.is_dir():  # pragma: no cover - repository layout guard
        pytest.fail(f"Specification fixture directory is missing: {directory}")

    return directory


@pytest.fixture(scope="session")
def pipeline_connector():
    """Connector to the pipeline database, for the catalog integration tests.

    A local twin of the fixture the discovery and ingestion suites define:
    pytest conftest files are directory-scoped, so neither is visible here.
    Skips - never fails, never fakes - when PostgreSQL is unreachable.
    """
    import os

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")

    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.connectors.postgresql import PostgreSQLConnector
    from erp_pipeline.schemas.enums import SourceType

    password = os.getenv("AI_DB_PASSWORD")
    if not password:
        pytest.skip("AI_DB_PASSWORD is not configured in .env")

    settings = ConnectionSettings(
        source_system_id="api_spec_probe",
        source_type=SourceType.POSTGRESQL,
        host=os.getenv("AI_DB_HOST", "localhost"),
        port=int(os.getenv("AI_DB_PORT", "5432")),
        database=os.getenv("AI_DB_NAME", "erp_ai_native_db"),
        username=os.getenv("AI_DB_USER", "postgres"),
        password=password,
        connect_timeout_seconds=10,
    )

    connector = PostgreSQLConnector(settings)
    try:
        connector.test_connection()
    except Exception as exc:  # noqa: BLE001 - availability probe
        connector.close()
        pytest.skip(f"Pipeline PostgreSQL unreachable: {exc}")

    yield connector
    connector.close()
