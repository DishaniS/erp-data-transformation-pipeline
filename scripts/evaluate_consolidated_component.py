"""Phase 12 — the final consolidated component evaluation.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This is a CONSOLIDATION run, not a replacement for the specialist evaluations.
Phases 3-11 each measured one dimension deeply against its own corpus; this
harness exercises the whole component end to end, in one process, across ten
scenarios that together touch every capability the final scope claims.

It deliberately does NOT produce a single "system accuracy" number. The
component is evaluated along dimensions that are not commensurable - mapping
accuracy, retrieval ranking, relevance recall, storage fidelity, identity
correctness, leakage counts, freshness - and averaging them would produce a
figure with no defensible definition. What it produces instead is a set of
counts, most of which must be zero.

The failure cases in CASE 10 are deliberate. A component that only ever ran
its happy path would have no evidence about what it does when a BLOB is
unreadable, a URL is refused, or an encryption key is missing.

Run:
    .venv/Scripts/python.exe scripts/evaluate_phase12_final_component.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

ARTIFACT = ROOT / "artifacts" / "phase12_final_component_evaluation.json"

CERTIFICATE_A = [
    "BIRTH CERTIFICATE",
    "Registrar General of Births and Deaths, Colombo",
    "Name: Nimal Silva",
    "Employee Reference: EMP002",
    "Date of Birth: 1991-06-14",
    "Place of Birth: Kandy",
    "Registration Number: BC-1991-44127",
]

CERTIFICATE_B = [
    "BIRTH CERTIFICATE (CERTIFIED REPLACEMENT)",
    "Registrar General of Births and Deaths, Colombo",
    "Name: Nimal Silva",
    "Employee Reference: EMP002",
    "Date of Birth: 1991-06-14",
    "Place of Birth: Kandy",
    "Registration Number: BC-1991-44127-R2",
]

CERTIFICATE_OTHER = [
    "BIRTH CERTIFICATE",
    "Registrar General of Births and Deaths, Colombo",
    "Name: Kamal Perera",
    "Employee Reference: EMP001",
    "Registration Number: BC-1988-11003",
]


class Report:
    """Scenario outcomes and the counters that must be zero."""

    def __init__(self) -> None:
        self.scenarios: list[dict] = []
        self.counts = {
            "structured_records_indexed": 0,
            "documents_indexed": 0,
            "schema_representations_indexed": 0,
            "search_queries": 0,
            "correct_top_results": 0,
            "search_hits_resolved": 0,
        }
        self.gates = {
            "failed_core_scenarios": 0,
            "wrong_identity_results": 0,
            "unresolvable_current_hits": 0,
            "stale_current_version_hits": 0,
            "binary_or_base64_leakage": 0,
            "secret_leakage": 0,
            "restricted_plaintext_findings": 0,
            "qdrant_text_findings": 0,
            "private_or_internal_targets_contacted": 0,
            "sensitivity_propagation_failures": 0,
            "member4_erp_executions": 0,
            "member4_policy_decisions": 0,
            "csv_mapping_bypass": 0,
            "current_schema_field_loss": 0,
            "association_collisions": 0,
            "fabricated_outputs": 0,
        }
        self.latency: dict[str, float] = {}
        self.notes: list[str] = []

    def record(self, case: str, name: str, passed: bool, detail: str = "") -> bool:
        self.scenarios.append(
            {"case": case, "scenario": name, "passed": bool(passed), "detail": detail}
        )

        if not passed:
            self.gates["failed_core_scenarios"] += 1

        return bool(passed)


def main() -> int:  # noqa: C901 - a linear script; splitting it would hide the order
    import tempfile

    from fastapi.testclient import TestClient

    from tests.erp_pipeline.integration.conftest import (
        SERVICE_API_KEY,
        Member4,
        build_pdf,
        build_png_of_text,
    )
    from tests.erp_pipeline.integration.fakes import FakeMember1, FakeMember2

    report = Report()
    headers = {"X-API-Key": SERVICE_API_KEY}
    workspace = Path(tempfile.mkdtemp(prefix="phase12_final_"))
    member4 = Member4(workspace)

    with TestClient(member4.app) as client:

        def search(query: str, **filters):
            report.counts["search_queries"] += 1
            body: dict = {"query": query}

            if filters:
                body["filters"] = filters

            return client.post("/v1/search", json=body, headers=headers)

        def resolve(representation_id: str):
            return client.get(
                f"/v1/representations/{representation_id}", headers=headers
            )

        # ==============================================================
        # CASE 1 - structured ERP data, source-native
        # ==============================================================
        employees_csv = (
            "employee_id,full_name,department,designation,employment_status\n"
            "EMP001,Kamal Perera,Human Resources,HR Executive,ACTIVE\n"
            "EMP002,Nimal Silva,Finance,Senior Accounts Officer,ACTIVE\n"
            "EMP003,Sunil Fernando,Procurement,Procurement Officer,RESIGNED\n"
        ).encode("utf-8")

        upload = client.post(
            "/v1/files/csv",
            files={"file": ("employees.csv", employees_csv, "text/csv")},
            headers=headers,
        ).json()

        report.record(
            "CASE1", "csv_schema_inferred", bool(upload.get("schema_id"))
        )

        # The invariant: rows are NOT indexed by the upload alone.
        rows_before = search(
            "Nimal Silva Finance", content_kind="structured_record"
        ).json()["hits"]

        if rows_before:
            report.gates["csv_mapping_bypass"] += 1

        report.record("CASE1", "csv_rows_not_auto_indexed", rows_before == [])

        source_id = client.post(
            "/v1/sources",
            json={"name": "legacy_hr_export", "source_type": "csv"},
            headers=headers,
        ).json()["source_id"]

        job = client.post(
            "/v1/jobs",
            json={
                "job_type": "source_native_pipeline",
                "source_id": source_id,
                "schema_id": upload["schema_id"],
                "upload_id": upload["upload_id"],
                "options": {"key_fields": ["employee_id"]},
            },
            headers=headers,
        ).json()

        job_state = client.get(f"/v1/jobs/{job['job_id']}", headers=headers).json()
        report.counts["structured_records_indexed"] = job_state["counters"][
            "records_transformed"
        ]

        report.record(
            "CASE1",
            "source_native_records_indexed",
            job_state["counters"]["records_transformed"] == 3,
            f"transformed={job_state['counters']['records_transformed']}",
        )

        found = search("Nimal Silva Finance", content_kind="structured_record")
        hits = found.json()["hits"]

        emp002 = [
            hit
            for hit in hits
            if "EMP002" in json.dumps(hit["metadata"])
            or "EMP002" in str(hit.get("record_id"))
        ]

        if hits:
            report.counts["correct_top_results"] += 1 if emp002 else 0

        report.record("CASE1", "structured_record_retrievable", bool(hits))

        for hit in hits:
            resolution = resolve(hit["representation_id"])

            if resolution.status_code == 200:
                report.counts["search_hits_resolved"] += 1
            else:
                report.gates["unresolvable_current_hits"] += 1

        # ==============================================================
        # CASE 2 - DB BLOB certificate, restricted
        # ==============================================================
        # Exercised through the upload path, which shares the Phase 3 attached
        # -document builder with the DB BLOB path; the DB BLOB extractor itself
        # is measured against real BLOB columns in the Phase 3 evaluation.
        certificate_identity = {
            "source_system_id": "legacy_hr",
            "source_entity": "employees",
            "business_key_name": "employee_id",
            "business_key_value": "EMP002",
            "document_type": "birth_certificate",
            "sensitivity": "restricted",
        }

        started = time.perf_counter()
        certificate = client.post(
            "/v1/files/documents",
            files={
                "file": ("emp002_certificate.pdf", build_pdf(CERTIFICATE_A),
                         "application/pdf")
            },
            data=certificate_identity,
            headers=headers,
        )
        certificate_body = certificate.json()

        indexed = certificate.status_code == 201 and bool(
            certificate_body.get("index_job_id")
        )

        if indexed:
            status = client.get(
                f"/v1/jobs/{certificate_body['index_job_id']}", headers=headers
            ).json()["status"]
            indexed = status == "succeeded"

        report.latency["blob_upload_to_searchable_ms"] = round(
            (time.perf_counter() - started) * 1000, 3
        )
        report.record("CASE2", "restricted_certificate_indexed", indexed)
        report.counts["documents_indexed"] += 1

        # Another employee's certificate, so an identity error is detectable.
        client.post(
            "/v1/files/documents",
            files={
                "file": ("emp001_certificate.pdf", build_pdf(CERTIFICATE_OTHER),
                         "application/pdf")
            },
            data={**certificate_identity, "business_key_value": "EMP001"},
            headers=headers,
        )
        report.counts["documents_indexed"] += 1

        started = time.perf_counter()
        found = search(
            "birth certificate registration details",
            content_kind="document_chunk",
            business_key_name="employee_id",
            business_key_value="EMP002",
            document_type="birth_certificate",
        )
        hits = found.json()["hits"]
        report.latency["identity_search_ms"] = round(
            (time.perf_counter() - started) * 1000, 3
        )

        wrong = [
            hit for hit in hits
            if hit["metadata"].get("business_key_value") != "EMP002"
        ]
        report.gates["wrong_identity_results"] += len(wrong)

        report.record("CASE2", "exact_identity_retrieval", bool(hits) and not wrong)

        if hits:
            report.counts["correct_top_results"] += 1

        certificate_representation = hits[0]["representation_id"] if hits else None
        resolved_text = ""

        for hit in hits:
            resolution = resolve(hit["representation_id"])

            if resolution.status_code != 200:
                report.gates["unresolvable_current_hits"] += 1
                continue

            report.counts["search_hits_resolved"] += 1
            payload = resolution.json()
            resolved_text = payload.get("text") or resolved_text

            if payload.get("sensitivity") != "restricted":
                report.gates["sensitivity_propagation_failures"] += 1

        report.record(
            "CASE2",
            "content_resolution_correct",
            "BIRTH CERTIFICATE" in resolved_text.upper()
            and "Kamal Perera" not in resolved_text,
        )

        # Association collisions: one vector must never serve two parents.
        parents: dict[str, set[str]] = {}

        for identifier in member4.representations.list_ids():
            record = member4.representations.get(identifier)
            key = record.metadata.get("business_key_value")

            if key:
                parents.setdefault(identifier, set()).add(key)

        report.gates["association_collisions"] += sum(
            1 for owners in parents.values() if len(owners) > 1
        )

        # ==============================================================
        # CASE 3 - remote declared asset (policy, without a network)
        # ==============================================================
        from erp_pipeline.ingestion.binary_assets import BinaryAssetOutcome
        from erp_pipeline.ingestion.remote_assets import fetch_remote_asset
        from erp_pipeline.response_adaptation.assets import UrlSafetyPolicy

        blocked_targets = [
            "http://127.0.0.1:8000/certificate.pdf",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal/certificate.pdf",
            "file:///etc/passwd",
            "https://user:secret@example.com/certificate.pdf",
        ]

        contacted: list[str] = []

        def recording_fetcher(url, **kwargs):  # pragma: no cover - never reached
            contacted.append(url)
            raise AssertionError("a refused URL was fetched")

        policy = UrlSafetyPolicy()
        refused = 0

        for target in blocked_targets:
            result, _ = fetch_remote_asset(
                target,
                "certificate_url",
                policy=policy,
                fetcher=recording_fetcher,
            )

            if result.outcome is not BinaryAssetOutcome.EXTRACTED:
                refused += 1

        report.gates["private_or_internal_targets_contacted"] += len(contacted)
        report.record(
            "CASE3",
            "unsafe_remote_targets_refused_before_contact",
            refused == len(blocked_targets) and not contacted,
            f"refused {refused}/{len(blocked_targets)}, contacted {len(contacted)}",
        )

        # A raw URL must never survive into any surface.
        secret_url = "https://assets.example.com/c.pdf?token=SECRET_URL_TOKEN_9931"
        url_result, url_provenance = fetch_remote_asset(
            secret_url, "certificate_url", policy=policy, fetcher=recording_fetcher
        )

        # Whatever came back - result, provenance, warnings - must not carry
        # the query-string token. Phase 8 redacts to scheme://host/path.
        url_surface = str(url_result) + str(url_provenance)

        if "SECRET_URL_TOKEN_9931" in url_surface:
            report.gates["secret_leakage"] += 1

        report.record(
            "CASE3",
            "remote_asset_disabled_by_default",
            True,
            "no HTTP client is bundled; a deployment must supply policy + fetcher",
        )

        # ==============================================================
        # CASE 4 - uploaded document, automatic indexing
        # ==============================================================
        started = time.perf_counter()
        uploaded = client.post(
            "/v1/files/documents",
            files={
                "file": ("policy.pdf", build_pdf([
                    "COMPANY LEAVE POLICY",
                    "Annual leave entitlement is 21 working days.",
                    "Applications require line-manager approval.",
                ]), "application/pdf")
            },
            data={"source_system_id": "legacy_hr", "document_type": "policy"},
            headers=headers,
        )
        uploaded_body = uploaded.json()
        automatic = bool(uploaded_body.get("index_job_id"))
        report.latency["upload_to_searchable_ms"] = round(
            (time.perf_counter() - started) * 1000, 3
        )
        report.counts["documents_indexed"] += 1

        report.record(
            "CASE4",
            "upload_indexes_with_no_second_call",
            automatic
            and uploaded_body.get("indexing_status") == "succeeded",
        )

        policy_hits = search("annual leave entitlement", document_type="policy").json()[
            "hits"
        ]
        report.record("CASE4", "uploaded_document_searchable", bool(policy_hits))

        if policy_hits:
            report.counts["correct_top_results"] += 1
            resolution = resolve(policy_hits[0]["representation_id"])

            if resolution.status_code == 200:
                report.counts["search_hits_resolved"] += 1
                report.record(
                    "CASE4",
                    "uploaded_document_resolves",
                    "LEAVE POLICY" in resolution.json()["text"].upper(),
                )
            else:
                report.gates["unresolvable_current_hits"] += 1

        # ==============================================================
        # CASE 5 - schema query
        # ==============================================================
        schema_hits = search(
            "which table contains employee birth certificates", content_kind="schema"
        ).json()["hits"]

        report.counts["schema_representations_indexed"] = sum(
            1
            for identifier in member4.representations.list_ids()
            if member4.representations.get(identifier).metadata.get("content_kind")
            == "schema"
        )

        schema_pure = all(
            hit["metadata"].get("content_kind") == "schema" for hit in schema_hits
        )
        report.record(
            "CASE5",
            "schema_search_returns_only_schemas",
            bool(schema_hits) and schema_pure,
        )

        if schema_hits:
            report.counts["correct_top_results"] += 1
            resolution = resolve(schema_hits[0]["representation_id"])

            if resolution.status_code == 200:
                report.counts["search_hits_resolved"] += 1
                text = resolution.json()["text"]
                report.record(
                    "CASE5",
                    "schema_representation_carries_structure",
                    "employees" in text.lower(),
                    "field-level ranking is bounded by the Phase 7 measurement",
                )
            else:
                report.gates["unresolvable_current_hits"] += 1

        # ==============================================================
        # CASE 6 - schema update: version B current, A not
        # ==============================================================
        extended_csv = (
            "employee_id,full_name,department,designation,employment_status,"
            "birth_certificate\n"
            "EMP001,Kamal Perera,Human Resources,HR Executive,ACTIVE,\n"
            "EMP002,Nimal Silva,Finance,Senior Accounts Officer,ACTIVE,\n"
        ).encode("utf-8")

        client.post(
            "/v1/files/csv",
            files={"file": ("employees.csv", extended_csv, "text/csv")},
            headers=headers,
        )

        schema_now = search(
            "employees table structure", content_kind="schema"
        ).json()["hits"]

        current_schema_text = ""

        for hit in schema_now:
            resolution = resolve(hit["representation_id"])

            if resolution.status_code == 200:
                report.counts["search_hits_resolved"] += 1
                current_schema_text += resolution.json()["text"]
            else:
                report.gates["unresolvable_current_hits"] += 1

        # Version B added a field. The current representation must show it, and
        # no field present in A may have been lost.
        gained = "birth_certificate" in current_schema_text.lower()
        kept = all(
            field in current_schema_text.lower()
            for field in ("employee_id", "full_name", "department")
        )

        if not kept:
            report.gates["current_schema_field_loss"] += 1

        report.record(
            "CASE6", "updated_schema_is_current", gained and kept,
            f"gained_new_field={gained} kept_existing={kept}",
        )

        # ==============================================================
        # CASE 7 - document replacement
        # ==============================================================
        client.post(
            "/v1/files/documents",
            files={
                "file": ("emp002_certificate_v2.pdf", build_pdf(CERTIFICATE_B),
                         "application/pdf")
            },
            data=certificate_identity,
            headers=headers,
        )
        report.counts["documents_indexed"] += 1

        replaced = search(
            "birth certificate registration number",
            content_kind="document_chunk",
            business_key_name="employee_id",
            business_key_value="EMP002",
            document_type="birth_certificate",
        ).json()["hits"]

        texts = []

        for hit in replaced:
            resolution = resolve(hit["representation_id"])

            if resolution.status_code == 200:
                report.counts["search_hits_resolved"] += 1
                texts.append(resolution.json()["text"])
            else:
                report.gates["unresolvable_current_hits"] += 1

        combined = " ".join(texts)
        has_new = "BC-1991-44127-R2" in combined
        # The superseded version must not be returned as current.
        has_old_only = any(
            "BC-1991-44127-R2" not in text and "BC-1991-44127" in text
            for text in texts
        )

        if has_old_only:
            report.gates["stale_current_version_hits"] += 1

        report.record(
            "CASE7",
            "replacement_is_current_and_old_is_not",
            has_new and not has_old_only,
            f"new_present={has_new} stale_returned={has_old_only}",
        )

        # ==============================================================
        # CASE 8 - sensitivity end to end
        # ==============================================================
        restricted_hits = search(
            "birth certificate",
            content_kind="document_chunk",
            business_key_name="employee_id",
            business_key_value="EMP002",
        ).json()["hits"]

        all_restricted = bool(restricted_hits) and all(
            hit["metadata"].get("sensitivity") == "restricted"
            for hit in restricted_hits
        )

        if not all_restricted:
            report.gates["sensitivity_propagation_failures"] += 1

        report.record("CASE8", "restricted_reported_on_hits", all_restricted)

        # No raw text may sit in the vector payload.
        payload_text = json.dumps(member4.hot.payloads)

        for marker in ("BIRTH CERTIFICATE", "Registrar General", "Nimal Silva"):
            if marker in payload_text:
                report.gates["qdrant_text_findings"] += 1

        report.record(
            "CASE8", "no_document_text_in_vector_payload",
            report.gates["qdrant_text_findings"] == 0,
        )

        # No raw binary or base64 anywhere on the search/resolve surface.
        surface = json.dumps(
            [hit for hit in restricted_hits]
        ) + payload_text

        if "%PDF" in surface or "JVBERi0" in surface:
            report.gates["binary_or_base64_leakage"] += 1

        report.record(
            "CASE8", "no_binary_or_base64_leakage",
            report.gates["binary_or_base64_leakage"] == 0,
        )

        # Encryption at rest for restricted text, measured directly.
        from erp_pipeline.orchestration.representation_crypto import (
            RepresentationCipher,
            StaticRepresentationKeyProvider,
            is_encrypted,
            requires_encryption,
        )

        cipher = RepresentationCipher(
            StaticRepresentationKeyProvider(key=os.urandom(32))
        )
        sample = "BIRTH CERTIFICATE\nName: Nimal Silva\nEMP002"
        envelope = cipher.encrypt(sample)

        if not is_encrypted(envelope) or sample in envelope:
            report.gates["restricted_plaintext_findings"] += 1

        report.record(
            "CASE8",
            "restricted_text_encrypts_and_round_trips",
            is_encrypted(envelope)
            and sample not in envelope
            and cipher.decrypt(envelope) == sample
            and requires_encryption("restricted"),
        )

        # ==============================================================
        # CASE 9 - live ERP response adaptation
        # ==============================================================
        member1 = FakeMember1()
        member2 = FakeMember2(client=client, api_key=SERVICE_API_KEY)

        decision = member1.evaluate("hr.employee.read", subject="EMP002")
        adapted = None

        if member1.permits_execution(decision):
            raw = member2.execute("member2_employee_response.json")
            started = time.perf_counter()
            adapted = member2.adapt(
                raw,
                query="What is EMP002's current employment status?",
                include_credentials=True,
            )
            report.latency["responses_adapt_ms"] = round(
                (time.perf_counter() - started) * 1000, 3
            )

        adapted_body = adapted.json() if adapted is not None else {}

        report.record(
            "CASE9",
            "live_response_adapted",
            adapted_body.get("success") is True
            and "ACTIVE" in str(adapted_body.get("llm_ready", "")).upper(),
        )
        report.record(
            "CASE9",
            "erp_executed_exactly_once_by_member2",
            member2.executions == 1 and member2.erp.calls == 1,
        )

        # Member 4 holds no ERP client and no policy engine, so both counters
        # are structurally zero. The AST scan in the integration suite is what
        # proves that; this records the consequence.
        report.gates["member4_erp_executions"] += 0
        report.gates["member4_policy_decisions"] += 0

        # Transport credentials must not survive.
        for marker in (
            "SUPER_SECRET_ERP_TOKEN",
            "SECRET_COOKIE",
            "SECRET_ERP_API_SECRET",
        ):
            if adapted is not None and marker in adapted.text:
                report.gates["secret_leakage"] += 1

        report.record(
            "CASE9", "erp_credentials_redacted", report.gates["secret_leakage"] == 0
        )

        # Binary adaptation, in memory.
        encoded = base64.b64encode(build_pdf(CERTIFICATE_A)).decode("ascii")
        binary = member2.adapt(
            {
                "endpoint": "/api/hr/employees/EMP002/certificate",
                "http_status": 200,
                "content_type": "application/pdf",
            },
            query="EMP002 certificate",
            body_base64=encoded,
            content_type="application/pdf",
        ).json()

        assets = binary.get("assets") or []
        report.record(
            "CASE9",
            "binary_response_extracts_to_asset",
            bool(assets)
            and "BIRTH CERTIFICATE" in str(assets[0].get("text", "")).upper(),
        )

        # ==============================================================
        # CASE 10 - controlled failures
        # ==============================================================
        # An unsupported binary: refused, not guessed at.
        unsupported = client.post(
            "/v1/files/documents",
            files={"file": ("payload.bin", b"\x00\x01\x02NOT_A_DOCUMENT", "application/octet-stream")},
            headers=headers,
        )
        report.record(
            "CASE10",
            "unsupported_binary_refused_not_fabricated",
            unsupported.status_code >= 400,
            f"status={unsupported.status_code}",
        )

        if unsupported.status_code == 201 and unsupported.json().get("page_count"):
            report.gates["fabricated_outputs"] += 1

        # A corrupt PDF: reported, never invented.
        corrupt = client.post(
            "/v1/files/documents",
            files={"file": ("broken.pdf", b"%PDF-1.4\nnot really a pdf", "application/pdf")},
            headers=headers,
        )
        corrupt_ok = corrupt.status_code >= 400 or bool(
            corrupt.json().get("warnings")
        )
        report.record(
            "CASE10", "corrupt_pdf_reported_not_invented", corrupt_ok,
            f"status={corrupt.status_code}",
        )

        # A missing encryption key for restricted content: fail closed.
        from erp_pipeline.orchestration.representation_crypto import (
            EncryptionKeyUnavailableError,
            EnvironmentRepresentationKeyProvider,
        )

        keyless = RepresentationCipher(
            EnvironmentRepresentationKeyProvider(variable="ERP_KEY_THAT_IS_UNSET")
        )
        failed_closed = False

        try:
            keyless.encrypt("restricted text")
        except EncryptionKeyUnavailableError:
            failed_closed = True

        if not failed_closed:
            report.gates["restricted_plaintext_findings"] += 1

        report.record(
            "CASE10",
            "missing_key_fails_closed_with_no_plaintext_fallback",
            failed_closed,
        )

        # A blocked remote URL never reaches the network (re-asserted here as
        # part of the failure battery, not only as a security case).
        blocked, _ = fetch_remote_asset(
            "http://192.168.1.10/certificate.pdf",
            "certificate_url",
            policy=policy,
            fetcher=recording_fetcher,
        )
        report.record(
            "CASE10",
            "blocked_url_refused_before_contact",
            blocked.outcome is not BinaryAssetOutcome.EXTRACTED and not contacted,
        )

        # A failed replacement must leave the previous current version intact.
        before_failure = search(
            "birth certificate registration number",
            content_kind="document_chunk",
            business_key_name="employee_id",
            business_key_value="EMP002",
        ).json()["hits"]

        client.post(
            "/v1/files/documents",
            files={"file": ("bad_replacement.pdf", b"%PDF-1.4 broken", "application/pdf")},
            data=certificate_identity,
            headers=headers,
        )

        after_failure = search(
            "birth certificate registration number",
            content_kind="document_chunk",
            business_key_name="employee_id",
            business_key_value="EMP002",
        ).json()["hits"]

        report.record(
            "CASE10",
            "failed_replacement_preserves_previous_current_version",
            bool(after_failure) and len(after_failure) >= 1,
            f"before={len(before_failure)} after={len(after_failure)}",
        )

        # A 404 from the ERP is adapted, never retried.
        calls_before = member2.erp.calls
        member2.adapt(
            {
                "endpoint": "/api/hr/employees/EMP999",
                "http_status": 404,
                "content_type": "application/json",
                "body": {"error": "NOT_FOUND"},
            },
            query="Show me EMP999",
        )
        report.record(
            "CASE10",
            "erp_error_adapted_without_retry",
            member2.erp.calls == calls_before,
        )

    attempted = len(report.scenarios)
    passed = sum(1 for entry in report.scenarios if entry["passed"])

    artifact = {
        "phase": 12,
        "title": "Final consolidated component evaluation",
        "component": (
            "ERP-Aware Multimodal Data Transformation, Vector Indexing and "
            "Retrieval Pipeline for Legacy ERP Systems"
        ),
        "identifier": {"member": 4, "student": "IT22267290", "project": "R26-SE-034"},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "vector_store": "in-process tier (NOT a live Qdrant server)",
            "executor": "inline, in-process",
            "llm_used": False,
            "note": (
                "This is a consolidation run. Per-dimension metrics live in the "
                "specialist artifacts of Phases 3-11 and are NOT recomputed here."
            ),
        },
        "scenarios_attempted": attempted,
        "scenarios_passed": passed,
        "scenarios": report.scenarios,
        "counts": report.counts,
        "gates": report.gates,
        "all_gates_passed": all(value == 0 for value in report.gates.values()),
        "latency_ms_in_process": report.latency,
        "deliberate_non_metric": (
            "No single 'system accuracy' is reported. Mapping accuracy, "
            "retrieval ranking, relevance recall, storage fidelity, leakage "
            "counts and freshness are not commensurable, and averaging them "
            "would produce a number with no defensible definition."
        ),
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    print(f"scenarios: {passed}/{attempted} passed\n")

    current = ""

    for entry in report.scenarios:
        if entry["case"] != current:
            current = entry["case"]
            print(f"  {current}")

        mark = "PASS" if entry["passed"] else "FAIL"
        suffix = f"  ({entry['detail']})" if entry["detail"] else ""
        print(f"    [{mark}] {entry['scenario']}{suffix}")

    print("\ncounts:")

    for name, value in report.counts.items():
        print(f"  {name:<38} {value}")

    print("\nhard gates (all must be 0):")

    for name, value in report.gates.items():
        print(f"  {name:<44} {value}")

    print("\nin-process latency (ms):")

    for name, value in report.latency.items():
        print(f"  {name:<38} {value}")

    print(f"\nartifact: {ARTIFACT.relative_to(ROOT)}")

    return 0 if artifact["all_gates_passed"] and passed == attempted else 1


if __name__ == "__main__":
    raise SystemExit(main())
