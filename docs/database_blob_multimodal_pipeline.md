# Phase 3 — Database BLOB → PDF / Image / OCR → Vector Pipeline

**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline for Legacy ERP Systems
**Student:** IT22267290 · Member 4 · R26-SE-034
**Status:** implemented and verified
**Date:** 2026-08-25

---

## 1. The gap this phase closes

A legacy ERP keeps scanned certificates, signed contracts and profile photos in
`BYTEA` / `LONGBLOB` / `VARBINARY` / `IMAGE` columns, right beside the scalar
fields describing the same employee. Before this phase the pipeline could read a
PDF a user *uploaded*, but not the identical PDF sitting in
`employees.birth_certificate`:

| path | before Phase 3 |
|---|---|
| uploaded PDF | fully extracted, chunked, embedded |
| PDF in a BLOB column, canonical path | base64-encoded into a string field |
| PDF in a BLOB column, source-native path (Phase 2) | excluded from AI text entirely |

Either way, **nobody ever opened it.** The document existed in the database, was
carried through the pipeline, and produced no retrievable knowledge.

Phase 3 opens it. The multimodal claim in the component title was, until now,
true only for files that arrived as files.

## 2. Scope

**In scope:** binary columns on database entities, routed through the existing
PDF / image / OCR engines into the existing chunking, representation, embedding
and tiering path.

**Out of scope (Phase 4+):** retrieval-side filtering on `content_kind`,
joined document-and-record answers, and document-type classification from
content. Phase 3's obligation is to make sure the metadata those phases need is
recorded and nothing is thrown away.

## 3. What was added

Three new modules, no existing engine rewritten:

| module | responsibility |
|---|---|
| `ingestion/binary_assets.py` | bytes → classification → `ExtractedDocument`, or a stated reason |
| `ai/attached_documents.py` | association-safe representations for an attached document |
| `orchestration/multimodal.py` | pairing ERP rows with the documents their BLOBs carried |

Four existing modules were extended additively:

| module | change |
|---|---|
| `ingestion/models.py` | `FileSource.payload` — optional in-memory content |
| `ingestion/pdf_ingestion.py` | `_open` reads from `payload` when present |
| `ingestion/image_ingestion.py` | `_open_source` returns a buffer or a path |
| `orchestration/{models,planner,stages,service}.py` | the `MULTIMODAL_EXTRACT` stage |

## 4. Detection: the bytes decide

`birth_certificate` is a perfectly good name for a column holding a JPEG, a PDF,
a ZIP of both, or a TIFF someone renamed during a migration. Three signals were
available and two were rejected:

| signal | used? | why |
|---|---|---|
| column name | **no** — as format evidence | it is a business label, not a content type |
| filename extension | **no** | a BLOB has no filename |
| declared MIME string | **no** | supplied by whoever wrote the row |
| **magic bytes** | **yes** | `detect_from_signature`, the same function uploads use |

The column name *is* retained, but only as ERP context (§7) — never as evidence
of format. A PDF stored in `profile_photo` is extracted as a PDF.

**Admission** is separate from **detection**. Which fields to open at all comes
from the discovered schema (`FieldDataType.BINARY`), because discovery has
already normalised every dialect's binary spelling onto one type. Sniffing every
string column for a magic byte instead would be slower, would re-answer a
question already answered, and would eventually misfire on a text field starting
with the wrong two characters.

## 5. Reuse, not reimplementation

Nothing in this phase parses a PDF, decodes an image, or performs OCR:

```
ingestion.detection        magic bytes  -> what these bytes actually are
ingestion.pdf_ingestion    PDF          -> page text, OCR fallback for scans
ingestion.image_ingestion  image        -> dimensions + OCR
ingestion.hashing          content-addressed identity
ai.chunking                chunk_document, unchanged
```

A second implementation of any of these would drift from the first, and the
existing ones are the heavily-tested ones.

## 6. Why nothing is written to disk

Both extractors took a *path*, so the first implementation spilled each BLOB to
a temporary file. The test suite rejected it, and correctly:

```
FAILED test_no_production_module_writes_to_the_filesystem[binary_assets.py]
FAILED test_files_are_only_ever_opened_for_reading[binary_assets.py]
```

