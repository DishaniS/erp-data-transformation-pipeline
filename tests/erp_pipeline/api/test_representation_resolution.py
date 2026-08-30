"""Phase 5 - closing the EMP002 loop through the published API.

    POST /v1/search           which representations are relevant?
    GET  /v1/representations  what does this one actually say?

The test that closes the loop is
``test_the_emp002_certificate_query_resolves_to_its_text``. Everything else
here defends a property that test depends on: that the id a search returns can
always be resolved, that it resolves to the RIGHT text, and that resolving it
never hands back the bytes the text came from.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

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
    InMemoryRepresentationStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
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


def _load_project_env() -> None:
    """Load ``.env`` before any OCR probe, as the ingestion conftest does.

    Without this, whether the scanned-PDF test runs or skips depends on whether
    an ingestion test happened to be collected first - a test whose outcome
    varies with collection order proves nothing either way.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a dependency
        return

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")


_load_project_env()


CERTIFICATE_LINES = [
    "BIRTH CERTIFICATE",
    "Registrar General, Colombo",
    "Name: Nimal Silva",
    "Serial: BC-4471",
]
CONTRACT_LINES = ["EMPLOYMENT CONTRACT", "Position: Senior Accountant", "Grade: M3"]


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type="X",
        normalized_data_type=data_type,
        is_primary_key=primary,
        nullable=not primary,
    )


def _pdf(lines, copies: int = 1) -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()

    for _ in range(copies):
        page = document.new_page()

        for index, line in enumerate(lines):
            page.insert_text((56, 70 + index * 22), line, fontsize=10)

    payload = document.tobytes()
    document.close()

    return payload


def _scanned_pdf(text: str) -> bytes:
    """A picture of text - no text layer at all."""
    fitz = pytest.importorskip("pymupdf")

    typed = fitz.open()
    typed.new_page(width=400, height=200).insert_text((40, 100), text, fontsize=28)
    bitmap = typed.load_page(0).get_pixmap(dpi=300).tobytes("png")
    typed.close()

    scanned = fitz.open()
    scanned.new_page(width=400, height=200).insert_image(
        fitz.Rect(0, 0, 400, 200), stream=bitmap
    )
    payload = scanned.tobytes()
    scanned.close()

    return payload


def _blank_png() -> bytes:
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (200, 100), "white").save(buffer, "PNG")

    return buffer.getvalue()


EMPLOYEES = SourceEntity(
    entity_id="hr.employees",
    source_name="employees",
    normalized_name="employees",
    entity_kind=EntityKind.TABLE,
    primary_key_fields=("employee_id",),
    fields=(
        _field("employee_id", FieldDataType.STRING, primary=True),
        _field("full_name", FieldDataType.STRING),
        _field("department", FieldDataType.STRING),
        _field("birth_certificate", FieldDataType.BINARY),
        _field("employment_contract", FieldDataType.BINARY),
        _field("profile_photo", FieldDataType.BINARY),
    ),
)


class Harness:
    """Indexes employees through the production builders, then serves them."""

    def __init__(self, tmp_path):
        self.representations = InMemoryRepresentationStore()
        self.records = InMemoryCanonicalStore()
        self.storage = PatchedStorage(
            hot=InProcessTier(), state_store=InMemoryTierStateStore()
        )
        self.embedding = EmbeddingService(DeterministicTestModel(dimension=DIMENSION))
        self.raw_blobs: list[bytes] = []

        self.services = PipelineServices(
            records=self.records,
            storage=self.storage,
            embedding=self.embedding,
            representations=self.representations,
        )
        self.app = create_app(
            settings=ApiSettings(upload_dir=tmp_path / "uploads"),
            orchestration=OrchestrationService(
                services=self.services,
                job_store=InMemoryJobStore(),
                executor=InlineJobExecutor(),
            ),
        )

    def add_employee(self, employee_id: str, name: str, **blobs) -> None:
        rows = [
            SourceRecord.from_mapping(
                {
                    "employee_id": employee_id,
                    "full_name": name,
                    "department": "Finance",
                    **blobs,
                }
            )
        ]
        canonical = SourceNativeTransformer().transform_records(
            rows, EMPLOYEES, "legacy_hr", SourceType.POSTGRESQL
        ).records[0]

        self.records.upsert(canonical)
        self._index(canonical_record_to_representation(canonical))

        for field_name, payload in blobs.items():
            if payload is None:
                continue

            self.raw_blobs.append(payload)
            asset = extract_binary_asset(payload, field_name)

            if not asset.succeeded:
                continue

            attachment = DocumentAttachment(
                parent_record_id=canonical.record_id,
                source_system_id="legacy_hr",
                source_entity="employees",
                source_field=field_name,
                document_id=asset.document_id or "",
                business_key_name="employee_id",
                business_key_value=employee_id,
                document_type=field_name,
                media_type=asset.media_type,
            )

            for representation in attached_document_to_representations(
                asset.document, attachment
            ):
                self._index(representation)

    def _index(self, representation) -> None:
        # The pipeline's order: persist, THEN embed and store.
        self.representations.upsert(representation)
        self.storage.store(self.embedding.embed_one(representation))


