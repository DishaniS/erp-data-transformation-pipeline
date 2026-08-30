"""Tests J, K, M and P: the boundaries that must not soften for integration.

The temptation this file guards against is specific and real: every one of
these controls is easier to integrate against with the control removed. An API
key is easier without a key, CORS is easier with a wildcard, and a browser
calling Member 4 directly is easier than standing up a backend. Each of those
would be a security regression bought with convenience, so each has a test.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests.erp_pipeline.integration.conftest import (
    ALLOWED_ORIGIN,
    CERTIFICATE_LINES,
    FOREIGN_ORIGIN,
    SERVICE_API_KEY,
    Member4,
    build_pdf,
)

PRODUCTION_ROOT = Path(__file__).resolve().parents[3] / "src" / "erp_pipeline"
FRONTEND_ROOT = Path(__file__).resolve().parents[3] / "frontend"


# ----------------------------------------------------------------------
# J - the service API key
# ----------------------------------------------------------------------


class TestJServiceApiKey:
    """What Member 2's and Member 3's backends must present."""

    def test_a_valid_key_is_accepted(self, client):
        response = client.post(
            "/v1/responses/adapt",
            json={"source_system_id": "legacy_hr", "body": {"employee_id": "EMP002"}},
            headers={"X-API-Key": SERVICE_API_KEY},
        )

        assert response.status_code == 200, response.text

    def test_a_missing_key_is_refused(self, client):
        response = client.post(
            "/v1/responses/adapt",
            json={"source_system_id": "legacy_hr", "body": {"employee_id": "EMP002"}},
        )

        assert response.status_code == 401, response.text

    def test_a_wrong_key_is_refused(self, client):
        response = client.post(
            "/v1/responses/adapt",
            json={"source_system_id": "legacy_hr", "body": {"employee_id": "EMP002"}},
            headers={"X-API-Key": "not-the-key"},
        )

        assert response.status_code == 401, response.text

    def test_a_refusal_never_echoes_either_key(self, client):
        """Neither the configured key nor the supplied one may appear."""
        response = client.post(
            "/v1/responses/adapt",
            json={"body": {}},
            headers={"X-API-Key": "WRONG_KEY_MARKER_5512"},
        )

        assert SERVICE_API_KEY not in response.text
        assert "WRONG_KEY_MARKER_5512" not in response.text

    def test_every_mutating_integration_route_is_protected(self, client):
        """The routes Members 2 and 3 actually POST to."""
        unprotected = []

        for path, payload in (
            ("/v1/search", {"query": "x"}),
            ("/v1/responses/adapt", {"body": {}}),
            ("/v1/jobs", {"job_type": "document_pipeline"}),
            ("/v1/sources", {"name": "x", "source_type": "csv"}),
        ):
            if client.post(path, json=payload).status_code != 401:
                unprotected.append(path)

        assert unprotected == [], f"these accepted an unauthenticated POST: {unprotected}"

    def test_health_stays_reachable_without_a_key(self, client):
        """A liveness probe that needs a credential pages you for the wrong reason."""
        assert client.get("/v1/health/live").status_code == 200

    def test_reads_follow_the_configured_protect_reads_setting(self, tmp_path):
        """Not weakened for integration - just reported truthfully."""
        member4 = Member4(tmp_path)

        assert member4.settings.protect_reads is False

        from fastapi.testclient import TestClient

        with TestClient(member4.app) as client:
            # A GET is reachable without a key under the default setting. This
            # is the existing documented behaviour, and the contract document
            # tells operators to enable protect_reads for a deployment where
            # reads are sensitive.
            assert client.get("/v1/capabilities").status_code == 200


# ----------------------------------------------------------------------
# K - credential redaction
# ----------------------------------------------------------------------


SECRET_MARKERS = (
    "SUPER_SECRET_ERP_TOKEN",
    "SECRET_COOKIE",
    "SECRET_ERP_API_SECRET",
)


class TestKCredentialRedaction:
    """Member 2 should not send credentials. If it does, they stop here."""

    @pytest.fixture
    def adapted(self, member2):
        raw = member2.execute("member2_employee_response.json")
        response = member2.adapt(
            raw, query="employment status", include_credentials=True
        )

        assert response.status_code == 200, response.text

        return response

    def test_no_secret_appears_in_the_response_body(self, adapted):
        for marker in SECRET_MARKERS:
            assert marker not in adapted.text, f"{marker} leaked into the response"

    def test_no_secret_appears_in_the_provenance(self, adapted):
        provenance = json.dumps(adapted.json()["provenance"])

        for marker in SECRET_MARKERS:
            assert marker not in provenance

    def test_no_secret_appears_in_the_diagnostic_report(self, adapted):
        report = json.dumps(adapted.json().get("report", {}))

        for marker in SECRET_MARKERS:
            assert marker not in report

    def test_no_secret_appears_in_the_warnings(self, adapted):
        warnings = json.dumps(adapted.json().get("warnings", []))

        for marker in SECRET_MARKERS:
            assert marker not in warnings

    def test_no_secret_reaches_the_logs(self, member2, caplog):
        import logging

        caplog.set_level(logging.DEBUG)

        raw = member2.execute("member2_employee_response.json")
        member2.adapt(raw, query="employment status", include_credentials=True)

        logged = caplog.text

        for marker in SECRET_MARKERS:
            assert marker not in logged, f"{marker} reached a log record"

    def test_no_secret_is_persisted(self, member2, member4):
        """Adaptation is stateless, so nothing should be written at all."""
        raw = member2.execute("member2_employee_response.json")
        member2.adapt(raw, query="employment status", include_credentials=True)

        stored = json.dumps(
            [
                str(member4.representations.get(rid))
                for rid in member4.representations.list_ids()
            ]
        )

        for marker in SECRET_MARKERS:
            assert marker not in stored

    def test_a_credential_inside_the_erp_body_is_passed_through(self, member2):
        """A MEASURED BOUND, not a redaction claim.

        The redaction guarantee covers what Member 4 is TOLD about the call -
        headers, provenance, logs, persistence. It does not cover the ERP's own
        business payload: adaptation is faithful by design, and a field named
        ``db_password`` returned by the ERP is content, not transport metadata.

        Suppressing it would require classifying response content, which the
        component deliberately does not do - a filter that catches
        ``db_password`` and misses ``dbPwd`` is worse than an honest bound,
        because it reads as protection. This is recorded as a known limitation
        and as guidance to Member 2: do not return credentials in ERP business
        payloads.
        """
        response = member2.adapt(
            {
                "endpoint": "/api/config",
                "http_status": 200,
                "content_type": "application/json",
                "body": {
                    "employee_id": "EMP002",
                    "db_password": "SECRET_DB_PASSWORD_11991",
                },
            },
            query="employment status",
        )

        assert response.status_code == 200
        body = response.json()

        # It passes through the adapted CONTENT, faithfully.
        assert "SECRET_DB_PASSWORD_11991" in json.dumps(body["llm_ready"])

        # And nowhere else. The transport-metadata guarantee still holds.
        assert "SECRET_DB_PASSWORD_11991" not in json.dumps(body["provenance"])
        assert "SECRET_DB_PASSWORD_11991" not in json.dumps(
            body.get("report", {})
        )


# ----------------------------------------------------------------------
# M - CORS
# ----------------------------------------------------------------------


class TestMCors:
    """Configurable, explicit, and never a wildcard with credentials."""

    def test_a_configured_origin_is_allowed(self, client):
        response = client.get(
            "/v1/capabilities", headers={"Origin": ALLOWED_ORIGIN}
        )

        assert response.status_code == 200
        assert (
            response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
        )

    def test_an_unconfigured_origin_gets_no_allow_header(self, client):
        response = client.get(
            "/v1/capabilities", headers={"Origin": FOREIGN_ORIGIN}
        )

        allowed = response.headers.get("access-control-allow-origin")

        assert allowed != FOREIGN_ORIGIN, (
            "an unconfigured origin was granted cross-origin access"
        )

    def test_a_preflight_from_a_foreign_origin_is_not_granted(self, client):
        response = client.options(
            "/v1/search",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-api-key",
            },
        )

        assert response.headers.get("access-control-allow-origin") != FOREIGN_ORIGIN

    def test_cors_is_off_entirely_when_no_origin_is_configured(self, tmp_path):
        """The default. A deployment that configures nothing allows nothing."""
        from fastapi.testclient import TestClient

        member4 = Member4(tmp_path, cors_origins=())

        with TestClient(member4.app) as client:
            response = client.get(
                "/v1/capabilities", headers={"Origin": ALLOWED_ORIGIN}
            )

            assert "access-control-allow-origin" not in response.headers

    def test_the_codebase_contains_no_wildcard_origin_with_credentials(self):
        """The specific dangerous combination, scanned for as CODE.

        Deliberately an AST check rather than a text search. ``config.py``
        contains the string ``allow_origins=["*"]`` inside a docstring
        explaining why the default is closed, and a grep-shaped test would
        report that warning as the very thing it warns about.
        """
        offenders = []

        for path in PRODUCTION_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue

                for keyword in node.keywords:
                    if keyword.arg != "allow_origins":
                        continue

                    value = keyword.value

                    if isinstance(value, (ast.List, ast.Tuple)):
                        literals = [
                            element.value
                            for element in value.elts
                            if isinstance(element, ast.Constant)
                        ]

                        if "*" in literals:
                            offenders.append(f"{path.name}:{node.lineno}")

        assert offenders == [], f"wildcard CORS origins found in: {offenders}"


# ----------------------------------------------------------------------
# Frontend: the service key must not be shipped to a browser
# ----------------------------------------------------------------------


class TestFrontendKeyExposure:
    """Member 4's developer frontend must not carry a production secret."""

    def test_no_frontend_source_file_contains_the_service_key_pattern(self):
        if not FRONTEND_ROOT.exists():  # pragma: no cover - optional tree
            pytest.skip("no frontend tree in this checkout")

        offenders = []
        source = FRONTEND_ROOT / "src"

        for path in source.rglob("*"):
            if path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".vue"}:
                continue

            text = path.read_text(encoding="utf-8", errors="ignore")

            # A hard-coded key: a long literal assigned to something key-ish.
            for line in text.splitlines():
                lowered = line.lower()

                if "api" in lowered and "key" in lowered and "=" in line:
                    literal = line.split("=", 1)[1].strip().strip(";,")

                    if literal.startswith(('"', "'")) and len(literal) > 14:
                        offenders.append(f"{path.name}: {line.strip()[:80]}")

        assert offenders == [], (
            f"a literal API key appears in browser source: {offenders}"
        )

    def test_the_example_env_file_ships_no_value(self):
        example = FRONTEND_ROOT / ".env.example"

        if not example.exists():  # pragma: no cover
            pytest.skip("no frontend .env.example")

        for line in example.read_text(encoding="utf-8").splitlines():
            if line.startswith("VITE_API_KEY"):
                _, _, value = line.partition("=")

                assert value.strip() == "", (
                    "the example env file ships a populated API key, which "
                    "would be bundled into browser JavaScript"
                )

    def test_env_files_are_git_ignored(self):
        ignore = (Path(__file__).resolve().parents[3] / ".gitignore").read_text(
            encoding="utf-8"
        )

        assert ".env" in ignore


