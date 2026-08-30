"""Phase 8 - declared remote assets through the real pipeline.

Covers the boundaries that only appear once a URL is attached to an ERP row:
that an undeclared field is never fetched, that two employees sharing one URL
stay separate, that a schema job never fetches anything, and that the whole
loop ends in resolved certificate text.

No test opens a socket.
"""

from __future__ import annotations

import json

import pytest

from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.ingestion.remote_assets import RemoteAssetOutcome
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemoryRepresentationStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.orchestration.multimodal import extract_record_assets
from erp_pipeline.response_adaptation.assets import FetchedAsset, UrlSafetyPolicy
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

SECRET = "SUPERSECRET"
CERT_URL = "https://assets.example.test/emp002-cert.pdf"
SIGNED_URL = f"{CERT_URL}?token={SECRET}&expires=1735689600"
PUBLIC = "93.184.216.34"


def pdf_bytes(text="BIRTH CERTIFICATE Nimal Silva") -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    document.new_page().insert_text((72, 96), text)
    payload = document.tobytes()
    document.close()

    return payload


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name, normalized_name=name, source_data_type="X",
        normalized_data_type=data_type, is_primary_key=primary,
        nullable=not primary,
    )


EMPLOYEES = SourceEntity(
    entity_id="legacy_hr.public.employees",
    source_name="employees",
    normalized_name="employees",
    entity_kind=EntityKind.TABLE,
    primary_key_fields=("employee_id",),
    fields=(
        _field("employee_id", FieldDataType.STRING, primary=True),
        _field("full_name", FieldDataType.STRING),
        _field("website", FieldDataType.STRING),
        _field("birth_certificate_url", FieldDataType.STRING),
    ),
)

POLICY = UrlSafetyPolicy(enabled=True, max_redirects=1)


class CountingFetcher:
    def __init__(self, body=None, content_type="application/pdf"):
        self.body = body if body is not None else pdf_bytes()
        self.content_type = content_type
        self.calls: list[str] = []

    def __call__(self, validated):
        self.calls.append(validated.url)

        return FetchedAsset(body=self.body, content_type=self.content_type)


def resolver(host):
    return (PUBLIC,)


def rows(*employees):
    return [
        SourceRecord.from_mapping(
            {
                "employee_id": employee,
                "full_name": f"Name {employee}",
                "website": "https://intranet.example.test/staff",
                "birth_certificate_url": url,
            }
        )
        for employee, url in employees
    ]


def canonicalise(records, asset_url_fields=()):
    return SourceNativeTransformer().transform_records(
        records, EMPLOYEES, "legacy_hr", SourceType.POSTGRESQL,
        asset_url_fields=asset_url_fields,
    ).records


def run(records, declared, fetcher):
    canonical = canonicalise(records, tuple(declared))

    return canonical, extract_record_assets(
        records, canonical, EMPLOYEES, (),
        asset_url_fields=declared, url_policy=POLICY,
        fetcher=fetcher, resolver=resolver,
    )


# ======================================================================
# TEST P - nothing undeclared is ever fetched
# ======================================================================


def test_an_undeclared_url_field_is_never_fetched():
    """``website`` holds a URL and is not declared. No request happens."""
    fetcher = CountingFetcher()
    _, result = run(rows(("EMP002", CERT_URL)), {}, fetcher)

    assert fetcher.calls == []
    assert result.representations == ()
    assert result.remote_assets == 0


def test_declaring_one_field_does_not_fetch_the_other():
    fetcher = CountingFetcher()
    _, result = run(
        rows(("EMP002", CERT_URL)), {"birth_certificate_url": None}, fetcher
    )

    assert len(fetcher.calls) == 1
    assert fetcher.calls[0] == CERT_URL
    assert "intranet" not in " ".join(fetcher.calls)


def test_a_column_named_url_is_not_enough_on_its_own():
    """``birth_certificate_url`` is a naming convention, not an authorisation."""
    fetcher = CountingFetcher()
    _, result = run(rows(("EMP002", CERT_URL)), {}, fetcher)

    assert fetcher.calls == []


# ======================================================================
# TEST V - the full source-native path
# ======================================================================


def test_a_declared_url_becomes_an_indexed_document():
    fetcher = CountingFetcher()
    canonical, result = run(
        rows(("EMP002", CERT_URL)),
        {"birth_certificate_url": "birth_certificate"},
        fetcher,
    )

    assert len(result.representations) == 1

    metadata = result.representations[0].metadata

    assert metadata["content_kind"] == "document_chunk"
    assert metadata["parent_record_id"] == "erp:legacy_hr:employees:emp002"
    assert metadata["business_key_value"] == "EMP002"
    assert metadata["document_type"] == "birth_certificate"
    assert metadata["source_field"] == "birth_certificate_url"
    assert metadata["asset_origin"] == "remote_url"
    assert metadata["source_url_host"] == "assets.example.test"
    assert "BIRTH CERTIFICATE" in result.representations[0].text_for_ai


