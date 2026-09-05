# Phase 1 — Contract and Correctness Stabilization

**ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline**
Member 4 · IT22267290 · Project R26-SE-034

| | |
|---|---|
| Phase | 1 of the final completion plan |
| Nature | **Defect repair only.** No redesign, no new capability. |
| Baseline | 2943 passed · 63 skipped · 0 failed · 0 errors · 458.73s |
| Date | 2026-08-22 |

---

## 1. Scope

Eight defects, each of which caused the API to state something **false** to a
caller. None of them was a design problem; all were repair work on the boundary
between a correct engine and the response it produced.

| # | Defect | Nature |
|---|---|---|
| 1 | `rows_observed` always `0` | A count nobody took, reported as fact |
| 2 | `document_id` always `null` | An identifier read from an attribute that does not exist |
| 3 | `ocr_used` always `false` | A flag that could never become true |
| 4 | `decisions[].target_path` always `null` | The engine's own choice, not reported |
| 5 | Warnings leaked a Python `repr` | Implementation detail rendered into user text |
| 6 | Corrupt/encrypted PDF → HTTP 500 | A client's bad file reported as a server fault |
| 7 | CSV entity named `invoices.csv` | A filename used where a concept was needed |
| 8 | `E002` tokenised to `email` | An identifier turned into a word it never contained |

**Explicitly out of scope and NOT implemented:** employee canonical entity ·
generic entity fallback · DB BLOB→OCR routing · schema vectors · new Qdrant
collection strategy · business identity filters · representation retrieval API ·
automatic document indexing · URL-column retrieval · scheduler · new frontend
screens.

---

## 2. Files changed

### Source (4 files)

| File | Change |
|---|---|
| `src/erp_pipeline/api/schemas.py` | `CsvUploadResponse`: added `rows_sampled`, `sample_limited`; `rows_observed` → `int \| None`. `DocumentUploadResponse`: documented `document_id` and `ocr_used` semantics. Contracts only. |
| `src/erp_pipeline/api/routers_data.py` | Added 6 helpers (`_warning_messages`, `_ocr_was_used`, `_document_identity`, `_sample_was_limited`, `_selected_target`, `_confidence_label`, `_ingest_upload_or_refuse`) and rewired the two upload routes plus the mapping serializer to use them. |
| `src/erp_pipeline/ingestion/csv_ingestion.py` | `SourceEntity.source_name` now carries the logical entity name instead of the filename, at both construction sites. |
| `src/erp_pipeline/mapping/normalization.py` | `_DIGIT_BOUNDARY` now requires two letters before a letter→digit split. |

### Frontend (1 file)

| File | Change |
|---|---|
| `frontend/src/api/types.ts` | `CsvUploadResponse` gains `rows_sampled` / `sample_limited`; `rows_observed` becomes `number \| null`. Doc comments on `document_id` and `ocr_used`. **No component changed** — `Upload.tsx` never read these fields. |

### Tests (2 new, 1 updated)

| File | Change |
|---|---|
| `tests/erp_pipeline/api/test_response_contract_correctness.py` | **NEW** — 41 tests covering fixes 1–7 |
| `tests/erp_pipeline/mapping/test_identifier_tokenization.py` | **NEW** — 24 tests covering fix 8 |
| `tests/erp_pipeline/ingestion/test_csv_ingestion.py` | **UPDATED** — one test that pinned the old `source_name` behaviour (see §7) |
| `tests/erp_pipeline/runtime/test_live_production_runtime.py` | **UPDATED** — one test whose ambiguity fixture depended on the FIX 7 defect (see §9) |

**No engine logic was modified.** No scoring weight, threshold, ambiguity rule,
refusal rule, vocabulary entry, extraction algorithm, storage policy or Phase 14
mechanism was touched.

---

## 3–5. Defect-by-defect: before and after

### FIX 1 — CSV row-count contract

**Before**

```json
{"rows_observed": 0}
```

