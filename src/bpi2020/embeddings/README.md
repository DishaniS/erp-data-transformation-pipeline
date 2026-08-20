# Embeddings Layer

This folder contains the BPI 2020 embedding and Qdrant upload stage.

Responsibilities:
- read unified records from `data/bpi2020/unified/`
- generate embeddings with `sentence-transformers`
- stream deterministic point upserts to local Qdrant or Qdrant Cloud
- update embedding status fields in `erp_ai_native_db`

Qdrant Cloud requires `VECTOR_DB_URL` and `VECTOR_DB_API_KEY` in the project
`.env` (the older `QDRANT_URL` / `QDRANT_API_KEY` names still work as
deprecated fallbacks). See the root `README.md` for validation and full-upload
commands.

## Identity rules

- The Qdrant point ID is `uuid5(NAMESPACE_URL, "bpi2020/{record_id}")` where
  `record_id` is the unified record's stable business key. It is never derived
  from a PostgreSQL SERIAL, so the same logical record always maps to the same
  point and a rerun updates it in place.
- PostgreSQL status updates match on `case_record_id` / `document_record_id`.
- Every `UPDATE` checks the affected row count. Zero matched rows raises
  `EMBEDDING_SOURCE_RECORD_NOT_FOUND`; more than one raises
  `EMBEDDING_SOURCE_RECORD_AMBIGUOUS`. The run cannot report success while any
  linkage update failed.
- A unified record still using a pre-Phase-0 `case_<serial>` identifier is
  rejected with `EMBEDDING_STALE_SERIAL_RECORD_ID` rather than embedded.
