"""
BPI 2020 ERP Document Parser

This script converts unstructured ERP-related documents into reusable AI-ready JSON.

Inputs:
    data/bpi2020/documents/   -> PDF documents
    data/bpi2020/images/      -> scanned images

Outputs:
    data/bpi2020/ai_ready_documents/bpi2020_document_records.json
    data/bpi2020/ai_ready_documents/bpi2020_document_records.jsonl

Database target:
    erp_ai_native_db.ai_ready_documents

Purpose:
    Converts PDFs and scanned images only once into structured JSON so the AI
    layer does not need to repeatedly reprocess raw files.
"""

import json
import hashlib
import sys
from pathlib import Path
from typing import Dict, List, Optional

import fitz  # PyMuPDF
import pandas as pd
from PIL import Image
import pytesseract
from sqlalchemy import text


# ============================================================
# Project paths
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bpi2020.common.config import PostgresSettings, get_tesseract_cmd
from bpi2020.common.health import check_postgres, check_tesseract
from bpi2020.common.stable_ids import compute_content_hash, make_document_record_id

DOCUMENT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "documents"
IMAGE_DIR = PROJECT_ROOT / "data" / "bpi2020" / "images"
OUTPUT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "ai_ready_documents"

DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Optional Windows Tesseract path
# ============================================================

TESSERACT_CMD = get_tesseract_cmd()

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


# ============================================================
# Database configuration
# ============================================================

PIPELINE_DB = PostgresSettings.pipeline()
AI_DB_NAME = PIPELINE_DB.database

ai_engine = PIPELINE_DB.create_engine()


# ============================================================
# Supported files
# ============================================================

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}


# ============================================================
# Helper functions
# ============================================================

def generate_document_id(file_path: Path) -> str:
    """
    Generate stable document ID using file name and file content.

    This is content-derived, so the same file always produces the same id and a
    modified file produces a new one. It is the basis of document_record_id and
    therefore of the document's Qdrant point ID.
    """
    hasher = hashlib.sha256()

    hasher.update(file_path.name.encode("utf-8"))

    with open(file_path, "rb") as f:
        hasher.update(f.read())

    return hasher.hexdigest()[:24]


def clean_extracted_text(text: str) -> str:
    """
    Clean extracted document/OCR text.
    """
    if not text:
        return ""

    lines = text.splitlines()
    cleaned_lines = []

    for line in lines:
        line = line.strip()

        if line:
            cleaned_lines.append(" ".join(line.split()))

    return "\n".join(cleaned_lines)


def infer_document_type(file_path: Path, source_type: str) -> str:
    """
    Infer document type using filename keywords.
    """
    name = file_path.name.lower()

    if "invoice" in name or "receipt" in name or "bill" in name:
        return "invoice_or_receipt"

    if "policy" in name or "procedure" in name or "guideline" in name:
        return "policy_document"

    if "approval" in name or "form" in name:
        return "approval_form"

    if "claim" in name or "travel" in name:
        return "travel_claim_document"

    if source_type == "pdf":
        return "pdf_document"

    if source_type == "image":
        return "scanned_image_document"

    return "erp_document"


def build_text_for_ai(
    document_name: str,
    document_type: str,
    source_type: str,
    extracted_text: str,
) -> str:
    """
    Build reusable text representation for embeddings/RAG.
    """
    return (
        f"ERP document '{document_name}' is categorized as {document_type}. "
        f"The source format is {source_type}. "
        f"Extracted content: {extracted_text}"
    )


def extract_text_from_pdf(file_path: Path) -> Dict:
    """
    Extract text from a PDF using PyMuPDF.
    """
    pages = []
    full_text_parts = []

    with fitz.open(file_path) as pdf:
        for page_index, page in enumerate(pdf, start=1):
            page_text = page.get_text("text")
            page_text = clean_extracted_text(page_text)

            pages.append({
                "page_number": page_index,
                "text": page_text,
            })

            if page_text:
                full_text_parts.append(page_text)

    extracted_text = clean_extracted_text("\n".join(full_text_parts))

    return {
        "page_count": len(pages),
        "pages": pages,
        "extracted_text": extracted_text,
    }


def extract_text_from_image(file_path: Path) -> Dict:
    """
    Extract text from image using OCR.
    """
    image = Image.open(file_path)

    extracted_text = pytesseract.image_to_string(image)
    extracted_text = clean_extracted_text(extracted_text)

    return {
        "image_width": image.width,
        "image_height": image.height,
        "ocr_engine": "tesseract",
        "extracted_text": extracted_text,
    }


