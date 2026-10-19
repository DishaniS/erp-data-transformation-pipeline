"""The orchestration facade. Callable without FastAPI.

This is the object the API layer talks to, and it deliberately knows nothing
about HTTP. Keeping it framework-free means the whole pipeline can be driven
from a test or a script, and it stops request handling from leaking into
pipeline logic.

Every phase service is injected. Nothing here constructs a model, a database
engine or a vector client on import - see ``build_default_services`` for the
one place that does, called explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.orchestration.errors import (
    InvalidPipelineRequestError,
    MappingNotExecutableError,
    JobConflictError,
    JobNotFoundError,
    MappingNotFoundError,
    RecordNotFoundError,
    RetryNotSupportedError,
    SchemaNotFoundError,
    UnsupportedCapabilityError,
)
from erp_pipeline.orchestration.executor import InlineJobExecutor, JobExecutor
from erp_pipeline.orchestration.extraction import (
    ExtractionRequest,
    RelationalSnapshotExtractor,
    extractor_for,
    CsvSnapshotExtractor,
)
from erp_pipeline.orchestration.job_store import InMemoryJobStore, JobStore
from erp_pipeline.orchestration.models import (
    Job,
    JobRequest,
    JobStatus,
    JobType,
    PipelineStage,
    StageStatus,
    new_job_id,
)
from erp_pipeline.orchestration.pipeline import PipelineContext, PipelineRunner
from erp_pipeline.orchestration.planner import PipelinePlanner
from erp_pipeline.orchestration.secrets import NullSecretProvider, SecretProvider
from erp_pipeline.orchestration.sources import RegisteredSource, SourceRegistry
from erp_pipeline.orchestration.stages import DEFAULT_HANDLERS, INCREMENTAL_HANDLERS
from erp_pipeline.orchestration.upload_store import StoredUpload, UploadStore
from erp_pipeline.schemas.enums import SourceType

LOGGER = logging.getLogger("erp_pipeline.orchestration.service")


class BoundedExtractionCache:
    """A bounded LRU of extraction results, keyed by upload id.

    WHY IT IS BOUNDED NOW
    ---------------------
    Phase 6 made this cache load-bearing: the indexing job reads it so a
    scanned certificate is not OCR'd twice. Phase 6 also recorded that it was
    unbounded, which by Phase 10 is two problems rather than one. It grows
    without limit, and what it grows with is EXTRACTED DOCUMENT TEXT - the
    contents of every certificate, contract and payslip the service has seen,
    held in process memory indefinitely.

    WHY A CACHE AND NOT A STORE
    ---------------------------
    It is an optimisation, never authoritative. An evicted entry costs one
    re-extraction from the upload that is still on disk, which is exactly what
    happened before the cache existed. Nothing may depend on a hit, and
    ``ingest_upload`` re-extracts on a miss - so eviction and restart are both
    ordinary, not failure modes.
    """

    #: Small enough that a long-running service does not accumulate a corpus
    #: in memory, large enough that an upload's own indexing job hits it.
    DEFAULT_MAX_ENTRIES = 32

    def __init__(self, max_entries: int | None = None) -> None:
        from collections import OrderedDict

        resolved = (
            max_entries
            if max_entries is not None
            else _cache_size_from_environment()
        )
        # Never unlimited: a zero or negative configuration is a mistake, and
        # honouring it would restore the unbounded behaviour being removed.
        self._max_entries = max(1, int(resolved))
        self._entries: "OrderedDict[str, Any]" = OrderedDict()
        self.evictions = 0

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._entries:
            return default

        self._entries.move_to_end(key)

        return self._entries[key]

    def __getitem__(self, key: str) -> Any:
        value = self.get(key, _MISSING)

        if value is _MISSING:
            raise KeyError(key)

        return value

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._entries:
            self._entries.move_to_end(key)

        self._entries[key] = value

        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1

    def pop(self, key: str, default: Any = None) -> Any:
        return self._entries.pop(key, default)

    def clear(self) -> None:
        self._entries.clear()

    def keys(self):
        return self._entries.keys()


_MISSING = object()

#: Configurable, with a safe default. Never unlimited.
UPLOAD_CACHE_ENV = "ERP_UPLOAD_CACHE_MAX_ENTRIES"


def _cache_size_from_environment() -> int:
    import os

    try:
        return int(
            os.environ.get(UPLOAD_CACHE_ENV)
            or BoundedExtractionCache.DEFAULT_MAX_ENTRIES
        )
    except (TypeError, ValueError):
        return BoundedExtractionCache.DEFAULT_MAX_ENTRIES


@dataclass
class PipelineServices:
    """The phase services orchestration calls. All optional, all injectable.

    A missing service is not an error at construction: a deployment with no
    Qdrant should still be able to register sources and parse specs. It becomes
    an error only when a stage actually needs it.
    """

    catalog: Any = None
    mapping: Any = None
    transformation: Any = None
    ingestion: Any = None
    api_specs: Any = None
    sync: Any = None
    embedding: Any = None
    storage: Any = None
    records: Any = None
    #: Phase 5's authoritative store for AI-ready text. Optional like every
    #: other service: a deployment without one indexes exactly as it did
    #: before, and simply cannot resolve a hit back to its content.
    representations: Any = None
    #: Phase 8. The policy governing outbound asset fetches, and the client
    #: that performs them. BOTH default to None, which means refused: this
    #: package ships no HTTP client, so importing it can never cause a request
    #: and a deployment that configures nothing fetches nothing.
    #: Phase 9's current-version registry. Optional like everything else: a
    #: deployment without one behaves exactly as it did before Phase 9.
    lifecycle: Any = None
    remote_asset_policy: Any = None
    remote_asset_fetcher: Any = None
    remote_asset_resolver: Any = None
    sources: SourceRegistry = field(default_factory=SourceRegistry)
    uploads: UploadStore | None = None
    secrets: SecretProvider = field(default_factory=NullSecretProvider)
    connection_factory: Any = None
    #: Set by the service so stages can reach schemas without a catalog.
    schema_cache: dict[str, Any] = field(default_factory=dict)
    mapping_cache: dict[str, Any] = field(default_factory=dict)
    upload_results: "BoundedExtractionCache" = field(
        default_factory=lambda: BoundedExtractionCache()
    )
    #: Mappings that are NOT yet executable because Phase 8 found ambiguity.
    #: They are addressable so a human can resolve them, but a draft can never
    #: be handed to a transformation - see `get_mapping_profile`.
    mapping_drafts: dict[str, Any] = field(default_factory=dict)

    # -- helpers the stage handlers call --

    @property
    def embedding_model_id(self) -> str:
        return getattr(self.embedding, "model_id", "unavailable")

    def _require(self, service: Any, name: str) -> Any:
        if service is None:
            raise UnsupportedCapabilityError(
                f"this deployment has no {name} configured, so that stage "
                "cannot run"
            )

        return service

    def get_schema(self, schema_id: str) -> Any:
        if schema_id in self.schema_cache:
            return self.schema_cache[schema_id]

        if self.catalog is not None:
            try:
                schema = self.catalog.get_snapshot(schema_id)
            except Exception as error:  # noqa: BLE001 - normalised below
                raise SchemaNotFoundError(
                    f"schema {schema_id!r} was not found", schema_id=schema_id
                ) from error

            self.schema_cache[schema_id] = schema

            return schema

        raise SchemaNotFoundError(
            f"schema {schema_id!r} was not found", schema_id=schema_id
        )

    def get_mapping_profile(self, mapping_id: str) -> Any:
        if mapping_id in self.mapping_cache:
            return self.mapping_cache[mapping_id]

        # A draft exists but is not executable. Refusing here is the whole
        # point of Phase 8's ambiguity detection: a pipeline must not run
        # through a mapping nobody approved.
        if mapping_id in self.mapping_drafts:
            draft = self.mapping_drafts[mapping_id]

            raise MappingNotExecutableError(
                f"mapping {mapping_id!r} still has "
                f"{draft['ambiguous_fields']} ambiguous field(s) awaiting "
                "review; approve them before running data through it",
                mapping_id=mapping_id,
                ambiguous_fields=draft["ambiguous_fields"],
            )

        if self.catalog is not None:
            try:
                profile = self.catalog.get_mapping_profile(mapping_id)
            except Exception as error:  # noqa: BLE001
                raise MappingNotFoundError(
                    f"mapping {mapping_id!r} was not found", mapping_id=mapping_id
                ) from error

            self.mapping_cache[mapping_id] = profile

            return profile

        raise MappingNotFoundError(
            f"mapping {mapping_id!r} was not found", mapping_id=mapping_id
        )

    def discover_schema(self, source: RegisteredSource) -> Any:
        """Delegates to Phase 4 or Phase 5 - never re-implements discovery."""
        settings = source.connection_settings(self.secrets)

        if source.source_type is SourceType.MONGODB:
            from erp_pipeline.connectors.mongodb import MongoDBConnector
            from erp_pipeline.discovery import MongoDBInferenceService

            # Mongo inference samples documents, so it needs a CONNECTOR - a
            # live handle it can read through. This branch previously handed
            # it the settings, so every orchestrated MongoDB discovery failed
            # with "requires a source connector, got ConnectionSettings".
            #
            # The connector is closed here because discovery owns it: nothing
            # downstream holds a reference, and leaving a client open would leak
            # a socket per job.
            connector = MongoDBConnector(settings)

            try:
                result = MongoDBInferenceService().infer(connector)
                schema = getattr(result, "schema", result)
            finally:
                connector.close()
        else:
            from erp_pipeline.connectors.registry import ConnectorRegistry
            from erp_pipeline.discovery import RelationalDiscoveryService

            # Relational discovery inspects live database metadata (tables,
            # columns, keys) through SQLAlchemy, so it needs the same kind of
            # CONNECTOR as Mongo - settings alone are not a handle it can read
            # through. This branch previously handed it the settings, so every
            # orchestrated relational discovery (a PostgreSQL/MySQL/SQL Server
            # source registered through POST /v1/sources) failed with
            # "requires a source connector, got ConnectionSettings".
            connector = ConnectorRegistry.create(settings)

            try:
                result = RelationalDiscoveryService().discover(connector)
                schema = getattr(result, "schema", result)
            finally:
                connector.close()

        self.schema_cache[schema.schema_id] = schema

        if self.catalog is not None:
            self._publish_discovered_schema(source, schema)

        return schema

    def _publish_discovered_schema(
        self, source: RegisteredSource, schema: Any
    ) -> bool:
        """Register the source system, then publish the discovered schema.

        ``schema_snapshots.source_system_id`` is a foreign key into
        ``source_systems``. A source registered through ``POST /v1/sources``
        lives in ``erp_runtime.registered_sources``, which is a different
        table, so the catalog has never heard of it. Publishing without
        registering first therefore failed for every discovery.

        Registration is idempotent. The ``SourceSystem`` carries no credential:
        it is built from the registered source's identity and type only, and
        the connection settings resolved above are deliberately not consulted.
        """
        from erp_pipeline.schemas.source_models import SourceSystem

        try:
            self.catalog.register_source_system(
                SourceSystem(
                    source_system_id=schema.source_system_id,
                    name=getattr(source, "name", None) or schema.source_system_id,
                    source_type=source.source_type,
                    description=(
                        "Registered through the orchestration API and "
                        "discovered by erp_pipeline.discovery."
                    ),
                )
            )
            self.catalog.publish_schema(schema)

            return True
        except Exception as error:  # noqa: BLE001 - reported, never discarded
            # Logged with the failure type so this stops being invisible. The
            # message is not re-raised: discovery itself succeeded and the
            # schema is usable from schema_cache for this process.
            LOGGER.warning(
                "schema discovered but not published to the catalog; it will "
                "not survive a restart",
                exc_info=True,
                extra={
                    "schema_id": getattr(schema, "schema_id", None),
                    "source_system_id": getattr(schema, "source_system_id", None),
                    "error_type": type(error).__name__,
                },
            )

            return False

    def extract_snapshot(
        self, source: RegisteredSource, request: ExtractionRequest
    ) -> tuple[Any, ...]:
        """Read a bounded snapshot of one entity, using the source's extractor.

        The extractor is chosen by SOURCE TYPE rather than hardcoded. Before
        this, every source went through ``RelationalSnapshotExtractor``, which
        issues SQL - so ``MongoSnapshotExtractor`` existed, was exported, and
        was never reachable. A MongoDB source-native job could discover a schema
        and then fail to read a single document.

        ``extractor_for`` already encoded this mapping; it simply had no caller.
        CSV never arrives here - the EXTRACT stage routes uploads to
        ``extract_csv_records`` before this point.
        """
        if self.connection_factory is not None:
            factory = lambda: self.connection_factory(source)  # noqa: E731
        else:
            factory = self._sqlalchemy_factory(source)

        return extractor_for(source.source_type).extract(request, factory)

    def _sqlalchemy_factory(self, source: RegisteredSource):
        import sqlalchemy as sa

        password = (
            self.secrets.resolve(source.credential_ref)
            if source.credential_ref
            else ""
        )
        url = sa.engine.URL.create(
            drivername="postgresql+psycopg2",
            username=source.username,
            password=password,
            host=source.host,
            port=source.port,
            database=source.database,
        )
        engine = sa.create_engine(url)

        return engine.connect

    def extract_csv_records(
        self, upload_id: str, entity: Any, limit: int
    ) -> tuple[Any, ...]:
        result = self.upload_results.get(upload_id)

        if result is None:
            result = self.ingest_upload(upload_id)

        rows = _csv_rows(result)

        return CsvSnapshotExtractor().extract_rows(rows, entity, limit)

    def transform(
        self,
        records: Sequence[Any],
        profile: Any,
        schema: Any,
        source_type: Any = None,
    ) -> Any:
        from erp_pipeline.transformation import TransformationContext

        service = self._require(self.transformation, "transformation service")

        # `schema.origin` is a SchemaOrigin (how the schema was obtained), NOT
        # a SourceType (what kind of system it came from). Passing one where
        # the other is expected makes every record fail provenance
        # construction, so the source type is taken from the plan instead.
        context = TransformationContext(
            source_type=source_type,
            schema_id=getattr(schema, "schema_id", None),
            schema_version=getattr(schema, "schema_version", None),
        )

        return service.transform_records(records, profile, context)

    def remote_asset_settings(self) -> tuple[Any, Any, Any]:
        """Policy, fetcher and resolver for this deployment, or refusal."""
        return (
            self.remote_asset_policy,
            self.remote_asset_fetcher,
            self.remote_asset_resolver,
        )

    def extract_binary_assets(
        self,
        source_records: Sequence[Any],
        canonical_records: Sequence[Any],
        entity: Any,
        binary_fields: Sequence[str],
        asset_url_fields: Any = None,
        field_sensitivity: Any = None,
        job_sensitivity: Any = None,
    ) -> Any:
        """Open every attachment the rows carried or pointed at.

        Pairs each raw row with the canonical record it became, because the
        document needs the PARENT'S identity - a vector that cannot name the ERP
        row it belongs to is not traceable, and Phase 4 has nothing to filter on.

        ``asset_url_fields`` are the fields a caller EXPLICITLY declared
        fetchable. Nothing is fetched for any other field, whatever it is named
        or contains.
        """
        from erp_pipeline.orchestration.multimodal import extract_record_assets

        policy, fetcher, resolver = self.remote_asset_settings()

        return extract_record_assets(
            source_records,
            canonical_records,
            entity,
            binary_fields,
            asset_url_fields=asset_url_fields,
            field_sensitivity=field_sensitivity,
            job_sensitivity=job_sensitivity,
            url_policy=policy,
            fetcher=fetcher,
            resolver=resolver,
        )

    def transform_source_native(
        self,
        records: Sequence[Any],
        entity: Any,
        schema: Any,
        source_type: Any = None,
        source_id: str | None = None,
        key_fields: Sequence[str] | None = None,
        asset_url_fields: Sequence[str] = (),
        sensitivity: Any = None,
    ) -> Any:
        """Transform an uncovered ERP entity under its own field names.

        Delegates to Phase 9's source-native transformer. Orchestration's job
        here is only to supply accurate provenance - which source system, which
        schema snapshot - never to decide field meanings, which is exactly the
        division the canonical ``transform`` already follows.
        """
        from erp_pipeline.transformation.source_native import SourceNativeTransformer

        transformer = getattr(self, "source_native", None) or SourceNativeTransformer()

        # The schema knows which system it describes; the registered source is
        # only a fallback for schemas that predate the field.
        source_system_id = getattr(schema, "source_system_id", None) or source_id

        if not source_system_id:
            # Refused rather than defaulted. This value becomes
            # ``SourceReference.source_system_id`` on every canonical record and
            # the ``source_system_id`` key on every resulting Qdrant point - it
            # is one third of the canonical identity triple. A stand-in would
            # index real business rows under a source system that does not
            # exist, and nothing downstream could tell that from the truth.
            raise InvalidPipelineRequestError(
                "a source-native transformation needs a source system: neither "
                "the schema nor the job supplied source_system_id"
            )

        return transformer.transform_records(
            records,
            entity,
            source_system_id=source_system_id,
            source_type=source_type,
            schema_id=getattr(schema, "schema_id", None),
            schema_version=str(getattr(schema, "schema_version", "") or "") or None,
            key_fields=key_fields,
            asset_url_fields=asset_url_fields,
            # Phase 10: the job's declared class, or the transformer's existing
            # default when nothing was declared. Passing None would override
            # that default with nothing.
            **(
                {"sensitivity": sensitivity} if sensitivity is not None else {}
            ),
        )

    def build_representations(self, records: Iterable[Any]) -> tuple[Any, ...]:
        from erp_pipeline.ai import canonical_record_to_representation

        return tuple(canonical_record_to_representation(r) for r in records)

    def build_document_representations(
        self, result: Any, identity: Any = None
    ) -> tuple[Any, ...]:
        """Chunks from an extracted document, attached to an ERP record or not.

        TWO IDENTITY REGIMES, AND WHY
        -----------------------------
        Without a declared ERP identity a chunk is identified by its CONTENT.
        That is correct and deliberate: the same policy PDF uploaded twice is
        the same document, and it should occupy one representation rather than
        accumulating a copy per upload.

        With a declared identity the chunk is identified by its ATTACHMENT,
        through the same Phase 3 builder a database BLOB uses. Content identity
        alone would collide the moment one certificate is uploaded against two
        employees - identical bytes, identical chunk id, identical vector, and
        one employee's document silently overwriting the other's. That is the
        exact failure Phase 3 exists to prevent, and an upload is not a
        different enough arrival to justify a different answer.
        """
        from erp_pipeline.ai import document_to_representations

        document = getattr(result, "document", None) or result

        if identity is None or getattr(identity, "is_empty", True):
            return tuple(document_to_representations(result))

        from erp_pipeline.ai.attached_documents import (
            DocumentAttachment,
            attached_document_to_representations,
        )

        file_source = getattr(document, "file", None)
        document_id = getattr(file_source, "content_hash", None) or ""

        attachment = DocumentAttachment(
            # Only what the caller actually declared. No parent is invented
            # from the business key: an `employee_id` is not a canonical record
            # id, and a fabricated one would be indistinguishable from a real
            # reference to whoever tried to resolve it.
            parent_record_id=identity.parent_record_id,
            # What keeps two employees' copies of one certificate apart when
            # neither upload named a parent record. Without this the attachment
            # key would be identical for both and one vector would overwrite
            # the other - the Phase 3 collision, reintroduced by the upload
            # path.
            attachment_scope=_upload_attachment_scope(identity, document_id),
            # Exactly what the caller declared, and nothing else. These are two
            # thirds of the canonical identity triple and are filterable Qdrant
            # payload keys, so a stand-in ("uploaded", "documents") would assert
            # a source system and entity that do not exist - and would collapse
            # every anonymous upload, from any number of real ERP systems, into
            # one synthetic identity. Undeclared stays undeclared; the payload
            # omits the key.
            source_system_id=identity.source_system_id,
            source_entity=identity.source_entity,
            # An upload has no ERP column. The declared document type is the
            # closest true equivalent; absent when nothing was declared.
            source_field=identity.document_type,
            document_id=document_id,
            business_key_name=identity.business_key_name,
            business_key_value=identity.business_key_value,
            document_type=identity.document_type,
            # Phase 11. Phase 10 added the form field, the validation and the
            # attachment field, but not the line joining them: an upload that
            # declared RESTRICTED was accepted, validated, and then indexed
            # with no classification at all. The declaration is the caller's
            # and is carried through unchanged - never inferred, never
            # upgraded from the document type.
            sensitivity=identity.sensitivity,
            media_type=getattr(file_source, "media_type", None),
        )

        return tuple(
            attached_document_to_representations(document, attachment)
        )

    def embed(self, representations: Sequence[Any]) -> Any:
        service = self._require(self.embedding, "embedding service")

        return service.embed_many(representations)

    def store_vector(self, record: Any, profile: Any = None) -> Any:
        """Hand one embedding to storage, with the record's own routing facts.

        Orchestration's job here is to supply ACCURATE METADATA, never to pick
        a tier. The record already carries the sensitivity its canonical record
        declared - the AI layer carried it forward - so the profile is derived
        from that rather than defaulted to INTERNAL. Which tier that sensitivity
        leads to remains entirely the storage policy's decision.
        """
        service = self._require(self.storage, "storage service")

        if profile is None:
            from erp_pipeline.storage.service import StorageProfile

            profile = StorageProfile.from_metadata(
                getattr(record, "metadata", None)
            )

        return service.store(record, profile=profile)

    def ingest_upload(self, upload_id: str, reuse: bool = True) -> Any:  # noqa: D401
        """Extract an uploaded file, reusing the result if it is already known.

        The upload endpoint extracts the file to answer the request, and the
        indexing job then needs the same extraction. Re-parsing would OCR a
        scanned certificate twice for one upload - the single most expensive
        operation in the pipeline, repeated for no gain, because the bytes
        behind an upload id never change.

        The cache was already being populated here; only the read was missing.
        ``reuse=False`` forces a fresh parse for a caller that wants one.
        """
        if reuse and upload_id in self.upload_results:
            return self.upload_results[upload_id]

        service = self._require(self.ingestion, "file ingestion service")
        uploads = self._require(self.uploads, "upload store")
        result = service.ingest(uploads.path_for(upload_id))
        self.upload_results[upload_id] = result

        return result

    def parse_api_spec(self, upload_id: str) -> Any:
        service = self._require(self.api_specs, "API specification service")
        uploads = self._require(self.uploads, "upload store")

        return service.parse(uploads.path_for(upload_id))

    # ------------------------------------------------------------------
    # Phase 10 delegation
    # ------------------------------------------------------------------

    #: Source types whose changes can be polled with a watermark. A PDF or an
    #: OpenAPI document has no cursor and no change stream, so pretending it
    #: supports CDC would be a lie the planner then has to live with.
    INCREMENTAL_SOURCES = frozenset(
        {
            SourceType.POSTGRESQL,
            SourceType.MYSQL,
            SourceType.SQL_SERVER,
            SourceType.MONGODB,
        }
    )

    def build_sync_target(self, request: JobRequest, schema: Any) -> Any:
        """Assemble Phase 10's ``SyncTarget`` from the job request."""
        from erp_pipeline.sync import SyncTarget

        profile = (
            self.get_mapping_profile(request.mapping_id)
            if request.mapping_id
            else None
        )
        source = self.sources.get(request.source_id)

        from erp_pipeline.orchestration.extraction import resolve_entity

        entity = resolve_entity(schema, request.entity)

        return SyncTarget(
            source_system_id=source.source_id,
            source_entity=entity.source_name,
            source_type=source.source_type,
            mapping_profile=profile,
            schema_id=getattr(schema, "schema_id", None),
            schema_hash=getattr(schema, "schema_hash", None),
        )

    def build_extraction_config(self, request: JobRequest, schema: Any) -> Any:
        """Validate the incremental cursor against the DISCOVERED schema.

        A watermark field is an identifier that reaches SQL, so it is checked
        against the schema rather than trusted. Accepting an arbitrary string
        here would undo the extraction guarantees the rest of the system keeps.
        """
        from erp_pipeline.orchestration.extraction import resolve_entity
        from erp_pipeline.sync import ExtractionConfig, WatermarkStrategy

        entity = resolve_entity(schema, request.entity)
        known = {field.source_name for field in entity.fields}
        options = request.options

        def checked(name: str, label: str) -> str | None:
            value = options.get(name)

            if value is None:
                return None

            if value not in known:
                raise InvalidPipelineRequestError(
                    f"{label} {value!r} is not a field of "
                    f"{entity.source_name!r}; incremental extraction only "
                    "accepts fields present in the discovered schema"
                )

            return value

        watermark_field = checked("watermark_field", "watermark field")

        if watermark_field is None:
            raise InvalidPipelineRequestError(
                "an incremental sync needs options.watermark_field naming a "
                f"monotonic column of {entity.source_name!r} (for example an "
                "updated_at timestamp)"
            )

        key_field = checked("key_field", "key field") or (
            entity.primary_key_fields[0] if entity.primary_key_fields else None
        )

        if key_field is None:
            raise InvalidPipelineRequestError(
                f"{entity.source_name!r} has no primary key and no "
                "options.key_field was supplied"
            )

        strategy_name = str(options.get("strategy", "composite")).lower()

        try:
            strategy = WatermarkStrategy(strategy_name)
        except ValueError as error:
            raise InvalidPipelineRequestError(
                f"{strategy_name!r} is not a supported watermark strategy"
            ) from error

        return ExtractionConfig(
            source_entity=entity.source_name,
            strategy=strategy,
            key_field=key_field,
            watermark_field=watermark_field,
            # Ties on the watermark are broken by the key, which is what makes
            # equal timestamps safe rather than a source of skipped rows.
            tie_break_field=checked("tie_break_field", "tie-break field") or key_field,
            namespace=entity.namespace,
            deleted_flag_field=checked("deleted_flag_field", "soft-delete field"),
        )

    def _incremental_engine(self, source: RegisteredSource) -> Any:
        """The SQLAlchemy engine Phase 10's extractor reads changes from."""
        if self.connection_factory is not None:
            return self.connection_factory(source)

        import sqlalchemy as sa

        password = (
            self.secrets.resolve(source.credential_ref)
            if source.credential_ref
            else ""
        )
        url = sa.engine.URL.create(
            drivername="postgresql+psycopg2",
            username=source.username,
            password=password,
            host=source.host,
            port=source.port,
            database=source.database,
        )

        return sa.create_engine(url, pool_pre_ping=True)

    def check_drift(self, request: JobRequest) -> Any:
        """Really invoke Phase 10's drift computation.

        This previously returned a cached attribute, which meant the API could
        report "no drift" for a schema nobody had compared. It now discovers
        the CURRENT schema, loads the PREVIOUS one from the catalog, and hands
        both to Phase 10 - which owns the diff and the mapping-impact rules.
        """
        service = self._require(self.sync, "sync service")
        source = self.sources.get(request.source_id)

        new_schema = self.discover_schema(source)
        previous_schema = None

        if self.catalog is not None:
            try:
                history = self.catalog.history(
                    source.source_id, getattr(new_schema, "schema_name", None)
                )
                # history[0] is the snapshot just published, so the comparison
                # baseline is the one before it.
                for record in list(history)[1:]:
                    candidate = self.catalog.get_snapshot(record.schema_id)

                    if candidate.schema_id != new_schema.schema_id:
                        previous_schema = candidate
                        break
            except Exception:  # noqa: BLE001 - absence is a valid answer
                previous_schema = None

        target = self.build_sync_target(request, new_schema)

        return service.check_drift(
            target=target,
            new_schema=new_schema,
            previous_schema=previous_schema,
        )

    def run_incremental(self, request: JobRequest) -> Any:
        """Really invoke Phase 10's incremental run.

        Phase 13 supplies configuration and collaborators; every watermark,
        checkpoint, content-hash and at-least-once decision stays inside
        Phase 10.
        """
        service = self._require(self.sync, "sync service")
        source = self.sources.get(request.source_id)

        if source.source_type not in self.INCREMENTAL_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source.source_type.value} has no incremental cursor, so "
                "change data capture is not available for it",
                code_hint="UNSUPPORTED_INCREMENTAL_STRATEGY",
                source_type=source.source_type.value,
            )

        if source.source_type is SourceType.MONGODB:
            raise UnsupportedCapabilityError(
                "incremental sync for MongoDB needs a change-stream extractor "
                "that is not configured in this deployment",
                source_type=source.source_type.value,
            )

        schema = (
            self.get_schema(request.schema_id)
            if request.schema_id
            else self.discover_schema(source)
        )

        from erp_pipeline.sync import RelationalIncrementalExtractor, SyncOptions

        config = self.build_extraction_config(request, schema)
        extractor = RelationalIncrementalExtractor(
            self._incremental_engine(source), config
        )
        target = self.build_sync_target(request, schema)

        options = SyncOptions(
            batch_size=int(request.options.get("batch_size", 100)),
            process_deletes=bool(request.options.get("process_deletes", False)),
        )

        return service.run_incremental(
            target=target,
            extractor=extractor,
            options=options,
            strategy=config.strategy,
            watermark_field=config.watermark_field,
            tie_break_field=config.tie_break_field,
        )


