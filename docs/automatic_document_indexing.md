# Phase 6 — Automatic Uploaded Document Indexing

**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline for Legacy ERP Systems
**Student:** IT22267290 · Member 4 · R26-SE-034
**Status:** implemented and verified
**Date:** 2026-08-25

---

## 1. Problem

Phases 1–5 made a database BLOB fully retrievable: extract, chunk, persist,
embed, filter, resolve. An **uploaded** document got as far as extraction and
stopped.

```
POST /v1/files/documents  →  validate → extract/OCR → response → STOP
```

Nothing was broken; something was simply missing. A second, manual
`POST /v1/jobs` had to happen for the document to become searchable, and
nothing in the upload response said so. A caller who uploaded a certificate and
then searched for it found nothing, with no error to explain why — the worst
kind of gap, because it looks like a bug in retrieval rather than a step nobody
performed.

## 2. Previous manual workflow

```
POST /v1/files/documents   →  upload_id, document_id, extraction status
        ↓
   (a human, or client code, has to know to do this)
        ↓
POST /v1/jobs {"job_type": "document_pipeline", "upload_id": …}
        ↓
INGEST → AI_BUILD → PERSIST_REPRESENTATIONS → EMBED → TIER_ROUTE
```

## 3. New automatic workflow

```
POST /v1/files/documents  (+ optional ERP identity fields)
        ↓  validate, store, extract/OCR
        ↓  submit the SAME document_pipeline job
        ↓
INGEST → AI_BUILD → PERSIST_REPRESENTATIONS → EMBED → TIER_ROUTE
        ↓
201 { upload_id, document_id, …, index_job_id, indexing_status }
        ↓
GET /v1/jobs/{index_job_id}     authoritative lifecycle
POST /v1/search                 filtered retrieval
GET  /v1/representations/{id}   the actual text
```

The manual route still exists and still works. Phase 6 removed a **required**
step, not an available one.

## 4. Existing components reused

Nothing new indexes anything. Phase 6 is orchestration:

| reused | from |
|---|---|
| `document_pipeline` plan and every stage | Phase 13 |
| PDF / image / OCR extraction | Phase 1 |
| `chunk_document`, `document_to_representations` | Phase 11 |
| `DocumentAttachment`, `attached_document_to_representations` | **Phase 3** |
| `PERSIST_REPRESENTATIONS` ordering | Phase 5 |
| identity metadata and filters | Phase 4 |
| `OrchestrationService.submit` | Phase 13 |

## 5. Job orchestration decision

The upload endpoint calls **`service.submit(...)`** — the same service-layer
function `POST /v1/jobs` calls — rather than issuing an HTTP request to its own
API. A service that calls itself over the network adds a socket, a
serialization round trip, and a second failure mode to a call that is one
function away.

`submit()` plans before persisting, so an impossible request is refused
synchronously rather than becoming a failed job.

## 6. Sync vs async behaviour

**Asynchronous, matching the existing job engine.** The upload returns as soon
as extraction is done and the job is enqueued; the job runs on the bounded
executor. Blocking the upload request until embedding finished would tie a
request socket to a model run, which is exactly what `POST /v1/jobs` returns
202 to avoid.

`indexing_status` is a **snapshot** taken as the response is written, not a
promise. The job is re-read from the store after `submit()` returns, because
`submit()` hands back the job as it was *created*: under the inline executor the
work has already finished by then, and reporting the stale object would say
"pending" about a job that had succeeded — or, worse, about one that had failed.

## 7. Upload response contract

Additive. Every pre-existing field keeps its name and meaning.

```json
{
  "upload_id": "upl_…", "filename": "scan.pdf", "content_hash": "…",
  "size_bytes": 24680, "document_id": "…", "file_type": "pdf",
  "page_count": 1, "extraction_status": "extracted", "ocr_used": false,

  "index_job_id": "job_…",
  "indexing_status": "succeeded",
  "indexing_error": null,

  "warnings": []
}
```

**There is deliberately no `searchable` field.** A document is searchable only
once its representation is persisted, its embedding generated, and its vector
routed to a searchable tier — none of which has happened when this response is
written. Reporting `searchable: true` at upload time would be a lie the caller
could not detect.

`index_job_id` and `indexing_status` are both `null` when indexing did not
start; `indexing_error` then says why, and the same message is appended to
`warnings`.

## 8. Metadata upload contract

Six optional multipart form fields, all `Optional[str] = Form(default=None)`:

```
source_system_id   source_entity      parent_record_id
business_key_name  business_key_value document_type
```