@pytest.fixture
def harness(tmp_path):
    pytest.importorskip("pymupdf")

    certificate = _pdf(CERTIFICATE_LINES)
    built = Harness(tmp_path)

    built.add_employee(
        "EMP001", "Sunil Bandara", birth_certificate=_pdf(["BIRTH CERTIFICATE One"])
    )
    built.add_employee(
        "EMP002",
        "Nimal Silva",
        birth_certificate=certificate,
        employment_contract=_pdf(CONTRACT_LINES),
        profile_photo=_blank_png(),
    )
    # The SAME certificate bytes.
    built.add_employee("EMP003", "Amal Perera", birth_certificate=certificate)

    return built


@pytest.fixture
def client(harness):
    from fastapi.testclient import TestClient

    with TestClient(harness.app) as test_client:
        yield test_client


def search(client, **body):
    body.setdefault("query", "birth certificate details")
    body.setdefault("top_k", 20)

    return client.post("/v1/search", json=body)


def resolve(client, representation_id: str):
    return client.get(f"/v1/representations/{representation_id}")


# ======================================================================
# THE HEADLINE LOOP
# ======================================================================


def test_the_emp002_certificate_query_resolves_to_its_text(client):
    """Search for EMP002's certificate, then read what it says.

    This is the scenario the whole component is built around, and until Phase 5
    it stopped one step short: the right chunk was found and nothing could say
    what was in it.
    """
    hits = search(
        client,
        filters={
            "business_key_name": "employee_id",
            "business_key_value": "EMP002",
            "document_type": "birth_certificate",
            "content_kind": "document_chunk",
        },
    ).json()["hits"]

    assert hits

    body = resolve(client, hits[0]["representation_id"]).json()

    assert "BIRTH CERTIFICATE" in body["text"]
    assert "Nimal Silva" in body["text"]
    assert body["business_key_value"] == "EMP002"
    assert body["document_type"] == "birth_certificate"
    assert body["content_kind"] == "document_chunk"
    assert body["parent_record_id"] == "erp:legacy_hr:employees:emp002"
    assert body["page_start"] == 1
    assert body["chunk_index"] == 0


# ======================================================================
# TEST H - every search hit resolves
# ======================================================================


def test_every_hit_of_an_unfiltered_search_resolves(client):
    """Hard gate: a searchable vector whose content cannot be found is the
    defect this phase exists to remove."""
    hits = search(client, query="employee certificate contract", top_k=50).json()[
        "hits"
    ]

    assert hits

    unresolvable = [
        hit["representation_id"]
        for hit in hits
        if resolve(client, hit["representation_id"]).status_code != 200
    ]

    assert unresolvable == []


@pytest.mark.parametrize(
    "filters",
    [
        {"content_kind": "document_chunk"},
        {"content_kind": "structured_record"},
        {"business_key_value": "EMP002"},
        {"document_type": "birth_certificate"},
    ],
)
def test_every_hit_of_a_filtered_search_resolves(client, filters):
    hits = search(client, filters=filters).json()["hits"]

    assert hits

    for hit in hits:
        assert resolve(client, hit["representation_id"]).status_code == 200


