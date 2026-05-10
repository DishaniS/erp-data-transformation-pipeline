# ERP-Aware Data Transformation Pipeline

This repository contains the current implementation of the ERP-aware data transformation pipeline for project `R26-SE-034`.

The active research direction is based on the `BPI Challenge 2020` finance-sector ERP event logs. The older AdventureWorks prototype is preserved under `_archive_adventureworks_legacy/` and is no longer the active pipeline.

## Current Architecture

`BPI 2020 CSV files`
-> `PostgreSQL simulated legacy ERP database: bpi2020_old_erp_db`
-> `AI-native PostgreSQL database: erp_ai_native_db`
-> `cleaned_event_logs`
-> `ai_ready_cases`
-> `ai_ready_documents`
-> `unified BPI AI-ready knowledge base`
-> `embeddings`
-> `Qdrant`

## Active Databases And Tables

### Legacy-source database
- `bpi2020_old_erp_db`

### AI-native database
- `erp_ai_native_db`
- `cleaned_event_logs`
- `ai_ready_cases`
- `ai_ready_documents`
- `transformation_logs`
- `sync_state`

## Active Repository Layout

```text
erp-data-transformation-pipeline/
|-- README.md
|-- requirements.txt
|-- .env.example
|-- .gitignore
|-- data/
|   |-- bpi2020/
|   |   |-- raw/
|   |   |-- documents/
|   |   |-- images/
|   |   |-- cleaned/
|   |   |-- ai_ready/
|   |   |-- ai_ready_documents/
|   |   `-- unified/
|   `-- raw/
|-- src/
|   `-- bpi2020/
|       |-- storage/
|       |-- transformation/
|       |-- documents/
|       |-- sync/
|       |-- embeddings/
|       `-- retrieval/
`-- _archive_adventureworks_legacy/
```

`data/raw/` is retained as an empty compatibility folder after the BPI raw CSV files were moved into `data/bpi2020/raw/`.

## Active Pipeline Scripts

### Storage
- `src/bpi2020/storage/import_bpi_csv_to_old_db.py`
  Imports BPI 2020 CSV files into `bpi2020_old_erp_db`.
- `src/bpi2020/storage/create_ai_native_db_schema.py`
  Creates the AI-native schema in `erp_ai_native_db`.

### Transformation
- `src/bpi2020/transformation/clean_and_load_to_ai_db.py`
  Cleans legacy ERP records and loads them into `cleaned_event_logs`.
- `src/bpi2020/transformation/build_ai_ready_cases.py`
  Groups cleaned event logs into case-level AI-ready records.
- `src/bpi2020/transformation/build_unified_bpi_knowledge_base.py`
  Merges structured case records and parsed document records into one unified AI-ready layer.

### Documents
- `src/bpi2020/documents/parse_bpi_documents.py`
  Parses BPI-related PDFs and images into `ai_ready_documents`.

### Sync
- `src/bpi2020/sync/realtime_incremental_sync.py`
  Simulates polling-based incremental synchronization from the legacy ERP layer into the AI-native layer.

## Data Folders

- `data/bpi2020/raw/`
  BPI Challenge 2020 CSV source files used for initial ERP ingestion.
- `data/bpi2020/cleaned/`
  Cleaned JSON and JSONL outputs generated from the ERP event tables.
- `data/bpi2020/ai_ready/`
  Case-level AI-ready ERP outputs.
- `data/bpi2020/documents/`
  PDF files used for document parsing.
- `data/bpi2020/images/`
  Scanned images used for OCR-based document parsing.
- `data/bpi2020/ai_ready_documents/`
  Parsed JSON and JSONL document outputs.
- `data/bpi2020/unified/`
  Unified BPI AI-ready knowledge-base outputs.

## Recommended Run Order

1. Create and populate `bpi2020_old_erp_db` from `data/bpi2020/raw/`.
2. Create the schema in `erp_ai_native_db`.
3. Run the clean-and-load pipeline into `cleaned_event_logs`.
4. Build `ai_ready_cases`.
5. Parse documents and images into `ai_ready_documents`.
6. Build the unified BPI AI-ready knowledge base.
7. Run incremental sync when you need near-real-time updates.

## Setup

1. Create `.env` from `.env.example`.
2. Install dependencies from `requirements.txt`.
3. Ensure PostgreSQL is running and both databases are available.
4. If OCR is needed on Windows, set `TESSERACT_PATH` in `.env`.

## Notes

- The top-level AdventureWorks prototype code and outputs were moved to `_archive_adventureworks_legacy/`.
- `src/bpi2020/embeddings/` and `src/bpi2020/retrieval/` are reserved for the next stage of embedding generation and retrieval integration.
