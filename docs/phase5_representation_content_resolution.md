# Phase 5 — Representation Persistence and Document Content Resolution

**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline for Legacy ERP Systems
**Student:** IT22267290 · Member 4 · R26-SE-034
**Status:** implemented and verified
**Date:** 2026-08-25

---

## 1. Problem

After Phase 4 the system could find exactly the right chunk of EMP002's birth
certificate — correct employee, correct document type, correct page and chunk —
and had no way to tell anyone what it said.

```
POST /v1/search  →  representation_id, document_id, parent_record_id,
                    business identity, document type, page/chunk, score, tier
                 →  STOP
```

That is a retrieval system that returns a citation without the quotation. The
component's stated purpose is producing **AI-ready context** from legacy ERP
data; an identifier is not context.

## 2. Architecture chosen

```
AIRepresentation
     ├──────────────→  erp_runtime.ai_representations   (authoritative text)
     ↓
EmbeddingRecord  →  Qdrant vector  (identity + safe metadata only)
     ↓
POST /v1/search              which representations are relevant?
     ↓  representation_id
GET  /v1/representations/…   what does this one actually say?
```

Two stores, two questions, one answer each:

| store | answers |
|---|---|
| Qdrant HOT/WARM/COLD | *which* representations are relevant |
| `erp_runtime.ai_representations` | *what* a representation contains |

Collapsing them — putting `text_for_ai` into the vector payload — would have
been the shortest path and was rejected in §4.

## 3. Where representation text previously disappeared

The audit found exactly one place, and it was not subtle:

```
AIRepresentation.text_for_ai
     ↓
EmbeddingService._record()      reads the text, embeds it, and returns an
                                EmbeddingRecord that HAS NO TEXT FIELD   ✗
     ↓
EmbeddingRecord  →  StorageRecordMetadata  →  Qdrant payload
```

The only other copy lived on `PipelineContext.representations`, which exists for
the duration of one job and is garbage as soon as it finishes. Nothing was
losing the text through a bug; nothing had ever been asked to keep it.

So the smallest correct intervention is to persist the representation **while
the pipeline still holds it** — after it is built, before it is embedded.

## 4. Representation persistence design

### Why not just put the text in Qdrant

It would have taken one line: `_payload_for` already has an `include_text`
flag, defaulted to `False` since Phase 11. Turning it on would make the vector
index a second copy of the entire corpus, put extracted document text inside
every payload a search touches, and create two sources of truth that can
disagree after a re-extraction. The flag's own docstring says as much — *"it
would double the storage and turn the index into a second copy of the corpus"* —
and Phase 5 leaves it off. A test asserts no text reaches the payload.

### Why the table mirrors the model

`AIRepresentation` has four first-class fields plus `metadata` and `content`.
The table stores exactly that, so **no field is duplicated between a column and
the JSON** and there is no way for the two to disagree. Everything the API
reports that is not a first-class field is read back out of `metadata_json`.

## 5. Database schema / store

```sql
CREATE TABLE IF NOT EXISTS erp_runtime.ai_representations (
    representation_id      TEXT        PRIMARY KEY,
    entity_type            TEXT        NOT NULL,
    text_for_ai            TEXT        NULL,
    content_hash           TEXT        NULL,
    content_json           TEXT        NOT NULL DEFAULT '{}',
    metadata_json          TEXT        NOT NULL DEFAULT '{}',
    source_record_ids_json TEXT        NOT NULL DEFAULT '[]',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```

**Field placement decisions, and why:**

| field | where | why |
|---|---|---|
| `representation_id` | column, PK | the only lookup key Phase 5 has |
| `entity_type` | column | a first-class field on the model |
| `text_for_ai` | column | the payload; burying it in JSON would make every read parse the whole document |
| `content_hash` | column | integrity checkable without parsing JSON |
| `content` / `metadata` / `source_record_ids` | JSON columns | the model's own open-ended parts; a column-per-field table would need migrating every time a phase adds provenance |
| `content_kind`, `parent_record_id`, `business_key_value`, `document_type`, `page_start`, `page_end`, `chunk_index` | **metadata JSON, not columns** | Phase 5 looks up by primary key only. Promoting them is an additive migration the moment a query needs them — the same move Phase 4 made on the tier-state table |
| the source bytes | **not persisted anywhere** | a schema that cannot hold a blob cannot leak one |

`text_for_ai` is NULLABLE deliberately: an image OCR read nothing from still has
identity and provenance worth resolving, and storing the row anyway is what
keeps *"never persisted"* distinguishable from *"genuinely empty"*.

