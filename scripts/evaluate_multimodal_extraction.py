"""Phase 3 mini-evaluation: database BLOBs through to vectors.

Measures the two gates the phase is judged on - binary/base64 leakage and
association collisions - plus the extraction outcome distribution across a
deliberately adversarial corpus of BLOBs.

This is an evaluation harness, not a test. It builds its own corpus, prints a
table, and writes a JSON artifact. It asserts nothing; the pass/fail judgement
belongs to the report that reads its output.

Run:
    python scripts/evaluate_multimodal_extraction.py
"""

from __future__ import annotations

import base64
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The ingestion package never reads .env - that is an application concern - so
# the harness loads it, exactly as the test conftest does, to pick up
# TESSERACT_CMD.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from erp_pipeline.ai.attached_documents import DOCUMENT_ENTITY_TYPE
from erp_pipeline.ingestion.binary_assets import (
    BinaryAssetOutcome,
    binary_field_names_for_entity,
)
from erp_pipeline.ingestion.ocr import probe_ocr
from erp_pipeline.orchestration.multimodal import extract_record_assets
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

ARTIFACT = ROOT / "artifacts" / "multimodal_extraction_evaluation.json"


# ----------------------------------------------------------------------
# Corpus
# ----------------------------------------------------------------------


def _text_pdf(lines: list[str]) -> bytes:
    import pymupdf as fitz

    document = fitz.open()
    page = document.new_page()

    for index, line in enumerate(lines):
        page.insert_text((72, 96 + index * 20), line, fontsize=12)

    payload = document.tobytes()
    document.close()

    return payload


def _scanned_pdf(text: str) -> bytes:
    """A picture of text - what a scanner actually produces."""
    import pymupdf as fitz

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


def _image(fmt: str) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (320, 140), "white").save(buffer, fmt)

    return buffer.getvalue()


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type="X",
        normalized_data_type=data_type,
        is_primary_key=primary,
        nullable=not primary,
    )


def build_corpus() -> tuple[SourceEntity, list[SourceRecord], dict[str, bytes]]:
    """Six employees covering every outcome the BLOB path can produce."""
    shared_certificate = _text_pdf(
        ["BIRTH CERTIFICATE", "Registrar General, Colombo", "Serial: BC-4471"]
    )
    payloads = {
        "shared_certificate": shared_certificate,
        "contract": _text_pdf(
            ["EMPLOYMENT CONTRACT", "Position: Senior Accountant", "Grade: M3"]
        ),
        "scanned": _scanned_pdf("BIRTH CERTIFICATE"),
        "jpeg": _image("JPEG"),
        "png": _image("PNG"),
        "zip": b"PK\x03\x04" + b"\x00" * 512,
        "corrupt_pdf": b"%PDF-1.7\n" + b"\x00" * 128,
    }

    entity = SourceEntity(
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
            _field("archive", FieldDataType.BINARY),
        ),
    )

    rows = [
        # Two employees issued the SAME standard-form certificate. This is the
        # association-collision case.
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "full_name": "Nimal Silva",
                "department": "Finance",
                "birth_certificate": shared_certificate,
                "employment_contract": payloads["contract"],
                "profile_photo": payloads["jpeg"],
                "archive": None,
            }
        ),
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP003",
                "full_name": "Amal Perera",
                "department": "HR",
                "birth_certificate": shared_certificate,
                "employment_contract": None,
                "profile_photo": payloads["png"],
                "archive": None,
            }
        ),
        # A scanned certificate: no text layer, OCR path.
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP004",
                "full_name": "Kamala Fernando",
                "department": "Operations",
                "birth_certificate": payloads["scanned"],
                "employment_contract": None,
                "profile_photo": None,
                "archive": None,
            }
        ),
        # Unsupported and corrupt content beside good content.
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP005",
                "full_name": "Sunil Bandara",
                "department": "IT",
                "birth_certificate": payloads["corrupt_pdf"],
                "employment_contract": payloads["contract"],
                "profile_photo": None,
                "archive": payloads["zip"],
            }
        ),
        # Driver-native wrapper types.
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP006",
                "full_name": "Ranjith Silva",
                "department": "Finance",
                "birth_certificate": memoryview(shared_certificate),
                "employment_contract": bytearray(payloads["contract"]),
                "profile_photo": None,
                "archive": None,
            }
        ),
        # No attachments at all - the ordinary ERP row.
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP007",
                "full_name": "Dilani Jayasuriya",
                "department": "Legal",
                "birth_certificate": None,
                "employment_contract": None,
                "profile_photo": None,
                "archive": None,
            }
        ),
    ]

    return entity, rows, payloads


# ----------------------------------------------------------------------
# Measurement
# ----------------------------------------------------------------------


