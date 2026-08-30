"""Phase 5 - PERSIST_REPRESENTATIONS inside a real pipeline run.

The ordering assertion here is the one that matters. ``TIER_ROUTE`` is what
makes a vector searchable, so persistence has to happen before it - otherwise
there is a window in which a search can return a hit nobody can resolve, which
is the defect Phase 5 exists to close.
"""

from __future__ import annotations

import pytest

from erp_pipeline.orchestration import (
    InMemoryRepresentationStore,
    PipelineServices,
)
from erp_pipeline.orchestration.models import (
    Job,
    JobRequest,
    JobType,
    PipelineStage,
)
from erp_pipeline.orchestration.pipeline import PipelineContext
from erp_pipeline.orchestration.planner import PipelinePlanner
from erp_pipeline.orchestration.stages import DEFAULT_HANDLERS
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.sync.propagation import AIRepresentation


def representation(identifier: str, text: str = "BIRTH CERTIFICATE") -> AIRepresentation:
    return AIRepresentation(
        representation_id=identifier,
        entity_type="document",
        text_for_ai=text,
        metadata={"content_kind": "document_chunk"},
        source_record_ids=("erp:legacy_hr:employees:emp002",),
    )


def run_stage(services, representations) -> PipelineContext:
    request = JobRequest(
        job_type=JobType.SOURCE_NATIVE_PIPELINE, source_id="legacy_hr"
    )
    context = PipelineContext(
        job=Job(job_id="job_1", request=request),
        plan=PipelinePlanner().plan(request, SourceType.POSTGRESQL),
        services=services,
        representations=tuple(representations),
    )
    handler = DEFAULT_HANDLERS[PipelineStage.PERSIST_REPRESENTATIONS]
    context.outputs[PipelineStage.PERSIST_REPRESENTATIONS.value] = handler(context)

    return context


# ======================================================================
# Ordering - the property everything else depends on
# ======================================================================


@pytest.mark.parametrize(
    "job_type, source_type, extra",
    [
        (JobType.SOURCE_NATIVE_PIPELINE, SourceType.POSTGRESQL, {}),
        (JobType.STRUCTURED_PIPELINE, SourceType.POSTGRESQL, {}),
        (JobType.STRUCTURED_PIPELINE, SourceType.CSV, {}),
        # A document pipeline is only plannable with something to ingest.
        (JobType.DOCUMENT_PIPELINE, SourceType.PDF, {"upload_id": "up_1"}),
        (JobType.INCREMENTAL_SYNC, SourceType.POSTGRESQL, {}),
    ],
)
def test_representations_are_persisted_before_anything_is_embedded(
    job_type, source_type, extra
):
    """Every pipeline shape, not just the one Phase 5 was written against."""
    plan = PipelinePlanner().plan(
        JobRequest(job_type=job_type, source_id="s", **extra), source_type
    )
    stages = list(plan.stages)

    assert PipelineStage.PERSIST_REPRESENTATIONS in stages
    assert stages.index(PipelineStage.PERSIST_REPRESENTATIONS) < stages.index(
        PipelineStage.EMBED
    )


def test_persistence_comes_after_the_representations_are_built():
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.SOURCE_NATIVE_PIPELINE, source_id="s"),
        SourceType.POSTGRESQL,
    )
    stages = list(plan.stages)

    assert stages.index(PipelineStage.AI_BUILD) < stages.index(
        PipelineStage.PERSIST_REPRESENTATIONS
    )
    # Document chunks are produced by MULTIMODAL_EXTRACT, so persistence has to
    # follow it or the attachments would never be stored.
    assert stages.index(PipelineStage.MULTIMODAL_EXTRACT) < stages.index(
        PipelineStage.PERSIST_REPRESENTATIONS
    )


def test_persistence_precedes_the_stage_that_makes_vectors_searchable():
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.SOURCE_NATIVE_PIPELINE, source_id="s"),
        SourceType.POSTGRESQL,
    )
    stages = list(plan.stages)

    assert stages.index(PipelineStage.PERSIST_REPRESENTATIONS) < stages.index(
        PipelineStage.TIER_ROUTE
    )


# ======================================================================
# The stage itself
# ======================================================================


def test_the_stage_writes_every_representation():
    store = InMemoryRepresentationStore()
    services = PipelineServices(representations=store)
    context = run_stage(
        services, [representation(f"ai:document:c{index}") for index in range(3)]
    )

    assert store.count() == 3
    assert context.counters.representations_persisted == 3
    assert context.outputs["persist_representations"][
        "representations_persisted"
    ] == 3


def test_the_stored_text_is_what_was_built():
    store = InMemoryRepresentationStore()
    run_stage(
        PipelineServices(representations=store),
        [representation("ai:document:c0", text="BIRTH CERTIFICATE Nimal Silva")],
    )

    assert store.get("ai:document:c0").text_for_ai == (
        "BIRTH CERTIFICATE Nimal Silva"
    )


def test_a_run_with_nothing_to_persist_is_not_an_error():
    store = InMemoryRepresentationStore()
    context = run_stage(PipelineServices(representations=store), [])

    assert store.count() == 0
    assert context.outputs["persist_representations"]["note"] == "nothing to persist"


def test_a_deployment_without_a_store_still_runs_and_says_so():
    """Phase 5 must not break a deployment that predates it."""
    context = run_stage(PipelineServices(), [representation("ai:document:c0")])
    output = context.outputs["persist_representations"]

    assert output["representations_persisted"] == 0
    assert "no representation store" in output["note"]
    assert any("not be resolvable" in warning for warning in context.warnings)


def test_rerunning_the_stage_does_not_duplicate_rows():
    store = InMemoryRepresentationStore()
    services = PipelineServices(representations=store)
    built = [representation("ai:document:c0")]

    run_stage(services, built)
    run_stage(services, built)

    assert store.count() == 1


# ======================================================================
# TEST N - Phase 14 stays out of the corpus
# ======================================================================


def test_response_adaptation_does_not_write_to_the_representation_store(tmp_path):
    """Phase 14 adapts a response at runtime; it does not index one.

    Routing adapted text into the representation store would put transient
    per-request output into the durable corpus, where a later search could
    return it as though it were ERP content.
    """
    from fastapi.testclient import TestClient

    from erp_pipeline.api import ApiSettings, create_app
    from erp_pipeline.orchestration import (
        InlineJobExecutor,
        InMemoryJobStore,
        OrchestrationService,
    )

    store = InMemoryRepresentationStore()
    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=OrchestrationService(
            services=PipelineServices(representations=store),
            job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses/adapt",
            json={
                "payload": {"invoice_number": "INV-204", "total": "45000.00"},
                "target_entity": "invoice",
            },
        )

    assert response.status_code in (200, 422)
    assert store.count() == 0
    assert store.upsert_calls == 0
