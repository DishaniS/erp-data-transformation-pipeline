# ERP Data Transformation API — Endpoint Handoff

**Base URL:** `https://erp-data-transformation-api-ju0h8k.azurewebsites.net`
**Swagger:** `https://erp-data-transformation-api-ju0h8k.azurewebsites.net/docs`
**OpenAPI:** `https://erp-data-transformation-api-ju0h8k.azurewebsites.net/openapi.json`
**Auth:** `X-API-Key` header — value supplied separately, **never in this document**

Every endpoint below was confirmed against the **live deployed OpenAPI document**
on 2026-08-29. **24 operations**, OpenAPI 3.1.0, service version 1.0.

---

## Authentication

| | |
|---|---|
| Header | `X-API-Key: <YOUR_VALUE>` |
| Comparison | Constant-time (`hmac.compare_digest`) |
| Always required | All `POST`, `PUT`, `PATCH`, `DELETE` |
| Also required | All `GET`, because the deployment sets `ERP_API_PROTECT_READS=true` |
| Always public | `/v1/health/live`, `/v1/health/ready`, `/docs`, `/redoc`, `/openapi.json` |

Verified live: no key → **401** · wrong key → **401** · valid key → **200**.

In Swagger, click **Authorize**, paste the key once, and every protected
operation sends the header automatically.

> **The key is a server-to-server secret.** It must never be embedded in browser
> JavaScript, a Vite/React build, or any static frontend configuration.

---

## Complete endpoint inventory

### health — 2 operations · public

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| GET | `/v1/health/live` | Liveness probe | Platform / monitoring | **public** |
| GET | `/v1/health/ready` | Readiness + dependency report | Platform / operators | **public** |

`/v1/health/ready` returns per-dependency status for `postgresql`, `job_store`,
`embedding_model`, `cold_archive` and `vector_storage` — useful for integration
debugging without a key.

```bash
curl https://erp-data-transformation-api-ju0h8k.azurewebsites.net/v1/health/ready
```

### capabilities — 1 operation

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| GET | `/v1/capabilities` | Discover what this deployment supports and has enabled | ERPBridge, Frontend, Governance | required |

Returns `source_types`, `file_types`, `job_types`, `content_kinds`,
`storage_tiers`, `embedding_model`, `embedding_dimension`, `limitations`, and an
`integration_capabilities` block reporting **`supported`** and **`enabled`**
separately per capability.

```bash
curl -H "X-API-Key: <YOUR_VALUE>" https://erp-data-transformation-api-ju0h8k.azurewebsites.net/v1/capabilities
```

### files — 2 operations

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| POST | `/v1/files/csv` | Upload CSV → infer + catalog schema, **auto-index the schema** | Frontend | required |
| POST | `/v1/files/documents` | Upload PDF/image → extract + OCR, **auto-index the document** | Frontend | required |

**`multipart/form-data`.** These two behave differently — see the CSV warning
below.

```bash
curl -X POST https://erp-data-transformation-api-ju0h8k.azurewebsites.net/v1/files/csv \
  -H "X-API-Key: <YOUR_VALUE>" -F "file=@employees.csv;type=text/csv"
```

Response: `upload_id`, `filename`, `content_hash`, `size_bytes`, `schema_id`,
`columns`, `rows_sampled`, `sample_limited`, `rows_observed`, `published`,
`schema_index_job_id`, `schema_indexing_status`, `warnings`.

```bash
curl -X POST https://erp-data-transformation-api-ju0h8k.azurewebsites.net/v1/files/documents \
  -H "X-API-Key: <YOUR_VALUE>" -F "file=@certificate.pdf;type=application/pdf" -F "source_system_id=legacy_hr" -F "source_entity=employees" -F "business_key_name=employee_id" -F "business_key_value=EMP002" -F "document_type=birth_certificate" -F "sensitivity=restricted"
```

Response: `upload_id`, `document_id`, `page_count`, `ocr_used`, `index_job_id`,
`indexing_status`, `warnings`.

