# Phase 11 — Four-Member Group Integration Contract

**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and
Retrieval Pipeline for Legacy ERP Systems (Member 4)
**Contract version / date:** 2026-08-25
**OpenAPI:** 3.1.0 · service version `1.0` · **24 operations**
**Snapshot:** `artifacts/phase13_openapi.json`

Every member should integrate against the operation set recorded in that
snapshot. If it disagrees with this document, the snapshot wins — it is
generated from the running application, and this file is written by hand.

---

## 1. The four-member architecture

```
                    ┌────────────────────┐
                    │      MEMBER 3      │
                    │      Frontend      │
                    └─────────┬──────────┘
                              │  user request / files
             ┌────────────────┴────────────────┐
             │                                 │
             ▼                                 ▼
     ┌──────────────┐                  ┌──────────────┐
     │   MEMBER 1   │                  │   MEMBER 4   │
     │ Policy Gate  │                  │ Data/Vector  │
     └──────┬───────┘                  │  Pipeline    │
            │ governance               └──────────────┘
            ▼                            (indexed knowledge:
     ┌──────────────┐                     upload, search, resolve)
     │   MEMBER 2   │
     │ ERPBridge/MCP│
     └──────┬───────┘
            │ live API
            ▼
      ┌─────────────┐
      │ Legacy ERP  │
      └──────┬──────┘
             │ raw ERP response
             ▼
        MEMBER 2 ──── POST /v1/responses/adapt ───▶ MEMBER 4
                                                      │
                                            AI-ready response
                                                      ▼
                                              Member 2 / Member 3
```

Two distinct paths, and the distinction is the point of the design:

| Question | Answered by | Path |
|---|---|---|
| *What is EMP002's employment status **right now**?* | Member 2 | live ERP execution, then Member 4 adapts the result |
| *What do EMP002's **documents** say?* | Member 4 | indexed retrieval — search, then resolve |

A transactional fact must never come from the vector index. The index is a
snapshot whose freshness is bounded by a poll interval (Phase 9), and answering
"is this invoice paid?" from it would be answering from a stale copy.

## 2. Responsibility matrix

| | Member 1 | Member 2 | Member 3 | Member 4 |
|---|---|---|---|---|
| Policy / governance decision | **owns** | consumes | consumes | never |
| ERP credentials | no | **owns** | no | **never** |
| ERP business API execution | no | **owns** | no | **never** |
| MCP tool selection | no | **owns** | no | **never** |
| User authorization | **owns** | enforces | presents | **never** |
| User interface | no | no | **owns** | no |
| Data discovery / preparation | no | no | no | **owns** |
| Multimodal extraction, OCR | no | no | no | **owns** |
| Embedding, Qdrant, retrieval | no | no | no | **owns** |
| Sensitivity **metadata** | consumes | consumes | consumes | **owns** |
| Response adaptation | no | calls | consumes | **owns** |
| Final LLM answer generation | no | — | — | **never** |

## 3. Member 3 → Member 4 endpoints

| Purpose | Operation |
|---|---|
| Discover what is available | `GET /v1/capabilities` |
| Upload a document | `POST /v1/files/documents` |
| Upload a CSV | `POST /v1/files/csv` |
| Read an inferred schema | `GET /v1/schemas/{schema_id}` |
| Suggest / update a mapping | `POST /v1/mappings/suggest`, `PUT /v1/mappings/{mapping_id}` |
| Register a source | `POST /v1/sources` |
| Submit a pipeline job | `POST /v1/jobs` → **202 Accepted** |
| Poll a job | `GET /v1/jobs/{job_id}` |
| Search | `POST /v1/search` |
| Resolve a hit to its text | `GET /v1/representations/{representation_id}` |

## 4. Member 2 → Member 4 endpoints

| Purpose | Operation |
|---|---|
| Adapt a raw ERP response | `POST /v1/responses/adapt` |
| Discover capabilities | `GET /v1/capabilities` |

That is the entire runtime surface Member 2 needs.

## 5. Member 1 boundary

