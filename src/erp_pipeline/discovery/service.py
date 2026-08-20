"""Discovery services: run discovery or inference, then optionally publish.

One service per paradigm - ``RelationalDiscoveryService`` reads declared
metadata and can profile; ``MongoDBInferenceService`` samples documents - but
both hand Phase 2 the same ``SourceSchema``, and the publish half is
deliberately identical.

Neither owns any versioning logic. Phase 2's ``SchemaCatalogService`` /
``CatalogRepository`` remain solely responsible for idempotency,
``catalog_version`` assignment, snapshot immutability and history. Discovery
produces a ``SourceSchema`` and hands it over; duplicating any of that
decision-making here would create two sources of truth about what a version
means.
"""

from __future__ import annotations

from typing import Any

from erp_pipeline.connectors.base import BaseSourceConnector
from erp_pipeline.discovery.models import (
    DiscoveryOptions,
    DiscoveryResult,
    MongoDiscoveryResult,
    MongoInferenceOptions,
    ProfilingSummary,
)
from erp_pipeline.discovery.mongodb import MongoDBSchemaInference
from erp_pipeline.discovery.profiling import profile_schema
from erp_pipeline.discovery.relational import RelationalSchemaDiscovery
from erp_pipeline.schemas.source_models import SourceSchema


class RelationalDiscoveryService:
    """Convenience orchestration over discovery, profiling and publishing."""

    def __init__(self, options: DiscoveryOptions | None = None) -> None:
        self._options = options or DiscoveryOptions()

    @property
    def options(self) -> DiscoveryOptions:
        return self._options

    def discover(self, connector: BaseSourceConnector) -> DiscoveryResult:
        """Discover structure and, when enabled, gather aggregate profiles."""
        discovery = RelationalSchemaDiscovery(connector, self._options)
        schema = discovery.discover()

        profiling: ProfilingSummary
        if self._options.profiling_enabled:
            profiling = profile_schema(connector, schema, self._options)
        else:
            profiling = ProfilingSummary(enabled=False)

        return DiscoveryResult(
            schema=schema,
            profiling=profiling,
            warnings=discovery.warnings,
        )

    def discover_and_publish(
        self,
        connector: BaseSourceConnector,
        catalog_service: Any,
        register_source_system: Any | None = None,
    ) -> tuple[DiscoveryResult, Any]:
        """Discover, then publish the schema through the Phase 2 catalog.

        Returns ``(DiscoveryResult, SchemaSnapshotResult)``. The snapshot
        result - including whether a new ``catalog_version`` was created - is
        produced entirely by Phase 2; this method only forwards the schema.

        ``register_source_system`` optionally supplies a ``SourceSystem`` to
        register first, since the catalog requires a registered source system
        before a schema snapshot referencing it can be saved.
        """
        if register_source_system is not None:
            catalog_service.register_source_system(register_source_system)

        result = self.discover(connector)
        snapshot_result = catalog_service.publish_schema(result.schema)

        return result, snapshot_result


class MongoDBInferenceService:
    """Convenience orchestration over MongoDB inference and publishing.

    The document-store counterpart of ``RelationalDiscoveryService``, with the
    same shape and the same boundaries. It has no profiling step: the sampling
    pass already produces per-field aggregate statistics, so a second pass
    over the same documents would only re-read data for no new information.
    """

    def __init__(self, options: MongoInferenceOptions | None = None) -> None:
        self._options = options or MongoInferenceOptions()

    @property
    def options(self) -> MongoInferenceOptions:
        return self._options

    def infer(self, connector: BaseSourceConnector) -> MongoDiscoveryResult:
        """Infer the observed structure of a MongoDB database."""
        inference = MongoDBSchemaInference(connector, self._options)
        schema = inference.infer()

        return MongoDiscoveryResult(
            schema=schema,
            inference=inference.summary(),
            warnings=inference.warnings,
        )

    def infer_and_publish(
        self,
        connector: BaseSourceConnector,
        catalog_service: Any,
        register_source_system: Any | None = None,
    ) -> tuple[MongoDiscoveryResult, Any]:
        """Infer, then publish the observed schema through the Phase 2 catalog.

        Returns ``(MongoDiscoveryResult, SchemaSnapshotResult)``. Whether a new
        ``catalog_version`` was created is decided entirely by Phase 2; this
        method only forwards the schema.

        ``register_source_system`` optionally supplies a ``SourceSystem`` to
        register first, since the catalog requires a registered source system
        before a schema snapshot referencing it can be saved.
        """
        if register_source_system is not None:
            catalog_service.register_source_system(register_source_system)

        result = self.infer(connector)
        snapshot_result = catalog_service.publish_schema(result.schema)

        return result, snapshot_result


def discover_relational_schema(
    connector: BaseSourceConnector, options: DiscoveryOptions | None = None
) -> SourceSchema:
    """Module-level convenience: return just the discovered ``SourceSchema``."""
    return RelationalSchemaDiscovery(connector, options).discover()


__all__ = [
    "RelationalDiscoveryService",
    "MongoDBInferenceService",
    "discover_relational_schema",
]