def test_without_a_declared_document_type_the_field_name_is_used():
    """No suffix stripping: a wrong document_type is a wrong filter."""
    fetcher = CountingFetcher()
    _, result = run(
        rows(("EMP002", CERT_URL)), {"birth_certificate_url": None}, fetcher
    )

    assert result.representations[0].metadata["document_type"] == (
        "birth_certificate_url"
    )


# ======================================================================
# TEST R - same URL, two employees
# ======================================================================


def test_one_url_referenced_by_two_employees_stays_separate():
    fetcher = CountingFetcher()
    _, result = run(
        rows(("EMP002", CERT_URL), ("EMP003", CERT_URL)),
        {"birth_certificate_url": "birth_certificate"},
        fetcher,
    )

    assert len(result.representations) == 2

    first, second = result.representations

    # Same bytes, so the same document. Never the same attachment.
    assert first.metadata["document_id"] == second.metadata["document_id"]
    assert first.representation_id != second.representation_id
    assert first.vector_id != second.vector_id
    assert {
        item.metadata["business_key_value"] for item in result.representations
    } == {"EMP002", "EMP003"}


# ======================================================================
# TEST S / T - the URL is not the identity
# ======================================================================


def test_two_urls_serving_the_same_bytes_share_content_identity():
    """Attachment identity is the parent and field, never the raw URL."""
    fetcher = CountingFetcher()
    _, first = run(
        rows(("EMP002", "https://a.example.test/c.pdf")),
        {"birth_certificate_url": "birth_certificate"}, fetcher,
    )
    _, second = run(
        rows(("EMP002", "https://b.example.test/moved.pdf")),
        {"birth_certificate_url": "birth_certificate"}, fetcher,
    )

    one = first.representations[0]
    two = second.representations[0]

    assert one.metadata["document_id"] == two.metadata["document_id"]
    # The same employee's same field: one attachment, not two.
    assert one.representation_id == two.representation_id
    # The provenance still records that the host changed.
    assert one.metadata["source_url_host"] != two.metadata["source_url_host"]


def test_one_url_serving_changed_bytes_produces_new_content_identity():
    """``document_id`` is the content hash, never a hash of the URL."""
    first_fetcher = CountingFetcher(pdf_bytes("BIRTH CERTIFICATE version one"))
    second_fetcher = CountingFetcher(pdf_bytes("BIRTH CERTIFICATE amended"))

    _, first = run(
        rows(("EMP002", CERT_URL)), {"birth_certificate_url": None}, first_fetcher
    )
    _, second = run(
        rows(("EMP002", CERT_URL)), {"birth_certificate_url": None}, second_fetcher
    )

    assert (
        first.representations[0].metadata["document_id"]
        != second.representations[0].metadata["document_id"]
    )
    assert "amended" in second.representations[0].text_for_ai


# ======================================================================
# TEST Q / value handling at the record level
# ======================================================================


def test_a_declared_field_holding_a_non_url_is_reported_not_requested():
    fetcher = CountingFetcher()
    _, result = run(
        rows(("EMP002", "not a url")), {"birth_certificate_url": None}, fetcher
    )

    assert fetcher.calls == []
    assert result.representations == ()
    assert result.skipped == 1
    assert any("absolute URL" in warning for warning in result.warnings)


def test_a_row_with_no_url_is_skipped_silently():
    fetcher = CountingFetcher()
    _, result = run(rows(("EMP002", None)), {"birth_certificate_url": None}, fetcher)

    assert fetcher.calls == []
    assert result.fields_seen == 0


# ======================================================================
# TEST X / DR19 - the scalar record survives every failure
# ======================================================================


