"""End-to-end adaptation and the ``/v1/responses/adapt`` route (Phase 14)."""

from __future__ import annotations

import base64
import io
import json

import pytest

from erp_pipeline.response_adaptation import (
    AssetReference,
    ResponseAdaptationService,
    ResponseEnvelope,
    ResponseType,
)
from erp_pipeline.response_adaptation.assets import AssetOptions, UrlSafetyPolicy
from erp_pipeline.schemas.enums import SensitivityLevel

WRAPPED_INVOICE = {
    "result": {
        "inv_no": "INV-204",
        "cust_ref": "CUS-17",
        "total_amt": "45000.00",
        "curr": "LKR",
        "approval_status": "A",
        "row_version": 7,
        "etl_batch_id": "B-99",
        "created_by": "svc_acct",
    },
    "success": True,
    "server_time": "2026-08-22T09:00:00Z",
}


@pytest.fixture
def service() -> ResponseAdaptationService:
    return ResponseAdaptationService()


def envelope(**overrides) -> ResponseEnvelope:
    base = dict(
        query="How much is invoice INV-204 for and in what currency?",
        source_system_id="finance_erp",
        endpoint="/api/invoices/INV-204",
        http_status=200,
        content_type="application/json",
        body=WRAPPED_INVOICE,
    )
    base.update(overrides)

    return ResponseEnvelope(**base)


# ----------------------------------------------------------------------
# The structured path, end to end
# ----------------------------------------------------------------------


def test_a_wrapped_vendor_response_becomes_canonical_llm_context(service):
    result = service.adapt(envelope())

    assert result.success
    assert result.response_type is ResponseType.STRUCTURED
    assert result.entity_type == "invoice"
    assert result.report.wrapper_path == ("result",)
    assert result.llm_ready["invoice_id"] == "INV-204"
    assert result.llm_ready["currency"] == "LKR"
    assert result.llm_ready["amount"] == "45000.00"


def test_operational_noise_does_not_reach_the_model(service):
    result = service.adapt(envelope())

    for noise in ("row_version", "etl_batch_id", "created_by", "server_time"):
        assert noise not in result.llm_ready


def test_the_metrics_are_measured_from_the_real_payloads(service):
    result = service.adapt(envelope())
    metrics = result.transformation

    assert metrics.input_bytes > metrics.output_bytes > 0
    assert metrics.input_fields > metrics.selected_fields > 0
    assert 0.0 < metrics.field_reduction_ratio < 1.0
    assert 0.0 < metrics.size_reduction_ratio < 1.0
    assert metrics.processing_ms > 0.0


def test_the_output_is_json_serializable(service):
    """A Decimal reaching the API serializer would be an error at the edge."""
    result = service.adapt(envelope())

    json.dumps(result.to_dict())


def test_the_report_explains_every_removal(service):
    result = service.adapt(envelope())

    assert result.report.removed_by_reason
    assert sum(result.report.removed_by_reason.values()) > 0

    for decision in result.report.field_decisions:
        assert decision.reason
        assert set(decision.signals) == {"alias", "name", "entity", "identity"}


def test_authorization_headers_never_reach_provenance(service):
    """Provenance is stored and logged, so anything in it was chosen
    deliberately. An allow-list is the only way that stays true when an ERP
    invents a new header name."""
    result = service.adapt(
        envelope(
            headers={
                "Authorization": "Bearer SECRET-TOKEN",
                "X-Api-Key": "SECRET-KEY",
                "Cookie": "session=SECRET",
                "Content-Type": "application/json",
                "ETag": 'W/"9"',
            }
        )
    )
    rendered = json.dumps(result.to_dict())

    assert "SECRET-TOKEN" not in rendered
    assert "SECRET-KEY" not in rendered
    assert "session=SECRET" not in rendered
    assert result.provenance.headers["Content-Type"] == "application/json"