def build_document_record(file_path: Path, source_type: str) -> Dict:
    """
    Build one AI-ready document record from a PDF/image file.
    """
    document_id = generate_document_id(file_path)
    document_name = file_path.name
    document_type = infer_document_type(file_path, source_type)

    if source_type == "pdf":
        extraction_result = extract_text_from_pdf(file_path)
    elif source_type == "image":
        extraction_result = extract_text_from_image(file_path)
    else:
        raise ValueError(f"Unsupported source type: {source_type}")

    extracted_text = extraction_result.get("extracted_text", "")
    text_for_ai = build_text_for_ai(
        document_name=document_name,
        document_type=document_type,
        source_type=source_type,
        extracted_text=extracted_text,
    )

    document_record_id = make_document_record_id(document_id)
    source_file_path = str(file_path.relative_to(PROJECT_ROOT))

    document_json = {
        "document_record_id": document_record_id,
        "document_id": document_id,
        "record_type": "erp_unstructured_document",
        "document_name": document_name,
        "document_type": document_type,
        "source_type": source_type,
        "source_file_path": source_file_path,
        "record_source": "bpi2020_erp_document_layer",
        "extraction_method": "pymupdf" if source_type == "pdf" else "tesseract_ocr",
        "extraction_result": extraction_result,
        "text_for_ai": text_for_ai,
    }

    content_hash = compute_content_hash(
        record_id=document_record_id,
        text_for_ai=text_for_ai,
        metadata={
            "document_id": document_id,
            "document_name": document_name,
            "document_type": document_type,
            "source_type": source_type,
            "source_file_path": source_file_path,
            "text_length": len(extracted_text),
        },
    )

    return {
        "document_record_id": document_record_id,
        "content_hash": content_hash,
        "document_id": document_id,
        "document_type": document_type,
        "document_name": document_name,
        "source_file_path": source_file_path,
        "extracted_text": extracted_text,
        "text_for_ai": text_for_ai,
        "document_json": document_json,
    }


def save_document_outputs(records: List[Dict]) -> None:
    """
    Save document records as JSON and JSONL.
    """
    json_path = OUTPUT_DIR / "bpi2020_document_records.json"
    jsonl_path = OUTPUT_DIR / "bpi2020_document_records.jsonl"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False, default=str)

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    print(f"\nSaved JSON : {json_path}")
    print(f"Saved JSONL: {jsonl_path}")


def load_existing_document_hashes() -> Dict[str, Optional[str]]:
    """Read the current document_record_id -> content_hash map."""
    query = text("SELECT document_record_id, content_hash FROM ai_ready_documents")

    with ai_engine.connect() as connection:
        return {
            row[0]: row[1]
            for row in connection.execute(query)
            if row[0] is not None
        }


def upsert_document_records(records: List[Dict]) -> Dict[str, int]:
    """
    UPSERT AI-ready document records into PostgreSQL.

    Before Phase 0 the caller ran "DELETE FROM ai_ready_documents" immediately
    before this statement, which made the ON CONFLICT clause unreachable: every
    logical document was re-inserted and received a new SERIAL id on every run.
    Files written by an earlier run then referenced ids that no longer existed.

    The DELETE is gone. Identity now comes from the content-derived
    document_record_id, and embedding_status is only invalidated when
    content_hash actually changed.
    """
    upsert_sql = text("""
        INSERT INTO ai_ready_documents (
            document_record_id,
            content_hash,
            document_id,
            document_type,
            document_name,
            source_file_path,
            extracted_text,
            text_for_ai,
            document_json,
            embedding_status,
            updated_at
        )
        VALUES (
            :document_record_id,
            :content_hash,
            :document_id,
            :document_type,
            :document_name,
            :source_file_path,
            :extracted_text,
            :text_for_ai,
            CAST(:document_json AS JSONB),
            'pending',
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (document_id)
        DO UPDATE SET
            document_record_id = EXCLUDED.document_record_id,
            content_hash = EXCLUDED.content_hash,
            document_type = EXCLUDED.document_type,
            document_name = EXCLUDED.document_name,
            source_file_path = EXCLUDED.source_file_path,
            extracted_text = EXCLUDED.extracted_text,
            text_for_ai = EXCLUDED.text_for_ai,
            document_json = EXCLUDED.document_json,
            updated_at = CURRENT_TIMESTAMP,
            embedding_status = CASE
                WHEN ai_ready_documents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN 'pending'
                ELSE ai_ready_documents.embedding_status
            END
    """)

    existing_hashes = load_existing_document_hashes()

    stats = {"written": 0, "new": 0, "content_changed": 0, "content_unchanged": 0}

    with ai_engine.begin() as connection:
        for record in records:
            document_record_id = record["document_record_id"]

            if document_record_id not in existing_hashes:
                stats["new"] += 1
            elif existing_hashes[document_record_id] != record["content_hash"]:
                stats["content_changed"] += 1
            else:
                stats["content_unchanged"] += 1

            connection.execute(
                upsert_sql,
                {
                    "document_record_id": document_record_id,
                    "content_hash": record["content_hash"],
                    "document_id": record["document_id"],
                    "document_type": record["document_type"],
                    "document_name": record["document_name"],
                    "source_file_path": record["source_file_path"],
                    "extracted_text": record["extracted_text"],
                    "text_for_ai": record["text_for_ai"],
                    "document_json": json.dumps(record["document_json"], ensure_ascii=False, default=str),
                },
            )

            stats["written"] += 1

    return stats


