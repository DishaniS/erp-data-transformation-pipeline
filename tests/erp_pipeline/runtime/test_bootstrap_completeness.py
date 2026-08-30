"""``erp-bootstrap`` must create EVERY object the application owns.

THE DEFECT THIS PINS
--------------------
``bootstrap_all`` created ``erp_runtime.canonical_records`` but not the other
three tables in the same schema - ``registered_sources``, ``uploads`` and
``mapping_drafts`` - because those were created only by API startup. So this
sequence failed, and nothing in the bootstrap output hinted at why:

    erp-bootstrap
        -> start the API with ERP_BOOTSTRAP_ON_STARTUP=false
        -> the first source registration fails on a missing table

The SQL-recording tests below run everywhere and pin the DDL. The live tests
prove the same thing against a real PostgreSQL and skip cleanly when one is
not configured.
"""

from __future__ import annotations

import os
import uuid

import pytest

from erp_pipeline.runtime.bootstrap import bootstrap_all, verify_all
from erp_pipeline.runtime.database import OWNED_SCHEMAS

#: Every table ``erp-bootstrap`` must produce, by schema.
REQUIRED_TABLES: dict[str, tuple[str, ...]] = {
    "erp_catalog": (
        "source_systems",
        "schema_snapshots",
        "source_entities",
        "source_fields",
        "source_relationships",
        "mapping_profiles",
        "field_mappings",
    ),
    "erp_sync": ("sync_state",),
    "erp_vector_storage": (
        "vector_storage_state",
        "vector_tier_transitions",
        "vector_access_stats",
    ),
    "erp_orchestration": ("jobs", "job_stages"),
    "erp_runtime": (
        "canonical_records",
        "registered_sources",
        "uploads",
        "mapping_drafts",
        # Phase 5: the AI text a search hit resolves to. Without this table a
        # vector is searchable and its content is not retrievable.
        "ai_representations",
        # Phase 9: which version of a slot is current, and scheduler leadership.
        "representation_lifecycle",
        "scheduler_lease",
    ),
}


# ======================================================================
# SQL-recording: runs everywhere, no database needed
# ======================================================================


