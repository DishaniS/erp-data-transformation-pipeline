# Retrieval Layer

This folder contains the BPI 2020 semantic retrieval stage.

Responsibilities:
- query Qdrant using vector similarity
- retrieve linked ERP case and document context
- support downstream RAG and semantic search workflows

The search script uses the same `VECTOR_DB_URL`, `VECTOR_DB_API_KEY`, and
`VECTOR_COLLECTION` settings as the uploader.

Each result carries `record_id`, the stable cross-layer key. That is the value
to use when resolving a hit back to `ai_ready_cases.case_record_id` or
`ai_ready_documents.document_record_id`. The `source_record_id` field is a
PostgreSQL SERIAL shown for traceability only and must not be used for lookup.

Non-interactive usage:

```powershell
.\.venv\Scripts\python.exe src\bpi2020\retrieval\search_erp_knowledge.py --demo
.\.venv\Scripts\python.exe src\bpi2020\retrieval\search_erp_knowledge.py --query "travel claim approval policy"
```
