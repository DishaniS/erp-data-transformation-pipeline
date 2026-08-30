"""Phase 3 - MULTIMODAL_EXTRACT inside a real pipeline run.

Exercised at the stage boundary rather than through ``POST /v1/jobs`` because a
CSV upload cannot carry a BLOB: the binary path only exists for live database
sources, which the test suite does not require to be running. What is verified
here is everything the stage owns - counters, appended representations, parent
linkage, partial-success reporting, and the guarantee that no byte of any BLOB
reaches the job report.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

from erp_pipeline.ai.attached_documents import (
    CONTENT_KIND_DOCUMENT_CHUNK,
    DOCUMENT_ENTITY_TYPE,
)
from erp_pipeline.ingestion.binary_assets import binary_field_names_for_entity
from erp_pipeline.orchestration.models import (
    Job,
    JobRequest,
    JobType,
    PipelineStage,
)
from erp_pipeline.orchestration.pipeline import PipelineContext
from erp_pipeline.orchestration.planner import PipelinePlanner
from erp_pipeline.orchestration.stages import DEFAULT_HANDLERS
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SchemaOrigin,
    SourceType,
)
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type="X",
        normalized_data_type=data_type,
        is_primary_key=primary,
        nullable=not primary,
    )


class _ScalarRepresentation:
    """Stand-in for what AI_BUILD produced, so appending can be observed."""

    def __init__(self, record_id: str) -> None:
        self.representation_id = f"scalar::{record_id}"
        self.entity_type = "employee"
        self.metadata = {"content_kind": "structured_record"}


@pytest.fixture
def certificate() -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    document.new_page().insert_text((72, 96), "BIRTH CERTIFICATE\nNimal Silva")
    payload = document.tobytes()
    document.close()

    return payload


@pytest.fixture
def photo() -> bytes:
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (200, 100), "white").save(buffer, "PNG")

    return buffer.getvalue()


@pytest.fixture
def employees() -> SourceEntity:
    return SourceEntity(
        entity_id="hr.employees",
        source_name="employees",
        normalized_name="employees",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("employee_id",),
        fields=(
            _field("employee_id", FieldDataType.STRING, primary=True),
            _field("full_name", FieldDataType.STRING),
            _field("birth_certificate", FieldDataType.BINARY),
            _field("profile_photo", FieldDataType.BINARY),
        ),
    )


@pytest.fixture
def invoices() -> SourceEntity:
    return SourceEntity(
        entity_id="erp.invoices",
        source_name="invoices",
        normalized_name="invoices",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("inv_no",),
        fields=(
            _field("inv_no", FieldDataType.STRING, primary=True),
            _field("total_amt", FieldDataType.DECIMAL),
        ),
    )


def run_stage(services, entity, rows) -> PipelineContext:
    """Drive MULTIMODAL_EXTRACT exactly as the runner does."""
    schema = SourceSchema(
        schema_id="sch_1",
        source_system_id="legacy_hr",
        schema_name="legacy_hr",
        origin=SchemaOrigin.DISCOVERED,
        entities=(entity,),
    )
    request = JobRequest(
        job_type=JobType.SOURCE_NATIVE_PIPELINE,
        source_id="legacy_hr",
        entity=entity.source_name,
    )
    canonical = SourceNativeTransformer().transform_records(
        rows, entity, "legacy_hr", SourceType.POSTGRESQL
    ).records
    context = PipelineContext(
        job=Job(job_id="job_1", request=request),
        plan=PipelinePlanner().plan(request, SourceType.POSTGRESQL),
        services=services,
        schema=schema,
        source_records=tuple(rows),
        canonical_records=tuple(canonical),
        # AI_BUILD has already run and ASSIGNED these.
        representations=tuple(
            _ScalarRepresentation(record.record_id) for record in canonical
        ),
    )
    handler = DEFAULT_HANDLERS[PipelineStage.MULTIMODAL_EXTRACT]
    context.outputs[PipelineStage.MULTIMODAL_EXTRACT.value] = handler(context)

    return context


# ======================================================================
# TEST I - a run that carries binary fields
# ======================================================================


def test_the_stage_indexes_a_certificate_and_links_it_to_its_employee(
    services, employees, certificate
):
    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "full_name": "Nimal Silva",
                "birth_certificate": certificate,
                "profile_photo": None,
            }
        )
    ]
    context = run_stage(services, employees, rows)
    documents = [
        r for r in context.representations
        if getattr(r, "entity_type", None) == DOCUMENT_ENTITY_TYPE
    ]

    assert len(documents) == 1
    assert documents[0].metadata["content_kind"] == CONTENT_KIND_DOCUMENT_CHUNK
    assert documents[0].source_record_ids == ("erp:legacy_hr:employees:emp002",)
    assert documents[0].metadata["business_key_value"] == "EMP002"
    assert "BIRTH CERTIFICATE" in documents[0].text_for_ai


def test_the_scalar_representations_survive_the_stage(
    services, employees, certificate
):
    """AI_BUILD's output is appended to, never replaced."""
    rows = [
        SourceRecord.from_mapping(
            {"employee_id": "EMP002", "full_name": "N", "birth_certificate": certificate}
        )
    ]
    context = run_stage(services, employees, rows)
    ids = [r.representation_id for r in context.representations]

    assert "scalar::erp:legacy_hr:employees:emp002" in ids
    assert len(ids) == 2