**Identity form fields** (all optional, all *declared* — never inferred):
`source_system_id`, `source_entity`, `parent_record_id`, `business_key_name`,
`business_key_value`, `document_type`, `sensitivity`.

**422 rules:** `business_key_name` and `business_key_value` are one declaration
in two fields — sending one without the other is refused. `sensitivity` must be
`public`, `internal`, `confidential` or `restricted`.

### sources — 5 operations

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| GET | `/v1/sources` | List registered sources | Frontend | required |
| POST | `/v1/sources` | Register a source (201) | Frontend | required |
| GET | `/v1/sources/{source_id}` | Read one source | Frontend | required |
| POST | `/v1/sources/{source_id}/discover` | Discover schema from a live source | Frontend | required |
| POST | `/v1/sources/{source_id}/test` | Test connectivity | Frontend | required |

```bash
curl -X POST https://erp-data-transformation-api-ju0h8k.azurewebsites.net/v1/sources \
  -H "X-API-Key: <YOUR_VALUE>" -H "Content-Type: application/json" \
  -d '{"name":"legacy_hr_export","source_type":"csv"}'
```

A supplied password is moved into the secret provider and dropped — it never
reaches storage or any response.

### schemas — 1 operation

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| GET | `/v1/schemas/{schema_id}` | Read an inferred/discovered schema | Frontend, ERPBridge, Governance | required |

### mappings — 3 operations

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| POST | `/v1/mappings/suggest` | Suggest source→canonical field mappings | Frontend | required |
| PUT | `/v1/mappings/{mapping_id}` | Update / approve a mapping | Frontend | required |
| POST | `/v1/mappings/{mapping_id}/validate` | Validate a mapping | Frontend | required |

Three outcomes: **automatic** (confident, unambiguous), **review** (ambiguous),
**refusal** (no candidate clears the floor). A refusal is a result, not a bug.

### jobs — 4 operations

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| GET | `/v1/jobs` | List jobs | Frontend | required |
| POST | `/v1/jobs` | Submit a pipeline job → **202 Accepted** | Frontend | required |
| GET | `/v1/jobs/{job_id}` | Poll job status | Frontend | required |
| POST | `/v1/jobs/{job_id}/retry` | Re-run a **pipeline** job | Frontend | required |

**`POST /v1/jobs` returns 202, not 201.** The job is accepted, not finished.

```bash
curl -X POST https://erp-data-transformation-api-ju0h8k.azurewebsites.net/v1/jobs \
  -H "X-API-Key: <YOUR_VALUE>" -H "Content-Type: application/json" \
  -d '{"job_type":"source_native_pipeline","source_id":"src_example","schema_id":"file_source.employees.abc123","upload_id":"upl_example","options":{"key_fields":["employee_id"]}}'
```

**JobTypes (7):** `structured_pipeline`, `document_pipeline`, `incremental_sync`,
`drift_check`, `api_spec_preparation`, `source_native_pipeline`,
`schema_pipeline`.

**Statuses (6):** `pending`, `running`, `succeeded`, `failed`, `partial`,
`interrupted`. A job whose records partially failed is `partial` — never
`succeeded`. Always read `counters` and `warnings`.

`POST /v1/jobs/{job_id}/retry` re-runs a **pipeline** job. It has nothing to do
with ERP calls.

### search — 1 operation

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| POST | `/v1/search` | Semantic search with exact metadata filters | Frontend, ERPBridge | required |

```bash
curl -X POST https://erp-data-transformation-api-ju0h8k.azurewebsites.net/v1/search \
  -H "X-API-Key: <YOUR_VALUE>" -H "Content-Type: application/json" \
  -d '{"query":"birth certificate details","filters":{"content_kind":"document_chunk","business_key_name":"employee_id","business_key_value":"EMP002","document_type":"birth_certificate"}}'
```

Response: `query_model`, `dimension`, `hits[]`, `tiers_searched`, `include_cold`,
`filters_applied`, `deep_search_used`.