Lives in `erp_runtime` beside `canonical_records`, created by the same
`bootstrap_all` path (`CREATE TABLE IF NOT EXISTS`, idempotent, no destructive
reset). `InMemoryRepresentationStore` and `PostgresRepresentationStore` share
one contract: `upsert` / `upsert_many` / `get` / `get_many` / `delete` / `count`.

## 6. Identity model

Lookup is by **`representation_id`**, never `document_id`. That is load-bearing
rather than stylistic:

```
EMP002.birth_certificate = bytes X   ┐ same document_id
EMP003.birth_certificate = bytes X   ┘ different representation_id
```

Resolving by `document_id` would collapse the association Phase 3 built and
Phase 4 made queryable, handing back whichever employee's row was written last.
Phase 5 preserves it: same text, same `document_id`, different
`representation_id`, different `parent_record_id`.

## 7. Content / hash integrity

`AIRepresentation.content_hash` already hashes `(representation_id,
text_for_ai, content)` via `representation_content_hash`. Phase 5 reuses it
rather than inventing a second convention: the stored hash is the
representation's own, so a row read back can re-derive and verify it
(`test_a_restored_representation_still_verifies_its_own_hash`).

**Retrievable text is embedded text.** The store writes `text_for_ai`
unmodified — no second truncation — because the text was already bounded
upstream by `ChunkingConfig.max_characters` (800) and `RepresentationConfig`.
Re-truncating at the storage boundary would silently break the property that
what a caller reads is what the model saw.

## 8. Write lifecycle

A new stage, `PERSIST_REPRESENTATIONS`, in **all four** pipeline tails:

```
structured      … AI_BUILD → MULTIMODAL_EXTRACT → PERSIST → EMBED → TIER_ROUTE
source-native   … AI_BUILD → MULTIMODAL_EXTRACT → PERSIST → EMBED → TIER_ROUTE
document        … AI_BUILD →                      PERSIST → EMBED → TIER_ROUTE
incremental     … AI_BUILD →                      PERSIST → EMBED → TIER_UPDATE
```

Placed after the representations are built and **before anything is embedded**,
because `TIER_ROUTE` is what makes a vector searchable. Persisting after it
would leave a window in which a search returns a hit nobody can resolve — the
exact defect Phase 5 exists to close.

### The failure window, stated honestly

PostgreSQL and Qdrant are two systems and this pipeline has **no distributed
transaction**. Phase 5 does not claim atomicity it does not have. What it
guarantees is the **order**, and therefore the **direction** of the failure:

| failure | result | severity |
|---|---|---|
| persist succeeds, embed/store fails | text with no vector | harmless — returns no wrong answers; repaired by re-running the job |
| persist fails | job reports it; nothing downstream indexed | safe |
| the inverse (vector with no text) | **structurally prevented by the ordering** | — |

A deployment with no representation store configured runs exactly as it did
before Phase 5: the stage records that it stored nothing, notes that resolution
will be unavailable, and does not fail a previously-valid job.

## 9. Search → resolution workflow

```
POST /v1/search
  {"query": "birth certificate details",
   "filters": {"business_key_name": "employee_id",
               "business_key_value": "EMP002",
               "document_type": "birth_certificate",
               "content_kind": "document_chunk"}}
        ↓  hits[0].representation_id
GET /v1/representations/ai:document:erp_legacy_hr_employees_emp002_…
        ↓
  {"text": "BIRTH CERTIFICATE\nRegistrar General…", "business_key_value": "EMP002",
   "parent_record_id": "erp:legacy_hr:employees:emp002", "page_start": 1, …}
```

### Why search stays content-light

`POST /v1/search` deliberately returns **no text**. Ranked ids stay small, the
caller chooses which hit is worth expanding, extracted document content is
exposed once rather than N times per query, and a 20-hit search does not ship
20 chunks of a personnel file to a caller who wanted one.

## 10. API contract

`GET /v1/representations/{representation_id}` — the `:path` converter is
required, not decorative: representation ids contain colons and dots and the
default converter stops at the first slash.

```json
{
  "representation_id": "ai:document:…", "entity_type": "document",
  "content_kind": "document_chunk",
  "text": "BIRTH CERTIFICATE\nRegistrar General, Colombo\n…",
  "content_hash": "…",
  "canonical_record_id": null,
  "parent_record_id": "erp:legacy_hr:employees:emp002",
  "source_system_id": "legacy_hr", "source_entity": "employees",
  "source_field": "birth_certificate",
  "business_key_name": "employee_id", "business_key_value": "EMP002",
  "document_id": "…", "document_type": "birth_certificate",
  "page_start": 1, "page_end": 1, "chunk_index": 0,
  "sensitivity": "internal",
  "source_record_ids": ["erp:legacy_hr:employees:emp002"]
}
```