def test_the_stage_reports_what_it_did(services, employees, certificate, photo):
    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "full_name": "N",
                "birth_certificate": certificate,
                "profile_photo": photo,
            }
        )
    ]
    context = run_stage(services, employees, rows)

    assert context.counters.binary_fields_seen == 2
    assert context.counters.binary_assets_extracted >= 1


def test_a_null_blob_is_not_counted_as_a_field_seen(services, employees):
    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "full_name": "N",
                "birth_certificate": None,
                "profile_photo": None,
            }
        )
    ]
    context = run_stage(services, employees, rows)

    assert context.counters.binary_fields_seen == 0
    assert context.representations[0].representation_id.startswith("scalar::")


# ======================================================================
# TEST K - entities with no binary columns
# ======================================================================


def test_a_table_with_no_binary_columns_short_circuits(services, invoices):
    rows = [SourceRecord.from_mapping({"inv_no": "INV-1", "total_amt": "10.00"})]
    context = run_stage(services, invoices, rows)
    output = context.outputs[PipelineStage.MULTIMODAL_EXTRACT.value]

    assert output["binary_fields_seen"] == 0
    assert "no binary fields" in output["note"]
    assert len(context.representations) == 1


def test_an_unmapped_binary_column_is_still_opened(services, certificate):
    """A BLOB in a canonical-looking table is not skipped for being unmapped."""
    entity = SourceEntity(
        entity_id="erp.invoices",
        source_name="invoices",
        normalized_name="invoices",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("inv_no",),
        fields=(
            _field("inv_no", FieldDataType.STRING, primary=True),
            _field("total_amt", FieldDataType.DECIMAL),
            _field("scanned_invoice", FieldDataType.BINARY),
        ),
    )

    assert binary_field_names_for_entity(entity) == ("scanned_invoice",)

    rows = [
        SourceRecord.from_mapping(
            {"inv_no": "INV-1", "total_amt": "10.00", "scanned_invoice": certificate}
        )
    ]
    context = run_stage(services, entity, rows)
    documents = [
        r for r in context.representations
        if getattr(r, "entity_type", None) == DOCUMENT_ENTITY_TYPE
    ]

    assert len(documents) == 1
    assert documents[0].metadata["source_field"] == "scanned_invoice"


# ======================================================================
# Partial success
# ======================================================================


def test_a_bad_blob_degrades_the_stage_without_failing_the_job(services, employees):
    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "full_name": "Nimal Silva",
                "birth_certificate": b"PK\x03\x04" + b"\x00" * 100,
            }
        )
    ]
    context = run_stage(services, employees, rows)

    assert context.counters.binary_assets_skipped >= 1
    assert context.partial_reasons
    # The employee record itself was still indexed.
    assert any(
        r.representation_id.startswith("scalar::") for r in context.representations
    )


# ======================================================================
# TEST M - nothing binary reaches the job report
# ======================================================================


def test_no_blob_content_reaches_counters_warnings_or_outputs(
    services, employees, certificate, photo
):
    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "full_name": "N",
                "birth_certificate": certificate,
                "profile_photo": photo,
            }
        ),
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP003",
                "full_name": "A",
                "birth_certificate": b"PK\x03\x04" + b"\x00" * 60,
            }
        ),
    ]
    context = run_stage(services, employees, rows)
    report = json.dumps(
        {
            "counters": context.counters.to_dict(),
            "warnings": list(context.warnings),
            "partial": list(context.partial_reasons),
            "outputs": context.outputs,
        },
        default=str,
    )

    for payload in (certificate, photo):
        assert base64.b64encode(payload).decode()[:24] not in report

    assert "JVBERi0x" not in report
    assert "iVBORw0KGgo" not in report
    assert "%PDF" not in report


def test_two_employees_sharing_a_certificate_keep_separate_vectors(
    services, employees, certificate
):
    rows = [
        SourceRecord.from_mapping(
            {"employee_id": e, "full_name": e, "birth_certificate": certificate}
        )
        for e in ("EMP002", "EMP003")
    ]
    context = run_stage(services, employees, rows)
    documents = [
        r for r in context.representations
        if getattr(r, "entity_type", None) == DOCUMENT_ENTITY_TYPE
    ]

    assert len(documents) == 2
    assert len({d.vector_id for d in documents}) == 2
    assert len({d.metadata["document_id"] for d in documents}) == 1
    assert {d.source_record_ids[0] for d in documents} == {
        "erp:legacy_hr:employees:emp002",
        "erp:legacy_hr:employees:emp003",
    }
