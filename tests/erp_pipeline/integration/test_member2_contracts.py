"""Contract E: Member 2 executes the ERP, then Member 4 adapts what came back.

THE ORDERING IS THE CONTRACT
----------------------------
Member 2 chooses the operation, holds the credentials, and calls the ERP.
Member 4 sees the RESULT and nothing else. Every test in this file therefore
executes against the fake ERP first and only then posts to
``/v1/responses/adapt`` - and asserts afterwards that Member 4 did not cause a
second execution.
"""

from __future__ import annotations

import base64

import pytest

from tests.erp_pipeline.integration.conftest import (
    CERTIFICATE_LINES,
    build_pdf,
    build_png_of_text,
)
from tests.erp_pipeline.integration.fakes import load_fixture


class TestContractEJsonAdaptation:
    """The ordinary live read: JSON in, AI-ready content out."""

    def test_a_live_employee_response_adapts(self, member2):
        raw = member2.execute("member2_employee_response.json")
        response = member2.adapt(raw, query="What is EMP002's employment status?")

        assert response.status_code == 200, response.text
        body = response.json()

        assert body["success"] is True, body
        assert body["llm_ready"], "adaptation produced no AI-ready content"

    def test_the_erp_was_executed_exactly_once(self, member2):
        raw = member2.execute("member2_employee_response.json")
        member2.adapt(raw, query="What is EMP002's employment status?")

        assert member2.executions == 1
        assert member2.erp.calls == 1, (
            "the ERP was called more than once; Member 4 must never re-execute"
        )

    def test_the_relevant_content_survives_adaptation(self, member2):
        raw = member2.execute("member2_employee_response.json")
        body = member2.adapt(
            raw, query="What is EMP002's employment status?"
        ).json()

        rendered = str(body["llm_ready"])

        assert "EMP002" in rendered
        assert "ACTIVE" in rendered.upper()

    def test_source_provenance_is_preserved(self, member2):
        raw = member2.execute("member2_employee_response.json")
        body = member2.adapt(raw, query="employment status").json()

        provenance = body["provenance"]

        assert provenance["source_system_id"] == "legacy_hr"
        assert provenance["endpoint"] == "/api/hr/employees/EMP002"
        assert provenance["http_status"] == 200

    def test_the_response_carries_transformation_metrics(self, member2):
        raw = member2.execute("member2_employee_response.json")
        body = member2.adapt(raw, query="employment status").json()

        assert "metrics" in body or "transformation" in body, body.keys()

    def test_an_entity_type_is_reported_where_the_model_knows_one(self, member2):
        raw = member2.execute("member2_employee_response.json")
        body = member2.adapt(raw, query="employment status").json()

        # Reported when recognised, honestly absent when not. Either is a valid
        # contract answer; a fabricated entity type would not be.
        assert "entity_type" in body

    def test_a_business_error_response_adapts_without_a_retry(self, member2):
        """Member 4 adapts a 404 body. It does not re-issue the request."""
        raw = member2.execute("member2_error_response.json")
        response = member2.adapt(raw, query="Show me EMP999")

        assert response.status_code == 200, response.text
        assert member2.erp.calls == 1, "Member 4 retried the ERP business request"

    def test_a_write_confirmation_adapts(self, member2):
        raw = member2.execute("member2_invoice_response.json")
        body = member2.adapt(raw, query="Release payment INV-204").json()

        assert body["success"] is True
        assert "INV-204" in str(body["llm_ready"])


class TestContractECollectionLimitation:
    """The Phase 14 list limitation, MEASURED rather than assumed or fixed.

    Phase 11 is explicitly not the place to redesign collection handling. What
    it must establish is whether the limitation is silent - because a partial
    answer that looks complete is a correctness problem, while a partial answer
    that says so is a documented bound.
    """

    def test_only_the_first_record_of_a_collection_is_adapted(self, member2):
        raw = member2.execute("member2_collection_response.json")
        body = member2.adapt(raw, query="List the employees").json()

        rendered = str(body["llm_ready"])

        assert "EMP001" in rendered, "the first record was not adapted"
        assert "EMP003" not in rendered, (
            "the measured limitation has changed; the documentation and the "
            "Phase 14 artifact describe first-record-only adaptation"
        )

    def test_the_caller_is_warned_that_records_were_dropped(self, member2):
        """The limitation is bounded because it is DECLARED."""
        raw = member2.execute("member2_collection_response.json")
        body = member2.adapt(raw, query="List the employees").json()

        assert any(
            "records" in warning and "first" in warning
            for warning in body["warnings"]
        ), body["warnings"]

    def test_the_warning_states_how_many_records_were_present(self, member2):
        raw = member2.execute("member2_collection_response.json")
        body = member2.adapt(raw, query="List the employees").json()

        assert any("3" in warning for warning in body["warnings"]), body["warnings"]


