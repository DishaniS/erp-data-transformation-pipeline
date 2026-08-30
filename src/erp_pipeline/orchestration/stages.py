"""One handler per stage. Each delegates to the phase that owns the work.

READ THIS AS A TABLE OF DELEGATIONS
-----------------------------------
    DISCOVER   -> Phase 4 RelationalDiscoveryService / Phase 5 MongoDBInferenceService
    MAP        -> Phase 8 MappingService
    EXTRACT    -> Phase 13's bounded snapshot reader (Phase 3 stays no-execute)
    TRANSFORM  -> Phase 9 TransformationService
    VALIDATE   -> reports Phase 9's validation outcome; does NOT re-validate
    LOAD       -> Phase 10 CanonicalRecordStore contract
    AI_BUILD   -> Phase 11 representation / chunking
    EMBED      -> Phase 11 EmbeddingService
    TIER_ROUTE -> Phase 12 StorageService
    DRIFT      -> Phase 10 SyncService
    INGEST     -> Phase 6 FileIngestionService
    PARSE_SPEC -> Phase 7 ApiSpecificationService

If a handler grows logic of its own, the phase boundary has been broken.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from erp_pipeline.ingestion.remote_assets import declared_asset_fields
from erp_pipeline.schemas.sensitivity import (
    job_sensitivity,
    resolve as resolve_sensitivity,
)
from erp_pipeline.orchestration.document_identity import DocumentIdentity
from erp_pipeline.orchestration.errors import (
    InvalidPipelineRequestError,
    MappingNotExecutableError,
    SourceNativeNotPermittedError,
    UnsupportedCapabilityError,
)
from erp_pipeline.orchestration.extraction import (
    CsvSnapshotExtractor,
    ExtractionRequest,
    RelationalSnapshotExtractor,
    resolve_entity,
)
from erp_pipeline.ingestion.binary_assets import binary_field_names_for_entity as binary_field_names
from erp_pipeline.orchestration.models import JobType, PipelineStage
from erp_pipeline.orchestration.pipeline import PipelineContext, StageFailure
from erp_pipeline.schemas.enums import SourceType

LOGGER = logging.getLogger("erp_pipeline.orchestration.stages")


# ----------------------------------------------------------------------
# DISCOVER - Phase 4 / Phase 5
# ----------------------------------------------------------------------


def run_discover(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services
    source = services.sources.get(context.job.request.source_id)

    schema = services.discover_schema(source)
    context.schema = schema
    context.outputs["schema_id"] = schema.schema_id

    return {
        "schema_id": schema.schema_id,
        "entity_count": len(schema.entities),
        "field_count": sum(len(entity.fields) for entity in schema.entities),
    }


# ----------------------------------------------------------------------
# MAP - Phase 8
# ----------------------------------------------------------------------


def run_map(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services
    request = context.job.request

    if context.schema is None:
        if not request.schema_id:
            raise InvalidPipelineRequestError(
                "the job has no schema to map; supply schema_id or run discovery"
            )

        context.schema = services.get_schema(request.schema_id)

    # A mapping the caller already approved takes precedence: re-generating it
    # would discard their decisions.
    if request.mapping_id:
        profile = services.get_mapping_profile(request.mapping_id)
        context.mapping_profile = profile
        context.outputs["mapping_id"] = request.mapping_id

        return {"mapping_id": request.mapping_id, "source": "supplied"}

    result = services.mapping.generate(context.schema)
    context.mapping_result = result

    coverage = result.coverage
    review_required = getattr(coverage, "review_required_fields", 0)
    ambiguous = getattr(coverage, "ambiguous_fields", 0)

    if not result.profiles:
        raise StageFailure(
            "the mapping engine produced no executable profile",
            code="MAPPING_EMPTY",
        )

    profile = result.profiles[0]
    context.mapping_profile = profile
    context.outputs["mapping_id"] = getattr(profile, "mapping_id", None)

    # Ambiguity is surfaced, never silently accepted. Phase 8 decided these
    # fields need a human; Phase 13 does not overrule that.
    if ambiguous:
        context.partial_reasons.append(
            f"{ambiguous} field(s) were ambiguous and were not auto-approved"
        )
        context.note(
            f"{ambiguous} ambiguous field(s) require review before this mapping "
            "should be trusted for production data"
        )

    return {
        "mapping_id": getattr(profile, "mapping_id", None),
        "mapped_fields": getattr(coverage, "mapped_fields", None),
        "total_fields": getattr(coverage, "total_fields", None),
        "ambiguous_fields": ambiguous,
        "review_required_fields": review_required,
        "unmapped_fields": getattr(coverage, "unmapped_fields", None),
    }


# ----------------------------------------------------------------------
# SOURCE_NATIVE_GUARD - the admission decision (Phase 2)
# ----------------------------------------------------------------------


def run_source_native_guard(context: PipelineContext) -> Mapping[str, Any]:
    """Decide whether this entity is ALLOWED to be indexed source-natively.

    THIS STAGE EXISTS TO SAY NO.

    Without it, a caller facing an ambiguous mapping - the engine having
    correctly decided a human must choose between ``invoice.customer_id`` and
    ``customer.customer_id`` - could simply re-submit the job as source-native
    and index the data anyway. That would route around the refusal mechanism
    which is the whole reason the mapping engine is trustworthy, and it would do
    so silently.

    So admission is decided by ``MappingResult.unmatched_entities``, whose own
    contract is "source entities no canonical entity could be matched to". That
    is a statement about VOCABULARY COVERAGE, and it is deliberately not the
    same question as "did mapping succeed":

        entity matched, fields ambiguous   -> REFUSED, resolve the mapping
        entity matched, mapping fine       -> REFUSED, use structured_pipeline
        entity matched to nothing          -> admitted

    A caller who genuinely has an uncovered entity is unaffected. A caller
    trying to dodge review is stopped, and told exactly what to do instead.
    """
    services = context.services
    request = context.job.request

    if context.schema is None:
        if not request.schema_id:
            raise InvalidPipelineRequestError(
                "a source-native job needs a schema; supply schema_id or run "
                "discovery"
            )

        context.schema = services.get_schema(request.schema_id)

    entity = resolve_entity(context.schema, request.entity)

    if services.mapping is None:
        raise InvalidPipelineRequestError(
            "the mapping engine is required to decide whether this entity is "
            "outside the canonical vocabulary; source-native indexing cannot "
            "be authorised without it"
        )

    result = services.mapping.generate(context.schema, validate=False)
    context.mapping_result = result

    unmatched = set(getattr(result, "unmatched_entities", ()) or ())
    covered = entity.normalized_name not in unmatched and entity.source_name not in unmatched

    if covered:
        profile = result.profile_for(entity.source_name)
        coverage = result.coverage
        ambiguous = getattr(coverage, "ambiguous_fields", 0)

        raise SourceNativeNotPermittedError(
            f"{entity.source_name!r} matches a canonical entity, so it must go "
            "through the canonical mapping path. Source-native indexing is for "
            "entities the canonical model does not cover, and using it here "
            "would bypass a mapping decision rather than make one."
            + (
                f" {ambiguous} field(s) are currently ambiguous and need a "
                "human decision; resolve them with PUT /v1/mappings/{id} and "
                "run a structured_pipeline job."
                if ambiguous
                else " Run a structured_pipeline job instead."
            ),
            entity=entity.source_name,
            ambiguous_fields=ambiguous,
            mapping_id=getattr(profile, "mapping_id", None),
        )

    context.outputs["source_native_entity"] = entity.source_name

    return {
        "entity": entity.source_name,
        "admitted": True,
        "reason": "no canonical entity claims this source entity",
        "canonical_model": getattr(result, "canonical_model_identity", None),
        "unmatched_entities": sorted(unmatched),
    }


# ----------------------------------------------------------------------
# EXTRACT
# ----------------------------------------------------------------------


def run_extract(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services
    request = context.job.request

    if context.schema is None and request.schema_id:
        context.schema = services.get_schema(request.schema_id)

    if context.schema is None:
        raise InvalidPipelineRequestError("extraction needs a discovered schema")

    entity = resolve_entity(context.schema, request.entity)
    limit = int(request.options.get("limit", 500))
    source_type = context.plan.source_type
    key_fields = request.options.get("key_fields") or ()

    if isinstance(key_fields, str):
        key_fields = (key_fields,)
    else:
        key_fields = tuple(key_fields)

    if source_type is SourceType.CSV:
        records = services.extract_csv_records(request.upload_id, entity, limit)
    else:
        source = services.sources.get(request.source_id)
        records = services.extract_snapshot(
            source,
            ExtractionRequest(
                context.schema,
                entity,
                limit,
                key_fields=key_fields,
            ),
        )

    context.source_records = tuple(records)
    context.counters = context.counters.merged(records_read=len(context.source_records))

    if not context.source_records:
        context.note("the source returned no records for this entity")

    return {
        "entity": entity.source_name,
        "records_read": len(context.source_records),
    }


# ----------------------------------------------------------------------
# TRANSFORM - Phase 9
# ----------------------------------------------------------------------


def run_transform(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services

    if context.job.request.job_type is JobType.SOURCE_NATIVE_PIPELINE:
        return _run_source_native_transform(context)

    if context.mapping_profile is None:
        raise MappingNotExecutableError(
            "no approved mapping profile is available, so no record may be "
            "transformed through it"
        )

    summary = services.transform(
        context.source_records,
        context.mapping_profile,
        context.schema,
        source_type=context.plan.source_type,
    )

    # Phase 9 returns the CanonicalRecords themselves, not result wrappers.
    context.canonical_records = tuple(summary.successful_records)

    rejected = len(summary.rejected_records)
    skipped = len(summary.skipped_records)

    context.counters = context.counters.merged(
        records_transformed=len(context.canonical_records),
        records_failed=rejected,
        records_skipped=skipped,
    )

    # Phase 9 owns the threshold decision; Phase 13 only reports it.
    if summary.threshold_exceeded:
        raise StageFailure(
            "the configured data-quality threshold was exceeded, so the run "
            "was stopped rather than loading partial data",
            code="QUALITY_THRESHOLD_EXCEEDED",
        )

    if rejected:
        context.partial_reasons.append(f"{rejected} record(s) were rejected")

    return {
        "records_transformed": len(context.canonical_records),
        "records_rejected": rejected,
        "records_skipped": skipped,
        "duration_seconds": summary.duration_seconds,
    }


def _run_source_native_transform(context: PipelineContext) -> Mapping[str, Any]:
    """Transform an admitted uncovered entity under its own field names.

    Reached only after ``run_source_native_guard`` allowed the job, so there is
    no path from an ambiguous canonical mapping to here.
    """
    services = context.services
    request = context.job.request
    entity = resolve_entity(context.schema, request.entity)

    # An explicit caller decision outranks anything inferred. A CSV declares no
    # primary key - the ingestion layer refuses to invent one - so for uploaded
    # files the caller must SAY which column identifies a record. A database
    # source usually declares its key and needs nothing here.
    key_fields = request.options.get("key_fields") or None

    if isinstance(key_fields, str):
        key_fields = [key_fields]

    result = services.transform_source_native(
        context.source_records,
        entity,
        context.schema,
        source_type=context.plan.source_type,
        source_id=request.source_id,
        key_fields=key_fields,
        # Declared asset URLs are pointers, not scalar content.
        asset_url_fields=tuple(declared_asset_fields(request.options)),
        # Phase 10: a job-wide declaration, applied to every record it builds.
        sensitivity=resolve_sensitivity(job=job_sensitivity(request.options)),
    )

    context.canonical_records = tuple(result.records)
    context.counters = context.counters.merged(
        records_transformed=len(context.canonical_records),
        records_failed=len(result.rejected),
    )

    if result.rejected:
        context.partial_reasons.append(
            f"{len(result.rejected)} record(s) had no stable identity and were "
            "not indexed"
        )
        for note in result.rejected[:5]:
            context.note(note)

    if result.binary_fields_omitted:
        # Stated rather than assumed: a caller must not think a birth
        # certificate was read just because the row carrying it was indexed.
        context.note(
            "binary field(s) "
            f"{list(result.binary_fields_omitted)} were recorded but not "
            "opened; extracting their content is not part of this pipeline"
        )

    return {
        "records_transformed": len(context.canonical_records),
        "records_rejected": len(result.rejected),
        "binary_fields_omitted": list(result.binary_fields_omitted),
        "mode": "source_native",
    }


# ----------------------------------------------------------------------
# VALIDATE - reports Phase 9's outcome, does not re-validate
# ----------------------------------------------------------------------


def run_validate(context: PipelineContext) -> Mapping[str, Any]:
    """Phase 9 already validated during transformation.

    This stage exists because the public job contract lists VALIDATE as a
    distinct step, and an operator wants to see quality separately from
    conversion. It interprets the outcome Phase 9 produced. Running a second
    validator here would be a duplicate implementation and could disagree with
    the one that actually gated the data.
    """
    counters = context.counters
    failed = counters.records_failed or 0
    transformed = counters.records_transformed or 0
    total = failed + transformed

    return {
        "validated_by": "phase_9_transformation_validation",
        "records_passed": transformed,
        "records_rejected": failed,
        "rejection_rate": round(failed / total, 6) if total else 0.0,
        "note": (
            "quality was decided by the Phase 9 validation profile during "
            "transformation; this stage reports that outcome and does not "
            "re-run validation"
        ),
    }


# ----------------------------------------------------------------------
# LOAD - Phase 10's CanonicalRecordStore contract
# ----------------------------------------------------------------------


def run_load(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services
    stored = 0

    for record in context.canonical_records:
        services.records.upsert(record)
        stored += 1

    context.outputs["records_loaded"] = stored

    return {"records_loaded": stored}


# ----------------------------------------------------------------------
# AI_BUILD - Phase 11
# ----------------------------------------------------------------------


#: How far past an entity's current representation count to look for stale
#: field groups left by a wider previous version. A table shedding more than
#: this many field groups in one revision is rare enough that the leftovers are
#: better handled by an explicit re-index than by an unbounded scan on every
#: schema job.
_PRUNE_LOOKAHEAD = 8


def run_ai_build(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services

    if context.job.request.job_type is JobType.SCHEMA_PIPELINE:
        return _build_schema_representations(context)

    if context.document is not None:
        # Declared by the upload, never inferred from the filename.
        identity = DocumentIdentity.from_options(context.job.request.options)
        representations = services.build_document_representations(
            context.document, identity
        )
        context.representations = tuple(representations)
        context.counters = context.counters.merged(
            chunks_built=len(context.representations),
            representations_built=len(context.representations),
        )

        return {"chunks_built": len(context.representations)}

    representations = services.build_representations(context.canonical_records)
    context.representations = tuple(representations)
    context.counters = context.counters.merged(
        representations_built=len(context.representations)
    )

    return {"representations_built": len(context.representations)}


# ----------------------------------------------------------------------
# MULTIMODAL_EXTRACT - database BLOBs as documents (Phase 3)
# ----------------------------------------------------------------------


def run_multimodal_extract(context: PipelineContext) -> Mapping[str, Any]:
    """Open the binary fields the source rows carried.

    Runs AFTER ``AI_BUILD`` for two concrete reasons:

    * ``AI_BUILD`` ASSIGNS ``context.representations``. Producing document
      representations before it would have them overwritten.
    * the parent record ids only exist once ``TRANSFORM`` has run, and a
      document with no stable parent is a vector nobody can trace back.

    Both the raw ``source_records`` and the transformed ``canonical_records``
    are still on the context at this point, which is the only place in the
    pipeline where that is true.

    A row with no binary column costs one dictionary lookup and returns
    immediately, so the stage is close to free for ordinary ERP tables.
    """
    services = context.services

    if not context.source_records or context.schema is None:
        return {"binary_fields_seen": 0, "note": "no records to inspect"}

    entity = resolve_entity(context.schema, context.job.request.entity)
    binary_fields = binary_field_names(entity)
    # Phase 8: fields a caller explicitly declared as remote references. Never
    # inferred from a column name, and never from a value that looks like a URL.
    asset_url_fields = declared_asset_fields(context.job.request.options)

    if not binary_fields and not asset_url_fields:
        return {
            "binary_fields_seen": 0,
            "note": (
                "this entity declares no binary fields and the job declared no "
                "remote asset fields"
            ),
        }

    if services.embedding is None:
        context.note(
            "binary fields were found but no embedding service is configured, "
            "so their documents were not indexed"
        )

    result = services.extract_binary_assets(
        context.source_records,
        context.canonical_records,
        entity,
        binary_fields,
        asset_url_fields=asset_url_fields,
        field_sensitivity=(context.job.request.options or {}).get(
            "field_sensitivity"
        ),
        job_sensitivity=job_sensitivity(context.job.request.options),
    )

    # APPENDED, never assigned - the scalar representations AI_BUILD produced
    # must survive alongside the document ones.
    context.representations = tuple(context.representations) + result.representations

    for warning in result.warnings[:10]:
        context.note(warning)

    if result.skipped:
        context.partial_reasons.append(
            f"{result.skipped} binary asset(s) could not be indexed"
        )

    context.counters = context.counters.merged(
        binary_fields_seen=result.fields_seen,
        binary_assets_extracted=result.extracted,
        binary_assets_skipped=result.skipped,
        ocr_assets=result.ocr_assets,
        remote_assets_attempted=getattr(result, "remote_assets", 0),
        documents_ingested=result.extracted,
        chunks_built=len(result.representations),
        representations_built=(context.counters.representations_built or 0)
        + len(result.representations),
    )

    return {
        "binary_fields_seen": result.fields_seen,
        "binary_assets_extracted": result.extracted,
        "binary_assets_skipped": result.skipped,
        "ocr_assets": result.ocr_assets,
        "remote_assets_attempted": getattr(result, "remote_assets", 0),
        "document_chunks_built": len(result.representations),
        # Per-asset outcomes, with no bytes and no extracted text.
        "assets": [item.to_dict() for item in result.assets[:25]],
    }


# ----------------------------------------------------------------------
# EMBED - Phase 11
# ----------------------------------------------------------------------


def _build_schema_representations(context: PipelineContext) -> Mapping[str, Any]:
    """Turn a catalogued schema into searchable structure.

    Also PRUNES. A table that needed four field groups and now needs two leaves
    two representations behind describing columns that no longer exist. Left
    alone they would keep answering questions about a schema that changed,
    which is worse than not indexing the table at all - a stale answer is
    indistinguishable from a current one.
    """
    from erp_pipeline.ai.schema_representation import (
        representation_ids_for_entity,
        source_entity_to_representations,
    )

    services = context.services
    schema = services.get_schema(context.job.request.schema_id)
    context.schema = schema

    wanted = context.job.request.entity
    entities = [
        entity
        for entity in getattr(schema, "entities", ()) or ()
        if wanted is None
        or wanted in (entity.source_name, entity.normalized_name, entity.entity_id)
    ]

    if wanted and not entities:
        raise StageFailure(
            f"entity {wanted!r} is not part of schema "
            f"{context.job.request.schema_id!r}",
            code="ENTITY_NOT_IN_SCHEMA",
        )

    built: list[Any] = []
    pruned = 0
    store = getattr(services, "representations", None)

    for entity in entities:
        representations = source_entity_to_representations(
            schema, entity, None,
            resolve_sensitivity(job=job_sensitivity(context.job.request.options)),
        )
        built.extend(representations)

        if store is None:
            continue

        # Anything this entity used to occupy beyond what it now needs.
        for index in range(
            len(representations), len(representations) + _PRUNE_LOOKAHEAD
        ):
            stale = representation_ids_for_entity(entity.entity_id, index + 1)[-1]

            if store.get(stale) is not None and store.delete(stale):
                pruned += 1

    context.representations = tuple(built)
    context.counters = context.counters.merged(
        representations_built=len(built),
        schema_entities_indexed=len(entities),
        schema_representations_pruned=pruned,
    )

    return {
        "schema_id": getattr(schema, "schema_id", None),
        "schema_entities_indexed": len(entities),
        "representations_built": len(built),
        "schema_representations_pruned": pruned,
    }


def run_persist_representations(context: PipelineContext) -> Mapping[str, Any]:
    """Write the AI text to durable storage BEFORE anything is embedded.

    The ordering is the whole point. ``TIER_ROUTE`` is what makes a vector
    searchable, so persisting anywhere after it would leave a window in which a
    search could return a hit nobody can resolve - which is the exact defect
    Phase 5 exists to close. Persisting first inverts the failure: if this
    stage succeeds and a later one fails, the corpus holds text with no vector,
    which returns no wrong answers and is repaired by re-running the job.

    NOT ATOMIC ACROSS STORES. PostgreSQL and Qdrant are two systems and this
    pipeline has no distributed transaction. What is guaranteed is the ORDER,
    and the direction of the failure window that follows from it.

    A deployment with no representation store configured runs exactly as it did
    before Phase 5: the stage records that it stored nothing and says why,
    rather than failing a job that was previously valid.
    """
    services = context.services
    store = getattr(services, "representations", None)

    if store is None:
        context.note(
            "no representation store is configured, so the AI text for these "
            "vectors will not be resolvable through the representation API"
        )
        return {
            "representations_persisted": 0,
            "note": "no representation store is configured",
        }

    if not context.representations:
        return {"representations_persisted": 0, "note": "nothing to persist"}

    persisted = store.upsert_many(context.representations)
    context.counters = context.counters.merged(
        representations_persisted=int(persisted or 0)
    )

    return {"representations_persisted": int(persisted or 0)}


def run_embed(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services
    summary = services.embed(context.representations)

    context.embeddings = tuple(summary.records)
    generated = sum(
        1 for record in context.embeddings if getattr(record, "vector", None)
    )
    skipped = len(context.embeddings) - generated

    context.counters = context.counters.merged(
        embeddings_generated=generated, embeddings_skipped=skipped
    )

    return {
        "embeddings_generated": generated,
        "embeddings_skipped": skipped,
        "model_id": services.embedding_model_id,
    }


# ----------------------------------------------------------------------
# TIER_ROUTE / TIER_UPDATE - Phase 12
# ----------------------------------------------------------------------


def run_tier_route(context: PipelineContext) -> Mapping[str, Any]:
    """Hand each embedding to Phase 12 and let IT choose the tier.

    Orchestration passes routing metadata (sensitivity, criticality, latency
    need) and never names HOT, WARM or COLD. Choosing here would fork the
    routing policy that Phase 12 exists to own.
    """
    services = context.services
    stored = 0
    failed = 0
    tiers: dict[str, int] = {}

    for record in context.embeddings:
        if not getattr(record, "vector", None):
            continue

        try:
            metadata, decision = services.store_vector(record)
        except Exception:  # noqa: BLE001 - one bad vector must not kill the job
            failed += 1
            continue

        stored += 1
        tier = metadata.current_tier.value
        tiers[tier] = tiers.get(tier, 0) + 1

    context.counters = context.counters.merged(
        vectors_stored=stored, vectors_failed=failed
    )

    if failed:
        context.partial_reasons.append(f"{failed} vector(s) failed to store")

    return {"vectors_stored": stored, "vectors_failed": failed, "tiers": tiers}


# ----------------------------------------------------------------------
# INGEST - Phase 6
# ----------------------------------------------------------------------


def run_ingest(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services
    request = context.job.request

    if not request.upload_id:
        raise InvalidPipelineRequestError("a document job needs an upload_id")

    result = services.ingest_upload(request.upload_id)
    context.document = result
    context.counters = context.counters.merged(documents_ingested=1)

    document = getattr(result, "document", result)

    return {
        "upload_id": request.upload_id,
        "status": str(getattr(result, "status", "")),
        "page_count": len(getattr(document, "pages", ()) or ()),
    }


# ----------------------------------------------------------------------
# DRIFT_CHECK / EXTRACT_CHANGED - Phase 10
# ----------------------------------------------------------------------


def run_drift_check(context: PipelineContext) -> Mapping[str, Any]:
    """Report Phase 10's real DriftReport. No diff is computed here."""
    services = context.services
    report = services.check_drift(context.job.request)

    if report is None:
        return {
            "drift_detected": False,
            "note": "no previous schema snapshot exists to compare against",
        }

    status = getattr(report, "status", None)
    status_value = getattr(status, "value", str(status or ""))
    findings = tuple(getattr(report, "findings", ()) or ())
    blocked = status_value in {"blocked", "review_required"}

    if blocked:
        context.partial_reasons.append(
            f"schema drift status is {status_value}; the mapping needs review"
        )
        context.note(
            f"Phase 10 reported drift status {status_value!r} with "
            f"{len(findings)} finding(s)"
        )

    context.outputs["drift_status"] = status_value

    return {
        "drift_status": status_value,
        "drift_detected": status_value != "no_drift",
        "blocked": blocked,
        "severity": str(getattr(getattr(report, "severity", None), "value", "")),
        "finding_count": len(findings),
        # Field names and change kinds only - never a business value.
        "findings": [
            {
                "field": str(getattr(f, "field_name", "") or getattr(f, "path", "")),
                "type": str(getattr(getattr(f, "drift_type", None), "value", "")),
                "severity": str(getattr(getattr(f, "severity", None), "value", "")),
            }
            for f in findings[:50]
        ],
        "old_schema_id": getattr(report, "old_schema_id", None),
        "new_schema_id": getattr(report, "new_schema_id", None),
        "computed_by": "phase_10_detect_drift",
    }