# ======================================================================
# TEST A - structured records resolve too
# ======================================================================


def test_a_structured_record_resolves_to_its_flattened_text(client):
    """Proves the store is generic rather than document-specific."""
    hits = search(
        client,
        query="employee finance department",
        filters={"content_kind": "structured_record", "business_key_value": "EMP002"},
    ).json()["hits"]

    assert hits

    body = resolve(client, hits[0]["representation_id"]).json()

    assert "EMP002" in body["text"]
    assert "Nimal Silva" in body["text"]
    assert "Finance" in body["text"]
    assert body["content_kind"] == "structured_record"
    assert body["canonical_record_id"] == "erp:legacy_hr:employees:emp002"
    # A structured record has no page or chunk, and does not pretend otherwise.
    assert body["page_start"] is None
    assert body["chunk_index"] is None
    assert body["document_type"] is None


def test_a_structured_record_names_the_canonical_record_it_came_from(client):
    hits = search(
        client,
        query="employee",
        filters={"content_kind": "structured_record", "business_key_value": "EMP003"},
    ).json()["hits"]
    body = resolve(client, hits[0]["representation_id"]).json()

    assert body["source_record_ids"] == ["erp:legacy_hr:employees:emp003"]


# ======================================================================
# TEST C / D - PDF and OCR text
# ======================================================================


def test_a_text_pdf_resolves_to_its_extracted_content(client):
    hits = search(
        client,
        filters={"business_key_value": "EMP002", "document_type": "employment_contract"},
    ).json()["hits"]

    assert hits

    text = resolve(client, hits[0]["representation_id"]).json()["text"]

    assert "EMPLOYMENT CONTRACT" in text
    assert "Senior Accountant" in text


def test_a_scanned_pdf_resolves_to_ocr_derived_text(tmp_path):
    from erp_pipeline.ingestion.ocr import probe_ocr

    if not probe_ocr().available:
        pytest.skip("OCR is unavailable on this machine")

    from fastapi.testclient import TestClient

    built = Harness(tmp_path)
    built.add_employee(
        "EMP004", "Kamala Fernando", birth_certificate=_scanned_pdf("BIRTH CERTIFICATE")
    )

    with TestClient(built.app) as client:
        hits = search(
            client,
            filters={
                "business_key_value": "EMP004",
                "content_kind": "document_chunk",
            },
        ).json()["hits"]

        assert hits

        body = resolve(client, hits[0]["representation_id"]).json()

        assert body["text"]
        assert "CERTIFICATE" in body["text"].upper()


def test_an_image_with_no_readable_text_indexes_nothing_to_resolve(client):
    """The blank profile photo produced no chunk, so there is nothing to find.

    Zero hits is the correct answer - an empty representation would be a
    fabricated one.
    """
    hits = search(
        client,
        filters={"business_key_value": "EMP002", "document_type": "profile_photo"},
    ).json()["hits"]

    assert hits == []


# ======================================================================
# TEST E - same document, two employees
# ======================================================================


def test_the_shared_certificate_resolves_separately_for_each_employee(client):
    """Identical text is correct. Identical association would not be."""
    resolved = {}

    for employee in ("EMP002", "EMP003"):
        hits = search(
            client,
            filters={
                "business_key_value": employee,
                "document_type": "birth_certificate",
            },
        ).json()["hits"]

        assert hits
        resolved[employee] = resolve(client, hits[0]["representation_id"]).json()

    second, third = resolved["EMP002"], resolved["EMP003"]

    assert second["representation_id"] != third["representation_id"]
    # Same document, so the same text and the same document id.
    assert second["text"] == third["text"]
    assert second["document_id"] == third["document_id"]
    # But never the same employee.
    assert second["parent_record_id"] == "erp:legacy_hr:employees:emp002"
    assert third["parent_record_id"] == "erp:legacy_hr:employees:emp003"
    assert second["business_key_value"] == "EMP002"
    assert third["business_key_value"] == "EMP003"


