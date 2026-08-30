# ERP Data Transformation Pipeline — Architecture

**Service:** ERP Data Transformation API · **Audited:** 2026-08-29

Diagrams show only stages, tiers and endpoints that exist in the current
implementation.

---

## 1. High-level system architecture

```mermaid
flowchart TB
    subgraph EXT["External systems"]
        DB[("Legacy ERP databases<br/>PostgreSQL · MySQL<br/>SQL Server · MongoDB")]
        CSVF["CSV exports"]
        DOC["PDF / image uploads"]
        URLS["Declared asset URLs"]
        BR["ERPBridge — raw ERP responses"]
    end

    subgraph SVC["ERP Data Transformation API — 24 operations"]
        direction TB
        ADM["Admission & discovery"]
        PREP["Transformation & multimodal preparation"]
        REPR["AI representation build"]
        EMB["Embedding — 384-D local"]
        ROUTE["Tier routing & lifecycle"]
        RET["Retrieval & resolution"]
        ADAPT["Response adaptation"]
    end

    subgraph PERSIST["Persistence"]
        PG[("PostgreSQL<br/>authoritative state<br/>5 schemas · ~20 tables")]
        HOT[("Qdrant Cloud<br/>erp_vectors_hot")]
        WARM[("Qdrant Cloud<br/>erp_vectors_warm")]
        COLD[("COLD archive<br/>gzip + AES-256-GCM<br/>filesystem, NOT Qdrant")]
    end

    DB --> ADM
    CSVF --> ADM
    DOC --> PREP
    URLS --> PREP
    BR --> ADAPT
    ADM --> PREP --> REPR --> EMB --> ROUTE
    REPR --> PG
    ROUTE --> HOT & WARM & COLD
    HOT & WARM --> RET
    PG --> RET
    ADM --> PG
```

**Reading the diagram:** authoritative text goes to PostgreSQL; only vectors and
filterable metadata go to Qdrant. Retrieval consults both — Qdrant for
similarity, PostgreSQL for the authoritative answer.

---

## 2. Complete internal pipeline

```mermaid
flowchart TB
    IN["Input"] --> ADM{"Admission"}
    ADM -->|"registered source"| DISC["DISCOVER<br/>tables · columns · types · relationships"]
    ADM -->|"uncovered entity"| SNG["SOURCE_NATIVE_GUARD<br/>refuses entities the canonical model covers"]
    DISC --> MAP["MAP<br/>automatic · review · refusal"]
    MAP --> EXT["EXTRACT"]
    SNG --> EXT
    EXT --> TRA["TRANSFORM<br/>canonical OR source-native"]
    TRA --> VAL["VALIDATE"]
    VAL --> LOAD["LOAD"]
    LOAD --> AIB["AI_BUILD<br/>deterministic representations"]
    AIB --> MME["MULTIMODAL_EXTRACT<br/>BLOB · PDF · image · OCR · chunk"]
    MME --> PER["PERSIST_REPRESENTATIONS<br/>AES-256-GCM at CONFIDENTIAL+"]
    PER --> EMB["EMBED — 384-D"]
    EMB --> TR["TIER_ROUTE"]
    TR --> LC["LIFECYCLE_COMMIT"]
    LC --> DONE["Searchable"]

    EXT -.->|"failure"| PART["status = partial<br/>counters + warnings reported"]
    PER -.->|"no encryption key"| FAIL["fail closed — no vector written"]
```

`MULTIMODAL_EXTRACT` runs after `AI_BUILD` because `AI_BUILD` *assigns*
`context.representations`; producing document representations earlier would have
them overwritten.

---

## 3. Storage architecture

```mermaid
flowchart LR
    REC["EmbeddingRecord<br/>+ sensitivity + criticality"] --> ROUTER["StoragePolicyRouter"]
    ROUTER -->|"hot criticality"| HOT[("erp_vectors_hot<br/>float32 · in-memory<br/>Cosine · 384-D")]
    ROUTER -->|"aging"| WARM[("erp_vectors_warm<br/>int8 quantized · on-disk<br/>Cosine · 384-D")]
    ROUTER -->|"archival"| COLD[("COLD archive<br/>gzip → AES-256-GCM<br/>filesystem")]
    HOT <-->|"migration"| WARM
    WARM <-->|"rehydrate"| COLD

    TEXT["Authoritative representation text"] --> PGT[("PostgreSQL<br/>erp_runtime.ai_representations")]
    STATE["Tier state · lifecycle · jobs · catalog"] --> PGT
```