That invariant was guarding something real. Writing an employee's birth
certificate to `%TEMP%` puts it on disk **in plaintext, outside every access
control and encryption guarantee the storage tiers otherwise provide** — in a
system that AES-256-GCM encrypts its own cold tier — for the sake of handing a
parser a path it did not need.

The invariant was kept and the design changed instead. `FileSource` gained an
optional in-memory `payload`; both libraries read bytes natively
(`fitz.open(stream=…)`, `Image.open(BytesIO)`). The file-based path is
byte-for-byte unchanged, and the BLOB path touches no filesystem at all:

```
temp files created: NONE
```

`payload` is runtime-only and excluded from `to_dict()`, exactly as
`local_path` is — it is content, not identity.

## 7. The association problem — the core of this phase

Two employees are issued the same standard-form certificate:

```
EMP002.birth_certificate = bytes X
EMP003.birth_certificate = bytes X
```

Identical bytes → identical `document_id` → identical `chunk_id` → identical
`representation_id` → **identical `vector_id`**. One employee's certificate
would silently overwrite the other's, and a search for EMP002 would return a
vector that now belongs to EMP003. Silent, plausible, and wrong.

The resolution separates two kinds of identity that were previously one:

| identity | scope | value for the case above |
|---|---|---|
| `document_id` | content | **shared** — it genuinely is the same document |
| `content_chunk_id` | content | **shared** |
| `representation_id` | attachment | **distinct** |
| `vector_id` | attachment | **distinct** |

Attachment identity is `parent_record_id | source_field | chunk_id`. Nothing
about ordinary uploaded documents changes: `chunk_to_representation` still uses
the chunk id directly. This is an additional builder, not a replacement.

Measured on three employees sharing one certificate:

```
shared doc c0af62d46b40 -> 3 records: [emp002, emp003, emp006]
vector ids  7 (7 unique)     COLLISIONS 0
```

## 8. Metadata carried to the vector

Every document chunk records where it came from in ERP terms, so Phase 4 can
filter on it and Phase 5 can resolve it:

```
content_kind      document_chunk        parent_record_id  erp:legacy_hr:employees:emp002
source_system_id  legacy_hr             source_entity     employees
source_field      birth_certificate     document_type     birth_certificate
business_key_name employee_id           business_key_value EMP002
document_id       c0af62d46b40…         content_chunk_id  …
page_start / page_end / chunk_index     media_type        application/pdf
```

`document_type` defaults to the **column name**, because that is deterministic
ERP context — `birth_certificate` is what the business calls it. It is never
inferred from content, which would be a guess dressed up as a classification.

`source_record_ids` points at the parent row, so any vector can be traced back
to the record that carried it. The evaluation confirms **0 orphan
representations**.

## 9. Where the stage runs, and why

```
… → LOAD → AI_BUILD → MULTIMODAL_EXTRACT → EMBED → TIER_ROUTE
```

After `AI_BUILD`, for two concrete reasons:

1. `AI_BUILD` **assigns** `context.representations`. Producing document
   representations before it would have them overwritten.
2. Parent record ids only exist once `TRANSFORM` has run, and a document with no
   stable parent is a vector nobody can trace back.

This is the only point in the pipeline where raw `source_records` and
transformed `canonical_records` are both still on the context. The stage
**appends** to `representations` and never assigns, so the scalar records
`AI_BUILD` produced survive alongside the document chunks. Both the structured
and source-native tails carry the stage, so they remain identical to each other.

An entity with no binary columns costs one schema lookup and returns
immediately.

## 10. Pairing rows to documents

`EXTRACT` produces `SourceRecord`s; `TRANSFORM` produces `CanonicalRecord`s.
The lists are usually parallel — but a row whose identity could not be resolved
is absent from the second. Zipping blindly would attach EMP003's certificate to
EMP002 the first time any row was rejected.

Pairing is therefore positional **only when the lists are the same length**, and
matches on the canonical record's own `source_record_key` otherwise. This is
covered by `test_a_rejected_row_does_not_shift_the_pairing`.

## 11. Degradation, and what is never invented

Every field is processed independently: a corrupt contract does not stop the
profile photo beside it, and neither stops the scalar record already built.