class TestContractEBinaryAdaptation:
    """PDF and image bodies, base64-encoded by Member 2."""

    @pytest.fixture
    def pdf_response(self):
        return {
            "endpoint": "/api/hr/employees/EMP002/certificate",
            "http_status": 200,
            "content_type": "application/pdf",
        }

    def test_a_pdf_body_extracts_its_text_into_an_asset(self, member2, pdf_response):
        """Where a binary body's text actually lands.

        NOT in ``llm_ready``. A PDF carries no structured fields, so field
        selection has nothing to select and ``llm_ready`` is legitimately
        empty; the extracted text is an ASSET. Member 2 must read
        ``assets[].text`` for binary responses, which is exactly the kind of
        shape mismatch this phase exists to find before Member 2 does.
        """
        payload = base64.b64encode(build_pdf(CERTIFICATE_LINES)).decode("ascii")

        response = member2.adapt(
            pdf_response,
            query="EMP002 birth certificate",
            body_base64=payload,
            content_type="application/pdf",
        )

        assert response.status_code == 200, response.text
        body = response.json()

        assert body["response_type"] == "document"
        assert body["assets"], "a PDF body produced no asset"

        asset = body["assets"][0]

        assert asset["extraction_status"] == "extracted"
        assert "BIRTH CERTIFICATE" in asset["text"].upper()
        assert asset["page_count"] == 1

    def test_a_binary_response_is_reported_as_partial(self, member2, pdf_response):
        """``partial`` is how the caller learns not to expect fields."""
        payload = base64.b64encode(build_pdf(CERTIFICATE_LINES)).decode("ascii")

        body = member2.adapt(
            pdf_response,
            query="EMP002 birth certificate",
            body_base64=payload,
            content_type="application/pdf",
        ).json()

        assert body["success"] is True
        assert body["partial"] is True
        assert body["llm_ready"] == {}

    def test_the_asset_reports_a_content_hash_and_size(self, member2, pdf_response):
        payload = base64.b64encode(build_pdf(CERTIFICATE_LINES)).decode("ascii")

        asset = member2.adapt(
            pdf_response,
            query="EMP002 birth certificate",
            body_base64=payload,
            content_type="application/pdf",
        ).json()["assets"][0]

        assert len(asset["content_hash"]) == 64
        assert asset["size_bytes"] > 0
        assert asset["llm_directly_readable"] is False

    def test_an_image_body_adapts(self, member2, pdf_response):
        payload = base64.b64encode(
            build_png_of_text("EMP002 CERTIFICATE")
        ).decode("ascii")

        response = member2.adapt(
            {**pdf_response, "content_type": "image/png"},
            query="EMP002 certificate",
            body_base64=payload,
            content_type="image/png",
        )

        assert response.status_code == 200, response.text
        assert response.json()["response_type"], "no response type was reported"

    def test_binary_adaptation_writes_no_temporary_files(
        self, member2, pdf_response, tmp_path, monkeypatch
    ):
        """Phase 10 moved this extraction in-memory. It must stay there."""
        import tempfile

        scratch = tmp_path / "tempwatch"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))

        payload = base64.b64encode(build_pdf(CERTIFICATE_LINES)).decode("ascii")
        member2.adapt(
            pdf_response,
            query="EMP002 birth certificate",
            body_base64=payload,
            content_type="application/pdf",
        )

        leftovers = list(scratch.iterdir())

        assert leftovers == [], f"adaptation left temporary files: {leftovers}"

    def test_the_raw_bytes_are_not_echoed_back_in_the_response(
        self, member2, pdf_response
    ):
        payload = base64.b64encode(build_pdf(CERTIFICATE_LINES)).decode("ascii")

        body = member2.adapt(
            pdf_response,
            query="EMP002 birth certificate",
            body_base64=payload,
            content_type="application/pdf",
        ).json()

        assert payload[:200] not in str(body), (
            "the base64 body was echoed back into the adapted response"
        )


class TestContractEMember4ExecutesNothing:
    """Member 4's side of the boundary, asserted from the outside."""

    def test_adaptation_needs_no_erp_call_at_all(self, member2):
        """Adapting a recorded response must not touch the ERP."""
        recorded = load_fixture("member2_employee_response.json")
        recorded.pop("_comment", None)

        before = member2.erp.calls
        response = member2.adapt(recorded, query="employment status")

        assert response.status_code == 200
        assert member2.erp.calls == before, (
            "Member 4 caused an ERP call while adapting an already-fetched "
            "response"
        )

    def test_member4_does_not_choose_the_endpoint(self, member2):
        """The endpoint is provenance Member 2 supplies, not a choice.

        Sent deliberately as an opaque string Member 4 has no vocabulary for:
        if it were selecting operations, an unrecognised endpoint would have to
        fail or be rewritten. It is echoed back untouched instead.
        """
        raw = member2.execute("member2_employee_response.json")
        raw["endpoint"] = "/api/some/opaque/path/Member4/never/heard/of"

        body = member2.adapt(raw, query="employment status").json()

        assert (
            body["provenance"]["endpoint"]
            == "/api/some/opaque/path/Member4/never/heard/of"
        )

    def test_an_unreachable_erp_is_member2s_problem_not_member4s(self, member2):
        """Member 4 adapts a 503 body; it does not wait, retry or escalate."""
        response = member2.adapt(
            {
                "endpoint": "/api/hr/employees/EMP002",
                "http_status": 503,
                "content_type": "application/json",
                "body": {"error": "SERVICE_UNAVAILABLE"},
            },
            query="employment status",
        )

        assert response.status_code == 200, response.text
        assert member2.erp.calls == 0
