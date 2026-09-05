# PRODUCTION DATA REALISM — AUDIT AND REFACTOR

**Project:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline
**Date:** 2026-08-31
**Source of truth:** the current working tree. No prior audit was relied upon.
**Nothing deployed. No database, Qdrant, Azure resource, secret or `.env` file was modified. `.azure-oldkey` was never read.**

---

## 1. EXECUTIVE SUMMARY

The question this audit answers is narrow: **does production logic ever invent a business value instead of obtaining it from the request, the registered source, the discovered schema, the stored record, or the document itself?**

It found **one serious defect and four supporting ones**, all on live runtime paths, plus one class of misreported runtime metadata.

### The headline defect

`PipelineServices.build_document_representations` fabricated two thirds of the canonical identity triple for any partially-declared document upload:

```python
source_system_id = identity.source_system_id or "uploaded"
source_entity    = identity.source_entity    or "documents"
source_field     = identity.document_type    or "upload"
```

`DocumentIdentity.declare()` enforces the business-key pair rule but does **not** require `source_system_id`. So this request is valid today:

```
POST /v1/files/documents
  file=EMP-0002_certificate.pdf
  business_key_name=employee_id
  business_key_value=EMP-0002
```

and the document was indexed into Qdrant claiming it came from a source system called `uploaded`, in an entity called `documents`, from a column called `upload`. **None of those exist.** All three are *filterable payload keys*, so:

- a caller filtering `source_system_id=uploaded` retrieves every document whose origin was merely **unstated** — indistinguishable from a real source of that name;
- anonymous uploads from **any number of unrelated ERP systems** collapse into one synthetic identity;
- the search contract requires `source_system_id` for an exact `record_key` lookup ("EMP-0002 is not globally unique"), so the caller must search under a value they never supplied.

Beneath it, `DocumentAttachment` carried the same fabrication as **dataclass defaults** (`"unknown_source"`, `"unknown_entity"`, `"unknown_field"`), meaning any future caller inherited it silently.

### What was NOT a defect

Deliberately reported as clean, because the brief asks for judgement rather than literal-removal:

- **The frontend** is already strictly API-driven. `Upload.tsx` is a four-state machine whose only business text is `status.detail`, populated exclusively from an awaited API response. Its resting state renders nothing.
- **The composition root imports no test, fixture, demo or seed module.** Verified by AST across all 195 production files.
- **Connector selection is already type-driven** via `ConnectorRegistry` + `SourceType`. No `if source == "..."` logic exists anywhere.
- **Search contains no demo-database assumption.** Source, entity and field vocabulary all resolve from the live catalog and the live Qdrant payload index.
- `erp_vectors_hot` / `erp_vectors_warm`, `all-MiniLM-L6-v2`, `/v1`, the sensitivity and content-kind enums, MIME signatures and crypto algorithm names are all **legitimate** and were kept.

### Result

| | |
|---|---|
| Production business hardcodes found / removed | **1 / 1** |
| Fabricated fallbacks found / removed | **7 / 7** |
| Runtime-metadata literals corrected | **2** |
| Files changed | **5** (106 insertions, 29 deletions) |
| Tests added | **19** (14 Python + 5 frontend) |
| Full regression | **3865 passed, 39 skipped, 0 failed, 0 errors** |
| API contract | **structurally identical — 0 differences** |

---

## 2. SEARCH METHODOLOGY

Grep alone produces mostly false positives (`sample` matches `rows_sampled`; `demo` matches `demotion_margin`). Four passes were used:

1. **Term sweep** across `src/`, `frontend/src/`, `scripts/`, `examples/`, `tests/`, `docs/`, `artifacts/`, root files — for the full Part-1 term list.
2. **Pattern sweep** for the fabrication idioms: `or "<literal>"`, dataclass/Pydantic defaults, `getattr(x, y, "<literal>")`, environment fallbacks.
3. **AST literal scan** — parsed every production file and extracted *string literals only*, excluding docstrings, so documentation prose could not be mistaken for code. This is what reduced the noise to five candidates, all of which proved legitimate.
4. **Reachability trace** — from `python -m erp_pipeline.api` → `runtime/application.py` → `build_production_services` → routers → orchestration → stores, to decide whether each candidate could influence a real response.

A value was only treated as a defect if it was **both** a business fact **and** reachable from a registered endpoint or background worker.

---

## 3–5. FINDINGS, CLASSIFICATION AND PRODUCTION REACHABILITY

