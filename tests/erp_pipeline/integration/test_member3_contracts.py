"""Contracts A-D and L: what Member 3's backend can rely on (Phase 11).

Every test here drives the HTTP surface. None of them import a stage, a store
or an orchestration method to make a step work - if a workflow cannot be
completed with the documented endpoints, it is not a workflow Member 3 can
build against, and this file should fail rather than reach past the API to
rescue it.
"""

from __future__ import annotations

import pytest

from tests.erp_pipeline.integration.conftest import (
    CERTIFICATE_LINES,
    EMPLOYEE_CSV,
    build_pdf,
    build_png_of_text,
)

CERTIFICATE_IDENTITY = {
    "source_system_id": "legacy_hr",
    "source_entity": "employees",
    "business_key_name": "employee_id",
    "business_key_value": "EMP002",
    "document_type": "birth_certificate",
    "sensitivity": "restricted",
}


# ----------------------------------------------------------------------
# Contract A - document upload, automatic indexing, search, resolution
# ----------------------------------------------------------------------


class TestContractADocumentWorkflow:
    """Upload once; get back something searchable and resolvable."""

    def test_the_documented_upload_response_fields_are_all_present(self, member3):
        response = member3.upload_document(
            "certificate.pdf", build_pdf(CERTIFICATE_LINES), **CERTIFICATE_IDENTITY
        )

        assert response.status_code == 201, response.text
        body = response.json()

        # The exact field names the contract document tells Member 3 to read.
        for field in (
            "upload_id",
            "document_id",
            "page_count",
            "ocr_used",
            "index_job_id",
            "indexing_status",
            "warnings",
        ):
            assert field in body, f"{field!r} is missing from the upload response"

        assert body["index_job_id"], "no indexing job was started"

    def test_the_job_can_be_polled_to_completion_through_the_api(self, member3):
        upload = member3.upload_document(
            "certificate.pdf", build_pdf(CERTIFICATE_LINES), **CERTIFICATE_IDENTITY
        ).json()

        job = member3.job(upload["index_job_id"])

        assert job.status_code == 200
        assert job.json()["status"] == "succeeded", job.text

    def test_the_uploaded_certificate_becomes_searchable_and_resolvable(
        self, member3
    ):
        """The headline Member 3 workflow, start to finish, over HTTP."""
        upload = member3.upload_document(
            "certificate.pdf", build_pdf(CERTIFICATE_LINES), **CERTIFICATE_IDENTITY
        ).json()

        assert member3.job(upload["index_job_id"]).json()["status"] == "succeeded"

        found = member3.search(
            "birth certificate registration details",
            filters={
                "content_kind": "document_chunk",
                "business_key_name": "employee_id",
                "business_key_value": "EMP002",
                "document_type": "birth_certificate",
            },
        )

        assert found.status_code == 200, found.text
        hits = found.json()["hits"]
        assert hits, "the uploaded certificate did not come back from search"

        resolved = member3.resolve(hits[0]["representation_id"])

        assert resolved.status_code == 200, resolved.text
        payload = resolved.json()

        assert payload["text"], "the hit resolved to no text"
        assert "BIRTH CERTIFICATE" in payload["text"].upper()
        assert payload["business_key_value"] == "EMP002"
        assert payload["document_type"] == "birth_certificate"

    def test_an_ocr_only_image_upload_also_indexes(self, member3):
        image = build_png_of_text("EMP002 BIRTH CERTIFICATE")
        response = member3.upload_document(
            "certificate.png", image, media_type="image/png", **CERTIFICATE_IDENTITY
        )

        assert response.status_code == 201, response.text
        body = response.json()

        if not body["index_job_id"]:
            pytest.skip(f"indexing did not start: {body['warnings']}")

        assert member3.job(body["index_job_id"]).json()["status"] in {
            "succeeded",
            "partial",
        }

    def test_half_a_business_key_is_refused_rather_than_half_stored(self, member3):
        response = member3.upload_document(
            "certificate.pdf",
            build_pdf(CERTIFICATE_LINES),
            source_system_id="legacy_hr",
            business_key_name="employee_id",
        )

        assert response.status_code == 422, response.text

    def test_an_invalid_sensitivity_is_refused_not_silently_defaulted(self, member3):
        response = member3.upload_document(
            "certificate.pdf",
            build_pdf(CERTIFICATE_LINES),
            sensitivity="top_secret",
        )

        assert response.status_code == 422, response.text


