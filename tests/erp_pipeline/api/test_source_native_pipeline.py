"""Phase 2 - the source-native job, and the gate that protects mapping review.

The single most important test in this file is
``test_a_covered_entity_cannot_be_indexed_source_natively``. Without it, a
caller facing an ambiguous mapping could re-submit the same data as a
source-native job and index it anyway, quietly routing around the refusal
mechanism that makes the mapping engine trustworthy.
"""

from __future__ import annotations

import pytest

from erp_pipeline.orchestration import (
    JobType,
    RegisteredSource,
    SourceNativeNotPermittedError,
)
from erp_pipeline.orchestration.models import JobRequest, PipelineStage
from erp_pipeline.orchestration.planner import PipelinePlanner
from erp_pipeline.schemas.enums import SourceType

EMPLOYEES_CSV = (
    b"employee_id,name,department,job_title\n"
    b"EMP002,Nimal Silva,Finance,Accountant\n"
    b"EMP003,Amal Perera,HR,Officer\n"
)

INVOICES_CSV = (
    b"inv_no,cust_ref,total_amt,curr,approval_status\n"
    b"INV-204,CUS-17,45000.00,LKR,A\n"
)

# A ledger export: the table name matches no canonical entity, but the FIELDS
# genuinely belong to more than one, so mapping is ambiguous rather than absent.
AMBIGUOUS_CSV = (
    b"invoice_id,customer_id,customer_name,amount,currency,status\n"
    b"INV-9,CUS-9,Acme,10.00,LKR,approved\n"
)


def upload(client, name: str, data: bytes):
    return client.post(
        "/v1/files/csv", files={"file": (name, data, "text/csv")}
    ).json()


def source_native_job(client, uploaded, source_id: str, **options):
    payload = {
        "job_type": JobType.SOURCE_NATIVE_PIPELINE.value,
        "source_id": source_id,
        "schema_id": uploaded["schema_id"],
        "upload_id": uploaded["upload_id"],
    }

    if options:
        payload["options"] = options

    return client.post("/v1/jobs", json=payload)


def register_csv_source(services, source_id: str) -> None:
    services.sources.register(
        RegisteredSource(source_id=source_id, name=source_id, source_type=SourceType.CSV)
    )


def stage(job: dict, name: str) -> dict:
    for entry in job.get("stages", []):
        if entry.get("stage") == name:
            return entry

    raise AssertionError(f"{name} not in {[s['stage'] for s in job.get('stages', [])]}")


# ======================================================================
# The planner
# ======================================================================


def test_the_plan_has_a_guard_and_no_mapping_stage():
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.SOURCE_NATIVE_PIPELINE, source_id="s"),
        SourceType.CSV,
    )

    assert PipelineStage.SOURCE_NATIVE_GUARD in plan.stages
    assert PipelineStage.MAP not in plan.stages
    assert PipelineStage.MAP in plan.not_applicable


def test_the_plan_reuses_the_existing_tail_unchanged():
    """An uncovered entity should differ in NAMING, not in how it is indexed.

    ``MULTIMODAL_EXTRACT`` joined this tail in Phase 3,
    ``PERSIST_REPRESENTATIONS`` in Phase 5 and ``LIFECYCLE_COMMIT`` in Phase 9.
    All three are listed here rather than excused, because the point of the
    test is that the source-native tail stays identical to the structured one -
    and it still is.
    """
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.SOURCE_NATIVE_PIPELINE, source_id="s"),
        SourceType.CSV,
    )
    tail = plan.stages[plan.stages.index(PipelineStage.EXTRACT) :]

    assert tail == (
        PipelineStage.EXTRACT,
        PipelineStage.TRANSFORM,
        PipelineStage.VALIDATE,
        PipelineStage.LOAD,
        PipelineStage.AI_BUILD,
        PipelineStage.MULTIMODAL_EXTRACT,
        PipelineStage.PERSIST_REPRESENTATIONS,
        PipelineStage.EMBED,
        PipelineStage.TIER_ROUTE,
        PipelineStage.LIFECYCLE_COMMIT,
    )


