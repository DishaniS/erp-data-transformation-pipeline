"""Deployment hardening: configuration, composition, persistence, wiring.

The point of every test here is the difference between "the capability exists"
and "the shipped application uses it". Before this pass each durable store was
implemented and verified, and none of them was the default.
"""

from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import pytest

from erp_pipeline.api.config import ApiSettings
from erp_pipeline.runtime import (
    OWNED_SCHEMAS,
    ColdSettings,
    ConfigurationError,
    DatabaseSettings,
    QdrantSettings,
    RuntimeSettings,
    bootstrap_all,
    build_pipeline_engine,
    verify_all,
)
from erp_pipeline.runtime.persistence import bootstrap_runtime_persistence

SENTINEL_PASSWORD = "SECRET_DB_PASSWORD_13981"
SENTINEL_API_KEY = "SECRET_API_KEY_88221"
SENTINEL_QDRANT_KEY = "SECRET_QDRANT_KEY_55031"


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def test_a_complete_configuration_reports_no_problems(tmp_path: Path):
    settings = RuntimeSettings(
        api=ApiSettings(upload_dir=tmp_path / "uploads"),
        database=DatabaseSettings(database="db", user="u", password="p"),
        cold=ColdSettings(enabled=False),
    )

    assert settings.validate() == ()


def test_missing_database_configuration_is_reported_by_name(tmp_path: Path):
    settings = RuntimeSettings(
        api=ApiSettings(upload_dir=tmp_path),
        database=DatabaseSettings(database="db", user="u", password=None),
        cold=ColdSettings(enabled=False),
    )
    problems = " ".join(settings.validate())

    assert "PIPELINE_DB_PASSWORD" in problems
    # The variable NAME is safe to print; a value never is.
    assert SENTINEL_PASSWORD not in problems


def test_cold_enabled_without_a_key_is_refused(tmp_path: Path, monkeypatch):
    """Never silently fall back to writing archives unencrypted."""
    monkeypatch.delenv("ERP_COLD_ARCHIVE_KEY", raising=False)

    settings = RuntimeSettings(
        api=ApiSettings(upload_dir=tmp_path),
        database=DatabaseSettings(database="db", user="u", password="p"),
        cold=ColdSettings(enabled=True, directory=tmp_path / "cold"),
    )
    problems = " ".join(settings.validate())

    assert "ERP_COLD_ARCHIVE_KEY" in problems

    with pytest.raises(ConfigurationError):
        settings.require_valid()


def test_building_cold_without_a_key_raises_rather_than_degrading(
    tmp_path: Path, monkeypatch
):
    from erp_pipeline.runtime.services import build_storage_service

    monkeypatch.delenv("ERP_COLD_ARCHIVE_KEY", raising=False)

    settings = RuntimeSettings(
        api=ApiSettings(upload_dir=tmp_path),
        database=DatabaseSettings(database="db", user="u", password="p"),
        qdrant=QdrantSettings(enabled=False),
        cold=ColdSettings(enabled=True, directory=tmp_path / "cold"),
    )

    with pytest.raises(ConfigurationError):
        build_storage_service(settings, engine=None)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::"])
def test_a_routable_bind_without_an_api_key_is_refused(host: str, tmp_path: Path):
    """The single most dangerous misconfiguration this system can have."""
    settings = RuntimeSettings(
        api=ApiSettings(host=host, api_key=None, upload_dir=tmp_path),
        database=DatabaseSettings(database="db", user="u", password="p"),
        cold=ColdSettings(enabled=False),
    )
    problems = " ".join(settings.validate())

    assert "ERP_API_KEY" in problems

    with pytest.raises(ConfigurationError):
        settings.require_valid()


def test_a_routable_bind_with_an_api_key_is_allowed(tmp_path: Path):
    settings = RuntimeSettings(
        api=ApiSettings(host="0.0.0.0", api_key="a-real-key", upload_dir=tmp_path),
        database=DatabaseSettings(database="db", user="u", password="p"),
        cold=ColdSettings(enabled=False),
    )

    assert settings.validate() == ()


