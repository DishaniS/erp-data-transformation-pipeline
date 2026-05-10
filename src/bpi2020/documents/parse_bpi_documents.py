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

import os
import json
import hashlib
from pathlib import Path
from typing import Dict, List, Optional

import fitz  # PyMuPDF
import pandas as pd
from PIL import Image
import pytesseract
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


# ============================================================
# Project paths
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]

DOCUMENT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "documents"
IMAGE_DIR = PROJECT_ROOT / "data" / "bpi2020" / "images"
OUTPUT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "ai_ready_documents"

DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")


# ============================================================
# Optional Windows Tesseract path
# ============================================================

TESSERACT_PATH = os.getenv("TESSERACT_PATH")

if TESSERACT_PATH:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH


# ============================================================
# Database configuration
# ============================================================

AI_DB_HOST = os.getenv("AI_DB_HOST", "localhost")
AI_DB_PORT = os.getenv("AI_DB_PORT", "5432")
AI_DB_NAME = os.getenv("AI_DB_NAME", "erp_ai_native_db")
AI_DB_USER = os.getenv("AI_DB_USER", "postgres")
AI_DB_PASSWORD = os.getenv("AI_DB_PASSWORD", "postgres")

AI_DATABASE_URL = (
    f"postgresql+psycopg2://{AI_DB_USER}:{AI_DB_PASSWORD}"
    f"@{AI_DB_HOST}:{AI_DB_PORT}/{AI_DB_NAME}"
)

ai_engine = create_engine(AI_DATABASE_URL)


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

    document_json = {
        "document_id": document_id,
        "record_type": "erp_unstructured_document",
        "document_name": document_name,
        "document_type": document_type,
        "source_type": source_type,
        "source_file_path": str(file_path.relative_to(PROJECT_ROOT)),
        "record_source": "bpi2020_erp_document_layer",
        "extraction_method": "pymupdf" if source_type == "pdf" else "tesseract_ocr",
        "extraction_result": extraction_result,
        "text_for_ai": text_for_ai,
    }

    return {
        "document_id": document_id,
        "document_type": document_type,
        "document_name": document_name,
        "source_file_path": str(file_path.relative_to(PROJECT_ROOT)),
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


def clear_existing_document_records():
    """
    Clear existing document records before rerun.
    This keeps the prototype output clean and reproducible.
    """
    with ai_engine.begin() as connection:
        connection.execute(text("DELETE FROM ai_ready_documents;"))


def insert_document_records(records: List[Dict]) -> int:
    """
    Insert AI-ready document records into PostgreSQL.
    """
    insert_sql = text("""
        INSERT INTO ai_ready_documents (
            document_id,
            document_type,
            document_name,
            source_file_path,
            extracted_text,
            text_for_ai,
            document_json,
            embedding_status
        )
        VALUES (
            :document_id,
            :document_type,
            :document_name,
            :source_file_path,
            :extracted_text,
            :text_for_ai,
            CAST(:document_json AS JSONB),
            'pending'
        )
        ON CONFLICT (document_id)
        DO UPDATE SET
            document_type = EXCLUDED.document_type,
            document_name = EXCLUDED.document_name,
            source_file_path = EXCLUDED.source_file_path,
            extracted_text = EXCLUDED.extracted_text,
            text_for_ai = EXCLUDED.text_for_ai,
            document_json = EXCLUDED.document_json,
            embedding_status = 'pending'
    """)

    inserted_count = 0

    with ai_engine.begin() as connection:
        for record in records:
            connection.execute(
                insert_sql,
                {
                    "document_id": record["document_id"],
                    "document_type": record["document_type"],
                    "document_name": record["document_name"],
                    "source_file_path": record["source_file_path"],
                    "extracted_text": record["extracted_text"],
                    "text_for_ai": record["text_for_ai"],
                    "document_json": json.dumps(record["document_json"], ensure_ascii=False, default=str),
                },
            )

            inserted_count += 1

    return inserted_count


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

    files = collect_files()

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

    # For reproducible prototype runs.
    clear_existing_document_records()

    inserted_count = insert_document_records(records)

    log_transformation(
        records_count=inserted_count,
        status="success",
        message=f"Parsed and inserted {inserted_count} AI-ready document records.",
    )

    print(f"\nInserted records into ai_ready_documents: {inserted_count}")
    print("\nBPI document/image parsing pipeline completed.")


if __name__ == "__main__":
    main()