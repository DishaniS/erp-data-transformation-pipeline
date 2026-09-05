# IT22267290 — Complete Project Scenario and Workflow

**ERP-Aware Data Transformation Pipeline**
Research Project **R26-SE-034** · Member 4 · SLIIT 4th Year Research Component

| | |
|---|---|
| Primary implementation | `src/erp_pipeline/` |
| Packages | 17 |
| Python source lines | **60,276** across **181** files under `src/erp_pipeline/` |
| Test files | 104 |
| Full suite | 2943 passed · 63 skipped · 0 failed · 0 errors |
| REST operations | 23 |
| Analysis date | 2026-08-22 |
| Analysis mode | **READ-ONLY.** No source file was modified to produce this document. |

---

## How to read this document

Every claim carries its evidence — a file path, a class, a function, an
endpoint, a test, or an artifact. Where behaviour was verified by executing
code during this scan, the output is shown.

Status labels are used strictly and never blurred:

| Label | Meaning |
|---|---|
| **IMPLEMENTED** | Present in `src/`, reachable, covered by tests |
| **IMPLEMENTED — BACKEND ONLY** | Works over HTTP/Python; **no frontend UI** |
| **DEMO/EXAMPLE ONLY** | Exists in `scripts/` or `examples/`, not in the product path |
| **EXTERNAL INTEGRATION — NOT PRESENT IN THIS REPOSITORY** | Another member owns it; no code here |
| **NOT IMPLEMENTED** | Does not exist |
| **CONTRACT ONLY** | Parsed/described but never executed |

---

# PART 1 — WHAT IS THIS COMPONENT?

## One paragraph

This component is the **data layer that makes a legacy ERP system legible to
an AI**. Old ERP systems store business data behind opaque column names
(`inv_no`, `cust_ref`, `KUNNR`), across incompatible technologies (PostgreSQL,
MySQL, SQL Server, MongoDB, CSV exports, scanned PDFs), with no vocabulary an
AI model can reason over. This component discovers what a source actually
contains, maps its vendor-specific fields onto a shared canonical ERP
vocabulary with an explainable confidence score, converts and validates the
values deterministically, turns each record into AI-ready text plus a vector
embedding, and routes those vectors across hot/warm/cold storage tiers by an
explicit policy. It then closes the loop at the other end: when another team
member has already called a live ERP API, this component transforms the raw
response — JSON, PDF, image or unsupported binary — into compact, traceable,
query-relevant context for a language model. **It never calls an LLM, and it
never calls an ERP API.**

## Detailed

### 1. What problem does it solve?

A legacy ERP cannot be handed to an AI as-is, for four independent reasons,
each of which the component addresses:

| Problem | Where it is solved |
|---|---|
| **Vocabulary opacity** — `cust_ref` means nothing to a model | `mapping/` — canonical model + explainable scoring |
| **Technology fragmentation** — four database dialects, files, API specs | `connectors/`, `discovery/`, `ingestion/`, `api_specs/` |
| **No semantic access** — SQL cannot answer "unpaid supplier invoice in euros" | `ai/`, `storage/` — embeddings + vector retrieval |
| **Context bloat** — a raw ERP response wastes the model's context on plumbing | `response_adaptation/` — query-aware field selection |

### 2. Why does a traditional ERP need this component?

Evidence: `README.md`, `docs/canonical_erp_model.md`, `src/erp_pipeline/mapping/canonical_model.py`.

A traditional ERP is optimised for transactional integrity, not for meaning.
Its schema is authoritative but not self-describing; its field names encode
decades of local convention. Three things follow:

1. **You cannot change the ERP.** It is load-bearing, audited, and often
   vendor-locked. So the adaptation must happen *outside* it — which is exactly
   what this component is.
2. **Two ERP systems never agree on names.** `mapping/canonical_model.py`
   records that `invoice_id` is spelled `invoice_no`, `inv_id`, `inv_no`,
   `invoiceid`, `bill_no` and six other ways in the wild
   (`_INVOICE_FIELDS`, lines 359+). A canonical target model is what makes
   cross-system reasoning possible at all.
3. **AI needs vectors, ERP has rows.** Nothing in an ERP produces an embedding.

### 3. What kinds of ERP data can it consume?

Verified from `SourceType` (`src/erp_pipeline/schemas/enums.py:80-97`) — a
**closed** enum, deliberately:

```
POSTGRESQL · MYSQL · SQL_SERVER · MONGODB · CSV · PDF · IMAGE · OPENAPI · POSTMAN
```

Plus, at runtime, **live ERP API responses** (JSON / image / PDF / binary) via
`response_adaptation/` — which is not a `SourceType` because it is not a
registered source; it is a transient response.

### 4. What forms of data can it produce?

| Output | Contract | Evidence |
|---|---|---|
| `SourceSchema` | discovered/inferred structure | `schemas/source_models.py` |
| `MappingProfile` | explainable source→canonical mapping | `mapping/models.py` |
| `CanonicalRecord` | normalised, validated, identified ERP record | `schemas/canonical_models.py` |
| `AIRepresentation` | deterministic text + structured content | `ai/models.py` |
| `EmbeddingRecord` | 384-d vector + model fingerprint | `ai/models.py` |
| `RoutingDecision` | which tier, and the full reasoning | `storage/models.py:267` |
| `ProcessCase` / `ProcessModel` | observed process state | `process/models.py:278,410` |
| `IntegrityReport` | cross-store consistency findings | `verification/models.py` |
| `AdaptedResponse` | **LLM-ready context** | `response_adaptation/models.py` |

### 5. How does it make ERP information usable by AI/LLMs?

Two distinct mechanisms, and it is important not to conflate them:

- **Offline (Path A):** records become canonical → become text → become
  vectors → become semantically searchable. The AI can *find* relevant ERP
  knowledge.
- **Real-time (Path B):** a response that has already been fetched becomes
  compact canonical context. The AI can *read* an ERP answer without drowning
  in it.

### 6. What responsibilities belong to Member 4?

Schema discovery · schema catalog · canonical mapping · transformation ·
validation · identity · AI representation · embedding · tiered vector storage ·
semantic retrieval · incremental sync · schema-drift detection · cross-store
verification · process/case modelling · **response adaptation** · the REST
control plane over all of it.

### 7. What responsibilities explicitly do NOT belong to Member 4?

Each is enforced in code, not merely stated:

| Not Member 4 | Enforcement evidence |
|---|---|
| Deciding **which** ERP API to call | No such code path exists |
| **Executing** an ERP API | `api_specs/service.py:16` — "It does not call the API it just read." `/v1/capabilities` advertises this as a limitation (`api/routers.py:227-229`) |
| Generating an **answer** | `/v1/search` docstring: "No LLM and no generated answer" (`api/routers_data.py:600-604`) |
| **Authorization / governance** | EXTERNAL INTEGRATION — NOT PRESENT IN THIS REPOSITORY |
| **Inferring** sensitivity | Consumed only — see Part 7 |

---

# PART 2 — FINAL ARCHITECTURE

All 17 packages verified present in `src/erp_pipeline/`.

| Package | LOC | Responsibility | Main classes/services | Input | Output | Used by |
|---|---:|---|---|---|---|---|
| `schemas/` | 3,365 | The frozen contracts and the identity grammar. Every other package depends on it; it depends on none. | `SourceSchema`, `SourceEntity`, `SourceField`, `CanonicalRecord`, `SensitivityLevel`, `make_canonical_record_id` | — | contracts | all |
| `catalog/` | 2,458 | Durable PostgreSQL home for schemas, versions and mapping profiles. Exists so a schema survives a restart and can be diffed across time. | `CatalogService`, `publish_schema`, `register_source_system` | `SourceSchema` | versioned snapshot | discovery, api, sync |
| `connectors/` | 1,617 | One connection abstraction over four database dialects, so discovery is written once. | `PostgresConnector`, `MySqlConnector`, `SqlServerConnector`, `MongoConnector`, `registry` | connection config | live connection | discovery, sync |
| `discovery/` | 3,753 | Read a source's **declared** structure (SQL) or **observe** it (MongoDB). Separated because a declared schema is a fact and an observed one is a sample. | `DiscoveryService`, `mongodb_inference`, `type_mapping`, `profiling` | connection | `SourceSchema` | catalog, mapping |
| `ingestion/` | 4,469 | Files: CSV streaming, PDF/image extraction, OCR, magic-byte detection, content hashing. | `FileIngestionService`, `PdfFileIngestion`, `ImageFileIngestion`, `probe_ocr`, `detect_from_signature` | file path | `SourceSchema` / `ExtractedDocument` | api, response_adaptation |
| `api_specs/` | 5,283 | Parse OpenAPI 3.x / Swagger 2.0 / Postman **as contracts**. Never calls them. | `ApiSpecService`, `infer_structure_from_examples` | spec file | `SourceSchema` + operations | api, response_adaptation |
| `mapping/` | 4,339 | **The explainable mapping engine.** Source fields → canonical ERP fields, with per-candidate evidence. | `MappingService`, `ScoringWeights`, `CanonicalTargetModel`, `AliasIndex` | `SourceSchema` | `MappingProfile` + decisions | transformation, response_adaptation |
| `transformation/` | 4,876 | Deterministic conversion, normalisation, validation, quality codes, identity. | `TransformationService`, `TypeConverter`, `TransformationContext` | `SourceRecord` + profile | `CanonicalRecord` | sync, orchestration, response_adaptation |
| `process/` | 1,724 | Generic event-log → process-case modelling with observed current/next states. | `ProcessCaseService`, `build_case`, `build_process_model` | event rows | `ProcessCase`, `ProcessModel` | demo, ai |
| `sync/` | 4,387 | Incremental extraction by watermark, schema-drift detection, impact analysis, safe checkpointing. | `SyncService`, `SyncCoordinator`, `detect_drift`, `PostgresSyncStateStore` | source + state | changed records + new watermark | orchestration |
| `ai/` | 2,815 | Canonical record → AI text → embedding; chunking; model registry. | `canonical_record_to_representation`, `SentenceTransformerModel`, `chunk_document` | `CanonicalRecord` | `AIRepresentation`, `EmbeddingRecord` | storage, api |
| `storage/` | 6,339 | **Policy-driven hot/warm/cold tiering**, Qdrant tiers, encrypted cold archive, tier state, migration, cost model. | `StoragePolicyRouter`, `HybridVectorStore`, `QdrantHotTier`, `QdrantWarmTier`, `ColdArchiveTier` | `EmbeddingRecord` | `RoutingDecision` + stored vector | api, verification |
| `verification/` | 1,293 | Cross-store integrity: do canonical records, representations, embeddings, tier state and Qdrant agree? | `IntegrityVerificationService`, `IntegrityCode` (18 codes) | all stores | `IntegrityReport` | operations |
| `response_adaptation/` | 4,746 | **Phase 14.** Already-executed ERP response → LLM-ready context. | `ResponseAdaptationService`, `RelevanceScorer`, `AssetAdapter`, `detect_response_type` | `ResponseEnvelope` | `AdaptedResponse` | api |
| `orchestration/` | 4,264 | Job planning, durable job store, stage execution, canonical record store. | `OrchestrationService`, `PipelinePlanner`, `PostgresJobStore` | `JobRequest` | `Job` + stages | api |
| `api/` | 2,664 | FastAPI control plane: 23 operations, API-key auth, request ids, typed errors. | `create_app`, routers, `error_body` | HTTP | HTTP | frontend, members 1–3 |
| `runtime/` | 1,767 | Deployable composition, settings validation, idempotent schema bootstrap, upload/source/draft persistence. | `bootstrap_all`, `ApiSettings`, `PostgresUploadStore` | env | wired app | deployment |

**Why the split matters.** The dependency direction is strictly one-way:
`schemas/` ← everything, and `api/` depends on services but no service depends
on `api/`. Verified: `grep -rn "from erp_pipeline.api" src/erp_pipeline/` returns
matches only inside `api/` and `runtime/`. This is what lets the whole pipeline
run from a script or a test with no web server
(`orchestration/` imports no web framework — stated at `docs/orchestration_and_rest_api.md:36`).

---

# PART 3 — COMPLETE HIGH-LEVEL DATA FLOW

## PATH A — Offline / historical ERP data preparation

**The proposed flow in the brief is broadly correct, with three corrections
from actual code:**

1. **Source registration is NOT required for file uploads.** A CSV upload
   creates and registers a *synthetic* source system (`file_source` by
   default) inside the upload route (`api/routers_data.py:_publish_file_schema`).
   `POST /v1/sources` is for databases.
2. **Mapping is NOT automatic.** Upload stops at catalog. Mapping is a separate
   explicit call. This is the single most important STOP POINT in the system.
3. **"Validation" is not a separate stage after transformation** — it is a
   stage in the job graph (`PipelineStage.VALIDATE`) but the validation itself
   runs *inside* `TransformationService`.

```mermaid
flowchart TD
    A["ERP database / CSV / PDF / image"] --> B{"Entry"}
    B -->|"database"| C["POST /v1/sources<br/>register + credential_ref"]
    B -->|"file"| D["POST /v1/files/csv<br/>POST /v1/files/documents"]
    C --> E["POST /v1/sources/{id}/discover<br/>DiscoveryService"]
    D --> F["FileIngestionService<br/>detect → hash → parse"]
    E --> G["SourceSchema"]
    F --> G
    G --> H["CatalogService.publish_schema<br/>erp_catalog (PostgreSQL)"]
    H --> STOP(["STOP — nothing further is automatic"])
    STOP -.->|"explicit call"| I["POST /v1/mappings/suggest<br/>MappingService"]
    I --> J["MappingProfile<br/>+ per-field decisions"]
    J -.->|"human review"| K["PUT /v1/mappings/{id}<br/>overrides"]
    K --> L["POST /v1/jobs<br/>STRUCTURED_PIPELINE"]
    J --> L
    L --> M["EXTRACT → TRANSFORM → VALIDATE → LOAD"]
    M --> N["CanonicalRecord<br/>erp:sys:entity:key"]
    N --> O["AI_BUILD<br/>canonical_record_to_representation"]
    O --> P["AIRepresentation<br/>ai:entity:key"]
    P --> Q["EMBED<br/>all-MiniLM-L6-v2 · 384-d"]
    Q --> R["EmbeddingRecord"]
    R --> S["TIER_ROUTE<br/>StoragePolicyRouter"]
    S --> T{"RoutingDecision"}
    T -->|"hot"| U["Qdrant float32 in RAM"]
    T -->|"warm"| V["Qdrant int8 on disk"]
    T -->|"cold"| W["gzip + AES-256-GCM file"]
    U --> X["POST /v1/search"]
    V --> X
    W -.->|"include_cold=true<br/>rehydrate"| X
```

**Evidence for the stage graph:** `orchestration/planner.py:52-60`
(`STRUCTURED_TAIL`), `:62-67` (`DOCUMENT_STAGES`), `:69-78`
(`INCREMENTAL_STAGES`), `:80-84` (`SPEC_STAGES`), `:86` (`DRIFT_STAGES`).

## PATH B — Real-time ERP API response adaptation

**The proposed flow is correct.** One correction: *query relevance and context
optimisation are not two stages* — relevance selects, then `formatter.py`
applies budgets; they are deliberately separate because they fail differently
(see Part 13).

```mermaid
flowchart TD
    M2["Member 2 executes the ERP API<br/>EXTERNAL — NOT IN THIS REPOSITORY"] --> RAW["raw response"]
    RAW --> EP["POST /v1/responses/adapt<br/>api/routers_adaptation.py"]
    EP --> ENV["ResponseEnvelope"]
    ENV --> DET["detect_response_type()<br/>magic bytes > structure > content-type > fallback"]
    DET --> BR{"ResponseType"}

    BR -->|"STRUCTURED"| U1["unwrap_payload()<br/>structural envelope rule"]
    U1 --> U2["infer_response_schema()<br/>api_specs.inference · SchemaOrigin.INFERRED"]
    U2 --> U3["MappingService.generate()"]
    U3 --> U4["TransformationService.transform_record()"]
    U4 --> REL["RelevanceScorer.rank()<br/>alias · name · entity · identity"]
    REL --> FMT["build_payload()<br/>budgets · truncation · sensitivity"]

    BR -->|"IMAGE"| A1["AssetAdapter.adapt_bytes()<br/>ingest_image_file + OCR"]
    BR -->|"DOCUMENT"| A2["AssetAdapter.adapt_bytes()<br/>ingest_pdf_file + chunk_document"]
    BR -->|"BINARY / UNKNOWN"| A3["_unsupported()<br/>described, never guessed"]
    ENV -.->|"asset_urls"| A4["validate_asset_url()<br/>SSRF policy · refused by default"]

    FMT --> OUT["AdaptedResponse"]
    A1 --> OUT
    A2 --> OUT
    A3 --> OUT
    A4 --> OUT
    OUT --> M3["Member 3 / downstream model<br/>EXTERNAL"]
```

---

# PART 4 — FRONTEND: WHAT CAN THE USER ACTUALLY DO?

**The frontend is deliberately tiny. This must not be overstated.**

Full inventory (`frontend/src/`): `App.tsx`, `main.tsx`, `pages/Upload.tsx`,
`api/client.ts`, `api/types.ts`, `api/index.ts`, plus two test files.
**There is no router** — `App.tsx` renders exactly one page:

```tsx
// frontend/src/App.tsx
export default function App() {
  return <UploadPage />;
}
```

Its own docstring states the boundary: *"This frontend exists to put files into
the pipeline. Everything else the backend can do stays available over HTTP but
is deliberately not surfaced here."*

## Every user-facing operation

| # | Operation | Component | Endpoint | Payload | Backend route | Service | Shown to user |
|---|---|---|---|---|---|---|---|
| 1 | Upload CSV | `pages/Upload.tsx` → `DropBox(kind="csv")` | `POST /v1/files/csv` | `multipart/form-data`, field `file` | `routers_data.py:137 upload_csv` | `FileIngestionService` → `CatalogService` | `"{columns} columns, {size}"` |
| 2 | Upload PDF/image | `pages/Upload.tsx` → `DropBox(kind="document")` | `POST /v1/files/documents` | `multipart/form-data`, field `file` | `routers_data.py:182 upload_document` | `FileIngestionService` (PDF/image + OCR) | `"{n} pages, {size}"` |

That is the complete list. **Two operations.**

Client-side pre-checks (`api/client.ts:138-172`): `.csv` routes to the CSV box,
`.pdf/.png/.jpg/.jpeg` to the document box; a mismatch produces an inline
message without a round trip. The comment is explicit that this only chooses
the *endpoint* — "the backend still inspects the content and rejects a
mislabelled file."

## Everything else — BACKEND ONLY — NO FRONTEND UI

| Capability | Status |
|---|---|
| Source registration (`POST /v1/sources`) | **BACKEND ONLY — NO FRONTEND UI** |
| Connection test / discovery | **BACKEND ONLY — NO FRONTEND UI** |
| Schema view (`GET /v1/schemas/{id}`) | **BACKEND ONLY — NO FRONTEND UI** |
| Mapping suggest / review / override / validate | **BACKEND ONLY — NO FRONTEND UI** |
| Job execution and monitoring | **BACKEND ONLY — NO FRONTEND UI** |
| Semantic search (`POST /v1/search`) | **BACKEND ONLY — NO FRONTEND UI** |
| Record resolution (`GET /v1/records/{id}`) | **BACKEND ONLY — NO FRONTEND UI** |
| **Response adaptation** (`POST /v1/responses/adapt`) | **BACKEND ONLY — NO FRONTEND UI** |
| Storage / tier monitoring | **NOT IMPLEMENTED** (no endpoint at all) |
| API-spec upload | **BACKEND ONLY — NO FRONTEND UI** (the page mentions specs in prose only) |
| API-key entry in the browser | **NOT IMPLEMENTED** — `client.ts` sends no key header |

> **Viva warning.** The frontend cannot demonstrate mapping, search, or Phase 14.
> Those must be demonstrated over HTTP (`curl` / Swagger UI at `/docs`).

---

# PART 5 — SCENARIO 1: USER UPLOADS AN ERP CSV FROM THE FRONTEND

Input:

```
employees.csv
emp_id,full_name,date_of_birth,department
E001,Amal Perera,1998-05-12,Finance
E002,Nimal Silva,1997-03-20,HR
```

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Frontend component | `DropBox` inside `UploadPage`; `classifyUpload("employees.csv")` → `"csv"` | `frontend/src/pages/Upload.tsx`, `api/client.ts:149` |
| 2 | Endpoint | `POST /v1/files/csv` | `client.ts:120`, `routers_data.py:134` |
| 3 | HTTP representation | `multipart/form-data`, form field **`file`**. `Content-Type` deliberately **not** set by the client so the browser writes the boundary | `client.ts:71-79` |
| 4 | Validation | Three layers: (a) client extension pre-check; (b) `_store_upload` streams to disk then checks the suffix against `CSV_SUFFIXES = {".csv",".tsv",".txt"}`; (c) `FileIngestionService.ingest` re-detects by **content** | `client.ts:168`, `routers_data.py:_store_upload`, `ingestion/service.py` |
| 5 | File-type/security checks | Filename sanitised by the upload store (`UnsafeUploadNameError` exists); size cap (`UploadTooLargeError`, HTTP 413); magic-byte/text detection; **filename alone is never trusted** — `_store_upload` docstring says exactly this | `runtime/persistence.py`, `api/responses.py:ERROR_STATUS`, `ingestion/detection.py` |
| 6 | Where stored | `PostgresUploadStore` under the configured upload root; row in `erp_runtime.uploads` | `runtime/persistence.py:45 UPLOADS_TABLE` |
| 7 | Content hash | **Yes** — SHA-256 over the bytes, `hash_file()`; becomes `FileSource.content_hash` and `file_id` via `make_file_id()` | `ingestion/hashing.py:32-48`, `ingestion/service.py:169-182` |
| 8 | CSV parsing | Streaming reader with BOM/delimiter/encoding handling and conservative type inference | `ingestion/csv_ingestion.py`, `ingestion/csv_inference.py` |
| 9 | Schema inference | Column names → `SourceField` with `source_data_type` + `normalized_data_type`; entity name taken **from the filename** (`employees`) — this is why the UI tip says to name the file after the entity | `ingestion/csv_inference.py`; `Upload.tsx:216` |
| 10 | `SourceSchema` produced | `schema_id`, `source_system_id` (`file_source`), one `SourceEntity` (`employees`, `EntityKind.TABLE`) with 4 fields, `origin=SchemaOrigin.INFERRED` | `schemas/source_models.py` |
| 11 | Source system registered? | **Yes, automatically** — `_publish_file_schema` calls `catalog.register_source_system(ingestion.source_system())` **before** publishing. This exists because `schema_snapshots.source_system_id` is a foreign key; without it every upload raised `SourceSystemNotFoundError`. Fixed in commit `7d5504a` | `routers_data.py:_publish_file_schema` |
| 12 | Published to catalog? | **Yes, if a catalog is configured.** Failure is caught, **logged, and reported to the caller as a warning** — never silently discarded. The response's `published` flag says which happened | `routers_data.py:_publish_file_schema` |
| 13 | IDs generated | `upload_id`, `content_hash` (SHA-256), `file_id` (`file:{hash}`), `schema_id`, `source_system_id` | Part 30 |
| 14 | API returns | `CsvUploadResponse{upload_id, filename, content_hash, size_bytes, source_system_id, schema_id, columns, rows_observed, published, warnings}` — **HTTP 201**. **No row data is echoed**: "an ingestion endpoint that replayed business data would be an accidental data-export endpoint" | `routers_data.py:141-144, 163-173` |
| 15 | Frontend shows | `"Uploaded employees.csv — 4 columns, 118 B"`. It does **not** display the schema, the id, or the fields (`types.ts` doesn't even type `source_system_id` or `published`) | `Upload.tsx:170-174` |
| 16 | Persisted in PostgreSQL | `erp_runtime.uploads` (the upload); `erp_catalog.source_systems`, `erp_catalog.schema_snapshots`, `erp_catalog.source_entities`, `erp_catalog.source_fields` (the schema) | `runtime/persistence.py`, `catalog/schema.py:87-238` |
| 17 | What does NOT happen | No mapping. No transformation. No canonical records. No embeddings. No vectors. No storage routing. The **rows are never read into the pipeline** — only enough to infer types | — |

## THE STOP POINT

```
Upload → content hash → detect → parse → infer schema → register source
       → publish to catalog → STOP
```

**This is confirmed true.** Nothing downstream is triggered. The upload route
contains no call to `mapping`, `transformation`, `ai` or `storage`.

*Why it stops here:* a mapping is a claim about **meaning**. Generating and
executing one without a human seeing the ambiguities would silently turn a
proposal into production data — which the mapping engine explicitly refuses
(`DEFAULT_EXECUTABLE_STATUSES` excludes `SUGGESTED` and `REVIEW_REQUIRED`,
`transformation/models.py:770-775`).

---

# PART 6 — WHAT HAPPENS AFTER SCENARIO 1?

## Step 1 — Mapping

| Aspect | Detail | Evidence |
|---|---|---|
| Endpoint | `POST /v1/mappings/suggest` `{schema_id, strict}` | `routers_data.py:381` |
| Service | `MappingService.generate(schema, overrides, rejected, validate, strict)` | `mapping/service.py` |
| Algorithm | For each source field, generate candidates over every canonical field, score four weighted signals, apply a type veto, then classify by threshold | `mapping/scoring.py`, `mapping/engine.py` |
| **Signals & weights** | `name` **0.50** · `type` **0.20** · `entity` **0.20** · `path` **0.10** (sum = 1.0) | `mapping/models.py:90-115` |
| Name evidence kinds | `EXACT` (1.0) · `NORMALIZED_EXACT` (0.98) · `EXPLICIT_ALIAS` · token overlap | `mapping/scoring.py:54-55, 191-216` |
| Confidence | `high_threshold = 0.75`, `medium_threshold = 0.50` | `mapping/models.py:158-160` |
| Outcomes | `AUTO_SELECTED` · `MANUAL_OVERRIDE` · `AMBIGUOUS` · `UNMAPPED` | `mapping/models.py:63-82` |
| Ambiguity/refusal | When the top two candidates are within a margin, the field is reported **AMBIGUOUS rather than silently decided** | `mapping/models.py:162` |
| Human override | `PUT /v1/mappings/{id}` — overrides are **fed back through the engine**, not patched into the profile, so engine validation still applies. `"approve"` without a target is refused: *"a human waving through a choice they never made"* | `routers_data.py:400-436` |
| Validation | `POST /v1/mappings/{id}/validate` | `routers_data.py` |
| **Persistence** | Drafts live in `erp_runtime.mapping_drafts`; **in-process** cache `PipelineServices.mapping_drafts` is a plain `dict` | `runtime/persistence.py:46`, `orchestration/service.py:87` |

### Measured mapping quality (this is a real research result)

Executed during this scan: `pytest tests/erp_pipeline/mapping/test_mapping_benchmark.py -k reported -s`

```
PHASE 8 MAPPING BENCHMARK
labelled mappings      : 68 (60 positive, 8 negative)
top-1 accuracy         : 1.0
top-3 recall           : 1.0
auto-selection precision: 1.0 (60/60)
automatic coverage     : 0.8824
ambiguity rate         : 0.0
unmapped rate          : 0.0882
correct refusal rate   : 1.0
alias-independent top-1: 1.0 (18/18 labels the alias registry never declared)
```

The **alias-independent** figure is the honest generalisation measure: 18 of the
68 labels use names the alias registry never declared, and the engine still got
all 18 right. The **8 negative labels** matter equally — a benchmark with no
negatives rewards guessing (`test_mapping_benchmark.py:14`).

### A concrete mapping example (canonical vocabulary that actually exists)

Verified against `mapping/canonical_model.py`:

| Source field | Canonical target | Evidence kind |
|---|---|---|
| `inv_no` | `invoice.invoice_id` | explicit alias |
| `cust_ref` | `invoice.customer_id` | explicit alias (`customer_ref`) |
| `total_amt` | `invoice.amount` | explicit alias (`total_amt`) |
| `curr` | `invoice.currency` | abbreviation `curr`→`currency` |
| `approval_status` | `invoice.status` | explicit alias |

## What happens to `emp_id`, `date_of_birth`, `full_name`?

**This is the honest and important answer.** The canonical model contains
**exactly three entities and fourteen fields**
(`mapping/canonical_model.py:505 DEFAULT_CANONICAL_MODEL`):

| Entity | Fields |
|---|---|
| `invoice` | `invoice_id`, `customer_id`, `amount`, `currency`, `status`, `issued_on` |
| `customer` | `customer_id`, `name`, `email`, `phone` |
| `purchase_order` | `purchase_order_id`, `supplier_id`, `amount`, `status` |

There is **no `employee` entity**. Therefore for `employees.csv`:

- `full_name` may match `customer.name` (alias `full_name` is declared) — but
  that would map an employee onto a *customer*, which is why entity evidence
  (weight 0.20) exists to fight it.
- `emp_id`, `date_of_birth`, `department` have **no canonical target** and are
  reported `UNMAPPED`.
- The engine **refuses rather than invents** — this is exactly what the 8
  negative benchmark labels and the 1.0 correct-refusal rate measure.

**Consequence, stated plainly:** the employee scenario is *outside the canonical
model's coverage*. It still flows through the passthrough path (fields keep
their source names), but it gains no cross-system canonicalisation. This is a
declared limitation, not a defect — see Part 40.

---

# PART 7 — TRANSFORMATION WORKFLOW

```
SourceRecord ──► MappingProfile ──► TransformationService ──► CanonicalRecord
```

Entry: `TransformationService.transform_record(source_record, mapping_profile, context, run_id)`
→ `RecordTransformationResult` with `.outcome`, `.record`, `.issues`, `.rejected`, `.skipped`.

| Concern | Behaviour | Evidence |
|---|---|---|
| Value conversion | `TypeConverter` per `normalized_data_type` | `transformation/type_converter.py` |
| Normalization | Whitespace, case, configurable per policy | `transformation/` policies |
| Enum mapping | Only via an explicit `enum_map` rule. **Unmapped values raise `UNKNOWN_ENUM_VALUE`, never a guess.** Verified: `approval_status: "A"` stays `"A"` unless a rule says otherwise — the engine will not invent that `A` means `approved` | `IssueCode.UNKNOWN_ENUM_VALUE` (`models.py:71`) |
| Dates | `DatePolicy`, ISO output | `transformation/models.py:790` |
| Numbers | **`Decimal`, never float.** `"45000.00"` → `Decimal("45000.00")`, serialised as the exact string `"45000.00"` because *"rendering money as a float would silently change 25000.10 into 25000.099999999999"* | `schemas/serialization.py:102-105` |
| Missing values | `NullPolicy`; codes `SOURCE_FIELD_MISSING`, `SOURCE_VALUE_NULL` | `models.py:65-66` |
| Duplicates | Detected at load by canonical id; `DUPLICATE_RECORD_ID` in verification | `verification/models.py:35` |
| Validation | Runs **inside** transformation, emitting `DataQualityIssue` with stable `IssueCode`s — declared once and *"never derived from an exception class name or a message string"* | `models.py:55-62` |
| Quality issues | ~15 stable codes across extraction / conversion / computed / validation | `models.py:64-90` |
| Quarantine / refusal | Outcome `REJECTED` / `SKIPPED`; in sync, quarantined records **do not advance the watermark** | `models.py:867-868`, `sync/coordinator.py:195-198` |
| Identity | `make_canonical_record_id(source_system_id, entity_type, stable_source_key)` → `erp:{sys}:{entity}:{key}`. `require_business_key` + `looks_like_surrogate_key` **refuse a digits-only key** so a PostgreSQL `SERIAL` can never become identity | `schemas/identity.py:138-214` |
| Sensitivity | Carried on `CanonicalRecord.sensitivity`. **Configured/propagated, never inferred** | Part 31 |
| Config traceability | `TransformationOptions.fingerprint()` recorded on the run, so a record traces to the exact configuration that produced it | `transformation/models.py:778-786` |

### Realistic before / after

**Before** — raw ERP row + mapping profile:

```json
{"inv_no": "INV-204", "cust_ref": "CUS-17", "total_amt": "45000.00",
 "curr": "LKR", "approval_status": "A", "row_version": 7}
```

**After** — `CanonicalRecord` (verified by execution, Part 12):

```json
{
  "record_id": "erp:finance_erp:invoice:inv-204",
  "entity_type": "invoice",
  "normalized_data": {
    "invoice_id": "INV-204",
    "customer_id": "CUS-17",
    "amount": "45000.00",
    "currency": "LKR",
    "status": "A"
  },
  "sensitivity": "internal"
}
```

`row_version` is absent — no canonical target. `amount` is a `Decimal`.
`status` is **`"A"`, not `"approved"`** — the framework was never told what `A`
means and refuses to guess.

---

# PART 8 — AI REPRESENTATION + EMBEDDING WORKFLOW

`canonical_record_to_representation(record, config)` — `ai/representation.py:173`.

| Aspect | Behaviour | Evidence |
|---|---|---|
| Representation id | `make_representation_id(entity_type, record_id)` → `ai:{entity}:{normalized_key}` | `ai/models.py:296-305` |
| Canonical record id | Carried **forward** in `metadata["canonical_record_id"]` | `representation.py:224` |
| Text generation | `build_text()` — labelled `Key: Value` lines, humanised, sorted. **Generic**: *"nothing here knows what an invoice is"* | `representation.py:128-171` |
| Bounding | `max_characters`, truncation marked **in the text itself** with `[content truncated]` | `representation.py:165-170` |
| Metadata | Structural provenance **only** — `canonical_record_id`, `source_system_id`, `source_type`, `source_entity`, `sensitivity`, `representation_config`. **No business values** | `representation.py:220-231` |
| Sensitivity propagation | `record.sensitivity.value` copied into metadata → flows to storage routing | `representation.py:227` |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2`, **local only, no remote inference** | `ai/embedding.py:42` |
| Dimension | **MEASURED at load, not assumed** — probed via `get_sentence_embedding_dimension`, falling back to encoding a probe string | `embedding.py:149-175` |
| Model fingerprint | `f"{model_id}@{dimension}"` recorded on every embedding | `embedding.py:87` |
| Batching | `encode()` accepts a list | `embedding.py:213` |
| Skip-if-unchanged | Content hash compared; unchanged content is **not re-embedded** | `sync/coordinator.py` docstring: *"embed only if changed"* |
| Content hash | `representation_content_hash(representation_id, text_for_ai, content)` | `representation.py:232-234` |
| Embedding id | `make_embedding_id(representation_id, model_id)` — derived from **both**, so re-embedding updates one logical embedding while a model change is distinguishable | `ai/models.py:308-316` |

### Example representation (built by `build_text`)

```
Entity: Invoice
Source System: finance_erp
Source Entity: invoices
Amount: 45000.00
Currency: LKR
Customer Id: CUS-17
Invoice Id: INV-204
Status: A
```

`representation_id = ai:invoice:erp_finance_erp_invoice_inv-204`

> **Critical identity note.** `normalize_identifier` replaces `:` with `_`, so
> the canonical id is **lossy** inside the representation id. The canonical id
> **cannot be parsed back out** — which is why it is carried forward explicitly
> in metadata and in storage state. See Part 30.

---

# PART 9 — HOT / WARM / COLD STORAGE WORKFLOW

```
EmbeddingRecord → StorageRecordMetadata → StorageRoutingContext
               → StoragePolicyRouter.route() → RoutingDecision → tier
```

## Per-tier reality

| | HOT | WARM | COLD |
|---|---|---|---|
| Technology | Qdrant | Qdrant | local encrypted files |
| Vector format | float32, **no quantization, not on disk** | **int8 scalar quantization, `on_disk=True`** | serialized → gzip → AES-256-GCM |
| Location | `ON_PREMISES` | `ON_PREMISES` | `ON_PREMISES` |
| Performance intent | lowest latency, RAM-resident | lower footprint, some retrieval trade-off | **not searchable in place** |
| Cost intent (multiplier) | 1.0 | 0.4 | 0.05 |
| Encryption | — | — | **AES-256-GCM, 96-bit random nonce per write** |
| Quantization | none | int8, **verified from the server**, never rounded in Python | none |
| Retrieval | direct ANN | direct ANN | **requires rehydration into a temporary collection** |

Evidence: `storage/hot_tier.py:52-76`, `storage/warm_tier.py:1-23`,
`storage/cold_tier.py:1-42, 355-402`, `storage/storage_policy.py:82-88`.

> `warm_tier.py:16` — `quantization_verified()` returns True **only when the
> server itself reports a scalar quantizer**. "Nothing here rounds floats in
> Python and calls it quantization."

## The two-stage routing algorithm

### Stage 1 — hard constraints REMOVE tiers (before any scoring)

`StoragePolicyRouter.prohibited_tiers()` — `storage/vector_router.py:113-162`.
*"These are compliance and physics, not preference. Nothing in the scoring
stage can reinstate a tier that appears here."*

| Constraint | Effect |
|---|---|
| `requires_on_premises(sensitivity)` | removes any tier whose location is not `ON_PREMISES` |
| `legal_hold` | removes COLD — must stay directly readable |
| active retention | removes COLD |
| `LOW_LATENCY` requirement | removes COLD — cannot meet it through rehydration |
| `CRITICAL` criticality | removes COLD — *"never archived, regardless of age"* |

> **Honest note that must be stated in a viva.** `DEFAULT_TIER_LOCATIONS`
> (`storage_policy.py:82-88`) places **all three tiers `ON_PREMISES`**.
> Therefore *the on-premises constraint currently prohibits nothing.* The
> capability exists and is tested so that a deployment which later adds an
> off-premises archive cannot route restricted data into it by accident — the
> code comment says exactly this (`models.py:57-62`).

### Stage 2 — weighted scoring over six factors

`TierWeights` (`storage_policy.py:42-73`), all measured against normalised factors:

| Factor | HOT | WARM | COLD |
|---|---:|---:|---:|
| recency | 0.20 | 0.05 | — |
| access | 0.30 | 0.10 | — |
| criticality | 0.25 | 0.05 | — |
| latency | 0.25 | 0.05 | — |
| age | — | 0.40 | **0.35** |
| dormancy | — | 0.35 | **0.65** |

Normalisation saturations: `age_saturation_days = 180`,
`recent_access_window_days = 30`, `access_saturation_count = 20`,
`dormancy_saturation_days = 120`.

Two design decisions are documented **with their reasoning**, which is itself
research evidence:

- Age saturates at **180 days, not 365**: *"A 365-day saturation left
  six-month-old records scoring as 'recent', which kept them in HOT well past
  their useful heat."*
- COLD leans on **dormancy (0.65) over age (0.35)**: *"An even split let a
  record that was read last week be archived purely for being old… Archiving is
  about 'nobody is looking at this any more'."*

WARM has the flattest weights on purpose — *"it wins by the others not
winning, which is what a middle tier should do."*

### Stage 3 — hysteresis

`minimum_residence_days = 7.0` and a challenger must beat the incumbent by a
margin — *"Without it, 0.501 vs 0.499 would trigger a physical data movement."*
(`storage_policy.py:136-142`, `vector_router.py:304`).

## Supporting machinery

| Feature | Evidence |
|---|---|
| Manual overrides | `TransitionReason.MANUAL_OVERRIDE`; `RoutingDecision.forced` distinguishes *constraint/override left no choice* from *scoring preferred* |
| Migration | `storage/migration.py` — **re-checks policy before moving**; refuses a prohibited destination (`:191-205`) |
| Access statistics | `erp_vector_storage.vector_access_stats` |
| Transition audit | `erp_vector_storage.vector_tier_transitions` with stable `TransitionReason` codes — *"never free text"* |
| Cost tracking | `storage/cost.py` — normalized units, explicitly **not currency** |
| Provenance/evidence | `RoutingDecision.scores` carries every tier's score **and** its prohibition reason |

## Worked examples — and their status

| Example | Outcome | Status |
|---|---|---|
| Frequently accessed invoice → HOT | high recency + access + latency | **POLICY EXAMPLE** — follows from the weights |
| Less frequently used record → WARM | age/dormancy moderate, nothing argues for extremes | **POLICY EXAMPLE** |
| Long-term archival vector → COLD | dormancy saturated, no constraint blocks COLD | **POLICY EXAMPLE** |

**These are policy examples, not measured runtime behaviour.** What *is*
measured is in `artifacts/tiered_storage_benchmark.json` (Part 39): latency,
recall, cold archive bytes, quantization verification and rehydration fidelity
over a 500-record corpus.

---

# PART 10 — SEMANTIC SEARCH WORKFLOW

`POST /v1/search` — `api/routers_data.py:596-694`.

| # | Step | Implementation |
|---|---|---|
| 1 | Guard | Refuses if no embedding service **or** no storage (`InvalidPipelineRequestError` → 422) |
| 2 | Filter parse | `SearchFilters.from_mapping(payload.filters)`. **Unknown filters are REFUSED with 422, never ignored** |
| 3 | Query embedding | `services.embedding.model.encode([payload.query])[0]` — the same local model, no LLM |
| 4 | Retrieval | `services.storage.search(vector, limit, include_cold, filters)` |
| 5 | HOT/WARM | Filters pushed **server-side into Qdrant** so the ANN search itself is constrained |
| 6 | COLD | Off by default. Filters applied to **tier-state metadata before rehydration** — *"a filtered-out archive is never decrypted at all"* |
| 7 | Merge / dedupe | Inside `HybridVectorStore.search` |
| 8 | Authoritative metadata | `services.storage.state.load(hit.representation_id)` — **PostgreSQL tier state, not Qdrant**. Hot search runs `with_payload=False` (`hot_tier.py:195`) |
| 9 | Canonical id | `hit.canonical_record_id or metadata.canonical_record_id` — **carried forward, never reconstructed** by parsing the representation id |
| 10 | Response | `SearchResponse` with hits, `tiers_searched`, `filters_applied`, `deep_search_used`, `took_ms`. **No vector is ever returned** |

## Supported filters (closed set)

`FILTERABLE_FIELDS` — `storage/filters.py:38-45`:

```
entity_type · source_system_id · source_entity · sensitivity · document_id
```

`sensitivity` is validated against the `SensitivityLevel` enum
(`_ENUM_FIELDS`, `filters.py:49`). The module docstring gives the reason for
refusing unknown filters: *"a filter that is silently dropped returns a
plausible-looking unfiltered result, which is the single worst thing a
retrieval API can do to a caller who is about to put those results in front of
a governance model."*

## Search → record resolution

```
POST /v1/search → SearchHitResponse.canonical_record_id
                → GET /v1/records/{canonical_record_id}
                → RecordResponse.data (business values only)
```

`GET /v1/records/{record_id:path}` uses a `:path` converter so the colons in
`erp:finance_erp:invoice:inv-204` survive routing. `get_record`
(`routers_data.py:702-733`) returns `record.to_json_dict()["normalized_data"]`
— *"Business values only. Provenance and internal metadata stay out of the
payload body."* Returns 404 `RecordNotFoundError` if absent.

### Concrete example

```bash
curl -X POST http://127.0.0.1:8000/v1/search \
  -H "X-API-Key: $ERP_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"unpaid supplier invoice in euros","top_k":5,
       "include_cold":false,"filters":{"entity_type":"invoice"}}'
```

```json
{"query_model":"sentence-transformers/all-MiniLM-L6-v2","dimension":384,
 "hits":[{"representation_id":"ai:invoice:erp_finance_erp_invoice_inv-204",
          "canonical_record_id":"erp:finance_erp:invoice:inv-204",
          "record_id":"erp:finance_erp:invoice:inv-204",
          "entity_type":"invoice","score":0.71,"tier":"hot",
          "metadata":{"content_hash":"…","model_id":"…","source_system_id":"finance_erp",
                      "source_entity":"invoices","sensitivity":"internal","document_id":null}}],
 "tiers_searched":["hot","warm"],"include_cold":false,
 "filters_applied":{"entity_type":"invoice"},"deep_search_used":false,"took_ms":18.4}
```

*(Shape verified against `api/schemas.py` `SearchResponse`/`SearchHitResponse`
and the route body; score/timing values are illustrative.)*

Tests: `tests/erp_pipeline/api/test_search_resolution_and_filters.py`,
`tests/erp_pipeline/storage/test_search_filters.py`,
`tests/erp_pipeline/storage/test_canonical_resolution.py`.

---

# PART 11 — SCENARIO 2A: "FIND E002 BIRTH CERTIFICATE DETAILS" (GROUP LEVEL)

| # | Step | Owner | Status in this repository |
|---|---|---|---|
| 1 | User asks the question | — | — |
| 2 | Governance / authorization decision | Member 1 | **EXTERNAL INTEGRATION — NOT PRESENT IN THIS REPOSITORY** |
| 3 | Choose which ERP API answers it (MCP/tool selection) | Member 2 | **EXTERNAL INTEGRATION — NOT PRESENT IN THIS REPOSITORY** |
| 4 | Execute the legacy ERP API call | Member 2 | **EXTERNAL INTEGRATION — NOT PRESENT IN THIS REPOSITORY** |
| 5 | Raw response comes back | Member 2 | — |
| 6 | **Detect, adapt, canonicalise, select, bound** | **Member 4** | **OWNED BY THIS REPOSITORY** — `POST /v1/responses/adapt` |
| 7 | LLM-ready context returned | **Member 4** | **OWNED BY THIS REPOSITORY** — `AdaptedResponse.llm_ready` |
| 8 | Prompt assembly + model invocation | Member 3 | **EXTERNAL INTEGRATION — NOT PRESENT IN THIS REPOSITORY** |
| 9 | Answer shown to the user | Member 3 | **EXTERNAL INTEGRATION — NOT PRESENT IN THIS REPOSITORY** |

**Only steps 6 and 7 are implemented here.** No code for Members 1, 2 or 3 was
found: `src/` contains only `erp_pipeline/`, and no module imports or references
a member-owned package.

```mermaid
sequenceDiagram
    actor U as User
    participant M1 as Member 1 — Governance (EXTERNAL)
    participant M2 as Member 2 — MCP / ERP execution (EXTERNAL)
    participant ERP as Legacy ERP
    participant M4 as Member 4 — this repository
    participant M3 as Member 3 — LLM answer (EXTERNAL)

    U->>M1: "Find E002 birth certificate details."
    M1->>M1: authorize (NOT IMPLEMENTED HERE)
    M1->>M2: permitted request
    M2->>ERP: GET /api/employees/E002/documents
    ERP-->>M2: raw JSON / PDF / image
    M2->>M4: POST /v1/responses/adapt
    Note over M4: detect → unwrap → map → transform<br/>→ relevance → budgets
    M4-->>M2: AdaptedResponse (llm_ready + report + provenance)
    M2->>M3: LLM-ready context
    M3-->>U: natural-language answer
```

---

# PART 12 — SCENARIO 2B: ERP RETURNS JSON

**This section reports behaviour VERIFIED BY EXECUTION during this scan**, not
inferred from the design.

Input posted to the service:

```json
{"status":"success",
 "result":{"emp_no":"E002","employee_name":"Nimal Silva","doc_type":"BIRTH_CERT",
           "certificate_no":"BC-928821","date_of_birth":"1997-03-20",
           "place_of_birth":"Kandy","document_url":"https://erp.example/doc/BC-928821.pdf",
           "internal_row_version":81}}
```

## The exact call chain

| # | Stage | Exact class / function | File |
|---|---|---|---|
| 1 | Route | `adapt_response()` | `api/routers_adaptation.py:110` |
| 2 | Decode base64 (binary only) | `_decode_raw()` | `api/routers_adaptation.py:90` |
| 3 | Budget overlay | `_apply_options()` | `api/routers_adaptation.py:62` |
| 4 | Build input | `ResponseEnvelope(...)` | `response_adaptation/models.py` |
| 5 | Orchestrate | `ResponseAdaptationService.adapt()` | `response_adaptation/service.py` |
| 6 | Classify | `detect_response_type(content_type, body, raw)` | `response_adaptation/detector.py:122` |
| 7 | Unwrap | `unwrap_payload(body, max_depth=3)` | `response_adaptation/structured.py:82` |
| 8 | Count input | `count_leaf_fields(body)` | `structured.py:156` |
| 9 | Infer schema | `infer_response_schema(...)` → `SchemaOrigin.INFERRED` | `structured.py:179` |
| 10 | Map | `StructuredResponseAdapter.adapt()` → `MappingService.generate()` | `structured.py:287` |
| 11 | Transform | `TransformationService.transform_record()` | `structured.py:321` |
| 12 | Candidates | `_mapped_candidates()` / `_passthrough_candidates()` | `service.py` |
| 13 | **Score** | `RelevanceScorer.rank(...)` | `relevance.py` |
| 14 | Mandatory preservation | `infer_identity_field()` + `REASON_MANDATORY` | `relevance.py` |
| 15 | Budgets | `build_payload()` | `formatter.py:99` |
| 16 | Reconcile report | `apply_budget_to_decisions()`, `limit_decisions()` | `formatter.py` |
| 17 | Result | `AdaptedResponse` | `models.py` |

## MEASURED behaviour — three queries, same payload

```
Q: "Find E002 birth certificate details."
   entity_type : None      wrapper_path: ('result',)
   llm_ready   : 8 fields (certificate_no, date_of_birth, doc_type, document_url,
                           emp_no, employee_name, internal_row_version, place_of_birth)
   metrics     : 9 input leaves -> 8 selected | context reduction 0.113208
   removed     : {}