def run_extract_changed(context: PipelineContext) -> Mapping[str, Any]:
    """Run Phase 10's incremental engine and report its own counters.

    Phase 10 performs the whole propagation - extract, transform, load,
    rebuild, re-embed, re-store - inside one run. The later stages of this
    plan therefore REPORT that run rather than repeating it; repeating it
    would double-write every record.
    """
    services = context.services
    summary = services.run_incremental(context.job.request)

    context.sync_summary = summary
    context.outputs["sync_run_id"] = getattr(summary, "run_id", None)

    context.counters = context.counters.merged(
        records_read=getattr(summary, "changes_read", None),
        records_transformed=getattr(summary, "changes_processed", None),
        records_failed=getattr(summary, "changes_failed", None),
        records_skipped=getattr(summary, "changes_skipped", None),
        representations_built=getattr(summary, "representations_rebuilt", None),
        embeddings_generated=getattr(summary, "embeddings_generated", None),
        embeddings_skipped=getattr(summary, "embeddings_skipped", None),
        vectors_stored=getattr(summary, "vectors_upserted", None),
    )

    if getattr(summary, "changes_failed", 0):
        context.partial_reasons.append(
            f"{summary.changes_failed} change(s) failed and were quarantined"
        )

    return {
        "changes_read": getattr(summary, "changes_read", None),
        "changes_processed": getattr(summary, "changes_processed", None),
        "changes_failed": getattr(summary, "changes_failed", None),
        "canonical_upserts": getattr(summary, "canonical_upserts", None),
        "canonical_deletes": getattr(summary, "canonical_deletes", None),
        "representations_changed": getattr(summary, "representations_changed", None),
        "embeddings_generated": getattr(summary, "embeddings_generated", None),
        "vectors_upserted": getattr(summary, "vectors_upserted", None),
        "checkpoint_advanced": getattr(summary, "checkpoint_advanced", None),
        "watermark_after": str(getattr(summary, "watermark_after", "") or "")[:200],
        "status": str(getattr(getattr(summary, "status", None), "value", "")),
        "executed_by": "phase_10_sync_service",
    }


