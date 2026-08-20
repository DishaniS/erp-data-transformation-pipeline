"""The production composition root.

This is the one place that decides which *implementation* backs each contract.
Before this module existed, that decision was made by whichever test fixture
happened to assemble the system, and the shipped default was in-memory
everywhere.

    TEST MODE        in-memory stores are fine and fast
    PRODUCTION MODE  every store is durable, or startup fails saying why

The in-memory implementations are untouched and still used by tests. They are
simply never selected here.
"""

from __future__ import annotations

import logging
from typing import Any

from erp_pipeline.runtime.settings import (
    COLD_KEY_VARIABLE,
    ConfigurationError,
    QdrantSettings,
    RuntimeSettings,
)

LOGGER = logging.getLogger("erp_pipeline.runtime.services")


def build_qdrant_client(settings: QdrantSettings) -> Any:
    """One factory for both local and remote Qdrant.

    A URL wins when present (that is how a managed cluster is addressed);
    otherwise host and port. The API key is passed to the client and never
    logged, echoed or placed in an error message.
    """
    from qdrant_client import QdrantClient

    if settings.uses_url:
        return QdrantClient(
            url=settings.url,
            api_key=settings.api_key,
            timeout=settings.timeout_seconds,
        )

    return QdrantClient(
        host=settings.host,
        port=settings.port,
        api_key=settings.api_key,
        timeout=settings.timeout_seconds,
    )


def build_storage_service(settings: RuntimeSettings, engine: Any) -> Any:
    """Assemble Phase 12 with durable tier state and real tiers.

    Returns ``None`` when vectors are disabled, so a deployment without Qdrant
    still serves everything that does not need them.
    """
    from erp_pipeline.storage import (
        ColdArchiveTier,
        EnvironmentKeyProvider,
        PostgresTierStateStore,
        QdrantHotTier,
        QdrantWarmTier,
        StorageService,
    )

    hot = warm = cold = None

    if settings.qdrant.enabled:
        client = build_qdrant_client(settings.qdrant)
        hot = QdrantHotTier(
            client, settings.qdrant.hot_collection, settings.qdrant.dimension
        )
        warm = QdrantWarmTier(
            client, settings.qdrant.warm_collection, settings.qdrant.dimension
        )
        hot.ensure_collection()
        warm.ensure_collection()

    if settings.cold.enabled:
        if not settings.cold.key_present:
            # Never fall back to writing archives unencrypted. Refusing to
            # start is the correct behaviour for a missing encryption key.
            raise ConfigurationError(
                f"the cold tier is enabled but {COLD_KEY_VARIABLE} is not set; "
                "archives are never written unencrypted"
            )

        settings.cold.directory.mkdir(parents=True, exist_ok=True)
        cold = ColdArchiveTier(settings.cold.directory, EnvironmentKeyProvider())

    if hot is None and warm is None and cold is None:
        return None

    return StorageService(
        hot=hot,
        warm=warm,
        cold=cold,
        # The durable tier state - never InMemoryTierStateStore in production.
        state_store=PostgresTierStateStore(engine),
    )


def build_sync_service(settings: RuntimeSettings, engine: Any, services: Any) -> Any:
    """Assemble Phase 10 with durable sync state and real propagation.

    Phase 10 owns the whole incremental engine; this wires its collaborators
    to the same durable stores the rest of the application uses, so an
    incremental run updates the same canonical records and the same vectors as
    a full run.
    """
    from erp_pipeline.sync import (
        PostgresSyncStateStore,
        PropagationPipeline,
        SyncService,
    )

    builder = None
    embedder = None
    vector_store = None

    if services.embedding is not None:
        from erp_pipeline.ai import Phase11EmbeddingUpdater

        embedder = Phase11EmbeddingUpdater(services.embedding)

    if services.storage is not None:
        vector_store = _StorageVectorRecordStore(services.storage)

    pipeline = PropagationPipeline(
        canonical_store=services.records,
        builder=_CanonicalRepresentationBuilder(),
        embedder=embedder,
        vector_store=vector_store,
    )

    return SyncService(
        state_store=PostgresSyncStateStore(engine),
        pipeline=pipeline,
        transformation_service=services.transformation,
    )


class _CanonicalRepresentationBuilder:
    """Adapts Phase 11's representation builder to Phase 10's contract.

    Phase 10 asks for `rebuild(record)`; Phase 11 provides
    `canonical_record_to_representation`. This is a two-line adapter, not a
    reimplementation - the text construction stays entirely in Phase 11.
    """

    def rebuild(self, record: Any) -> Any:
        from erp_pipeline.ai import canonical_record_to_representation

        return canonical_record_to_representation(record)