def test_loopback_without_a_key_stays_practical_for_development(tmp_path: Path):
    settings = RuntimeSettings(
        api=ApiSettings(host="127.0.0.1", api_key=None, upload_dir=tmp_path),
        database=DatabaseSettings(database="db", user="u", password="p"),
        cold=ColdSettings(enabled=False),
    )

    assert settings.validate() == ()


def test_the_insecure_override_exists_and_must_be_explicit(tmp_path: Path):
    settings = RuntimeSettings(
        api=ApiSettings(host="0.0.0.0", api_key=None, upload_dir=tmp_path),
        database=DatabaseSettings(database="db", user="u", password="p"),
        cold=ColdSettings(enabled=False),
        allow_insecure_bind=True,
    )

    assert settings.validate() == ()


# ----------------------------------------------------------------------
# Secret redaction
# ----------------------------------------------------------------------


def test_settings_never_render_a_secret(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ERP_COLD_ARCHIVE_KEY", "cold-key-material")

    settings = RuntimeSettings(
        api=ApiSettings(api_key=SENTINEL_API_KEY, upload_dir=tmp_path),
        database=DatabaseSettings(database="db", user="u", password=SENTINEL_PASSWORD),
        qdrant=QdrantSettings(api_key=SENTINEL_QDRANT_KEY),
        cold=ColdSettings(enabled=True, directory=tmp_path),
    )

    rendered = f"{settings.describe()} {settings.database!r} {settings.qdrant!r}"

    for planted in (SENTINEL_PASSWORD, SENTINEL_API_KEY, SENTINEL_QDRANT_KEY):
        assert planted not in rendered

    assert "[REDACTED]" in str(settings.describe())


def test_the_database_url_is_never_part_of_the_repr():
    settings = DatabaseSettings(database="db", user="u", password=SENTINEL_PASSWORD)

    assert SENTINEL_PASSWORD not in repr(settings)
    # It IS in the URL - which is why the URL is never logged.
    assert SENTINEL_PASSWORD in settings.url()


# ----------------------------------------------------------------------
# Qdrant configuration
# ----------------------------------------------------------------------


def test_qdrant_settings_prefer_a_url_when_one_is_given(monkeypatch):
    monkeypatch.setenv("ERP_QDRANT_URL", "https://cluster.example.test:6333")
    monkeypatch.setenv("ERP_QDRANT_HOST", "ignored-host")

    settings = QdrantSettings.from_environment()

    assert settings.uses_url
    assert settings.describe()["mode"] == "url"


def test_qdrant_settings_fall_back_to_host_and_port(monkeypatch):
    monkeypatch.delenv("ERP_QDRANT_URL", raising=False)
    monkeypatch.setenv("ERP_QDRANT_HOST", "vectors.internal")
    monkeypatch.setenv("ERP_QDRANT_PORT", "7333")

    settings = QdrantSettings.from_environment()

    assert not settings.uses_url
    assert settings.host == "vectors.internal"
    assert settings.port == 7333


def test_generic_code_does_not_read_the_bpi_vector_variables(monkeypatch):
    """VECTOR_DB_* belongs to the BPI prototype and points at its collection."""
    monkeypatch.setenv("VECTOR_DB_HOST", "bpi-host.example.test")
    monkeypatch.setenv("VECTOR_DB_URL", "https://bpi.example.test")
    monkeypatch.delenv("ERP_QDRANT_HOST", raising=False)
    monkeypatch.delenv("ERP_QDRANT_URL", raising=False)

    settings = QdrantSettings.from_environment()

    assert settings.host == "localhost"
    assert settings.url is None


def test_cloud_qdrant_never_falls_back_to_localhost(monkeypatch):
    """The defect this guards against.

    The deployment .env used unprefixed ``QDRANT_*`` names while the code reads
    ``ERP_QDRANT_*``. Nothing matched, so the settings fell through to their own
    ``localhost:6333`` defaults and vectors were written to a local instance
    that happened to be listening - silently, with no error anywhere.
    """
    monkeypatch.setenv("ERP_QDRANT_URL", "https://cluster.example.test")
    monkeypatch.delenv("ERP_QDRANT_API_KEY", raising=False)

    settings = QdrantSettings.from_environment()

    assert settings.deployment == "cloud"

    with pytest.raises(ConfigurationError) as failure:
        settings.validate()

    message = str(failure.value)

    assert "ERP_QDRANT_API_KEY" in message
    assert "localhost" in message, "the refusal must say what it did NOT do"


def test_an_api_key_without_a_url_is_also_refused(monkeypatch):
    monkeypatch.delenv("ERP_QDRANT_URL", raising=False)
    monkeypatch.setenv("ERP_QDRANT_API_KEY", "unused-in-this-test")

    with pytest.raises(ConfigurationError) as failure:
        QdrantSettings.from_environment().validate()

    assert "ERP_QDRANT_URL" in str(failure.value)


def test_a_fully_configured_cloud_cluster_validates(monkeypatch):
    monkeypatch.setenv("ERP_QDRANT_URL", "https://cluster.example.test")
    monkeypatch.setenv("ERP_QDRANT_API_KEY", "unused-in-this-test")

    settings = QdrantSettings.from_environment()
    settings.validate()

    assert settings.deployment == "cloud"
    assert settings.uses_url


def test_local_qdrant_requires_no_key_but_must_be_the_absence_of_cloud(monkeypatch):
    """Local stays available - it is simply never the silent consolation prize."""
    for name in ("ERP_QDRANT_URL", "ERP_QDRANT_API_KEY", "ERP_QDRANT_MODE"):
        monkeypatch.delenv(name, raising=False)

    settings = QdrantSettings.from_environment()
    settings.validate()

    assert settings.deployment == "local"
    assert settings.host == "localhost"


def test_local_mode_can_be_declared_explicitly(monkeypatch):
    monkeypatch.setenv("ERP_QDRANT_MODE", "local")
    monkeypatch.setenv("ERP_QDRANT_HOST", "vectors.internal")
    monkeypatch.delenv("ERP_QDRANT_API_KEY", raising=False)
    monkeypatch.delenv("ERP_QDRANT_URL", raising=False)

    settings = QdrantSettings.from_environment()
    settings.validate()

    assert settings.deployment == "local"
    assert settings.host == "vectors.internal"


def test_an_unrecognised_mode_is_refused(monkeypatch):
    monkeypatch.setenv("ERP_QDRANT_MODE", "cloudy")

    with pytest.raises(ConfigurationError) as failure:
        QdrantSettings.from_environment().validate()

    assert "ERP_QDRANT_MODE" in str(failure.value)


def test_a_disabled_vector_store_is_not_validated(monkeypatch):
    """Nothing connects, so an incomplete cluster config is not an error."""
    monkeypatch.setenv("ERP_QDRANT_MODE", "cloud")
    monkeypatch.setenv("ERP_QDRANT_ENABLED", "false")
    monkeypatch.delenv("ERP_QDRANT_URL", raising=False)

    QdrantSettings.from_environment().validate()


def test_the_api_key_never_appears_in_any_rendering(monkeypatch):
    secret = "QDRANT_KEY_SENTINEL_40771"
    monkeypatch.setenv("ERP_QDRANT_URL", "https://cluster.example.test")
    monkeypatch.setenv("ERP_QDRANT_API_KEY", secret)

    settings = QdrantSettings.from_environment()

    assert secret not in repr(settings)
    assert secret not in str(settings.describe())

    monkeypatch.delenv("ERP_QDRANT_API_KEY", raising=False)

    with pytest.raises(ConfigurationError) as failure:
        QdrantSettings.from_environment().validate()

    assert secret not in str(failure.value)


def test_the_collection_names_are_unchanged_by_the_cloud_switch(monkeypatch):
    """The architecture rule: cloud or local, the two collections are the same."""
    monkeypatch.setenv("ERP_QDRANT_URL", "https://cluster.example.test")
    monkeypatch.setenv("ERP_QDRANT_API_KEY", "unused-in-this-test")

    settings = QdrantSettings.from_environment()

    assert settings.hot_collection == "erp_vectors_hot"
    assert settings.warm_collection == "erp_vectors_warm"


def test_the_bpi_single_collection_variable_is_still_not_read(monkeypatch):
    """``QDRANT_COLLECTION`` names a prototype collection and must stay unread.

    It sits in the deployment .env beside the cluster URL, so wiring the URL up
    without this guard would be one careless alias away from pointing the hot
    tier at a dataset-specific collection.
    """
    monkeypatch.setenv("QDRANT_COLLECTION", "bpi2020_erp_knowledge")

    settings = QdrantSettings.from_environment()

    assert settings.hot_collection == "erp_vectors_hot"
    assert settings.warm_collection == "erp_vectors_warm"


def test_the_qdrant_collection_names_are_configurable(monkeypatch):
    monkeypatch.setenv("ERP_QDRANT_HOT_COLLECTION", "tenant_a_hot")
    monkeypatch.setenv("ERP_QDRANT_WARM_COLLECTION", "tenant_a_warm")

    settings = QdrantSettings.from_environment()

    assert settings.hot_collection == "tenant_a_hot"
    assert settings.warm_collection == "tenant_a_warm"


def test_the_cold_archive_directory_is_configurable(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ERP_COLD_ARCHIVE_DIR", str(tmp_path / "archives"))

    assert ColdSettings.from_environment().directory == tmp_path / "archives"


# ----------------------------------------------------------------------
# Import safety - Phase 13's guarantee must survive this pass
# ----------------------------------------------------------------------


def test_importing_the_runtime_loads_no_model_and_opens_no_connection():
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import erp_pipeline.runtime, erp_pipeline.runtime.application;"
        "print('sentence_transformers' in sys.modules,"
        "      'qdrant_client' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"


def test_the_asgi_target_exists_and_is_lazy():
    """`app` must be importable without a database being present."""
    from erp_pipeline.runtime.application import app

    assert callable(app)
    assert app._app is None, "the app was built at import time"


# ----------------------------------------------------------------------
# Incremental and drift wiring
# ----------------------------------------------------------------------


def test_run_incremental_no_longer_raises_unconditionally():
    """The audit's headline gap: it refused every source, always."""
    import inspect

    from erp_pipeline.orchestration.service import PipelineServices

    source = inspect.getsource(PipelineServices.run_incremental)

    assert "service.run_incremental(" in source
    assert "RelationalIncrementalExtractor" in source


def test_check_drift_calls_phase_10_rather_than_reading_a_cached_attribute():
    import inspect

    from erp_pipeline.orchestration.service import PipelineServices

    source = inspect.getsource(PipelineServices.check_drift)

    assert "service.check_drift(" in source
    assert "last_drift_report" not in source


def test_sources_without_a_cursor_are_refused_for_incremental():
    from erp_pipeline.orchestration.service import PipelineServices
    from erp_pipeline.schemas.enums import SourceType

    for unsupported in (
        SourceType.CSV,
        SourceType.PDF,
        SourceType.IMAGE,
        SourceType.OPENAPI,
        SourceType.POSTMAN,
    ):
        assert unsupported not in PipelineServices.INCREMENTAL_SOURCES

    for supported in (SourceType.POSTGRESQL, SourceType.MYSQL, SourceType.SQL_SERVER):
        assert supported in PipelineServices.INCREMENTAL_SOURCES


def test_the_watermark_field_is_validated_against_the_schema():
    """An unvalidated cursor is an identifier that reaches SQL."""
    from erp_pipeline.orchestration import InvalidPipelineRequestError, JobRequest, JobType
    from erp_pipeline.orchestration.service import PipelineServices
    from erp_pipeline.schemas.enums import FieldDataType
    from erp_pipeline.schemas.source_models import (
        SourceEntity,
        SourceField,
        SourceSchema,
    )

    entity = SourceEntity(
        entity_id="e1",
        source_name="invoices",
        normalized_name="invoices",
        fields=(
            SourceField(
                source_name="invoice_id",
                normalized_name="invoice_id",
                source_data_type="text",
                normalized_data_type=FieldDataType.STRING,
                nullable=False,
                is_primary_key=True,
            ),
            SourceField(
                source_name="updated_at",
                normalized_name="updated_at",
                source_data_type="timestamptz",
                normalized_data_type=FieldDataType.DATETIME,
                nullable=True,
            ),
        ),
        primary_key_fields=("invoice_id",),
    )
    from erp_pipeline.schemas.enums import SchemaOrigin

    schema = SourceSchema(
        schema_id="s1",
        source_system_id="src",
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(entity,),
    )
    services = PipelineServices()

    # A real column is accepted.
    config = services.build_extraction_config(
        JobRequest(
            job_type=JobType.INCREMENTAL_SYNC,
            source_id="src",
            options={"watermark_field": "updated_at"},
        ),
        schema,
    )
    assert config.watermark_field == "updated_at"
    # Equal watermarks are broken by the key, so paging cannot skip rows.
    assert config.tie_break_field == "invoice_id"

    # An invented one is refused.
    with pytest.raises(InvalidPipelineRequestError):
        services.build_extraction_config(
            JobRequest(
                job_type=JobType.INCREMENTAL_SYNC,
                source_id="src",
                options={"watermark_field": "; DROP TABLE invoices"},
            ),
            schema,
        )

    # And omitting it entirely is refused rather than guessed.
    with pytest.raises(InvalidPipelineRequestError):
        services.build_extraction_config(
            JobRequest(job_type=JobType.INCREMENTAL_SYNC, source_id="src"), schema
        )


def test_incremental_stages_report_phase_10_rather_than_repeating_it():
    """Re-running TRANSFORM after Phase 10 would write every record twice."""
    from erp_pipeline.orchestration.stages import (
        DEFAULT_HANDLERS,
        INCREMENTAL_HANDLERS,
        run_incremental_passthrough,
    )
    from erp_pipeline.orchestration.models import PipelineStage

    for stage in (
        PipelineStage.TRANSFORM,
        PipelineStage.VALIDATE,
        PipelineStage.LOAD,
        PipelineStage.AI_BUILD,
        PipelineStage.EMBED,
    ):
        assert INCREMENTAL_HANDLERS[stage] is run_incremental_passthrough
        assert DEFAULT_HANDLERS[stage] is not run_incremental_passthrough


# ----------------------------------------------------------------------
# gitignore
# ----------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]


def _ignored(relative: str) -> bool:
    import subprocess

    return (
        subprocess.run(
            ["git", "check-ignore", "-q", relative],
            cwd=REPO_ROOT,
            capture_output=True,
        ).returncode
        == 0
    )


@pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(), reason="not a git working tree"
)
def test_research_artifacts_and_fixtures_are_trackable():
    """A clone that cannot run the suite is not a reproducible artifact."""
    for asset in (
        "artifacts/phase12_storage_benchmark.json",
        "artifacts/phase13_openapi.json",
        "tests/fixtures/ingestion/normal.csv",
    ):
        if (REPO_ROOT / asset).exists():
            assert not _ignored(asset), f"{asset} is still gitignored"


@pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(), reason="not a git working tree"
)
def test_secrets_and_runtime_data_remain_ignored():
    for protected in (
        ".env",
        "data/raw/company_export.csv",
        "var/uploads/customer.csv",
        "var/cold-archive/a.erpcold",
    ):
        assert _ignored(protected), f"{protected} would be committed"