# ----------------------------------------------------------------------
# P - no cross-member production clients
# ----------------------------------------------------------------------


#: Names that would mean Member 4 had absorbed another member's job.
FORBIDDEN_CLASS_MARKERS = (
    "policygateclient",
    "policyclient",
    "member1client",
    "member2client",
    "member3client",
    "mcpclient",
    "erpapiclient",
    "erpexecutionclient",
    "erpbridgeclient",
)


class TestPNoCrossMemberClients:
    """A structural scan, not a promise in a document."""

    def _production_files(self):
        return sorted(PRODUCTION_ROOT.rglob("*.py"))

    def test_no_production_class_implements_another_members_role(self):
        offenders = []

        for path in self._production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    if node.name.lower() in FORBIDDEN_CLASS_MARKERS:
                        offenders.append(f"{path.name}:{node.name}")

        assert offenders == [], f"cross-member client classes found: {offenders}"

    def test_no_production_module_imports_an_mcp_library(self):
        offenders = []

        for path in self._production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                names: list[str] = []

                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]

                for name in names:
                    root = name.split(".")[0].lower()

                    if root in {"mcp", "fastmcp", "modelcontextprotocol"}:
                        offenders.append(f"{path.name}: {name}")

        assert offenders == [], f"MCP imports in production code: {offenders}"

    def test_the_package_ships_no_http_client_for_erp_business_calls(self):
        """Phase 8's rule, re-checked after a phase full of integration work.

        ``requests``/``httpx`` must not be imported by the production package
        at all. The remote-asset feature deliberately ships without a client
        precisely so that importing this package can never cause a request.
        """
        offenders = []

        for path in self._production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                names = []

                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]

                for name in names:
                    if name.split(".")[0].lower() in {"requests", "httpx", "aiohttp"}:
                        offenders.append(f"{path.name}: {name}")

        assert offenders == [], f"an HTTP client is imported in: {offenders}"

    def test_no_production_module_imports_the_test_fakes(self):
        """The fakes are test actors. Production must not know they exist."""
        offenders = [
            str(path)
            for path in self._production_files()
            if "integration.fakes" in path.read_text(encoding="utf-8")
        ]

        assert offenders == []

    def test_no_production_code_makes_an_authorization_decision(self):
        """Member 1's boundary, scanned for as a code shape.

        Looks for the specific pattern the brief forbids - a role or permission
        check that denies a user. Storage-tier and sensitivity routing are not
        that: they decide where a vector lives, never whether a person may see
        it.
        """
        offenders = []

        for path in self._production_files():
            text = path.read_text(encoding="utf-8")

            for number, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()

                if stripped.startswith("#"):
                    continue

                lowered = stripped.lower()

                if "user.role" in lowered or "current_user" in lowered:
                    offenders.append(f"{path.name}:{number}: {stripped[:70]}")

        assert offenders == [], f"user authorization logic found: {offenders}"


class TestSensitivityIsMetadataNotEnforcement:
    """Member 4 classifies. It does not deny."""

    def test_a_restricted_document_is_still_returned_over_http(self, member3):
        member3.upload_document(
            "certificate.pdf",
            build_pdf(CERTIFICATE_LINES),
            source_system_id="legacy_hr",
            source_entity="employees",
            business_key_name="employee_id",
            business_key_value="EMP002",
            document_type="birth_certificate",
            sensitivity="restricted",
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
        assert all(h["metadata"]["sensitivity"] == "restricted" for h in hits)

        resolved = member3.resolve(hits[0]["representation_id"])

        assert resolved.status_code == 200, (
            "Member 4 denied access to a restricted document; that decision "
            "belongs to Member 1"
        )