def test_provenance_records_the_configuration_that_produced_the_output(service):
    result = service.adapt(envelope())

    assert result.provenance.config_fingerprint
    assert result.provenance.engine_version
    assert result.provenance.canonical_record_id.startswith("erp:finance_erp:")


def test_sensitivity_is_consumed_from_the_envelope_not_inferred(service):
    from dataclasses import replace

    from erp_pipeline.response_adaptation.models import AdaptationPolicy

    options = replace(
        service.options,
        policy=AdaptationPolicy(
            blocked_sensitivities=frozenset({SensitivityLevel.RESTRICTED})
        ),
    )

    allowed = service.adapt(envelope(sensitivity=SensitivityLevel.INTERNAL), options)
    blocked = service.adapt(envelope(sensitivity=SensitivityLevel.RESTRICTED), options)

    assert allowed.llm_ready
    assert blocked.llm_ready == {}
    assert any("withheld" in warning for warning in blocked.warnings)


def test_a_collection_response_says_how_many_records_it_left_behind(service):
    """Adapting one row of forty and presenting it as the answer would look
    complete while being wrong."""
    result = service.adapt(
        envelope(body={"data": [{"inv_no": "INV-1"}, {"inv_no": "INV-2"}],
                       "count": 2})
    )

    assert result.success
    assert any("2 records" in warning for warning in result.warnings)


def test_an_unreadable_structured_body_fails_honestly(service):
    result = service.adapt(envelope(body="not a record", content_type="application/json"))

    assert result.success is False
    assert result.warnings
    assert result.llm_ready == {}


def test_disabling_erp_mapping_falls_back_to_source_field_names(service):
    from dataclasses import replace

    result = service.adapt(
        envelope(), replace(service.options, enable_erp_mapping=False)
    )

    assert result.entity_type is None
    assert "inv_no" in result.llm_ready or "total_amt" in result.llm_ready
    assert "invoice_id" not in result.llm_ready


def test_the_ablation_switch_keeps_every_field(service):
    """With relevance off, this is the GENERIC baseline: still unwrapped, still
    canonicalised, but with no query-aware selection."""
    from dataclasses import replace

    full = service.adapt(
        envelope(), replace(service.options, enable_relevance_selection=False)
    )
    selective = service.adapt(envelope())

    assert len(full.llm_ready) > len(selective.llm_ready)
    assert full.transformation.output_bytes > selective.transformation.output_bytes


def test_two_identical_requests_produce_identical_output(service):
    first = service.adapt(envelope()).to_dict()
    second = service.adapt(envelope()).to_dict()

    for payload in (first, second):
        payload["transformation"].pop("processing_ms")
        payload["provenance"].pop("adapted_at", None)

    assert first == second


# ----------------------------------------------------------------------
# Non-structured responses
# ----------------------------------------------------------------------


def test_an_image_response_is_adapted_into_an_asset():
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (100, 40), "white").save(buffer, "PNG")

    result = ResponseAdaptationService().adapt(
        ResponseEnvelope(
            query="what does this receipt show?",
            content_type="image/png",
            raw=buffer.getvalue(),
            endpoint="/api/receipts/1",
        )
    )

    assert result.response_type is ResponseType.IMAGE
    assert result.assets[0].llm_directly_readable is True
    assert result.success


def test_an_unsupported_binary_response_still_succeeds():
    """Truthfully describing unreadable content is what stops a model
    inventing its contents."""
    result = ResponseAdaptationService().adapt(
        ResponseEnvelope(content_type="application/zip",
                         raw=b"PK\x03\x04" + b"\x00" * 50)
    )

    assert result.success
    assert result.assets[0].kind.value == "unsupported_binary"
    assert result.assets[0].llm_directly_readable is False