@pytest.mark.parametrize(
    "policy, fetcher_factory",
    [
        (UrlSafetyPolicy(), lambda: CountingFetcher()),
        (UrlSafetyPolicy(enabled=True, max_bytes=8), lambda: CountingFetcher()),
        (
            UrlSafetyPolicy(enabled=True),
            lambda: (_ for _ in ()).throw,  # placeholder replaced below
        ),
    ],
)
def test_the_scalar_record_survives_a_refused_or_failed_asset(
    policy, fetcher_factory
):
    records = rows(("EMP002", CERT_URL))
    canonical = canonicalise(records, ("birth_certificate_url",))

    def failing(validated):
        raise TimeoutError("read timed out")

    fetcher = failing if policy.max_bytes > 1000 and policy.enabled else CountingFetcher()
    result = extract_record_assets(
        records, canonical, EMPLOYEES, (),
        asset_url_fields={"birth_certificate_url": None},
        url_policy=policy, fetcher=fetcher, resolver=resolver,
    )

    # The employee is still a perfectly good canonical record.
    assert canonical[0].record_id == "erp:legacy_hr:employees:emp002"
    assert canonical[0].normalized_data["full_name"] == "Name EMP002"
    # And nothing was fabricated for the document.
    assert result.representations == ()
    assert result.skipped == 1


# ======================================================================
# DR3 - the URL value stays out of scalar AI text
# ======================================================================


def test_a_declared_asset_url_is_not_scalar_content():
    canonical = canonicalise(
        rows(("EMP002", SIGNED_URL)), ("birth_certificate_url",)
    )[0]

    assert "birth_certificate_url" not in canonical.normalized_data
    assert SECRET not in json.dumps(canonical.to_json_dict(), default=str)
    # The field is still recorded as structure.
    assert "birth_certificate_url" in canonical.metadata.get("binary_fields", [])


def test_an_undeclared_url_remains_ordinary_scalar_content():
    """Only DECLARED asset fields get this treatment."""
    canonical = canonicalise(rows(("EMP002", CERT_URL)), ())[0]

    assert canonical.normalized_data["website"] == (
        "https://intranet.example.test/staff"
    )
    assert canonical.normalized_data["birth_certificate_url"] == CERT_URL


# ======================================================================
# TEST O / Y - no secret anywhere in the pipeline output
# ======================================================================


def test_a_signed_url_never_reaches_a_representation():
    fetcher = CountingFetcher()
    canonical, result = run(
        rows(("EMP002", SIGNED_URL)),
        {"birth_certificate_url": "birth_certificate"},
        fetcher,
    )
    surface = json.dumps(
        [item.to_dict() for item in result.representations]
        + [record.to_json_dict() for record in canonical]
        + [asset.to_dict() for asset in result.assets]
        + list(result.warnings),
        default=str,
    )

    assert SECRET not in surface
    assert "token=" not in surface
    assert SIGNED_URL not in surface

    # The redacted provenance IS kept - on the metadata, which
    # ``AIRepresentation.to_dict`` deliberately omits because it is the
    # privacy-safe summary. Checked at its real location.
    metadata = result.representations[0].metadata

    assert metadata["source_url_host"] == "assets.example.test"
    assert metadata["source_url_path"] == "/emp002-cert.pdf"
    assert SECRET not in json.dumps(dict(metadata))


def test_the_full_url_is_not_in_the_vector_payload():
    from erp_pipeline.ai.service import _carried_identity
    from erp_pipeline.storage.migration import _payload_for
    from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier

    fetcher = CountingFetcher()
    _, result = run(
        rows(("EMP002", SIGNED_URL)), {"birth_certificate_url": None}, fetcher
    )
    carried = _carried_identity(result.representations[0], filter_token_secret=None)
    state = StorageRecordMetadata(
        representation_id="r", embedding_id="e", vector_id="v",
        current_tier=StorageTier.HOT, content_hash="h", model_id="m",
        dimension=4,
        **{k: v for k, v in carried.items()
           if k in {"source_system_id", "source_entity", "document_id",
                    "content_kind", "parent_record_id", "source_field",
                    "business_key_name", "business_key_value", "document_type"}},
    )
    payload = json.dumps(_payload_for(state), default=str)

    assert SECRET not in payload
    assert "token=" not in payload
    assert SIGNED_URL not in payload


# ======================================================================
# TEST W - a schema job never fetches
# ======================================================================


def test_a_schema_job_never_fetches_a_url_field(tmp_path):
    """A schema describes the FIELD. There is no row value to retrieve."""
    from erp_pipeline.schemas.enums import SchemaOrigin
    from erp_pipeline.schemas.source_models import SourceSchema

    fetcher = CountingFetcher()
    representations = InMemoryRepresentationStore()
    services = PipelineServices(
        records=InMemoryCanonicalStore(),
        representations=representations,
        storage=PatchedStorage(
            hot=InProcessTier(), state_store=InMemoryTierStateStore()
        ),
        embedding=EmbeddingService(DeterministicTestModel(dimension=DIMENSION)),
        remote_asset_policy=POLICY,
        remote_asset_fetcher=fetcher,
        remote_asset_resolver=resolver,
    )
    schema = SourceSchema(
        schema_id="sch_1", source_system_id="legacy_hr", schema_name="public",
        origin=SchemaOrigin.DISCOVERED, entities=(EMPLOYEES,),
    )
    services.schema_cache[schema.schema_id] = schema
    orchestration = OrchestrationService(
        services=services, job_store=InMemoryJobStore(),
        executor=InlineJobExecutor(),
    )

    job_id, status, error = orchestration.index_schema(schema.schema_id)

    assert error is None
    assert status == "succeeded"
    # The field is described...
    stored = [
        representations.get(key).text_for_ai
        for key in representations.list_ids()
    ]
    assert any("birth_certificate_url" in text for text in stored)
    # ...and nothing was fetched.
    assert fetcher.calls == []