Q: "What is E002's date of birth?"
   llm_ready   : {"certificate_no":"BC-928821","date_of_birth":"1997-03-20"}
   metrics     : 9 -> 2 | context reduction 0.777358
   removed     : {'score_below_threshold': 6}

Q: "Where was E002 born?"
   llm_ready   : 8 fields
   reasons     : {'no_relevance_signal', 'mandatory_identity_field'}
```

## Four honest findings from this execution

### Finding 1 — `entity_type` is `None`: this runs the PASSTHROUGH path

The canonical model has **no `employee` and no `document` entity** (Part 6), so
`MappingService` produced no profile and the service fell back to
`_passthrough_candidates()`. Fields keep their **source names**; there is no
canonicalisation. The relevance mechanism still runs — on the `name` signal
alone — which is why the date-of-birth query still works well.

**This is the most important correction to the intuitive story.** The E002
birth-certificate scenario does *not* demonstrate ERP canonical mapping. It
demonstrates detection, unwrapping, relevance, identity preservation and
budgeting. Use an **invoice** payload to demonstrate canonical mapping.

### Finding 2 — "details" triggers broad-query behaviour

`is_broad_query()` matched `details` from `BROAD_QUERY_TERMS`, so selection
**stepped aside** and every field was kept with reason
`broad_query_requests_whole_record`. Context reduction was only **0.113** — the
`status` wrapper was removed and nothing else.

Correct by design (the user literally asked for "details"), but it means **the
headline query in the project brief is the one that reduces least.** For a
demonstration use "What is E002's date of birth?" (0.777 reduction).

### Finding 3 — `certificate_no` became the inferred identity

No canonical `is_identifier` existed, so `infer_identity_field()` selected the
first field whose **raw** tokens end in `_id`/`_no`/`_number` in flattened
(alphabetical) order: `certificate_no` sorts before `emp_no`. It is preserved
with reason `mandatory_identity_field` even at score 0.0.

Defensible — a certificate number *is* the document's key — but worth knowing:
**the heuristic picks alphabetically-first, not semantically-best.**

### Finding 4 — A REAL DEFECT: `E002` tokenises to `email`

Verified directly during this scan:

```
E002    split=('e','002')   canonical=('email','002')
E001    split=('e','001')   canonical=('email','001')
DEFAULT_SYNONYMS['e'] == 'email'
```

`mapping/normalization.py` maps the single letter `e` → `email` (intended for
`e_mail` → `(e, mail)`). Any ERP identifier of the form *letter + digits*
therefore injects a spurious `email` token into the query.

**Demonstrated consequence** — a query that never mentions email selects the
email field:

```
query: "Who is customer E002?"   →   llm_ready = ['customer_id', 'email', 'name']
   KEEP email_addr   0.75   score_above_threshold   alias=1.0 name=0.5
```

**Status: CONFIRMED DEFECT — NOT FIXED (this task is read-only).**
Impact: a false-positive field — costs precision, not recall. It does not appear
in the Phase 14 evaluation because no evaluation query contains a letter+digit
identifier of this shape. **Disclose this in the viva rather than letting a
panel find it.**

```mermaid
flowchart TD
    A["raw JSON: status + result 8 fields"] --> B["detect_response_type → STRUCTURED"]
    B --> C["unwrap_payload → wrapper_path=('result',)"]
    C --> D["count_leaf_fields → 9"]
    D --> E["infer_response_schema (INFERRED)"]
    E --> F{"MappingService matched an entity?"}
    F -->|"NO — no employee entity"| G["_passthrough_candidates<br/>entity_type = None"]
    F -->|"yes"| H["TransformationService → CanonicalRecord"]
    G --> I["RelevanceScorer.rank"]
    H --> I
    I --> J["infer_identity_field → certificate_no MANDATORY"]
    J --> K["build_payload: budgets + JSON-safe"]
    K --> L["AdaptedResponse"]
```

---

# PART 13 — QUERY-RELEVANCE ALGORITHM

`src/erp_pipeline/response_adaptation/relevance.py` — **the research mechanism
of Phase 14.**

## The scoring formula

```
score = (alias·Wa + name·Wn + entity·We + identity·Wi) / (Wa + Wn + We + Wi)
```

| Signal | Weight | Definition | Implementation |
|---|---:|---|---|
| `alias` | **0.45** | Best coverage across the canonical field's **name and every declared alias** — the ERP vocabulary | `_alias_coverage()` |
| `name` | **0.30** | Coverage of the **source field's** own tokens | `_coverage()` |
| `entity` | **0.15** | 1.0 same entity · 0.25 different entity · 0.0 unmapped · 0.5 entity unknown | `_entity_signal()` |
| `identity` | **0.10** | 1.0 if the canonical field `is_identifier` | inline in `score_field()` |

Defaults confirmed in the artifact's `configuration.relevance_weights` block.
`alias` is heaviest because it is what makes the mechanism **ERP-aware rather
than lexical**.

## Normalization and matching

**Overlap coefficient, not Jaccard.** `_coverage()` divides shared tokens by
`min(|target|, |query|)`:

> *"Dividing by the target alone would penalise a field for having a longer name
> than the question used — asking 'when is it due' would score `due_date` at 0.5
> purely because the schema also spelled out 'date'."*

**Asymmetric by design** — a question is a sentence, a field name is one or two
words; symmetric similarity would punish every field for the length of the
question that asked about it.

**Entity-noun discounting** (`entity_tokens()`) — the entity's own name is
removed from **both** sides:

> *"An invoice response has aliases `invoice_amount`, `invoice_date` and
> `invoice_status`, so the word 'invoice' would half-match all three and the
> lexical signal would stop distinguishing between them. Entity membership is
> already measured, once, by the `entity` signal; counting it again would be the
> same evidence paid for twice."*

**Structural-suffix handling** (`GENERIC_FIELD_TOKENS`, `_distinctive()`) —
`id, no, num, nbr, number, code, key, ref, reference, name, value, flag, ind,
indicator` are dropped from the *target* side unless that empties it.
`merchant_name` → `merchant`.

**Query-intent vocabulary** (`QUERY_INTENT_TERMS`, **31 entries**, verified),
matched **contiguously against raw tokens before stopword removal** — "how much"
is two stopwords and would otherwise be gone before it could be recognised:

```
("how","much")  → amount, total, price      ("who",)   → customer, supplier, name
("how","many")  → quantity, count           ("when",)  → date
("overdue",)    → due, date, status         ("paid",)  → status, payment
("outstanding",)→ amount, status            ("where",) → address, location
```

Kept **separate** from the mapping vocabulary because declaring "much" a synonym
of "amount" globally would corrupt every schema mapping in the pipeline.
**Hand-authored: part of the method, not an emergent result. Its size is
reported in the artifact.**

## Threshold — derived, not chosen

`minimum_relevance_score = 0.25`, with the derivation written into the code:

> A field mapped cleanly onto the queried entity but never mentioned scores
> exactly `entity / total = 0.15` — the entity signal alone. A threshold at or
> below that floor admits **every** well-mapped field regardless of the
> question, *making query relevance decorative*. 0.25 sits above the floor;
> any real alias/name evidence (≥ 0.5 coverage = 0.225 alone) clears it.

## Preservation and fallbacks

| Rule | Reason code | Behaviour |
|---|---|---|
| Mandatory identity | `mandatory_identity_field` | Selected **before** the budget is consulted; still counts against it |
| Inferred identity | (same code, via `infer_identity_field()`) | Raw-token suffix `_id`/`_no`/`_number`; returns `None` rather than guessing from field order |
| Broad question | `broad_query_requests_whole_record` | 10 `BROAD_QUERY_TERMS` → keep everything |
| No query | `no_query_supplied` | Keep everything |
| **No signal** | `no_relevance_signal` | If nothing non-mandatory clears the threshold, **keep everything** |
| Policy block | `blocked_by_policy` | Outranks mandatory |
| Field budget | `field_budget_exhausted` | — |
| Character budget | `character_budget_exhausted` | Applied in `formatter.py` |
| Sensitivity | `blocked_by_sensitivity` | Applied in `formatter.py` |

The **no-signal fallback** is the conservative failure, and the code says why:

> *"Returning the identity field alone would be a confidently wrong answer: the
> caller learns which record it is and nothing else… Falling back to the
> unfiltered record costs context, which is measurable and bounded by the
> budgets, instead of costing recall, which is not recoverable downstream."*

Every field in such a result is marked `no_relevance_signal`, so the evaluation
**counts an abstention as an abstention** rather than crediting the fallback as
a successful selection.

## Deterministic tie-breaking

Ordering key: `(not mandatory, -score, source_field)`. The name tie-break stops
equally-scored fields swapping places between runs — *"an evaluation cannot be
reproducible without it."* Asserted by
`test_scoring_is_deterministic_across_instances`.

## Budgets

`max_fields = 24` · `max_output_characters = 8000` · `max_value_characters = 2000`.
**Characters, not tokens** — this project ships no tokenizer, and adding one
would mean shipping a model's vocabulary to make a budget decision. Numbers are
never clipped: *"45000.00 cut to 450 is not a shorter amount, it is a wrong
one."*

## Worked scored-field examples (MEASURED)

**Example 1 — the alias route carries a field the query never names literally**

```
query "who is the customer?"      field cust_ref → invoice.customer_id
signals: alias=1.0  name=1.0  entity=1.0  identity=0.0      score 1.00   KEEP
```

Both routes fire here: the alias registry declares `customer_ref`, **and** the
pipeline's abbreviation table expands `cust`→`customer`. Isolating the alias
route (name weight 0) still selects it —
`test_the_canonical_vocabulary_alone_can_select_a_field`.

**Example 2 — the intent lexicon reaches a word the query never used**

```
query "how much is this invoice for?"   → tokens gain 'amount','total','price'
field total_amt → invoice.amount
signals: alias=1.0  name=1.0  entity=1.0  identity=0.0      score 0.90   KEEP
```

**Example 3 — operational plumbing is refused**

```
query "how much is this invoice for?"   field row_version → (no canonical target)
signals: alias=0.0  name=0.0  entity=0.0  identity=0.0      score 0.00   DROP
reason: score_below_threshold
```

**Example 4 — the entity-only floor, which the threshold exists to reject**

```
query "what currency?"            field cust_ref → invoice.customer_id
signals: alias=0.0  name=0.0  entity=1.0  identity=0.0      score 0.15   DROP
```

---

# PART 14 — SCENARIO 2C: ERP RETURNS A PDF BIRTH CERTIFICATE

```
PDF bytes → detect_response_type → DOCUMENT → AssetAdapter._adapt_document
          → _temp_file → ingest_pdf_file → text + OCR fallback + page provenance
          → chunk_document (page anchoring) → AdaptedAsset → AdaptedResponse
```

| Stage | Implementation |
|---|---|
| Detect | `detect_from_signature(payload[:64])` matches `b"%PDF-"` → `FileType.PDF` → `ResponseType.DOCUMENT` |
| Materialise | `_temp_file(payload, ".pdf")` — both ingestors take a path because both wrap libraries that open files. Rewriting them "for the convenience of one caller" was rejected as the larger risk |
| Extract | `ingest_pdf_file(FileSource, PdfOptions(max_pages, max_total_text_chars, ocr_fallback))` → PyMuPDF |
| OCR fallback | `PdfOptions.ocr_fallback=True`, triggered per page when the text layer is empty or near-empty (`ocr_min_text_chars`) |
| Page provenance | `ExtractedPage(page_number, text, status, extraction_method, char_count, truncated)`; `page_range=(min,max)` on the asset |
| Chunking | `chunk_document(document, ChunkingConfig)` — used for **page anchoring**, not to split the output |
| Cleanup | `_cleanup(path)` in `finally`, swallowing `OSError` |

## Behaviour per PDF condition (from current code)

| Condition | Result |
|---|---|
| **Text PDF** | `kind=DOCUMENT`, `text` populated, `ocr_used=False`, `page_count`, `page_range`, `llm_directly_readable=False` |
| **Scanned PDF** | Empty text layer → OCR per page → `ocr_used=True` (`page.extraction_method == "ocr"`). **If Tesseract is absent**, `extraction_status` reports `ocr_unavailable` and text is empty — verified in this environment |
| **Corrupt PDF** | `MalformedPDFError` caught → degrades to `AssetKind.UNSUPPORTED_BINARY` with warning `"the document could not be read (MalformedPDFError)"`. **Never fatal to the JSON that arrived with it** |
| **Encrypted PDF** | `EncryptedPDFError` (an `IngestionError`) → the same degradation path |
| **Oversized PDF** | Two ceilings: `AssetOptions.max_bytes` (12 MB) raises `AssetTooLargeError` **before extraction**; `max_text_chars` (20,000) bounds the text and sets `truncated=True` |

> **Two defects found and fixed during Phase 14**, worth citing as engineering
> evidence. (1) On Windows a parser that fails to open a file may still hold a
> handle; `unlink` in a `finally` then raised `PermissionError`, which
> *replaced* the extraction error — turning "this PDF is corrupt" into
> "permission denied". (2) The PDF ingestor's own character budget truncated
> text while the asset reported `truncated=False` — a shortened document
> presented as complete. Both fixed; `test_extracted_text_is_bounded` pins the
> second.

---

# PART 15 — SCENARIO 2D: ERP RETURNS AN IMAGE

```
JPEG/PNG bytes → detect_from_signature (magic bytes) → IMAGE
               → _temp_file → ingest_image_file(ImageOptions)
               → Pillow header inspection (dimensions) → OCR → hash_bytes
               → AdaptedAsset
```

| Aspect | Implementation |
|---|---|
| Magic-byte validation | `_SIGNATURES` — PNG `\x89PNG\r\n\x1a\n`, JPEG `\xff\xd8\xff`, TIFF `II*\x00`/`MM\x00*`, WEBP `RIFF????WEBP` |
| Dimensions | From `ExtractedDocument.document_metadata["width"/"height"]`, read by `_inspect()` via `Image.open` — a **lazy header parse before any pixel decode**, which is the decompression-bomb defence (`max_pixels`) |
| OCR | `ImageOptions(ocr_enabled, ocr_language, max_text_chars)`; `ocr_used = page.extraction_method == "ocr"` |
| Content hash | `hash_bytes(payload)` — SHA-256 |
| `llm_directly_readable` | **`True`** — the one asset kind a model can take as-is |

**Why OCR text is carried *alongside* the image, not instead of it:**

> *"a caller with a vision-capable model is not forced to accept a lossy
> transcription of a document it could have read."*

## What is sent downstream

`AdaptedAsset.to_dict()` emits `type, mime_type, size_bytes, content_hash,
llm_directly_readable, width, height, extraction_status, source_url, label,
ocr_used, text, truncated, warnings`.

## Are raw bytes ever placed in `llm_ready`?

**No. Verified three independent ways:**

1. **By contract** — `llm_ready` is populated only by `build_payload()`, which
   reads canonical/source *field values*. The asset path writes to `assets` and
   sets `llm_ready={}` (`service.py::_adapt_non_structured`).
2. **By the model docstring** — `AdaptedAsset`: *"Raw bytes are NEVER placed in
   this contract."*
3. **By test** —
   `tests/erp_pipeline/response_adaptation/test_assets_and_url_safety.py::test_no_asset_ever_carries_raw_bytes`:

```python
asset = AssetAdapter().adapt_bytes(png_bytes, "image/png")
payload = asset.to_dict()
assert not any(isinstance(value, (bytes, bytearray)) for value in payload.values())
```

Also `test_service_and_api.py::test_the_output_is_json_serializable` — the whole
`to_dict()` must survive `json.dumps`, which raw bytes could not.

---

# PART 16 — SCENARIO 2E: ERP RETURNS AN IMAGE/DOCUMENT URL

An asset URL is chosen by the **ERP system**, not by us. Fetching it
unconditionally would make this service a request proxy **inside the network
perimeter** — the classic SSRF position, where `http://169.254.169.254/` returns
cloud credentials and `http://127.0.0.1:5432/` reaches a database that trusts
local connections.

## Every control, from `assets.py`

| Control | Implementation | Default |
|---|---|---|
| **Fetching disabled by default** | `UrlSafetyPolicy.enabled = False` | **off** |
| **No HTTP client ships** | `Fetcher` is injected; `fetch_asset` raises `AssetFetchRefusedError(rule="url_fetching_disabled")` when `fetcher is None` | none |
| Scheme allow-list | `allowed_schemes = {"https"}` — this is what refuses `file://`, `ftp://`, `gopher://` | https only |
| Port allow-list | `DEFAULT_ALLOWED_PORTS = {80, 443, 8080, 8443}` | — |
| Host allow-list | `allowed_hosts` (empty = any); *"the strongest control available and the one a production deployment should use"* | off |
| DNS resolution | `default_resolver()` via `socket.getaddrinfo`; **injected**, so no test ever needs a network | — |
| **Every address checked** | *"A DNS entry that returns one public and one loopback address would otherwise pass validation and then connect to whichever the OS picked."* | — |
| Loopback / private / link-local / multicast / reserved / unspecified | `_is_forbidden_address()` via `ipaddress` | blocked |
| **IPv4-mapped IPv6** | `parsed.ipv4_mapped` unwrapped first — *"how `http://[::ffff:127.0.0.1]/` slips past a naive check"* | blocked |
| Credentials in URL | `parts.username or parts.password` → `credentials_in_url` | refused |
| Redirect re-validation | `final_url != url` → refused unless `max_redirects > 0`, then **re-validated** | `max_redirects = 0` |
| Size limit | `max_bytes = 12 MB`, checked after fetch and again before adaptation | — |
| Timeout | `timeout_seconds = 10.0`, carried on `ValidatedUrl` | — |
| MIME vs magic bytes | Declared type compared to `detect_from_signature`; **bytes win, mismatch reported as a warning** | — |
| Header forwarding | **None.** No request headers are constructed here at all | — |

## Allowed vs refused — MEASURED (15 parametrised cases pass)

```
ALLOW   https://cdn.example.com/a.png             (resolves 93.184.216.34)

REFUSE  https://169.254.169.254/latest/meta-data/   rule=private_or_reserved_address
REFUSE  https://localhost/a.png                     rule=private_or_reserved_address
REFUSE  https://internal.erp/a.png     (10.0.0.5)   rule=private_or_reserved_address
REFUSE  https://intranet/a.png      (192.168.1.10)  rule=private_or_reserved_address
REFUSE  https://svc/a.png           (172.16.4.4)    rule=private_or_reserved_address
REFUSE  https://v6/a.png                 (::1)      rule=private_or_reserved_address
REFUSE  https://mapped/a.png  (::ffff:127.0.0.1)    rule=private_or_reserved_address
REFUSE  https://mixed.example.com (public+loopback) rule=private_or_reserved_address
REFUSE  file:///etc/passwd                          rule=scheme_not_allowed
REFUSE  ftp://example.com/a.png                     rule=scheme_not_allowed
REFUSE  gopher://example.com/a                      rule=scheme_not_allowed
REFUSE  http://example.com/a.png                    rule=scheme_not_allowed
REFUSE  https://example.com:5432/a.png              rule=port_not_allowed
REFUSE  https://example.com:6379/a.png              rule=port_not_allowed
REFUSE  https://user:pass@example.com/a.png         rule=credentials_in_url
REFUSE  https:///a.png                              rule=no_host
REFUSE  (default policy, any URL)                   rule=url_fetching_disabled
REFUSE  cdn.example.com → 169.254.169.254 redirect  rule=too_many_redirects
```

Every refusal carries a **named rule**, so an operator learns *which setting to
change*, not merely that the fetch did not happen.

## Partial success

A refused URL becomes `refused_asset(url, reason, label)` → `AssetKind.REFUSED`,
plus a warning. **The JSON that adapted correctly is kept.** Verified by
`test_a_refused_asset_url_does_not_discard_the_json_that_adapted`:

```python
assert result.success        # True
assert result.is_partial     # True
assert result.llm_ready["invoice_id"] == "INV-204"
assert result.assets[-1].kind.value == "refused"
```

> *"Discarding the JSON over the image would be the wrong trade every time."*

A refused URL is recorded **rather than omitted**, so a caller comparing a
response against its asset list can see that something was referenced and
deliberately not retrieved.

---

# PART 17 — SCENARIO 2F: UNSUPPORTED BINARY

ZIP, unknown `application/octet-stream`, or anything with no matching signature
→ `AssetAdapter._unsupported()`.

**MEASURED:**

```
input : b"PK\x03\x04" + 200 zero bytes, declared "application/zip"
output: kind=unsupported_binary   mime_type=application/zip   size_bytes=204
        content_hash=<sha256>     llm_directly_readable=False  text=None
        extraction_status="unsupported"
        warnings=["the payload is not a supported image or PDF; only its
                   metadata was adapted"]
```

## Are raw bytes passed to the LLM?

**No.** `text` is `None` and no byte-valued key exists in `to_dict()`;
`test_no_asset_ever_carries_raw_bytes` covers this path too. The whole response
still reports `success=True`
(`test_an_unsupported_binary_response_still_succeeds`).

**Why this is a success and not an error**, from the code:

> *"A response whose JSON adapted correctly should not be discarded because it
> also carried a ZIP attachment. The caller receives a truthful description
> saying the content is unavailable, which a model can relay, rather than a
> hallucination-inviting silence."*

Fallback metadata produced: `kind`, `mime_type`, `size_bytes`, `content_hash`,
`llm_directly_readable=False`, `extraction_status="unsupported"`, `source_url`,
`label`, `warnings`.

---

# PART 18 — REAL-TIME RESPONSE ADAPTATION OUTPUT

`AdaptedResponse` — `response_adaptation/models.py`.

| Field | Type | Meaning |
|---|---|---|
| `response_type` | `ResponseType` | `structured` / `image` / `document` / `binary` / `unknown` |
| `entity_type` | `str \| None` | Canonical entity, or **`None` when the canonical model has no vocabulary for it** |
| `llm_ready` | `Mapping[str, Any]` | **The payload to put in front of a model.** Canonical keys where mapped, source names otherwise |
| `assets` | `tuple[AdaptedAsset, ...]` | Images/documents/binaries/refusals. Text + metadata, **never bytes** |
| `provenance` | `AdaptationProvenance` | source system, endpoint, status, content type, `adapted_at`, engine version, **config fingerprint**, sensitivity, **allow-listed headers**, canonical record id, source entity |
| `transformation` | `TransformationMetrics` | `input_bytes`, `output_bytes`, `input_fields`, `selected_fields`, `processing_ms`, `truncated`. **Ratios are derived properties** — a caller cannot report a reduction that did not happen |
| `report` | `AdaptationReport` | detection, detected entity, entity confidence, input/selected counts, **per-field decisions with all four signals**, `removed_by_reason`, `wrapper_path`, `decisions_truncated` |
| `warnings` | `tuple[str, ...]` | Everything that went partly wrong |
| `success` | `bool` | **False only when nothing usable could be produced** |
| `is_partial` | property | `success and bool(warnings)` |

## Complete realistic example (shape verified by execution)

```json
{
  "response_type": "structured",
  "entity_type": "invoice",
  "llm_ready": {"invoice_id": "INV-204", "amount": "45000.00", "currency": "LKR"},
  "assets": [],
  "provenance": {
    "source_system_id": "finance_erp",
    "endpoint": "/api/invoices/INV-204",
    "http_status": 200,
    "content_type": "application/json",
    "adapted_at": "2026-08-22T09:00:00+00:00",
    "engine_version": "1.0",
    "config_fingerprint": "adapt@1.0/w(...)/policy(...)/min=0.25/max_fields=24/...",
    "sensitivity": "internal",
    "headers": {"Content-Type": "application/json", "ETag": "W/\"9\""},
    "canonical_record_id": "erp:finance_erp:invoice:inv-204",
    "source_entity": "invoice"
  },
  "transformation": {
    "input_bytes": 223, "output_bytes": 61,
    "input_fields": 10, "selected_fields": 3,
    "field_reduction_ratio": 0.7, "size_reduction_ratio": 0.726457,
    "processing_ms": 16.022, "truncated": false
  },
  "report": {
    "detection": {"response_type": "structured", "evidence": "payload_structure"},
    "detected_entity": "invoice", "entity_confidence": 0.892,
    "input_field_count": 10, "selected_field_count": 3,
    "wrapper_path": ["result"],
    "removed_by_reason": {"score_below_threshold": 5},
    "field_decisions": [
      {"source_field": "curr", "canonical_target": "invoice.currency",
       "score": 0.9, "selected": true, "reason": "score_above_threshold",
       "signals": {"alias": 1.0, "name": 1.0, "entity": 1.0, "identity": 0.0}}
    ],
    "decisions_truncated": false
  },
  "warnings": [],
  "success": true
}
```

