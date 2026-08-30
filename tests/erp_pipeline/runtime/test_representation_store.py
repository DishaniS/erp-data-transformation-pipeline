"""Phase 5 - the store that holds the AI text behind every vector.

The durability tests here run against a REAL on-disk database rather than a
dict. That is the whole point of the phase: an in-memory store would pass every
behavioural test and still lose the corpus on restart, which is the defect
being fixed rather than a reimplementation of it.

SQLite stands in for PostgreSQL, with ``erp_runtime`` provided by an attached
database file so the schema-qualified SQL is exercised unchanged. Where a live
PostgreSQL is configured, the same assertions run against it too.
"""

from __future__ import annotations

import json

import pytest

from erp_pipeline.orchestration.representation_store import (
    REPRESENTATIONS_TABLE,
    InMemoryRepresentationStore,
    PostgresRepresentationStore,
    create_representations_sql,
)
from erp_pipeline.sync.propagation import AIRepresentation

sa = pytest.importorskip("sqlalchemy")

#: SQLite's DBAPI deprecates its own datetime adapter on Python 3.12+. It fires
#: only because SQLite is standing in for PostgreSQL here - the production
#: driver binds timestamps natively - so it is silenced rather than left to
#: fill the suite's warning summary with noise about a database this project
#: does not deploy on.
pytestmark = pytest.mark.filterwarnings(
    "ignore:The default datetime adapter is deprecated:DeprecationWarning"
)


CERTIFICATE_TEXT = "BIRTH CERTIFICATE\nName: Nimal Silva\nDOB: 1997-03-20"


def representation(
    representation_id: str = "ai:document:emp002_cert.c0",
    text: str | None = CERTIFICATE_TEXT,
    **metadata,
) -> AIRepresentation:
    base = {
        "content_kind": "document_chunk",
        "parent_record_id": "erp:legacy_hr:employees:emp002",
        "source_system_id": "legacy_hr",
        "source_entity": "employees",
        "source_field": "birth_certificate",
        "business_key_name": "employee_id",
        "business_key_value": "EMP002",
        "document_type": "birth_certificate",
        "page_start": 1,
        "page_end": 1,
        "chunk_index": 0,
    }
    base.update(metadata)

    return AIRepresentation(
        representation_id=representation_id,
        entity_type="document",
        text_for_ai=text,
        content={"document_id": "doc-1", "chunk_index": 0},
        source_record_ids=("erp:legacy_hr:employees:emp002",),
        metadata=base,
    )


# ----------------------------------------------------------------------
# A real on-disk database, and a way to "restart" against it
# ----------------------------------------------------------------------


@pytest.fixture
def database(tmp_path):
    """An engine factory. Calling it again is a restart against the same files.

    ``erp_runtime`` is an ATTACHed database file, so the store's
    schema-qualified SQL runs exactly as written rather than being rewritten
    for the test.
    """
    runtime_file = tmp_path / "erp_runtime.sqlite"
    main_file = tmp_path / "main.sqlite"

    def connect():
        engine = sa.create_engine(f"sqlite:///{main_file}")

        @sa.event.listens_for(engine, "connect")
        def _attach(dbapi_connection, _record):  # noqa: ANN001
            dbapi_connection.execute(
                f"ATTACH DATABASE '{runtime_file}' AS erp_runtime"
            )

        return engine

    engine = connect()

    with engine.begin() as connection:
        connection.execute(sa.text(create_representations_sql()))

    return connect


@pytest.fixture
def store(database):
    return PostgresRepresentationStore(database())


# ======================================================================
# Round-trip
# ======================================================================


def test_the_stored_text_is_returned_exactly(store):
    """Retrievable text IS embedded text. Re-truncating here would break that."""
    store.upsert(representation())

    assert store.get("ai:document:emp002_cert.c0").text_for_ai == CERTIFICATE_TEXT


def test_every_field_of_the_model_survives_a_round_trip(store):
    original = representation()
    store.upsert(original)
    restored = store.get(original.representation_id)

    assert restored.representation_id == original.representation_id
    assert restored.entity_type == original.entity_type
    assert restored.text_for_ai == original.text_for_ai
    assert dict(restored.content) == dict(original.content)
    assert dict(restored.metadata) == dict(original.metadata)
    assert restored.source_record_ids == original.source_record_ids