**No runtime Member 1 → Member 4 call is required.** The normal flow is
Member 3 → Member 1 → Member 2 → ERP → Member 4.

Member 1 *may* consume design-time information if the group's policy component
needs it — `GET /v1/capabilities` and `GET /v1/schemas/{id}` are both safe to
read at design time. It must not read fresh transactional facts from Member 4;
those come from Member 2's live execution.

Member 4 contains no `PolicyGateClient` and no `Member1Client`, and a
structural test (`test_integration_security.py::TestPNoCrossMemberClients`)
scans the production package to keep it that way.

## 6. Authentication model

```
Browser / Member 3 UI
        │  user session auth  (Member 3's own concern)
        ▼
Member 3 trusted backend / BFF
        │  X-API-Key: <member 4 service key>
        ▼
    MEMBER 4
```

- Header: `X-API-Key`.
- **All mutating methods** (`POST`, `PUT`, `PATCH`, `DELETE`) require it.
- Reads require it only when `protect_reads` is enabled. It defaults to
  `False`; enable it for any deployment where reads are sensitive.
- Always public: `/v1/health/live`, `/v1/health/ready`, `/docs`, `/redoc`,
  `/openapi.json`.
- Comparison is constant-time (`hmac.compare_digest`), so a wrong key does not
  leak its length or prefix through timing.
- Neither the configured key nor a supplied wrong key appears in any response
  or log line.

### Frontend security — required

**Do not embed Member 4's service API key in browser JavaScript.** A Vite
`VITE_*` variable is inlined into the published bundle, so anything placed
there is public. Member 4's repository currently declares `VITE_API_KEY` in
`frontend/.env.example` with **no value**, and no source file reads it; a test
enforces both.

If the group's final application calls Member 4 directly from the browser for
a local demonstration, that configuration is **DEMO-ONLY / NOT PRODUCTION** and
must be labelled as such. It is not the default architecture.

## 7. CORS

Closed by default: `cors_origins` is empty, so no browser origin is allowed
until one is configured explicitly. `cors_allow_credentials` defaults to
`False`.

To allow a browser origin, configure it explicitly (for example
`http://localhost:5173`). Never use `allow_origins=["*"]`, and never combine a
wildcard with credentials — an AST-level test fails the build if a wildcard
appears as an actual argument anywhere in the production package.

Verified behaviour: a configured origin receives
`access-control-allow-origin`; an unconfigured origin does not.

## 8. Document upload workflow

```
POST /v1/files/documents          (multipart/form-data)

  file                = certificate.jpg
  source_system_id    = legacy_hr
  source_entity       = employees
  business_key_name   = employee_id
  business_key_value  = EMP002
  document_type       = birth_certificate
  sensitivity         = restricted
```

**201 Created:**

```json
{
  "upload_id": "...",
  "document_id": "...",
  "page_count": 1,
  "ocr_used": false,
  "index_job_id": "job_...",
  "indexing_status": "succeeded",
  "warnings": []
}
```

Then `GET /v1/jobs/{index_job_id}` until `status` is `succeeded`, then search.

**Indexing is automatic.** One call in; a searchable, resolvable document out.

Rules worth knowing before you build against it:

- Identity is **declared, never inferred**. A filename saying `EMP002` proves
  nothing, so nothing is read from it.
- `business_key_name` and `business_key_value` are one declaration in two
  fields. Sending half of it is **422**, not a partial store.
- `sensitivity` must be one of `public`, `internal`, `confidential`,
  `restricted`. An unrecognised value is **422** rather than a silent default.
- Every identity field is capped at 200 characters and rejected if it looks
  like a credential or a connection string.

## 9. CSV workflow

```
POST /v1/files/csv
    → schema_id, columns, schema_index_job_id, schema_indexing_status
    → the SCHEMA is indexed automatically
    → GET /v1/schemas/{schema_id}
    → POST /v1/mappings/suggest   (review / approve where needed)
    → POST /v1/sources            (register the source)
    → POST /v1/jobs               structured_pipeline or source_native_pipeline
    → rows become searchable
```