# ======================================================================
# TEST Z - search and resolve, end to end
# ======================================================================


@pytest.fixture
def indexed(tmp_path):
    """EMP002 and EMP003 indexed from a declared remote certificate."""
    from fastapi.testclient import TestClient

    fetcher = CountingFetcher()
    representations = InMemoryRepresentationStore()
    storage = PatchedStorage(
        hot=InProcessTier(), state_store=InMemoryTierStateStore()
    )
    embedding = EmbeddingService(DeterministicTestModel(dimension=DIMENSION))
    services = PipelineServices(
        records=InMemoryCanonicalStore(), representations=representations,
        storage=storage, embedding=embedding,
    )
    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=OrchestrationService(
            services=services, job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        ),
    )

    records = rows(("EMP002", SIGNED_URL), ("EMP003", SIGNED_URL))
    canonical = canonicalise(records, ("birth_certificate_url",))
    result = extract_record_assets(
        records, canonical, EMPLOYEES, (),
        asset_url_fields={"birth_certificate_url": "birth_certificate"},
        url_policy=POLICY, fetcher=fetcher, resolver=resolver,
    )

    for record in canonical:
        from erp_pipeline.ai.representation import canonical_record_to_representation

        built = canonical_record_to_representation(record)
        representations.upsert(built)
        storage.store(embedding.embed_one(built))

    for representation in result.representations:
        representations.upsert(representation)
        storage.store(embedding.embed_one(representation))

    with TestClient(app) as client:
        yield client, storage, representations


def test_the_emp002_remote_certificate_resolves_to_its_text(indexed):
    client, _, _ = indexed
    hits = client.post(
        "/v1/search",
        json={
            "query": "birth certificate details",
            "top_k": 20,
            "filters": {
                "content_kind": "document_chunk",
                "business_key_name": "employee_id",
                "business_key_value": "EMP002",
                "document_type": "birth_certificate",
            },
        },
    ).json()["hits"]

    assert hits

    body = client.get(
        f"/v1/representations/{hits[0]['representation_id']}"
    ).json()

    assert "BIRTH CERTIFICATE" in body["text"]
    assert body["business_key_value"] == "EMP002"
    assert body["parent_record_id"] == "erp:legacy_hr:employees:emp002"
    assert body["source_field"] == "birth_certificate_url"


def test_the_emp002_filter_never_returns_emp003s_remote_certificate(indexed):
    client, _, _ = indexed

    for employee in ("EMP002", "EMP003"):
        hits = client.post(
            "/v1/search",
            json={
                "query": "certificate",
                "top_k": 20,
                "filters": {
                    "business_key_value": employee,
                    "document_type": "birth_certificate",
                },
            },
        ).json()["hits"]

        assert hits

        for hit in hits:
            body = client.get(
                f"/v1/representations/{hit['representation_id']}"
            ).json()

            assert body["business_key_value"] == employee


def test_no_secret_reaches_the_api_surface(indexed):
    client, storage, _ = indexed
    hits = client.post(
        "/v1/search",
        json={"query": "certificate", "top_k": 20,
              "filters": {"content_kind": "document_chunk"}},
    ).json()["hits"]
    surface = json.dumps(
        [dict(hit) for hit in hits]
        + [
            client.get(f"/v1/representations/{hit['representation_id']}").json()
            for hit in hits
        ]
    )

    assert SECRET not in surface
    assert "token=" not in surface


def test_no_new_filter_field_was_needed(indexed):
    """Remote origin is provenance. The existing filters already scope it."""
    from erp_pipeline.storage.filters import FILTERABLE_FIELDS

    assert "asset_origin" not in FILTERABLE_FIELDS
    assert "source_url_host" not in FILTERABLE_FIELDS
    # Canonical record_key is an identity filter; remote-asset provenance still
    # does not add an asset-specific filter field.
    assert "record_key" in FILTERABLE_FIELDS
    assert len(FILTERABLE_FIELDS) == 14