The `Authorization: Bearer …` header sent with that request is **absent** —
allow-listed out. `test_the_endpoint_never_echoes_an_authorization_header` and
`test_authorization_headers_never_reach_provenance` assert that `"SECRET-TOKEN"`,
`"SECRET-KEY"` and `"session=SECRET"` appear nowhere in the serialized output.

---

# PART 19 — PHASE 14 MEASUREMENTS

Source: `artifacts/response_adaptation_evaluation.json`, produced by
`scripts/evaluate_response_adaptation.py`. **Values below are
copied exactly from the artifact — no rounding was applied.**

## Dataset

| | |
|---|---|
| Cases | **68** |
| Labelled relevant fields | **149** |
| Labelled irrelevant fields | **225** |
| Payloads | synthetic, modelled on real ERP response shapes |
| Labelling | single annotator (the component author); no inter-annotator agreement possible |
| LLM used | **false** · external services used: **none** |
| Engine version | 1.0 |
| Intent lexicon entries | 31 · broad-query terms: 10 |
| Environment | Python 3.13.9, Windows-11 |

Category distribution:

| Category | Cases |
|---|---:|
| invoice | 24 |
| customer | 14 |
| purchase_order | 11 |
| receipt | 7 |
| document | 6 |
| process_case | 6 |

## The three methods

| Metric | RAW | GENERIC | **ERP-AWARE ADAPTIVE** |
|---|---:|---:|---:|
| Relevant field recall | 1.0 | 1.0 | **0.979866** |
| Cases with perfect recall | 1.0 | 1.0 | **0.955882** |
| Irrelevant field removal | 0.0 | 0.0 | **0.608889** |
| Field reduction ratio | 0.0 | 0.1168 | **0.4736** |
| Context reduction ratio | 0.0 | 0.143311 | **0.500405** |
| Adaptation success rate | 1.0 | 1.0 | **1.0** |
| Median latency (ms) | 0.0002 | 0.0409 | **15.8268** |
| p95 latency (ms) | 0.0004 | 0.0763 | **24.0542** |
| Mean latency (ms) | 0.0003 | 0.0475 | 16.49 |

Absolute totals — identical inputs for all three: **16,049 input bytes**,
**625 input leaf fields**.

| | RAW | GENERIC | ADAPTIVE |
|---|---:|---:|---:|
| output_bytes | 16,049 | 13,749 | **8,018** |
| output_fields | 625 | 552 | **329** |
| relevant kept / 149 | 149 | 149 | **146** |
| irrelevant removed / 225 | 0 | 0 | **137** |

## Per category (proposed method)

| Category | n | Recall | Context reduction |
|---|---:|---:|---:|
| customer | 14 | 1.0 | 0.470551 |
| receipt | 7 | 1.0 | 0.474239 |
| document | 6 | 1.0 | 0.205729 |
| invoice | 24 | 0.981818 | 0.605308 |
| purchase_order | 11 | 0.958333 | 0.540117 |
| process_case | 6 | 0.923077 | 0.422 |

## Two fairness corrections applied before these numbers were accepted

Both moved results **against** the proposed method:

1. **Field matcher.** RAW never unwraps, so its path to a nested address is
   `customer.contact.email` while the other two reach `contact.email`. Exact
   string matching scored RAW as *missing fields it plainly contained*. With a
   unified matcher (`field_present()` — equality or dotted-suffix, one
   direction only), RAW recall rose **0.973 → 1.000**.
2. **Field counting.** Counting top-level keys credited RAW with a **70%
   "field reduction"** for handing over an untouched three-key envelope
   wrapping ten leaves. With leaf counting for every method, RAW field
   reduction fell **0.707 → 0.000**.

## The honest headline

**The proposed method is worse than both baselines on recall.** Both baselines
achieve 1.0 trivially, by not making a decision. The proposed method removes
61% of labelled noise and halves serialized context for a 2% recall cost —
three fields across 68 cases, each named in Part 21.

---

# PART 20 — ABLATION

One ablation, isolating the single mechanism Phase 14 contributes. Unwrapping,
canonical mapping and budgets are **identical** in both arms, so the difference
is attributable.

| Arm | Recall | Irrelevant removed | Field reduction | Context reduction |
|---|---:|---:|---:|---:|
| **With** query relevance | 0.979866 | 0.608889 | 0.4736 | 0.500405 |
| **Without** query relevance | 1.0 | 0.0 | 0.1168 | 0.1673 |

## What changed

- Context reduction **0.1673 → 0.5004** — query relevance contributes roughly
  **two thirds of the total reduction**.
- Irrelevant removal **0.0 → 0.6089** — *all* of it.
- Recall **1.0 → 0.9799** — *all* of the loss.

## What stayed the same

Envelope unwrapping, canonical mapping, identity preservation, budgets,
sensitivity handling, adaptation success rate (1.0 in both arms), and the
dataset.

## What it demonstrates

1. The reduction is **not** an artefact of unwrapping. Unwrapping + canonical
   mapping alone yield 0.1673 — real, but a third of the total.
2. Query relevance is the **causal** component of both the benefit and the cost.
3. The trade is **explicit and one-directional**: context is bought with recall.
   Reported plainly rather than averaged into a single score that would hide it.

---

# PART 21 — KNOWN RECALL FAILURES

All three, exactly as recorded in the artifact's `limitations` block.

| Case | Query | Missed field | Category | Reason |
|---|---|---|---|---|
| `sap-04` | "What is the status of this invoice?" | `BELNR` | **insufficient ERP vocabulary** | The canonical model's `invoice_id` alias list contains no SAP mnemonics, and `BELNR` carries no `_id`/`_no` suffix for the identity heuristic to recognise. The record key was dropped for a question that did not name it. |
| `po-05` | "How much did we order and from whom?" | `supplier_no` | **insufficient query vocabulary** | The question asks "from **whom**". The intent lexicon contains `who` but not its objective inflection `whom`. |
| `proc-02` | "Who performed this activity?" | `resource` | **insufficient query vocabulary** | "Who" should reach `resource` — process-mining vocabulary for the actor. The lexicon maps `who` onto customer/supplier/name only. |

## Why the vocabulary was deliberately NOT modified

Each failure has an obvious one-line "fix": add `BELNR` to the canonical alias
list, add `("whom",)` to `QUERY_INTENT_TERMS`, add `resource` to the `who`
expansion. **None was applied.**

> Extending the vocabulary *in response to observing which specific cases
> failed* is fitting the vocabulary to the test set. The resulting recall
> number would no longer measure the method — it would measure how many
> failures the author had already seen.

The line that **was** applied: **method defects were fixed; vocabulary gaps were
reported.** Four method defects were fixed after the first evaluation run,
raising recall 0.899 → 0.980:

1. Identity inference for entities outside the canonical model
2. Broad-query handling
3. Generic structural-suffix discounting
4. The unified fairness matcher

These are *general rules* that would have been written the same way without
seeing the dataset. The three remaining failures are *specific vocabulary
entries* that only this dataset motivates — which is exactly the distinction.

**Three named, classified limitations make the evaluation more credible than a
tuned 1.0.** Recorded in the artifact, in
`docs/architecture/RESPONSE_ADAPTATION_IMPLEMENTATION_REPORT.md` §20, and in the project's
persistent memory so a future session does not silently "fix" them.

---

# PART 22 — PROCESS / CASE WORKFLOW

`src/erp_pipeline/process/` — 7 files, 1,724 lines. Created during the
architecture consolidation to hold the **generic** process-mining capability
that the former BPI-specific package contained.

```
event rows → ProcessEvent → ProcessCase → (+ProcessModel) → CanonicalRecord → AIRepresentation
```

| Stage | Implementation | File |
|---|---|---|
| Column meaning | `EventLogConfig` — **which column is the case id, the activity, the timestamp, the resource**. Configuration, not hard-coded | `process/models.py:64` |
| Row → event | `event_normalizer.py` | — |
| Group | `group_events(events)` by case id | `case_builder.py:243` |
| Build a case | `build_case(...)` → `ProcessCase` | `case_builder.py:182` |
| Sequence | `sort_events()` → `activity_sequence()` → `unique_activities()` | `case_builder.py:38-64` |
| Duration | `case_duration_seconds(start, end)` | `case_builder.py:81` |
| Entity refs | `extract_entity_references()` → `{canonical entity type: business key}` | `case_builder.py:91` |
| Directly-follows model | `build_process_model(cases)` → `ProcessModel` | `case_builder.py:280` |
| Apply model | `apply_process_model()` → populates `allowed_next_states` | `case_builder.py:336` |
| Case identity | `make_case_record_id(...)` | `process/models.py:260` |
| To canonical/AI | `process/cascade.py` | — |
| Orchestrating service | `ProcessCaseService` | `process/service.py` |

## `ProcessCase` — the contract

```
case_record_id · case_id · process_type · source_system_id · total_events
activity_sequence · unique_activities · events
start_timestamp · end_timestamp · duration_seconds
current_state · allowed_next_states · entity_references
content_hash · config_fingerprint
```

Two fields carry the honesty of the design, in the code's own words:

- **`current_state`** — *"Last observed activity. The closest thing to a process
  state that an event log can honestly report: it is **observed, not
  declared**."*
- **`allowed_next_states`** — *"Successors observed elsewhere in the same
  process. **Empty until a `ProcessModel` is applied**, because a single case
  cannot know what the process as a whole allows."*

This matters: the component does **not** claim to know a business's real
workflow rules. It reports what the log actually shows.

## How BPI 2020 demonstrates it

**DEMO/EXAMPLE ONLY.** The BPI 2020 dataset is demonstrated through
`scripts/demos/run_bpi2020_demo.py` driving the **generic** framework, with the
only dataset-specific knowledge isolated in
`examples/bpi2020/event_log_config.json`. This separation was a deliberate
outcome of the consolidation
(`docs/architecture/ARCHITECTURE_CONSOLIDATION_REPORT.md`), and the README states
the rule: *"no module carries dataset-specific knowledge. Dataset vocabulary
belongs in `examples/`, in a fixture, or in a demo script — never in a generic
module."*

Evidence that the rule is enforced: during consolidation, the word
`"declaration"` was found in the **default** document-classification rules —
BPI vocabulary leaking into a generic module. It was removed from core and moved
into `examples/bpi2020/event_log_config.json`.

## Does process have a runtime `JobType`?

**NO — NOT IMPLEMENTED.** Verified exhaustively from
`orchestration/models.py:85-92`:

```python
class JobType(str, Enum):
    STRUCTURED_PIPELINE = "structured_pipeline"
    DOCUMENT_PIPELINE   = "document_pipeline"
    INCREMENTAL_SYNC    = "incremental_sync"
    DRIFT_CHECK         = "drift_check"
    API_SPEC_PREPARATION = "api_spec_preparation"
```

There is **no `PROCESS_PIPELINE`**, and `PipelinePlanner.plan()` has no branch
for one. Consequently:

- Process/case modelling is **BACKEND ONLY, and not even reachable through
  `POST /v1/jobs`.**
- It is reachable from Python (`ProcessCaseService`) and from the BPI demo
  script only.
- `/v1/capabilities` truthfully advertises only the five job types above.

---

# PART 23 — INCREMENTAL SYNC + SCHEMA DRIFT

```
load state → drift check → bounded change fetch → transform → canonical upsert
           → affected representations → rebuild → content hash
           → embed ONLY if changed → vector update → checkpoint safely → report
```

| Stage | Class / function | File |
|---|---|---|
| Durable state | `PostgresSyncStateStore` / `InMemorySyncStateStore` | `sync/state.py:191, 62` |
| State table | `erp_sync.sync_state` | `sync/state.py:44-45` |
| Watermark | `SyncState.watermark`, `advanced_to(...)` | `sync/models.py` |
| Extraction | `sync/extractor.py` — bounded batches | — |
| Schema fingerprint | `sync/hashing.py`, `SourceSchema.schema_hash` | — |
| Drift detection | `detect_drift(...)` → `DriftReport(findings)` | `sync/drift.py:357` |
| Drift classification | `DriftType`, `DriftSeverity`, `DriftStatus`, `_classify_field_change` | `sync/drift.py:39-215` |
| Impact analysis | `sync/impact.py` — which mappings a change breaks | — |
| Propagation | `sync/propagation.py` — record → representation → embedding → vector | — |
| Orchestration | `SyncCoordinator.run()` | `sync/coordinator.py:151` |
| Service facade | `SyncService` | `sync/service.py:83` |
| Job types | `INCREMENTAL_SYNC`, `DRIFT_CHECK` | `orchestration/models.py:90-91` |
| Stage graph | `INCREMENTAL_STAGES` = DRIFT_CHECK → EXTRACT_CHANGED → TRANSFORM → VALIDATE → LOAD → AI_BUILD → EMBED → **TIER_UPDATE** | `planner.py:69-78` |
| Eligible sources | `INCREMENTAL_SOURCES` = PostgreSQL, MySQL, SQL Server, MongoDB **only** | `orchestration/service.py:350-357` |

> A PDF or an OpenAPI document has no cursor and no change stream, *"so
> pretending it supports CDC would be a lie the planner then has to live with."*

## Does the checkpoint advance on failure?

**NO — and this is the safety property of the whole package.**
`sync/coordinator.py:195-198`:

```python
# Keep processing to collect the full picture, but never let
# the checkpoint pass the failure - that would lose it.
checkpoint_open = False
continue
```

The module docstring states the rule precisely:

> *"The watermark advances to the last change that completed **EVERY** stage,
> and never past a change that did not. A row that was merely READ does not move
> it; a row whose vector write failed does not move it either. That is what
> makes an interrupted run resumable without losing work."*

Nuance worth knowing for a viva: with `FailurePolicy.SKIP` the caller has
**explicitly accepted losing that change**, so the checkpoint *may* pass it —
`skipped` is counted separately from `failed`, and the final `SyncStatus` is
`ACTIVE` only when `failed == 0`.

State is saved with `expected_version=state.version` — **optimistic concurrency**,
so two concurrent runs cannot silently clobber each other's watermark.

## Per-change behaviour

| Change | Behaviour |
|---|---|
| **New column appears** | `DriftFinding` of additive type, low severity. Existing mappings still execute; the new field is `UNMAPPED` until a human maps it |
| **Column type changes** | `_classify_field_change` raises severity. If the change breaks a mapping's type compatibility, impact analysis flags the mapping |
| **Mapping becomes unsafe** | Records are **quarantined** rather than transformed with a broken profile; `MappingNotExecutableError` → HTTP 409 |
| **Record changes** | Transform → canonical upsert → representation rebuilt → **content hash compared** → embedded **only if changed** → vector upserted (same deterministic UUID → update in place) → `TIER_UPDATE` |
| **Embedding changes** | New vector, same `representation_id` and same vector UUID; tier state updated, transition recorded if the tier moved |

---

# PART 24 — CROSS-STORE VERIFICATION

`src/erp_pipeline/verification/` — `IntegrityVerificationService`, 6 files.

Checks agreement across **five** layers: `CanonicalRecord` ·
`AIRepresentation` · `EmbeddingRecord` · storage metadata / tier state ·
Qdrant.

## All 18 `IntegrityCode`s (`verification/models.py:29-58`)

| Group | Codes |
|---|---|
| **Identity** | `MALFORMED_RECORD_ID`, `SURROGATE_KEY_IDENTITY`, `DUPLICATE_RECORD_ID` |
| **Presence** | `CANONICAL_RECORD_MISSING`, `REPRESENTATION_MISSING`, `EMBEDDING_MISSING`, `VECTOR_MISSING` |
| **Agreement** | `CONTENT_HASH_MISMATCH`, `CANONICAL_REFERENCE_MISMATCH`, `MODEL_ID_MISMATCH`, `DIMENSION_MISMATCH`, `VECTOR_ID_MISMATCH`, `TIER_METADATA_MISMATCH`, `ENTITY_TYPE_MISMATCH` |
| **Orphans** | `ORPHANED_VECTOR`, `ORPHANED_TIER_STATE` |
| **Embedding state** | `EMBEDDING_NOT_GENERATED`, `EMBEDDING_STALE` |

## The important ones explained

| Code | What it catches | Why it matters |
|---|---|---|
| `ORPHANED_VECTOR` | A vector in Qdrant with no canonical record behind it | Search would return a hit that cannot be resolved — the worst retrieval failure |
| `CONTENT_HASH_MISMATCH` | Stored hash ≠ recomputed hash | Content drifted without the pipeline noticing; the vector answers from stale text |
| `MODEL_ID_MISMATCH` | Vector produced by a different embedding model | Two incomparable vector spaces in one collection |
| `DIMENSION_MISMATCH` | Vector length ≠ collection dimension | A structurally impossible search |
| `CANONICAL_REFERENCE_MISMATCH` | `representation.metadata["canonical_record_id"]` disagrees with tier state | Traceability broken — a hit cannot be resolved to the right record |
| `TIER_METADATA_MISMATCH` | Tier state says HOT, the vector is in WARM | Routing decisions no longer describe reality |
| `SURROGATE_KEY_IDENTITY` | A canonical id built from a `SERIAL`/row number | The id will change on reload — identity is not stable |

## Severity model

`IntegritySeverity.FAILURE` vs `WARNING` (`models.py:64-74`):

> *"`FAILURE` means the stores genuinely disagree and a consumer would get a
> wrong answer. `WARNING` means something is worth investigating but has a
> legitimate explanation — a record embedded but not yet stored, for example, is
> normal mid-run."*

## Can verification run without live infrastructure?

**Partially — and the split is precise.**

| Check class | Needs live infra? |
|---|---|
| Identity checks (`MALFORMED_RECORD_ID`, `SURROGATE_KEY_IDENTITY`, `DUPLICATE_RECORD_ID`) | **No** — pure computation over canonical ids |
| Cross-layer agreement (hash, model id, dimension, canonical reference) | **No**, provided the objects are supplied in memory |
| Presence of a vector / orphaned vectors / tier-state agreement | **Yes** — requires Qdrant and PostgreSQL tier state |

Evidence: `tests/erp_pipeline/verification/test_cross_store.py` (19 tests) and
`test_record_integrity.py` (35 tests) run in the full suite **without** Qdrant —
they passed in this session's run while 37 Qdrant-dependent tests skipped.
So the verification logic itself is infrastructure-free; only the
live-store checks are not.

---

# PART 25 — SOURCE TYPES MATRIX

Statuses are used strictly. Verified against `SourceType`, `connectors/`,
`discovery/`, `ingestion/`, `api_specs/`, `orchestration/planner.py` and
`orchestration/service.py:INCREMENTAL_SOURCES`.

| Source / Input | Discovery | Extraction | Transformation | Embedding | Search | Response Adaptation |
|---|---|---|---|---|---|---|
| **PostgreSQL** | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | n/a |
| **MySQL** | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | n/a |
| **SQL Server** | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | n/a |
| **MongoDB** | IMPLEMENTED (observed/inferred, bounded sample) | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | n/a |
| **CSV** | IMPLEMENTED (inferred) | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | n/a |
| **PDF** | n/a (no tabular schema) | IMPLEMENTED (text + OCR + page provenance) | PARTIAL — document path is INGEST→AI_BUILD→EMBED→TIER_ROUTE; **no TRANSFORM/VALIDATE stage** | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| **Image** | n/a | IMPLEMENTED (dimensions + OCR) | PARTIAL — same document path | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED |
| **OpenAPI 3.x** | IMPLEMENTED (spec → SourceSchema) | **CONTRACT ONLY** — never calls the API | IMPLEMENTED (mapping only) | NOT IMPLEMENTED | NOT IMPLEMENTED | n/a |
| **Swagger 2.0** | IMPLEMENTED | **CONTRACT ONLY** | IMPLEMENTED (mapping only) | NOT IMPLEMENTED | NOT IMPLEMENTED | n/a |
| **Postman collection** | IMPLEMENTED | **CONTRACT ONLY** | IMPLEMENTED (mapping only) | NOT IMPLEMENTED | NOT IMPLEMENTED | n/a |
| **JSON API response** | n/a (schema inferred per response, never catalogued) | n/a (already executed by Member 2) | IMPLEMENTED | NOT IMPLEMENTED (responses are not embedded) | NOT IMPLEMENTED | **IMPLEMENTED** |

### Notes that must not be blurred

- **SQL Server: live verification deferred.** `/v1/capabilities` appends the
  limitation *"SQL Server support is implemented but live verification remains
  deferred"* when `settings.sql_server_live_verified` is false
  (`api/routers.py:235-239`). The code exists; a live database has not confirmed
  it in this environment.
- **API specs are CONTRACT ONLY.** `SPEC_STAGES = (PARSE_SPEC, SCHEMA, MAP)` —
  the graph *terminates at mapping*. `ApiSpecUploadResponse.endpoints_called` is
  hard-coded to `0`.
- **PDF/image "transformation" is not the Phase 9 engine.** `DOCUMENT_STAGES`
  omits `TRANSFORM` and `VALIDATE`; documents become chunks and representations,
  not `CanonicalRecord`s with type conversion.
- **Incremental sync applies to the four databases only.**

---

# PART 26 — API SPECIFICATIONS

```
POST /v1/api-specs/openapi   |   POST /v1/api-specs/postman
        ↓ _parse_spec() → PostgresUploadStore
        ↓ ApiSpecService.parse(path)
        ↓ operations + request/response schemas
        ↓ SourceSchema (entities from response bodies)
        ↓ schema_cache / catalog → available to POST /v1/mappings/suggest
        ↓ STOP
```

| Stage | Implementation |
|---|---|
| Upload | `_parse_spec()` (`routers_data.py`), suffixes `.json/.yaml/.yml` |
| Parse | `ApiSpecService.parse()`; OpenAPI 3.x, Swagger 2.0, Postman collections |
| Structure inference | `infer_structure_from_examples(payloads, options, entity_hint)` — reuses the **MongoDB document inference engine**, never retains values |
| Output | `ApiSpecUploadResponse{spec_id, spec_format, schema_id, operations_count, entities_count, endpoints_called, warnings}` |
| Job type | `API_SPEC_PREPARATION`, stages `PARSE_SPEC → SCHEMA → MAP` |

## Does this component EXECUTE documented APIs?

# **NO. NEVER. THIS IS A DELIBERATE ARCHITECTURAL BOUNDARY.**

Four independent pieces of evidence:

1. **`api_specs/service.py:16`** — *"It does not call the API it just read. No
   endpoint is contacted, no token is…"*
2. **`api_specs/schema_conversion.py:298`** — *"…fetched; Phase 7 performs no
   network access."*
3. **`ApiSpecUploadResponse.endpoints_called = 0`** — hard-coded in the route,
   not computed.
4. **`/v1/capabilities` self-declares it** as the first limitation:
   *"This component parses API specifications but never calls the documented
   endpoints; runtime REST and SOAP ERP execution is out of scope."*

A search for HTTP clients in `api_specs/` returns only the word "requests"
inside an error message about the Postman *operation* limit — there is no
`requests.get`, `httpx`, or `urlopen` anywhere in the package.

## Member 2's boundary, precisely

| Concern | Owner |
|---|---|
| Reading an API contract and describing its shape | **Member 4** (`api_specs/`) |
| Turning that description into an MCP tool definition | **Member 2** |
| Choosing which operation answers a user's question | **Member 2** |
| Authenticating to the ERP and executing the call | **Member 2** |
| Transforming what came back | **Member 4** (`response_adaptation/`) |

The two Member-4 halves never touch: `api_specs/` describes an API it will never
call, and `response_adaptation/` adapts a response it never requested.

---

# PART 27 — DATABASE PERSISTENCE

**Five application-owned PostgreSQL schemas**, all created idempotently by
`python -m erp_pipeline.runtime.bootstrap` (`runtime/bootstrap.py:78 bootstrap_all`).

## `erp_catalog` — the schema catalog (`catalog/schema.py`)