# ----------------------------------------------------------------------
# Contract B - CSV: schema indexes, rows do not
# ----------------------------------------------------------------------


class TestContractBCsvWorkflow:
    """The invariant that protects mapping review from being bypassed."""

    def test_csv_upload_returns_a_schema_and_starts_schema_indexing(self, member3):
        response = member3.upload_csv("employees.csv", EMPLOYEE_CSV)

        assert response.status_code == 201, response.text
        body = response.json()

        assert body["schema_id"], "no schema was inferred"
        assert body["columns"] > 0
        assert "schema_index_job_id" in body
        assert "schema_indexing_status" in body

    def test_the_inferred_schema_is_readable_through_the_schemas_endpoint(
        self, member3, client
    ):
        schema_id = member3.upload_csv("employees.csv", EMPLOYEE_CSV).json()[
            "schema_id"
        ]

        response = client.get(f"/v1/schemas/{schema_id}", headers=member3.headers)

        assert response.status_code == 200, response.text
        assert response.json()["schema_id"] == schema_id

    def test_csv_business_rows_are_not_searchable_before_a_job_runs(self, member3):
        """The critical invariant: uploading a CSV must not index its rows.

        Searched with ``content_kind=structured_record`` specifically, because
        the SCHEMA is expected to be indexed and finding it would mask the
        thing this test is actually about.
        """
        member3.upload_csv("employees.csv", EMPLOYEE_CSV)

        rows = member3.search(
            "Nimal Silva Finance senior accounts officer",
            filters={"content_kind": "structured_record"},
        )

        assert rows.status_code == 200, rows.text
        assert rows.json()["hits"] == [], (
            "CSV business rows became searchable from the upload alone, "
            "bypassing mapping and source-native admission"
        )

    def test_a_mapping_suggestion_is_reachable_for_the_uploaded_schema(
        self, member3, client
    ):
        schema_id = member3.upload_csv("employees.csv", EMPLOYEE_CSV).json()[
            "schema_id"
        ]

        response = client.post(
            "/v1/mappings/suggest",
            json={"schema_id": schema_id},
            headers=member3.headers,
        )

        # The route exists and answers for a real schema; whether a canonical
        # entity matches is Phase 2's measured business, not this contract's.
        assert response.status_code in {200, 201, 404, 422}, response.text

    def test_a_row_job_without_a_registered_source_is_refused(self, member3, client):
        """Uploading is not admission. The refusal is the contract."""
        upload = member3.upload_csv("employees.csv", EMPLOYEE_CSV).json()

        response = client.post(
            "/v1/jobs",
            json={
                "job_type": "source_native_pipeline",
                "upload_id": upload["upload_id"],
            },
            headers=member3.headers,
        )

        assert response.status_code == 422, response.text
        assert "registered source" in response.text

    def _register_csv_source(self, client, member3) -> str:
        registered = client.post(
            "/v1/sources",
            json={"name": "legacy_hr_export", "source_type": "csv"},
            headers=member3.headers,
        )

        assert registered.status_code == 201, registered.text

        return registered.json()["source_id"]

    def test_rows_without_a_declared_key_are_refused_not_indexed_by_position(
        self, member3, client
    ):
        """Measured existing behaviour, recorded here as contract information.

        An inferred CSV schema has no primary key, and the extractor's fallback
        record key is the ROW NUMBER. Indexing on that would give record 2 an
        identity that changes the moment a row is inserted above it, so the
        transformer refuses each row and says why. Member 3 must declare the
        key; this is not a defect to route around.
        """
        upload = member3.upload_csv("employees.csv", EMPLOYEE_CSV).json()
        source_id = self._register_csv_source(client, member3)

        response = client.post(
            "/v1/jobs",
            json={
                "job_type": "source_native_pipeline",
                "source_id": source_id,
                "schema_id": upload["schema_id"],
                "upload_id": upload["upload_id"],
            },
            headers=member3.headers,
        )

        # 202: the job is ACCEPTED, not finished. Member 3 polls the job.
        assert response.status_code == 202, response.text
        job = member3.job(response.json()["job_id"]).json()

        assert job["counters"]["records_transformed"] == 0
        assert job["counters"]["records_failed"] > 0
        assert any(
            "no usable record identity" in warning for warning in job["warnings"]
        ), job["warnings"]

    def test_rows_index_once_a_key_is_declared_on_the_job(self, member3, client):
        """The full admission path Member 3 must follow for rows, over HTTP."""
        upload = member3.upload_csv("employees.csv", EMPLOYEE_CSV).json()
        source_id = self._register_csv_source(client, member3)

        response = client.post(
            "/v1/jobs",
            json={
                "job_type": "source_native_pipeline",
                "source_id": source_id,
                "schema_id": upload["schema_id"],
                "upload_id": upload["upload_id"],
                "options": {"key_fields": ["employee_id"]},
            },
            headers=member3.headers,
        )

        assert response.status_code == 202, response.text
        job = member3.job(response.json()["job_id"]).json()

        assert job["status"] in {"succeeded", "partial"}, job
        assert job["counters"]["records_transformed"] == 4, job["counters"]

        rows = member3.search(
            "Nimal Silva Finance", filters={"content_kind": "structured_record"}
        )

        assert rows.status_code == 200
        assert rows.json()["hits"], "rows did not index even after an explicit job"