**Critical invariant: a CSV upload does NOT index its business rows.**

The schema may index immediately — structure is not business data. Rows require
a mapping decision or explicit source-native admission, and using schema
indexing as a backdoor around that review is exactly what this must not do.
Verified by searching `content_kind=structured_record` after an upload and
requiring zero hits.

Two further facts Member 3 will hit immediately:

- A source-native job **without a registered source** is `422`
  (`"a source-native pipeline needs a registered source"`).
- An inferred CSV schema has **no primary key**, and the extractor's fallback
  record key is the row number. A row's *position* is not its identity — it
  changes the moment a row is inserted above it — so every row is refused with
  `"no usable record identity"`. Declare the key on the job:

  ```json
  { "job_type": "source_native_pipeline",
    "source_id": "...", "schema_id": "...", "upload_id": "...",
    "options": { "key_fields": ["employee_id"] } }
  ```

  With that, all rows transform and index.

## 10. Schema workflow

*"Which table contains employee birth certificates?"*

```
POST /v1/search
{ "query": "which table contains employee birth certificates",
  "filters": { "content_kind": "schema" } }

→ hits with metadata.content_kind == "schema"
→ GET /v1/representations/{representation_id}
→ the structural representation: entity, fields, source type, normalized type
```

No separate schema-search endpoint exists or is needed. Retrieval quality is
bounded by the results measured in Phase 7, which were **not** tuned during
this phase.

## 11. Indexed retrieval workflow

```
POST /v1/search
{ "query": "birth certificate details",
  "filters": {
    "content_kind":      "document_chunk",
    "business_key_name": "employee_id",
    "business_key_value": "EMP002",
    "document_type":     "birth_certificate" } }
```

Each hit carries `representation_id`, `score`, `tier`, and a `metadata` block
including `sensitivity`, `page_start`, `chunk_index` and `document_id`.

```
GET /v1/representations/{representation_id}
→ text, content_hash, identity, page/chunk provenance, sensitivity
```

**Search does not return document text.** Hits carry identity and provenance;
text is resolved in a second call. Do not build a UI that expects text in a
hit — Qdrant holds no raw content by design, and this is the mechanism that
keeps it that way.

13 filterable fields are available; filters are exact-match and are re-checked
against authoritative state after the vector search, so a payload that
disagrees with state cannot leak a non-matching hit.

## 12. Live ERP read workflow

```
Member 3  "What is EMP002's current employment status?"
   → Member 1  evaluates policy               → ALLOW
   → Member 2  selects the operation, executes the ERP API   (exactly once)
   → Member 2  POST /v1/responses/adapt with the RAW response
   → Member 4  returns AI-ready content
   → Member 2 / Member 3
```

Measured: governance invoked 1×, ERP executed 1×, Member 4 adaptations 1×,
Member 4 ERP executions **0**, Member 4 policy decisions **0**.

## 13. ERP write workflow

Identical shape, with the write executed once after `ALLOW`:

```
Member 3  "Release payment INV-204."
   → Member 1  → ALLOW
   → Member 2  executes the write ONCE
   → Member 4  adapts the confirmation
```

Member 4 does not authorize, does not execute, and does not select the tool.

## 14. allow / deny / conditions

| Member 1 decision | Member 2 ERP executions | Member 4 adaptations |
|---|---|---|
| `ALLOW` | 1 | 1 |
| `DENY` | **0** | **0** |
| `ALLOW_WITH_CONDITIONS`, unsatisfied | **0** | **0** |
| `ALLOW_WITH_CONDITIONS`, satisfied | 1 | 1 |

Condition evaluation belongs to Member 1. Member 4 has no vocabulary for
conditions and is not consulted about them.

## 15. Response-adaptation contract

**Request** — `POST /v1/responses/adapt`:

```json
{
  "query": "What is EMP002's employment status?",
  "source_system_id": "legacy_hr",
  "endpoint": "/api/hr/employees/EMP002",
  "http_status": 200,
  "content_type": "application/json",
  "body": { "employee_id": "EMP002", "name": "Nimal Silva",
            "department": "Finance", "employment_status": "ACTIVE" }
}
```