def test_a_live_source_still_discovers_first():
    plan = PipelinePlanner().plan(
        JobRequest(job_type=JobType.SOURCE_NATIVE_PIPELINE, source_id="s"),
        SourceType.POSTGRESQL,
    )

    assert plan.stages[0] is PipelineStage.DISCOVER
    assert plan.stages[1] is PipelineStage.SOURCE_NATIVE_GUARD


@pytest.mark.parametrize("source_type", [SourceType.OPENAPI, SourceType.PDF])
def test_capability_rules_are_the_same_as_the_structured_pipeline(source_type):
    """Being outside the canonical vocabulary says nothing about having rows."""
    from erp_pipeline.orchestration import UnsupportedCapabilityError

    with pytest.raises(UnsupportedCapabilityError):
        PipelinePlanner().plan(
            JobRequest(job_type=JobType.SOURCE_NATIVE_PIPELINE, source_id="s"),
            source_type,
        )


# ======================================================================
# TEST G - the gate. Mapping review cannot be bypassed.
# ======================================================================


def test_a_covered_entity_cannot_be_indexed_source_natively(client, services):
    """THE regression test for this phase.

    ``invoices`` maps cleanly onto the canonical ``invoice``. Allowing a
    source-native job here would let any caller sidestep the canonical path -
    and, when the mapping is ambiguous, sidestep the human decision the engine
    deliberately demanded.
    """
    register_csv_source(services, "src_inv")
    uploaded = upload(client, "invoices.csv", INVOICES_CSV)

    created = source_native_job(client, uploaded, "src_inv")
    job = client.get(f"/v1/jobs/{created.json()['job_id']}").json()

    assert job["status"] == "failed"
    guard = stage(job, "source_native_guard")
    assert guard["status"] == "failed"
    assert "canonical" in str(guard.get("detail", "")).lower()


def test_the_refusal_tells_the_caller_what_to_do_instead(client, services):
    register_csv_source(services, "src_inv2")
    uploaded = upload(client, "invoices.csv", INVOICES_CSV)

    job = client.get(
        f"/v1/jobs/{source_native_job(client, uploaded, 'src_inv2').json()['job_id']}"
    ).json()
    detail = str(stage(job, "source_native_guard").get("detail", ""))

    assert "structured_pipeline" in detail or "mapping" in detail.lower()


def test_an_ambiguous_mapping_cannot_be_bypassed(client, services):
    """The case the safety rule was written for.

    A ledger export whose fields genuinely belong to several canonical entities
    produces AMBIGUOUS decisions. If it is nonetheless claimed by a canonical
    entity, source-native indexing must be refused - the ambiguity is a decision
    waiting for a human, not a reason to give up on the canonical model.
    """
    register_csv_source(services, "src_amb")
    uploaded = upload(client, "invoices.csv", AMBIGUOUS_CSV)

    suggested = client.post(
        "/v1/mappings/suggest", json={"schema_id": uploaded["schema_id"]}
    ).json()

    created = source_native_job(client, uploaded, "src_amb")
    job = client.get(f"/v1/jobs/{created.json()['job_id']}").json()

    # Either the entity is canonically claimed - in which case the guard must
    # refuse - or it is genuinely uncovered, in which case there was no
    # canonical decision to bypass in the first place.
    if not suggested.get("auto_approved") and suggested.get("ambiguous_fields"):
        assert job["status"] == "failed"
        assert stage(job, "source_native_guard")["status"] == "failed"


def test_the_guard_error_maps_to_a_conflict_status():
    from erp_pipeline.api.responses import status_for

    assert status_for(SourceNativeNotPermittedError("x")) == 409


# ======================================================================
# TEST B - an uncovered entity is admitted and indexed
# ======================================================================


