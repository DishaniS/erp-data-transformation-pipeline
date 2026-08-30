"""Two MongoDB orchestration defects, pinned so they cannot return.

Both were the same shape of bug: MongoDB support existed, was exported, and was
never reached. Discovery worked, so the gap only appeared when a job actually
tried to read documents - which no test did, because nothing exercised the
orchestrated MongoDB path end to end.

DEFECT A - the extractor was chosen by nobody
---------------------------------------------
``PipelineServices.extract_snapshot`` constructed ``RelationalSnapshotExtractor``
unconditionally. ``extractor_for()`` already mapped MongoDB to
``MongoSnapshotExtractor``, but had no caller anywhere in the package, so a
MongoDB source-native job would have issued SQL against a Mongo connection.

DEFECT B - discovery handed the wrong object to inference
---------------------------------------------------------
The MongoDB branch of ``PipelineServices.discover_schema`` called
``MongoDBInferenceService().infer(settings)``. Mongo inference samples documents,
so it needs a live CONNECTOR; relational discovery is satisfied by settings
alone. Every orchestrated MongoDB discovery failed with
``UnsupportedDiscoverySourceError: requires a source connector, got
ConnectionSettings``.

These tests need no live MongoDB. They assert the DISPATCH - which class is
chosen, and what type is handed to inference - because that is exactly what was
wrong.
"""

from __future__ import annotations

import pytest

from erp_pipeline.connectors.config import ConnectionSettings
from erp_pipeline.orchestration.extraction import (
    ExtractionRequest,
    MongoSnapshotExtractor,
    RelationalSnapshotExtractor,
)
from erp_pipeline.orchestration.service import PipelineServices
from erp_pipeline.orchestration.sources import RegisteredSource
from erp_pipeline.schemas.enums import EntityKind, SchemaOrigin, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema


def mongo_source() -> RegisteredSource:
    return RegisteredSource(
        source_id="mongo_probe",
        name="mongo_probe",
        source_type=SourceType.MONGODB,
        host="mongo.invalid",
        port=27017,
        database="probe_db",
        username="probe_reader",
        auth_database="admin",
    )


def relational_source() -> RegisteredSource:
    return RegisteredSource(
        source_id="pg_probe",
        name="pg_probe",
        source_type=SourceType.POSTGRESQL,
        host="postgres.invalid",
        port=5432,
        database="probe_db",
        username="probe_reader",
    )


def probe_schema() -> tuple[SourceSchema, SourceEntity]:
    entity = SourceEntity(
        entity_id="probe.employees",
        source_name="employees",
        normalized_name="employees",
        entity_kind=EntityKind.COLLECTION,
        fields=(
            SourceField(
                source_name="employee_id",
                normalized_name="employee_id",
                source_data_type="string",
            ),
        ),
    )
    schema = SourceSchema(
        schema_id="probe.employees.v1",
        source_system_id="mongo_probe",
        schema_name="probe_db",
        origin=SchemaOrigin.INFERRED,
        entities=(entity,),
    )

    return schema, entity


# ======================================================================
# DEFECT A - extractor dispatch
# ======================================================================