def test_a_refused_asset_url_does_not_discard_the_json_that_adapted(service):
    """Partial success is the normal case, and dropping good fields over a
    blocked image would be the wrong trade every time."""
    result = service.adapt(
        envelope(
            asset_urls=(AssetReference(url="https://169.254.169.254/x.png",
                                       label="scan"),)
        )
    )

    assert result.success
    assert result.is_partial
    assert result.llm_ready["invoice_id"] == "INV-204"
    assert result.assets[-1].kind.value == "refused"


def test_asset_urls_are_never_fetched_without_a_configured_fetcher(service):
    result = service.adapt(
        envelope(asset_urls=(AssetReference(url="https://cdn.example.com/a.png"),))
    )

    assert result.assets[-1].kind.value == "refused"
    assert any("Refused" in warning or "refused" in warning
               for warning in result.warnings)


def test_a_permitted_asset_url_is_adapted_alongside_the_json():
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (80, 30), "white").save(buffer, "PNG")
    payload = buffer.getvalue()

    from erp_pipeline.response_adaptation.assets import FetchedAsset

    service = ResponseAdaptationService(
        asset_options=AssetOptions(url_policy=UrlSafetyPolicy(enabled=True)),
        fetcher=lambda validated: FetchedAsset(payload, "image/png"),
        resolver=lambda host: ["93.184.216.34"],
    )
    result = service.adapt(
        envelope(asset_urls=(AssetReference(url="https://cdn.example.com/a.png",
                                            label="scan"),))
    )

    assert result.llm_ready["invoice_id"] == "INV-204"
    assert result.assets[-1].kind.value == "image"


# ----------------------------------------------------------------------
# The HTTP route
# ----------------------------------------------------------------------


@pytest.fixture
def client():
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from erp_pipeline.api.main import create_app

    return fastapi_testclient.TestClient(create_app())


def test_the_endpoint_adapts_a_posted_response(client):
    response = client.post(
        "/v1/responses/adapt",
        json={
            "query": "How much is invoice INV-204 for?",
            "source_system_id": "finance_erp",
            "endpoint": "/api/invoices/INV-204",
            "http_status": 200,
            "content_type": "application/json",
            "body": WRAPPED_INVOICE,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["entity_type"] == "invoice"
    assert body["llm_ready"]["invoice_id"] == "INV-204"
    assert body["transformation"]["field_reduction_ratio"] > 0


def test_the_endpoint_never_echoes_an_authorization_header(client):
    response = client.post(
        "/v1/responses/adapt",
        json={
            "query": "how much?",
            "content_type": "application/json",
            "headers": {"Authorization": "Bearer SECRET-TOKEN"},
            "body": WRAPPED_INVOICE,
        },
    )

    assert "SECRET-TOKEN" not in response.text


def test_the_endpoint_accepts_a_base64_binary_response(client):
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (60, 20), "white").save(buffer, "PNG")

    response = client.post(
        "/v1/responses/adapt",
        json={
            "content_type": "image/png",
            "body_base64": base64.b64encode(buffer.getvalue()).decode(),
        },
    )

    assert response.status_code == 200
    assert response.json()["response_type"] == "image"


def test_the_endpoint_refuses_a_request_with_no_body_at_all(client):
    response = client.post("/v1/responses/adapt", json={"query": "how much?"})

    assert response.status_code == 422
    assert response.json()["success"] is False


def test_the_endpoint_refuses_malformed_base64(client):
    response = client.post("/v1/responses/adapt", json={"body_base64": "!!!not b64"})

    assert response.status_code == 422


def test_the_endpoint_honours_a_field_budget(client):
    response = client.post(
        "/v1/responses/adapt",
        json={
            "content_type": "application/json",
            "body": WRAPPED_INVOICE,
            "options": {"max_fields": 1, "enable_relevance_selection": False},
        },
    )

    assert response.status_code == 200
    assert len(response.json()["llm_ready"]) == 1


def test_the_endpoint_appears_in_the_openapi_document(client):
    document = client.get("/openapi.json").json()

    assert "/v1/responses/adapt" in document["paths"]