def _csv_rows(result: Any) -> Iterable[Any]:
    """Phase 6 exposes rows through a reader; find it without assuming shape."""
    for attribute in ("iter_rows", "rows", "read_rows"):
        candidate = getattr(result, attribute, None)

        if callable(candidate):
            return candidate()

        if candidate is not None:
            return candidate

    reader = getattr(result, "_row_reader", None)

    if callable(reader):
        return reader()

    raise InvalidPipelineRequestError("the CSV result exposes no row reader")


class OrchestrationService:
    """Creates jobs, runs pipelines, and answers questions about them."""

    def __init__(
        self,
        services: PipelineServices | None = None,
        job_store: JobStore | None = None,
        executor: Any = None,
        planner: PipelinePlanner | None = None,
        handlers: Mapping[PipelineStage, Any] | None = None,
    ) -> None:
        self.services = services or PipelineServices()
        self.jobs = job_store or InMemoryJobStore()
        self.executor = executor or InlineJobExecutor()
        self.planner = planner or PipelinePlanner()
        self._handlers = dict(handlers or DEFAULT_HANDLERS)
        self.runner = PipelineRunner(self._handlers)

        # An incremental job reuses stage NAMES that mean something different:
        # Phase 10 does the whole propagation in one call, so those stages
        # report rather than re-execute. Using the standard handlers would
        # transform and store every change a second time.
        self._incremental_runner = PipelineRunner(
            {**self._handlers, **INCREMENTAL_HANDLERS} if handlers is None
            else self._handlers
        )

    # -- sources --

    @property
    def sources(self) -> SourceRegistry:
        return self.services.sources

    @property
    def uploads(self) -> UploadStore | None:
        return self.services.uploads

    # -- lifecycle --

    def recover_interrupted_jobs(self) -> tuple[Job, ...]:
        """Called at startup. Never promotes an unfinished job to success."""
        return self.jobs.reap_interrupted()

    # -- jobs --

    def index_schema(self, schema_id: str) -> tuple[str | None, str | None, str | None]:
        """Start a schema indexing job for a schema already in the catalog.

        Returns ``(job_id, status, error)``. Never raises: a schema that was
        discovered and catalogued successfully is a real result, and losing it
        because indexing could not be scheduled would be the wrong trade. The
        caller reports the failure and the manual job route remains available.
        """
        try:
            job = self.submit(
                JobRequest(job_type=JobType.SCHEMA_PIPELINE, schema_id=schema_id)
            )
        except Exception as error:  # noqa: BLE001 - reported, never raised
            return (
                None,
                None,
                "the schema was discovered and catalogued, but automatic "
                f"indexing could not be started ({type(error).__name__}). It "
                "can be started with POST /v1/jobs using "
                "job_type=schema_pipeline and this schema_id.",
            )

        try:
            current = self.get(job.job_id)
        except Exception:  # noqa: BLE001 - the job exists; the snapshot is extra
            current = job

        return job.job_id, (current or job).status.value, None

    def submit(
        self, request: JobRequest, idempotency_key: str | None = None
    ) -> Job:
        """Validate, plan, persist, enqueue. Returns before the work is done."""
        fingerprint = request.fingerprint()

        if idempotency_key:
            existing = self.jobs.find_by_idempotency_key(idempotency_key)

            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise JobConflictError(
                        "this idempotency key was already used with a "
                        "different request payload",
                        idempotency_key=idempotency_key,
                        existing_job_id=existing.job_id,
                    )

                return existing

        source_type = None

        if request.source_id:
            source_type = self.sources.get(request.source_id).source_type

        # Planning happens before persistence so an impossible request is
        # refused synchronously rather than becoming a failed job.
        plan = self.planner.plan(request, source_type)

        job = Job(
            job_id=new_job_id(),
            request=request,
            status=JobStatus.PENDING,
            stages=self.runner.initial_stages(plan),
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        self.jobs.create(job)

        self.executor.submit(lambda: self._execute(job.job_id, plan))

        return job

    def _execute(self, job_id: str, plan: Any) -> Job:
        job = self.jobs.load(job_id)

        if job is None:  # pragma: no cover - only on external deletion
            raise JobNotFoundError(f"job {job_id!r} disappeared before execution")

        context = PipelineContext(job=job, plan=plan, services=self.services)
        runner = (
            self._incremental_runner
            if job.request.job_type is JobType.INCREMENTAL_SYNC
            else self.runner
        )

        try:
            finished = runner.run(context)
        except Exception as error:  # noqa: BLE001 - a job must always settle
            LOGGER.exception("pipeline raised outside a stage")
            finished = replace(
                job,
                status=JobStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                error_code=getattr(error, "code", "PIPELINE_ERROR"),
                error_message="the pipeline failed before completing",
                version=job.version + 1,
            )

        return self.jobs.save(finished)

    def get(self, job_id: str) -> Job:
        job = self.jobs.load(job_id)

        if job is None:
            raise JobNotFoundError(f"job {job_id!r} was not found", job_id=job_id)

        return job

    def list(self, **filters: Any) -> tuple[Job, ...]:
        return self.jobs.list(**filters)

    def retry(self, job_id: str) -> Job:
        """Re-submit a failed job as a NEW job.

        Deliberately not an in-place resume. Phase 10's incremental state moves
        forward as it runs, so replaying a half-finished sync could reprocess
        or skip changes. Only job types that are safe to run from the start are
        allowed.
        """
        job = self.get(job_id)

        if job.status not in {JobStatus.FAILED, JobStatus.INTERRUPTED}:
            raise RetryNotSupportedError(
                f"only failed or interrupted jobs may be retried; this job is "
                f"{job.status.value}",
                job_id=job_id,
                status=job.status.value,
            )

        if job.request.job_type in {JobType.INCREMENTAL_SYNC}:
            raise RetryNotSupportedError(
                "incremental sync advances watermark state as it runs, so a "
                "generic replay could reprocess or skip changes; re-run it "
                "explicitly instead",
                job_id=job_id,
                job_type=job.request.job_type.value,
            )

        return self.submit(job.request)


__all__ = ["PipelineServices", "OrchestrationService"]


def _upload_attachment_scope(identity: Any, document_id: str) -> str:
    """What makes one upload's attachment distinct from another's.

    Preference order, and the reason for it:

    1. the declared ``parent_record_id`` - an actual ERP record beats anything
       derived,
    2. the declared business key - two employees issued one certificate are
       different attachments even though neither upload named a record id,
    3. the document's own content id - nothing was declared, so the document is
       its own scope and re-uploading it is genuinely the same attachment.

    This value is an identity discriminator, NOT a record reference: it never
    reaches ``parent_record_id``, which stays absent unless the caller declared
    one.
    """
    if identity.parent_record_id:
        return identity.parent_record_id

    if identity.has_business_key:
        return f"{identity.business_key_name}={identity.business_key_value}"

    return document_id
