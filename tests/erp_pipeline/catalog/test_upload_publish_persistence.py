"""Regression tests for catalog persistence of uploaded-file schemas.

THE DEFECT THESE TESTS LOCK DOWN
--------------------------------
``erp_catalog.schema_snapshots.source_system_id`` carries a foreign key into
``erp_catalog.source_systems``. A CSV uploaded through ``POST /v1/files/csv``
is attributed to the logical system named by
``IngestionOptions.source_system_id`` - ``file_source`` by default - and
nothing ever created a ``source_systems`` row for it.

So every upload raised ``SourceSystemNotFoundError`` inside
``publish_schema``. The API route caught bare ``Exception``, set
``published = False`` and returned 201, so the upload looked successful while
the catalog stayed permanently empty. Observed in a live research database:
37 uploads and 22 jobs recorded, and 0 rows in ``schema_snapshots``.

The fix registers the source system first (registration is idempotent) and
reports a failure instead of discarding it.

``test_publishing_without_registering_the_source_system_is_rejected`` is the
canary: it asserts the underlying constraint still bites. If someone drops the
foreign key, that test fails and tells them the guarantee changed - rather than
these tests quietly passing for the wrong reason.

Every test here uses an isolated ``pytest_catalog``-prefixed source system and
cleans up its own rows, per this package's conventions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from erp_pipeline.catalog.repository import CatalogRepository
from erp_pipeline.catalog.service import SchemaCatalogService
from erp_pipeline.ingestion import FileIngestionService
from erp_pipeline.ingestion.models import IngestionOptions
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.schemas.source_models import SourceSystem

FIXTURE_CSV = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "ingestion" / "normal.csv"
)


def _ingestion_service(source_system_id: str) -> FileIngestionService:
    """A file ingestion service scoped to an isolated test source system."""
    return FileIngestionService(
        IngestionOptions(source_system_id=source_system_id)
    )


@pytest.fixture()
def csv_path() -> Path:
    if not FIXTURE_CSV.exists():  # pragma: no cover - fixture ships with the repo
        pytest.skip(f"CSV fixture missing: {FIXTURE_CSV}")

    return FIXTURE_CSV


# ============================================================
# The defect itself
# ============================================================


def test_publishing_without_registering_the_source_system_is_rejected(
    service, unique_id, csv_path, cleanup
):
    """The FK is real: publishing an unregistered source system must fail.

    This is the exact failure that used to be swallowed by the upload route.
    """
    source_system_id = cleanup.source_system_id(f"pytest_catalog_unreg_{unique_id}")
    result = _ingestion_service(source_system_id).ingest(csv_path)
    cleanup.schema_id(result.schema.schema_id)

    assert result.schema.source_system_id == source_system_id

    with pytest.raises(Exception) as excinfo:
        service.publish_schema(result.schema)

    # Not merely "some error": the message must name the unregistered system,
    # which is what makes the failure actionable for an operator.
    assert source_system_id in str(excinfo.value)


def test_registering_then_publishing_persists_the_schema(
    service, unique_id, csv_path, cleanup
):
    """Register-then-publish is the fixed path and must succeed."""
    source_system_id = cleanup.source_system_id(f"pytest_catalog_pub_{unique_id}")
    ingestion = _ingestion_service(source_system_id)
    result = ingestion.ingest(csv_path)
    cleanup.schema_id(result.schema.schema_id)

    service.register_source_system(ingestion.source_system())
    service.publish_schema(result.schema)

    retrieved = service.get_snapshot(result.schema.schema_id)

    assert retrieved.schema_id == result.schema.schema_id
    assert retrieved.source_system_id == source_system_id


def test_registration_is_idempotent_across_repeated_uploads(
    service, unique_id, csv_path, cleanup
):
    """Uploading twice must not fail on the second registration.

    The fix calls ``register_source_system`` on every upload, so this has to
    hold or the fix would break the second upload of any file.
    """
    source_system_id = cleanup.source_system_id(f"pytest_catalog_idem_{unique_id}")
    ingestion = _ingestion_service(source_system_id)

    for _ in range(3):
        result = ingestion.ingest(csv_path)
        cleanup.schema_id(result.schema.schema_id)
        service.register_source_system(ingestion.source_system())
        service.publish_schema(result.schema)

    assert service.get_snapshot(result.schema.schema_id) is not None


# ============================================================
# Restart survival - the property the defect destroyed
# ============================================================


def test_published_schema_survives_a_restart(
    service, catalog_engine, unique_id, csv_path, cleanup
):
    """upload -> publish -> restart -> schema still available.

    "Restart" is simulated the only way that actually proves anything: a brand
    new repository and service over the same database, holding none of the
    original process's in-memory state. If persistence had not happened, the
    read below would raise instead of returning the schema.
    """
    source_system_id = cleanup.source_system_id(f"pytest_catalog_restart_{unique_id}")
    ingestion = _ingestion_service(source_system_id)
    result = ingestion.ingest(csv_path)
    schema_id = cleanup.schema_id(result.schema.schema_id)

    service.register_source_system(ingestion.source_system())
    service.publish_schema(result.schema)

    expected_entities = {entity.source_name for entity in result.schema.entities}
    expected_fields = {
        field.source_name
        for entity in result.schema.entities
        for field in entity.fields
    }

    # --- restart: nothing from the writing service is reused ---
    restarted = SchemaCatalogService(CatalogRepository(catalog_engine))
    retrieved = restarted.get_snapshot(schema_id)

    assert retrieved.schema_id == schema_id
    assert retrieved.source_system_id == source_system_id

    # The structure has to survive too, not just the identifying row - a
    # snapshot with no entities would still satisfy a naive existence check.
    assert {entity.source_name for entity in retrieved.entities} == expected_entities
    assert {
        field.source_name
        for entity in retrieved.entities
        for field in entity.fields
    } == expected_fields
    assert expected_fields, "the fixture must contribute at least one field"


def test_latest_schema_is_resolvable_after_restart(
    service, catalog_engine, unique_id, csv_path, cleanup
):
    """The scope lookup used by callers must work post-restart too.

    ``get_latest`` is how a consumer finds a schema without already knowing its
    id, so restart survival via ``get_snapshot`` alone would be an incomplete
    guarantee.
    """
    source_system_id = cleanup.source_system_id(f"pytest_catalog_latest_{unique_id}")
    ingestion = _ingestion_service(source_system_id)
    result = ingestion.ingest(csv_path)
    cleanup.schema_id(result.schema.schema_id)

    service.register_source_system(ingestion.source_system())
    service.publish_schema(result.schema)

    restarted = SchemaCatalogService(CatalogRepository(catalog_engine))
    latest = restarted.get_latest(source_system_id, result.schema.schema_name)

    assert latest.schema_id == result.schema.schema_id


def test_mapping_profile_survives_a_restart(
    service, catalog_engine, repository, unique_id, csv_path, cleanup
):
    """Mapping profiles must persist across a restart as well.

    ``mapping_profiles.source_system_id`` carries the same foreign key, so it
    had the same latent defect.
    """
    from erp_pipeline.schemas.mapping_models import (
        FieldMapping,
        MappingProfile,
        MappingStatus,
    )

    source_system_id = cleanup.source_system_id(f"pytest_catalog_map_{unique_id}")
    ingestion = _ingestion_service(source_system_id)
    result = ingestion.ingest(csv_path)
    schema_id = cleanup.schema_id(result.schema.schema_id)

    service.register_source_system(ingestion.source_system())
    service.publish_schema(result.schema)

    # Built explicitly rather than via MappingService.generate: this fixture's
    # entity normalizes to "normal", which matches no canonical entity, so the
    # engine correctly declines to produce a profile. What is under test here
    # is catalog PERSISTENCE, not mapping quality - that is covered by
    # tests/erp_pipeline/mapping/.
    entity = result.schema.entities[0]
    mapping_id = cleanup.mapping_id(f"{source_system_id}_mapping")
    profile = MappingProfile(
        mapping_id=mapping_id,
        source_system_id=source_system_id,
        source_schema_id=schema_id,
        source_entity=entity.source_name,
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(
                source_field=entity.fields[0].source_name,
                target_field="invoice_id",
                status=MappingStatus.APPROVED,
            ),
        ),
        status=MappingStatus.APPROVED,
        approved_by="pytest",
    )

    service.save_mapping_profile(profile)

    restarted = SchemaCatalogService(CatalogRepository(catalog_engine))
    retrieved = restarted.get_mapping_profile(mapping_id)

    assert retrieved.mapping_id == mapping_id
    assert retrieved.source_system_id == source_system_id
    # The field mappings must survive too, not just the profile header.
    assert len(retrieved.field_mappings) == 1
    assert retrieved.field_mappings[0].target_field == "invoice_id"


# ============================================================
# The route-level helper
# ============================================================


def test_publish_helper_registers_before_publishing(
    catalog_engine, unique_id, csv_path, cleanup
):
    """``_publish_file_schema`` must report success and actually persist.

    This exercises the API helper directly rather than through HTTP, so the
    assertion is about persistence rather than about status codes.
    """
    from erp_pipeline.api.routers_data import _publish_file_schema

    source_system_id = cleanup.source_system_id(f"pytest_catalog_helper_{unique_id}")
    ingestion = _ingestion_service(source_system_id)
    result = ingestion.ingest(csv_path)
    schema_id = cleanup.schema_id(result.schema.schema_id)

    class _Services:
        catalog = SchemaCatalogService(CatalogRepository(catalog_engine))

    services = _Services()
    services.ingestion = ingestion

    published, problem = _publish_file_schema(services, result.schema)

    assert published is True
    assert problem is None

    restarted = SchemaCatalogService(CatalogRepository(catalog_engine))
    assert restarted.get_snapshot(schema_id).schema_id == schema_id


def test_publish_helper_reports_failure_instead_of_swallowing_it(unique_id, csv_path):
    """A publish failure must surface a reason, never a silent ``False``.

    The original defect was not that publishing failed - it was that the
    failure was invisible. This asserts the reason travels back to the caller.
    """
    from erp_pipeline.api.routers_data import _publish_file_schema

    source_system_id = f"pytest_catalog_fail_{unique_id}"
    ingestion = _ingestion_service(source_system_id)
    result = ingestion.ingest(csv_path)

    class _BrokenCatalog:
        def register_source_system(self, source_system):
            raise RuntimeError("catalog unavailable")

        def publish_schema(self, schema):  # pragma: no cover - never reached
            raise AssertionError("publish must not run after registration failed")

    class _Services:
        catalog = _BrokenCatalog()

    services = _Services()
    services.ingestion = ingestion

    published, problem = _publish_file_schema(services, result.schema)

    assert published is False
    assert problem is not None
    assert "RuntimeError" in problem
    # The operator needs to know the consequence, not just that something broke.
    assert "restart" in problem.lower()


def test_publish_helper_problem_message_carries_no_credentials(unique_id, csv_path):
    """A driver error can embed a connection string; it must not be echoed."""
    from erp_pipeline.api.routers_data import _publish_file_schema

    secret = "hunter2"
    ingestion = _ingestion_service(f"pytest_catalog_secret_{unique_id}")
    result = ingestion.ingest(csv_path)

    class _LeakyCatalog:
        def register_source_system(self, source_system):
            raise RuntimeError(
                f"could not connect to postgresql://erp:{secret}@db:5432/erp"
            )

        def publish_schema(self, schema):  # pragma: no cover - never reached
            raise AssertionError("unreachable")

    class _Services:
        catalog = _LeakyCatalog()

    services = _Services()
    services.ingestion = ingestion

    published, problem = _publish_file_schema(services, result.schema)

    assert published is False
    assert secret not in problem
    assert "postgresql://" not in problem