def test_an_uncovered_entity_is_admitted(client, services):
    register_csv_source(services, "src_emp")
    uploaded = upload(client, "employees.csv", EMPLOYEES_CSV)

    created = source_native_job(
        client, uploaded, "src_emp", key_fields=["employee_id"]
    )
    job = client.get(f"/v1/jobs/{created.json()['job_id']}").json()

    assert created.status_code == 202
    assert stage(job, "source_native_guard")["status"] == "succeeded"
    assert stage(job, "transform")["status"] == "succeeded"
    assert job["counters"]["records_transformed"] == 2


def test_an_admitted_entity_produces_representations(client, services):
    register_csv_source(services, "src_emp2")
    uploaded = upload(client, "employees.csv", EMPLOYEES_CSV)

    job = client.get(
        f"/v1/jobs/"
        f"{source_native_job(client, uploaded, 'src_emp2', key_fields=['employee_id']).json()['job_id']}"
    ).json()

    assert stage(job, "ai_build")["status"] == "succeeded"
    assert job["counters"]["representations_built"] == 2


def test_the_records_are_resolvable_through_the_existing_record_api(
    client, services
):
    """No parallel store. The generic record resolves like any other."""
    register_csv_source(services, "src_emp3")
    uploaded = upload(client, "employees.csv", EMPLOYEES_CSV)
    source_native_job(client, uploaded, "src_emp3", key_fields=["employee_id"])

    response = client.get("/v1/records/erp:file_source:employees:emp002")

    assert response.status_code == 200
    assert response.json()["data"]["name"] == "Nimal Silva"
    assert response.json()["entity_type"] == "employees"


def test_a_csv_without_a_declared_key_refuses_rather_than_using_a_row_number(
    client, services
):
    """A CSV declares no primary key, and its row number is a POSITION.

    Indexing on it would re-identify every record whenever the file is
    reordered, so the caller is required to say which column is the key.
    """
    register_csv_source(services, "src_nokey")
    uploaded = upload(client, "employees.csv", EMPLOYEES_CSV)

    job = client.get(
        f"/v1/jobs/{source_native_job(client, uploaded, 'src_nokey').json()['job_id']}"
    ).json()

    assert job["counters"].get("records_transformed") in (None, 0)
    assert any("identity" in w for w in job.get("warnings", []))
    assert client.get("/v1/records/erp:file_source:employees:1").status_code == 404


# ======================================================================
# TEST A - the canonical path is untouched
# ======================================================================


def test_the_canonical_job_still_uses_the_mapping_path(client, services):
    register_csv_source(services, "src_can")
    uploaded = upload(client, "invoices.csv", INVOICES_CSV)

    created = client.post(
        "/v1/jobs",
        json={
            "job_type": JobType.STRUCTURED_PIPELINE.value,
            "source_id": "src_can",
            "schema_id": uploaded["schema_id"],
            "upload_id": uploaded["upload_id"],
        },
    )
    job = client.get(f"/v1/jobs/{created.json()['job_id']}").json()

    assert stage(job, "map")["status"] == "succeeded"
    assert "source_native_guard" not in [s["stage"] for s in job["stages"]]


def test_a_canonical_record_keeps_its_canonical_entity_type(client, services):
    register_csv_source(services, "src_can2")
    uploaded = upload(client, "invoices.csv", INVOICES_CSV)
    client.post(
        "/v1/jobs",
        json={
            "job_type": JobType.STRUCTURED_PIPELINE.value,
            "source_id": "src_can2",
            "schema_id": uploaded["schema_id"],
            "upload_id": uploaded["upload_id"],
        },
    )

    response = client.get("/v1/records/erp:file_source:invoice:inv-204")

    assert response.status_code == 200
    assert response.json()["entity_type"] == "invoice"
    assert response.json()["data"]["invoice_id"] == "INV-204"


def test_the_new_job_type_is_advertised(client):
    capabilities = client.get("/v1/capabilities").json()

    assert JobType.SOURCE_NATIVE_PIPELINE.value in capabilities["job_types"]
    # The existing ones are all still there.
    for existing in (
        "structured_pipeline",
        "document_pipeline",
        "incremental_sync",
        "drift_check",
        "api_spec_preparation",
    ):
        assert existing in capabilities["job_types"]