# ----------------------------------------------------------------------
# .env.example completeness
# ----------------------------------------------------------------------


def test_env_example_documents_every_runtime_variable():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    for variable in (
        "ERP_API_HOST",
        "ERP_API_PORT",
        "ERP_API_KEY",
        "ERP_API_PROTECT_READS",
        "ERP_API_CORS_ORIGINS",
        "ERP_API_MAX_UPLOAD_BYTES",
        "ERP_API_UPLOAD_DIR",
        "ERP_SQL_SERVER_LIVE_VERIFIED",
        "ERP_QDRANT_HOST",
        "ERP_QDRANT_PORT",
        "ERP_QDRANT_API_KEY",
        "ERP_QDRANT_HOT_COLLECTION",
        "ERP_QDRANT_WARM_COLLECTION",
        "ERP_COLD_ARCHIVE_DIR",
        "ERP_COLD_ARCHIVE_KEY",
        "ERP_EXECUTOR_WORKERS",
    ):
        assert variable in text, f"{variable} is undocumented"

    # Documented, but never with a value.
    for line in text.splitlines():
        if line.startswith(("ERP_API_KEY=", "ERP_COLD_ARCHIVE_KEY=", "ERP_QDRANT_API_KEY=")):
            assert line.split("=", 1)[1].strip() == "", (
                f"{line.split('=')[0]} must ship empty"
            )