- **Unknown id → 404** `REPRESENTATION_NOT_FOUND`, never `200` with null text.
  A distinct code from `RECORD_NOT_FOUND` so a caller can tell *"this ERP record
  does not exist"* from *"it exists but its text was never persisted"*.
- **No store configured → 422**, saying so. A missing capability and a missing
  row are different answers.
- Registered on the same router list, and therefore the same API-key and CORS
  middleware, as every other Member 4 endpoint. Authentication is unchanged.
- Present in the generated OpenAPI as `getRepresentation`; the artifact is
  regenerated by its own test, never hand-edited.
- **No batch endpoint.** `POST /v1/representations/resolve` was considered and
  not added: the current flow expands one chosen hit, so a batch endpoint would
  be speculative surface. `get_many` already exists on the store as a single
  `IN` query, so adding the endpoint later is a router change with no storage
  work. Recorded as future optimization rather than built now.

### Response bounds

`text_for_ai` is already bounded upstream (800 characters per chunk by default),
so the chunk **is** the bounded retrieval unit and the endpoint returns it
directly. No pagination was added inside a chunk, which would be paginating
something already sized to be a page.

## 11. Structured-record resolution

The store is generic, not document-specific. An EMP002 scalar record resolves to
its flattened text:

```
Entity: Employees
Source Entity: employees
Source System: legacy_hr
Department: Finance
Employee Id: EMP002
Full Name: Nimal Silva
```

with `content_kind: structured_record`, `canonical_record_id` set, and
`document_type`, `page_start`, `chunk_index` all `null` — it does not pretend to
have pages.

## 12. Document-chunk resolution

A document chunk resolves to the actual extracted or OCR-derived text for that
exact vector, retaining `document_id`, `parent_record_id`, business identity,
`document_type`, and page/chunk provenance. Verified for text PDFs, scanned
PDFs through OCR, and multi-chunk documents where **no chunk may resolve to
another chunk's text** (measured: 0 crosstalk across 10 chunks).

## 13. Same content, multiple parents

The Phase 3 case, now closed end to end:

| | EMP003 | EMP004 |
|---|---|---|
| `text` | identical | identical |
| `document_id` | shared | shared |
| `representation_id` | **distinct** | **distinct** |
| `parent_record_id` | `…employees:emp003` | `…employees:emp004` |
| `business_key_value` | `EMP003` | `EMP004` |

Identical text is *correct* — it is the same certificate. Identical association
would be the bug. Measured: **0 association collapse**.

## 14. Tier independence

The representation store is logically independent of vector tiering. Text is
stored **once**, and moving a vector HOT → WARM → COLD does not copy, move or
re-encrypt it. `test_resolution_does_not_depend_on_the_vector_tier` moves a
vector's recorded tier and asserts the resolved text and parent are unchanged.

**Deliberately NOT placed in the encrypted COLD archive.** The archive holds
vectors; putting representation text there would mean a cold hit had to be
decrypted and rehydrated just to read text that PostgreSQL could return
directly — and would create the second copy §4 exists to avoid. The trade-off
is that representation text does not inherit the archive's at-rest encryption;
it inherits the database's. That is a deployment-level control, and stating it
plainly is better than implying an encryption property the design does not have.

## 15. Security / content safety

Returned: derived AI-ready text, safe provenance, safe identity.
Never returned: raw BLOB, base64, ERP credentials, Authorization headers,
connection strings, internal or temporary file paths, vectors.