def test_page_ordinals_survive_as_integers(store):
    store.upsert(representation())
    metadata = store.get("ai:document:emp002_cert.c0").metadata

    assert metadata["page_start"] == 1
    assert metadata["chunk_index"] == 0
    assert isinstance(metadata["page_start"], int)


def test_a_representation_with_no_text_is_still_stored(store):
    """An image OCR read nothing from still has an identity worth resolving.

    Dropping the row would make "never persisted" and "genuinely empty"
    indistinguishable, which is exactly the ambiguity this phase removes.
    """
    store.upsert(representation("ai:document:blank_photo.c0", text=None))
    restored = store.get("ai:document:blank_photo.c0")

    assert restored is not None
    assert restored.text_for_ai is None


def test_an_unknown_id_reads_back_as_none(store):
    assert store.get("ai:document:never-stored") is None


# ======================================================================
# TEST K - idempotent reprocessing
# ======================================================================


def test_reprocessing_the_same_representation_updates_one_row(store):
    original = representation()

    for _ in range(3):
        store.upsert(original)

    assert store.count() == 1
    assert store.get(original.representation_id).text_for_ai == CERTIFICATE_TEXT


def test_changed_content_replaces_the_stored_text(store):
    """The id is derived from identity, so a re-extraction updates in place."""
    store.upsert(representation())
    store.upsert(representation(text="BIRTH CERTIFICATE\nName: Nimal Silva\nAmended"))

    assert store.count() == 1
    assert "Amended" in store.get("ai:document:emp002_cert.c0").text_for_ai


def test_the_content_hash_tracks_the_text(store):
    """Integrity: the stored hash is the representation's own, over its text."""
    first = representation()
    second = representation(text="something else entirely")

    assert first.resolved_hash() != second.resolved_hash()

    store.upsert(first)
    assert store.get(first.representation_id).content_hash == first.resolved_hash()

    store.upsert(second)
    assert store.get(second.representation_id).content_hash == second.resolved_hash()


def test_a_restored_representation_still_verifies_its_own_hash(store):
    store.upsert(representation())
    restored = store.get("ai:document:emp002_cert.c0")

    assert restored.content_hash == restored.compute_hash()


# ======================================================================
# TEST E - same document, two employees
# ======================================================================


def test_two_employees_sharing_a_certificate_keep_separate_rows(store):
    for employee in ("EMP002", "EMP003"):
        store.upsert(
            representation(
                f"ai:document:{employee.lower()}_cert.c0",
                parent_record_id=f"erp:legacy_hr:employees:{employee.lower()}",
                business_key_value=employee,
            )
        )

    second = store.get("ai:document:emp002_cert.c0")
    third = store.get("ai:document:emp003_cert.c0")

    assert store.count() == 2
    # Identical text is correct - it IS the same certificate.
    assert second.text_for_ai == third.text_for_ai
    # The association must not be.
    assert second.metadata["business_key_value"] == "EMP002"
    assert third.metadata["business_key_value"] == "EMP003"
    assert second.metadata["parent_record_id"].endswith("emp002")
    assert third.metadata["parent_record_id"].endswith("emp003")


# ======================================================================
# Batch access
# ======================================================================


def test_many_representations_resolve_in_one_query(store):
    ids = [f"ai:document:chunk{index}" for index in range(5)]

    for index, identifier in enumerate(ids):
        store.upsert(representation(identifier, chunk_index=index))

    found = store.get_many(ids + ["ai:document:missing"])

    assert set(found) == set(ids)
    assert found[ids[3]].metadata["chunk_index"] == 3


def test_get_many_on_nothing_asks_the_database_nothing(store):
    assert store.get_many([]) == {}


def test_upsert_many_writes_the_batch(store):
    written = store.upsert_many(
        representation(f"ai:document:b{index}") for index in range(4)
    )

    assert written == 4
    assert store.count() == 4


def test_delete_removes_one_row(store):
    store.upsert(representation())

    assert store.delete("ai:document:emp002_cert.c0") is True
    assert store.get("ai:document:emp002_cert.c0") is None
    assert store.delete("ai:document:emp002_cert.c0") is False


# ======================================================================
# TEST L - the database restart
# ======================================================================