| Table | Purpose | Writer | Reader | Key columns |
|---|---|---|---|---|
| `source_systems` | Registered ERP/source systems | `register_source_system` | all publishers | `source_system_id` (PK) |
| `schema_snapshots` | Versioned `SourceSchema` snapshots | `publish_schema` | `GET /v1/schemas/{id}`, drift | `schema_id`, `source_system_id` (**FK**), `schema_hash`, version |
| `source_entities` | Entities within a snapshot | `publish_schema` | mapping | `entity_id`, `schema_id` |
| `source_fields` | Fields within an entity | `publish_schema` | mapping | `field_id`, `entity_id`, `source_data_type`, `normalized_data_type` |
| `source_relationships` | FK/reference/embedded/parent-child/inferred | `publish_schema` | mapping, `GET /v1/schemas` | `from_entity`, `to_entity`, `from_fields`, `to_fields`, `confidence` |
| `mapping_profiles` | Approved source→canonical profiles | mapping service | transformation, sync | `mapping_id`, `source_schema_id`, `status` |
| `field_mappings` | Per-field target + rule | mapping service | transformation | `mapping_id`, `source_field`, `target_field` |

> The `schema_snapshots.source_system_id` **foreign key** is why every file
> upload must register its synthetic source system first — the bug fixed in
> commit `7d5504a`.

## `erp_sync` — incremental synchronization (`sync/state.py:44`)

| Table | Purpose | Writer | Reader | Keys |
|---|---|---|---|---|
| `sync_state` | Watermark, last record key, run id, status, schema id/hash, mapping id, engine version, **version** (optimistic lock) | `SyncCoordinator` | `SyncService`, drift | `(source, entity)`, `version` |

## `erp_vector_storage` — tier state (`storage/state.py:52`)

| Table | Purpose | Writer | Reader | Keys |
|---|---|---|---|---|
| `vector_storage_state` | **Authoritative** tier + metadata per representation | `HybridVectorStore`, migration | `POST /v1/search`, verification | `representation_id` (PK), `canonical_record_id`, `entity_type`, `source_system_id`, `source_entity`, `sensitivity`, `document_id`, `content_hash`, `model_id`, `tier` |
| `vector_tier_transitions` | Audit trail of every move, with `TransitionReason` | migration/router | operations | `representation_id`, `from_tier`, `to_tier`, `reason` |
| `vector_access_stats` | Read counts / last access, feeding the routing factors | search path | router | `representation_id` |

## `erp_orchestration` — jobs (`orchestration/job_store.py:36`)

| Table | Purpose | Writer | Reader | Keys |
|---|---|---|---|---|
| `jobs` | Durable job records, idempotency keys, counters | `PostgresJobStore` | `GET /v1/jobs`, `/{id}` | `job_id` (PK), idempotency key |
| `job_stages` | Per-stage status/history | `PostgresJobStore` | `GET /v1/jobs/{id}` | `job_id`, stage |

## `erp_runtime` — runtime persistence (`runtime/persistence.py:43`, `orchestration/record_store.py:25`)

| Table | Purpose | Writer | Reader | Keys |
|---|---|---|---|---|
| `registered_sources` | Sources created via `POST /v1/sources` | source router | discovery, jobs | `source_id` (PK), `credential_ref` (**never a password**) |
| `uploads` | Uploaded file metadata | `PostgresUploadStore` | ingestion | `upload_id` (PK), `content_hash` |
| `mapping_drafts` | Unapproved mapping drafts | mapping router | `PUT /v1/mappings/{id}` | `mapping_id`, `schema_id`, `status`, `ambiguous_fields` |
| `canonical_records` | Canonical records | `PostgresRecordStore` | `GET /v1/records/{id}` | `record_id` (PK) |

## Persistence diagram

```mermaid
erDiagram
    source_systems      ||--o{ schema_snapshots  : "FK source_system_id"
    schema_snapshots    ||--o{ source_entities   : contains
    source_entities     ||--o{ source_fields     : contains
    schema_snapshots    ||--o{ source_relationships : declares
    schema_snapshots    ||--o{ mapping_profiles  : "source_schema_id"
    mapping_profiles    ||--o{ field_mappings    : contains
    schema_snapshots    ||--o{ sync_state        : "schema_id + schema_hash"
    canonical_records   ||--o| vector_storage_state : "canonical_record_id"
    vector_storage_state ||--o{ vector_tier_transitions : audits
    vector_storage_state ||--o| vector_access_stats    : counts
    jobs                ||--o{ job_stages        : contains
    registered_sources  ||--o{ jobs              : "source_id"
    uploads             ||--o{ schema_snapshots  : "produces (files)"
    mapping_drafts      ||--o| mapping_profiles  : "becomes on approval"
```

---

# PART 28 — QDRANT

| Aspect | Reality |
|---|---|
| **Collections** | One per online tier. HOT: no quantization, not on disk. WARM: `on_disk=True` + `ScalarQuantization(INT8)`. Benchmark used isolated `erp_phase12_bench_*` collections |
| **HOT role** | Full-precision float32 vectors in RAM — lowest latency, highest resource cost |
| **WARM role** | int8-quantized, disk-resident — lower footprint, some retrieval trade-off. `quantization_verified()` returns True **only when the server itself reports a scalar quantizer** |
| **Payload** | Identity/provenance facts that mirror `StorageRecordMetadata` — this is what lets server-side filters work |
| **Vector ID** | `make_deterministic_uuid(record_id)` = UUIDv5 over `NAMESPACE_URL`. Same record → same UUID → **upsert updates in place**; changed content → same UUID, new payload |
| **Canonical reference** | Carried in the payload and, authoritatively, in `vector_storage_state.canonical_record_id` |
| **Filtering fields** | `entity_type`, `source_system_id`, `source_entity`, `sensitivity`, `document_id` — pushed into Qdrant as a server-side `Filter` |
| **Sensitivity** | Present as a filterable payload field; also drives tier constraints in PostgreSQL-side policy |
| **Search** | `client.search(collection, vector, limit, filter)` with **`with_payload=False`** (`hot_tier.py:195`) |
| **Migration** | `storage/migration.py` moves a vector between collections, **re-checking policy first** and refusing a prohibited destination |

## What Qdrant IS authoritative for

- The vector itself and its approximate-nearest-neighbour ranking.
- Server-side filtering during retrieval.

## What Qdrant is NOT authoritative for

- **Tier placement** — `erp_vector_storage.vector_storage_state` is authoritative.
- **Canonical record identity** — carried in PostgreSQL tier state; the search
  route reads it from `services.storage.state.load(...)`, **not** from the
  Qdrant payload (`with_payload=False`).
- **Content hash, model id, sensitivity** for the API response — all read from
  tier state.
- **Access statistics** — `vector_access_stats`.

> This separation is deliberate: if Qdrant is rebuilt or a collection is
> recreated, the authoritative record of *what should be where* survives in
> PostgreSQL. Verification (`ORPHANED_VECTOR`, `TIER_METADATA_MISMATCH`) exists
> precisely to detect when the two disagree.

## What happens if Qdrant is down?

- `GET /v1/health/live` still returns 200 — *"If liveness depended on Qdrant, an
  outage in a vector database would get the API process killed and restarted —
  which fixes nothing and loses in-flight jobs"* (`api/routers.py:74-79`).
- `GET /v1/health/ready` reports the dependency as unhealthy.
- `POST /v1/search` raises `DependencyUnavailableError` → **HTTP 503**.
- **`POST /v1/responses/adapt` is unaffected** — Phase 14 needs no vector store.
- Uploads, schema inference, mapping and transformation are unaffected.

---

# PART 29 — COLD STORAGE

`src/erp_pipeline/storage/cold_tier.py`.

```
EmbeddingRecord
   ↓ serialize        vector + metadata → JSON
   ↓ compress         gzip level 9
   ↓ encrypt          AES-256-GCM, random 96-bit nonce per write
   ↓ write            header-length | header JSON | nonce | ciphertext
ColdArchiveTier (one file per record)
```

| Aspect | Implementation |
|---|---|
| Format version | `COLD_FORMAT_VERSION = "1.0"` in the plaintext header |
| Compression | `gzip.compress(plaintext, compresslevel=9)` |
| Encryption | `AESGCM(key).encrypt(nonce, compressed, None)` — `cryptography` library, **not home-grown** |
| Nonce | `os.urandom(12)` — **96 bits, fresh per write** |
| On-disk layout | `len(header).to_bytes(4,"big") + header + nonce + ciphertext` |
| Truncation detection | `ColdArchiveIntegrityError("archive is truncated: header or nonce")` |
| Safe logging | `to_dict()` emits header + sizes only — *"Never nonce or ciphertext"* |
| Key management | `StaticKeyProvider`, `generate_key()`; the key is **not stored in the archive** |

## Why the nonce is random, not derived — a genuine security decision

From the module docstring:

> *"Deriving the nonce from the record id or the content hash would make
> ciphertext reproducible, which is tempting for tests and **catastrophic in
> GCM: nonce reuse** …"*

This is the correct call and is worth stating in a viva: GCM nonce reuse under
the same key breaks both confidentiality and authenticity.

Also noted honestly in the docstring: an encrypted payload is *"high-entropy
bytes that gzip cannot shrink at all"* — which is exactly why compression
happens **before** encryption, not after.

## Content hash and integrity

The GCM **authentication tag** makes tampering detectable on decrypt. The
content hash travels in the header/metadata so a rehydrated record can be
checked against the canonical layer (`CONTENT_HASH_MISMATCH` in verification).

## Rehydration and `include_cold`

COLD is **not searchable in place**. `POST /v1/search` with
`include_cold: true`:

1. Filters are applied to **tier-state metadata first** — *"a filtered-out
   archive is never decrypted at all"*.
2. Surviving archives are read, decrypted, decompressed, deserialized.
3. They are loaded into an **isolated temporary Qdrant collection**.
4. The same query runs against it.
5. The response sets `deep_search_used: true` and carries a note: *"archived
   vectors were rehydrated into a temporary index to answer this query; this
   costs materially more than a hot or warm search"*.

**Off by default** — `SearchRequest.include_cold = False`, *"cold search
rehydrates archives and is expensive"*.

### Measured rehydration cost (from `artifacts/tiered_storage_benchmark.json`)

| Measurement | Value |
|---|---|
| Archive bytes read (500 records) | 2,322,058 |
| Archive read time | 103.06 ms |
| Decrypt + decompress + deserialize (DERIVED) | 255.88 ms |
| Full rehydrate | 358.95 ms |
| **Temporary index population** | **8,976.74 ms** |
| **Rehydration total** | **9,335.69 ms** |
| Per record | 18.67 ms |
| Vector round-trip fidelity | **lossless**, max component deviation `0.0` |

**The dominant cost is index population (96%), not cryptography.** That is a
non-obvious, measured finding, and the artifact explicitly states the derived
measurement's method rather than presenting it as directly timed.

---

# PART 30 — IDENTITY SYSTEM

| ID | Grammar / derivation | Produced by |
|---|---|---|
| `source_system_id` | operator-supplied or `file_source` for uploads | registration / ingestion |
| `schema_id` | `{source_system_id}.{...}` + content hash | discovery / inference |
| `mapping_id` | mapping profile identity | `MappingService` |
| **`record_id` / `canonical_record_id`** | **`erp:{source_system_id}:{entity_type}:{stable_source_key}`** | `make_canonical_record_id()` |
| `document_id` | `erp:{sys}:document:{content-derived id}` | `make_canonical_document_id()` |
| **`representation_id`** | **`ai:{entity_type}:{normalized_key}`** | `make_representation_id()` |
| `embedding_id` | derived from `representation_id` **+** `model_id` | `make_embedding_id()` |
| **`vector_id`** | **UUIDv5** over `NAMESPACE_URL` + `erp-vector/{record_id}` | `make_deterministic_uuid()` |
| `file_id` | `file:{sha256}` | `make_file_id()` |
| `job_id` | job store | `PostgresJobStore` |
| `upload_id` | upload store | `PostgresUploadStore` |
| `case_record_id` | case identity | `make_case_record_id()` |

## Relationships

```mermaid
flowchart LR
    SS["source_system_id<br/>finance_erp"] --> SCH["schema_id"]
    SCH --> MAP["mapping_id"]
    SS --> CR["canonical_record_id<br/>erp:finance_erp:invoice:inv-204"]
    CR --> REP["representation_id<br/>ai:invoice:erp_finance_erp_invoice_inv-204"]
    REP --> EMB["embedding_id<br/>representation + model"]
    CR --> VEC["vector_id<br/>UUIDv5"]
    CR -.->|"carried, never parsed back"| REP
    VEC --> QD["Qdrant point id"]
    CR --> PG["vector_storage_state.canonical_record_id"]
```

## Why the three are NOT interchangeable — the critical point

```
canonical_record_id   erp:finance_erp:invoice:inv-204          the business record
representation_id     ai:invoice:erp_finance_erp_invoice_inv-204   its AI projection
vector_id             3f2504e0-4f89-51d3-9a0c-0305e82c3301     its point id in Qdrant
```

1. **They name different things.** One record can have several representations
   (different `RepresentationConfig`), and one representation has one vector per
   model. Collapsing them would make "re-embed with a new model" indistinguishable
   from "this is a different record".

2. **`erp:` and `ai:` prefixes prevent collision** — *"a representation and the
   record it projects can coexist in one store without collision."*

3. **THE LOSSY-NORMALIZATION RULE.** `normalize_identifier` replaces `:` with
   `_`. So the canonical id embedded inside a representation id is
   **irreversibly flattened**:

   ```
   erp:finance_erp:invoice:inv-204   →   erp_finance_erp_invoice_inv-204
   ```

   You **cannot** recover the canonical id by parsing the representation id —
   the separators are gone and the boundaries are ambiguous. This is why the
   canonical id is **carried forward explicitly** in
   `AIRepresentation.metadata["canonical_record_id"]` and in
   `vector_storage_state.canonical_record_id`, and why the search route comment
   says: *"Carried forward from storage state, never reconstructed."*

   This was one of the five defects fixed in the integration-stabilization task.

4. **`vector_id` is a projection for one consumer.** *"The human-readable
   `record_id` remains the authoritative identity. This value is a derived
   projection for a specific consumer, and nothing should resolve a record by it
   alone."*

## The surrogate-key refusal

`require_business_key()` + `looks_like_surrogate_key()` refuse a **digits-only**
key, so a PostgreSQL `SERIAL` or a row offset can never become identity — *"it
must never be… any value that changes when the source is reloaded."* The
narrow, digits-only rule is deliberate: an earlier, broader regex wrongly
flagged legitimate business keys like `INV-001` and `cus-44`.

---

# PART 31 — SECURITY

## IMPLEMENTED SECURITY CONTROLS

| Control | Implementation | Evidence |
|---|---|---|
| **API key** | `X-API-Key`; required for every mutating method and for reads when `protect_reads`. Public paths: `/v1/health/live`, `/v1/health/ready`, `/docs`, `/redoc`, `/openapi.json`. **Verified: `requires_key("POST","/v1/responses/adapt",False) → True`** | `api/security.py:53-60` |
| **Constant-time key comparison** | `keys_match()` | `api/security.py` |
| **Credential redaction** | `redact()` → `"<redacted>"` / `"<absent>"` in logs | `api/security.py:63-65` |
| **Secrets never stored on sources** | `credential_ref` names a secret; `SecretStr` keeps a password out of `repr` and out of the OpenAPI document | `api/schemas.py` header |
| **Error messages never leak internals** | Unmapped exceptions return only `{"exception_type": …}` — *"a connection string, a row value or a file path"* is never echoed | `api/responses.py:111-118` |
| **Sensitivity** | Propagated end-to-end: canonical → representation metadata → tier state → Qdrant payload → routing constraints → Phase 14 output policy |
| **Storage constraints** | `prohibited_tiers()` removes non-compliant tiers **before** scoring | `storage/vector_router.py:113` |
| **AES-256-GCM** | Cold archive, random 96-bit nonce per write, authenticated | `storage/cold_tier.py` |
| **SSRF protection** | 14 controls (Part 16), fetching **off by default**, no HTTP client shipped | `response_adaptation/assets.py` |
| **File size limits** | `UploadTooLargeError` → HTTP 413; `AssetOptions.max_bytes` 12 MB | `runtime/`, `assets.py` |
| **MIME / magic bytes** | Content decides, never the filename or the declared type; mismatch reported | `ingestion/detection.py`, `response_adaptation/detector.py` |
| **Decompression-bomb defence** | `Image.open` header parse **before** pixel decode; `max_pixels` | `ingestion/image_ingestion.py:79-107` |
| **OCR constraints** | `max_text_chars`, `ocr_min_text_chars`, `max_pages`; OCR absence degrades to `ocr_unavailable` rather than failing | `ingestion/models.py:221-256` |
| **Header allow-list** | Provenance keeps only `content-type, content-length, date, etag, last-modified`. *"A deny-list has to anticipate every header that might carry a secret and gets it wrong the first time an ERP invents `X-Vendor-Session`."* | `response_adaptation/models.py`, `service.py::_allowed_headers` |
| **Input validation** | Pydantic bounds on every request field; unknown search filters **refused** with 422, never ignored | `api/schemas.py`, `storage/filters.py` |
| **SQL identifier validation** | `_validate_schema()` guards every `CREATE SCHEMA`/`CREATE TABLE` interpolation | `storage/state.py`, `orchestration/job_store.py`, `runtime/persistence.py` |
| **No data echo** | Upload endpoints return metadata only; search returns no vectors; records return business values only |
| **CORS** | Explicit origin list via `ERP_API_CORS_ORIGINS` | `api/main.py` |
| **Request id** | `X-Request-ID` on every response, included in error bodies | `api/main.py` |

### XXE

`api_specs/` parses JSON and YAML. **YAML is parsed with PyYAML** — the safe
loader must be used to avoid arbitrary object construction. XML/SOAP is
**NOT IMPLEMENTED**, so the classic XXE vector (XML external entities) does not
apply to any current code path.

## FUTURE / EXTERNAL CONTROLS

| Control | Status |
|---|---|
| **Authorization / access control per user or role** | **EXTERNAL — Member 1.** This component authenticates a *caller*, not a *user*, and makes no authorization decision |
| **Automatic PII / sensitivity detection** | **NOT IMPLEMENTED — deliberately.** Sensitivity is **consumed, never inferred**: *"guessing would produce a classification nothing else in the pipeline agrees with"* |
| Frontend API-key entry | **NOT IMPLEMENTED** — `client.ts` sends no key header |
| Rate limiting | **NOT IMPLEMENTED** |
| Audit log of who read what | **NOT IMPLEMENTED** (tier transitions are audited; reads are counted, not attributed) |
| TLS termination | Deployment concern, outside the application |
| Key rotation for cold archives | `StaticKeyProvider` only; rotation is **NOT IMPLEMENTED** |
| Secret manager integration | `EnvironmentSecretProvider` is **read-only**; `POST /v1/sources` can only place a password into a provider exposing `put` |

---

# PART 32 — MEMBER 1 INTEGRATION (GOVERNANCE)

**Status: EXTERNAL INTEGRATION — NOT PRESENT IN THIS REPOSITORY.**
No governance, authorization, policy-decision-point or consent code exists here.
This section describes *what could be offered*, using **only interfaces that
already exist**.

## Member 4 → Member 1 (what this repository can already provide)

| Governance need | Existing interface | Endpoint / field |
|---|---|---|
| What sources exist | `SourceResponse` | `GET /v1/sources` |
| What data a source holds | `SchemaResponse` with entities, fields, relationships | `GET /v1/schemas/{schema_id}` |
| **Sensitivity of a record** | `sensitivity` on tier state, search hits, and adaptation provenance | `POST /v1/search` hit metadata; `AdaptationProvenance.sensitivity` |
| **Provenance** | source system, source entity, endpoint, content hash, config fingerprint, `adapted_at` | `AdaptationProvenance`, `RecordResponse` |
| **Record identity** | `canonical_record_id` — stable, deterministic, resolvable | search hits → `GET /v1/records/{id}` |
| Which operations exist on an ERP API | parsed spec operations | `POST /v1/api-specs/*` result |
| What the component will and will not do | self-declared `limitations` | `GET /v1/capabilities` |
| Field-level decision trail | per-field signals and reasons | `AdaptationReport.field_decisions` |

## Member 1 → Member 4 (what would have to be supplied)

| Input | Where it would land | Exists today? |
|---|---|---|
| Sensitivity classification per entity/field | `CanonicalRecord.sensitivity`, `ResponseEnvelope.sensitivity` | **Field exists; no ingestion API for a classification decision** |
| Blocked sensitivities for a caller | `AdaptationPolicy.blocked_sensitivities` | **Exists as configuration; not per-request-caller** |
| Blocked field names | `AdaptationPolicy.blocked_fields` | **Exists as configuration** |
| Tier location policy for restricted data | `StoragePolicy.requires_on_premises` + `DEFAULT_TIER_LOCATIONS` | **Exists; all tiers currently on-premises** |
| An authorization verdict before adaptation | — | **NOT IMPLEMENTED** — no hook |

## Suggested contract (proposal only — not implemented)

```
Member 1 → Member 4 :  {caller_id, permitted_sensitivities[], blocked_fields[]}
                       carried per-request into AdaptationPolicy
Member 4 → Member 1 :  {canonical_record_id, sensitivity, source_system_id,
                        source_entity, content_hash, config_fingerprint,
                        field_decisions[]}
```

The second direction needs **no new code** — every field already exists on
`AdaptationProvenance` and `AdaptationReport`. The first direction would need a
per-request policy override, which today is deployment-level configuration.

---

# PART 33 — MEMBER 2 INTEGRATION (MCP / ERP EXECUTION)

**The most important integration boundary in the project.**

## A. Schema / API metadata integration

What Member 2 can obtain to build MCP tool definitions:

| Need | Source | Endpoint |
|---|---|---|
| Which entities exist and their fields/types | `SchemaResponse` → `SchemaEntityResponse` → `SchemaFieldResponse` (`source_data_type` **and** `normalized_data_type`) | `GET /v1/schemas/{schema_id}` |
| Relationships between entities | `SchemaRelationshipResponse` — `from_entity`, `to_entity`, `from_fields`, `to_fields`, `type`, `confidence` | same |
| API operations and their request/response shapes | `ApiSpecUploadResponse` + the derived `SourceSchema` | `POST /v1/api-specs/openapi` \| `/postman` |
| Source metadata | `SourceResponse` | `GET /v1/sources` |
| Canonical vocabulary for tool parameter naming | `MappingResponse` field decisions | `POST /v1/mappings/suggest` |
| What this component supports | `CapabilitiesResponse` | `GET /v1/capabilities` |

> **The relationship contract was a defect that is now fixed.** During
> integration stabilization, the response model initially invented
> `source_entity`/`target_entity`; the real `SourceRelationship` contract uses
> `from_entity`/`to_entity`/`from_fields`/`to_fields`/`confidence`. Member 2
> should code against the **generated OpenAPI**
> (`artifacts/openapi_contract_snapshot.json`), which is regenerated from the live
> application by a contract test.

## B. Runtime response integration

```
Member 2 executes the ERP API  →  POST /v1/responses/adapt  →  llm_ready
```

### Request contract

| Field | Required | Meaning |
|---|---|---|
| `query` | recommended | The user's question. **Omitted ⇒ no field is dropped for irrelevance** |
| `source_system_id` | recommended | Which ERP; used for the canonical record id |
| `endpoint` | recommended | Entity hint + provenance |
| `http_status` | optional | Recorded |
| `content_type` | optional | The server's claim; **bytes still outrank it** |
| `body` | one of | The decoded JSON body |
| `body_base64` | one of | Bytes, for an image or PDF response |
| `headers` | optional | **Allow-listed before storage; secrets are dropped** |
| `asset_urls` | optional | **Never fetched unless the deployment enables it** |
| `entity_hint` | optional | Overrides the endpoint-derived entity name |
| `sensitivity` | optional | **Consumed, never inferred** |
| `options` | optional | Per-request budgets and the ablation switches |

### Member 2 MUST

- Send the response **exactly as received, including the envelope** — unwrapping
  is Member 4's job and doing it early loses the `wrapper_path`.
- Send `query` when field selection is wanted.
- Use `body_base64` for image/PDF responses.
- Check **`partial` and `warnings`**, not just `success`.

### Member 2 MUST NOT

- Expect this service to call an ERP system.
- Send credentials in `headers` expecting them to be used — **they are dropped**.
- Expect an asset URL to be fetched unless fetching is enabled **and** a fetcher
  is configured.
- Assume `entity_type` will be non-null — it is `None` whenever the canonical
  model has no vocabulary for the response (as in the E002 employee case).

### Response guarantees

- **HTTP 200 even on partial success.** A refused asset URL or a truncating
  budget is reported in `warnings`/`partial`, not as an HTTP error, *because the
  fields that did adapt are still the answer the caller needs*.
- **HTTP 422** means the request itself could not be interpreted.
- `llm_ready` is JSON-safe (Decimals rendered as exact strings).
- `report.field_decisions` explains every inclusion **and every removal**.

## The boundary, stated once more

# **Member 4 DOES NOT execute the ERP API.**