class _StorageVectorRecordStore:
    """Routes Phase 10's vector writes through Phase 12's tiering.

    Without this, an incremental update would write straight to a collection
    and bypass the routing policy, so a record's tier would depend on which
    pipeline last touched it.
    """

    def __init__(self, storage: Any) -> None:
        self._storage = storage
        self.upsert_calls = 0
        self.delete_calls = 0

    def upsert(self, record: Any) -> Any:
        self.upsert_calls += 1

        return self._storage.store(record)

    def delete(self, representation_id: str) -> bool:
        self.delete_calls += 1

        return self._storage.delete(representation_id, force=True)


def build_production_services(
    settings: RuntimeSettings | None = None, engine: Any = None
) -> Any:
    """Assemble every phase service with durable implementations.

    Raises ``ConfigurationError`` rather than silently degrading: a service
    that quietly starts with in-memory storage looks healthy right up until a
    restart loses a day of work.
    """
    from erp_pipeline.api_specs import ApiSpecificationService
    from erp_pipeline.catalog import SchemaCatalogService
    from erp_pipeline.ingestion import FileIngestionService
    from erp_pipeline.mapping import MappingService
    from erp_pipeline.orchestration import (
        EnvironmentSecretProvider,
        PipelineServices,
        PostgresCanonicalRecordStore,
    )
    from erp_pipeline.runtime.database import build_pipeline_engine
    from erp_pipeline.runtime.persistence import (
        PostgresMappingDraftStore,
        PostgresSourceRegistry,
        PostgresUploadStore,
    )
    from erp_pipeline.transformation import TransformationService

    resolved = settings or RuntimeSettings.from_environment()
    resolved.require_valid()

    active_engine = engine or build_pipeline_engine(resolved.database)

    services = PipelineServices(
        ingestion=FileIngestionService(),
        api_specs=ApiSpecificationService(),
        mapping=MappingService(),
        transformation=TransformationService(),
        # -- durable stores --
        records=PostgresCanonicalRecordStore(active_engine),
        sources=PostgresSourceRegistry(active_engine),
        uploads=PostgresUploadStore(
            resolved.api.upload_dir,
            active_engine,
            max_bytes=resolved.api.max_upload_bytes,
        ),
        # Real credential resolution: a registered source names a secret and
        # the environment supplies it. Never NullSecretProvider.
        secrets=EnvironmentSecretProvider(),
    )
    services.mapping_drafts = PostgresMappingDraftStore(active_engine)

    try:
        from erp_pipeline.catalog import CatalogRepository

        services.catalog = SchemaCatalogService(CatalogRepository(active_engine))
    except Exception:  # noqa: BLE001 - catalog is optional at this layer
        LOGGER.warning(
            "the schema catalog could not be attached; schemas will be served "
            "from the in-process cache only"
        )

    # The model is constructed lazily by design: importing or starting the
    # application must not download or load MiniLM.
    if resolved.embedding_enabled:
        services.embedding = _LazyEmbeddingService()

    services.storage = build_storage_service(resolved, active_engine)
    services.sync = build_sync_service(resolved, active_engine, services)
    services.runtime_engine = active_engine
    services.runtime_settings = resolved

    return services


class _LazyEmbeddingService:
    """Defers model loading until the first embedding call.

    Phase 13 guarantees that importing the API loads no model. Building the
    real `EmbeddingService` in the composition root would break that
    guarantee for anyone who starts the app, so it is deferred to first use.
    """

    def __init__(self) -> None:
        self._service: Any = None

    def _load(self) -> Any:
        if self._service is None:
            from erp_pipeline.ai import EmbeddingService, SentenceTransformerModel

            LOGGER.info("loading the embedding model on first use")
            self._service = EmbeddingService(SentenceTransformerModel())

        return self._service

    @property
    def loaded(self) -> bool:
        return self._service is not None

    @property
    def model(self) -> Any:
        return self._load().model

    @property
    def model_id(self) -> str:
        # Reported without loading: the id is configuration, not a weight.
        if self._service is None:
            return "sentence-transformers/all-MiniLM-L6-v2"

        return self._service.model_id

    @property
    def dimension(self) -> int:
        if self._service is None:
            return 384

        return self._service.dimension

    def embed_one(self, *args: Any, **kwargs: Any) -> Any:
        return self._load().embed_one(*args, **kwargs)

    def embed_many(self, *args: Any, **kwargs: Any) -> Any:
        return self._load().embed_many(*args, **kwargs)

    def embed_and_store(self, *args: Any, **kwargs: Any) -> Any:
        return self._load().embed_and_store(*args, **kwargs)

    def is_current(self, *args: Any, **kwargs: Any) -> Any:
        return self._load().is_current(*args, **kwargs)

    def fingerprint(self, *args: Any, **kwargs: Any) -> Any:
        return self._load().fingerprint(*args, **kwargs)


__all__ = [
    "build_qdrant_client",
    "build_storage_service",
    "build_sync_service",
    "build_production_services",
]