def test_representations_survive_a_restart(database):
    """The proof that this is persistence and not a cache.

    The first engine is disposed entirely; the second opens the same files with
    no shared process state.
    """
    first = database()
    PostgresRepresentationStore(first).upsert(representation())
    first.dispose()

    restored = PostgresRepresentationStore(database()).get(
        "ai:document:emp002_cert.c0"
    )

    assert restored is not None
    assert restored.text_for_ai == CERTIFICATE_TEXT
    assert restored.metadata["business_key_value"] == "EMP002"
    assert restored.metadata["parent_record_id"] == (
        "erp:legacy_hr:employees:emp002"
    )


def test_a_restart_does_not_duplicate_rows(database):
    first = database()
    PostgresRepresentationStore(first).upsert(representation())
    first.dispose()

    second = PostgresRepresentationStore(database())
    second.upsert(representation())

    assert second.count() == 1


# ======================================================================
# TEST I - what must never be stored
# ======================================================================


def test_no_raw_bytes_reach_the_stored_row(store, tmp_path):
    """Extracted text is the point; the bytes it came from are not."""
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    document.new_page().insert_text((72, 96), "BIRTH CERTIFICATE")
    payload = document.tobytes()
    document.close()

    import base64

    store.upsert(representation())
    engine = store._engine

    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(f"SELECT * FROM erp_runtime.{REPRESENTATIONS_TABLE}")
        ).mappings().all()

    surface = json.dumps([dict(row) for row in rows], default=str)

    assert base64.b64encode(payload).decode()[:24] not in surface
    assert "%PDF" not in surface
    assert "JVBERi0x" not in surface
    assert "iVBORw0KGgo" not in surface


def test_the_row_holds_no_column_for_bytes(store):
    """A schema that cannot hold a blob cannot leak one."""
    engine = store._engine
    inspector = sa.inspect(engine)
    columns = {
        column["name"]
        for column in inspector.get_columns(
            REPRESENTATIONS_TABLE, schema="erp_runtime"
        )
    }

    assert "blob" not in columns
    assert "raw_bytes" not in columns
    assert "payload" not in columns
    assert "text_for_ai" in columns


# ======================================================================
# The in-memory store matches the durable one
# ======================================================================


@pytest.fixture(params=["memory", "database"])
def either_store(request, database):
    if request.param == "memory":
        return InMemoryRepresentationStore()

    return PostgresRepresentationStore(database())


def test_both_stores_agree_on_upsert_and_get(either_store):
    either_store.upsert(representation())

    assert either_store.get("ai:document:emp002_cert.c0").text_for_ai == (
        CERTIFICATE_TEXT
    )


def test_both_stores_agree_that_upsert_replaces(either_store):
    either_store.upsert(representation())
    either_store.upsert(representation())

    assert either_store.count() == 1


def test_both_stores_agree_on_missing_ids(either_store):
    assert either_store.get("nope") is None
    assert either_store.get_many(["nope"]) == {}


def test_both_stores_agree_on_delete(either_store):
    either_store.upsert(representation())

    assert either_store.delete("ai:document:emp002_cert.c0") is True
    assert either_store.count() == 0


# ======================================================================
# Live PostgreSQL, when one is configured
# ======================================================================


@pytest.fixture
def live_store():
    pytest.importorskip("psycopg2")

    from erp_pipeline.runtime.database import DatabaseSettings

    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except Exception:  # pragma: no cover
        pass

    settings = DatabaseSettings.from_environment()

    if not settings.configured:
        pytest.skip("PIPELINE_DB_*/AI_DB_* settings are not configured")

    try:
        engine = sa.create_engine(settings.url())
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"live PostgreSQL unreachable: {type(error).__name__}")

    from erp_pipeline.orchestration.representation_store import (
        bootstrap_representation_schema,
    )

    bootstrap_representation_schema(engine)

    return PostgresRepresentationStore(engine)


def test_a_representation_round_trips_through_live_postgres(live_store):
    identifier = "ai:document:phase5_live_probe.c0"
    live_store.upsert(representation(identifier))

    try:
        restored = live_store.get(identifier)

        assert restored.text_for_ai == CERTIFICATE_TEXT
        assert restored.metadata["business_key_value"] == "EMP002"
        assert restored.content_hash == restored.compute_hash()
    finally:
        live_store.delete(identifier)
