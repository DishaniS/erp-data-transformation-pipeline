"""Thin orchestration layer combining the repository and pure versioning logic.

``CatalogRepository`` is CRUD only - it knows how to read and write rows.
``versioning`` is pure - it knows how to compare and summarize already-loaded
objects. ``SchemaCatalogService`` is the seam between them: the handful of
operations that need both a database round trip and a versioning decision.

This is not, and must not become, an API. No HTTP, no request/response
objects - just plain Python methods over the repository.
"""

from __future__ import annotations

from erp_pipeline.catalog.repository import (
    CatalogRepository,
    SchemaSnapshotRecord,
    SchemaSnapshotResult,
)
from erp_pipeline.catalog.versioning import SchemaDiff, SchemaSnapshotSummary, compare_schemas, summarize_schema
from erp_pipeline.schemas.mapping_models import MappingProfile
from erp_pipeline.schemas.source_models import SourceSchema, SourceSystem


class SchemaCatalogService:
    """Register sources, publish schema snapshots, compare and summarize them."""

    def __init__(self, repository: CatalogRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> CatalogRepository:
        """Escape hatch to the underlying repository for callers that need it."""
        return self._repository

    # ---- Source systems ----

    def register_source_system(self, source_system: SourceSystem) -> SourceSystem:
        """Register (or idempotently re-register) a source system."""
        return self._repository.save_source_system(source_system)

    # ---- Schema snapshots ----

    def publish_schema(self, source_schema: SourceSchema) -> SchemaSnapshotResult:
        """Persist a schema snapshot, applying the catalog's versioning rules."""
        return self._repository.save_schema_snapshot(source_schema)

    def get_latest(self, source_system_id: str, schema_name: str) -> SourceSchema:
        """Retrieve and fully reconstruct the latest snapshot in a scope."""
        return self._repository.get_latest_schema(source_system_id, schema_name)

    def get_snapshot(self, schema_id: str) -> SourceSchema:
        return self._repository.get_schema_snapshot(schema_id)

    def history(
        self, source_system_id: str, schema_name: str | None = None
    ) -> tuple[SchemaSnapshotRecord, ...]:
        """Snapshot history for a scope, oldest first."""
        return self._repository.list_schema_snapshots(source_system_id, schema_name)

    # ---- Comparison and summary ----

    def compare_versions(self, old_schema_id: str, new_schema_id: str) -> SchemaDiff:
        """Compare two persisted snapshots by id and return their structural diff."""
        old_schema = self._repository.get_schema_snapshot(old_schema_id)
        new_schema = self._repository.get_schema_snapshot(new_schema_id)
        return compare_schemas(old_schema, new_schema)

    def summarize(self, schema_id: str) -> SchemaSnapshotSummary:
        """Build a snapshot summary from data actually retrieved from the catalog."""
        record = self._repository.get_schema_snapshot_record(schema_id)
        schema = self._repository.get_schema_snapshot(schema_id)
        source_system = self._repository.get_source_system(schema.source_system_id)
        return summarize_schema(source_system, schema, record.catalog_version)

    # ---- Mapping profiles ----

    def save_mapping_profile(self, profile: MappingProfile) -> MappingProfile:
        return self._repository.save_mapping_profile(profile)

    def get_mapping_profile(self, mapping_id: str) -> MappingProfile:
        return self._repository.get_mapping_profile(mapping_id)


__all__ = ["SchemaCatalogService"]