Enforced by absence (no HTTP client in any source-facing package), by
documentation (`api_specs/service.py:16`), by the hard-coded
`endpoints_called = 0`, and by `/v1/capabilities` advertising it as a
limitation.

---

# PART 34 — MEMBER 3 INTEGRATION (ANSWER GENERATION)

**Status: EXTERNAL INTEGRATION — NOT PRESENT IN THIS REPOSITORY.** No prompt
assembly, model invocation, or answer-generation code exists here. `/v1/search`
explicitly states: *"No LLM and no generated answer."*

## Currently exposed over HTTP

| Consumable | Interface | Endpoint |
|---|---|---|
| **LLM-ready adapted response** | `AdaptedResponse.llm_ready` | `POST /v1/responses/adapt` |
| Retrieved ERP knowledge | `SearchResponse.hits` | `POST /v1/search` |
| Full canonical record behind a hit | `RecordResponse.data` | `GET /v1/records/{id}` |
| Traceability for citation | `AdaptationProvenance`, hit `metadata` | both |
| Explanation of what was dropped | `AdaptationReport.field_decisions`, `removed_by_reason` | adapt |
| Asset text (OCR/PDF) | `AdaptedAsset.text` + `llm_directly_readable` | adapt |

## Requires additional integration (currently Python-only)

| Consumable | Where it lives | Why it is not exposed |
|---|---|---|
| `ProcessCase.current_state` | `process/models.py` | **No endpoint and no `JobType`** |
| `ProcessCase.allowed_next_states` | `process/models.py` | same |
| `ProcessCase.activity_sequence` | `process/models.py` | same |
| `ProcessCase.entity_references` | `process/models.py` | same |
| `ProcessModel` directly-follows graph | `process/case_builder.py:280` | same |
| Document chunks with page anchors | `ai/chunking.py` | no chunk-retrieval endpoint |
| Cross-store integrity status | `verification/` | no endpoint |

> **This is the clearest gap for group integration.** The process/case
> capability is fully implemented and tested but has **no HTTP surface at all** —
> Member 3 cannot reach `current_state` or `allowed_next_states` without a new
> endpoint or an in-process import.

---

# PART 35 — COMPLETE SCENARIO MATRIX

| Scenario | Start | Main modules | End result | Fully implemented? | External dependency? |
|---|---|---|---|---|---|
| **Upload ERP CSV** | Frontend drop box | `api`, `runtime`, `ingestion`, `catalog` | Schema inferred + published; **stops there** | **COMPLETE** | PostgreSQL (else in-process only, warned) |
| **Register + discover a database** | `POST /v1/sources` | `connectors`, `discovery`, `catalog` | `SourceSchema` in catalog | **COMPLETE — BACKEND ONLY** | Live DB |
| **Suggest a mapping** | `POST /v1/mappings/suggest` | `mapping` | `MappingProfile` + per-field decisions | **COMPLETE — BACKEND ONLY** | None |
| **Human mapping review** | `PUT /v1/mappings/{id}` | `mapping`, `runtime` | Approved profile | **COMPLETE — BACKEND ONLY** | None |
| **Transform uploaded ERP records** | `POST /v1/jobs` (STRUCTURED_PIPELINE) | `orchestration`, `transformation` | `CanonicalRecord`s | **COMPLETE — BACKEND ONLY** | PostgreSQL |
| **Embed transformed records** | same job (AI_BUILD, EMBED) | `ai` | `EmbeddingRecord`s | **COMPLETE — BACKEND ONLY** | Model files (local) |
| **Route to tiers** | same job (TIER_ROUTE) | `storage` | `RoutingDecision` + stored vector | **COMPLETE — BACKEND ONLY** | Qdrant |
| **Search invoice data** | `POST /v1/search` | `ai`, `storage` | Ranked hits with canonical ids | **COMPLETE — BACKEND ONLY** | Qdrant + PostgreSQL |
| **Resolve a search result** | `GET /v1/records/{id}` | `orchestration` | Canonical record | **COMPLETE — BACKEND ONLY** | PostgreSQL |
| **Upload PDF** | Frontend drop box | `ingestion`, `ai` | Extracted text + page provenance (metadata returned) | **COMPLETE** | PyMuPDF; OCR needs Tesseract |
| **Upload image** | Frontend drop box | `ingestion`, `ai` | Dimensions + OCR text (metadata returned) | **COMPLETE** | Pillow; OCR needs Tesseract |
| **E002 birth certificate — JSON** | Member 2 response | `response_adaptation` | LLM-ready JSON (**passthrough path — `entity_type=None`**) | **COMPLETE — BACKEND ONLY** | **Member 2** |
| **E002 birth certificate — PDF** | Member 2 response | `response_adaptation`, `ingestion`, `ai` | LLM-ready document text + page range | **COMPLETE — BACKEND ONLY** | **Member 2**; OCR for scans |
| **E002 birth certificate — image** | Member 2 response | `response_adaptation`, `ingestion` | Image asset + dimensions + OCR | **COMPLETE — BACKEND ONLY** | **Member 2**; Tesseract for OCR |
| **E002 certificate — URL** | Member 2 response | `response_adaptation` | Refusal placeholder (default) or fetched asset | **COMPLETE — BACKEND ONLY** | **Member 2** + an injected fetcher |
| **Unsupported binary** | Member 2 response | `response_adaptation` | `unsupported_binary` asset, `success=true` | **COMPLETE — BACKEND ONLY** | **Member 2** |
| **Incremental ERP update** | Source change | `sync`, `transformation`, `ai`, `storage` | Updated record + vector, safe checkpoint | **COMPLETE — BACKEND ONLY** | Live DB + Qdrant |
| **Schema drift** | Changed source schema | `sync/drift`, `sync/impact` | `DriftReport`; unsafe mappings quarantined | **COMPLETE — BACKEND ONLY** | Live DB |
| **Process case** | Event log rows | `process` | `ProcessCase` + `ProcessModel` | **BACKEND ONLY — NO JobType, NO ENDPOINT** | None (demo script) |
| **Cross-store verification** | Stored artefacts | `verification` | `IntegrityReport` (18 codes) | **BACKEND ONLY — NO ENDPOINT** | Live stores for some checks |

---

# PART 36 — WHAT CAN BE DEMONSTRATED RIGHT NOW?

## DEMO READY — no infrastructure at all

| Demonstration | Command |
|---|---|
| **Phase 14 response adaptation (the research contribution)** | `.venv/Scripts/python.exe scripts/evaluate_response_adaptation.py` |
| **Phase 14 targeted test suite (106 tests)** | `.venv/Scripts/python.exe -m pytest tests/erp_pipeline/response_adaptation/ -q` |
| **Mapping benchmark (measured research result)** | `.venv/Scripts/python.exe -m pytest tests/erp_pipeline/mapping/test_mapping_benchmark.py -k reported -s` |
| **Adapt one response in Python** | see snippet below |
| **Storage routing decisions (pure computation)** | `.venv/Scripts/python.exe -m pytest tests/erp_pipeline/storage -q -k "policy or router or sensitivity"` |
| **Cross-store verification logic** | `.venv/Scripts/python.exe -m pytest tests/erp_pipeline/verification -q` |
| **Process/case modelling** | `.venv/Scripts/python.exe -m pytest tests/erp_pipeline/process -q` |
| **SSRF refusal matrix** | `.venv/Scripts/python.exe -m pytest tests/erp_pipeline/response_adaptation/test_assets_and_url_safety.py -q` |

```bash
.venv/Scripts/python.exe -c "
import sys, json; sys.path.insert(0,'src')
from erp_pipeline.response_adaptation import ResponseAdaptationService, ResponseEnvelope
svc = ResponseAdaptationService()
r = svc.adapt(ResponseEnvelope(
    query='How much is invoice INV-204 for and in what currency?',
    source_system_id='finance_erp', endpoint='/api/invoices/INV-204',
    content_type='application/json',
    body={'result':{'inv_no':'INV-204','cust_ref':'CUS-17','total_amt':'45000.00',
                    'curr':'LKR','approval_status':'A','row_version':7},'success':True}))
print(json.dumps(r.llm_ready, indent=2))
print(json.dumps(r.transformation.to_dict(), indent=2))
"
```

## DEMO READY WITH LOCAL INFRASTRUCTURE

| Demonstration | Needs | Command |
|---|---|---|
| **API server + Swagger UI** | PostgreSQL | `.venv/Scripts/python.exe -m erp_pipeline.api` then open `http://127.0.0.1:8000/docs` |
| **Frontend CSV/PDF upload** | API + PostgreSQL | `cd frontend && npm run dev` |
| **Schema bootstrap** | PostgreSQL | `.venv/Scripts/python.exe -m erp_pipeline.runtime.bootstrap` |
| **Database discovery** | PostgreSQL/MySQL/MongoDB | `POST /v1/sources` → `POST /v1/sources/{id}/discover` |
| **Semantic search** | Qdrant + PostgreSQL | `POST /v1/search` |
| **Phase 12 storage benchmark** | Qdrant | `.venv/Scripts/python.exe scripts/benchmark_tiered_storage.py` |
| **Full test suite** | optional (skips gracefully) | `.venv/Scripts/python.exe -m pytest -q` |

> In this environment, MongoDB (`localhost:27018`) and Qdrant (`localhost:6333`)
> were **not running**: 63 tests skipped, 0 failed. Starting both recovers ~61.

## BACKEND ONLY — NO FRONTEND UI

Source registration · discovery · schema view · mapping suggest/review/validate ·
job creation and monitoring · search · record resolution ·
**response adaptation** · API-spec upload.

## DEMO/EXAMPLE ONLY

| Demonstration | Command |
|---|---|
| BPI 2020 dataset over the generic framework | `.venv/Scripts/python.exe scripts/demos/run_bpi2020_demo.py` |

## REQUIRES MEMBER 2

An **end-to-end live** birth-certificate flow. Member 4's half is fully
demonstrable by posting a **recorded/simulated** ERP response to
`POST /v1/responses/adapt` — which is exactly how it should be demonstrated,
because Member 4 never calls the ERP.

## NOT IMPLEMENTED

Storage/tier monitoring endpoint · process `JobType` or endpoint · verification
endpoint · chunk-retrieval endpoint · frontend for anything beyond upload ·
API-key entry in the browser · SOAP/WSDL · DOCX/Excel/HTML/email/audio/video.

---

# PART 37 — RECOMMENDED FINAL DEMO FLOW (5–10 minutes)

Designed around **what actually runs**, and around Part 12's finding that the
brief's headline query is the one that reduces least.

## Pre-flight (before the panel arrives)

```bash
.venv/Scripts/python.exe -m erp_pipeline.runtime.bootstrap   # if PostgreSQL is up
.venv/Scripts/python.exe -m erp_pipeline.api                 # terminal 1
cd frontend && npm run dev                                   # terminal 2
```

If infrastructure is unavailable, **skip Demo 1 and lead with Demo 2** — it
needs nothing.

## Demo 1 — ERP data preparation (≈3 min)

| Step | Action | What to say |
|---|---|---|
| 1 | Drop `invoices.csv` on the frontend | "The schema is inferred from content, not from the filename extension." |
| 2 | `GET /v1/schemas/{schema_id}` in `/docs` | "Fields carry both the source type and the normalized type." |
| 3 | `POST /v1/mappings/suggest` | **"Every field shows its candidates, its four evidence components and its confidence — and ambiguous fields are refused, not guessed."** |
| 4 | Point at one `UNMAPPED` field | "The engine refuses rather than inventing. That refusal rate is measured: 1.0 over 8 negative labels." |
| 5 | *(optional, if Qdrant is up)* `POST /v1/search` | "Retrieval returns records, never prose — there is no LLM in this component." |

> **Name the CSV after the entity** (`invoices.csv`), because entity naming
> comes from the filename.

## Demo 2 — real-time birth-certificate request (≈4 min) — **THE CORE**

| Step | Action | What to say |
|---|---|---|
| 1 | Show the raw ERP JSON | "Member 2 has already called the ERP. This is what came back — envelope, vendor names, a row version." |
| 2 | `POST /v1/responses/adapt` with **`"What is E002's date of birth?"`** | **Use this query, not "find details" — it gives 9→2 fields and 77.7% context reduction.** |
| 3 | Show `llm_ready` | "Two fields. The record key is preserved so the answer stays traceable." |
| 4 | Show `report.field_decisions` | **"Every removal has a reason and four signals. Nothing is a black box, and no LLM was involved."** |
| 5 | Show `transformation` | "Reduction ratios are derived from measured bytes — a caller cannot report a reduction that did not happen." |
| 6 | Re-run with an **invoice** payload | "Here `entity_type` is `invoice` and the keys are canonical — `inv_no` became `invoice_id`. That is the ERP-awareness." |
| 7 | Show `provenance.headers` | "The `Authorization` header we sent is gone. Headers are allow-listed, never copied." |

## Demo 3 — multimodal + safety (≈2 min)

| Step | Action | What to say |
|---|---|---|
| 1 | POST a base64 PDF | "Detected by magic bytes; extracted through the same PDF ingestion the file pipeline uses." |
| 2 | POST a base64 image | "`llm_directly_readable: true` — the OCR text rides alongside, not instead of, the image." |
| 3 | POST a ZIP | "`unsupported_binary`, `success: true`. A truthful description beats a hallucination-inviting silence." |
| 4 | POST with `asset_urls: ["https://169.254.169.254/…"]` | **"Refused: `private_or_reserved_address`. Fetching is off by default and this package ships no HTTP client."** |
| 5 | Show that `llm_ready` survived | "Partial success. A blocked image never discards the JSON that adapted." |

## If asked "show me the research result"

```bash
.venv/Scripts/python.exe scripts/evaluate_response_adaptation.py
```

Then say the honest headline out loud before they read it:
**"Recall 0.9799 — lower than both baselines, which get 1.0 by not making a
decision. We buy 61% noise removal and half the context for 2% recall, and the
three misses are named in the artifact."**

---

# PART 38 — RESEARCH VS ORDINARY ENGINEERING

## Ordinary engineering (competent, but not novel)

FastAPI routing and the error envelope · multipart upload handling · SQLAlchemy
Core repositories · PostgreSQL schema bootstrap · Qdrant client integration ·
PyMuPDF / Pillow / pytesseract wiring · gzip + AES-GCM file writing (standard
library + `cryptography`) · sentence-transformers invocation · the React upload
page · job queue and stage execution · CSV parsing · YAML/JSON spec parsing ·
connection pooling · request ids and API-key auth.

**All necessary. None of it is a contribution.** Do not present these as novel.

## Research mechanisms — three, ranked by evidential strength

### 1. Explainable ERP schema mapping (strongest evidence)

**What it is.** Source fields are matched to a canonical ERP model by four
weighted evidence components (`name` 0.50, `type` 0.20, `entity` 0.20, `path`
0.10) with an explicit alias registry, a type veto, confidence thresholds and an
**ambiguity refusal**.

**Why it is more than string matching.** It (a) reports *why* each mapping was
chosen with per-candidate evidence, (b) **refuses** rather than guessing when the
top candidates are close, and (c) is versioned — `model_id@version` is recorded
on every profile so a stored mapping cannot claim a vocabulary it never saw.

**Evidence.** Hand-labelled 68-case benchmark, executed during this scan:
top-1 **1.0**, top-3 **1.0**, auto-precision **1.0 (60/60)**, automatic coverage
**0.8824**, ambiguity rate **0.0**, unmapped rate **0.0882**, **correct-refusal
rate 1.0** over 8 negative labels, and — the honest generalisation measure —
**alias-independent top-1 1.0 (18/18 labels the alias registry never declared)**.

### 2. ERP-aware adaptive response transformation (Phase 14 — the newest)

**What it is.** A deterministic, explainable, four-signal relevance score that
selects response fields against a natural-language question, reusing the
canonical ERP alias vocabulary as a **query-understanding resource**.

**Why it is a contribution.** The novel claim is precise: *the alias table built
for schema mapping is reusable for question understanding.* A question about
"the customer" reaches `cust_ref` through the same vocabulary that maps it for
storage. No LLM, so it is reproducible, attributable and measurable.

**Evidence.** 68 labelled cases, three methods, one matcher: recall **0.979866**,
irrelevant removal **0.608889**, context reduction **0.500405**, success rate
**1.0**, median **15.83 ms** / p95 **24.05 ms**. Ablation shows query relevance
causes **0.1673 → 0.5004** of the reduction and *all* of the recall loss. Three
failures named and left unfixed.

**Caveats that must be stated.** Synthetic corpus · single annotator ·
hand-authored 31-entry lexicon · no comparison against an LLM selector ·
downstream answer quality not measured.

### 3. Policy-driven hybrid tiered vector storage

**What it is.** Two-stage routing — **hard constraints remove tiers before any
scoring**, then six weighted factors per tier, then hysteresis — with a full
`RoutingDecision` audit including each rejected tier's prohibition reason.

**Why it is more than a cache policy.** Constraints are compliance facts that
scoring **cannot** overturn; every transition carries a stable reason code; and
the weights are declared experimental assumptions rather than tuned results.

**Evidence.** `artifacts/tiered_storage_benchmark.json` over 500 real
384-d vectors: measured latency for all three tiers, identical recall across
tiers (`0.15/0.475/0.55` at k=1/3/5), **lossless** cold round-trip (max
deviation `0.0`), server-verified int8 quantization, measured cold archive bytes.

**Caveats.** Recall is *identical* across tiers, so the benchmark demonstrates
**fidelity**, not a retrieval-quality advantage. Cost figures are explicitly
**normalized units, not currency**. The artifact's own `claim_safety` block
lists what is measured, what is proxy, what is estimated and what is **not
claimed** — which is itself good research practice worth showing a panel.

## Honest ranking

Mapping has the cleanest measured result. Phase 14 has the most novel mechanism
and the most rigorous experimental design (baselines + ablation + named
failures). Storage has the most thorough measurement discipline but the weakest
novelty claim — tiering is a known idea; the *explainability* of the routing is
the contribution.

---

# PART 39 — FINAL RESEARCH RESULTS

## Mapping — MEASURED

Source: `tests/erp_pipeline/mapping/test_mapping_benchmark.py`, executed during
this scan.

```
labelled mappings      : 68 (60 positive, 8 negative)
top-1 accuracy         : 1.0
top-3 recall           : 1.0
auto-selection precision: 1.0 (60/60)
automatic coverage     : 0.8824
ambiguity rate         : 0.0
unmapped rate          : 0.0882
correct refusal rate   : 1.0
alias-independent top-1: 1.0 (18/18 labels the alias registry never declared)
```

**Limitations.** Hand-labelled by one author. 68 labels is small. Perfect scores
on a self-authored corpus must be presented as *"the engine is consistent with
the vocabulary and refusals we declared"*, not as a generalisation claim — which
is precisely why the alias-independent subset is reported separately.

## Storage — MEASURED (`artifacts/tiered_storage_benchmark.json`)

Corpus: **500 records**, 40 queries, 384-d, `all-MiniLM-L6-v2` (5.6.1), real
model output, identical corpus in every tier, `llm_calls: 0`.

| Latency | median | p95 | mean | n |
|---|---:|---:|---:|---:|
| HOT search | 11.0098 | 24.5065 | 12.2485 | 40 |
| WARM search | 16.4469 | 33.2030 | 18.2924 | 40 |
| COLD post-rehydration search | 15.3532 | 34.9939 | 16.2978 | 40 |
| COLD single fetch by id | 14.7328 | 23.4524 | 15.4555 | 40 |

| Recall @k | HOT | WARM | COLD |
|---|---:|---:|---:|
| @1 | 0.15 | 0.15 | 0.15 |
| @3 | 0.475 | 0.475 | 0.475 |
| @5 | 0.55 | 0.55 | 0.55 |

`hot_warm_top5_overlap = 1.0` and `cold_hot_top5_overlap = 1.0` — int8
quantization did not perturb the top-5 ranking on this corpus. The artifact
calls this *"a diagnostic… not a quality metric."*

| Footprint | Value |
|---|---:|
| Comparable proxy (all tiers) | 1,536 bytes/record (384 × 4) |
| **Cold archive MEASURED** | **4,644.12 bytes/record** (2,322,058 total) |
| Cold rehydration total | **9,335.69 ms**, of which index population **8,976.74 ms** |
| Cold vector round-trip | **lossless**, max deviation `0.0` |

Cost (normalized multipliers HOT 1.0 / WARM 0.4 / COLD 0.05) → relative to HOT:
WARM **0.1**, COLD **0.05**.

**Limitations, from the artifact's own `claim_safety` block:**
- *Measured:* cold archive bytes, compression ratio, all latency samples, recall
  against declared labels, server-side int8 verification, cold rehydration fidelity.
- *Proxy:* per-tier vector payload bytes (formula stated).
- *Estimated:* cost resource multipliers — **experimental assumptions**.
- **Not claimed:** monetary savings · production-scale performance ·
  generalization beyond this corpus and model · that cold latency is comparable
  to hot/warm search latency.

> **Recall of 0.15@1 is low in absolute terms.** It is *identical across tiers*,
> which is the point being measured — tiering does not degrade retrieval. Do not
> present 0.15 as a retrieval-quality result.

## Phase 14 — MEASURED

See Part 19 for the full table. Headline:

| | RAW | GENERIC | ADAPTIVE |
|---|---:|---:|---:|
| Relevant recall | 1.0 | 1.0 | **0.979866** |
| Irrelevant removed | 0.0 | 0.0 | **0.608889** |
| Context reduction | 0.0 | 0.143311 | **0.500405** |
| Success rate | 1.0 | 1.0 | **1.0** |
| Median / p95 (ms) | 0.0002 / 0.0004 | 0.0409 / 0.0763 | **15.8268 / 24.0542** |

Ablation: with relevance **0.9799 / 0.5004**; without **1.0 / 0.1673**.

---

# PART 40 — LIMITATIONS

## Research limitations

1. **Canonical model covers three entities only** — invoice, customer,
   purchase_order (14 fields). No employee, document, receipt, or process
   entity. *Verified: `DEFAULT_CANONICAL_MODEL`.* This is why the E002 scenario
   runs the passthrough path.
2. **Phase 14 evaluation corpus is synthetic** — realistic in shape and
   vocabulary, not drawn from a live ERP.
3. **Single annotator** for both the mapping benchmark (68 labels) and the
   Phase 14 dataset (68 cases). No inter-annotator agreement is computable.
4. **The query-intent lexicon is hand-authored** (31 entries + 10 broad-query
   terms). Part of the method, not an emergent result.
5. **Mapping benchmark perfection is on a self-authored corpus.** The
   alias-independent subset (18 labels) is the only generalisation evidence.
6. **Storage recall is 0.15@1** — identical across tiers by design; it measures
   fidelity, not retrieval quality.
7. **Cost figures are normalized units, not currency.**
8. **No comparison against an LLM-based field selector** — none was run.
9. **Downstream answer quality was never measured.** Context reduction is
   measured; whether it improves a model's answers is a different experiment.
10. **Three named recall failures** left unfixed to avoid test-set fitting
    (Part 21).

## Engineering limitations

11. **CONFIRMED DEFECT: `E002` → `email`.** `DEFAULT_SYNONYMS["e"] == "email"`
    makes any letter+digit identifier inject a spurious `email` token, causing a
    false-positive field selection (Part 12, Finding 4). **Not fixed — this task
    is read-only.**
12. **Identity inference picks alphabetically-first**, not semantically-best
    (Part 12, Finding 3).
13. **Characters used instead of a tokenizer** for output budgets. Monotonic in
    tokens, but not a token count.
14. **Only the first record of a collection response is adapted**, with a
    warning. Phase 14 adapts a record, not a result set.
15. **No mapping-profile caching in Phase 14** — ~16 ms median latency is
    dominated by rebuilding an inferred schema and mapping profile per response.
16. **Mapping profiles are cached in a plain in-process `dict`**
    (`PipelineServices.mapping_drafts`), with `erp_runtime.mapping_drafts` as the
    durable store. A restart loses the in-process cache.
17. **`schema_cache` is in-process.** If catalog publication fails, the schema is
    available "for this process only and will not survive a restart" — the API
    says exactly this in a warning.
18. **Sensitivity is propagated but never inferred** — deliberate, but it means
    an unclassified source stays `INTERNAL` by default.
19. **All three storage tiers are `ON_PREMISES`**, so the on-premises constraint
    currently prohibits nothing. The capability is tested, not exercised.
20. **Document pipeline has no TRANSFORM/VALIDATE stage** — PDFs and images do
    not become type-converted `CanonicalRecord`s.
21. **No retry/back-off inside a stage.** `POST /v1/jobs/{id}/retry` exists at
    the job level; `RetryNotSupportedError` → 409 for job types that cannot.