def run_incremental_passthrough(context: PipelineContext) -> Mapping[str, Any]:
    """A no-op stage that reports what Phase 10 already did.

    TRANSFORM, VALIDATE, LOAD, AI_BUILD and EMBED appear in the incremental
    plan because operators expect to see them, but Phase 10 performed all of
    them inside its own run. Executing them again here would reprocess every
    change and write each record twice.
    """
    summary = getattr(context, "sync_summary", None)

    if summary is None:
        return {"note": "no incremental run produced results for this stage"}

    return {
        "performed_by": "phase_10_sync_service",
        "note": (
            "this work was completed inside the Phase 10 incremental run; "
            "the stage reports it rather than repeating it"
        ),
        "canonical_upserts": getattr(summary, "canonical_upserts", None),
        "representations_rebuilt": getattr(summary, "representations_rebuilt", None),
        "embeddings_generated": getattr(summary, "embeddings_generated", None),
        "vectors_upserted": getattr(summary, "vectors_upserted", None),
    }


# ----------------------------------------------------------------------
# PARSE_SPEC / SCHEMA - Phase 7
# ----------------------------------------------------------------------


def run_parse_spec(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services
    request = context.job.request

    if not request.upload_id:
        raise InvalidPipelineRequestError("an API-spec job needs an upload_id")

    result = services.parse_api_spec(request.upload_id)
    operations = getattr(result, "operations", ()) or ()

    context.schema = getattr(result, "schema", None)
    context.counters = context.counters.merged(operations_parsed=len(operations))

    return {
        "operations_parsed": len(operations),
        "endpoints_called": 0,
        "note": (
            "the specification was parsed as a contract; none of the "
            "documented endpoints were called"
        ),
    }


def run_schema_stage(context: PipelineContext) -> Mapping[str, Any]:
    if context.schema is None:
        raise StageFailure(
            "the specification produced no schema", code="SPEC_SCHEMA_MISSING"
        )

    context.outputs["schema_id"] = context.schema.schema_id

    return {
        "schema_id": context.schema.schema_id,
        "entity_count": len(context.schema.entities),
    }


#: The incremental plan reuses these stage names, but Phase 10 already did the
#: work, so they report instead of re-executing.
INCREMENTAL_HANDLERS = {
    PipelineStage.DRIFT_CHECK: run_drift_check,
    PipelineStage.EXTRACT_CHANGED: run_extract_changed,
    PipelineStage.TRANSFORM: run_incremental_passthrough,
    PipelineStage.VALIDATE: run_incremental_passthrough,
    PipelineStage.LOAD: run_incremental_passthrough,
    PipelineStage.AI_BUILD: run_incremental_passthrough,
    PipelineStage.PERSIST_REPRESENTATIONS: run_incremental_passthrough,
    PipelineStage.EMBED: run_incremental_passthrough,
    PipelineStage.TIER_UPDATE: run_incremental_passthrough,
}


DEFAULT_HANDLERS = {
    PipelineStage.DISCOVER: run_discover,
    PipelineStage.MAP: run_map,
    PipelineStage.SOURCE_NATIVE_GUARD: run_source_native_guard,
    PipelineStage.EXTRACT: run_extract,
    PipelineStage.TRANSFORM: run_transform,
    PipelineStage.VALIDATE: run_validate,
    PipelineStage.LOAD: run_load,
    PipelineStage.AI_BUILD: run_ai_build,
    PipelineStage.MULTIMODAL_EXTRACT: run_multimodal_extract,
    PipelineStage.PERSIST_REPRESENTATIONS: run_persist_representations,
    PipelineStage.EMBED: run_embed,
    PipelineStage.TIER_ROUTE: run_tier_route,
    PipelineStage.TIER_UPDATE: run_tier_route,
    PipelineStage.INGEST: run_ingest,
    PipelineStage.DRIFT_CHECK: run_drift_check,
    PipelineStage.EXTRACT_CHANGED: run_extract_changed,
    PipelineStage.PARSE_SPEC: run_parse_spec,
    PipelineStage.SCHEMA: run_schema_stage,
}


__all__ = ["DEFAULT_HANDLERS", "INCREMENTAL_HANDLERS"]


def run_lifecycle_commit(context: PipelineContext) -> Mapping[str, Any]:
    """Make this run's representations the current version of their ERP slots.

    ORDER IS THE WHOLE POINT. This runs after PERSIST, EMBED and TIER_ROUTE
    have all succeeded, so the sequence is:

        build B -> persist B -> embed B -> store B -> promote B -> supersede A

    never

        delete A -> build B -> B fails -> nothing searchable

    A failure at any earlier stage means this never runs, and A stays current.
    That is the invariant: a failed replacement must not destroy the last
    version that worked.

    Superseding marks state and registry FIRST and deletes the physical vector
    afterwards. If the delete fails, the vector is already excluded from search
    by ``is_current`` and is recorded for reconciliation - a cleanup backlog
    rather than a wrong answer.
    """
    from erp_pipeline.orchestration.lifecycle import (
        content_generation,
        group_by_slot,
    )

    services = context.services
    registry = getattr(services, "lifecycle", None)

    if registry is None or not context.representations:
        return {"slots_promoted": 0, "note": "no lifecycle registry configured"}

    grouped = group_by_slot(context.representations)

    if not grouped:
        # Nothing here occupies a managed slot - anonymous uploads, say.
        return {"slots_promoted": 0, "note": "no representations occupy a slot"}

    promoted = superseded = removed = deferred = 0

    for logical_key, members in grouped.items():
        generation = content_generation(members)
        result = registry.replace_current(
            logical_key,
            [item.representation_id for item in members],
            generation,
            sync_run_id=context.job.job_id,
        )

        if result.unchanged:
            continue

        promoted += 1
        _mark_current(context, [item.representation_id for item in members], True,
                      logical_key)

        for stale in result.superseded:
            superseded += 1
            # State first: search must stop returning it even if the physical
            # delete below fails.
            _mark_current(context, [stale], False, logical_key)

            if _remove_vector(context, stale):
                registry.mark_cleaned(logical_key, stale)
                removed += 1
            else:
                deferred += 1

    context.counters = context.counters.merged(
        slots_promoted=promoted,
        representations_superseded=superseded,
        stale_vectors_removed=removed,
        stale_cleanup_deferred=deferred,
    )

    if deferred:
        context.partial_reasons.append(
            f"{deferred} superseded vector(s) could not be removed and are "
            "excluded from search pending reconciliation"
        )

    return {
        "slots_promoted": promoted,
        "representations_superseded": superseded,
        "stale_vectors_removed": removed,
        "stale_cleanup_deferred": deferred,
    }


def _mark_current(
    context: PipelineContext,
    representation_ids: Sequence[str],
    current: bool,
    logical_key: str,
) -> None:
    """Record in authoritative state whether these vectors are current."""
    from dataclasses import replace

    storage = getattr(context.services, "storage", None)
    state = getattr(storage, "state", None)

    if state is None:
        return

    for representation_id in representation_ids:
        metadata = state.load(representation_id)

        if metadata is None:
            continue

        try:
            state.save(
                replace(metadata, is_current=current, logical_key=logical_key),
                expected_version=metadata.version,
            )
        except Exception:  # noqa: BLE001 - a concurrent write wins; search is
            # still correct because the winner is a live write of this vector.
            continue


def _remove_vector(context: PipelineContext, representation_id: str) -> bool:
    """Delete one superseded vector. Never raises."""
    storage = getattr(context.services, "storage", None)

    if storage is None or not hasattr(storage, "delete"):
        return False

    try:
        return bool(storage.delete(representation_id))
    except Exception:  # noqa: BLE001 - a failed delete is a backlog item
        return False


DEFAULT_HANDLERS[PipelineStage.LIFECYCLE_COMMIT] = run_lifecycle_commit
INCREMENTAL_HANDLERS[PipelineStage.LIFECYCLE_COMMIT] = run_lifecycle_commit
