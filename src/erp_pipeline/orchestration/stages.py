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

from erp_pipeline.orchestration.errors import (
    InvalidPipelineRequestError,
    MappingNotExecutableError,
    UnsupportedCapabilityError,
)
from erp_pipeline.orchestration.extraction import (
    CsvSnapshotExtractor,
    ExtractionRequest,
    RelationalSnapshotExtractor,
    resolve_entity,
)
from erp_pipeline.orchestration.models import PipelineStage
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

    if source_type is SourceType.CSV:
        records = services.extract_csv_records(request.upload_id, entity, limit)
    else:
        source = services.sources.get(request.source_id)
        records = services.extract_snapshot(
            source, ExtractionRequest(context.schema, entity, limit)
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


def run_ai_build(context: PipelineContext) -> Mapping[str, Any]:
    services = context.services

    if context.document is not None:
        representations = services.build_document_representations(context.document)
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
# EMBED - Phase 11
# ----------------------------------------------------------------------


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
    PipelineStage.EMBED: run_incremental_passthrough,
    PipelineStage.TIER_UPDATE: run_incremental_passthrough,
}


DEFAULT_HANDLERS = {
    PipelineStage.DISCOVER: run_discover,
    PipelineStage.MAP: run_map,
    PipelineStage.EXTRACT: run_extract,
    PipelineStage.TRANSFORM: run_transform,
    PipelineStage.VALIDATE: run_validate,
    PipelineStage.LOAD: run_load,
    PipelineStage.AI_BUILD: run_ai_build,
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