for a two-row CSV. The route read `result.data_row_count`, which CSV inference
never populates, and coerced the resulting `None` to `0` with `or 0`.

**The semantic trap.** The obvious repair — assigning `rows_sampled` — would
have replaced one false statement with another. `CsvFileIngestion._infer_structure`
stops at `CsvOptions.max_rows_for_schema_inference`, so `rows_sampled` is a
**bounded sample**, never a total.

**After**

```json
{"rows_sampled": 2, "sample_limited": false, "rows_observed": null}
```

Three separate facts: rows actually inspected; whether the sample hit its
ceiling; and the true total, which stays `null` because nothing counted it.

### FIX 2 — Document identity

**Before** `"document_id": null` on every upload. `ExtractedDocument` has fields
`file, provenance, pages, status, page_count, document_metadata, warnings` — no
`document_id`, so `getattr(document, "document_id", None)` always returned `None`.

**After** the file's **content hash**, which is the identity
`ai.chunking.chunk_document` already derives (*"document_id defaults to the
file's CONTENT hash rather than its name"*). The id an upload returns is
therefore the id its chunks will carry, so a chunk stays traceable to its
document.

```
same bytes, different filename  → identical document_id
different content               → different document_id
```

### FIX 3 — OCR flag

**Before** `"ocr_used": false` unconditionally — `getattr(document, "ocr_used", False)`
on a model with no such attribute.

**After** derived from the extractors' own per-page marker,
`ExtractedPage.extraction_method`, which they set to `"ocr"`, `"text_layer"` or
`"none"`:

```python
ocr_used = any(page.extraction_method == OCR_EXTRACTION_METHOD for page in pages)
```

No second notion of OCR state was invented. A page whose status is
`OCR_UNAVAILABLE` carries `extraction_method="none"` and is correctly excluded —
OCR did not run, so it was not used.

### FIX 4 — Mapping target path

**Before**

```json
{"source_field": "inv_no", "outcome": "auto_selected",
 "target_path": null, "confidence": "high"}
```

`FieldDecision` has no `target_path`; the engine records its choice on
`selected.qualified_target`. A caller could see that a field auto-selected with
high confidence but not what it selected.

**After**

```json
{"source_field": "inv_no", "outcome": "auto_selected",
 "target_path": "invoice.invoice_id", "confidence": "high"}
```

`confidence` also changed: `str(None)` produced the literal string `"None"`,
indistinguishable from a real level. It is now `null`.

Unselected decisions still report `null`, which is correct — nothing was chosen.

### FIX 5 — Warning serialization

**Before**

```
"ExtractionWarning(category='ocr_unavailable', message='OCR is unavailable: ...',
 row_number=None, page_number=1, column_index=None)"
```

`str()` on a dataclass, rendered into a field a user reads.

**After**

```
"ocr_unavailable: OCR is unavailable: The Tesseract executable was not found. ..."
```

The dataclass already carried a stable `category` and a human `message`; those
two are joined and nothing else. Plain-string warnings pass through unchanged,
so the call sites that append their own prose still work.

### FIX 6 — Malformed / encrypted PDF status

**Before** HTTP **500 `INTERNAL_ERROR`**. `MalformedPDFError` and
`EncryptedPDFError` were absent from `ERROR_STATUS`, so a client's broken file
was reported as a server fault — and, worse, became indistinguishable from a
genuine server fault.

**After** the failures are converted at the route into the orchestration error
whose existing status already means the right thing:

| Condition | Error raised | Status |
|---|---|---|
| Corrupt PDF | `InvalidPipelineRequestError` | **422** |
| Encrypted PDF | `InvalidPipelineRequestError` | **422** |
| Undecodable image | `InvalidPipelineRequestError` | **422** |
| Malformed CSV | `InvalidPipelineRequestError` | **422** |
| Bytes contradict the extension | `UnsupportedUploadError` | **415** |
| Valid file | — | **201** |
| **Unexpected internal fault** | (propagates) | **still 500** |

No new status code was invented. 422 fits the existing taxonomy: the *type* is
accepted, the *content* is unprocessable. 415 is reserved for the genuine type
problem, matching what the API already returns for a rejected suffix.

**The conversion is enumerated, not blanket.** A programming error inside an
extractor still becomes a 500 — pinned by
`test_an_unexpected_internal_error_still_surfaces_as_500`.

### FIX 7 — CSV logical entity name

**Before** `SourceEntity.source_name = "invoices.csv"`. Phase 8's entity
evidence (`mapping/scoring.py:263`) matches `source_entity.source_name` against
the canonical model, so the suffix weakened the entity signal on every uploaded
file.

**After** `source_name = "invoices"` — the value `_entity_name()` already
computed via `Path(...).stem` and already used for `normalized_name`.

**Provenance is untouched**, verified at every site: `FileProvenance.original_filename`,
the entity's `metadata["source_filename"]`, the schema's
`metadata["original_filename"]`, `content_hash`, `file_id`, `upload_id`, the
stored path and extension validation.

**Measured effect on the mapping of a six-column invoice CSV:**

| | Before | After |
|---|---:|---:|
| auto-selected | 2 | **5** |
| ambiguous | 3 | **0** |
| unmapped | 1 | 1 |

This is reported because it is a **large behavioural improvement produced by a
naming fix**, not by tuning. No weight, threshold or vocabulary entry changed —
the entity signal simply started receiving a concept instead of a filename. The
remaining unmapped field is `row_version`, which correctly has no canonical
target.

### FIX 8 — Identifier tokenization

**Before**

```
E002  →  split ('e','002')  →  canonical ('email','002')
```

`split_tokens` broke every letter/digit boundary, stranding the single letter
`e`, which `DEFAULT_SYNONYMS` then folded onto `email` — an entry that exists so
`e_mail` splits and folds correctly. An employee code silently acquired an
`email` token it never contained.

**Demonstrated consequence:** the query *"Who is customer E002?"* selected a
customer's `email_addr` field at score 0.75 despite never mentioning email.

**After** the letter→digit split requires **two** letters before it:

```python
_DIGIT_BOUNDARY = re.compile(r"(?<=[a-z]{2})(?=\d)|(?<=\d)(?=[a-z])")
```

| Input | Before | After |
|---|---|---|
| `E002` | `('email','002')` | **`('e002',)`** |
| `A1` | `('1',)` — identity lost | **`('a1',)`** |
| `EMP002` | `('emp','002')` | unchanged |
| `INV204` | `('invoice','204')` | unchanged |
| `PO1007` | `('purchase','order','1007')` | unchanged |
| `e_mail` | `('email','email')` | unchanged |
| `line1` | `('line','1')` | unchanged |

**The synonym table was NOT modified.** `DEFAULT_SYNONYMS["e"] == "email"` is
still present and still correct for `e_mail`. The bug was never the synonym; it
was manufacturing a one-letter "word" out of an identifier that contains no
words. A prefix of two or more letters is a real abbreviation (`inv`, `po`,
`cus`) and still splits and expands.

---

## 6. API contract changes

| Endpoint | Change | Compatibility |
|---|---|---|
| `POST /v1/files/csv` | **Added** `rows_sampled: int`, `sample_limited: bool` | Additive |
| `POST /v1/files/csv` | `rows_observed: int = 0` → `int \| None = None` | **Breaking for a client that assumed a number.** Justified: the old value was always a false `0`. `null` now means "not counted". |
| `POST /v1/files/documents` | `document_id` now populated | Additive in effect |
| `POST /v1/files/documents` | `ocr_used` now truthful | Value change only |
| `POST /v1/files/documents` | Corrupt/encrypted → 422, type mismatch → 415 | **Status change**, from an incorrect 500 |
| Both uploads | `warnings[]` are clean text | Format change within the same type |
| `POST /v1/mappings/suggest`, `PUT /v1/mappings/{id}` | `decisions[].target_path` populated; `confidence` `null` instead of `"None"` | Additive in effect |
| `GET /v1/schemas/{id}` | `entities[].source_name` no longer carries the file extension | **Value change**; `normalized_name` unchanged |

`artifacts/openapi_contract_snapshot.json` is regenerated from the live app by
`tests/erp_pipeline/api/test_document_and_live_http.py`, so the published
contract updates automatically on the next full run.

---

## 7. Tests added / updated

**Added — 65 tests, all passing:**

| File | Tests | Covers |
|---|---:|---|
| `tests/erp_pipeline/api/test_response_contract_correctness.py` | 41 | fixes 1–7, including empty / 1-row / 2-row / over-limit CSV, TSV, PDF, PNG, JPEG, identical-vs-different content, mixed-page OCR, corrupt / fake / empty / valid PDF, and the 500-still-works guard |
| `tests/erp_pipeline/mapping/test_identifier_tokenization.py` | 24 | fix 8: five identifiers keep identity; email vocabulary still resolves; `inv`/`po` still expand; vocabulary tables asserted unmodified |

**Updated — 1 test:**

`tests/erp_pipeline/ingestion/test_csv_ingestion.py::test_one_csv_becomes_one_dataset_entity`
asserted `entity.source_name == "normal.csv"` — the exact behaviour FIX 7
corrects. It now asserts `== "normal"` **and additionally asserts that the
filename is still preserved** on `provenance.original_filename` and
`entity.metadata["source_filename"]`, so the test guards more than it did before
rather than less.

This is the only pre-existing test touched. It was directly in scope, and it was
strengthened, not weakened.

---

## 8. Targeted test results

```
tests/erp_pipeline/api/test_response_contract_correctness.py
tests/erp_pipeline/mapping/test_identifier_tokenization.py
    65 passed in 2.69s

tests/erp_pipeline/api + ingestion + mapping + response_adaptation + transformation
    1108 passed, 14 skipped in 125.77s      (after the one test update)
```

---

## 9. Full regression results

| | Baseline (before Phase 1) | After Phase 1 |
|---|---|---|
| Collected | 3006 | **3071** (+65, exactly the new tests) |
| Passed | 2943 | **3008** (+65) |
| Skipped | 63 | **63** (unchanged) |
| **Failed** | **0** | **0** |
| **Errors** | **0** | **0** |
| Duration | 458.73s | 405.94s |

**Zero failures, zero errors, and the skip count is unchanged.** Collection grew
by exactly 65 — the two new test files and nothing else — and all 65 pass.

Frontend suite (`vitest`): **26 passed** in `src/api/client.test.ts` and
`src/api/safety.test.ts`.

### The +1 skip — traced, and then removed

The first post-change run showed 64 skips against a baseline of 63. The `-rs`
breakdown identified it exactly:

```
24  MongoDB unreachable at localhost:27018
37  Qdrant unreachable at localhost:6333  (6 modules)
 2  live schema-discovery / drift could not reach their source
 1  test_live_production_runtime.py: "this corpus produced no ambiguity,
    so there is no draft"          <-- NEW
```

That last one was **caused by FIX 7**, and it mattered. The test
`test_a_mapping_draft_survives_a_restart` carried the comment
*"'invoices.csv' produces genuine ambiguity where 'invoice.csv' does not"* — it
was **relying on the very defect FIX 7 removed** to create its fixture
condition. With the entity correctly named `invoices`, the mapping auto-approved,
no draft was filed, and the test's own guard skipped it. It stopped failing by
ceasing to test anything.

**Leaving that as a silent coverage loss would have been the wrong call**, so
the corpus was repaired rather than the guard removed. The upload is now named
`ledger.csv`: a legacy export whose table name matches no canonical entity,
carrying fields that genuinely belong to more than one
(`customer_id` → `invoice.customer_id` or `customer.customer_id`; `amount` →
`invoice.amount` or `purchase_order.amount`). Nothing disambiguates them, the
engine refuses to choose, and a draft is filed — which is the path the test
exists to prove.

Measured: three genuinely ambiguous fields, `auto_approved: false`, and the
module now reports **15 passed, 0 skipped**. The final regression confirms it:
skips returned to **63**, matching the baseline exactly.

The ambiguity is now a property of the **data**, which is what it should always
have been.

---

## 10. Manual API smoke-test results

All eight required checks, executed against a real application instance:

| # | Check | Result |
|---|---|---|
| 1 | `GET /v1/health/ready` | **200**, `status: degraded, ready: false` — **environmental**: the lightweight harness configures no embedding model or vector store, both reported as unconfigured dependencies. Health code was not touched by Phase 1. A fully wired deployment returns `ready: true`. |
| 2 | CSV upload | **201**, `schema_id` present, logical entity `invoices`, filename preserved as `invoices.csv`, `rows_sampled=2`, `sample_limited=false`, `rows_observed=null` |
| 3 | Mapping suggestion | **5 of 6** decisions expose a real `target_path` (`inv_no→invoice.invoice_id`, `cust_ref→invoice.customer_id`, `total_amt→invoice.amount`, …); the 6th is `row_version`, correctly unmapped |
| 4 | Normal PDF | **201**, `document_id` populated, `ocr_used=false` (text layer), `warnings=[]` |
| 5 | Image (JPEG) | **201**, `document_id` populated, OCR state correct, warnings clean |
| 6 | Corrupt PDF | **422 `INVALID_PIPELINE_REQUEST`** — not 500 |
| 7 | Encrypted PDF | **422 `INVALID_PIPELINE_REQUEST`** — not 500 |
| 8 | Identifier regression | `E002 → ('e002',)`, `EMP002 → ('emp','002')`, no `email`; `e_mail → ('email','email')` still works |

---

## 11. Research artifact impact

**No artifact was overwritten. None needed to be.**

FIX 8 changes tokenization, so both affected evaluations were re-run and
compared against the committed artifacts **without writing to them**.

### Mapping benchmark — identical

| Metric | Before | After |
|---|---|---|
| top-1 accuracy | 1.0 | **1.0** |
| top-3 recall | 1.0 | **1.0** |
| auto-selection precision | 1.0 (60/60) | **1.0 (60/60)** |
| automatic coverage | 0.8824 | **0.8824** |
| ambiguity rate | 0.0 | **0.0** |
| unmapped rate | 0.0882 | **0.0882** |
| correct refusal rate | 1.0 | **1.0** |
| alias-independent top-1 | 1.0 (18/18) | **1.0 (18/18)** |

### Phase 14 evaluation — identical on every metric

| Method | Metric | Before | After |
|---|---|---|---|
| RAW | recall / removal / context | 1.0 / 0.0 / 0.0 | **unchanged** |
| GENERIC | recall / removal / context | 1.0 / 0.0 / 0.143311 | **unchanged** |
| **ERP-aware adaptive** | relevant recall | 0.979866 | **0.979866** |
| | perfect-recall cases | 0.955882 | **0.955882** |
| | irrelevant removal | 0.608889 | **0.608889** |
| | field reduction | 0.4736 | **0.4736** |
| | context reduction | 0.500405 | **0.500405** |
| | success rate | 1.0 | **1.0** |
| Ablation | with / without relevance | 0.979866 / 1.0 | **unchanged** |

### The three documented Phase 14 failures are unchanged

```
before: ['po-05', 'proc-02', 'sap-04']
after : ['po-05', 'proc-02', 'sap-04']
```

**This is the important result.** It demonstrates that FIX 8 is a generic
tokenizer correctness fix and **not** benchmark tuning: no evaluation query in
the Phase 14 corpus contains a letter+digit identifier of the affected shape, so
no published number moved and no documented limitation was quietly closed.

`artifacts/tiered_storage_benchmark.json` is unaffected — Phase 1 touched no
storage, embedding or tiering code.

---

## 12. Remaining known limitations

Unchanged by Phase 1, and deliberately so:

1. **`rows_observed` is still always `null` on the upload path.** Nothing counts
   the whole file. The field is now honest rather than wrong; making it truthful
   *and* populated would require a full-file pass that schema inference does not
   need.
2. **`sample_limited` is inferred from the configured ceiling**, not reported by
   the extractor. If a file has exactly the ceiling number of rows it is flagged
   as limited when it was in fact complete — a conservative direction.
3. **OCR still requires Tesseract.** Absent, extraction reports
   `ocr_unavailable` and `ocr_used=false`, which is correct.
4. **The frontend still sends no `X-API-Key`** and CORS remains closed by
   default. Both are outside Phase 1's scope.
5. **`document_id` equals `content_hash`.** Two different files with identical
   bytes are one document — correct for content addressing, worth knowing.
6. The three Phase 14 recall failures remain, deliberately unfixed.

---

## 13. Phase 2+ functionality NOT implemented

Explicitly confirmed absent from this phase:

```
employee canonical entity            NOT ADDED
generic entity fallback              NOT ADDED
DB BLOB → OCR routing                NOT ADDED
schema vectors                       NOT ADDED
new Qdrant collection strategy       NOT ADDED
business identity filters            NOT ADDED
representation retrieval API         NOT ADDED
automatic document indexing          NOT ADDED
URL-column ingestion                 NOT ADDED
sync scheduler                       NOT ADDED
new frontend screens                 NOT ADDED
```

No endpoint was added. No `JobType` was added. `FILTERABLE_FIELDS`,
`DEFAULT_CANONICAL_MODEL`, `QUERY_INTENT_TERMS`, `DEFAULT_SYNONYMS`,
`DEFAULT_ABBREVIATIONS`, the storage policy and the Phase 14 relevance weights
are all byte-for-byte unchanged.

---

## 14. Final regression

```
collected: 3071
passed:    3008
failed:    0
errors:    0
skipped:   63     (identical to baseline)
duration:  405.94s (0:06:45)
```

Frontend: 26 passed (vitest).

### Preservation verification

| Check | Result |
|---|---|
| Protected modules importable | **22 / 22** |
| REST operations | **23** (unchanged) |
| `POST /v1/responses/adapt` | present |
| Canonical model | 3 entities, 14 fields — **unchanged** |
| Mapping weights | name 0.5 · type 0.2 · entity 0.2 · path 0.1 — **unchanged** |
| Mapping thresholds | high 0.75 · medium 0.5 — **unchanged** |
| Phase 14 weights | alias 0.45 · name 0.30 · entity 0.15 · identity 0.10, min 0.25 — **unchanged** |
| `DEFAULT_SYNONYMS` / `DEFAULT_ABBREVIATIONS` | 13 / 27 entries, `e → email` intact — **unchanged** |
| `QUERY_INTENT_TERMS` | 31 entries — **unchanged** |
| `FILTERABLE_FIELDS` | 5 fields — **unchanged** |
| Storage policy | age saturation 180d · residence 7d — **unchanged** |
| `JobType` | 5 members — **unchanged** |

### Files touched by Phase 1 (verified by modification time)

```
src/erp_pipeline/api/schemas.py
src/erp_pipeline/api/routers_data.py
src/erp_pipeline/ingestion/csv_ingestion.py
src/erp_pipeline/mapping/normalization.py
frontend/src/api/types.ts
tests/erp_pipeline/api/test_response_contract_correctness.py       (new)
tests/erp_pipeline/mapping/test_identifier_tokenization.py       (new)
tests/erp_pipeline/ingestion/test_csv_ingestion.py               (one test updated)
docs/api_contract_correctness.md                (this report)
```

`artifacts/openapi_contract_snapshot.json` is regenerated by an existing contract test and
now carries `rows_sampled`, `sample_limited` and the nullable `rows_observed`.

**Nothing was committed.**
