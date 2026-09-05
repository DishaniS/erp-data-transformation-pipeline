"""Phase 8 mini-evaluation: declared remote ERP assets, safely.

Drives a deterministic corpus of URL references through the real pipeline with
an INJECTED fetcher. No socket is opened and no public internet is touched, so
every "refused" result is a policy decision rather than a network failure.

The recording fetcher is the instrument that matters: a refusal only counts if
the fetcher was never called, and that is measured rather than assumed.

Run:
    python scripts/evaluate_remote_asset_security.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from erp_pipeline.ingestion.binary_assets import BinaryAssetOutcome
from erp_pipeline.ingestion.remote_assets import RemoteAssetOutcome
from erp_pipeline.orchestration.multimodal import extract_record_assets
from erp_pipeline.response_adaptation.assets import FetchedAsset, UrlSafetyPolicy
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.storage.migration import _payload_for
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

ARTIFACT = ROOT / "artifacts" / "remote_asset_security_evaluation.json"

SECRET = "SUPERSECRETTOKEN"
PUBLIC = "93.184.216.34"
HOST = "assets.example.test"


def pdf_bytes(text: str) -> bytes:
    import pymupdf as fitz

    document = fitz.open()
    document.new_page().insert_text((72, 96), text)
    payload = document.tobytes()
    document.close()

    return payload


def image_of_text(text: str) -> bytes:
    import pymupdf as fitz

    typed = fitz.open()
    typed.new_page(width=420, height=180).insert_text((28, 100), text, fontsize=26)
    bitmap = typed.load_page(0).get_pixmap(dpi=300).tobytes("png")
    typed.close()

    return bitmap


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name, normalized_name=name, source_data_type="X",
        normalized_data_type=data_type, is_primary_key=primary,
        nullable=not primary,
    )


EMPLOYEES = SourceEntity(
    entity_id="legacy_hr.public.employees", source_name="employees",
    normalized_name="employees", entity_kind=EntityKind.TABLE,
    primary_key_fields=("employee_id",),
    fields=(
        _field("employee_id", FieldDataType.STRING, primary=True),
        _field("full_name", FieldDataType.STRING),
        _field("website", FieldDataType.STRING),
        _field("birth_certificate_url", FieldDataType.STRING),
    ),
)

ENABLED = UrlSafetyPolicy(enabled=True, max_redirects=1)
DISABLED = UrlSafetyPolicy()


class Recorder:
    """Records every URL a fetch was actually attempted for.

    ``contacted`` is the evaluation's most important number: a refusal that
    still opened a socket is not a refusal.
    """

    def __init__(self):
        self.contacted: list[str] = []

    def make(self, body: bytes, content_type: str | None, final_url=None,
             error: Exception | None = None):
        def fetch(validated):
            self.contacted.append(validated.url)

            if error is not None:
                raise error

            return FetchedAsset(
                body=body, content_type=content_type, final_url=final_url
            )

        return fetch


def main() -> int:
    recorder = Recorder()
    certificate = pdf_bytes("BIRTH CERTIFICATE Registrar General Colombo")
    amended = pdf_bytes("BIRTH CERTIFICATE amended copy")
    scan = image_of_text("BIRTH CERTIFICATE")
    signed = f"https://{HOST}/emp002-cert.pdf?token={SECRET}&expires=1735689600"

    def resolve_public(host):
        return (PUBLIC,)

    def resolve_private(host):
        return ("127.0.0.1",)

    def resolve_metadata(host):
        return ("169.254.169.254",)

    #: (label, employee, url, policy, fetcher, resolver, declared, expect_contact)
    cases = [
        ("pdf url", "EMP002", f"https://{HOST}/c.pdf", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, True, True),
        ("image url", "EMP001", f"https://{HOST}/scan.png", ENABLED,
         recorder.make(scan, "image/png"), resolve_public, True, True),
        ("octet-stream pdf", "EMP005", f"https://{HOST}/blob", ENABLED,
         recorder.make(certificate, "application/octet-stream"), resolve_public,
         True, True),
        ("mime lies (jpeg->pdf)", "EMP006", f"https://{HOST}/p.jpg", ENABLED,
         recorder.make(certificate, "image/jpeg"), resolve_public, True, True),
        ("signed url", "EMP007", signed, ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, True, True),
        # Shared URL across two employees.
        ("shared url A", "EMP002", f"https://{HOST}/shared.pdf", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, True, True),
        ("shared url B", "EMP003", f"https://{HOST}/shared.pdf", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, True, True),
        # Same content behind a different URL.
        ("moved url", "EMP002", f"https://cdn.example.test/moved.pdf", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, True, True),
        # Same URL, changed content.
        ("changed content", "EMP008", f"https://{HOST}/c.pdf", ENABLED,
         recorder.make(amended, "application/pdf"), resolve_public, True, True),
        # -- refusals: the fetcher must never be called --
        ("private ip", "EMP010", "https://internal.example.test/c.pdf", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_private, True, False),
        ("cloud metadata", "EMP011", "https://meta.example.test/latest", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_metadata, True, False),
        ("http scheme", "EMP012", f"http://{HOST}/c.pdf", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, True, False),
        ("credentials in url", "EMP013", f"https://u:p@{HOST}/c.pdf", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, True, False),
        ("fetching disabled", "EMP014", f"https://{HOST}/c.pdf", DISABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, True, False),
        ("not a url", "EMP015", "not a url", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, True, False),
        ("undeclared field", "EMP016", f"https://{HOST}/c.pdf", ENABLED,
         recorder.make(certificate, "application/pdf"), resolve_public, False, False),
        # -- permitted, then refused on the response --
        ("redirect to private", "EMP017", f"https://{HOST}/c.pdf", ENABLED,
         recorder.make(certificate, "application/pdf",
                       final_url="https://internal.example.test/p.pdf"),
         lambda h: ("127.0.0.1",) if "internal" in h else (PUBLIC,), True, True),
        ("oversized", "EMP018", f"https://{HOST}/big.pdf",
         UrlSafetyPolicy(enabled=True, max_bytes=32),
         recorder.make(certificate, "application/pdf"), resolve_public, True, True),
        ("timeout", "EMP019", f"https://{HOST}/slow.pdf", ENABLED,
         recorder.make(b"", None, error=TimeoutError("read timed out")),
         resolve_public, True, True),
        ("remote 404", "EMP020", f"https://{HOST}/missing.pdf", ENABLED,
         recorder.make(b"", None, error=RuntimeError("unexpected status 404")),
         resolve_public, True, True),
        ("html page", "EMP021", f"https://{HOST}/page", ENABLED,
         recorder.make(b"<html><a href='/x'>l</a></html>", "text/html"),
         resolve_public, True, True),
        ("zip", "EMP022", f"https://{HOST}/bundle.zip", ENABLED,
         recorder.make(b"PK\x03\x04" + b"\x00" * 200, "application/pdf"),
         resolve_public, True, True),
    ]

    results: list[dict] = []
    representations: list = []
    attempted = permitted = refused = extracted_ok = 0
    unexpected_contact = 0
    validate_times: list[float] = []
    fetch_times: list[float] = []
    all_surface: list[str] = []

    for (label, employee, url, policy, fetcher, resolver, declared,
         expect_contact) in cases:
        before = len(recorder.contacted)
        record = SourceRecord.from_mapping({
            "employee_id": employee,
            "full_name": f"Name {employee}",
            "website": "https://intranet.example.test/staff",
            "birth_certificate_url": url,
        })
        declared_fields = (
            {"birth_certificate_url": "birth_certificate"} if declared else {}
        )
        canonical = SourceNativeTransformer().transform_records(
            [record], EMPLOYEES, "legacy_hr", SourceType.POSTGRESQL,
            asset_url_fields=tuple(declared_fields),
        ).records

        started = time.perf_counter()
        outcome = extract_record_assets(
            [record], canonical, EMPLOYEES, (),
            asset_url_fields=declared_fields, url_policy=policy,
            fetcher=fetcher, resolver=resolver,
        )
        elapsed = (time.perf_counter() - started) * 1000
        contacted = len(recorder.contacted) > before

        if declared:
            attempted += 1

            if contacted:
                permitted += 1
                fetch_times.append(elapsed)
            else:
                refused += 1
                validate_times.append(elapsed)

        if contacted and not expect_contact:
            unexpected_contact += 1

        asset = outcome.assets[0] if outcome.assets else None
        result_outcome = asset.outcome if asset else "no_reference"

        if result_outcome == BinaryAssetOutcome.EXTRACTED:
            extracted_ok += 1

        representations.extend(outcome.representations)
        all_surface.append(
            json.dumps(
                [item.to_dict() for item in outcome.representations]
                + [dict(item.metadata) for item in outcome.representations]
                + [record_.to_json_dict() for record_ in canonical]
                + [a.to_dict() for a in outcome.assets]
                + list(outcome.warnings),
                default=str,
            )
        )
        results.append({
            "case": label, "employee": employee, "declared": declared,
            "outcome": result_outcome, "contacted_remote": contacted,
            "expected_contact": expect_contact,
            "representations": len(outcome.representations),
            "elapsed_ms": round(elapsed, 3),
        })

    # ---- identity integrity ----
    by_employee: dict[str, set[str]] = {}

    for item in representations:
        by_employee.setdefault(
            item.metadata.get("business_key_value"), set()
        ).add(item.representation_id)

    wrong_employee = 0

    for employee, ids in by_employee.items():
        for identifier in ids:
            match = next(
                item for item in representations
                if item.representation_id == identifier
            )
            parent = match.metadata.get("parent_record_id") or ""

            if employee and employee.lower() not in parent.lower():
                wrong_employee += 1

    # An association collision is ONE VECTOR claimed by TWO DIFFERENT PARENTS -
    # the Phase 3 failure where one employee's certificate overwrites another's.
    #
    # It is NOT "the same vector id appearing twice". EMP002's certificate
    # reached this corpus three times (a plain URL, a shared URL and a moved
    # URL serving identical bytes), and all three SHOULD resolve to one
    # representation: same employee, same field, same content is one
    # attachment, which is exactly the idempotency the moved-URL case exists to
    # demonstrate. Counting those as collisions measured re-indexing, not
    # correctness.
    parents_by_vector: dict[str, set[str]] = {}

    for item in representations:
        parents_by_vector.setdefault(item.vector_id, set()).add(
            item.metadata.get("parent_record_id") or ""
        )

    collisions = sum(
        1 for parents in parents_by_vector.values() if len(parents) > 1
    )
    distinct_attachments = len(parents_by_vector)

    # ---- leakage ----
    surface = "\n".join(all_surface)
    secret_leaks = [
        marker for marker in (SECRET, "token=", "expires=") if marker in surface
    ]
    binary_leaks = [
        marker for marker in ("JVBERi0x", "iVBORw0KGgo", "%PDF-")
        if marker in surface
    ]
    html_indexed = sum(
        1 for item in results
        if item["case"] == "html page" and item["representations"] > 0
    )

    # ---- payload check ----
    from erp_pipeline.ai.service import _carried_identity
    from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier

    payload_surface = ""

    for item in representations:
        carried = _carried_identity(item)
        state = StorageRecordMetadata(
            representation_id="r", embedding_id="e", vector_id="v",
            current_tier=StorageTier.HOT, content_hash="h", model_id="m",
            dimension=4,
            **{k: v for k, v in carried.items()
               if k in {"source_system_id", "source_entity", "document_id",
                        "content_kind", "parent_record_id", "source_field",
                        "business_key_name", "business_key_value",
                        "document_type"}},
        )
        payload_surface += json.dumps(_payload_for(state), default=str)

    url_in_payload = any(
        marker in payload_surface
        for marker in (SECRET, "token=", "https://", HOST)
    )

    gates_ok = (
        unexpected_contact == 0
        and wrong_employee == 0
        and collisions == 0
        and not secret_leaks
        and not binary_leaks
        and html_indexed == 0
        and not url_in_payload
    )

    def percentile(values, fraction):
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[max(0, int(len(ordered) * fraction) - 1)]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "fetcher": "injected recorder - NO network, NO sockets opened",
            "note": (
                "elapsed times are in-process validation and extraction only; "
                "no internet round trip is included or simulated"
            ),
        },
        "cases": results,
        "counts": {
            "remote_references_attempted": attempted,
            "requests_permitted": permitted,
            "requests_refused_before_contact": refused,
            "successful_extractions": extracted_ok,
            "representations_indexed": len(representations),
            "distinct_attachments": distinct_attachments,
        },
        "gates": {
            "private_or_internal_targets_contacted": unexpected_contact,
            "wrong_employee_matches": wrong_employee,
            "association_collisions": collisions,
            "secret_url_leakage": len(secret_leaks),
            "secret_markers": secret_leaks,
            "raw_binary_or_base64_leakage": len(binary_leaks),
            "html_pages_indexed": html_indexed,
            "raw_url_in_vector_payload": url_in_payload,
        },
        "latency_ms": {
            "policy_validation_median": round(
                statistics.median(validate_times) if validate_times else 0.0, 4
            ),
            "fetch_and_extract_median": round(
                statistics.median(fetch_times) if fetch_times else 0.0, 4
            ),
            "fetch_and_extract_p95": round(percentile(fetch_times, 0.95), 4),
        },
        "passed": gates_ok,
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("=" * 76)
    print("PHASE 8 MINI-EVALUATION - declared remote ERP assets")
    print("=" * 76)
    print("fetcher: injected recorder (no sockets, no internet)")
    print()
    print(f"{'case':<26}{'declared':>9}{'contacted':>11}  outcome")

    for item in results:
        flag = "" if item["contacted_remote"] == item["expected_contact"] else "  <-- UNEXPECTED"
        print(f"  {item['case']:<24}{str(item['declared']):>9}"
              f"{str(item['contacted_remote']):>11}  {item['outcome']}{flag}")

    print()
    print(f"remote references attempted       {attempted}")
    print(f"requests permitted                {permitted}")
    print(f"refused before any contact        {refused}")
    print(f"successful extractions            {extracted_ok}")
    print(f"representations indexed           {len(representations)}")
    print()
    print(f"private/internal targets contacted {unexpected_contact}")
    print(f"wrong employee matches             {wrong_employee}")
    print(f"association collisions             {collisions}")
    print(f"secret URL leakage                 {len(secret_leaks)} {secret_leaks or ''}")
    print(f"raw binary / base64 leakage        {len(binary_leaks)}")
    print(f"HTML pages indexed                 {html_indexed}")
    print(f"raw URL in vector payload          {url_in_payload}")
    print()
    print(f"policy validation   median {report['latency_ms']['policy_validation_median']:.3f} ms")
    print(f"fetch + extract     median {report['latency_ms']['fetch_and_extract_median']:.3f} ms"
          f"   p95 {report['latency_ms']['fetch_and_extract_p95']:.3f} ms")
    print()
    print(f"artifact -> {ARTIFACT.relative_to(ROOT)}")
    print("=" * 76)
    print(f"GATES: {'PASS' if gates_ok else 'FAIL'}")

    return 0 if gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