**Physical collections = storage tiers. Logical data kinds = metadata.**

There is no `employee_vectors`, `schema_vectors`, `document_vectors` or
per-dataset collection. Separation is achieved by 13 filterable metadata fields.

| | Filterable (13) | Provenance-only (3) |
|---|---|---|
| Identity | `business_key_name`, `business_key_value`, `parent_record_id`, `document_id` | |
| Classification | `content_kind`, `document_type`, `entity_type`, `entity_kind`, `sensitivity` | |
| Origin | `source_system_id`, `source_entity`, `source_field`, `schema_name` | |
| Document position | | `page_start`, `page_end`, `chunk_index` |

---

## 4. Search flow

```mermaid
sequenceDiagram
    participant C as Caller
    participant API as API
    participant E as Embedding (local)
    participant Q as Qdrant Cloud
    participant P as PostgreSQL

    C->>API: POST /v1/search {query, filters}
    API->>E: encode query
    E-->>API: 384-D vector
    API->>Q: filtered search — HOT
    API->>Q: filtered search — WARM
    Q-->>API: vector ids + scores
    API->>P: state lookup by vector id
    P-->>API: identity · provenance · sensitivity · is_current
    Note over API: re-check filters against state<br/>drop is_current = false
    API-->>C: hits (identity + provenance, NO text)
    C->>API: GET /v1/representations/{id}
    API->>P: load representation
    P-->>API: text (decrypted if needed)
    API-->>C: text + provenance + sensitivity
```

Two calls, deliberately. That separation is what keeps raw content out of the
vector store.

---

## 5. Response adaptation flow

```mermaid
sequenceDiagram
    participant BR as ERPBridge
    participant ERP as Legacy ERP
    participant API as API

    BR->>ERP: execute selected operation (ONCE)
    ERP-->>BR: raw response
    BR->>API: POST /v1/responses/adapt
    Note over API: detect (magic bytes) → unwrap →<br/>canonical map → relevance selection → budgets
    API-->>BR: llm_ready · assets · provenance ·<br/>transformation · warnings · success · partial
    Note over API: NO ERP call · NO retry ·<br/>NO credentials stored
```

For **binary** bodies `llm_ready` is `{}`, `partial` is `true`, and the text is
in `assets[0].text`. For **collections**, only the first record is adapted and a
warning names the total.

---

## 6. Document / OCR flow

```mermaid
flowchart LR
    UP["POST /v1/files/documents<br/>multipart + declared identity"] --> VAL{"validate identity"}
    VAL -->|"half a business key"| R422["422 refused"]
    VAL -->|"unknown sensitivity"| R422
    VAL -->|"ok"| DET["detect by magic bytes"]
    DET -->|"PDF"| PDF["PyMuPDF text extraction"]
    DET -->|"image"| OCR["Tesseract OCR"]
    DET -->|"unsupported"| R415["415 refused — never guessed"]
    PDF --> CH["chunk"]
    OCR --> CH
    CH --> ATT["DocumentAttachment<br/>parent | source_field | chunk_id"]
    ATT --> JOB["automatic document_pipeline job"]
    JOB --> VEC["representation → embedding → Qdrant"]
    VEC --> S["searchable + resolvable"]
```

Extraction is **entirely in memory** — no temporary files are written.

---

## 7. CSV flows — two different things

```mermaid
flowchart TB
    subgraph SCHEMA["FLOW A — schema (automatic)"]
        A1["POST /v1/files/csv"] --> A2["sampled schema inference"]
        A2 --> A3["schema catalog → schema_id"]
        A3 --> A4["schema representation"]
        A4 --> A5["automatic schema_pipeline job"]
        A5 --> A6["schema vectors — content_kind = schema"]
    end

    subgraph ROWS["FLOW B — business rows (explicit, never automatic)"]
        B1["POST /v1/sources — register"] --> B2["POST /v1/mappings/suggest"]
        B2 --> B3["review / approve"]
        B3 --> B4["POST /v1/jobs<br/>structured_pipeline OR source_native_pipeline<br/>options.key_fields = [...]"]
        B4 --> B5["record vectors — content_kind = structured_record"]
    end

    SCHEMA -.->|"does NOT trigger"| ROWS
```

