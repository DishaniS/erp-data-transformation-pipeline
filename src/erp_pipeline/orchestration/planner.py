"""Which stages a job runs, decided by source capability - not by hope.

THE POINT OF A PLANNER
----------------------
A CSV has no database to discover. A PDF has no fields to map. An OpenAPI
document describes endpoints that Phase 13 is forbidden to call. Running one
fixed stage list and skipping whatever fails would turn every capability
boundary into a runtime error and every genuine error into noise.

So the plan is computed up front from the source type, and asking for something
a source cannot do is refused *before* any work starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from erp_pipeline.orchestration.errors import (
    InvalidPipelineRequestError,
    UnsupportedCapabilityError,
)
from erp_pipeline.orchestration.models import JobRequest, JobType, PipelineStage
from erp_pipeline.schemas.enums import SourceType

#: Structured extraction is only meaningful where records actually live.
RECORD_BEARING_SOURCES = frozenset(
    {
        SourceType.POSTGRESQL,
        SourceType.MYSQL,
        SourceType.SQL_SERVER,
        SourceType.MONGODB,
        SourceType.CSV,
    }
)

#: Sources whose schema is discovered by querying a live server.
DISCOVERABLE_SOURCES = frozenset(
    {
        SourceType.POSTGRESQL,
        SourceType.MYSQL,
        SourceType.SQL_SERVER,
        SourceType.MONGODB,
    }
)

DOCUMENT_SOURCES = frozenset({SourceType.PDF, SourceType.IMAGE})

#: Contracts, not data. Phase 13 parses these and stops.
SPEC_SOURCES = frozenset({SourceType.OPENAPI, SourceType.POSTMAN})

STRUCTURED_TAIL = (
    PipelineStage.EXTRACT,
    PipelineStage.TRANSFORM,
    PipelineStage.VALIDATE,
    PipelineStage.LOAD,
    PipelineStage.AI_BUILD,
    # Binary fields the row carried are opened here, after the scalar record
    # has a stable identity to hang them off. A row with no binary column
    # passes straight through.
    PipelineStage.MULTIMODAL_EXTRACT,
    # The AI text is written to durable storage BEFORE anything is embedded,
    # so a vector can never become searchable while its content is
    # unresolvable. See ``run_persist_representations``.
    PipelineStage.PERSIST_REPRESENTATIONS,
    PipelineStage.EMBED,
    PipelineStage.TIER_ROUTE,
    PipelineStage.LIFECYCLE_COMMIT,
)

DOCUMENT_STAGES = (
    PipelineStage.INGEST,
    PipelineStage.AI_BUILD,
    PipelineStage.PERSIST_REPRESENTATIONS,
    PipelineStage.EMBED,
    PipelineStage.TIER_ROUTE,
    PipelineStage.LIFECYCLE_COMMIT,
)

INCREMENTAL_STAGES = (
    PipelineStage.DRIFT_CHECK,
    PipelineStage.EXTRACT_CHANGED,
    PipelineStage.TRANSFORM,
    PipelineStage.VALIDATE,
    PipelineStage.LOAD,
    PipelineStage.AI_BUILD,
    PipelineStage.PERSIST_REPRESENTATIONS,
    PipelineStage.EMBED,
    PipelineStage.TIER_UPDATE,
    PipelineStage.LIFECYCLE_COMMIT,
)

#: A schema job needs no extraction: the schema is already in the catalog. It
#: goes straight to building representations and then runs the identical tail
#: every other pipeline uses.
SCHEMA_STAGES = (
    PipelineStage.AI_BUILD,
    PipelineStage.PERSIST_REPRESENTATIONS,
    PipelineStage.EMBED,
    PipelineStage.TIER_ROUTE,
    PipelineStage.LIFECYCLE_COMMIT,
)

SPEC_STAGES = (
    PipelineStage.PARSE_SPEC,
    PipelineStage.SCHEMA,
    PipelineStage.MAP,
)

DRIFT_STAGES = (PipelineStage.DRIFT_CHECK,)

#: A source-native run is the structured run with the mapping replaced by an
#: ADMISSION CHECK. Everything after the guard is byte-for-byte the same tail
#: the canonical path uses, which is the point: an uncovered entity should
#: differ from a covered one in how its fields are named, not in how it is
#: extracted, embedded, routed or searched.
SOURCE_NATIVE_TAIL = (
    PipelineStage.EXTRACT,
    PipelineStage.TRANSFORM,
    PipelineStage.VALIDATE,
    PipelineStage.LOAD,
    PipelineStage.AI_BUILD,
    PipelineStage.MULTIMODAL_EXTRACT,
    # Persisted before embedding, so a searchable vector always has
    # resolvable content. See ``run_persist_representations``.
    PipelineStage.PERSIST_REPRESENTATIONS,
    PipelineStage.EMBED,
    PipelineStage.TIER_ROUTE,
    PipelineStage.LIFECYCLE_COMMIT,
)


@dataclass(frozen=True)
class PipelinePlan:
    """The stage list a job will follow, and why it looks like that."""

    job_type: JobType
    stages: tuple[PipelineStage, ...]
    source_type: SourceType | None
    #: Stages a reader might expect that this shape genuinely never runs.
    not_applicable: tuple[PipelineStage, ...] = ()
    rationale: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "job_type": self.job_type.value,
            "source_type": self.source_type.value if self.source_type else None,
            "stages": [stage.value for stage in self.stages],
            "not_applicable": [stage.value for stage in self.not_applicable],
            "rationale": self.rationale,
        }


class PipelinePlanner:
    """Builds a stage graph from the job type and the source's capabilities."""

    def plan(
        self, request: JobRequest, source_type: SourceType | None
    ) -> PipelinePlan:
        if request.job_type is JobType.STRUCTURED_PIPELINE:
            return self._structured(request, source_type)

        if request.job_type is JobType.DOCUMENT_PIPELINE:
            return self._document(request, source_type)

        if request.job_type is JobType.INCREMENTAL_SYNC:
            return self._incremental(request, source_type)

        if request.job_type is JobType.DRIFT_CHECK:
            return PipelinePlan(
                job_type=request.job_type,
                stages=DRIFT_STAGES,
                source_type=source_type,
                rationale="drift check compares schemas and changes no data",
            )

        if request.job_type is JobType.API_SPEC_PREPARATION:
            return self._api_spec(request, source_type)

        if request.job_type is JobType.SOURCE_NATIVE_PIPELINE:
            return self._source_native(request, source_type)

        if request.job_type is JobType.SCHEMA_PIPELINE:
            return self._schema_index(request, source_type)

        raise InvalidPipelineRequestError(
            f"unknown job type {request.job_type!r}"
        )

    # ------------------------------------------------------------------

    def _schema_index(
        self, request: JobRequest, source_type: SourceType | None
    ) -> PipelinePlan:
        """Index an already-catalogued schema as searchable structure.

        Refused without a ``schema_id`` rather than defaulting to "whatever was
        discovered last": indexing the wrong schema is silent and looks like a
        stale index.
        """
        if not request.schema_id:
            raise InvalidPipelineRequestError(
                "a schema indexing job needs a schema_id"
            )

        return PipelinePlan(
            job_type=request.job_type,
            stages=SCHEMA_STAGES,
            source_type=source_type,
            rationale=(
                "the schema is already in the catalog, so there is nothing to "
                "discover, extract or transform"
            ),
            not_applicable=(
                PipelineStage.DISCOVER,
                PipelineStage.MAP,
                PipelineStage.EXTRACT,
                PipelineStage.TRANSFORM,
                PipelineStage.VALIDATE,
                PipelineStage.LOAD,
                PipelineStage.MULTIMODAL_EXTRACT,
            ),
        )

    def _structured(
        self, request: JobRequest, source_type: SourceType | None
    ) -> PipelinePlan:
        if source_type is None:
            raise InvalidPipelineRequestError(
                "a structured pipeline needs a registered source"
            )

        if source_type in SPEC_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source_type.value} describes API operations, not stored "
                "records. Phase 13 parses the contract and never calls the "
                "documented endpoints, so there is nothing to extract.",
                source_type=source_type.value,
                supported_job_type=JobType.API_SPEC_PREPARATION.value,
            )

        if source_type in DOCUMENT_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source_type.value} is a document; use a document pipeline, "
                "which needs no mapping or transformation stage.",
                source_type=source_type.value,
                supported_job_type=JobType.DOCUMENT_PIPELINE.value,
            )

        if source_type not in RECORD_BEARING_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source_type.value} does not expose extractable records",
                source_type=source_type.value,
            )

        # A CSV's schema arrived with the upload, so there is no server to
        # interrogate and DISCOVER genuinely does not apply.
        if source_type is SourceType.CSV:
            return PipelinePlan(
                job_type=request.job_type,
                stages=(PipelineStage.MAP,) + STRUCTURED_TAIL,
                source_type=source_type,
                not_applicable=(PipelineStage.DISCOVER,),
                rationale=(
                    "the CSV's schema was inferred at upload time, so there is "
                    "no live source to discover"
                ),
            )

        return PipelinePlan(
            job_type=request.job_type,
            stages=(PipelineStage.DISCOVER, PipelineStage.MAP) + STRUCTURED_TAIL,
            source_type=source_type,
            rationale="full structured pipeline over a live record-bearing source",
        )

    def _source_native(
        self, request: JobRequest, source_type: SourceType | None
    ) -> PipelinePlan:
        """An uncovered ERP entity, indexed under its own field names.

        Applies the SAME capability rules as the structured pipeline - an API
        specification still has no records, a PDF is still a document - because
        being outside the canonical vocabulary says nothing about whether a
        source holds extractable rows.
        """
        if source_type is None:
            raise InvalidPipelineRequestError(
                "a source-native pipeline needs a registered source"
            )

        if source_type in SPEC_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source_type.value} describes API operations, not stored "
                "records, so there is nothing to extract.",
                source_type=source_type.value,
                supported_job_type=JobType.API_SPEC_PREPARATION.value,
            )

        if source_type in DOCUMENT_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source_type.value} is a document; use a document pipeline.",
                source_type=source_type.value,
                supported_job_type=JobType.DOCUMENT_PIPELINE.value,
            )

        if source_type not in RECORD_BEARING_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source_type.value} does not expose extractable records",
                source_type=source_type.value,
            )

        if source_type is SourceType.CSV:
            return PipelinePlan(
                job_type=request.job_type,
                stages=(PipelineStage.SOURCE_NATIVE_GUARD,) + SOURCE_NATIVE_TAIL,
                source_type=source_type,
                not_applicable=(PipelineStage.DISCOVER, PipelineStage.MAP),
                rationale=(
                    "the CSV's schema was inferred at upload time, and this "
                    "entity is indexed under its own field names rather than "
                    "through a canonical mapping"
                ),
            )

        return PipelinePlan(
            job_type=request.job_type,
            stages=(PipelineStage.DISCOVER, PipelineStage.SOURCE_NATIVE_GUARD)
            + SOURCE_NATIVE_TAIL,
            source_type=source_type,
            not_applicable=(PipelineStage.MAP,),
            rationale=(
                "the entity is outside the canonical vocabulary, so it is "
                "indexed under its own field names; MAP does not apply and the "
                "guard stage verifies that no canonical entity claims it"
            ),
        )

    def _document(
        self, request: JobRequest, source_type: SourceType | None
    ) -> PipelinePlan:
        if source_type is not None and source_type not in DOCUMENT_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source_type.value} is not a document source",
                source_type=source_type.value,
            )

        if not request.upload_id:
            raise InvalidPipelineRequestError(
                "a document pipeline needs an uploaded document"
            )

        return PipelinePlan(
            job_type=request.job_type,
            stages=DOCUMENT_STAGES,
            source_type=source_type,
            not_applicable=(
                PipelineStage.DISCOVER,
                PipelineStage.MAP,
                PipelineStage.TRANSFORM,
                PipelineStage.VALIDATE,
            ),
            rationale=(
                "a document has no tabular fields, so there is nothing to map "
                "to canonical columns and nothing to transform; its text is "
                "chunked and embedded directly"
            ),
        )

    def _incremental(
        self, request: JobRequest, source_type: SourceType | None
    ) -> PipelinePlan:
        if source_type is None:
            raise InvalidPipelineRequestError(
                "an incremental sync needs a registered source"
            )

        if source_type not in DISCOVERABLE_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source_type.value} cannot be polled for changes; "
                "incremental sync needs a live queryable source",
                source_type=source_type.value,
            )

        return PipelinePlan(
            job_type=request.job_type,
            stages=INCREMENTAL_STAGES,
            source_type=source_type,
            rationale="incremental sync over a live source, driven by Phase 10",
        )

    def _api_spec(
        self, request: JobRequest, source_type: SourceType | None
    ) -> PipelinePlan:
        if source_type is not None and source_type not in SPEC_SOURCES:
            raise UnsupportedCapabilityError(
                f"{source_type.value} is not an API specification",
                source_type=source_type.value,
            )

        return PipelinePlan(
            job_type=request.job_type,
            stages=SPEC_STAGES,
            source_type=source_type,
            not_applicable=(
                PipelineStage.EXTRACT,
                PipelineStage.TRANSFORM,
                PipelineStage.LOAD,
                PipelineStage.EMBED,
            ),
            rationale=(
                "an API specification yields a schema and a mapping only; "
                "calling the documented endpoints is outside this component"
            ),
        )


__all__ = [
    "RECORD_BEARING_SOURCES",
    "DISCOVERABLE_SOURCES",
    "DOCUMENT_SOURCES",
    "SPEC_SOURCES",
    "PipelinePlan",
    "PipelinePlanner",
]
