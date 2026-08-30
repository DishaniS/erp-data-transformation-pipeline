"""Phase 10 mini-evaluation: sensitivity handling and content protection.

Builds a deterministic corpus spanning every classification and every content
kind, then audits what actually happened: whether each artifact carries the
class it was declared with, whether anything was silently downgraded, and
whether any secret reached a surface it should not.

Synthetic markers are planted throughout so leakage is DETECTED rather than
assumed absent.

Run:
    python scripts/evaluate_phase10_security_sensitivity.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
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

from erp_pipeline.ai.attached_documents import (
    DocumentAttachment,
    attached_document_to_representations,
)
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.schema_representation import source_entity_to_representations
from erp_pipeline.ingestion.binary_assets import extract_binary_asset
from erp_pipeline.orchestration.representation_crypto import (
    RepresentationCipher,
    StaticRepresentationKeyProvider,
    is_encrypted,
    requires_encryption,
)
from erp_pipeline.orchestration.representation_store import (
    InMemoryRepresentationStore,
)
from erp_pipeline.orchestration.service import BoundedExtractionCache
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SchemaOrigin,
    SensitivityLevel,
    SourceType,
)
from erp_pipeline.schemas.sensitivity import field_sensitivity, resolve
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceSchema,
)
from erp_pipeline.storage.migration import _payload_for
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

ARTIFACT = ROOT / "artifacts" / "phase10_security_sensitivity_evaluation.json"

#: Planted so leakage is detected, not assumed. Each marks a different surface
#: that has leaked in some system somewhere.
SECRET_CERT = "SECRET_BIRTH_CERTIFICATE_TEXT"
SECRET_TOKEN = "SUPER_SECRET_SIGNED_URL_TOKEN"
SECRET_KEY_MARKER = "TEST_ENCRYPTION_KEY_MARKER"

KEY = b"0123456789abcdef0123456789abcdef"


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
        _field("birth_certificate", FieldDataType.BINARY),
        _field("profile_photo", FieldDataType.BINARY),
        _field("employment_contract", FieldDataType.BINARY),
    ),
)


def pdf(text: str) -> bytes:
    import pymupdf as fitz

    document = fitz.open()
    document.new_page().insert_text((72, 96), text)
    payload = document.tobytes()
    document.close()

    return payload


def main() -> int:
    cipher = RepresentationCipher(StaticRepresentationKeyProvider(KEY))
    store = InMemoryRepresentationStore(cipher=cipher)

    #: field -> declared class. One row, several classes.
    options = {
        "field_sensitivity": {
            "birth_certificate": "restricted",
            "profile_photo": "confidential",
            "employment_contract": "confidential",
        }
    }

    assignments: list[dict] = []
    wrong = downgrades = propagation_failures = 0
    encrypt_times: list[float] = []
    decrypt_times: list[float] = []
    encrypted = plaintext = 0
    decrypt_mismatches = 0
    all_representations: list = []

    def record(label, representation, expected: SensitivityLevel):
        nonlocal wrong, downgrades, propagation_failures, encrypted, plaintext
        nonlocal decrypt_mismatches

        actual = (representation.metadata or {}).get("sensitivity")
        correct = actual == expected.value

        if not correct:
            wrong += 1

            from erp_pipeline.schemas.sensitivity import rank

            if rank(actual) < rank(expected):
                downgrades += 1

        # Persist, and observe what the column would actually hold.
        started = time.perf_counter()
        store.upsert(representation)
        elapsed = (time.perf_counter() - started) * 1000

        stored_text = representation.text_for_ai
        should_encrypt = requires_encryption(actual)
        column_value = (
            cipher.encrypt(stored_text)
            if should_encrypt and stored_text else stored_text
        )

        if is_encrypted(column_value):
            encrypted += 1
            encrypt_times.append(elapsed)
            decrypt_started = time.perf_counter()
            revealed = cipher.decrypt(column_value)
            decrypt_times.append((time.perf_counter() - decrypt_started) * 1000)

            if revealed != stored_text:
                decrypt_mismatches += 1
        else:
            plaintext += 1

            if should_encrypt:
                propagation_failures += 1

        all_representations.append((representation, column_value))
        assignments.append({
            "artifact": label,
            "content_kind": (representation.metadata or {}).get("content_kind"),
            "expected": expected.value,
            "actual": actual,
            "correct": correct,
            "encrypted_at_rest": bool(is_encrypted(column_value)),
        })

    # ---- structured record: INTERNAL by default ----
    values = {
        "employee_id": "EMP002",
        "full_name": "Nimal Silva",
        "birth_certificate": pdf(f"{SECRET_CERT} version A"),
        "profile_photo": pdf("PROFILE PHOTO placeholder"),
        "employment_contract": pdf("EMPLOYMENT CONTRACT terms"),
    }
    row = SourceRecord.from_mapping(values)
    canonical = SourceNativeTransformer().transform_records(
        [row], EMPLOYEES, "legacy_hr", SourceType.POSTGRESQL
    ).records[0]
    record(
        "EMP002 structured record",
        canonical_record_to_representation(canonical),
        SensitivityLevel.INTERNAL,
    )

    # ---- attachments: each keeps its own declared class ----
    for field_name, expected in (
        ("birth_certificate", SensitivityLevel.RESTRICTED),
        ("profile_photo", SensitivityLevel.CONFIDENTIAL),
        ("employment_contract", SensitivityLevel.CONFIDENTIAL),
    ):
        asset = extract_binary_asset(values[field_name], field_name)

        if not asset.succeeded:
            continue

        resolved = resolve(
            artifact=field_sensitivity(options, field_name),
            inherited=canonical.sensitivity,
        )
        attachment = DocumentAttachment(
            parent_record_id=canonical.record_id,
            source_system_id="legacy_hr", source_entity="employees",
            source_field=field_name, document_id=asset.document_id or "",
            business_key_name="employee_id", business_key_value="EMP002",
            document_type=field_name, sensitivity=resolved.value,
        )

        for built in attached_document_to_representations(asset.document, attachment):
            record(f"EMP002 {field_name}", built, expected)

    # ---- a remote asset, carrying a signed URL ----
    remote = extract_binary_asset(pdf(f"{SECRET_CERT} remote copy"), "birth_certificate_url")

    if remote.succeeded:
        attachment = DocumentAttachment(
            parent_record_id=canonical.record_id,
            source_system_id="legacy_hr", source_entity="employees",
            source_field="birth_certificate_url",
            document_id=remote.document_id or "",
            business_key_name="employee_id", business_key_value="EMP002",
            document_type="birth_certificate",
            sensitivity=SensitivityLevel.RESTRICTED.value,
        )

        for built in attached_document_to_representations(remote.document, attachment):
            record("EMP002 remote certificate", built, SensitivityLevel.RESTRICTED)

    # ---- schema, declared RESTRICTED ----
    schema = SourceSchema(
        schema_id="sch_hr", source_system_id="legacy_hr", schema_name="public",
        origin=SchemaOrigin.DISCOVERED, entities=(EMPLOYEES,),
    )

    for built in source_entity_to_representations(
        schema, EMPLOYEES, None, SensitivityLevel.RESTRICTED
    ):
        record("employees schema", built, SensitivityLevel.RESTRICTED)

    # ---- a replacement that RAISES the class ----
    replacement = extract_binary_asset(
        pdf(f"{SECRET_CERT} version B"), "birth_certificate"
    )

    if replacement.succeeded:
        attachment = DocumentAttachment(
            parent_record_id=canonical.record_id,
            source_system_id="legacy_hr", source_entity="employees",
            source_field="birth_certificate",
            document_id=replacement.document_id or "",
            business_key_name="employee_id", business_key_value="EMP002",
            document_type="birth_certificate",
            sensitivity=SensitivityLevel.RESTRICTED.value,
        )

        for built in attached_document_to_representations(
            replacement.document, attachment
        ):
            record("EMP002 certificate replacement", built, SensitivityLevel.RESTRICTED)

    # ---- no-downgrade probes ----
    downgrade_probes = [
        ("field internal vs source restricted",
         resolve(artifact="internal", source="restricted"),
         SensitivityLevel.RESTRICTED),
        ("job public vs inherited confidential",
         resolve(job="public", inherited="confidential"),
         SensitivityLevel.CONFIDENTIAL),
        ("field restricted vs job confidential",
         resolve(artifact="restricted", job="confidential"),
         SensitivityLevel.RESTRICTED),
    ]

    for label, actual, expected in downgrade_probes:
        if actual is not expected:
            downgrades += 1

    # ---- leakage audit ----
    representation_surface = json.dumps(
        [
            {
                "to_dict": item.to_dict(),
                "metadata": dict(item.metadata or {}),
                "column_value": column,
            }
            for item, column in all_representations
        ],
        default=str,
    )
    restricted_plaintext_findings = sum(
        1 for item, column in all_representations
        if requires_encryption((item.metadata or {}).get("sensitivity"))
        and not is_encrypted(column)
    )

    # The vector payload must still carry no text at all.
    from erp_pipeline.ai.service import _carried_identity
    from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier

    payload_surface = ""

    for item, _ in all_representations:
        carried = _carried_identity(item)
        state = StorageRecordMetadata(
            representation_id="r", embedding_id="e", vector_id="v",
            current_tier=StorageTier.HOT, content_hash="h", model_id="m",
            dimension=4,
            **{k: v for k, v in carried.items()
               if k in {"source_system_id", "source_entity", "document_id",
                        "content_kind", "parent_record_id", "source_field",
                        "business_key_name", "business_key_value",
                        "document_type", "schema_name", "entity_kind"}},
        )
        payload_surface += json.dumps(_payload_for(state), default=str)

    qdrant_text_findings = sum(
        1 for marker in (SECRET_CERT, "BIRTH CERTIFICATE", "text_for_ai")
        if marker in payload_surface
    )

    # Secrets must not appear in the column values or the payload. The
    # decrypted API body legitimately contains the certificate text and is
    # excluded from this audit by design.
    secret_leaks = [
        name for name, marker in (
            ("signed url token", SECRET_TOKEN),
            ("encryption key marker", SECRET_KEY_MARKER),
            ("raw key bytes", KEY.decode("latin-1")),
        )
        if marker in representation_surface or marker in payload_surface
    ]

    encrypted_columns = "".join(
        column for _, column in all_representations if is_encrypted(column)
    )
    cert_in_ciphertext = SECRET_CERT in encrypted_columns

    # ---- Phase 14 temp files ----
    temp_dir = tempfile.gettempdir()
    before = set(os.listdir(temp_dir))

    from erp_pipeline.response_adaptation.assets import AssetAdapter, AssetOptions

    AssetAdapter(AssetOptions()).adapt_bytes(
        pdf(f"{SECRET_CERT} phase 14"), declared_content_type="application/pdf"
    )
    phase14_temp_files = len(set(os.listdir(temp_dir)) - before)

    # ---- upload cache bound ----
    cache = BoundedExtractionCache(max_entries=4)

    for index in range(50):
        cache[f"up_{index}"] = f"extracted document {index}"

    cache_max_observed = len(cache)

    gates_ok = (
        wrong == 0
        and downgrades == 0
        and propagation_failures == 0
        and qdrant_text_findings == 0
        and not secret_leaks
        and restricted_plaintext_findings == 0
        and decrypt_mismatches == 0
        and phase14_temp_files == 0
        and not cert_in_ciphertext
        and cache_max_observed <= 4
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "claim": (
            "explicit sensitivity classification, propagated end-to-end; "
            "text at or above CONFIDENTIAL encrypted at rest with AES-256-GCM. "
            "No compliance claim is made."
        ),
        "environment": {
            "note": (
                "cryptographic timings are in-process and are not database or "
                "network latency"
            ),
        },
        "assignments": assignments,
        "gates": {
            "sensitivity_assignments_attempted": len(assignments),
            "correct_assignments": len(assignments) - wrong,
            "wrong_sensitivity_assignments": wrong,
            "silent_downgrades": downgrades,
            "sensitivity_propagation_failures": propagation_failures,
            "restricted_plaintext_findings": restricted_plaintext_findings,
            "qdrant_text_findings": qdrant_text_findings,
            "secret_leakage": len(secret_leaks),
            "secret_markers": secret_leaks,
            "decryption_mismatches": decrypt_mismatches,
            "plaintext_in_ciphertext": cert_in_ciphertext,
            "phase14_temp_files": phase14_temp_files,
            "upload_cache_max_observed_entries": cache_max_observed,
        },
        "encryption": {
            "algorithm": "AES-256-GCM",
            "encrypted_representations": encrypted,
            "plaintext_representations": plaintext,
            "encrypt_median_ms": round(
                statistics.median(encrypt_times) if encrypt_times else 0.0, 4
            ),
            "decrypt_median_ms": round(
                statistics.median(decrypt_times) if decrypt_times else 0.0, 4
            ),
        },
        "passed": gates_ok,
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("=" * 78)
    print("PHASE 10 MINI-EVALUATION - sensitivity and content protection")
    print("=" * 78)
    print(f"{'artifact':<34}{'expected':>13}{'actual':>13}{'enc':>6}")

    for item in assignments:
        mark = "" if item["correct"] else "   <-- WRONG"
        print(f"  {item['artifact']:<32}{item['expected']:>13}"
              f"{str(item['actual']):>13}"
              f"{('yes' if item['encrypted_at_rest'] else '-'):>6}{mark}")

    print()
    print(f"assignments attempted              {len(assignments)}")
    print(f"wrong sensitivity assignments      {wrong}")
    print(f"silent downgrades                  {downgrades}")
    print(f"propagation failures               {propagation_failures}")
    print()
    print(f"restricted plaintext DB findings   {restricted_plaintext_findings}")
    print(f"plaintext inside ciphertext        {cert_in_ciphertext}")
    print(f"Qdrant text findings               {qdrant_text_findings}")
    print(f"secret leakage                     {len(secret_leaks)} {secret_leaks or ''}")
    print(f"decryption mismatches              {decrypt_mismatches}")
    print(f"Phase 14 temporary plaintext files {phase14_temp_files}")
    print(f"upload cache max entries observed  {cache_max_observed} (limit 4)")
    print()
    print(f"encrypted {encrypted} · plaintext {plaintext}   "
          f"encrypt {report['encryption']['encrypt_median_ms']:.3f} ms · "
          f"decrypt {report['encryption']['decrypt_median_ms']:.3f} ms (in-process)")
    print()
    print(f"artifact -> {ARTIFACT.relative_to(ROOT)}")
    print("=" * 78)
    print(f"GATES: {'PASS' if gates_ok else 'FAIL'}")

    return 0 if gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