22. **No key rotation** for cold archives (`StaticKeyProvider` only).
23. **SQL Server live verification deferred** — self-declared in
    `/v1/capabilities`.

## Integration limitations

24. **Process/case has no `JobType` and no endpoint** — fully implemented,
    completely unreachable over HTTP.
25. **Verification has no endpoint.**
26. **No chunk-retrieval endpoint** — document chunks cannot be fetched.
27. **No storage/tier monitoring endpoint.**
28. **No per-request authorization hook** for Member 1.
29. **Member 1, 2 and 3 code does not exist here** — every group-level claim is
    a contract proposal, not an integration.

## Deployment limitations

30. **Most Python requirements are unpinned**; no lockfile. Only Phase 13
    packages have exact versions (README limitation 10).
31. **No schema migrations** — bootstrap is `CREATE ... IF NOT EXISTS` only.
32. **No CI**, no container/service definitions.
33. **Inline source passwords unusable with the production secret provider** —
    `EnvironmentSecretProvider` is read-only (README limitation 5).
34. **Frontend cannot send an API key**, so it works only against a deployment
    with reads unprotected and mutations… which upload is. This is a real gap.

## Evaluation limitations

35. **Live-dependent tests skip silently.** In this session 63 skipped (24
    MongoDB, 37 Qdrant, 2 discovery/drift) with 0 failures. **Compare collected
    totals, not passed counts.**
36. **The Phase 12 benchmark requires a live Qdrant** and cannot be reproduced
    without one.
37. **No load or concurrency testing.**

---

# PART 41 — WHAT IS COMPLETE VS NOT COMPLETE

| Capability | Status | Evidence | Required for final submission? |
|---|---|---|---|
| Architecture consolidation | **COMPLETE** | `docs/architecture/ARCHITECTURE_CONSOLIDATION_REPORT.md`; single `src/erp_pipeline/` | Yes — done |
| Schema discovery (4 databases) | **COMPLETE** | `discovery/`, `connectors/`; SQL Server live verification deferred | Yes — done |
| File ingestion (CSV/PDF/image) | **COMPLETE** | `ingestion/`, `POST /v1/files/*` | Yes — done |
| API-spec parsing | **COMPLETE (CONTRACT ONLY)** | `api_specs/`, `endpoints_called = 0` | Yes — done |
| Mapping | **COMPLETE** | `mapping/`; benchmark top-1 1.0, refusal 1.0 | Yes — done |
| Transformation | **COMPLETE** | `transformation/`, 15 `IssueCode`s, `Decimal` money | Yes — done |
| Identity system | **COMPLETE** | `schemas/identity.py`, surrogate-key refusal | Yes — done |
| AI representation + embedding | **COMPLETE** | `ai/`, 384-d local model, skip-if-unchanged | Yes — done |
| Tiered storage | **COMPLETE** | `storage/`, `artifacts/tiered_storage_benchmark.json` | Yes — done |
| Search | **BACKEND COMPLETE** | `POST /v1/search`, filters refused not ignored | Yes — done |
| Record resolution | **BACKEND COMPLETE** | `GET /v1/records/{id}`, canonical id carried forward | Yes — done |
| Incremental sync | **BACKEND COMPLETE** | `sync/`, checkpoint never passes a failure | Yes — done |
| Schema drift | **BACKEND COMPLETE** | `sync/drift.py`, `DRIFT_CHECK` job | Yes — done |
| Verification | **BACKEND COMPLETE — NO ENDPOINT** | `verification/`, 18 codes, 54 tests | Yes — done |
| Process cases | **BACKEND COMPLETE — NO JobType, NO ENDPOINT** | `process/`; **absent from `JobType`** | Partially — declare the gap |
| **Response adaptation** | **COMPLETE** | `response_adaptation/`, 106 tests, `artifacts/phase14_*.json` | **Yes — done** |
| Image/PDF adaptation | **COMPLETE** | `assets.py`; degrades safely on corrupt input | Yes — done |
| SSRF controls | **COMPLETE** | 14 controls, off by default, 15 refusal cases tested | Yes — done |
| REST control plane | **COMPLETE** | 23 operations, `artifacts/openapi_contract_snapshot.json` | Yes — done |
| Frontend | **PARTIAL — UPLOAD ONLY** | One page, two endpoints, no API-key support | Declare honestly |
| Member 1 integration | **EXTERNAL INTEGRATION REQUIRED** | No governance code here | Contract only |
| Member 2 integration | **EXTERNAL INTEGRATION REQUIRED** | `POST /v1/responses/adapt` is ready and tested; the caller does not exist here | Contract + endpoint ready |
| Member 3 integration | **EXTERNAL INTEGRATION REQUIRED** | `llm_ready` exposed; process state **not** exposed | Contract only |
| Storage monitoring UI/API | **NOT IMPLEMENTED** | No endpoint | No |
| SOAP / DOCX / Excel / HTML | **NOT IMPLEMENTED** | Out of declared scope | No |

---

# PART 42 — END-TO-END "ONE STORY"

*Written for a non-technical examiner. Every step is labelled with who owns it.*

A company has run the same ERP system for fifteen years. It holds every invoice,
customer and purchase order the business has ever produced — and none of it can
be given to an AI assistant, because the columns are called things like
`inv_no`, `cust_ref` and `total_amt`, and because an AI cannot read a database
the way a person reads a report.

**Step 1 — Look inside the old system.** *(Member 4 — IMPLEMENTED)*
The component connects to the ERP and reads its structure: which tables exist,
which columns, which types, how they reference each other. For files, it reads
the CSV or the scanned PDF instead. Nothing is assumed from a filename — the
content is inspected.

**Step 2 — Translate the vocabulary.** *(Member 4 — IMPLEMENTED)*
`inv_no` is proposed as `invoice_id`, `cust_ref` as `customer_id`, `total_amt`
as `amount`. Each proposal shows *why*: which evidence matched and how
confidently. When two candidates are too close to call, the system **says so and
refuses to choose** — a person decides. On our 68-label benchmark it chose
correctly every time it chose, and correctly declined every time it should have.

**Step 3 — Clean and standardise the records.** *(Member 4 — IMPLEMENTED)*
Text becomes typed values. Money becomes exact decimals — never floating-point,
because `25000.10` must not silently become `25000.099999999999`. Every problem
gets a stable code. Every record gets a permanent identity built from its
*business* key, never from a database row number that changes when the data is
reloaded.

**Step 4 — Make it readable by an AI.** *(Member 4 — IMPLEMENTED)*
Each record becomes a short labelled text, then a 384-number "meaning
fingerprint" produced by a model that runs entirely on our own machine. Nothing
is sent to any external AI service.

**Step 5 — Store it where it belongs.** *(Member 4 — IMPLEMENTED)*
Frequently used records go to fast memory (HOT); ordinary ones to disk (WARM);
dormant ones to compressed, encrypted archives (COLD). Compliance rules
**remove** tiers before any preference is considered, so sensitive data can
never be scored into a forbidden place. Every decision records its reasoning.

**Step 6 — Someone asks a question.** *"Find E002's date of birth."*

**Step 7 — Permission is checked.** *(Member 1 — EXTERNAL, not in this
repository)*

**Step 8 — The right ERP operation is chosen and called.** *(Member 2 —
EXTERNAL, not in this repository)* This component deliberately never calls an
ERP. Reading a contract and executing it are separate jobs, and mixing them
would put a request-proxy inside the network perimeter.

**Step 9 — The ERP answers** — with a wrapped JSON envelope, or a scanned PDF,
or a photograph of a certificate, or a file type nobody expected.

**Step 10 — The response is understood.** *(Member 4 — IMPLEMENTED)*
The component looks at the actual bytes, not the label the server attached. It
finds the business record inside the envelope by *structure*, not by guessing
that a key called `result` is special. A PDF is read and, if it is a scan, run
through OCR with page numbers preserved. An unreadable file is described
truthfully rather than guessed at.

**Step 11 — Only the useful part is kept.** *(Member 4 — IMPLEMENTED)*
The question is scored against every field using the same ERP vocabulary built
in Step 2. `date_of_birth` is kept; `internal_row_version` is dropped. The
record's key is always kept, so the answer stays traceable. In our measurements
this removes about 61% of the irrelevant fields and halves the size of what the
AI has to read — and it explains, field by field, exactly why.

**Step 12 — The AI receives a small, honest, traceable answer.**
*(handed to Member 3 — EXTERNAL)*

**Step 13 — The user gets their answer**, and it can be traced back through the
record identity to the exact row in the fifteen-year-old ERP that produced it.

> **One correction to this story, for accuracy.** In Step 11, the *employee*
> example runs without canonical translation, because the canonical vocabulary
> currently covers invoices, customers and purchase orders — not employees. The
> field selection still works; the renaming does not. For the translation part
> of the story, an invoice is the honest example.

---

# PART 43 — VIVA QUESTIONS

**1. Why not just change the ERP?**
*Short:* You cannot; it is load-bearing and vendor-locked.
*Technical:* A 15-year-old ERP is audited, integrated with a dozen systems, and
often unmodifiable by contract. The entire architecture is premised on adapting
*outside* the source. Nothing in this repository writes to a source system.
*Evidence:* No write path exists in `connectors/`; discovery is read-only.

**2. Why a canonical model at all?**
*Short:* Because two ERP systems never spell a concept the same way.
*Technical:* `invoice_id` appears as `invoice_no`, `inv_id`, `inv_no`,
`invoiceid`, `bill_no`… A shared target vocabulary is what makes cross-system
reasoning and cross-system retrieval possible.
*Evidence:* `mapping/canonical_model.py:359` `_INVOICE_FIELDS` alias lists.

**3. What makes your mapping "explainable"?**
*Short:* Every decision carries its evidence and can be overruled.
*Technical:* Four weighted components (name 0.50, type 0.20, entity 0.20, path
0.10), a named `NameMatchKind` per candidate, a confidence level from explicit
thresholds, and an `AMBIGUOUS` outcome instead of a silent choice.
*Evidence:* `mapping/scoring.py`, `mapping/models.py:90-115, 158-162`.

**4. Why Qdrant?**
*Short:* It gives server-side filtering and real int8 quantization.
*Technical:* The tiering research needs two *different* physical configurations
of the same store — RAM/float32 vs disk/int8 — plus payload filters pushed into
the ANN search. Qdrant provides both, and reports its quantizer so we can verify
it rather than assume it.
*Evidence:* `storage/hot_tier.py:63`, `storage/warm_tier.py:1-23`.

**5. Why HOT/WARM/COLD rather than one store?**
*Short:* Because vectors have wildly different access patterns and costs.
*Technical:* Keeping every vector in RAM at float32 is the most expensive
possible choice. The measured proxy shows WARM at 0.1 and COLD at 0.05 of HOT's
normalized cost, with **identical recall** across tiers on our corpus.
*Evidence:* `artifacts/tiered_storage_benchmark.json` → `cost.relative_to_hot`, `recall`.

**6. Why not just send the raw ERP response to the LLM?**
*Short:* Because it wastes context on plumbing and is untraceable.
*Technical:* Measured: the raw response is 16,049 bytes over 625 leaf fields
across 68 cases; adaptation halves the bytes and removes 61% of labelled noise
while preserving 98% of relevant fields — and produces a field-by-field record
of what was removed and why.
*Evidence:* `artifacts/response_adaptation_evaluation.json`.

**7. Why is RAW recall 1.0?**
*Short:* Because it keeps everything, so it can never drop a relevant field.
*Technical:* Recall of 1.0 is trivial for a method that makes no decision. It is
the *floor*, not a competitor. The comparison that matters is recall **against**
irrelevant-removal and context-reduction, where RAW scores 0.0 on both.
*Evidence:* artifact `methods.raw` — `irrelevant_field_removal_rate: 0.0`,
`context_reduction_ratio: 0.0`.

**8. Why is your proposed method's recall lower?**
*Short:* Because it makes decisions, and decisions can be wrong.
*Technical:* Three fields across 68 cases were dropped — two query-vocabulary
gaps, one ERP-vocabulary gap. That is the price of removing 137 of 225
irrelevant fields. We report it first rather than burying it.
*Evidence:* artifact `limitations`, Part 21.

**9. What does 0.9799 actually mean?**
*Short:* 146 of 149 labelled relevant fields survived selection.
*Technical:* Relevant-field recall = kept ÷ labelled, aggregated over all 68
cases. Per-case perfect recall is 0.9559 — 65 of 68 cases lost nothing.
*Evidence:* artifact `methods.erp_aware_adaptive.totals`.

**10. Why isn't that "accuracy"?**
*Short:* Because there is no single right answer to be accurate about.
*Technical:* This is a *selection* task with two asymmetric error types. A
dropped relevant field is unrecoverable downstream; a retained irrelevant one
merely costs context. Collapsing them into "accuracy" would hide which error
occurred. We report recall and removal separately and never blend them.
*Evidence:* `evaluation.py` module docstring: "RECALL IS THE METRIC THAT MATTERS".

**11. Why didn't you fix the three failing fields?**
*Short:* Because fixing them after seeing them is fitting to the test set.
*Technical:* Each has a one-line fix — add `BELNR`, add `whom`, add `resource`.
Applying them would make the recall number measure how many failures we had
already seen, not how the method performs. We fixed the four *general method
defects* and reported the three *specific vocabulary gaps*.
*Evidence:* Part 21; artifact `limitations[].detail`.

**12. Why is there no LLM in the relevance algorithm?**
*Short:* Because an LLM would make the result unmeasurable.
*Technical:* Non-deterministic between runs, unattributable to any rule, and
impossible to ablate. Every score here is a weighted sum of four bounded signals
and every field carries the signals that produced it.
*Evidence:* `relevance.py` module docstring; `test_scoring_is_deterministic_across_instances`.

**13. Why does Member 2 execute the API and not you?**
*Short:* Reading a contract and executing it are different jobs.
*Technical:* Executing arbitrary ERP endpoints from inside this service would
make it an outbound request proxy within the network perimeter — the same class
of risk the SSRF controls exist to prevent. Tool selection also needs
conversational context this component never sees.
*Evidence:* `api_specs/service.py:16`; `endpoints_called = 0`; `/v1/capabilities` limitation.

**14. How are PDFs handled?**
*Short:* Through the same ingestion the file pipeline uses, with OCR fallback.
*Technical:* Magic bytes → `ingest_pdf_file` (PyMuPDF) → per-page text, OCR when
the text layer is empty → page provenance and `page_range` → bounded text →
`AdaptedAsset` with `llm_directly_readable=False`.
*Evidence:* `response_adaptation/assets.py::_adapt_document`.

**15. How are images handled?**
*Short:* Dimensions plus OCR text, and the image stays directly readable.
*Technical:* Header parsed lazily before pixel decode (decompression-bomb
defence), dimensions from `document_metadata`, OCR text carried **alongside**
the image so a vision model is not forced to accept a lossy transcription.
*Evidence:* `image_ingestion.py:79-107`; `assets.py::_adapt_image`.

**16. How is SSRF prevented?**
*Short:* Fetching is off by default and the package ships no HTTP client.
*Technical:* Fourteen controls — scheme/port/host allow-lists, **every** resolved
address checked, loopback/private/link-local/multicast/reserved blocked
including IPv4-mapped IPv6, credentials-in-URL refused, redirects re-validated,
size and timeout limits, magic-byte verification. Each refusal names its rule.
*Evidence:* `assets.py::validate_asset_url`; 15 parametrised tests pass.

**17. How is sensitive data handled?**
*Short:* Consumed and enforced, never guessed.
*Technical:* Sensitivity flows canonical → representation → tier state → Qdrant
payload → routing constraints → adaptation policy. Blocked levels withhold the
payload while still naming the fields, so "withheld" is distinguishable from
"absent". We never inspect values to infer a classification.
*Evidence:* `formatter.py::build_payload`; `vector_router.py:113`.

**18. What happens on schema drift?**
*Short:* It is detected, classified, and unsafe mappings are quarantined.
*Technical:* Schema hashes are compared, `findings_from_diff` classifies each
change by type and severity, impact analysis identifies affected mappings, and
records that cannot be safely transformed are quarantined rather than corrupted.
*Evidence:* `sync/drift.py:263,357`; `sync/impact.py`.

**19. How do deterministic IDs work, and why three of them?**
*Short:* Same input always yields the same id; the three name different things.
*Technical:* `erp:{sys}:{entity}:{business key}` for the record;
`ai:{entity}:{key}` for its AI projection; UUIDv5 for the Qdrant point.
Normalization is **lossy** (`:`→`_`), so the canonical id cannot be parsed back
out of the representation id — it is carried forward explicitly instead.
*Evidence:* `schemas/identity.py:192-278`; `ai/models.py:296`.

**20. What happens if Qdrant is down?**
*Short:* Search returns 503; everything else keeps working.
*Technical:* Liveness deliberately does not depend on Qdrant. Readiness reports
it. Search raises `DependencyUnavailableError`. Uploads, schema inference,
mapping, transformation and **all of Phase 14** are unaffected.
*Evidence:* `api/routers.py:74-79`; `api/responses.py:ERROR_STATUS`.

**21. What are the current limitations?**
*Short:* Forty-plus, catalogued in Part 40.
*Technical:* Headline five: canonical model covers three entities; the Phase 14
corpus is synthetic and single-annotator; process/case has no HTTP surface;
the `E002`→`email` tokenisation defect; the frontend covers upload only.

**22. Is the canonical model big enough to be useful?**
*Short:* No — and that is stated, not hidden.
*Technical:* Three entities, 14 fields. Anything outside it runs the passthrough
path with source names. The framework is designed for extension
(`FieldProvenance.PHASE_8_EXTENSION` requires a written `reason` for every added
field), but the current vocabulary is a prototype.
*Evidence:* `canonical_model.py:105-113`, `DEFAULT_CANONICAL_MODEL`.

**23. Your frontend only uploads files. Why?**
*Short:* Deliberate scope; the research is in the backend.
*Technical:* `App.tsx` renders one page with no router, and its docstring says
so. Everything else is available over HTTP and demonstrated through `/docs`.
The honest gap: the frontend cannot send an API key.

**24. Why characters instead of tokens for budgets?**
*Short:* This project ships no tokenizer.
*Technical:* Adding one means shipping a specific model's vocabulary to make a
budget decision, coupling the component to one model family. Characters are
exact, dependency-free and monotonic in tokens. The report states the unit so
nobody mistakes it for a token count.
*Evidence:* `formatter.py` module docstring.

**25. What is the "no-signal fallback" and isn't it cheating?**
*Short:* It abstains, and it is counted as an abstention.
*Technical:* When nothing clears the threshold, everything is kept rather than
returning only the record id — because a one-field answer is indistinguishable
from an empty record. Every field is marked `no_relevance_signal`, so the
evaluation counts abstentions rather than crediting them as successful
selections.
*Evidence:* `relevance.py::_fallback`; `REASON_NO_SIGNAL`.

**26. How do you know your baselines are fair?**
*Short:* We found and fixed two biases that favoured us.
*Technical:* The field matcher penalised RAW for nested paths it plainly
contained (recall 0.973 → 1.000), and top-level field counting credited RAW with
a fictitious 70% reduction (→ 0.0). Both corrections moved results *against* the
proposed method. GENERIC is also **given** envelope unwrapping for free.
*Evidence:* Part 19; `evaluation.py::field_present`.

**27. Why does the checkpoint not advance past a failure?**
*Short:* Because that would silently lose the change.
*Technical:* The watermark advances only to the last change that completed
*every* stage — read, transform, load, embed, store. A row whose vector write
failed does not move it. That is what makes an interrupted run resumable.
*Evidence:* `sync/coordinator.py:195-198` and its module docstring.

**28. What stops a database row number becoming a record's identity?**
*Short:* An explicit refusal.
*Technical:* `require_business_key()` rejects digits-only keys, because a
`SERIAL` changes when data is reloaded and would silently create duplicate
records. The check is narrow on purpose — an earlier broad regex wrongly
rejected `INV-001`.
*Evidence:* `schemas/identity.py:138-190`; `IntegrityCode.SURROGATE_KEY_IDENTITY`.

**29. Can I trust the storage benchmark's cost numbers?**
*Short:* They are assumptions, and the artifact says so.
*Technical:* Multipliers are **normalized units, not currency**. The artifact's
`claim_safety` block separates measured from proxy from estimated, and
explicitly does **not claim** monetary savings or production-scale performance.
*Evidence:* `artifacts/tiered_storage_benchmark.json` → `cost.model.assumptions`, `claim_safety`.

**30. What is the single strongest contribution?**
*Short:* Reusing the ERP mapping vocabulary as a query-understanding resource.
*Technical:* The canonical alias table built for schema mapping also answers
"which field is this question about" — deterministically, explainably, and
measurably. The ablation shows this mechanism causes two thirds of the context
reduction and all of the recall cost.
*Evidence:* Part 20; `relevance.py`.

**31. Is there any known bug you have not fixed?**
*Short:* Yes — one, and here it is.
*Technical:* `DEFAULT_SYNONYMS["e"] == "email"` makes an identifier like `E002`
tokenise to `("email","002")`, causing a false-positive selection of an email
field for a query that never mentions email. It costs precision, not recall, and
does not affect the published metrics because no evaluation query uses that
identifier shape.
*Evidence:* Part 12, Finding 4 — reproduced during this scan.

**32. What would you do next?**
*Short:* Expand the canonical model, then re-run the same experiments.
*Technical:* (a) Add employee/document entities; (b) fix the `e`→`email` synonym
scoping; (c) cache mapping profiles per endpoint to cut the ~16 ms latency;
(d) expose process/case over HTTP; (e) obtain a second annotator; (f) evaluate
downstream answer quality, which this work does not measure.

---

# PART 44 — EXPLAIN MY ENTIRE COMPONENT IN 5 MINUTES

## 30-second explanation

> "Old ERP systems store business data in columns nobody can read — `inv_no`,
> `cust_ref`, `total_amt` — spread across four different database technologies
> and a pile of scanned PDFs. My component translates that into one shared
> business vocabulary, makes it searchable by meaning, and — when a live ERP
> answers a question — strips the response down to just the fields that question
> actually needs. It uses no AI model to make any of its decisions, so every
> choice can be explained and measured."

## 2-minute explanation

> "My component sits between a legacy ERP and an AI assistant, and it works in
> two directions.
>
> **Going in**, it discovers what an old system actually contains — tables,
> columns, types, relationships — and proposes a mapping from those local names
> onto a shared canonical vocabulary. Every proposal shows its evidence and its
> confidence, and when two candidates are too close to call, it refuses and asks
> a human. On a 68-label benchmark it was correct every time it chose, and
> correctly declined every time it should have. The records are then cleaned,
> typed, given permanent business identities, turned into text and embedded as
> vectors — using a model that runs locally, never an external AI service. Those
> vectors are routed to fast, cheap or archived storage by an explicit policy
> where compliance rules *remove* options before any preference is scored.
>
> **Coming back**, when a teammate's component has already called a live ERP API,
> mine transforms whatever came back — a wrapped JSON envelope, a scanned PDF, a
> photograph, or a file type nobody expected — into compact context an AI can
> use. It scores every field against the user's question using the same ERP
> vocabulary, keeps the record's identity so the answer stays traceable, and
> explains exactly what it dropped and why. That removes about 61% of the
> irrelevant fields and halves the context size, at a measured cost of 2% recall.
>
> What it never does: call an ERP, decide which API to call, or generate an
> answer. Those belong to my teammates."

## 5-minute explanation