### Defects fixed

| # | Location | Value | Class | Reachable from |
|---|---|---|---|---|
| **F1** | `orchestration/service.py:595-599` | `"uploaded"` / `"documents"` / `"upload"` | **A + C** | `POST /v1/files/documents` → `DOCUMENT_PIPELINE` job → `AI_BUILD` → Qdrant payload → `GET /v1/search` |
| **F2** | `ai/attached_documents.py:73-75` | `"unknown_source"` / `"unknown_entity"` / `"unknown_field"` (dataclass defaults) | **C** | Every `DocumentAttachment` construction — upload path **and** database-BLOB path |
| **F3** | `ai/attached_documents.py:117` | `document_type or source_field` where `source_field` was itself fabricated | **C** | Same |
| **F4** | `orchestration/multimodal.py:153-155` | `getattr(source, ..., "unknown_source")` / `"unknown_entity"` | **C** | `MULTIMODAL_EXTRACT` stage (database BLOB → document) |
| **F5** | `orchestration/service.py:523` | `or "unknown_source"` | **C** | `SOURCE_NATIVE_PIPELINE` → `SourceReference.source_system_id` on every canonical record |
| **F6** | `runtime/services.py:322` | literal `"sentence-transformers/all-MiniLM-L6-v2"` | **B** | `GET /v1/capabilities`, `GET /v1/health/ready` |
| **F7** | `runtime/services.py:330` | literal `384` | **B** | Same |
| **F8** | `api/routers_data.py:1226-1227` | `legacy_erp_pg` / `hr.employees` / `EMP-0001` in the endpoint docstring | **G** | Published into the OpenAPI operation `description` |

**On F5** — the fallback was effectively unreachable (a job always carries a source), but it guarded a value that becomes `SourceReference.source_system_id` on *every* canonical record. Defensive code that fabricates identity is still fabrication, so it now fails loudly instead.

**On F6/F7** — these were defensible as "configuration, not a weight", and today they resolve to the same values the loader produces. But they were **duplicated literals**, not references. `ERP_QDRANT_DIMENSION` *is* configurable: an operator running 768 was told **384** by `/v1/capabilities` and `/v1/health/ready` while the vector store expected the other number.

### Values examined and deliberately RETAINED

| Value | Location | Class | Why kept |
|---|---|---|---|
| `erp_vectors_hot` / `erp_vectors_warm` | `runtime/settings.py` | **E** | Configurable via `ERP_QDRANT_*_COLLECTION`; physical tier names, not business data |
| `all-MiniLM-L6-v2` as `DEFAULT_MODEL_ID` | `ai/embedding.py` | **E** | A genuine default; runtime now reports it *by reference* |
| `SchemaOrigin.UPLOADED = "uploaded"` | `schemas/enums.py:174` | **D** | Enum vocabulary. Notably it is *not* what F1 was doing — F1 put this word into `source_system_id` |
| `content_kind`, `sensitivity`, `entity_kind`, `FieldDataType` vocabularies | `schemas/enums.py` | **D** | Closed domain vocabularies |
| `"sample"` metadata keys | `discovery/mongodb.py:392`, `ingestion/csv_ingestion.py:542` | **D** | Key *names* for sampling statistics |
| `"unknown_source_field"` | `mapping/validation.py:78` | **D** | A validation error **code**, not data |
| `127.0.0.1`, `localhost`, port `8000`, `erp_ai_native_db`, timeouts, batch sizes | `api/config.py`, `runtime/settings.py`, `catalog/config.py` | **E** | Infrastructure defaults, all env-overridable, none pretending to be business data |
| `%PDF-`, `\x89PNG`, `\xff\xd8\xff` | `ingestion/detection.py` | **D** | Magic-byte signatures |
| `AES-256-GCM`, `HMAC-SHA256`, `keyword` | crypto + `payload_indexes.py` | **D** | Algorithm names |
| `DeterministicEmbedder(model_id="fake-deterministic")` | `sync/propagation.py:308` | **F** | Test scaffolding living in the production tree. **Not exported from `sync/__init__.py` and never instantiated in `src/` or `scripts/`** — unreachable from the composition root. Flagged, not deleted (deleting functionality is out of scope). |
| `DeterministicTestModel` | `ai/embedding.py` | **F** | Same — explicitly documented as "not a mock of the real model's SEMANTICS" |
| Everything in `tests/`, `scripts/`, `examples/`, `artifacts/`, `docs/` | — | **F/G/H/I** | Not importable from `src/` — verified |