def test_resolving_emp003_never_returns_emp002s_association(client):
    hits = search(
        client,
        filters={"business_key_value": "EMP003", "content_kind": "document_chunk"},
    ).json()["hits"]

    for hit in hits:
        body = resolve(client, hit["representation_id"]).json()

        assert "emp002" not in body["parent_record_id"]
        assert body["business_key_value"] != "EMP002"


# ======================================================================
# TEST F - multi-chunk documents
# ======================================================================


def test_each_chunk_resolves_to_its_own_text(tmp_path):
    """No chunk may resolve to another chunk's text."""
    from fastapi.testclient import TestClient

    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()

    for page_number in range(3):
        page = document.new_page()

        for line in range(30):
            page.insert_text(
                (56, 60 + line * 24),
                f"CONTRACT PAGE {page_number + 1} CLAUSE {line + 1} "
                "the parties agree to the terms set out herein",
                fontsize=9,
            )

    payload = document.tobytes()
    document.close()

    built = Harness(tmp_path)
    built.add_employee("EMP009", "Multi Page", employment_contract=payload)

    with TestClient(built.app) as client:
        hits = search(
            client,
            query="contract clause",
            filters={"business_key_value": "EMP009", "content_kind": "document_chunk"},
        ).json()["hits"]

        assert len(hits) > 1

        bodies = [resolve(client, hit["representation_id"]).json() for hit in hits]

        # Distinct chunks, distinct text, ordinals that line up.
        assert len({body["representation_id"] for body in bodies}) == len(bodies)
        assert len({body["text"] for body in bodies}) == len(bodies)
        assert sorted(body["chunk_index"] for body in bodies) == list(
            range(len(bodies))
        )

        for body in bodies:
            assert body["page_start"] >= 1
            assert body["page_end"] >= body["page_start"]
            assert body["text"].strip()


def test_a_chunk_resolves_to_the_text_its_own_id_names(tmp_path):
    """Resolution is by representation_id, so ordering cannot shift the answer."""
    from fastapi.testclient import TestClient

    built = Harness(tmp_path)
    built.add_employee(
        "EMP010", "Two Docs",
        birth_certificate=_pdf(CERTIFICATE_LINES),
        employment_contract=_pdf(CONTRACT_LINES),
    )

    with TestClient(built.app) as client:
        for document_type, marker in (
            ("birth_certificate", "BIRTH CERTIFICATE"),
            ("employment_contract", "EMPLOYMENT CONTRACT"),
        ):
            hits = search(
                client,
                filters={
                    "business_key_value": "EMP010",
                    "document_type": document_type,
                },
            ).json()["hits"]

            assert hits

            body = resolve(client, hits[0]["representation_id"]).json()

            assert marker in body["text"]
            assert body["document_type"] == document_type


# ======================================================================
# TEST G - unknown representation
# ======================================================================


@pytest.mark.parametrize(
    "identifier",
    ["not-real", "ai:document:never-indexed", "ai:employees:erp_x_y_z"],
)
def test_an_unknown_representation_is_a_404(client, identifier):
    response = resolve(client, identifier)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPRESENTATION_NOT_FOUND"


def test_a_missing_representation_is_never_a_200_with_null_text(client):
    response = resolve(client, "ai:document:definitely-absent")

    assert response.status_code != 200


# ======================================================================
# TEST I - nothing binary comes back
# ======================================================================


def test_no_raw_bytes_or_base64_reach_the_representation_api(client, harness):
    """Extracted text is the intended content; the bytes it came from are not."""
    hits = search(client, query="certificate contract", top_k=50).json()["hits"]
    surface = json.dumps(
        [resolve(client, hit["representation_id"]).json() for hit in hits]
    )

    for payload in harness.raw_blobs:
        assert base64.b64encode(payload).decode()[:24] not in surface

    assert "JVBERi0x" not in surface
    assert "iVBORw0KGgo" not in surface
    assert "/9j/4AAQ" not in surface
    assert "%PDF-" not in surface


def test_the_response_carries_no_vector_and_no_file_path(client):
    hits = search(client, filters={"content_kind": "document_chunk"}).json()["hits"]
    body = resolve(client, hits[0]["representation_id"]).json()

    assert "vector" not in body
    assert "embedding" not in body
    assert "local_path" not in body
    assert "path" not in body
    assert ":\\" not in json.dumps(body)