A client that posts only a file — which is what the existing frontend does —
behaves exactly as before. `required` in the generated OpenAPI is `["file"]`,
and a test asserts it.

**Explicit typed fields, not a metadata blob.** An open JSON field here would
let a caller put an Authorization header, a connection string or a local path
into a payload that is persisted with the job and echoed back through the API.
Values are additionally length-capped (200 chars) and rejected if they contain
credential or connection-string markers.

## 9. Identity handling

### Declared, never inferred

`EMP002_birth_certificate.jpg` uploaded without metadata carries **no** business
key. Not from the filename, not from the OCR text, not from a model.

The reason is that `business_key_value` is the same field Phase 2 fills from a
declared primary key and Phase 4 filters on exactly. A guess in that field makes
a search for EMP002 return documents that merely *look* like EMP002's,
indistinguishable from ones that provably are — and the caller cannot tell which
kind they got.

Filename parsing is a reasonable **UI convenience** for a future phase: let the
browser pre-fill the form, let a human confirm it, and the value arrives
declared. It is not an authoritative backend mechanism.

### The pair rule

`business_key_name` and `business_key_value` are one declaration in two fields.
Half of it names nothing a filter can use, so half is **refused with 422** —
before the file is stored, so a bad request leaves no orphan upload behind.

### `parent_record_id`

Preserved exactly when supplied. **Never derived** from the business key: an
`employee_id` is not a canonical record id, and a manufactured one would be
indistinguishable from a real reference to whoever tried to resolve it. When
not supplied it is reported as `null`.

### `document_type`

Caller-supplied or `null`. `content_kind` remains `document_chunk` either way.

## 10. Same content, same association

Content-addressed identity does the work:

```
upload bytes X for EMP002  →  document_id D, representation R
upload bytes X for EMP002  →  document_id D, representation R   (upsert)
```

One corpus entry, two job records. Jobs are execution history — two uploads
genuinely happened — while the corpus holds one document, because there is one
document. Measured: **0 duplicate semantic entries**.

## 11. Same content, different association

This is where the upload path could have quietly reintroduced the Phase 3
collision, and the first implementation did.

Both employees' uploads produced the same content-addressed chunk id, so the
same `representation_id`, so the same vector — one employee's certificate
overwriting the other's. Verified before the fix:

```
EMP002: rep=…62058402068d.c00000.56dc47b2
EMP003: rep=…62058402068d.c00000.56dc47b2
COLLISION: True
```

**Two identity regimes**, chosen by whether anything was declared:

| upload | identity | why |
|---|---|---|
| no ERP identity | **content** (`document_to_representations`) | the same policy PDF uploaded twice is the same document and should occupy one representation |
| ERP identity declared | **attachment** (`attached_document_to_representations`) | two employees issued one certificate are two attachments, exactly as with a database BLOB |

`DocumentAttachment` gained an additive `attachment_scope`: the discriminator
that keeps two attachments apart when neither declared a parent record. It
defaults to `parent_record_id`, so Phase 3 behaves identically, and the upload
path supplies `business_key_name=business_key_value` instead. Critically, it is
**not** written to `parent_record_id`, which stays `null` — a key discriminator
and a record reference are different things.

After the fix: distinct representation ids, distinct vectors, shared
`document_id`, `parent_record_id` null. **0 association collapse.**

## 12. Extraction reuse

**Extraction now happens once. It previously happened twice.**

The audit found `run_ingest` calling `services.ingest_upload(upload_id)`
unconditionally, while the upload endpoint had already extracted the same file.
`ingest_upload` was *already* caching its result in `upload_results`; only the
read was missing — the CSV path consulted the cache, the document path did not.

The fix is three lines: consult the cache. Uploaded bytes are immutable and
content-addressed, so a re-parse produces an identical result. Without it,
enabling automatic indexing would have OCR'd every scanned certificate twice —
the single most expensive operation in the pipeline, repeated for no gain.

`ingest_upload(upload_id, reuse=False)` forces a fresh parse for a caller that
wants one. A test asserts the extractor is invoked exactly once per upload.

## 13. Representation persistence ordering

Unchanged from Phase 5, and asserted for the automatic path:

```
INGEST → AI_BUILD → PERSIST_REPRESENTATIONS → EMBED → TIER_ROUTE
```

Both structurally (`test_the_document_plan_persists_before_it_embeds`) and
observationally, by reading the succeeded-stage order off a real automatic job.
The invariant holds: a searchable vector always has resolvable content.