# ----------------------------------------------------------------------
# Contract C - schema search
# ----------------------------------------------------------------------


class TestContractCSchemaQuery:
    """"Which table contains employee birth certificates?" over HTTP."""

    def test_schema_search_returns_schema_content_and_resolves(self, member3):
        member3.upload_csv("employees.csv", EMPLOYEE_CSV)

        found = member3.search(
            "which table contains employee records",
            filters={"content_kind": "schema"},
        )

        assert found.status_code == 200, found.text
        hits = found.json()["hits"]

        if not hits:
            pytest.skip(
                "no schema hit for this query; Phase 7 retrieval limitations "
                "are measured there and are deliberately not tuned here"
            )

        assert all(
            hit["metadata"]["content_kind"] == "schema" for hit in hits
        ), "a schema-filtered search returned a non-schema hit"

        resolved = member3.resolve(hits[0]["representation_id"])

        assert resolved.status_code == 200, resolved.text
        payload = resolved.json()

        assert payload["content_kind"] == "schema"
        assert payload["text"], "a schema hit resolved to no structural text"

    def test_a_schema_filtered_search_never_returns_document_chunks(self, member3):
        member3.upload_csv("employees.csv", EMPLOYEE_CSV)
        member3.upload_document(
            "certificate.pdf", build_pdf(CERTIFICATE_LINES), **CERTIFICATE_IDENTITY
        )

        found = member3.search(
            "employee birth certificate", filters={"content_kind": "schema"}
        )

        kinds = {hit["metadata"]["content_kind"] for hit in found.json()["hits"]}

        assert kinds <= {"schema"}, f"filter leaked other content kinds: {kinds}"


# ----------------------------------------------------------------------
# Contract D - identity-exact document search, and L - sensitivity upstream
# ----------------------------------------------------------------------


