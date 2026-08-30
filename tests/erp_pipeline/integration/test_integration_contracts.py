"""Tests N, O and R: the contract surface itself.

N asks whether ``/v1/capabilities`` tells the truth, O asks whether the OpenAPI
document describes the routes integration actually uses, and R asks whether any
of it survives a restart. All three are about the same worry: an integration
partner plans against a description, and a description that drifts from the
system is worse than no description at all.
"""

from __future__ import annotations

import pytest

from tests.erp_pipeline.integration.conftest import (
    CERTIFICATE_LINES,
    EMPLOYEE_CSV,
    SERVICE_API_KEY,
    Member4,
    build_pdf,
)

#: Every operation Members 1, 2 and 3 depend on, by operationId.
CRITICAL_OPERATIONS = {
    "getCapabilities",
    "uploadCsv",
    "uploadDocument",
    "getSchema",
    "suggestMapping",
    "updateMapping",
    "createJob",
    "getJob",
    "search",
    "getRepresentation",
    "adaptResponse",
    "createSource",
}


# ----------------------------------------------------------------------
# N - capabilities truthfulness
# ----------------------------------------------------------------------


class TestNCapabilitiesTruthfulness:
    """Every advertised capability must have a contract test behind it."""

    @pytest.fixture
    def advertised(self, client):
        response = client.get("/v1/capabilities")

        assert response.status_code == 200

        return response.json()

    def test_the_integration_block_is_present(self, advertised):
        assert advertised["integration_capabilities"], (
            "capabilities advertises nothing an integration partner can use"
        )

    def test_supported_and_enabled_are_reported_separately(self, advertised):
        for name, status in advertised["integration_capabilities"].items():
            assert "supported" in status, name
            assert "enabled" in status, name

    def test_a_wired_deployment_reports_its_capabilities_enabled(self, advertised):
        """The fixture wires embedding, storage and representations."""
        capabilities = advertised["integration_capabilities"]

        for name in (
            "csv_ingestion",
            "document_ingestion",
            "automatic_document_indexing",
            "schema_vector_retrieval",
            "semantic_search",
            "representation_resolution",
            "response_adaptation",
        ):
            assert capabilities[name]["enabled"] is True, (
                f"{name} is wired in this deployment but advertised as disabled"
            )

    def test_remote_asset_fetching_is_supported_but_not_enabled(self, advertised):
        """The supported/enabled distinction, on the capability that needs it.

        This is the whole reason the two booleans are separate: the code
        exists, and this deployment - like every default one - fetches nothing.
        """
        status = advertised["integration_capabilities"]["remote_asset_fetching"]

        assert status["supported"] is True
        assert status["enabled"] is False
        assert status["detail"], "a disabled capability must say why"

    def test_scheduled_sync_is_not_advertised_as_enabled_without_a_service(
        self, advertised
    ):
        status = advertised["integration_capabilities"]["scheduled_sync"]

        assert status["enabled"] is False

    def test_an_unwired_deployment_advertises_almost_nothing_as_enabled(
        self, tmp_path
    ):
        """Truthfulness in the other direction: no services, no claims."""
        from erp_pipeline.api import create_app
        from fastapi.testclient import TestClient

        with TestClient(create_app()) as bare:
            capabilities = bare.get("/v1/capabilities").json()[
                "integration_capabilities"
            ]

        assert capabilities["semantic_search"]["enabled"] is False
        assert capabilities["representation_resolution"]["enabled"] is False
        # Adaptation needs no services at all, so it stays honestly enabled.
        assert capabilities["response_adaptation"]["enabled"] is True

    def test_every_advertised_capability_maps_to_a_real_contract(self, advertised):
        """No capability name without a test that exercises it.

        The mapping is explicit rather than clever: a capability added to the
        endpoint without a corresponding contract test fails here, which is the
        point. This test is the reason the capability list cannot quietly grow
        marketing entries.
        """
        covered = {
            "csv_ingestion": "test_member3_contracts.py::TestContractBCsvWorkflow",
            "document_ingestion": "test_member3_contracts.py::TestContractADocumentWorkflow",
            "automatic_document_indexing": (
                "test_member3_contracts.py::"
                "test_the_uploaded_certificate_becomes_searchable_and_resolvable"
            ),
            "schema_discovery": "test_member3_contracts.py::test_the_inferred_schema_is_readable",
            "schema_vector_retrieval": "test_member3_contracts.py::TestContractCSchemaQuery",
            "structured_transformation": "test_member3_contracts.py::test_rows_index_once_a_key_is_declared",
            "semantic_search": "test_member3_contracts.py::TestContractDDocumentSearch",
            "representation_resolution": "test_member3_contracts.py::test_every_current_hit_resolves",
            "response_adaptation": "test_member2_contracts.py::TestContractEJsonAdaptation",
            "remote_asset_fetching": "test_remote_asset_pipeline.py (Phase 8)",
            "scheduled_sync": "test_sync_scheduler.py (Phase 9)",
            "sensitivity_metadata": "test_member3_contracts.py::TestContractLRestrictedMetadata",
        }

        advertised_names = set(advertised["integration_capabilities"])
        uncovered = advertised_names - set(covered)

        assert uncovered == set(), (
            f"advertised with no contract test behind them: {uncovered}"
        )

    def test_limitations_are_still_reported(self, advertised):
        """Phase 11 must not quietly drop the honest disclaimers."""
        assert advertised["limitations"]
        assert any(
            "never calls the documented endpoints" in limitation
            for limitation in advertised["limitations"]
        )

    def test_no_capability_makes_a_compliance_claim(self, advertised):
        """The forbidden vocabulary, checked against the advertised text."""
        rendered = str(advertised).lower()

        for claim in (
            "hipaa",
            "gdpr compliant",
            "zero trust",
            "end-to-end encryption",
            "fully secure",
            "real-time replication",
        ):
            assert claim not in rendered, f"capabilities claims {claim!r}"