## 14. Failure and retry semantics

Three failures, three distinct reports:

| what failed | result |
|---|---|
| upload / extraction | existing 4xx; no job, no representation, no vector |
| job **scheduling** | upload succeeds `201`; `index_job_id: null`, `indexing_error` explains, warning names the manual route |
| job **execution** | upload succeeds `201`; `indexing_status: "failed"`; job reports failure; nothing searchable |

A scheduling failure does not discard the upload. The bytes are stored and the
extraction succeeded; both are still worth returning, and the manual route
remains available to start indexing.

**Retry uses the existing job architecture** — `POST /v1/jobs/{job_id}/retry`,
or a fresh `POST /v1/jobs` with the same `upload_id`. No second retry subsystem
was built.

## 15. CSV behaviour preservation

**Unchanged, deliberately.** `POST /v1/files/csv` still stops at schema
inference and catalog publication. Automatic indexing would route around the
mapping review and refusal architecture that makes structured transformation
explainable — a caller would get vectors for an ambiguous mapping nobody
approved.

`test_a_csv_upload_still_stops_at_schema_inference` asserts no job, no
representation, no vector, and no `index_job_id` on the response. Measured:
**CSV started an index job: False**.

## 16. Frontend compatibility

- File-only upload works unchanged (test asserts every pre-existing response
  field is still present).
- All new form fields are optional; only `file` is required.
- `frontend/src/api/types.ts` gained the three new response fields as optional,
  so existing code compiles untouched.
- **No UI was built.** Phase 11 owns that.

## 17. Files changed

**New (3):**

| file | purpose |
|---|---|
| `orchestration/document_identity.py` | the declared-identity contract and its validation |
| `scripts/evaluate_automatic_document_indexing.py` | mini-evaluation |
| `docs/automatic_document_indexing.md` | this report |

**Modified (6):**

| file | change |
|---|---|
| `api/routers_data.py` | optional identity form fields, `_start_document_indexing`, automatic submit |
| `api/schemas.py` | `index_job_id`, `indexing_status`, `indexing_error` |
| `orchestration/service.py` | `ingest_upload` cache read; identity-aware `build_document_representations`; `_upload_attachment_scope` |
| `orchestration/stages.py` | `run_ai_build` passes the declared identity |
| `ai/attached_documents.py` | additive `attachment_scope`; optional `parent_record_id` |
| `frontend/src/api/types.ts` | the three new response fields |

## 18. Tests added

`tests/erp_pipeline/api/test_automatic_document_indexing.py` — **36 tests**
covering required tests A–O: the headline loop, image/OCR indexing, filename
and OCR identity refusal, the business-key pair rule, credential rejection,
same-file/same-association idempotency, same-file/different-employee
separation, uploads without identity, corrupt documents, scheduling failure,
execution failure, CSV preservation, the manual route, Phase 5 ordering,
single extraction, content safety, and backward compatibility.

**One existing behaviour was changed deliberately and is covered:**
`DocumentAttachment.parent_record_id` became optional, and `to_metadata` now
omits it when absent rather than writing `null`. Phase 3 and Phase 4 suites pass
unchanged because a BLOB attachment always declares one.

## 19. Mini-evaluation

Seven uploads: a text PDF, a readable image, EMP002's certificate, the same
certificate for EMP003, a generic document, a repeated EMP002 upload, and a
corrupt file.

```
uploads attempted            7
uploads accepted             6      (corrupt → 422)
automatic jobs created       6
jobs completed               6
jobs failed                  0
MANUAL job calls required    0

wrong identity matches       0      duplicate semantic entries   0
wrong document types         0      unresolvable hits            0
raw / base64 leakage         0      text in Qdrant payload       False
CSV started an index job     False

upload → searchable  median 28.8 ms   p95 47.5 ms   max 1142.4 ms   (n=6)

GATES: PASS
```

Artifact: `artifacts/automatic_document_indexing_evaluation.json`.

**Reading the latency honestly.** These are in-process measurements with an
inline executor and a deterministic test model — this is **automatic document
indexing latency**, not real-time synchronisation, and not production latency
with a real embedding model, Qdrant round trip, and bounded thread pool. With
n=6 a "p95" is really "the second slowest", which is why the **max** is
reported: the 1142 ms case is the OCR'd image, an order of magnitude above the
median, and it is the number that describes the worst a user waits.

Six accepted uploads produced **five** representations — EMP002's repeat
correctly upserted rather than accumulating.

## 20. Targeted results