Use `body_base64` instead of `body` for PDF or image responses, with the
matching `content_type`.

**Response:**

```json
{
  "response_type": "structured",
  "entity_type": "employee",
  "llm_ready": { "...": "selected, canonically-mapped fields" },
  "assets": [],
  "provenance": { "source_system_id": "legacy_hr",
                  "endpoint": "/api/hr/employees/EMP002",
                  "http_status": 200, "adapted_at": "...",
                  "engine_version": "1.0" },
  "transformation": { "input_bytes": 0, "output_bytes": 0,
                      "field_reduction_ratio": 0.0, "processing_ms": 0.0 },
  "warnings": [],
  "success": true,
  "partial": false
}
```

### Binary responses read differently

For a PDF or image, `llm_ready` is `{}` and `partial` is `true`. The extracted
text is in **`assets[0].text`**, with `page_count`, `page_start`, `page_end`,
`content_hash`, `size_bytes` and `extraction_status`. This is not a defect: a
PDF has no structured fields for field-selection to select. **Member 2 must
read `assets[].text` for binary responses.**

### Collection responses — measured limitation

A response containing several records adapts **the first record only**. The
caller is told, with the total count:

> `the response carried 3 records; the first was adapted and the rest were not`

The limitation is bounded because it is declared rather than silent. It was
**verified, not redesigned**, in this phase; a collection redesign would need
its own implementation, tests and evaluation.

## 16. Sensitivity metadata flow

```
declared at upload / job / field
        → strictest-wins resolution
        → representation metadata
        → vector metadata + storage routing
        → SearchHitResponse.metadata.sensitivity
        → GET /v1/representations/{id}.sensitivity
```

Levels: `public` < `internal` < `confidential` < `restricted`. Default
`internal`. Text at `confidential` and above is encrypted at rest with
AES-256-GCM (Phase 10).

**Member 4 reports sensitivity; it does not enforce it.** A `restricted`
document is returned with `sensitivity: "restricted"` attached, so the trusted
upstream layer can make an informed decision. Member 4 contains no
`if restricted: deny(user)` — that would move Member 1's decision into the
wrong component, and the caller here is a trusted server-side integration
rather than an end user.

## 17. Error handling

| Status | Meaning |
|---|---|
| `202` | job accepted (not finished — poll it) |
| `401` | missing or wrong `X-API-Key` |
| `404` | unknown job, schema, record or representation |
| `413` | upload exceeds the configured cap |
| `422` | invalid request — half a business key, unknown sensitivity, a source-native job with no registered source |

Error bodies are stable JSON:

```json
{ "success": false,
  "error": { "code": "INVALID_PIPELINE_REQUEST",
             "message": "...", "request_id": "..." } }
```

No Python tracebacks, no dataclass `repr`, no enum `repr`, no internal module
paths. Enums are rendered by their wire value.

## 18. Retry behaviour

- **Member 4 never retries an ERP business request.** A `404` or `503` body is
  adapted as what it is. Re-issuing an ERP call is Member 2's decision, and a
  retried write is a duplicated write.
- `POST /v1/jobs/{job_id}/retry` re-runs a **pipeline** job. It has nothing to
  do with ERP calls.
- `/v1/responses/adapt` is stateless and holds no session, so Member 2 may
  safely re-send an adaptation request.

## 19. Capabilities

`GET /v1/capabilities` returns the pre-existing fields plus an
`integration_capabilities` block added in this phase:

```json
{
  "integration_capabilities": {
    "remote_asset_fetching": {
      "supported": true,
      "enabled": false,
      "detail": "ships disabled: no HTTP client is bundled ..."
    }
  }
}
```

`supported` means this build contains the implementation. `enabled` means
**this deployment** has it wired. They are separate on purpose: remote asset
fetching is implemented and ships disabled, and a consumer told only `true`
would plan around a feature that refuses every call.