---

## 6–9. FIXES: BEFORE → AFTER DATA FLOW

### F1 + F2 + F3 — document identity

**Before**

```
upload declares business key only
  → source_system_id := "uploaded"      ← INVENTED
  → source_entity    := "documents"     ← INVENTED
  → source_field     := "upload"        ← INVENTED
  → document_type    := source_field = "upload"   ← INVENTED
  → Qdrant payload asserts a source system that does not exist
```

**After**

```
upload declares business key only
  → source_system_id := identity.source_system_id  (None)
  → source_entity    := identity.source_entity     (None)
  → source_field     := identity.document_type     (None)
  → to_metadata() OMITS every absent key
  → a filter on source_system_id correctly EXCLUDES this document
```

`to_metadata()` now routes all four through the module's **existing** "absent, not null" convention — the same rule `parent_record_id` already followed, and for the same stated reason. The database-BLOB path is unaffected: `source_field` there is the real ERP column name, so `document_type or source_field` still yields `birth_certificate`.

`attachment_key()` coerces `source_field or ""` — that value is an *opaque internal discriminator*, not a payload field, so an empty segment asserts nothing. Collision protection between two employees' copies of one certificate is unchanged (it comes from `attachment_scope`).

### F5 — structured-record source system

**Before:** `getattr(schema, "source_system_id", None) or source_id or "unknown_source"`
**After:** resolve from schema, then job; if neither supplies it, raise `InvalidPipelineRequestError` naming the missing field.

This value becomes `SourceReference.source_system_id` (a `require_identifier` field) on every canonical record and the `source_system_id` key on every resulting Qdrant point. Failing is correct; indexing real business rows under a source system that does not exist is not.

### F6 + F7 — runtime metadata

**Before:** `_LazyEmbeddingService` returned two duplicated literals before the model loaded.
**After:** `model_id` reads `ai.embedding.DEFAULT_MODEL_ID` — the same constant `_load()` builds from; `dimension` reads the **configured** `ERP_QDRANT_DIMENSION`, passed in from `RuntimeSettings` by the composition root.

Verified: with `configured_dimension=768` the service reports **768** (previously **384**), and `loaded is False` throughout — the "importing the API loads no model" guarantee is preserved.

### F8 — published documentation

The `GET /v1/search` docstring became the OpenAPI operation `description`, publishing our demo database name into the API contract. Rewritten to describe the *shape* of a canonical identity and to point callers at the bare endpoint for discovery. **Prose only** — no parameter, response, schema or status code changed.

---

## 8. EXACT FILES CHANGED

```
src/erp_pipeline/ai/attached_documents.py    | 47 +++++++++++++++-------
src/erp_pipeline/orchestration/service.py    | 31 +++++++++-----
src/erp_pipeline/runtime/services.py         | 41 ++++++++++++++++---
src/erp_pipeline/orchestration/multimodal.py |  8 +++--
src/erp_pipeline/api/routers_data.py         |  8 +++--
5 files changed, 106 insertions(+), 29 deletions(-)
```

New test files:
```
tests/erp_pipeline/api/test_production_data_realism.py   (14 tests)
frontend/src/api/realism.test.ts                          (5 tests)
```

---

## 10. TESTS ADDED

All 12 Part-15 requirements are covered.

| Req | Test | Proves |
|---|---|---|
| 1 | `test_two_different_source_systems_produce_distinct_results` | `acme_erp_pg` vs `globex_erp_mongo` — names production code has never seen |
| 2 | `test_a_source_entity_the_code_has_never_seen_works_unchanged` | Entity resolves from request + catalog |
| 3 | `test_a_record_key_is_only_ever_the_one_supplied` | EMP-0001/EMP-0002 come from the corpus and query |
| 4 | `test_search_never_defaults_to_a_particular_employee` | Bare call returns metadata; no identifier manufactured |
| 5 | `test_an_undeclared_upload_attaches_to_no_parent_record` | No parent derived from a business key |
| 6 | `test_document_type_is_never_forced_to_a_vocabulary_value` | No default type; ERP column still used where real |
| 7 | `test_qdrant_payload_identity_matches_the_actual_source` | Every payload identity traces to its record; no placeholder present |
| 8 | `test_reported_embedding_metadata_follows_configuration` + `test_capabilities_reports_the_wired_embedding_service` | Reported metadata follows configuration and wiring |
| 9 | `realism.test.ts` (5 tests) | No fabricated rows; resting state renders nothing; status only ever filled from an awaited API call; no `defaultValue` that could auto-submit |
| 10 | `test_production_composition_imports_no_test_or_demo_module` | AST scan of all 195 production files |
| 11 | `test_connector_choice_follows_the_registered_source_type` | All four connectors selected from `SourceType` alone |
| 12 | `test_a_transformation_without_a_source_system_fails_rather_than_inventing_one` | Clear failure, not fake identity |
| — | `test_an_undeclared_upload_invents_no_source_identity` | **The headline regression** |
| — | `test_a_fully_declared_upload_carries_exactly_what_was_declared` | Positive case: declared identity passes through unchanged |