class RecordingConnection:
    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def execute(self, statement, *args, **kwargs):
        self._statements.append(str(statement))
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingEngine:
    """Captures the DDL bootstrap emits, without executing any of it."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def begin(self):
        return RecordingConnection(self.statements)

    def connect(self):
        return RecordingConnection(self.statements)

    @property
    def sql(self) -> str:
        return "\n".join(self.statements).lower()


@pytest.fixture
def recorded(monkeypatch) -> RecordingEngine:
    """Run ``bootstrap_all`` against a recording engine.

    ``existing_schemas`` is stubbed because it issues a real query; the DDL is
    what this test is about.
    """
    engine = RecordingEngine()

    monkeypatch.setattr(
        "erp_pipeline.runtime.bootstrap.existing_schemas", lambda _e: ()
    )
    # Phase 2's helper builds SQLAlchemy metadata rather than emitting text,
    # so it is recorded separately below.
    monkeypatch.setattr(
        "erp_pipeline.catalog.bootstrap_catalog",
        lambda e: engine.statements.append("-- catalog metadata create_all"),
    )

    bootstrap_all(engine)

    return engine


@pytest.mark.parametrize("table", REQUIRED_TABLES["erp_runtime"])
def test_bootstrap_creates_every_runtime_table(recorded, table):
    """Every table in erp_runtime.

    Driven from ``REQUIRED_TABLES`` rather than a second hand-written list, so
    a table added there cannot be forgotten here - which is how three of these
    came to be missing in the first place.
    """
    assert table in recorded.sql, table


def test_bootstrap_creates_the_runtime_schema(recorded):
    assert "create schema if not exists erp_runtime" in recorded.sql


@pytest.mark.parametrize("schema", ["erp_sync", "erp_vector_storage", "erp_orchestration"])
def test_bootstrap_creates_the_other_owned_schemas(recorded, schema):
    assert f"create schema if not exists {schema}" in recorded.sql


@pytest.mark.parametrize(
    "table",
    ["sync_state", "vector_storage_state", "vector_tier_transitions",
     "vector_access_stats", "jobs", "job_stages"],
)
def test_bootstrap_creates_the_other_owned_tables(recorded, table):
    assert table in recorded.sql, table


def test_every_ddl_statement_is_conditional(recorded):
    """Idempotency by construction: nothing bootstrap emits can destroy data."""
    for statement in recorded.statements:
        lowered = statement.lower().strip()

        if lowered.startswith("--"):
            continue

        assert "if not exists" in lowered or lowered.startswith("select"), statement


def test_bootstrap_never_drops_or_truncates(recorded):
    """The guarantee that makes repeated runs safe on a research database."""
    for forbidden in ("drop table", "drop schema", "truncate", "delete from"):
        assert forbidden not in recorded.sql, forbidden


def test_the_added_state_columns_are_applied_additively(recorded):
    """The canonical_record_id migration must extend an existing table, never
    rebuild it."""
    assert "add column if not exists canonical_record_id" in recorded.sql
    assert "add column if not exists source_system_id" in recorded.sql


def test_bootstrap_reports_success_for_every_owned_schema(recorded):
    result = bootstrap_all(recorded)

    assert {item.schema for item in result.results} == set(OWNED_SCHEMAS)
    assert result.ok


def test_the_runtime_step_names_all_of_what_it_creates(recorded):
    """An operator reading the output should see that the step covers more
    than canonical records."""
    result = bootstrap_all(recorded)
    runtime = next(item for item in result.results if item.schema == "erp_runtime")

    assert "sources" in runtime.owner
    assert "uploads" in runtime.owner


# ======================================================================
# Live: a real PostgreSQL, skipped when unavailable
# ======================================================================


@pytest.fixture(scope="module")
def engine():
    """A live engine, or a skip naming why one was unavailable."""
    pytest.importorskip("sqlalchemy", reason="sqlalchemy is not installed")
    pytest.importorskip("psycopg2", reason="psycopg2 is not installed")

    import sqlalchemy as sa

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # pragma: no cover - dotenv is optional
        pass

    host = os.getenv("PIPELINE_DB_HOST") or os.getenv("AI_DB_HOST", "localhost")
    port = os.getenv("PIPELINE_DB_PORT") or os.getenv("AI_DB_PORT", "5432")
    name = os.getenv("PIPELINE_DB_NAME") or os.getenv("AI_DB_NAME")
    user = os.getenv("PIPELINE_DB_USER") or os.getenv("AI_DB_USER")
    password = os.getenv("PIPELINE_DB_PASSWORD") or os.getenv("AI_DB_PASSWORD")

    if not (name and user):
        pytest.skip("pipeline database connection settings are not configured")

    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"

    try:
        candidate = sa.create_engine(url)
        with candidate.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as error:  # pragma: no cover - environment dependent
        pytest.skip(f"live PostgreSQL unreachable at {host}:{port}: {error!r}")

    return candidate


@pytest.fixture(scope="module")
def bootstrapped(engine):
    """Run the real ``erp-bootstrap`` path once for this module.

    Safe to run against the configured research database: every statement is
    create-if-missing or add-column-if-not-exists, and a companion test proves
    nothing dropping or truncating is ever emitted. Nothing is torn down
    afterwards - dropping these schemas would destroy research data.
    """
    result = bootstrap_all(engine)

    assert result.ok, result.render()

    return result


def existing_tables(engine, schema: str) -> set[str]:
    import sqlalchemy as sa

    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema"
            ),
            {"schema": schema},
        ).all()

    return {row[0] for row in rows}


@pytest.mark.parametrize("schema", sorted(REQUIRED_TABLES))
def test_live_bootstrap_creates_every_required_table(bootstrapped, engine, schema):
    present = existing_tables(engine, schema)
    missing = set(REQUIRED_TABLES[schema]) - present

    assert not missing, f"{schema} is missing {sorted(missing)}"


def test_live_bootstrap_leaves_no_owned_schema_missing(bootstrapped, engine):
    assert verify_all(engine) == ()


def test_live_bootstrap_is_idempotent(bootstrapped, engine):
    """Three consecutive runs, as an operator might do."""
    for _ in range(2):
        result = bootstrap_all(engine)

        assert result.ok, result.render()

    assert verify_all(engine) == ()


def test_live_state_table_has_the_added_columns(bootstrapped, engine):
    """The additive migration reached an already-existing table."""
    import sqlalchemy as sa

    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'erp_vector_storage' "
                "AND table_name = 'vector_storage_state'"
            )
        ).all()

    columns = {row[0] for row in rows}

    for added in ("canonical_record_id", "source_system_id", "source_entity",
                  "document_id"):
        assert added in columns, added


# ----------------------------------------------------------------------
# Every runtime store must be usable against a bootstrapped database
# ----------------------------------------------------------------------


def test_the_source_registry_is_usable(bootstrapped, engine):
    """The operation that previously failed after a standalone bootstrap."""
    from erp_pipeline.orchestration.sources import RegisteredSource
    from erp_pipeline.runtime.persistence import PostgresSourceRegistry
    from erp_pipeline.schemas.enums import SourceType

    registry = PostgresSourceRegistry(engine)
    source_id = f"bootstrap_probe_{uuid.uuid4().hex[:10]}"

    registered = registry.register(
        RegisteredSource(
            source_id=source_id,
            name=source_id,
            source_type=SourceType.POSTGRESQL,
            host="localhost",
            port=5432,
            database="probe",
            username="probe",
            credential_ref="probe_ref",
        )
    )

    assert registry.get(registered.source_id).source_id == source_id


def test_the_upload_store_is_usable(bootstrapped, engine, tmp_path):
    import io

    from erp_pipeline.runtime.persistence import PostgresUploadStore

    store = PostgresUploadStore(tmp_path / "uploads", engine)
    stored = store.store_stream(io.BytesIO(b"col_a,col_b\n1,2\n"), "probe.csv")

    assert store.get(stored.upload_id).upload_id == stored.upload_id


def test_the_mapping_draft_store_is_usable(bootstrapped, engine):
    from erp_pipeline.runtime.persistence import PostgresMappingDraftStore

    store = PostgresMappingDraftStore(engine)
    draft_id = f"bootstrap_probe_{uuid.uuid4().hex[:10]}"

    store.save(
        draft_id,
        {
            "schema_id": "bootstrap_probe_schema",
            "source_entity": "probe",
            "status": "awaiting_review",
            "ambiguous_fields": 2,
        },
    )

    try:
        loaded = store.get(draft_id)

        assert loaded is not None
        assert loaded["schema_id"] == "bootstrap_probe_schema"
        assert loaded["ambiguous_fields"] == 2
        assert draft_id in store
    finally:
        store.pop(draft_id, None)


def test_the_canonical_record_store_is_usable(bootstrapped, engine):
    from erp_pipeline.orchestration.record_store import PostgresCanonicalRecordStore
    from erp_pipeline.schemas.canonical_models import CanonicalRecord, SourceReference
    from erp_pipeline.schemas.enums import SourceType

    store = PostgresCanonicalRecordStore(engine)
    key = f"probe-{uuid.uuid4().hex[:10]}"

    record = CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id="bootstrap_probe",
            source_type=SourceType.POSTGRESQL,
            source_entity="probe",
            source_record_key=key,
        ),
        entity_type="invoice",
        stable_source_key=key,
        normalized_data={"invoice_id": key},
    )

    store.upsert(record)

    try:
        assert store.get(record.record_id) is not None
    finally:
        store.delete(record.record_id)


def test_the_tier_state_store_is_usable(bootstrapped, engine):
    """Including the columns the canonical-resolution fix added."""
    from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier
    from erp_pipeline.storage.state import PostgresTierStateStore

    store = PostgresTierStateStore(engine)
    representation_id = f"ai:probe:{uuid.uuid4().hex[:10]}"

    store.save(
        StorageRecordMetadata(
            representation_id=representation_id,
            embedding_id="emb.probe",
            vector_id="vec-probe",
            current_tier=StorageTier.HOT,
            content_hash="h",
            model_id="m",
            dimension=384,
            canonical_record_id="erp:bootstrap_probe:invoice:probe",
        )
    )

    try:
        loaded = store.load(representation_id)

        assert loaded is not None
        assert loaded.canonical_record_id == "erp:bootstrap_probe:invoice:probe"
    finally:
        store.delete(representation_id)


def test_a_live_re_store_without_a_reference_does_not_erase_one(bootstrapped, engine):
    """The ``COALESCE`` branch of the upsert, against real PostgreSQL.

    A later write that happens to carry no canonical reference must not blank
    one an earlier write already established - that would silently orphan a
    stored vector from its record.
    """
    from dataclasses import replace

    from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier
    from erp_pipeline.storage.state import PostgresTierStateStore

    store = PostgresTierStateStore(engine)
    representation_id = f"ai:probe:{uuid.uuid4().hex[:10]}"

    first = StorageRecordMetadata(
        representation_id=representation_id,
        embedding_id="emb.probe",
        vector_id="vec-probe",
        current_tier=StorageTier.HOT,
        content_hash="h",
        model_id="m",
        dimension=384,
        canonical_record_id="erp:bootstrap_probe:invoice:probe",
        source_system_id="bootstrap_probe",
    )
    store.save(first)

    try:
        store.save(
            replace(
                first,
                canonical_record_id=None,
                source_system_id=None,
                version=first.version + 1,
            )
        )

        loaded = store.load(representation_id)

        assert loaded.canonical_record_id == "erp:bootstrap_probe:invoice:probe"
        assert loaded.source_system_id == "bootstrap_probe"
    finally:
        store.delete(representation_id)


def test_the_api_can_start_against_a_bootstrapped_database(bootstrapped, engine):
    """The scenario the defect broke: bootstrap first, then start with
    ERP_BOOTSTRAP_ON_STARTUP disabled."""
    from fastapi.testclient import TestClient

    from erp_pipeline.api import ApiSettings, create_app
    from erp_pipeline.orchestration import (
        InlineJobExecutor,
        OrchestrationService,
        PipelineServices,
        PostgresJobStore,
    )
    from erp_pipeline.runtime.persistence import PostgresSourceRegistry

    orchestration = OrchestrationService(
        services=PipelineServices(sources=PostgresSourceRegistry(engine)),
        job_store=PostgresJobStore(engine),
        executor=InlineJobExecutor(),
    )
    app = create_app(settings=ApiSettings(), orchestration=orchestration)

    with TestClient(app) as client:
        assert client.get("/v1/health/live").status_code == 200
        assert client.get("/v1/sources").status_code == 200