Advertised names: `csv_ingestion`, `document_ingestion`,
`automatic_document_indexing`, `schema_discovery`, `schema_vector_retrieval`,
`structured_transformation`, `semantic_search`, `representation_resolution`,
`response_adaptation`, `remote_asset_fetching`, `scheduled_sync`,
`sensitivity_metadata`.

Every `enabled` is computed from actual wiring, never from a constant, and a
test requires every advertised name to have a contract test behind it.

## 20. OpenAPI contract

- OpenAPI **3.1.0**, service version `1.0`, **24 operations**.
- Snapshot: `artifacts/phase13_openapi.json`, generated by its own test.
- Integration-critical operations asserted present: `getCapabilities`,
  `uploadCsv`, `uploadDocument`, `getSchema`, `suggestMapping`,
  `updateMapping`, `createJob`, `getJob`, `search`, `getRepresentation`,
  `adaptResponse`, `createSource`.
- Every mounted `/v1` route appears in the document — there is no undocumented
  production integration route.
- The document contains no API key.

## 21. Demo scenarios

1. **Indexed document retrieval** — legacy DB → `employees.EMP002.birth_certificate` BLOB → indexed by Member 4 → *"Give me EMP002 birth certificate details"* → identity-exact + semantic retrieval → resolution → restricted AI-ready text.
2. **Uploaded document** — Member 3 uploads EMP002's certificate with explicit metadata → automatic indexing → search → resolution. No hidden manual step.
3. **Schema query** — *"Which ERP table contains employee birth certificates?"* → schema retrieval → `employees.birth_certificate` with source and normalized type.
4. **Live ERP read** — *"What is EMP002's current employment status?"* → M1 ALLOW → M2 live ERP → raw JSON → M4 adapt → AI-ready result. Demonstrates live-fact vs indexed-knowledge.
5. **Write governance** — *"Release payment INV-204"* → DENY means no ERP call at all; ALLOW means M2 executes once and M4 adapts the confirmation.

Deterministic harness: `scripts/evaluate_phase11_group_integration.py`.

## 22. Integration test results

**Mini-evaluation:** 21/21 scenarios passed. All nine hard gates at zero:

| Gate | Value |
|---|---|
| failed integration scenarios | 0 |
| Member 4 ERP executions | 0 |
| Member 4 policy decisions | 0 |
| denied ERP operations executed | 0 |
| wrong identity results | 0 |
| unresolvable current hits | 0 |
| credential leakage | 0 |
| cross-member boundary violations | 0 |
| OpenAPI critical operation misses | 0 |

Member 2 ERP executions expected/actual: **5 / 5**.

**Integration suite:** 114 tests — Member 3 contracts 23, Member 2 contracts
20, group flows 19, security boundaries 28, contract surface 24.

**In-process latency** (harness measurement; says nothing about production ERP
latency, which is entirely on Member 2's side):

| Step | ms |
|---|---|
| upload → searchable | ~189 |
| search → resolved text | ~1315 (includes first-query model warm-up) |
| `POST /v1/responses/adapt` | ~21 |

## 23. Known integration limitations

1. **Collection responses adapt the first record only**, with a warning naming
   the total. Verified, documented, not redesigned.
2. **A credential inside an ERP business payload is passed through as
   content.** Redaction covers transport metadata — headers, provenance, logs,
   persistence — not response content. Adaptation is faithful by design, and a
   content filter that catches `db_password` but misses `dbPwd` would read as
   protection without being it. **Member 2 must not return credentials in ERP
   business payloads.**
3. **Schema retrieval quality** is bounded by Phase 7's measured results. Not
   tuned here, and its documented failures stand.
4. **Storage-tier sensitivity routing is not currently binding** — the
   `on_premises_only` constraint is enforced but excludes nothing while all
   three tiers are on-premises.
5. **Freshness of indexed data** is bounded by the sync poll interval. This is
   scheduled polling, not CDC and not database replication.
6. **`protect_reads` defaults to `False`**, so GET routes are unauthenticated
   unless a deployment enables it.
7. **SQL Server** support is implemented but live verification remains
   deferred.