| condition | outcome | indexed? |
|---|---|---|
| text PDF | `extracted` | yes |
| scanned PDF (no text layer) | `extracted`, OCR fallback | yes, if OCR reads it |
| image with text | `extracted`, OCR | yes |
| image with no text | `extracted`, no content | **no** — reported |
| ZIP / unknown signature | `unsupported_binary` | no |
| corrupt / truncated PDF | `unreadable` | no |
| password-protected PDF | `unreadable` | no — never bypassed |
| oversized (> 32 MB) | `too_large` | no |
| NULL / empty | `empty` | no, and not an error |
| text in a binary column | refused by `coerce_binary` | no — a schema disagreement is not base64 |

When OCR is unavailable the result is `ocr_unavailable` with empty text and a
warning — never a fabricated string, and never an empty vector presented as a
successful extraction. "We could not read this" and "there was nothing to read"
have different remedies and stay distinguishable.

A skipped asset adds a `partial_reason` to the job. The job does not fail: the
employee's name and department are still perfectly good data.

## 12. Binary safety

The requirement is that the raw value never appears in `normalized_data`,
`text_for_ai`, embedding input, logs, warnings, exceptions, job reports or API
responses.

Design decisions that enforce it rather than check for it:

- `BinaryAssetResult` **carries no bytes**. A result object holding the original
  value would put it into every log line and traceback that touched it.
- `to_dict()` excludes both bytes and extracted text.
- `original_filename` is synthesised from the content hash (`blob_c0af62d46b40.pdf`),
  so neither the ERP's vocabulary nor the content reaches an extractor's error message.
- On `IngestionError`, the exception **type name** is reported, not its message —
  an extractor is free to include file details in the latter.
- Phase 2's `normalize_values` already excludes binary fields from scalar
  normalized data.

Audited across 12,731 characters of every representation, canonical record,
asset report and warning the corpus produced:

```
LEAKAGE FINDINGS 0
```

checked for base64 prefixes of every corpus payload at 16/24/32 characters, plus
the literal markers `JVBERi0x`, `/9j/4AAQ`, `iVBORw0KGgo`, `%PDF-`, raw JPEG SOI
and raw ZIP header.

## 13. Storage

No new Qdrant collection. Document chunks are `AIRepresentation`s with
`entity_type="document"` and go through the same `EMBED` and `TIER_ROUTE` stages
as every other representation. `FILTERABLE_FIELDS` is unchanged at 5 fields —
promoting `content_kind` to a payload index is Phase 4's decision, taken when
there is a retrieval path that needs it.

## 14. Evaluation

`scripts/evaluate_multimodal_extraction.py` — six employees, four binary columns,
eleven binary values, deliberately adversarial.

```
rows                     6
binary columns declared  4
binary values present    11
OCR available            True

outcomes
  extracted            9
  unreadable           1
  unsupported_binary   1

documents indexed        7
skipped                  4
OCR-read assets          1
representations built    7

association integrity
  vector ids             7 (7 unique)
  COLLISIONS             0
  orphan representations 0
  shared doc c0af62d46b40 -> 3 records: [emp002, emp003, emp006]
  shared doc 75a8285d505a -> 3 records: [emp002, emp005, emp006]

binary safety
  LEAKAGE FINDINGS       0

GATES: leakage=0  collisions=0  orphans=0  ->  PASS
```

Artifact: `artifacts/multimodal_extraction_evaluation.json`.

The four skips are the corrupt PDF, the ZIP, and two blank images OCR found no
text in — all reported, none indexed.

**Note on `document_id` across runs.** The fixture generator embeds a creation
timestamp in each PDF, so the same *logical* content produces different bytes on
different runs and therefore a different `document_id`. Within a run, identical
bytes give an identical id — which is what the shared-document rows above
demonstrate. Identity is stable with respect to content, not with respect to the
generator.

### A counter that overstated itself

The first evaluation reported `OCR-read assets 3` when only one document had
actually been read by OCR. The other two were blank images: OCR ran, found
nothing, and the asset was then skipped for having no text to index. The counter
was incremented before that check.

An operator reading "3 OCR assets, 7 representations" would reasonably conclude
that three of the indexed documents came from OCR. The increment now happens
only once an asset is actually indexed, and `test_ocr_is_counted_only_for_assets_that_produced_a_vector`
pins the distinction.