class TestExtractSnapshotDispatchesBySourceType:
    """The extractor must be chosen from the source type, not hardcoded."""

    @pytest.fixture
    def spies(self, monkeypatch):
        """Record which extractor ran, without either touching a database."""
        used: list[str] = []

        def mongo_extract(self, request, connection_factory):
            used.append("mongo")

            return ()

        def relational_extract(self, request, connection_factory):
            used.append("relational")

            return ()

        monkeypatch.setattr(MongoSnapshotExtractor, "extract", mongo_extract)
        monkeypatch.setattr(RelationalSnapshotExtractor, "extract", relational_extract)

        return used

    def test_a_mongodb_source_uses_the_mongo_extractor(self, spies):
        """The defect: this used to record ``relational``.

        A relational extractor against a Mongo connection issues SQL, so the
        job could never have read a document.
        """
        schema, entity = probe_schema()
        services = PipelineServices(connection_factory=lambda source: object())

        services.extract_snapshot(
            mongo_source(), ExtractionRequest(schema, entity, 10)
        )

        assert spies == ["mongo"]

    def test_a_relational_source_still_uses_the_relational_extractor(self, spies):
        """The fix must not redirect the path that was already correct."""
        schema, entity = probe_schema()
        services = PipelineServices(connection_factory=lambda source: object())

        services.extract_snapshot(
            relational_source(), ExtractionRequest(schema, entity, 10)
        )

        assert spies == ["relational"]

    def test_the_dispatch_goes_through_extractor_for(self):
        """One mapping, not two.

        ``extractor_for`` is the declared source-type-to-extractor mapping. If
        ``extract_snapshot`` ever grows its own copy, the two will drift.
        """
        from erp_pipeline.orchestration.extraction import extractor_for

        assert isinstance(extractor_for(SourceType.MONGODB), MongoSnapshotExtractor)
        assert isinstance(
            extractor_for(SourceType.POSTGRESQL), RelationalSnapshotExtractor
        )


# ======================================================================
# DEFECT B - discovery receives a connector
# ======================================================================


class TestMongoDiscoveryReceivesAConnector:
    """Mongo inference samples documents, so settings alone cannot serve it."""

    @pytest.fixture
    def captured(self, monkeypatch):
        """Capture what inference was handed, without connecting to anything."""
        seen: list[object] = []

        def fake_infer(self, connector):
            seen.append(connector)
            schema, _ = probe_schema()

            return schema

        from erp_pipeline.discovery import MongoDBInferenceService

        monkeypatch.setattr(MongoDBInferenceService, "infer", fake_infer)

        # The connector is constructed but must never open a socket here.
        from erp_pipeline.connectors.mongodb import MongoDBConnector

        monkeypatch.setattr(MongoDBConnector, "close", lambda self: None)

        return seen

    def test_inference_receives_a_connector_not_connection_settings(self, captured):
        """The defect: this used to receive ``ConnectionSettings`` and raise."""
        from erp_pipeline.connectors.mongodb import MongoDBConnector

        PipelineServices().discover_schema(mongo_source())

        assert len(captured) == 1
        handed = captured[0]

        assert isinstance(handed, MongoDBConnector), (
            f"Mongo inference was handed {type(handed).__name__}; it requires a "
            "connector because it samples documents"
        )
        assert not isinstance(handed, ConnectionSettings)

    def test_the_discovered_schema_is_cached_like_any_other(self, captured):
        """The Mongo branch must not skip the shared bookkeeping."""
        services = PipelineServices()

        schema = services.discover_schema(mongo_source())

        assert services.schema_cache[schema.schema_id] is schema

    def test_the_connector_is_closed_after_discovery(self, monkeypatch):
        """Discovery owns the connector, so it must not leak a client per job."""
        closed: list[bool] = []

        from erp_pipeline.connectors.mongodb import MongoDBConnector
        from erp_pipeline.discovery import MongoDBInferenceService

        monkeypatch.setattr(
            MongoDBInferenceService, "infer", lambda self, connector: probe_schema()[0]
        )
        monkeypatch.setattr(
            MongoDBConnector, "close", lambda self: closed.append(True)
        )

        PipelineServices().discover_schema(mongo_source())

        assert closed == [True]

    def test_the_connector_is_closed_even_when_inference_fails(self, monkeypatch):
        """A failed discovery must not hold a socket open either."""
        closed: list[bool] = []

        from erp_pipeline.connectors.mongodb import MongoDBConnector
        from erp_pipeline.discovery import MongoDBInferenceService

        def exploding_infer(self, connector):
            raise RuntimeError("inference failed")

        monkeypatch.setattr(MongoDBInferenceService, "infer", exploding_infer)
        monkeypatch.setattr(
            MongoDBConnector, "close", lambda self: closed.append(True)
        )

        with pytest.raises(RuntimeError):
            PipelineServices().discover_schema(mongo_source())

        assert closed == [True]