# ----------------------------------------------------------------------
# O - the OpenAPI contract
# ----------------------------------------------------------------------


class TestOOpenApiContract:
    """The document all four members integrate against."""

    @pytest.fixture
    def spec(self, client):
        response = client.get("/openapi.json")

        assert response.status_code == 200

        return response.json()

    def _operations(self, spec) -> dict[str, str]:
        found = {}

        for path, operations in spec["paths"].items():
            for method, operation in operations.items():
                if method in {"get", "post", "put", "delete", "patch"}:
                    found[operation["operationId"]] = f"{method.upper()} {path}"

        return found

    def test_every_integration_critical_operation_is_documented(self, spec):
        missing = CRITICAL_OPERATIONS - set(self._operations(spec))

        assert missing == set(), f"undocumented integration operations: {missing}"

    def test_the_document_upload_operation_accepts_multipart(self, spec):
        operation = spec["paths"]["/v1/files/documents"]["post"]
        content = operation["requestBody"]["content"]

        assert "multipart/form-data" in content

    def test_the_document_upload_declares_its_identity_fields(self, spec):
        """The form fields the contract document tells Member 3 to send."""
        operation = spec["paths"]["/v1/files/documents"]["post"]
        schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]

        # The schema may be inlined or referenced; resolve one level.
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            schema = spec["components"]["schemas"][name]

        properties = set(schema.get("properties", {}))

        for field in (
            "file",
            "source_system_id",
            "source_entity",
            "business_key_name",
            "business_key_value",
            "document_type",
            "sensitivity",
        ):
            assert field in properties, f"{field} is not documented on the upload"

    def test_the_adapt_operation_documents_both_body_forms(self, spec):
        schema = spec["components"]["schemas"]["ResponseAdaptRequest"]
        properties = set(schema["properties"])

        assert {"body", "body_base64", "content_type", "endpoint"} <= properties

    def test_the_search_response_exposes_representation_ids(self, spec):
        schema = spec["components"]["schemas"]["SearchHitResponse"]

        assert "representation_id" in schema["properties"]

    def test_the_capability_status_model_is_documented(self, spec):
        assert "CapabilityStatus" in spec["components"]["schemas"]

    def test_no_undocumented_production_route_exists(self, client, spec):
        """Every mounted route appears in the document."""
        documented = set()

        for path, operations in spec["paths"].items():
            for method in operations:
                if method in {"get", "post", "put", "delete", "patch"}:
                    documented.add((method.upper(), path))

        mounted = set()

        for route in client.app.routes:
            path = getattr(route, "path", "")

            if not path.startswith("/v1"):
                continue

            for method in getattr(route, "methods", set()):
                if method in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                    mounted.add((method, path))

        assert mounted - documented == set(), (
            f"routes missing from OpenAPI: {mounted - documented}"
        )

    def test_the_spec_contains_no_secret(self, spec):
        rendered = str(spec)

        assert SERVICE_API_KEY not in rendered