## 15. Tests

| file | tests | covers |
|---|---|---|
| `tests/erp_pipeline/ingestion/test_database_blob_pipeline.py` | 39 | detection, extraction, degradation, collision, leakage, no-disk, OCR |
| `tests/erp_pipeline/api/test_multimodal_stage.py` | 9 | the stage: counters, appending, linkage, partial success, report safety |

The two that matter most:

- `test_the_same_document_on_two_employees_does_not_collide` — the identity
  chain that would otherwise silently overwrite one employee's certificate.
- `test_no_binary_or_base64_reaches_any_representation` — the invariant that
  raw bytes never become embedded text.

`test_the_scanned_fixture_really_has_no_text_layer` guards the TEST D fixture
itself: if it ever grew a text layer, the OCR test would pass while proving
nothing.

## 16. Changes to existing tests

One assertion was updated, deliberately and visibly:

`test_the_plan_reuses_the_existing_tail_unchanged` pins the exact source-native
stage tail. `MULTIMODAL_EXTRACT` was added to the expected tuple rather than the
test being relaxed, because the point of the test is that the source-native tail
stays identical to the structured one — and it still does.

No other existing test was modified.

## 17. Regression

| | collected | passed | skipped | failed |
|---|---|---|---|---|
| baseline before Phase 3 | 3114 | 3051 | 63 | 0 |
| after Phase 3 | **3164** | **3101** | **63** | **0** |

`3101 passed, 63 skipped, 30 warnings in 426.20s (0:07:06)`

The **+50** is fully accounted for:

- **+48** new tests (39 + 9, §15)
- **+2** automatically parametrized: `test_no_production_module_writes_to_the_filesystem`
  and `test_files_are_only_ever_opened_for_reading` iterate over every production
  module in the ingestion package, so `binary_assets.py` is now subject to the
  read-only invariant it originally violated (§6) without anyone having to
  remember to add it.

Skips are unchanged at 63 — no test was skipped to avoid a failure, and none of
the pre-existing infrastructure-dependent skips changed state.

## 18. Research artifacts

| artifact | status |
|---|---|
| `artifacts/tiered_storage_benchmark.json` | unchanged (`git diff` empty) |
| `artifacts/response_adaptation_evaluation.json` | unchanged |
| `artifacts/openapi_contract_snapshot.json` | regenerated by its own test; contains no pipeline-stage enum, so the API contract is unchanged by Phase 3 |
| mapping benchmark (test-computed) | unchanged — verified by the regression run |

Phase 14's three documented recall failures (`po-05`, `proc-02`, `sap-04`) are
untouched. They are deliberate limitations, not defects to close.

## 19. What Phase 3 does not do

- No document-type classification from content — the column name is the label.
- No retrieval-side filtering on `content_kind` — recorded, not yet queryable.
- No joined document-and-record answers.
- No new Qdrant collection, no `FILTERABLE_FIELDS` change.
- No LLM anywhere.

## 20. Limitations

1. **Text in a binary column is refused, not decoded.** If a legacy system
   base64-encodes documents into a `TEXT` column, Phase 3 will not open them.
   Guessing that a string might be base64 would be exactly the kind of invention
   this codebase refuses; such a source needs an explicit, declared decision.
2. **Admission depends on the discovered schema.** A BLOB in a column the
   dialect reports as something other than binary is not opened.
3. **OCR quality is Tesseract's.** A poor scan yields poor text, and the
   pipeline reports what it got rather than assessing it.
4. **Budgets are global**, not per-entity: 32 MB per asset, 40 pages, 40,000
   characters. A legitimately larger contract is truncated with a warning.
5. **Pairing by `source_record_key`** assumes the key value appears among the
   row's values. It does for every current transformer.

## 21. Result

```
binary/base64 leakage      0
association collisions     0
orphan representations     0
regression                 3164 collected, 3101 passed, 63 skipped, 0 failed
existing behaviour         unchanged
```

**Ready for Phase 4: YES.**

---

*See also: [Phase 2 — Generic ERP Entity Support](generic_erp_entity_support.md),
[Phase 1 — Contract and Correctness Stabilization](api_contract_correctness.md).*