def audit_leakage(blob: str, payloads: dict[str, bytes]) -> list[str]:
    """Every way a raw byte could show up in text intended for a model."""
    findings: list[str] = []

    for name, payload in payloads.items():
        encoded = base64.b64encode(payload).decode()

        for width in (16, 24, 32):
            if encoded[:width] in blob:
                findings.append(f"base64 prefix of {name} ({width} chars)")
                break

    # The literal signatures the Phase 3 brief names.
    for marker, label in (
        ("JVBERi0x", "base64 PDF header"),
        ("/9j/4AAQ", "base64 JPEG header"),
        ("iVBORw0KGgo", "base64 PNG header"),
        ("%PDF-", "raw PDF header"),
        ("\\u00ff\\u00d8\\u00ff", "raw JPEG SOI"),
        ("PK\\u0003\\u0004", "raw ZIP header"),
    ):
        if marker in blob:
            findings.append(label)

    return findings


def main() -> int:
    entity, rows, payloads = build_corpus()
    binary_fields = binary_field_names_for_entity(entity)

    transformed = SourceNativeTransformer().transform_records(
        rows, entity, "legacy_hr", SourceType.POSTGRESQL
    )
    canonical = transformed.records
    result = extract_record_assets(rows, canonical, entity, binary_fields)

    ocr = probe_ocr()

    # ---- outcome distribution ----
    outcomes: dict[str, int] = {}
    for asset in result.assets:
        outcomes[asset.outcome] = outcomes.get(asset.outcome, 0) + 1

    # ---- association integrity ----
    vector_ids = [r.vector_id for r in result.representations]
    representation_ids = [r.representation_id for r in result.representations]
    collisions = len(vector_ids) - len(set(vector_ids))

    by_document: dict[str, set[str]] = {}
    for representation in result.representations:
        by_document.setdefault(
            representation.metadata["document_id"], set()
        ).add(representation.metadata["parent_record_id"])

    shared_documents = {
        document_id: sorted(parents)
        for document_id, parents in by_document.items()
        if len(parents) > 1
    }

    # ---- parent linkage ----
    known_parents = {record.record_id for record in canonical}
    orphans = [
        r.representation_id
        for r in result.representations
        if not r.source_record_ids or r.source_record_ids[0] not in known_parents
    ]

    # ---- leakage ----
    surface = json.dumps(
        {
            "representations": [r.to_dict() for r in result.representations],
            "canonical": [c.to_json_dict() for c in canonical],
            "assets": [a.to_dict() for a in result.assets],
            "warnings": list(result.warnings),
        },
        default=str,
    )
    leakage = audit_leakage(surface, payloads)

    # ---- entity typing ----
    document_reps = [
        r for r in result.representations if r.entity_type == DOCUMENT_ENTITY_TYPE
    ]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "rows": len(rows),
            "binary_columns_declared": list(binary_fields),
            "binary_values_present": result.fields_seen,
            "canonical_records": len(canonical),
        },
        "environment": {
            "ocr_available": ocr.available,
            "ocr_reason": None if ocr.available else ocr.reason,
        },
        "extraction": {
            "outcomes": outcomes,
            "indexed_documents": result.extracted,
            "skipped": result.skipped,
            "ocr_assets": result.ocr_assets,
            "representations_built": len(result.representations),
        },
        "association_integrity": {
            "vector_ids": len(vector_ids),
            "unique_vector_ids": len(set(vector_ids)),
            "unique_representation_ids": len(set(representation_ids)),
            "collisions": collisions,
            "documents_shared_across_records": {
                document_id[:12]: parents
                for document_id, parents in shared_documents.items()
            },
            "orphan_representations": orphans,
        },
        "binary_safety": {
            "leakage_findings": leakage,
            "leakage_count": len(leakage),
            "surface_bytes_audited": len(surface),
        },
        "typing": {
            "document_representations": len(document_reps),
            "all_document_typed": len(document_reps) == len(result.representations),
        },
        "warnings": list(result.warnings),
    }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # ---- console ----
    print("=" * 66)
    print("PHASE 3 MINI-EVALUATION - database BLOB -> document -> vector")
    print("=" * 66)
    print(f"rows                     {len(rows)}")
    print(f"binary columns declared  {len(binary_fields)}  {list(binary_fields)}")
    print(f"binary values present    {result.fields_seen}")
    print(f"OCR available            {ocr.available}")
    print()
    print("outcomes")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {outcome:<20} {count}")
    print()
    print(f"documents indexed        {result.extracted}")
    print(f"skipped                  {result.skipped}")
    print(f"OCR-read assets          {result.ocr_assets}")
    print(f"representations built    {len(result.representations)}")
    print()
    print("association integrity")
    print(f"  vector ids             {len(vector_ids)} "
          f"({len(set(vector_ids))} unique)")
    print(f"  COLLISIONS             {collisions}")
    print(f"  orphan representations {len(orphans)}")
    for document_id, parents in shared_documents.items():
        print(f"  shared doc {document_id[:12]} -> {len(parents)} records: {parents}")
    print()
    print("binary safety")
    print(f"  surface audited        {len(surface)} chars")
    print(f"  LEAKAGE FINDINGS       {len(leakage)} {leakage or ''}")
    print()
    print(f"artifact -> {ARTIFACT.relative_to(ROOT)}")
    print("=" * 66)

    gates_ok = collisions == 0 and not leakage and not orphans
    print(f"GATES: leakage={len(leakage)}  collisions={collisions}  "
          f"orphans={len(orphans)}  ->  {'PASS' if gates_ok else 'FAIL'}")

    return 0 if gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
