"""MongoDB observed-schema inference - collections in, ``SourceSchema`` out.

Phase 5 answers "WHAT STRUCTURE WAS OBSERVED in this collection?" It does not,
and cannot, answer "what is guaranteed to exist in every future document?" -
an ordinary MongoDB collection declares no schema, so there is no authority to
read. Everything this module produces is sample-derived, and says so:
``SourceSchema.origin`` is ``SchemaOrigin.INFERRED``, never ``DISCOVERED``.

Position in the architecture::

    MongoDB
       |
       v
    Phase 3 MongoDBConnector      erp_pipeline.connectors.mongodb
       |                           (create_database_handle() seam)
       v
    Phase 5 bounded inference     THIS MODULE + mongodb_inference.py
       |
       v
       SourceSchema               erp_pipeline.schemas (Phase 1 contract)
       |
       v
    Phase 2 Schema Catalog        erp_pipeline.catalog

The output is the SAME contract relational discovery produces. There is
deliberately no ``MongoCollectionSchema`` / ``MongoFieldSchema`` public model
competing with ``SourceSchema`` -> ``SourceEntity`` -> ``SourceField``; that
convergence is the whole point of the phase::

    Relational Metadata
            |
            v
        SourceSchema
            ^
            |
    MongoDB Inference

Division of labour with ``mongodb_inference``: that module holds every
structural rule and touches no driver; this one holds every driver interaction
and makes no structural decision of its own.

READ-ONLY
---------
This module calls exactly three driver operations - ``list_collections``,
``find`` and ``estimated_document_count`` - and never constructs a
``MongoClient`` of its own. It contains no insert, update, delete, drop,
create, index or aggregation call;
``tests/erp_pipeline/discovery/test_mongodb_read_only_safety.py`` proves it by
walking this module's AST.

This module never imports ``pymongo`` or ``bson`` at module scope, nor any
dataset-specific module.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.connectors.base import BaseSourceConnector
from erp_pipeline.connectors.errors import redact_text
from erp_pipeline.discovery.errors import (
    MetadataInspectionError,
    MongoInferenceError,
    UnsupportedDiscoverySourceError,
)
from erp_pipeline.discovery.models import (
    CollectionInferenceSummary,
    MongoInferenceOptions,
    MongoInferenceSummary,
)
from erp_pipeline.discovery.mongodb_inference import (
    DocumentStructureInference,
    build_source_fields,
    deduplicate_normalized_name,
)
from erp_pipeline.schemas.enums import EntityKind, SchemaOrigin, SourceType
from erp_pipeline.schemas.identity import IdentityError, hash_json_payload, normalize_identifier
from erp_pipeline.schemas.source_models import SourceEntity, SourceSchema

#: Phase 5 covers document databases. A relational connector is rejected
#: explicitly rather than silently producing an empty inferred schema.
SUPPORTED_SOURCE_TYPES: frozenset[SourceType] = frozenset({SourceType.MONGODB})

#: Sort key for deterministic sampling. Every MongoDB document has ``_id``,
#: and it is always indexed, so ordering by it is both universally available
#: and cheap. See ``_sample_documents``.
SAMPLE_SORT_FIELD = "_id"

#: Placeholder used while the schema's own content hash - and therefore its
#: final snapshot id - is still being computed. Same two-pass trick as
#: relational discovery.
_PROVISIONAL_SCHEMA_ID = "provisional.schema.id"

#: Recorded in schema and entity metadata so that a stored snapshot states,
#: in the data itself, what kind of claim it is making.
_OBSERVED_SCHEMA_NOTE = (
    "Observed/inferred schema. Derived from a bounded sample of documents, "
    "not from a declared MongoDB schema. Field presence, types and "
    "requiredness describe the sample only."
)


class MongoDBSchemaInference:
    """Infers the observed structure of a MongoDB database.

    Returns the Phase 1 ``SourceSchema``; call ``summary()`` afterwards for the
    supplemental per-collection statistics that deliberately do not live
    inside it.
    """

    def __init__(
        self,
        connector: BaseSourceConnector,
        options: MongoInferenceOptions | None = None,
    ) -> None:
        self._connector = _require_mongodb_connector(connector)
        self._options = options or MongoInferenceOptions()
        self._warnings: list[str] = []
        self._collection_summaries: list[CollectionInferenceSummary] = []
        self._collections_discovered = 0
        self._documents_sampled = 0
        self._budget_exhausted = False

    @property
    def warnings(self) -> tuple[str, ...]:
        """Non-fatal problems - a collection skipped, a limit reached."""
        return tuple(self._warnings)

    @property
    def options(self) -> MongoInferenceOptions:
        return self._options

    # ------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------

    def infer(self) -> SourceSchema:
        """Sample the database and build the generic ``SourceSchema``."""
        database = self._connector._settings.database  # noqa: SLF001 - sanctioned seam
        handle = self._open_database()

        entities = self._infer_entities(handle)
        schema_name = self._build_schema_name(database)

        # Two-pass build, exactly as in relational discovery:
        # compute_schema_hash() excludes schema_id, so a provisional id can be
        # used to compute the hash and the final content-addressed id derived
        # from it (see _build_schema_id).
        provisional = self._assemble(
            schema_id=_PROVISIONAL_SCHEMA_ID,
            schema_name=schema_name,
            database=database,
            entities=entities,
            schema_hash=None,
        )
        structural_hash = provisional.compute_schema_hash()

        return self._assemble(
            schema_id=self._build_schema_id(database, schema_name, structural_hash),
            schema_name=schema_name,
            database=database,
            entities=entities,
            schema_hash=structural_hash,
        )

    def summary(self) -> MongoInferenceSummary:
        """Aggregate-only evidence for the run that just completed."""
        return MongoInferenceSummary(
            database=self._connector._settings.database,  # noqa: SLF001
            collections_discovered=self._collections_discovered,
            collections_inferred=len(self._collection_summaries),
            total_documents_sampled=self._documents_sampled,
            partial=any(summary.partial for summary in self._collection_summaries)
            or self._budget_exhausted,
            budget_exhausted=self._budget_exhausted,
            notes=tuple(self._warnings),
            collections=tuple(self._collection_summaries),
        )

    # ------------------------------------------------------------
    # Database access (Phase 3 seam)
    # ------------------------------------------------------------

    def _open_database(self):
        try:
            return self._connector.create_database_handle()
        except Exception as exc:
            raise MetadataInspectionError(
                f"Could not open a database handle for source_system_id="
                f"{self._connector.source_system_id!r}: {redact_text(str(exc))}"
            ) from exc

    # ------------------------------------------------------------
    # Collection discovery (Steps 5, 20)
    # ------------------------------------------------------------

    def _list_collections(self, handle) -> tuple[dict[str, Any], ...]:
        """Read collection metadata, including validator options.

        ``list_collections`` is the one call that reports a collection's
        ``type`` (collection / view / timeseries) and its ``options`` - which
        is where a configured validator lives. Reading it once here avoids a
        second metadata round trip per collection.
        """
        try:
            raw = list(handle.list_collections())
        except Exception as exc:
            raise MetadataInspectionError(
                f"Could not list collections for database "
                f"{self._connector._settings.database!r}: "  # noqa: SLF001
                f"{redact_text(str(exc))}"
            ) from exc

        described: list[dict[str, Any]] = []

        for entry in raw:
            if not isinstance(entry, Mapping):
                continue

            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue

            described.append(
                {
                    "name": name,
                    "type": str(entry.get("type") or "collection"),
                    "options": entry.get("options") or {},
                }
            )

        # Sorted so entity order never depends on server enumeration order.
        return tuple(sorted(described, key=lambda entry: entry["name"]))

    def _selected_collections(self, handle) -> tuple[dict[str, Any], ...]:
        described = self._list_collections(handle)
        self._collections_discovered = len(described)

        selected: list[dict[str, Any]] = []

        for entry in described:
            if not self._options.wants_collection(entry["name"]):
                continue

            if entry["type"] == "view" and not self._options.include_views:
                continue

            selected.append(entry)

        if not selected:
            self._warnings.append(
                "No collections matched the inference options; nothing to infer."
            )

        return tuple(selected)

    # ------------------------------------------------------------
    # Entities (Steps 6, 7, 10, 11, 12, 19)
    # ------------------------------------------------------------

    def _infer_entities(self, handle) -> tuple[SourceEntity, ...]:
        entities: list[SourceEntity] = []
        used_entity_names: dict[str, int] = {}

        for entry in self._selected_collections(handle):
            try:
                entities.append(
                    self._infer_entity(handle, entry, used_entity_names)
                )
            except Exception as exc:
                # A collection dropped or made unreadable mid-run must not
                # abort inference of every other collection.
                self._warnings.append(
                    f"Skipped collection {entry['name']!r}: {redact_text(str(exc))}"
                )

        return tuple(entities)

    def _infer_entity(
        self,
        handle,
        entry: Mapping[str, Any],
        used_entity_names: dict[str, int],
    ) -> SourceEntity:
        collection_name = entry["name"]
        database = self._connector._settings.database  # noqa: SLF001

        remaining = self._remaining_document_budget()
        limit = min(self._options.max_documents_per_collection, remaining)
        budget_exhausted = limit <= 0

        if budget_exhausted:
            self._budget_exhausted = True
            self._warnings.append(
                f"Collection {collection_name!r} was not sampled: the "
                f"max_total_documents budget "
                f"({self._options.max_total_documents}) was already spent."
            )

        notes: list[str] = []
        deterministic = self._options.deterministic_sampling

        inference = DocumentStructureInference(self._options)

        if not budget_exhausted:
            documents, deterministic = self._sample_documents(
                handle, collection_name, limit, notes
            )
            inference.observe_all(documents)
            self._documents_sampled += inference.documents_sampled

        inferred = build_source_fields(inference.observations(), self._options)
        notes.extend(inferred.notes)

        if inference.field_limit_reached:
            message = (
                f"Collection {collection_name!r} exceeded "
                f"max_fields_per_collection "
                f"({self._options.max_fields_per_collection}); "
                f"{inference.dropped_path_count} further observed path(s) were "
                "not recorded and the result is partial."
            )
            notes.append(message)
            self._warnings.append(message)

        if inference.depth_limit_reached:
            message = (
                f"Collection {collection_name!r} contains documents nested "
                f"deeper than max_depth ({self._options.max_depth}); the "
                "deepest levels were left unexpanded."
            )
            notes.append(message)
            self._warnings.append(message)

        estimated_count = self._estimate_document_count(handle, collection_name, notes)
        validator = self._validator_metadata(entry)

        summary = CollectionInferenceSummary(
            collection_name=collection_name,
            documents_sampled=inference.documents_sampled,
            field_path_count=len(inferred.fields),
            estimated_document_count=estimated_count,
            collection_type=str(entry.get("type") or "collection"),
            deterministic_sampling=deterministic,
            partial=inference.partial or budget_exhausted,
            field_limit_reached=inference.field_limit_reached,
            depth_limit_reached=inference.depth_limit_reached,
            sample_budget_exhausted=budget_exhausted,
            validator_present=validator.get("validator_present"),
            validation_level=validator.get("validation_level"),
            validation_action=validator.get("validation_action"),
            notes=tuple(notes),
            observations=inference.observations(),
        )
        self._collection_summaries.append(summary)

        normalized_name = _unique_entity_name(
            collection_name, used_entity_names, self._warnings
        )

        return SourceEntity(
            entity_id=self._build_entity_id(database, collection_name),
            source_name=collection_name,
            normalized_name=normalized_name,
            entity_kind=EntityKind.COLLECTION,
            # A MongoDB database is the namespace its collections live in,
            # the same role a schema plays for PostgreSQL.
            namespace=database,
            fields=inferred.fields,
            primary_key_fields=inferred.primary_key_fields,
            description=None,
            metadata=self._entity_metadata(summary, validator),
        )

    def _entity_metadata(
        self,
        summary: CollectionInferenceSummary,
        validator: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Per-collection evidence, kept out of the structural hash.

        ``SourceSchema.compute_schema_hash()`` ignores metadata, which is
        exactly what is wanted here: ``documents_sampled`` changes whenever
        the budget changes, and that is not a schema change.
        """
        metadata: dict[str, Any] = {
            "source_collection_name": summary.collection_name,
            "collection_type": summary.collection_type,
            "inference_method": "bounded_document_sample",
            "schema_claim": "observed",
            "observed_schema_note": _OBSERVED_SCHEMA_NOTE,
            "sample": {
                "documents_sampled": summary.documents_sampled,
                "max_documents_per_collection": (
                    self._options.max_documents_per_collection
                ),
                "deterministic_sampling": summary.deterministic_sampling,
                "sort_field": SAMPLE_SORT_FIELD if summary.deterministic_sampling else None,
                # Never claim a coverage percentage: an estimated count is an
                # estimate, and a sample is not a scan.
                "full_scan": False,
            },
            "estimated_document_count": summary.estimated_document_count,
            "observed_field_path_count": summary.field_path_count,
            "partial": summary.partial,
            "field_limit_reached": summary.field_limit_reached,
            "depth_limit_reached": summary.depth_limit_reached,
            "sample_budget_exhausted": summary.sample_budget_exhausted,
        }

        if validator:
            metadata.update(validator)

        if summary.notes:
            metadata["inference_notes"] = list(summary.notes)

        return metadata

    # ------------------------------------------------------------
    # Deterministic sampling (Step 15)
    # ------------------------------------------------------------

    def _sample_documents(
        self,
        handle,
        collection_name: str,
        limit: int,
        notes: list[str],
    ) -> tuple[tuple[Any, ...], bool]:
        """Read up to ``limit`` documents, reproducibly.

        Deterministic strategy: a stable ascending ``_id`` sort with a bounded
        limit. Random sampling is deliberately NOT used - ``$sample`` would
        make two inference runs over an unchanged collection disagree, which
        would produce a different observed structure, a different schema hash
        and a spurious new catalog version every time.

        Returns the documents and whether determinism actually held. If the
        sort is rejected (an unusual collection or a view that cannot sort),
        the read falls back to natural order, the caller is told, and the
        weaker guarantee is recorded in metadata rather than being quietly
        assumed.
        """
        collection = handle[collection_name]
        deterministic = self._options.deterministic_sampling

        if deterministic:
            try:
                return self._read(
                    collection.find(
                        filter={},
                        sort=[(SAMPLE_SORT_FIELD, 1)],
                        limit=limit,
                    )
                ), True
            except Exception as exc:
                notes.append(
                    f"Sorted sampling by {SAMPLE_SORT_FIELD!r} failed; fell back "
                    f"to natural order, so this sample is not reproducible: "
                    f"{redact_text(str(exc))}"
                )
                self._warnings.append(
                    f"Collection {collection_name!r} could not be sampled in "
                    f"{SAMPLE_SORT_FIELD} order; used natural order instead."
                )

        try:
            return self._read(collection.find(filter={}, limit=limit)), False
        except Exception as exc:
            raise MongoInferenceError(
                f"Could not sample documents from collection "
                f"{collection_name!r}: {redact_text(str(exc))}"
            ) from exc

    @staticmethod
    def _read(cursor: Iterable[Any]) -> tuple[Any, ...]:
        """Drain a cursor into memory.

        Bounded by the caller's ``limit`` before the query is issued, so this
        can never pull an unbounded collection into the process.
        """
        return tuple(cursor)

    def _remaining_document_budget(self) -> int:
        return max(self._options.max_total_documents - self._documents_sampled, 0)

    def _estimate_document_count(
        self, handle, collection_name: str, notes: list[str]
    ) -> int | None:
        """MongoDB's cheap metadata estimate, or ``None`` if unavailable.

        Reported only so that ``documents_sampled`` can be read in context.
        Never turned into a coverage claim, and never allowed to fail a run -
        the estimate is unavailable on some views and under some roles.
        """
        try:
            return int(handle[collection_name].estimated_document_count())
        except Exception as exc:
            notes.append(
                f"Document count estimate unavailable: {redact_text(str(exc))}"
            )
            return None

    # ------------------------------------------------------------
    # Collection validator metadata (Step 20)
    # ------------------------------------------------------------

    def _validator_metadata(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Report WHETHER a collection validator exists - nothing more.

        A validator is a real, declared schema, and parsing it would be a
        genuinely different feature from sampling documents. Phase 5 does not
        implement that, so it records only presence plus MongoDB's own
        enforcement settings, and never merges validator rules into the
        observed requiredness computed from the sample. The validator body
        itself is not stored: it can embed literal business values (allowed
        enum members, bounds), and this metadata is published to a catalog
        that must stay free of data content.
        """
        if not self._options.include_validator_presence:
            return {}

        options = entry.get("options")
        if not isinstance(options, Mapping):
            return {"validator_present": False}

        validator = options.get("validator")
        present = isinstance(validator, Mapping) and bool(validator)

        metadata: dict[str, Any] = {
            "validator_present": present,
            # Stated explicitly so no reader can mistake presence detection
            # for validator translation.
            "validator_parsed": False,
        }

        if present:
            level = options.get("validationLevel")
            action = options.get("validationAction")
            if level is not None:
                metadata["validation_level"] = str(level)
            if action is not None:
                metadata["validation_action"] = str(action)

        return metadata

    # ------------------------------------------------------------
    # Deterministic identity (Steps 5, 22)
    # ------------------------------------------------------------

    def _build_entity_id(self, database: str, collection_name: str) -> str:
        return normalize_identifier(
            f"{self._connector.source_system_id}.{database}.{collection_name}"
        )

    def _build_schema_name(self, database: str) -> str:
        """The STABLE logical scope Phase 2 versions snapshots within.

        Must not move when the observed CONTENT changes, or every new field
        would start a fresh version-1 history instead of incrementing the
        existing one. An explicit ``include_collections`` narrows the scope and
        therefore belongs in the name - two different collection subsets of one
        database are two different things to track.
        """
        if self._options.include_collections is not None:
            return ",".join(sorted(str(name) for name in self._options.include_collections))

        return database

    def _build_schema_id(
        self, database: str, schema_name: str, structural_hash: str
    ) -> str:
        """Deterministic, content-addressed SNAPSHOT identity.

        Identical to the relational rule, and for the same reason:

            unchanged observed structure -> identical id -> still version 1
            changed observed structure   -> new id       -> version N+1

        Nothing here uses a timestamp, a random UUID, or the order collections
        or documents were iterated in.
        """
        return normalize_identifier(
            f"{self._connector.source_system_id}.{database}.{schema_name}."
            f"{structural_hash[:12]}"
        )

    # ------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------

    def _assemble(
        self,
        schema_id: str,
        schema_name: str,
        database: str,
        entities: Sequence[SourceEntity],
        schema_hash: str | None,
    ) -> SourceSchema:
        metadata: dict[str, Any] = {
            "database": database,
            "engine": SourceType.MONGODB.value,
            "inference_method": "bounded_document_sample",
            "schema_claim": "observed",
            "observed_schema_note": _OBSERVED_SCHEMA_NOTE,
            "inference_options": self._options.to_dict(),
            "collections_discovered": self._collections_discovered,
            "collections_inferred": len(entities),
            "documents_sampled": self._documents_sampled,
            "sample_budget_exhausted": self._budget_exhausted,
            # Stated as data, not just documented: MongoDB enforces no
            # cross-collection foreign keys, so none are invented here.
            "relationship_inference": "disabled",
            "relationship_inference_reason": (
                "MongoDB enforces no cross-collection foreign keys. A field "
                "named customer_id, or holding an ObjectId, is not evidence of "
                "a relationship, so Phase 5 infers none."
            ),
        }

        if self._warnings:
            metadata["inference_warnings"] = list(self._warnings)

        return SourceSchema(
            schema_id=schema_id,
            source_system_id=self._connector.source_system_id,
            schema_name=schema_name,
            # INFERRED, never DISCOVERED: this came from documents, not from a
            # declared catalog.
            origin=SchemaOrigin.INFERRED,
            entities=tuple(entities),
            # Always empty. Embedded structure is represented by nested fields
            # on the owning entity, which is truthful and needs no invented
            # second entity.
            relationships=(),
            schema_hash=schema_hash,
            metadata=metadata,
        )


# ============================================================
# Module-level helpers
# ============================================================

def _require_mongodb_connector(connector: Any) -> BaseSourceConnector:
    if not isinstance(connector, BaseSourceConnector):
        raise UnsupportedDiscoverySourceError(
            f"MongoDB inference requires a source connector, got "
            f"{type(connector).__name__}."
        )

    if connector.source_type not in SUPPORTED_SOURCE_TYPES:
        raise UnsupportedDiscoverySourceError(
            f"MongoDB observed-schema inference supports "
            f"{SourceType.MONGODB.value!r}, but source_system_id="
            f"{connector.source_system_id!r} is "
            f"{connector.source_type.value!r}. Relational sources are Phase 4: "
            "use erp_pipeline.discovery.discover_schema instead."
        )

    if not hasattr(connector, "create_database_handle"):
        raise UnsupportedDiscoverySourceError(
            f"{type(connector).__name__} does not expose "
            "create_database_handle(); MongoDB inference needs a "
            "document-store connector."
        )

    return connector


def _unique_entity_name(
    collection_name: str, used_names: dict[str, int], warnings: list[str]
) -> str:
    """Normalize a collection name to a unique entity name.

    MongoDB collection names are case-sensitive, so ``Orders`` and ``orders``
    can both exist in one database and both normalize to ``orders``. Phase 1
    requires entity names to be unique within a schema, so the collision is
    resolved deterministically - collections are processed in sorted order,
    the first claims the plain name - rather than being allowed to fail the
    run.
    """
    try:
        base = normalize_identifier(collection_name)
    except IdentityError:
        base = f"collection.{hash_json_payload(collection_name)[:12]}"
        warnings.append(
            f"Collection name {collection_name!r} contains no characters usable "
            f"in a normalized name; recorded as {base!r}."
        )

    unique = deduplicate_normalized_name(base, used_names)

    if unique != base:
        warnings.append(
            f"Collection {collection_name!r} normalizes to {base!r}, which is "
            f"already used in this database; recorded as {unique!r}."
        )

    return unique


def infer_mongodb_schema(
    connector: BaseSourceConnector, options: MongoInferenceOptions | None = None
) -> SourceSchema:
    """Convenience wrapper: infer and return the observed ``SourceSchema``."""
    return MongoDBSchemaInference(connector, options).infer()


__all__ = [
    "SUPPORTED_SOURCE_TYPES",
    "SAMPLE_SORT_FIELD",
    "MongoDBSchemaInference",
    "infer_mongodb_schema",
]