def log_transformation(records_count: int, status: str, message: str):
    """
    Log document transformation stage.
    """
    log_sql = text("""
        INSERT INTO transformation_logs (
            pipeline_stage,
            source_database,
            target_database,
            source_table,
            total_input_records,
            total_output_records,
            status,
            message
        )
        VALUES (
            :pipeline_stage,
            :source_database,
            :target_database,
            :source_table,
            :total_input_records,
            :total_output_records,
            :status,
            :message
        )
    """)

    with ai_engine.begin() as connection:
        connection.execute(
            log_sql,
            {
                "pipeline_stage": "parse_bpi_documents",
                "source_database": "file_system",
                "target_database": AI_DB_NAME,
                "source_table": "data/bpi2020/documents_and_images",
                "total_input_records": records_count,
                "total_output_records": records_count,
                "status": status,
                "message": message,
            },
        )


def collect_files() -> List[Dict]:
    """
    Collect supported PDF and image files.
    """
    files = []

    for file_path in DOCUMENT_DIR.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in PDF_EXTENSIONS:
            files.append({
                "file_path": file_path,
                "source_type": "pdf",
            })

    for file_path in IMAGE_DIR.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in IMAGE_EXTENSIONS:
            files.append({
                "file_path": file_path,
                "source_type": "image",
            })

    return files


# ============================================================
# Main
# ============================================================

def main():
    print("\nStarting BPI document/image parsing pipeline...")
    print(f"PDF folder   : {DOCUMENT_DIR}")
    print(f"Image folder : {IMAGE_DIR}")
    print(f"Output folder: {OUTPUT_DIR}")
    print(f"Target table : ai_ready_documents")
    print(f"Database     : {PIPELINE_DB.safe_target}")

    print(f"\n{check_postgres(PIPELINE_DB, required_tables=('ai_ready_documents',))}")

    files = collect_files()

    # OCR is only required when there is at least one image to process.
    if any(item["source_type"] == "image" for item in files):
        print(f"{check_tesseract()}")

    if not files:
        print("\nNo PDF or image files found.")
        print("Add PDFs to data/bpi2020/documents/")
        print("Add images to data/bpi2020/images/")

        log_transformation(
            records_count=0,
            status="failed",
            message="No PDF or image files found for document parsing.",
        )
        return

    print(f"\nDetected files: {len(files)}")

    records = []

    for index, file_info in enumerate(files, start=1):
        file_path = file_info["file_path"]
        source_type = file_info["source_type"]

        print(f"\n[{index}/{len(files)}] Processing {source_type}: {file_path.name}")

        try:
            record = build_document_record(file_path, source_type)
            records.append(record)

            extracted_len = len(record["extracted_text"])
            print(f"   Extracted text length: {extracted_len} characters")

        except Exception as e:
            print(f"   ERROR processing {file_path.name}: {e}")

    if not records:
        log_transformation(
            records_count=0,
            status="failed",
            message="Files were detected, but no document records were successfully parsed.",
        )
        print("\nNo records created.")
        return

    save_document_outputs(records)

    # Reproducible reruns come from stable identity plus UPSERT, not from
    # deleting the table first.
    stats = upsert_document_records(records)

    print("\nUpsert summary")
    print("-" * 50)
    print(f"  Documents written : {stats['written']}")
    print(f"  New documents     : {stats['new']}")
    print(f"  Content changed   : {stats['content_changed']}")
    print(f"  Content unchanged : {stats['content_unchanged']}")

    log_transformation(
        records_count=stats["written"],
        status="success",
        message=(
            f"Upserted {stats['written']} AI-ready document records "
            f"(new={stats['new']}, changed={stats['content_changed']}, "
            f"unchanged={stats['content_unchanged']})."
        ),
    )

    print("\nBPI document/image parsing pipeline completed.")


if __name__ == "__main__":
    main()