**A CSV upload never indexes business rows.** Structure is not business data,
and schema indexing must not become a backdoor around mapping review.

Rows also require a **declared key**: an inferred CSV schema has no primary key,
and the extractor's fallback is the row *number* — a position, not an identity.
Rows are refused with `"no usable record identity"` until `key_fields` is
supplied.

---

## 8. Synchronisation and lifecycle

```mermaid
flowchart TB
    T["SyncScheduler.tick()<br/>ships DISABLED"] -->|"interval elapsed"| L["Lease<br/>single-process or Postgres"]
    L --> W["Watermark strategy<br/>timestamp · monotonic_id · composite<br/>source_cursor · content_hash"]
    W --> EC["EXTRACT_CHANGED"]
    EC --> P["pipeline"]
    P --> LC["LIFECYCLE_COMMIT"]
    LC --> CUR["mark current version"]
    CUR --> SUP["supersede previous — is_current = false"]
    SUP --> DEL["delete stale vector"]
    DEL -.->|"delete fails"| BACK["cleanup backlog<br/>NOT a wrong answer — is_current suppresses it"]
    P -.->|"any record failed"| SW["safe_watermark<br/>advances only past successes"]
```

**Polling, not change-data-capture.** Freshness is bounded by the configured
interval plus processing latency — measured at 5.0 s + 0.877 ms median.

---

## 9. Four-component runtime flow

```mermaid
sequenceDiagram
    participant U as User
    participant FE as Frontend
    participant GOV as Policy / Governance
    participant BR as ERPBridge
    participant ERP as Legacy ERP
    participant API as ERP Data Transformation API

    U->>FE: request
    FE->>GOV: is this permitted?
    GOV-->>FE: ALLOW / DENY / ALLOW_WITH_CONDITIONS
    alt ALLOW
        FE->>BR: execute operation
        BR->>ERP: live API call
        ERP-->>BR: raw response
        BR->>API: POST /v1/responses/adapt
        API-->>BR: AI-ready content
        BR-->>FE: result
    else DENY
        FE-->>U: refused — ERP never called, API never involved
    end

    Note over FE,API: Indexed path runs independently:<br/>FE → API upload / search / resolve
```

The governance component does **not** call this service at runtime. It may read
`GET /v1/capabilities` and `GET /v1/schemas/{id}` at design time.

---

## 10. Package responsibilities

| Package | Responsibility |
|---|---|
| `api/` | FastAPI control plane: routers, request/response models, API-key middleware, OpenAPI security scheme |
| `runtime/` | Composition root — settings, service assembly, schema bootstrap, ASGI application |
| `connectors/` | Database drivers: PostgreSQL, MySQL, SQL Server, MongoDB |
| `discovery/` | Relational introspection, MongoDB structure inference, profiling |
| `catalog/` | Schema catalog persistence and versioned snapshots |
| `mapping/` | Explainable source-to-canonical field mapping with refusal |
| `transformation/` | Canonical and source-native record transformation |
| `ingestion/` | CSV, PDF, image, OCR, binary assets, remote assets, content detection |
| `ai/` | Representations, chunking, schema representations, embedding service |
| `orchestration/` | Jobs, stages, planner, representation store, lifecycle, scheduler, multimodal coordination |
| `storage/` | HOT/WARM/COLD tiers, policy router, filters, tier state, migration |
| `sync/` | Incremental synchronisation, drift detection, watermarks, propagation |
| `response_adaptation/` | Live ERP response adaptation for ERPBridge |
| `schemas/` | Canonical models, enums, identity, sensitivity resolution |
| `api_specs/` | OpenAPI and Postman collection parsing (design-time) |
| `process/` | Process and case modelling support |
| `verification/` | Cross-store verification helpers |