> **The problem.** A company's ERP is fifteen years old. It cannot be changed —
> it is audited and integrated with everything. But its data is exactly what an
> AI assistant would need, and it is unusable: the column names are local
> shorthand, the technologies are incompatible, and there is no way to search it
> by meaning rather than by exact value.
>
> **My component is the translation layer**, and it has two halves.
>
> **Half one — preparing the historical data.** It connects to PostgreSQL, MySQL,
> SQL Server or MongoDB and reads the structure. For MongoDB, where there is no
> declared schema, it *observes* one from a bounded sample and marks it as
> observed rather than declared — because those are different kinds of fact. It
> also ingests CSV exports, PDFs and scanned images, always deciding the file
> type from its actual bytes rather than its name.
>
> Then it maps. Four weighted signals — the field's name, its type, which entity
> it belongs to, and its nesting path — produce a score for every candidate
> canonical field. The heaviest signal is name, because an explicit alias is a
> human's own statement that two names mean the same thing. Crucially, the engine
> *refuses* when it is not sure. It also refuses to build a record identity out
> of a database row number, because that number changes when the data reloads.
>
> Cleaned records become short labelled texts, then 384-number embeddings from a
> local model. Those vectors go to one of three tiers. This is where the policy
> work matters: compliance constraints *remove* tiers entirely before any scoring
> happens, so a restricted record can never be scored into a forbidden location.
> Then six weighted factors choose among what remains, and hysteresis stops a
> record oscillating between tiers over a trivial score difference. Every
> decision records its full reasoning, including why each rejected tier was
> rejected.
>
> **Half two — handling a live answer.** This is my newest research contribution.
> A teammate calls the ERP; I receive what came back. First I decide what it
> actually is, trusting the bytes over the server's own content-type header —
> because a legacy ERP labelling a PDF as JSON is not hypothetical. Then I find
> the business record inside the response envelope using a *structural* rule
> rather than a list of vendor wrapper names. Then I run it through the very same
> mapping engine the offline path uses, so an API response is absorbed like any
> other source rather than by a second parallel engine.
>
> Finally, and this is the new mechanism: I score every field against the user's
> question. Four signals again — but the heaviest is whether the question names
> the *canonical concept* the field maps to. That is what makes it ERP-aware
> rather than keyword matching: a question about "the customer" finds a field
> called `cust_ref`, not because the strings resemble each other, but because my
> canonical vocabulary says those are the same thing. I also handle the gap
> between how people *ask* and how schemas *spell* — "how much" contains no form
> of the word "amount", so a small hand-written lexicon bridges it. Identity
> fields are always preserved so the answer stays traceable, and if the question
> matches nothing at all, I keep everything rather than returning a confidently
> wrong single field.
>
> **The evidence.** 68 labelled cases, three methods, one scoring rule for all of
> them. My method achieves 0.9799 relevant-field recall while removing 61% of
> irrelevant fields and halving the serialized context, at about 16 milliseconds
> per response. Both baselines beat me on recall — with 1.0 — but they achieve
> that by never making a decision, and they remove nothing. An ablation shows my
> query-relevance mechanism causes two thirds of the context saving and all of
> the recall loss, which is exactly the trade I am claiming.
>
> **What I am careful not to claim.** The corpus is synthetic and I labelled it
> myself. The canonical vocabulary covers three entity types. I never compared
> against an LLM-based selector, and I never measured whether the smaller context
> produces better answers — only that it is smaller and preserves the fields I
> labelled as needed. Three cases still lose a relevant field, and I deliberately
> did not fix them, because fixing a failure after seeing which case produced it
> would mean my number measures my hindsight rather than my method."

---

# PART 45 — FINAL ARCHITECTURE DIAGRAMS

## 1. Overall Member 4 component

```mermaid
flowchart TB
    subgraph EXT["EXTERNAL — not in this repository"]
        M1["Member 1 · Governance"]
        M2["Member 2 · MCP + ERP execution"]
        M3["Member 3 · Answer generation"]
    end
    subgraph M4["MEMBER 4 — src/erp_pipeline/"]
        direction TB
        SCH["schemas · contracts + identity"]
        subgraph ACQ["Acquisition"]
            CON["connectors"] --- DIS["discovery"]
            ING["ingestion"] --- SPEC["api_specs"]
        end
        subgraph SEM["Semantics"]
            CAT["catalog"] --- MAP["mapping"] --- TRN["transformation"]
        end
        subgraph AIS["AI + storage"]
            AI["ai"] --- STO["storage"]
        end
        subgraph OPS["Operations"]
            SYN["sync"] --- VER["verification"] --- PRC["process"]
        end
        RA["response_adaptation · PHASE 14"]
        ORC["orchestration"] --- API["api · 23 operations"]
    end
    FE["frontend · upload only"] --> API
    M2 -->|"POST /v1/responses/adapt"| RA
    RA --> M3
    API --> M3
    API -.->|"schemas · sensitivity · provenance"| M1
    API -.->|"schemas · relationships · specs"| M2
    SCH --> ACQ & SEM & AIS & OPS & RA
```

## 2. Offline data-preparation path

```mermaid
flowchart LR
    S["ERP DB / CSV / PDF"] --> D["discovery / ingestion"]
    D --> SC["SourceSchema"] --> C["catalog"]
    C --> STOP1(["STOP — manual step"])
    STOP1 --> MP["mapping → MappingProfile"]
    MP --> TR["transformation → CanonicalRecord"]
    TR --> AR["ai → AIRepresentation"]
    AR --> EM["ai → EmbeddingRecord"]
    EM --> RT["storage → RoutingDecision"]
    RT --> H["HOT"] & W["WARM"] & CO["COLD"]
```

## 3. Real-time API response path

```mermaid
flowchart LR
    R["raw response"] --> DT["detector"]
    DT -->|structured| UW["unwrap"] --> IN["infer schema"] --> MA["map"] --> TF["transform"] --> RL["relevance"] --> BD["budgets"]
    DT -->|image/document/binary| AS["assets"]
    BD --> AD["AdaptedResponse"]
    AS --> AD
```

## 4. CSV upload workflow

```mermaid
sequenceDiagram
    participant U as User
    participant FE as UploadPage
    participant API as POST /v1/files/csv
    participant ST as PostgresUploadStore
    participant IG as FileIngestionService
    participant CT as CatalogService
    U->>FE: drop employees.csv
    FE->>FE: classifyUpload → "csv"
    FE->>API: multipart, field "file"
    API->>ST: store_stream → upload_id + sha256
    API->>API: suffix ∈ CSV_SUFFIXES?
    API->>IG: ingest(path)
    IG->>IG: detect by content → parse → infer schema
    IG-->>API: SourceSchema
    API->>CT: register_source_system → publish_schema
    CT-->>API: published: true
    API-->>FE: 201 CsvUploadResponse
    FE-->>U: "4 columns, 118 B"
    Note over API: STOP — no mapping, no embedding
```

## 5. Semantic search workflow

```mermaid
flowchart TD
    Q["POST /v1/search"] --> F{"SearchFilters.from_mapping"}
    F -->|"unknown field"| E["422 — refused, never ignored"]
    F -->|"valid"| V["embedding.encode(query)"]
    V --> HS["HOT · Qdrant + server-side filter"]
    V --> WS["WARM · Qdrant int8 + filter"]
    V -.->|"include_cold"| CS["filter tier state → decrypt → temp index"]
    HS & WS & CS --> MG["merge + dedupe"]
    MG --> ST["state.load(representation_id)<br/>PostgreSQL = authoritative"]
    ST --> RS["SearchResponse<br/>canonical_record_id carried forward"]
    RS --> GR["GET /v1/records/{canonical_record_id}"]
```

## 6. Birth-certificate JSON workflow

```mermaid
flowchart TD
    A["ERP JSON: status + result{8}"] --> B["detect → STRUCTURED"]
    B --> C["unwrap → ('result',)"]
    C --> D["infer schema (INFERRED)"]
    D --> E{"canonical entity matched?"}
    E -->|"NO — no employee entity"| P["passthrough · entity_type=None"]
    E -->|"yes"| M["map + transform"]
    P & M --> R["RelevanceScorer"]
    R --> I["identity preserved: certificate_no"]
    I --> F["budgets → llm_ready"]
    F --> O["AdaptedResponse + report"]
```

## 7. Birth-certificate PDF / image workflow

```mermaid
flowchart TD
    B["bytes"] --> S["detect_from_signature"]
    S -->|"%PDF-"| P["_adapt_document"]
    S -->|"PNG/JPEG/TIFF/WEBP"| I["_adapt_image"]
    S -->|"no match"| X["_unsupported"]
    P --> PT["ingest_pdf_file → pages + OCR fallback"]
    PT --> PC["chunk_document → page anchors"]
    I --> II["ingest_image_file → w/h + OCR"]
    PC --> A["AdaptedAsset · llm_directly_readable=false"]
    II --> A2["AdaptedAsset · llm_directly_readable=true"]
    X --> A3["AdaptedAsset · unsupported_binary"]
    PT -.->|"IngestionError"| A3
    II -.->|"IngestionError"| A3
```

## 8. Member 1 / 2 / 3 integration

```mermaid
flowchart LR
    subgraph M4["Member 4 — implemented"]
        SCHEMAS["GET /v1/schemas/{id}"]
        SPECS["POST /v1/api-specs/*"]
        ADAPT["POST /v1/responses/adapt"]
        SEARCH["POST /v1/search"]
        RECORDS["GET /v1/records/{id}"]
        CAPS["GET /v1/capabilities"]
        PROC["process/ · NO ENDPOINT"]
    end
    M1["Member 1 · governance<br/>EXTERNAL"] -.->|"needs sensitivity + provenance"| SCHEMAS
    M1 -.-> CAPS
    M2["Member 2 · MCP + execution<br/>EXTERNAL"] -->|"builds tools from"| SCHEMAS & SPECS
    M2 -->|"posts raw response"| ADAPT
    ADAPT -->|"llm_ready"| M3["Member 3 · answers<br/>EXTERNAL"]
    SEARCH --> M3
    RECORDS --> M3
    PROC -.->|"NOT REACHABLE OVER HTTP"| M3
```

## 9. HOT / WARM / COLD storage

```mermaid
flowchart TD
    E["EmbeddingRecord"] --> M["StorageRecordMetadata → StorageRoutingContext"]
    M --> PR["prohibited_tiers()<br/>sensitivity · legal hold · retention<br/>low latency · critical"]
    PR --> RM["REMOVE prohibited tiers<br/>(scoring cannot reinstate)"]
    RM --> SC["score_tiers()<br/>recency·access·criticality·latency·age·dormancy"]
    SC --> HY["hysteresis<br/>min residence 7d + margin"]
    HY --> DEC["RoutingDecision(tier, forced, scores, reason_code)"]
    DEC --> H["HOT · float32 RAM"]
    DEC --> W["WARM · int8 on disk"]
    DEC --> C["COLD · gzip + AES-256-GCM"]
    DEC --> TS["vector_storage_state<br/>AUTHORITATIVE"]
    TS --> TR["vector_tier_transitions (audit)"]
```

## 10. Incremental sync + schema drift

```mermaid
flowchart TD
    S["sync_state (watermark, version)"] --> DR["detect_drift(old, new schema)"]
    DR -->|"findings"| IM["impact analysis"]
    IM -->|"mapping unsafe"| QU["quarantine"]
    DR -->|"safe"| EX["extract bounded changes"]
    EX --> TF["transform → canonical upsert"]
    TF --> AR["rebuild representation"]
    AR --> CH{"content hash changed?"}
    CH -->|"no"| SK["skip embedding"]
    CH -->|"yes"| EM["embed → vector upsert"]
    EM --> TU["TIER_UPDATE"]
    SK & TU --> CP{"every stage succeeded?"}
    CP -->|"yes"| AD["advance watermark"]
    CP -->|"no"| HOLD["checkpoint_open = False<br/>WATERMARK DOES NOT PASS"]
    AD & HOLD --> SV["save(state, expected_version)"]
```

---

# PART 46 — FINAL FILE / FUNCTION INDEX

| Functionality | File | Class / function | Endpoint / test |
|---|---|---|---|
| Canonical identity | `schemas/identity.py` | `make_canonical_record_id`, `parse_canonical_id`, `make_deterministic_uuid`, `require_business_key` | `tests/erp_pipeline/test_identity_and_serialization.py` |
| Contracts | `schemas/source_models.py`, `canonical_models.py` | `SourceSchema`, `SourceEntity`, `SourceField`, `CanonicalRecord` | `test_source_models.py`, `test_canonical_models.py` |
| JSON serialization | `schemas/serialization.py` | `to_json_value` (Decimal → exact string) | `test_identity_and_serialization.py` |
| Catalog schema | `catalog/schema.py` | 7 tables in `erp_catalog` | `tests/erp_pipeline/catalog/` |
| Catalog service | `catalog/` | `register_source_system`, `publish_schema` | `tests/erp_pipeline/catalog/` |
| DB connectors | `connectors/postgresql.py`, `mysql.py`, `sqlserver.py`, `mongodb.py` | `registry` | `tests/erp_pipeline/connectors/` |
| Relational discovery | `discovery/relational.py` | `DiscoveryService` | `tests/erp_pipeline/discovery/` |
| MongoDB inference | `discovery/mongodb_inference.py` | bounded observed schema | `test_live_mongodb_inference.py` |
| File type detection | `ingestion/detection.py` | `detect_from_signature`, `_SIGNATURES`, `looks_like_text` | `test_detection_and_identity.py` |
| Content hashing | `ingestion/hashing.py` | `hash_bytes`, `hash_file`, `make_file_id` | same |
| CSV ingestion | `ingestion/csv_ingestion.py`, `csv_inference.py` | streaming + type inference | `test_csv_ingestion.py`, `test_csv_inference.py` |
| PDF ingestion | `ingestion/pdf_ingestion.py` | `PdfFileIngestion`, `ingest_pdf_file` | `test_pdf_ingestion.py` |
| Image ingestion | `ingestion/image_ingestion.py` | `ImageFileIngestion`, `_inspect` (bomb defence) | `test_image_ingestion.py` |
| OCR | `ingestion/ocr.py` | `probe_ocr`, `run_ocr`, `OcrCapability` | `test_image_ingestion.py` |
| Doc classification | `ingestion/document_classification.py` | `ClassificationConfig` | `test_document_classification.py` |
| API-spec parsing | `api_specs/service.py`, `postman_parser.py` | `ApiSpecService.parse` — **never calls** | `tests/erp_pipeline/api_specs/` |
| Payload inference | `api_specs/inference.py` | `infer_structure_from_examples` | same |
| **Canonical model** | `mapping/canonical_model.py` | `DEFAULT_CANONICAL_MODEL`, `CanonicalField.aliases` | `test_mapping_models.py` |
| **Mapping scoring** | `mapping/scoring.py` | 4 components, `NameMatchKind` | `tests/erp_pipeline/mapping/` |
| Normalization | `mapping/normalization.py` | `canonical_tokens`, `DEFAULT_ABBREVIATIONS`, `DEFAULT_SYNONYMS` | same |
| Mapping service | `mapping/service.py` | `MappingService.generate` | `POST /v1/mappings/suggest` |
| **Mapping benchmark** | `tests/erp_pipeline/mapping/test_mapping_benchmark.py` | 68 labels, refusal rate | `-k reported -s` |
| Transformation | `transformation/service.py` | `TransformationService.transform_record` | `tests/erp_pipeline/transformation/` |
| Type conversion | `transformation/type_converter.py` | `Decimal` money | same |
| Quality codes | `transformation/models.py` | `IssueCode` (15) | same |
| AI text | `ai/representation.py` | `build_text`, `canonical_record_to_representation` | `tests/erp_pipeline/ai/` |
| Embedding | `ai/embedding.py` | `SentenceTransformerModel`, measured `dimension` | same |
| Chunking | `ai/chunking.py` | `chunk_document`, `chunk_text` | same |
| **Storage policy** | `storage/storage_policy.py` | `StoragePolicy`, `TierWeights`, `DEFAULT_TIER_LOCATIONS` | `tests/erp_pipeline/storage/` |
| **Routing** | `storage/vector_router.py` | `prohibited_tiers`, `score_tiers`, `route`, `_apply_hysteresis` | same |
| HOT tier | `storage/hot_tier.py` | `QdrantHotTier` (`with_payload=False`) | `test_live_tiers.py` |
| WARM tier | `storage/warm_tier.py` | `QdrantWarmTier`, `quantization_verified` | same |
| COLD tier | `storage/cold_tier.py` | `ColdArchiveTier`, AES-GCM, `COLD_FORMAT_VERSION` | `test_cold_retrieval_benchmark.py` |
| Tier state | `storage/state.py` | 3 tables in `erp_vector_storage` | `test_live_postgres_state.py` |
| Search filters | `storage/filters.py` | `SearchFilters`, `FILTERABLE_FIELDS` | `test_search_filters.py` |
| Migration | `storage/migration.py` | policy re-check before moving | `tests/erp_pipeline/storage/` |
| **Storage benchmark** | `storage/benchmark.py` | `write_artifact` | `scripts/benchmark_tiered_storage.py` |
| Sync state | `sync/state.py` | `PostgresSyncStateStore`, `erp_sync.sync_state` | `tests/erp_pipeline/sync/` |
| **Checkpointing** | `sync/coordinator.py` | `SyncCoordinator.run` (never passes a failure) | same |
| Drift | `sync/drift.py` | `detect_drift`, `findings_from_diff`, `DriftSeverity` | same |
| Impact / propagation | `sync/impact.py`, `propagation.py` | — | same |
| Verification | `verification/cross_store.py`, `models.py` | `IntegrityVerificationService`, 18 `IntegrityCode`s | `test_cross_store.py`, `test_record_integrity.py` |
| Process cases | `process/case_builder.py` | `build_case`, `build_process_model`, `apply_process_model` | `tests/erp_pipeline/process/` |
| Process contracts | `process/models.py` | `ProcessCase`, `ProcessModel`, `EventLogConfig` | same |
| **Response detection** | `response_adaptation/detector.py` | `detect_response_type` | `test_detection_and_structured.py` |
| **Envelope unwrapping** | `response_adaptation/structured.py` | `unwrap_payload`, `count_leaf_fields`, `infer_response_schema` | same |
| **Query relevance** | `response_adaptation/relevance.py` | `RelevanceScorer.rank`, `QUERY_INTENT_TERMS`, `infer_identity_field` | `test_relevance_and_budgets.py` |
| **Budgets** | `response_adaptation/formatter.py` | `build_payload`, `apply_budget_to_decisions` | same |
| **Assets + SSRF** | `response_adaptation/assets.py` | `AssetAdapter`, `validate_asset_url`, `UrlSafetyPolicy` | `test_assets_and_url_safety.py` |
| **Adaptation service** | `response_adaptation/service.py` | `ResponseAdaptationService.adapt`, `_allowed_headers` | `test_service_and_api.py` |
| **Phase 14 evaluation** | `response_adaptation/evaluation.py` | `build_cases`, `evaluate`, `run_ablation`, `field_present` | `scripts/evaluate_response_adaptation.py` |
| Job planning | `orchestration/planner.py` | `PipelinePlanner`, `JobType`, stage tuples | `tests/erp_pipeline/orchestration/` |
| Job store | `orchestration/job_store.py` | `erp_orchestration.jobs`, `job_stages` | same |
| Record store | `orchestration/record_store.py` | `erp_runtime.canonical_records` | `GET /v1/records/{id}` |
| API app | `api/main.py` | `create_app`, exception handlers, router registration | `test_api_contract.py` |
| API security | `api/security.py` | `requires_key`, `keys_match`, `redact` | same |
| Error contract | `api/responses.py` | `ERROR_STATUS`, `error_body`, `failure` | same |
| Data routes | `api/routers_data.py` | uploads, specs, schemas, mappings, jobs, search, records | `test_search_resolution_and_filters.py` |
| **Adaptation route** | `api/routers_adaptation.py` | `adapt_response`, `get_adaptation_service` | `test_service_and_api.py` |
| Serialization | `api/serialization.py` | `schema_response`, `relationship_response` | `test_schema_contract_fields.py` |
| Bootstrap | `runtime/bootstrap.py` | `bootstrap_all`, `OWNED_SCHEMAS` | `test_bootstrap_completeness.py` |
| Runtime persistence | `runtime/persistence.py` | uploads, sources, mapping drafts | `test_runtime_hardening.py` |
| Frontend client | `frontend/src/api/client.ts` | `ApiClient.uploadCsv/uploadDocument` | `client.test.ts`, `safety.test.ts` |
| Frontend page | `frontend/src/pages/Upload.tsx` | `UploadPage`, `DropBox` | — |
| BPI demo | `scripts/demos/run_bpi2020_demo.py` | generic framework over BPI data | — |

---

# PART 47 — FINAL SUMMARY

## COMPONENT STATUS

### What is fully complete

The entire backend pipeline, end to end, in both directions:

- Schema discovery over four database technologies, plus CSV/PDF/image ingestion
  and OpenAPI/Swagger/Postman contract parsing.
- A durable, versioned schema catalog with five PostgreSQL schemas.
- **Explainable canonical mapping** with measured benchmark results and a
  refusal mechanism.
- Deterministic transformation with stable quality codes, exact decimal money,
  and a surrogate-key refusal that protects record identity.
- AI representation and local 384-dimension embedding with skip-if-unchanged.
- **Policy-driven hot/warm/cold storage** with constraints-before-scoring,
  hysteresis, migration, audit trail and a measured benchmark.
- Semantic search with server-side filtering, refused-not-ignored filter
  validation, and resolvable canonical record ids.
- Incremental sync with a checkpoint that never passes a failure, and schema
  drift detection with impact analysis.
- Cross-store verification across 18 integrity codes.
- **Phase 14 response adaptation** with detection, structural unwrapping,
  canonical mapping reuse, deterministic query relevance, mandatory identity
  preservation, budgets, multimodal assets and 14 SSRF controls.
- A 23-operation REST control plane with API-key auth, request ids and a typed
  error contract.
- 2,943 passing tests, 0 failures, 0 errors.

### What is partially complete

- **Frontend** — upload only (2 of 23 operations), and cannot send an API key.
- **Process/case modelling** — fully implemented and tested, but has **no
  `JobType` and no endpoint**; unreachable over HTTP.
- **Verification** — no endpoint.
- **Document pipeline** — no TRANSFORM/VALIDATE stage; PDFs and images do not
  become type-converted canonical records.
- **SQL Server** — implemented, live verification deferred.

### What requires group integration

Members 1, 2 and 3 own governance, ERP execution and answer generation
respectively. **No code for any of them exists in this repository.** The
interfaces they would consume are implemented and documented (Parts 32–34); the
integrations themselves are not.

### Strongest research contributions

1. **Explainable ERP schema mapping** — strongest measured evidence
   (top-1 1.0, correct-refusal 1.0, alias-independent 1.0 on 18 labels).
2. **ERP-aware adaptive response transformation** — most novel mechanism and
   most rigorous experimental design (two baselines, one ablation, three named
   failures left unfixed).
3. **Policy-driven tiered vector storage** — strongest measurement discipline;
   the contribution is the *explainability* of the routing, not tiering itself.

### Experimental evidence that exists

| Artifact | Contents |
|---|---|
| `artifacts/response_adaptation_evaluation.json` | 68 cases, 3 methods, per-category, per-case, ablation, named limitations |
| `artifacts/tiered_storage_benchmark.json` | 500 vectors, latency/recall/footprint/cost with an explicit `claim_safety` block |
| `tests/erp_pipeline/mapping/test_mapping_benchmark.py` | 68 hand labels (60 positive, 8 negative), reproducible on demand |
| `artifacts/openapi_contract_snapshot.json` | 23-operation generated contract, regenerated by a test |

### What should be demonstrated

Phase 14 response adaptation (needs no infrastructure), the mapping benchmark,
the SSRF refusal matrix, and — if PostgreSQL is available — the CSV upload →
schema → mapping flow. Use **"What is E002's date of birth?"** rather than
"find details".

### What should NOT be claimed

- Do **not** claim the component calls ERP APIs.
- Do **not** claim it uses or contains an LLM.
- Do **not** claim the canonical model is comprehensive — it has three entities.
- Do **not** present recall 0.9799 as beating the baselines; it does not.
- Do **not** present storage recall 0.15 as a retrieval-quality result.
- Do **not** present cost multipliers as money.
- Do **not** claim the frontend demonstrates the pipeline.
- Do **not** claim process/case is reachable over HTTP.
- Do **not** claim Member 1/2/3 integration exists.
- Do **not** claim generalisation from synthetic, single-annotator corpora.

---

## FINAL RECOMMENDATION

# **READY FOR FINAL DEMO**

**Justification, strictly on evidence:**

- The complete backend pipeline is implemented and passes 2,943 tests with zero
  failures and zero errors.
- Three research mechanisms exist, all with **measured, reproducible artifacts**,
  and all with limitations documented rather than hidden.
- The newest contribution (Phase 14) runs with **no infrastructure whatsoever**,
  so a demonstration cannot be blocked by an unavailable database.
- The component's boundaries are enforced in code, not merely asserted, and it
  self-declares its own limitations through `/v1/capabilities`.

**Conditions attached to that recommendation:**

1. Demonstrate through **`/docs` (Swagger UI) or `curl`**, not the frontend —
   the frontend covers 2 of 23 operations.
2. Lead with the **honest headline** on Phase 14 before a panel reads it.
3. Disclose the **`E002` → `email` tokenisation defect** rather than letting a
   panel find it.
4. State clearly that the **E002 employee scenario runs the passthrough path**
   because the canonical model has no employee entity — and use an **invoice**
   to show canonical mapping.
5. If asked about process/case, state plainly that it is implemented and tested
   but has **no HTTP surface**.

**The work is not "finished" in the sense of having no gaps — it is finished in
the sense that every gap is known, located, and written down.** That is the
stronger position to defend.