### The tests are load-bearing — verified, not assumed

The `DocumentAttachment` defaults were temporarily reverted to `"unknown_source"` / `"unknown_entity"` / `"unknown_field"`:

```
2 failed, 12 passed
  FAILED test_an_undeclared_upload_invents_no_source_identity
  FAILED test_document_type_is_never_forced_to_a_vocabulary_value
```

The fix was then restored byte-for-byte and all 14 pass again. A test that passes against the broken code would have been worthless.

---

## 11. TEST RESULTS

| Suite | Result |
|---|---|
| New realism tests (Python) | **14 passed** |
| New realism tests (frontend) | **5 passed** |
| Frontend suite (total) | **31 passed** |
| Affected suites (identity search, auto-indexing, multimodal, BLOB pipeline, orchestration, runtime) | **390 passed, 2 skipped** |
| **Full regression** | **3865 passed, 39 skipped, 0 failed, 0 errors** |
| OpenAPI structural diff vs committed snapshot | **0 differences** |

Baseline was 3851 passed / 39 skipped. **3851 + 14 new = 3865.** Collection: 3890 + 14 = 3904 = 3865 + 39. The arithmetic closes exactly.

> **One intermediate run reported 3841 / 63 and was investigated rather than accepted.** Docker Desktop shut down mid-run (the log contains `failed to connect to the docker API … is the daemon running?`), taking MongoDB with it and skipping 24 tests. Restarting Docker and the container returned the definitive 3865 / 39 above. The 39 standing skips are the local Qdrant container, which is down in the baseline too.

---

## 12. STATIC SCAN AFTER FIX (Part 16)

An AST scan of **string literals in production code, docstrings excluded**, returns five occurrences. Every one is accounted for:

| Path | Line | Value | Why safe | Category |
|---|---|---|---|---|
| `src/erp_pipeline/discovery/mongodb.py` | 392 | `'sample'` | Metadata **key name** for sampling statistics | LEGITIMATE CONSTANT |
| `src/erp_pipeline/ingestion/csv_ingestion.py` | 542 | `'sample'` | Same | LEGITIMATE CONSTANT |
| `src/erp_pipeline/mapping/validation.py` | 78 | `'unknown_source_field'` | Validation error **code**, never data | LEGITIMATE CONSTANT |
| `src/erp_pipeline/schemas/enums.py` | 174 | `'uploaded'` | `SchemaOrigin.UPLOADED` enum member | LEGITIMATE CONSTANT |
| `src/erp_pipeline/sync/propagation.py` | 308 | `'fake-deterministic'` | `DeterministicEmbedder` — not exported from `sync/__init__.py`, never instantiated in `src/` or `scripts/`, unreachable from the composition root | TEST |

**Zero occurrences** of `EMP-0001`, `EMP-0002`, `MC-2026`, `Demo Medical Centre`, `John Doe`, `Jane Doe`, `legacy_erp_pg`, `hr.employees` or `hr.employee_documents` remain anywhere in `src/` — in code **or** in comments.

`localhost` / `127.0.0.1` remain only as env-overridable infrastructure defaults (`api/config.py`, `runtime/settings.py`, `catalog/config.py`) and in SSRF-defence documentation in `response_adaptation/assets.py`. **CONFIG DEFAULT.**

Occurrences outside `src/` — `tests/` (78 files), `docs/` (33), `scripts/` (12), `artifacts/` (6), `examples/` (1) — are **TEST / DEMO / DOC / RESEARCH** and are retained deliberately. None is importable from `src/`, verified by the AST test.

---

## FINAL TABLE