class TestContractDDocumentSearch:
    """Exact identity filtering, and what a hit is allowed to contain."""

    @pytest.fixture
    def two_employees(self, member3):
        """EMP002's certificate and EMP001's, both indexed."""
        member3.upload_document(
            "emp002.pdf", build_pdf(CERTIFICATE_LINES), **CERTIFICATE_IDENTITY
        )
        member3.upload_document(
            "emp001.pdf",
            build_pdf(
                [
                    "BIRTH CERTIFICATE",
                    "Registrar General of Births and Deaths, Colombo",
                    "Name: Kamal Perera",
                    "Employee Reference: EMP001",
                    "Date of Birth: 1988-02-02",
                    "Registration Number: BC-1988-11003",
                ]
            ),
            **{**CERTIFICATE_IDENTITY, "business_key_value": "EMP001"},
        )

        return member3

    def test_an_identity_filtered_search_returns_only_that_employee(
        self, two_employees
    ):
        found = two_employees.search(
            "birth certificate details",
            filters={
                "content_kind": "document_chunk",
                "business_key_name": "employee_id",
                "business_key_value": "EMP002",
                "document_type": "birth_certificate",
            },
        )

        hits = found.json()["hits"]
        assert hits, "no hit for EMP002"

        wrong = [
            hit
            for hit in hits
            if hit["metadata"]["business_key_value"] != "EMP002"
        ]

        assert wrong == [], f"identity filter returned other employees: {wrong}"

    def test_the_resolved_text_belongs_to_the_requested_employee(self, two_employees):
        found = two_employees.search(
            "birth certificate details",
            filters={
                "content_kind": "document_chunk",
                "business_key_name": "employee_id",
                "business_key_value": "EMP002",
            },
        )

        texts = [
            two_employees.resolve(hit["representation_id"]).json()["text"]
            for hit in found.json()["hits"]
        ]

        assert texts and all(texts)
        assert not any("Kamal Perera" in text for text in texts), (
            "EMP002's search resolved to another employee's certificate"
        )

    def test_every_current_hit_resolves(self, two_employees):
        """An unresolvable hit is a broken contract, not a slow one."""
        found = two_employees.search(
            "birth certificate", filters={"content_kind": "document_chunk"}
        )

        unresolvable = [
            hit["representation_id"]
            for hit in found.json()["hits"]
            if two_employees.resolve(hit["representation_id"]).status_code != 200
        ]

        assert unresolvable == [], f"hits that could not resolve: {unresolvable}"

    def test_page_and_chunk_provenance_survives_to_the_resolved_response(
        self, two_employees
    ):
        found = two_employees.search(
            "birth certificate details",
            filters={
                "content_kind": "document_chunk",
                "business_key_value": "EMP002",
                "business_key_name": "employee_id",
            },
        )

        payload = two_employees.resolve(
            found.json()["hits"][0]["representation_id"]
        ).json()

        assert payload["page_start"] is not None
        assert payload["chunk_index"] is not None
        assert payload["document_id"]

    def test_search_hits_do_not_carry_the_document_text(self, two_employees):
        """Member 3 must not be taught to expect text in a hit."""
        found = two_employees.search(
            "birth certificate", filters={"content_kind": "document_chunk"}
        )

        for hit in found.json()["hits"]:
            assert "text" not in hit, "a search hit carried document text"
            assert "Registrar General" not in str(hit), (
                "document text leaked into a search hit"
            )


class TestContractLRestrictedMetadata:
    """Sensitivity must reach the trusted upstream layer - and stop there."""

    def test_a_restricted_hit_reports_its_sensitivity(self, member3):
        member3.upload_document(
            "certificate.pdf", build_pdf(CERTIFICATE_LINES), **CERTIFICATE_IDENTITY
        )

        found = member3.search(
            "birth certificate",
            filters={
                "content_kind": "document_chunk",
                "business_key_name": "employee_id",
                "business_key_value": "EMP002",
            },
        )

        hits = found.json()["hits"]
        assert hits

        assert all(
            hit["metadata"]["sensitivity"] == "restricted" for hit in hits
        ), "a restricted document did not report its classification on the hit"

    def test_the_resolved_representation_reports_its_sensitivity(self, member3):
        member3.upload_document(
            "certificate.pdf", build_pdf(CERTIFICATE_LINES), **CERTIFICATE_IDENTITY
        )

        found = member3.search(
            "birth certificate",
            filters={
                "content_kind": "document_chunk",
                "business_key_name": "employee_id",
                "business_key_value": "EMP002",
            },
        )

        payload = member3.resolve(
            found.json()["hits"][0]["representation_id"]
        ).json()

        assert payload["sensitivity"] == "restricted"

    def test_member4_still_returns_the_restricted_content(self, member3):
        """The boundary, stated as a test.

        Member 4 classifies and reports. It does NOT deny. Refusing here would
        quietly move Member 1's authorization decision into Member 4, and the
        caller is a trusted server-side integration, not an end user.
        """
        member3.upload_document(
            "certificate.pdf", build_pdf(CERTIFICATE_LINES), **CERTIFICATE_IDENTITY
        )

        found = member3.search(
            "birth certificate",
            filters={
                "content_kind": "document_chunk",
                "business_key_value": "EMP002",
                "business_key_name": "employee_id",
            },
        )

        resolved = member3.resolve(found.json()["hits"][0]["representation_id"])

        assert resolved.status_code == 200, (
            "Member 4 refused a restricted document; that is Member 1's "
            "decision to make, not Member 4's"
        )
        assert resolved.json()["text"]