Enforced structurally rather than by filtering: the table has no column that
could hold bytes, and `FileSource.payload` (Phase 3's in-memory blob) is never
part of a representation. Extracted OCR/PDF **text** is returned, because that
is the intended AI-ready content.

`sensitivity` is passed through exactly as stored — Phase 5 adds no inference
and rewrites nothing. Governance enforcement belongs to Member 1's layer; this
endpoint's job is not to silently drop a classification that search already
exposed.

## 16. Failure semantics

Distinguished rather than collapsed:

| condition | signal |
|---|---|
| representation persistence failed | stage reports it; job partial |
| no store configured | stage note + `422` on resolve |
| embedding failed | existing `embeddings_skipped` counter |
| Qdrant storage failed | existing `vectors_failed` counter |
| representation lookup missing | `404 REPRESENTATION_NOT_FOUND` |
| representation genuinely has no text | `200` with `text: null` |

The last two are the pair that matters: *"I have no row for this"* and *"the row
exists and is empty"* are different facts with different fixes.

## 17. Verification / integrity

No new integrity codes were added to the verification framework. The prompt
allows them only where a checker can genuinely evaluate them, and
`VECTOR_WITHOUT_REPRESENTATION` requires enumerating vector state and
representation ids together — cheap to write, but it would be a checker with no
caller until Phase 9 owns cleanup. Adding a code with no evaluator is the
cosmetic outcome the brief warns against.

What exists instead is enforcement where it can actually fail: the ordering
tests (§8), and a hard gate in both the test suite and the mini-evaluation that
**every hit of every search resolves** — measured 58/58, 0 unresolvable.

## 18. Old-vector compatibility

A vector indexed before Phase 5 has no stored representation. Resolving it
returns `404`, which is the honest answer: the text was never kept, and
inventing one is not available. Search behaviour for such vectors is unchanged.

**To populate them**, re-run the pipeline for the affected source. Representation
ids are deterministic, so the re-run writes the same ids, upserts in place, and
produces no duplicates (`test_reprocessing_the_same_employee_does_not_duplicate_representations`).
No bespoke backfill script exists, and none is claimed.

## 19. Files changed

**New (4):**

| file | purpose |
|---|---|
| `orchestration/representation_store.py` | the store, in-memory and PostgreSQL |
| `scripts/evaluate_phase5_representation_resolution.py` | mini-evaluation |
| `docs/phase5_representation_content_resolution.md` | this report |
| *(3 test files, §20)* | |

**Modified (9):**

| file | change |
|---|---|
| `orchestration/models.py` | `PERSIST_REPRESENTATIONS` stage, `representations_persisted` counter |
| `orchestration/planner.py` | the stage, in all four tails |
| `orchestration/stages.py` | `run_persist_representations`, registered in both handler tables |
| `orchestration/service.py` | `PipelineServices.representations` |
| `orchestration/errors.py` | `RepresentationNotFoundError` |
| `orchestration/__init__.py` | exports |
| `api/schemas.py` | `RepresentationResponse` |
| `api/routers_data.py` | `GET /v1/representations/{id:path}` |
| `api/responses.py` | 404 mapping |
| `api/main.py` | router registration, store wiring |
| `runtime/services.py` | `PostgresRepresentationStore` in production |
| `runtime/bootstrap.py` | table creation in `bootstrap_all` |

## 20. Tests added

| file | tests |
|---|---|
| `tests/erp_pipeline/runtime/test_representation_store.py` | 27 |
| `tests/erp_pipeline/api/test_representation_resolution.py` | 29 |
| `tests/erp_pipeline/api/test_representation_persistence_stage.py` | 13 |

The durability tests run against a **real on-disk database**, not a dict —
SQLite with `erp_runtime` provided by an attached file, so the store's
schema-qualified SQL is exercised unchanged, plus the same assertions against
live PostgreSQL where one is configured. An in-memory store would pass every
behavioural assertion and still lose the corpus on restart, which is the defect
rather than a test of it.

**Two existing tests were extended, not relaxed.**
`test_the_plan_reuses_the_existing_tail_unchanged` pins the exact source-native
tail and now lists `PERSIST_REPRESENTATIONS`, because the point of the test is
that the source-native tail matches the structured one — and it still does.
`test_bootstrap_creates_every_runtime_table` was re-pointed at `REQUIRED_TABLES`
instead of a second hand-written list, so a table added there can no longer be
forgotten here — which is how three tables came to be missing originally.

One test was also made **deterministic**: the scanned-PDF OCR test loaded
`.env` only when an ingestion test happened to be collected first, so it ran or
skipped depending on collection order. It now loads `.env` itself.

## 21. Mini-evaluation

20 representations across 5 employees — including an OCR'd certificate image, a
text PDF, a 10-chunk contract, a blank photo, and one certificate shared by two
employees.

```
search hits attempted        58
search hits resolved         58
unresolvable hits            0
wrong text resolutions       0
wrong parent identities      0
wrong document types         0
chunk provenance mismatches  0
association collapse         0
chunk crosstalk              0
raw binary / base64 leakage  0
text in Qdrant payload       False
survived restart             True

representation lookup  median 0.251 ms   p95 0.512 ms
search + resolve       median 0.458 ms   p95 0.764 ms

GATES: PASS
```

Artifact: `artifacts/phase5_representation_resolution_evaluation.json`.

**On the latency figures.** These are in-process measurements against SQLite and
a filter-aware in-memory tier. They bound the overhead the resolution step adds;
they are **not** production network latency for PostgreSQL or Qdrant, and
sub-millisecond timings carry real run-to-run variance. The useful reading is
that a primary-key lookup adds well under a millisecond, not the specific
digits. Lookup is `WHERE representation_id = ?` — never a scan.

## 22. Targeted tests

`runtime`, `api`, `storage`, `ai`, `orchestration`, `sync`, `ingestion`,
`transformation`:

```
1576 passed, 39 skipped, 0 failed
```

## 23. Full regression

| | collected | passed | failed | errors | skipped |
|---|---|---|---|---|---|
| baseline (after Phase 4) | 3223 | 3160 | 0 | 0 | 63 |
| after Phase 5 | **3293** | **3230** | **0** | **0** | **63** |

`3230 passed, 63 skipped, 30 warnings in 336.95s (0:05:36)`

The **+70** is fully accounted for:

- **+69** new tests (27 + 29 + 13, §20)
- **+1** from `test_bootstrap_creates_every_runtime_table`, which is
  parametrized over `REQUIRED_TABLES["erp_runtime"]` and now covers five tables
  instead of four.

**Skips are unchanged at 63.** No test was skipped to avoid a failure, and none
of the pre-existing infrastructure-dependent skips changed state. The one skip
Phase 5 could have added — the scanned-PDF OCR test — was made deterministic
instead (§20), so it runs rather than skipping.

## 24. Research artifact impact

| artifact | status |
|---|---|
| `phase12_storage_benchmark.json` | unchanged (Aug 14) |
| `phase14_response_adaptation_evaluation.json` | unchanged (Aug 22) |
| `phase3_multimodal_evaluation.json` | unchanged by Phase 5 |
| `phase4_identity_retrieval_evaluation.json` | unchanged by Phase 5 |
| `phase13_openapi.json` | regenerated by its own test, as on every run; now includes `getRepresentation` |
| `phase5_representation_resolution_evaluation.json` | **new** |

No prior artifact was re-run or overwritten to make Phase 5 look better. Phase 3
and Phase 4 gates are re-verified by their own test suites inside the full
regression rather than by re-running their evaluation scripts, precisely so
their artifacts stay untouched.

## 25. Known limitations

1. **Not atomic across PostgreSQL and Qdrant** (§8). The ordering makes the
   failure direction safe; it does not make the write transactional.
2. **Old vectors resolve to 404** until their source is re-run (§18).
3. **No batch resolve endpoint** — one GET per expanded hit (§10).
4. **Representation text is not in the encrypted COLD archive** (§14); it
   inherits database-level protection, not the archive's.
5. **Stale old documents are not cleaned up.** Replacing a document leaves the
   previous representation and its vector in place. Phase 9 owns this; Phase 5
   guarantees only that new content can be persisted and resolved.
6. **Delete is available but not wired into sync propagation.** The store has an
   explicit `delete`, and lifecycle ownership sits with Phase 9. Nothing in
   Phase 5 removes representations automatically.
7. **No integrity codes** for vector/representation divergence (§17).
8. **Lookup columns are minimal** — queries by parent record or content kind
   would need those fields promoted from JSON (§5).
9. **Latency figures are in-process** (§21).

## 26. Explicit Phase 6+ exclusions

Confirmed absent:

```
automatic frontend PDF/image indexing
schema representations / embeddings / vectors / content_kind=schema
database URL detection or fetching   (only a "url_fetching_disabled" refusal marker)
sync scheduler / near-real-time scheduled sync
stale old-document cleanup
new sensitivity inference
frontend search or resolution screens
LLM answer generation
```

One endpoint was added — `GET /v1/representations/{representation_id}` — and no
new Qdrant collection.

## 27. EMP002 readiness

```
EMP002 structured ingestion                  WORKING
EMP002 birth-certificate BLOB ingestion      WORKING
EMP002 PDF / image extraction                WORKING
EMP002 OCR                                   WORKING
EMP002 chunking                              WORKING
EMP002 embedding                             WORKING
EMP002 vector storage                        WORKING
EMP002 exact identity filtering              WORKING
birth_certificate document-type filtering    WORKING
EMP002 search result                         WORKING
EMP002 representation lookup                 WORKING
EMP002 birth-certificate text retrieval      WORKING
```

```
"Give me EMP002 birth certificate details"
        ↓  exact identity + semantic search
        ↓  correct representation
        ↓  actual certificate text
WORKING
```

The core Member 4 backend retrieval loop is complete. Remaining phases improve
frontend automation, schema vectors, URL assets, freshness, security and
integration — rather than repairing this loop.

---

*See also: [Phase 4 — Identity-Aware Retrieval](phase4_identity_aware_retrieval.md),
[Phase 3 — Database BLOB Multimodal Pipeline](phase3_database_blob_multimodal_pipeline.md).*
