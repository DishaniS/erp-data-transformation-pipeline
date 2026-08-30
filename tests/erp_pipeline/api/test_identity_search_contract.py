"""Phase 4 - the EMP002 query, through the published API.

The storage tests prove the filters work. This proves a CALLER can reach them:
that ``POST /v1/search`` accepts the new fields, refuses everything else, and
returns enough provenance for a consumer to say what it found and which ERP
record it belongs to.

What it must NOT return is the certificate's text. That is Phase 5.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ai.attached_documents import (
    DocumentAttachment,
    attached_document_to_representations,
)
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.ingestion.binary_assets import extract_binary_asset
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SourceType,
)
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.storage.filters import FILTERABLE_FIELDS
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync import InMemoryCanonicalStore
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

from tests.erp_pipeline.api.test_search_resolution_and_filters import (  # noqa: E402
    DIMENSION,
    DeterministicTestModel,
    InProcessTier,
    PatchedStorage,
)


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type="X",
        normalized_data_type=data_type,
        is_primary_key=primary,
        nullable=not primary,
    )


def _pdf(text: str) -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    document.new_page().insert_text((72, 96), text)
    payload = document.tobytes()
    document.close()

    return payload


EMPLOYEES = SourceEntity(
    entity_id="hr.employees",
    source_name="employees",
    normalized_name="employees",
    entity_kind=EntityKind.TABLE,
    primary_key_fields=("employee_id",),
    fields=(
        _field("employee_id", FieldDataType.STRING, primary=True),
        _field("full_name", FieldDataType.STRING),
        _field("birth_certificate", FieldDataType.BINARY),
        _field("employment_contract", FieldDataType.BINARY),
    ),
)


@pytest.fixture
def client(tmp_path):
    """EMP002 and EMP003, each with a scalar record and attachments."""
    pytest.importorskip("pymupdf")

    certificate = _pdf("BIRTH CERTIFICATE Registrar General Colombo")
    contract = _pdf("EMPLOYMENT CONTRACT Senior Accountant")

    records = InMemoryCanonicalStore()
    storage = PatchedStorage(
        hot=InProcessTier(), state_store=InMemoryTierStateStore()
    )
    embedding = EmbeddingService(DeterministicTestModel(dimension=DIMENSION))

    rows = [
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "full_name": "Nimal Silva",
                "birth_certificate": certificate,
                "employment_contract": contract,
            }
        ),
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP003",
                "full_name": "Amal Perera",
                # The SAME certificate bytes.
                "birth_certificate": certificate,
                "employment_contract": None,
            }
        ),
    ]
    canonical = SourceNativeTransformer().transform_records(
        rows, EMPLOYEES, "legacy_hr", SourceType.POSTGRESQL
    ).records

    for row, record in zip(rows, canonical):
        records.upsert(record)
        storage.store(
            embedding.embed_one(canonical_record_to_representation(record))
        )

        for field_name in ("birth_certificate", "employment_contract"):
            payload = row.values.get(field_name)

            if payload is None:
                continue

            asset = extract_binary_asset(payload, field_name)
            attachment = DocumentAttachment(
                parent_record_id=record.record_id,
                source_system_id="legacy_hr",
                source_entity="employees",
                source_field=field_name,
                document_id=asset.document_id or "",
                business_key_name="employee_id",
                business_key_value=row.values["employee_id"],
                document_type=field_name,
                media_type=asset.media_type,
            )

            for representation in attached_document_to_representations(
                asset.document, attachment
            ):
                storage.store(embedding.embed_one(representation))

    services = PipelineServices(
        records=records, storage=storage, embedding=embedding
    )
    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=OrchestrationService(
            services=services,
            job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        ),
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


def search(client, **body):
    body.setdefault("query", "birth certificate details")
    body.setdefault("top_k", 20)

    return client.post("/v1/search", json=body)


# ======================================================================
# The headline query
# ======================================================================


def test_the_emp002_certificate_query_returns_only_emp002(client):
    """The exact request Phase 4 exists to make possible."""
    body = search(
        client,
        filters={
            "business_key_name": "employee_id",
            "business_key_value": "EMP002",
            "document_type": "birth_certificate",
            "content_kind": "document_chunk",
        },
    ).json()

    assert body["hits"]

    for hit in body["hits"]:
        assert hit["metadata"]["business_key_value"] == "EMP002"
        assert hit["metadata"]["document_type"] == "birth_certificate"
        assert hit["metadata"]["content_kind"] == "document_chunk"
        assert hit["metadata"]["parent_record_id"] == (
            "erp:legacy_hr:employees:emp002"
        )


def test_emp003_is_not_reachable_through_the_emp002_query(client):
    body = search(
        client, filters={"business_key_value": "EMP002"}
    ).json()

    assert body["hits"]
    assert all(
        "emp003" not in (hit["metadata"]["parent_record_id"] or "").lower()
        for hit in body["hits"]
    )


def test_the_shared_certificate_is_reachable_from_either_employee(client):
    """Same bytes, same document, two separately addressable attachments."""
    for employee in ("EMP002", "EMP003"):
        body = search(
            client,
            filters={
                "business_key_value": employee,
                "document_type": "birth_certificate",
            },
        ).json()

        assert body["hits"]
        assert {hit["metadata"]["business_key_value"] for hit in body["hits"]} == {
            employee
        }


# ======================================================================
# Response provenance
# ======================================================================


def test_a_document_hit_names_where_it_came_from(client):
    hit = search(
        client, filters={"content_kind": "document_chunk"}
    ).json()["hits"][0]
    metadata = hit["metadata"]

    for key in (
        "content_kind",
        "source_system_id",
        "source_entity",
        "source_field",
        "parent_record_id",
        "business_key_name",
        "business_key_value",
        "document_id",
        "document_type",
        "page_start",
        "page_end",
        "chunk_index",
    ):
        assert key in metadata, key

    assert metadata["page_start"] == 1
    assert metadata["chunk_index"] == 0


def test_a_structured_hit_reports_absent_document_fields_as_null(client):
    """Present-and-null, not missing: the caller should not have to guess."""
    hit = search(
        client,
        query="employee record",
        filters={"content_kind": "structured_record"},
    ).json()["hits"][0]

    assert hit["metadata"]["content_kind"] == "structured_record"
    assert hit["metadata"]["document_type"] is None
    assert hit["metadata"]["page_start"] is None
    assert hit["metadata"]["business_key_value"] in {"EMP002", "EMP003"}


def test_no_document_text_is_returned_yet(client):
    """Phase 5 owns text resolution. Phase 4 must not quietly ship it."""
    body = search(client, filters={"content_kind": "document_chunk"}).json()

    for hit in body["hits"]:
        assert "text" not in hit
        assert "text_for_ai" not in hit
        assert "content" not in hit
        assert "BIRTH CERTIFICATE" not in str(hit)
        assert "Registrar" not in str(hit)


def test_a_hit_still_carries_no_vector(client):
    hit = search(client).json()["hits"][0]

    assert "vector" not in hit
    assert "embedding" not in hit


# ======================================================================
# The contract stays closed
# ======================================================================


@pytest.mark.parametrize("field", ["employee_ssn", "salary", "text_for_ai"])
def test_an_unregistered_filter_is_refused_by_the_api(client, field):
    response = search(client, filters={field: "x"})

    assert response.status_code == 422
    assert field in response.text


def test_the_refusal_names_the_supported_set(client):
    response = search(client, filters={"employee_ssn": "x"})

    for name in FILTERABLE_FIELDS:
        assert name in response.text


@pytest.mark.parametrize("field", ["page_start", "page_end", "chunk_index"])
def test_provenance_fields_are_not_accepted_as_filters(client, field):
    """Returned with every hit, deliberately not matchable."""
    assert search(client, filters={field: 1}).status_code == 422


def test_a_content_kind_that_does_not_exist_is_refused(client):
    """``schema`` became real in Phase 7; ``magic_schema`` never will."""
    response = search(client, filters={"content_kind": "magic_schema"})

    assert response.status_code == 422
    assert "structured_record" in response.text


def test_the_applied_filters_are_echoed_back(client):
    applied = {
        "business_key_value": "EMP002",
        "content_kind": "document_chunk",
    }
    body = search(client, filters=applied).json()

    assert body["filters_applied"] == applied


# ======================================================================
# Combinations
# ======================================================================


def test_filters_combine_with_and_semantics(client):
    body = search(
        client,
        filters={
            "business_key_value": "EMP002",
            "document_type": "employment_contract",
        },
    ).json()

    assert body["hits"]
    assert {hit["metadata"]["document_type"] for hit in body["hits"]} == {
        "employment_contract"
    }


def test_a_contradictory_combination_returns_nothing(client):
    body = search(
        client,
        filters={
            "business_key_value": "EMP003",
            "document_type": "employment_contract",
        },
    ).json()

    assert body["hits"] == []


def test_the_original_filters_still_work_through_the_api(client):
    assert search(client, filters={"entity_type": "document"}).json()["hits"]
    assert search(client, filters={"source_system_id": "legacy_hr"}).json()["hits"]
    assert search(client, filters={"sensitivity": "internal"}).json()["hits"]