# ----------------------------------------------------------------------
# R - restart
# ----------------------------------------------------------------------


class TestRRestartIntegration:
    """What Members 2 and 3 can still do after Member 4 restarts."""

    @pytest.fixture
    def restarted(self, tmp_path):
        """Index a document and a schema, then rebuild the application."""
        from fastapi.testclient import TestClient

        member4 = Member4(tmp_path)
        headers = {"X-API-Key": SERVICE_API_KEY}

        with TestClient(member4.app) as before:
            upload = before.post(
                "/v1/files/documents",
                files={
                    "file": (
                        "certificate.pdf",
                        build_pdf(CERTIFICATE_LINES),
                        "application/pdf",
                    )
                },
                data={
                    "source_system_id": "legacy_hr",
                    "source_entity": "employees",
                    "business_key_name": "employee_id",
                    "business_key_value": "EMP002",
                    "document_type": "birth_certificate",
                    "sensitivity": "restricted",
                },
                headers=headers,
            ).json()

            assert upload["index_job_id"]

            before.post(
                "/v1/files/csv",
                files={"file": ("employees.csv", EMPLOYEE_CSV, "text/csv")},
                headers=headers,
            )

            found = before.post(
                "/v1/search",
                json={
                    "query": "birth certificate",
                    "filters": {
                        "content_kind": "document_chunk",
                        "business_key_name": "employee_id",
                        "business_key_value": "EMP002",
                    },
                },
                headers=headers,
            ).json()

            assert found["hits"], "nothing was indexed before the restart"
            representation_id = found["hits"][0]["representation_id"]

        # Member 4 restarts. New application, new orchestration, same stores.
        member4.rebuild_app()

        with TestClient(member4.app) as after:
            yield after, representation_id, headers

    def test_search_still_works_after_a_restart(self, restarted):
        after, _, headers = restarted

        response = after.post(
            "/v1/search",
            json={
                "query": "birth certificate",
                "filters": {
                    "content_kind": "document_chunk",
                    "business_key_name": "employee_id",
                    "business_key_value": "EMP002",
                },
            },
            headers=headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["hits"], "the index did not survive the restart"

    def test_a_representation_still_resolves_after_a_restart(self, restarted):
        after, representation_id, headers = restarted

        response = after.get(
            f"/v1/representations/{representation_id}", headers=headers
        )

        assert response.status_code == 200, response.text
        assert response.json()["text"]

    def test_sensitivity_survives_the_restart(self, restarted):
        after, representation_id, headers = restarted

        payload = after.get(
            f"/v1/representations/{representation_id}", headers=headers
        ).json()

        assert payload["sensitivity"] == "restricted"

    def test_identity_survives_the_restart(self, restarted):
        after, representation_id, headers = restarted

        payload = after.get(
            f"/v1/representations/{representation_id}", headers=headers
        ).json()

        assert payload["business_key_value"] == "EMP002"
        assert payload["document_type"] == "birth_certificate"

    def test_adaptation_works_immediately_after_a_restart(self, restarted):
        """It holds no state, so a restart should not matter at all."""
        after, _, headers = restarted

        response = after.post(
            "/v1/responses/adapt",
            json={
                "query": "employment status",
                "source_system_id": "legacy_hr",
                "body": {"employee_id": "EMP002", "employment_status": "ACTIVE"},
            },
            headers=headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["success"] is True

    def test_an_empty_state_store_is_not_silently_replaced(self):
        """The defect the restart contract exposed, pinned directly.

        ``InMemoryTierStateStore`` defines ``__len__``, so a fresh one is
        falsy - and a fresh one is what every caller passes at startup. The
        service used ``state_store or InMemoryTierStateStore()``, which threw
        the caller's store away and kept a private one. Nothing failed loudly:
        writes succeeded, searches worked, and the state simply was not where
        the caller had put it, so it vanished on restart.
        """
        from erp_pipeline.storage.service import StorageService
        from erp_pipeline.storage.state import InMemoryTierStateStore

        store = InMemoryTierStateStore()

        assert not store, "the premise changed: an empty store is now truthy"

        service = StorageService(state_store=store)

        assert service._state is store, (
            "the caller's state store was replaced by a private one"
        )

    def test_capabilities_still_answers_after_a_restart(self, restarted):
        after, _, _ = restarted

        response = after.get("/v1/capabilities")

        assert response.status_code == 200
        assert response.json()["integration_capabilities"]