# ======================================================================
# TEST M - Phase 4 search is unchanged
# ======================================================================


def test_search_still_returns_no_text(client):
    """The split is deliberate: rank ids, expand on demand."""
    body = search(client, filters={"content_kind": "document_chunk"}).json()

    assert body["hits"]

    for hit in body["hits"]:
        assert "text" not in hit
        assert "text_for_ai" not in hit["metadata"]
        assert "BIRTH CERTIFICATE" not in json.dumps(hit)


def test_the_phase_4_filters_still_behave(client):
    assert search(client, filters={"business_key_value": "EMP002"}).json()["hits"]
    assert search(client, filters={"entity_type": "document"}).json()["hits"]
    assert search(client, filters={"source_system_id": "legacy_hr"}).json()["hits"]
    assert search(client, filters={"employee_ssn": "x"}).status_code == 422
    assert search(client, filters={"content_kind": "magic_schema"}).status_code == 422


def test_no_text_reaches_the_vector_payload(harness):
    """Qdrant stays an index, not a second copy of the corpus."""
    from erp_pipeline.storage.migration import _payload_for

    surface = json.dumps(
        [_payload_for(state) for state in harness.storage.state.list_all()],
        default=str,
    )

    assert "BIRTH CERTIFICATE" not in surface
    assert "EMPLOYMENT CONTRACT" not in surface
    assert "text_for_ai" not in surface


# ======================================================================
# TEST J - tier independence
# ======================================================================


def test_resolution_does_not_depend_on_the_vector_tier(client, harness):
    """The representation store is independent of where the vector lives."""
    from erp_pipeline.storage.models import StorageTier

    hits = search(
        client,
        filters={"business_key_value": "EMP002", "document_type": "birth_certificate"},
    ).json()["hits"]
    representation_id = hits[0]["representation_id"]
    before = resolve(client, representation_id).json()

    # Move the vector's recorded tier without touching the representation.
    state = harness.storage.state
    metadata = state.load(representation_id)

    for tier in (StorageTier.WARM, StorageTier.COLD):
        state.save(metadata.with_tier(tier), expected_version=None)
        after = resolve(client, representation_id).json()

        assert after["text"] == before["text"]
        assert after["parent_record_id"] == before["parent_record_id"]


# ======================================================================
# TEST K - reprocessing
# ======================================================================


def test_reprocessing_the_same_employee_does_not_duplicate_representations(
    tmp_path,
):
    built = Harness(tmp_path)
    certificate = _pdf(CERTIFICATE_LINES)

    built.add_employee("EMP002", "Nimal Silva", birth_certificate=certificate)
    first = built.representations.count()

    built.add_employee("EMP002", "Nimal Silva", birth_certificate=certificate)

    assert built.representations.count() == first


# ======================================================================
# OpenAPI
# ======================================================================


def test_the_endpoint_is_published_in_the_openapi_document(harness):
    paths = harness.app.openapi()["paths"]

    assert "/v1/representations/{representation_id}" in paths
    assert (
        paths["/v1/representations/{representation_id}"]["get"]["operationId"]
        == "getRepresentation"
    )


def test_the_response_schema_declares_the_text_field(harness):
    schema = harness.app.openapi()["components"]["schemas"]["RepresentationResponse"]

    for field in (
        "representation_id", "text", "content_kind", "parent_record_id",
        "business_key_value", "document_type", "page_start", "chunk_index",
    ):
        assert field in schema["properties"], field


# ======================================================================
# A deployment with no store configured
# ======================================================================


def test_without_a_store_resolution_says_so_rather_than_404ing(tmp_path):
    """A missing capability and a missing row are different answers."""
    from fastapi.testclient import TestClient

    services = PipelineServices(records=InMemoryCanonicalStore())
    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=OrchestrationService(
            services=services,
            job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        ),
    )

    with TestClient(app) as client:
        response = client.get("/v1/representations/ai:document:x")

        assert response.status_code == 422
        assert "representation store" in response.text