| Finding | Old behaviour | New behaviour | Data source now used | Tests |
|---|---|---|---|---|
| **F1** upload identity | `source_system_id="uploaded"`, `source_entity="documents"`, `source_field="upload"` written into the Qdrant payload | Absent keys omitted entirely | Declared `DocumentIdentity` from the multipart request | `test_an_undeclared_upload_invents_no_source_identity`, `test_a_fully_declared_upload_carries_exactly_what_was_declared` |
| **F2** attachment defaults | Dataclass defaulted to `"unknown_source"` / `"unknown_entity"` / `"unknown_field"` | `None`, omitted from payload | Caller / registered source / discovered entity | `test_qdrant_payload_identity_matches_the_actual_source` |
| **F3** document type | Fell back to a fabricated `source_field` | Falls back only to a **real ERP column name**; otherwise omitted | Declared type, else the discovered column | `test_document_type_is_never_forced_to_a_vocabulary_value` |
| **F4** BLOB provenance | `getattr(..., "unknown_source")` | `None` when genuinely unavailable | `RegisteredSource` + discovered `SourceEntity` | `test_qdrant_payload_identity_matches_the_actual_source` |
| **F5** record source system | `or "unknown_source"` | Raises `InvalidPipelineRequestError` naming the missing field | Discovered `SourceSchema`, else the job's registered source | `test_a_transformation_without_a_source_system_fails_rather_than_inventing_one` |
| **F6** reported model id | Duplicated literal | Reads `DEFAULT_MODEL_ID`, or the loaded model | `ai.embedding` constant / loaded model | `test_reported_embedding_metadata_follows_configuration` |
| **F7** reported dimension | Literal `384` | Configured width pre-load; model's own width after | `ERP_QDRANT_DIMENSION` via `RuntimeSettings` | `test_reported_embedding_metadata_follows_configuration`, `test_capabilities_reports_the_wired_embedding_service` |
| **F8** published example | Docstring named our demo DB in the OpenAPI description | Describes the identity **shape**; points at the bare endpoint | — (documentation) | OpenAPI structural diff = 0 |

---

## 13. REMAINING LIMITATIONS

1. **`DeterministicEmbedder` still lives in `src/erp_pipeline/sync/propagation.py`.** Unreachable (unexported, never instantiated) and therefore not a defect, but test scaffolding in the production tree is a latent trap. Removing it deletes functionality and was out of scope. **Flagged for your decision.**
2. **The embedding model id is not configurable by environment.** `DEFAULT_MODEL_ID` is the only value the runtime can load. Reporting is now honest; making the model itself selectable is a feature, not a naming fix.
3. **`document_type` still falls back to `source_field` for database BLOBs.** Judged correct — the ERP column name *is* what the business calls the document, and the module documents that reasoning. Flagged so the judgement is visible rather than buried.
4. **A partially-declared upload now produces a different `representation_id`** than before, because `source_field` no longer contributes the literal `"upload"` to the attachment key. No current production data is affected (the 50 live employee vectors are `structured_record`; the probe documents carried explicit identity), but a re-index of any previously anonymous upload will yield a new point id.
5. **Lifecycle registry and `.azure-oldkey`** findings from the structural audit were deliberately **not** touched here, as instructed.

---

## SUMMARY

```
PRODUCTION BUSINESS HARDCODES FOUND:      1   (F1 — fabricated canonical identity on document upload)
PRODUCTION BUSINESS HARDCODES REMOVED:    1

FABRICATED FALLBACKS FOUND:               7   (F1 ×3, F2 ×3, F5 ×1)
FABRICATED FALLBACKS REMOVED:             7
                                              (+ F3 document_type fallback corrected,
                                                 F4 ×2 BLOB provenance fallbacks removed)

ENVIRONMENT HARDCODES FIXED:              2   (F6 model id, F7 dimension)
DOCUMENTATION HARDCODE FIXED:             1   (F8 — demo DB name removed from published OpenAPI)

TEST/DEMO VALUES RETAINED:              all   (tests/, scripts/, examples/, artifacts/, docs/,
                                               DeterministicTestModel, DeterministicEmbedder —
                                               none reachable from the composition root)

FUNCTIONAL API CONTRACT CHANGED:         NO   (25 operations, identical schemas and security;
                                               0 structural differences — one prose description edited)
DATABASE DATA MODIFIED:                  NO
QDRANT PRODUCTION DATA MODIFIED:         NO
SECRETS READ:                            NO
DEPLOYED:                                NO
```

**The production implementation now works with any newly registered ERP source and real runtime data without source-code modification.** Where a business value cannot be resolved, it is absent or the request fails — never invented.