Each hit: `representation_id`, `score`, `tier`, `canonical_record_id`,
`record_id`, `entity_type`, and a `metadata` block including `sensitivity`,
`page_start`, `page_end`, `chunk_index`, `document_id`.

**Search does not return text.** Resolve it separately.

**13 filterable fields:** `business_key_name`, `business_key_value`,
`content_kind`, `document_id`, `document_type`, `entity_kind`, `entity_type`,
`parent_record_id`, `schema_name`, `sensitivity`, `source_entity`,
`source_field`, `source_system_id`.

Filters are **exact-match equality combined with AND**. No ranges, no OR, no
negation. An unknown filter is rejected with 422, not ignored.

### representations — 1 operation

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| GET | `/v1/representations/{representation_id}` | Resolve a search hit to its authoritative AI-ready text | Frontend, ERPBridge | required |

```bash
curl -H "X-API-Key: <YOUR_VALUE>" \
  "https://erp-data-transformation-api-ju0h8k.azurewebsites.net/v1/representations/ai:document:employee_id_emp002_birth_certificate_abc.c00000.def"
```

Returns `text`, `content_hash`, `content_kind`, `sensitivity`, identity fields
and document provenance (`page_start`, `page_end`, `chunk_index`,
`document_id`). Text encrypted at rest is decrypted transparently.

### records — 1 operation

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| GET | `/v1/records/{record_id}` | Read one canonical record | Frontend | required |

### responses — 1 operation

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| POST | `/v1/responses/adapt` | Convert a raw ERP response into AI-ready content | **ERPBridge** | required |

```bash
curl -X POST https://erp-data-transformation-api-ju0h8k.azurewebsites.net/v1/responses/adapt \
  -H "X-API-Key: <YOUR_VALUE>" -H "Content-Type: application/json" \
  -d '{"query":"What is the employment status?","source_system_id":"legacy_hr","endpoint":"/api/hr/employees/EMP002","http_status":200,"content_type":"application/json","body":{"employee_id":"EMP002","employment_status":"ACTIVE"}}'
```

Response: `response_type`, `entity_type`, `llm_ready`, `assets[]`, `provenance`,
`transformation`, `warnings[]`, `success`, `partial`.

Use `body_base64` plus a matching `content_type` for PDF or image responses.
**For binary bodies `llm_ready` is `{}` and `partial` is `true` — the text is in
`assets[0].text`.**

**Collections adapt the first record only**, with a warning naming the total.

### api-specs — 2 operations

| Method | Endpoint | Purpose | Caller | Auth |
|---|---|---|---|---|
| POST | `/v1/api-specs/openapi` | Parse an OpenAPI document (design-time) | Tooling | required |
| POST | `/v1/api-specs/postman` | Parse a Postman collection (design-time) | Tooling | required |

Parsing only. This service **never calls** the documented endpoints.

---

## There is no sync endpoint

Synchronisation is a **JobType** (`incremental_sync`, `drift_check`) submitted
through `POST /v1/jobs`, and a scheduler that ships disabled. There is no
`/v1/sync` route. Any integration plan assuming one is mistaken.

---

## Error contract

```json
{ "success": false,
  "error": { "code": "INVALID_PIPELINE_REQUEST",
             "message": "...", "request_id": "..." } }
```

Stable JSON. No Python tracebacks, no dataclass `repr`, no enum `repr`, no
internal module paths.

| Status | Meaning |
|---|---|
| `202` | Job accepted — poll it |
| `401` | Missing or wrong `X-API-Key` |
| `404` | Unknown job, schema, record or representation |
| `413` | Upload exceeds the configured cap |
| `415` | Unsupported binary type — refused, never guessed |
| `422` | Invalid request — half a business key, unknown sensitivity, unknown filter, source-native job with no registered source |

---

## Retry behaviour

- This service **never retries an ERP business request.** A 404 or 503 body is
  adapted as what it is; re-issuing is ERPBridge's decision.
- `POST /v1/jobs/{job_id}/retry` re-runs a **pipeline** job only.
- `/v1/responses/adapt` is stateless — safe to re-send.