`api`, `ingestion`, `ai`, `orchestration`, `storage`, `runtime`, `sync`:

```
1287 passed, 39 skipped, 0 failed in 324.89s
```

## 21. Full regression

| | collected | passed | failed | errors | skipped |
|---|---|---|---|---|---|
| baseline (after Phase 5) | 3293 | 3230 | 0 | 0 | 63 |
| after Phase 6 | **3329** | **3266** | **0** | **0** | **63** |

`3266 passed, 63 skipped, 30 warnings in 377.38s (0:06:17)`

The **+36** is exactly the new test file — nothing was auto-parametrized and no
existing test count changed.

**Skips are unchanged at 63.** No test was skipped to avoid a failure, and the
OCR-dependent image test loads `.env` itself rather than depending on collection
order, so it runs rather than skipping.

Notably, **no existing test needed editing.** Phase 6 changed
`DocumentAttachment.parent_record_id` to optional and made `to_metadata` omit it
when absent, and every Phase 3 and Phase 4 assertion still passed unchanged —
because a database BLOB attachment always declares a parent.

## 22. Existing artifact impact

| artifact | status |
|---|---|
| `tiered_storage_benchmark.json` | unchanged |
| `response_adaptation_evaluation.json` | unchanged |
| `multimodal_extraction_evaluation.json` | unchanged |
| `identity_retrieval_evaluation.json` | unchanged |
| `representation_resolution_evaluation.json` | unchanged |
| `openapi_contract_snapshot.json` | regenerated by its own test, as on every run; now carries the new form fields and response fields |
| `automatic_document_indexing_evaluation.json` | **new** |

No prior artifact was re-run or overwritten. Phases 3–5 are re-verified by their
own suites inside the full regression, precisely so their artifacts stay
untouched.

## 23. Known limitations

1. **`upload_results` is an unbounded in-process cache.** It was already
   unbounded before Phase 6 (every ingest wrote to it); Phase 6 makes it load-
   bearing by reading from it. A long-running server that uploads many large
   documents holds their extracted text in memory, and the cache does not
   survive a restart — after which the job re-extracts, correctly but slowly.
   An eviction policy is worth a future phase.
2. **Extraction reuse depends on same-process execution.** With a distributed
   executor the job would miss the cache and re-extract. Correct, not free.
3. **No cross-request job idempotency.** Two identical uploads create two job
   records. The corpus is idempotent; the job history is not, by design.
4. **Sensitivity defaults to INTERNAL** for uploaded documents, as before.
   Phase 6 adds no inference — classifying a birth certificate as RESTRICTED is
   a decision for the security phase, not a side effect of an upload endpoint.
5. **No filename pre-fill UI.** Deliberate (§9); Phase 11 may add it as a
   confirmed convenience.
6. **Latency figures are in-process** (§19).
7. **A blank image still uploads successfully** with `201` and produces no
   chunk, so nothing becomes searchable. That is existing Phase 3 behaviour —
   nothing is fabricated — but the upload response does not currently
   distinguish "indexed nothing" from "indexed something".

## 24. Explicit Phase 7+ exclusions

Confirmed absent:

```
schema AI representations / embeddings / vectors / content_kind=schema
semantic schema search
database URL or document-reference fetching
incremental-sync scheduler
stale old-document cleanup
new sensitivity inference or classification
frontend search screen / schema browser / four-member workflow UI
LLM answer generation
```

**No new endpoint** was added — `POST /v1/files/documents` gained optional
fields and new response fields. **No new Qdrant collection.**

## 25. EMP002 readiness

```
EMP002 DB-BLOB backend flow                 WORKING
EMP002 uploaded-document backend flow       WORKING
EMP002 exact filtered search                WORKING
EMP002 document content resolution          WORKING

Manual indexing step after upload           REMOVED FROM NORMAL WORKFLOW
Manual job route                            STILL AVAILABLE
CSV controlled mapping workflow             PRESERVED
```

```
POST /v1/files/documents  (file + employee_id=EMP002 + birth_certificate)
        ↓  one call, no second job
POST /v1/search  {business_key_value: EMP002, document_type: birth_certificate}
        ↓
GET /v1/representations/{id}
        ↓
"BIRTH CERTIFICATE … Name: Nimal Silva"
```

---

*See also: [Phase 5 — Representation Content Resolution](representation_content_resolution.md),
[Phase 4 — Identity-Aware Retrieval](identity_aware_retrieval.md),
[Phase 3 — Database BLOB Multimodal Pipeline](database_blob_multimodal_pipeline.md).*
