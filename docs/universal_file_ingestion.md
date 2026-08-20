# Universal File Ingestion

Phase 6 of the ERP-Aware Data Transformation Pipeline
(SLIIT R26-SE-034, component IT22267290).

## 1. Purpose

Answer one question:

> **What data or content exists in this uploaded file, and how can the rest of
> the pipeline consume it safely?**

Not:

> ~~What canonical ERP field does this value map to?~~ — that is Phase 8.

## 2. Architecture

Databases and CSV files expose **structure**. PDFs and images expose
**document content**. Both are legitimate ERP sources, and Phase 6 supports
both *without pretending they are the same thing*:

```
File
 │
 ├── CSV ────► encoding → delimiter → bounded row sample → type inference
 │                                            │
 │                                            ├──► SourceSchema (Phase 1)
 │                                            └──► streamed SourceRows
 │
 ├── PDF ────► page text extraction → OCR fallback for scanned pages
 │                                            │
 │                                            └──► ExtractedDocument
 │
 └── IMAGE ─► validation → properties → OCR
                                              └──► ExtractedDocument
```

```
CSV / PDF / Image
       │
       ▼
Phase 6 Ingestion            erp_pipeline.ingestion
       │
       ├── SourceSchema (CSV only) ──► Phase 2 Schema Catalog
       └── ExtractedDocument ────────► later document processing
```

Module layout, following the pure/IO split Phase 5 established:

| Module | Responsibility |
|---|---|
| `service.py` | The public entry point and format dispatch |
| `detection.py` | Type detection from signature + extension |
| `hashing.py` | Content hash and deterministic file identity |
| `safety.py` | Path validation, size limits, text budgets |
| `models.py` | Options and all source-level result models |
| `csv_inference.py` | **Pure** CSV typing rules — no I/O |
| `csv_ingestion.py` | CSV encoding, delimiter, streaming, `SourceSchema` |
| `pdf_ingestion.py` | PDF text extraction, page provenance, OCR fallback |
| `image_ingestion.py` | Image validation, properties, OCR |
| `ocr.py` | Shared OCR capability detection and invocation |
| `errors.py` | The controlled error hierarchy |

The package never imports `bpi2020` — proven by test, including a subprocess
check that importing it loads no `bpi2020` module.

## 3. Supported file types

| Type | Extensions | Result |
|---|---|---|
| CSV | `.csv`, `.tsv` | `TabularFileResult` → `SourceSchema` + rows |
| PDF | `.pdf` | `DocumentFileResult` → `ExtractedDocument` |
| Image | `.png`, `.jpg`, `.jpeg`, `.webp`, `.tif`, `.tiff` | `DocumentFileResult` → `ExtractedDocument` |

`FileType` is an ingestion-internal discriminator, deliberately **not** added
to the frozen Phase 1 `SourceType` (which already carries `CSV`, `PDF` and
`IMAGE`). `FileType.to_source_type()` is the single place the two vocabularies
are mapped.

## 4. File detection

An extension is a claim, not a fact. Detection reads both signals:

1. **Signature** — `%PDF-`, the PNG 8-byte header, the JPEG SOI marker,
   `RIFF….WEBP`, the TIFF byte-order marks. Authoritative when it matches.
2. **Text probe** — CSV has no signature, so a file is CSV-eligible only if its
   prefix decodes as text and contains no NUL byte. This is what stops
   arbitrary binary content reaching the CSV parser.
3. **Extension** — chooses between text formats and detects mismatches; never
   the sole basis for treating a file as a PDF or an image.

**Mismatch behaviour.** A `.csv` whose bytes begin with `%PDF-` raises
`FileTypeMismatchError` by default. Trusting either side silently would be
wrong, so the caller decides: `IngestionOptions.allow_type_mismatch=True`
trusts the *content* and records a `file_type_mismatch` warning. A `.png` with
no PNG signature is likewise refused rather than handed to a decoder.

A UTF-16 BOM is treated as *text* despite its NUL bytes, so the caller gets the
reader's actionable "set `encoding='utf-16'`" message instead of a blanket
"looks binary" rejection.

`python-magic` is deliberately not used: it is not installed here and would add
a libmagic system dependency to detect five formats.

## 5. Content identity

```
content_hash = SHA-256 over the file bytes (streamed in 1 MiB chunks)
file_id      = file.sha256.<hex digest>
```

Identity is what is *inside* a file, never where it sits or what it is called:

- same bytes, different filename → **same** `file_id`;
- one byte changed → **different** `file_id`;
- no timestamp, no UUID, no path participates.

Every character of a `file_id` is drawn from `[a-z0-9.]`, so it is already a
valid normalized identifier under Phase 1's rules and can be embedded in
composite ids unchanged. `parse_file_id()` recovers the digest, so a stored id
can be matched against a freshly hashed upload.

## 6. Provenance

`FileProvenance` records the pointer plus the extraction facts, never a second
copy of the data — the rule Phase 1's `RecordProvenance` follows:

`file_id` · `content_hash` · `original_filename` · `file_type` · `media_type` ·
`size_bytes` · `extractor` · `extractor_version` · `encoding` · `delimiter` ·
`row_count` · `column_count` · `page_count` · `ocr_engine` ·
`ocr_engine_version` · `extracted_at`

**Absolute workstation paths are never part of the portable payload.**
`FileSource.local_path` is a runtime handle for the parser and is excluded from
`to_dict()`; `to_dict(include_local_path=True)` emits it under the explicitly
named key `runtime_local_path`. `extracted_at` is operational only and feeds no
hash or identity.

## 7. CSV ingestion

```
CSV → encoding → delimiter → header → bounded row sample → SourceSchema
                                                        → streamed SourceRows
```

The standard library `csv` module is used, not pandas. pandas is a project
dependency but the wrong tool here: it reads a frame into memory (defeating
streaming), applies its own type coercion (pre-empting the inference this phase
must perform explicitly), and rewrites duplicate headers to `amount.1` before
this code would ever see them — destroying exactly the source fidelity §9
requires.

## 8. CSV delimiter handling

Comma, semicolon, tab and pipe are supported. `csv.Sniffer` is **not** trusted
on its own — it raises on single-column files, is confused by punctuation
inside quoted text, and is not stable across similar inputs. Instead each
candidate is scored by parsing the first N lines with it and asking:

1. does it split the header into more than one field?
2. do the data rows agree with the header's field count?

Ties break on a fixed priority order (comma first), so the same file always
yields the same answer. `CsvOptions.delimiter` overrides detection entirely.
The chosen delimiter is recorded in provenance and entity metadata.

## 9. CSV encoding and headers

**Encoding.** An explicit `CsvOptions.encoding` always wins; a UTF-8 BOM
selects `utf-8-sig` (stripping it, so the first header is not corrupted);
everything else is read as UTF-8 with `errors="strict"`. A non-UTF-8 file
produces `MalformedCSVError` naming the **byte offset** — never a best-effort
decode with replacement characters presented as success. No statistical
encoding detector is installed, and guessing an encoding from byte frequencies
is precisely the silent, occasionally-wrong decision that corrupts ERP data.

**Headers.** Preserved exactly as written — spaces, punctuation, quoting,
non-ASCII — as `SourceField.source_name`. Only the *normalized* name is
adjusted:

| Situation | Behaviour |
|---|---|
| Duplicate names (`amount`, `Amount`, `amount`) | All kept; normalized to `amount`, `amount.2`, `amount.3`. **No column is ever dropped.** |
| Blank header cell | Becomes `column_N` positionally, with a warning — dropping it would misalign every row |
| Header normalizing to nothing (`###`) | Deterministic content-derived `column.<hash>` fallback |
| `has_header=False` | Positional `column_1…N`; the first line is treated as data |

## 10. CSV type inference

Each cell is classified into a category, then the column's categories are
resolved to one existing `FieldDataType`. No competing type enum is introduced;
the categories are an internal parser vocabulary.

| Category | `FieldDataType` | Notes |
|---|---|---|
| empty / null marker | — | carries no type evidence |
| `boolean` | `BOOLEAN` | **only** `true`/`false` |
| `integer` | `INTEGER` | zero-padded values are *not* integers |
| `decimal` | `DECIMAL` | needs `.` or an exponent; NaN/Infinity rejected |
| `date` | `DATE` | `YYYY-MM-DD`, `YYYY/MM/DD` |
| `datetime` | `DATETIME` | ISO-8601, including offsets and `Z` |
| `string` | `STRING` | the fallback, never a preference |

Three deliberate refusals:

- **`007` is a string, not 7.** A leading zero is almost always significant — a
  cost centre, an account code, a country prefix — and parsing it away destroys
  information irreversibly.
- **`1`/`0` and `yes`/`no` are not booleans.** `1`/`0` are far more often
  quantities; `yes`/`no` is ordinary domain vocabulary.
- **`03/04/2026` stays a string.** It is two different dates depending on
  locale, and no amount of sampling can settle which. A mapping profile can
  convert it later, once a human has stated the locale.

## 11. CSV mixed types

The full distribution is preserved in field metadata
(`value_category_distribution`, `mixed_types`). Resolution is deterministic:

| Observed | Result | Why |
|---|---|---|
| only empties / null markers | `UNKNOWN` | an absent value reveals no type |
| one category | that type | |
| `INTEGER` + `DECIMAL` | `DECIMAL` | widening is lossless |
| `DATE` + `DATETIME` | `DATETIME` | same family, wider member |
| anything else | `STRING` | |

That last rule differs from Phase 5's MongoDB policy, which returns `UNKNOWN`
for incompatible types — and the difference is principled, not an
inconsistency. In MongoDB an `int` really is an `int` and a `string` really is
a `string`, so a field holding both has **no** common type. In a CSV *every
cell is already text*, so `STRING` is a statement that is true of every single
observed value. It loses nothing (the raw bytes are preserved verbatim in
`SourceRow`) and lets a later mapping profile apply a business-aware
conversion.

`source_data_type` renders as `mixed<decimal|integer>` — category names sorted,
so the rendering depends only on *which* categories were seen, never how many.
That matters because it feeds the structural hash.

## 12. Null and presence handling

Three states are distinguished:

| State | Meaning |
|---|---|
| missing | the row had no cell for this column (a short row) |
| empty | the cell existed and was blank |
| null marker | the cell matched a **configured** null token |

`CsvOptions.null_tokens` is **empty by default**. Silently reading `N/A` as
null would destroy the distinction between "no value" and "the literal text
N/A", which some ERP exports use as a real value. Matching is
case-insensitive unless `case_insensitive_null_tokens=False`.

`required` is set only when every sampled row had a cell and none was empty or
a null marker — **observed** requiredness over a bounded sample, never a
constraint. A CSV declares none.

## 13. CSV SourceSchema integration

One CSV file → one `SourceEntity` of kind `DATASET` inside one `SourceSchema`:

```
SourceSchema (origin = INFERRED)
  └── SourceEntity (entity_kind = DATASET, source_name = "invoices.csv")
       └── SourceField (ordinal, source_data_type, normalized_data_type, …)
```

- **`SchemaOrigin.INFERRED`**, never `DISCOVERED` — the structure was read off
  rows, not from a declared catalog.
- **No primary key is invented.** A CSV declares no key, and a distinct-looking
  column is not one. `primary_key_fields` is always empty and `is_unique` is
  always false.
- **No relationships are invented.** Two files that look joinable are not a
  declared constraint.
- **No semantic types.** `cust_no`, `email_addr` and `total_amt` get *types*
  (`STRING`, `STRING`, `DECIMAL`), never *meanings*.

**Identity versus scope** — these answer different questions and must not be
conflated:

| Question | Answer | Derived from |
|---|---|---|
| Which bytes are these? | `file_id` | content hash |
| Which dataset is this? | `schema_name` | filename stem |

`schema_name` deliberately excludes the content hash: it is the stable scope
Phase 2 versions snapshots within, so an edited CSV must increment the existing
history rather than starting a fresh version 1. The snapshot id is
content-addressed exactly as in Phase 4/5:

```
schema_id = {source_system_id}.{schema_name}.{structural_hash[:12]}
```

**Structural vs incidental.** `SourceSchema.compute_schema_hash()` — the
existing Phase 1 algorithm, not a new one — covers column existence, types and
the `required`/`nullable` flags. Row counts, sample sizes, encodings, warnings
and the ingestion timestamp live in unhashed metadata, so widening
`max_rows_for_schema_inference` over a uniform file produces **no** new catalog
version, while a new column, a removed column or a type change does.

## 14. CSV streaming

Schema inference reads a bounded sample (`max_rows_for_schema_inference`,
default 1000); `iter_records()` re-opens the file and streams every row lazily,
one physical row at a time. Rows are never held on the result object.

Proven by measurement, not assertion: quadrupling the input file grows peak
memory by less than 1.5×, where buffering would put that ratio near 4.0.

## 15. Source records

```python
for row in result.iter_records():
    row.row_number      # 1-based position in the data section
    row.values          # {normalized_field_name: raw source string}
    row.file_id         # provenance back to the exact bytes
    row.missing_fields  # header columns this physical row lacked
    row.extra_value_count
```

Values are the source's own strings, **unconverted**. A conversion applied here
would be an irreversible guess made by the wrong layer. Nothing builds a
`CanonicalRecord` — that requires a mapping profile, which is Phase 8.

A row the parser cannot read raises `MalformedCSVError` *here*, whereas schema
inference merely warns and skips it. The asymmetry is deliberate: inference
reads a sample to describe structure, so skipping one row costs nothing and is
reported; this iterator is the **data handoff**, and silently dropping a row
would lose business data a mapping phase would never know was missing.

## 16. PDF extraction and page provenance

PyMuPDF (`fitz`), imported lazily. Extraction is page by page and page
boundaries are preserved:

```
ExtractedDocument
  pages:
    - page_number: 1   text: …   extraction_method: text_layer   char_count: …
    - page_number: 2   text: …   extraction_method: ocr          char_count: …
```

`document_text` is a deterministic join in page order using `\f`, so page
boundaries stay recoverable from the joined string. Merging pages irreversibly
would break a later retrieval phase's ability to cite "page 4".

PDF-declared metadata (title, author, subject, creator, producer) is captured
on `document_metadata` and treated as **content**, not operational metadata — a
document title routinely contains a customer or project name.

## 17. Scanned PDFs and OCR fallback

A page whose text layer yields fewer than `ocr_min_text_chars` (default 16) is
treated as a probable scan and rendered in memory at `ocr_render_dpi` for OCR.
Nothing is written to disk.

Two rules keep this honest:

- **OCR never overwrites real text.** Its output is used only when it recovered
  *more* than the text layer did.
- **A thin text layer is never discarded when OCR is unavailable.** The page
  keeps whatever text it produced and a `ocr_unavailable_low_text` warning is
  recorded. Only a page with *no* text at all becomes
  `ExtractionStatus.OCR_UNAVAILABLE`.

(The second rule exists because the first implementation got it wrong: a page
reading "PAGE MARKER 1" — 13 characters, below the threshold — had its real
text thrown away when OCR was missing. There is a regression test.)

Text PDF extraction works whether or not Tesseract is installed.

## 18. Image ingestion and OCR configuration

An image produces an `ExtractedDocument` with exactly one page. That is not a
workaround: a scanned receipt *is* a one-page document, and it means PDF and
image results share one shape.

Recorded properties: `format`, `mode`, `width`, `height`, `frame_count`, plus
the content hash and OCR engine metadata. Images are never forced into a
`SourceSchema` — a photograph of an invoice has no columns.

**OCR configuration**, resolved in this order, with no developer-specific path
hardcoded anywhere:

1. `IngestionOptions.tesseract_cmd`
2. the `TESSERACT_CMD` environment variable (this repository's convention)
3. the `TESSERACT_PATH` environment variable (its older name, still honoured)
4. ordinary PATH discovery via `shutil.which`

Production code never reads `.env` — that is an application concern; the test
suite loads it.

## 19. Extraction states

Missing OCR is never reported as "no text found". Those are different facts
with different remedies, and collapsing them would leave a later phase unable
to tell whether re-running would help.

| Status | Meaning |
|---|---|
| `EXTRACTED` | Content was read successfully |
| `NO_CONTENT_DETECTED` | Extraction succeeded; there was nothing to read |
| `PARTIAL` | Some content read, but a budget or a page failure intervened |
| `OCR_UNAVAILABLE` | The content needs OCR and OCR could not run |
| `FAILED` | The engine was present and errored |

## 20. Privacy model

Phase 6 is the first part of this framework that deliberately **retains**
source values — a mapping phase cannot transform data it never received. The
privacy rule is therefore two-sided, and enforced structurally rather than by
convention:

| May hold source values | May never hold source values |
|---|---|
| `TabularFileResult.iter_records()` | every `to_dict()` (default) |
| `ExtractedPage.text` | `SourceSchema` and all field metadata |
| `ExtractedDocument.document_text` | `ExtractionWarning` |
| `ExtractedDocument.document_metadata` | every exception message |
| | logs |

`to_dict()` defaults to the **operational** form because that is what gets
logged, diffed, summarized and published; content requires
`to_dict(include_text=True)`. Warnings and errors carry positions only — a row
number, a page number, a byte offset, a limit — because the position is what
identifies a problem and a position is not sensitive. Nothing in the package
logs anything, and in particular nothing logs "the first N characters" of
extracted text.

`tests/erp_pipeline/ingestion/test_ingestion_privacy.py` plants sentinels
(`SECRET_CUSTOMER_EMAIL_92831`, `SECRET_IBAN_55231`,
`SECRET_INVOICE_88192`) in CSV values, PDF text and image OCR text, then asserts
they **are** present in source content and **absent** from schemas, summaries,
warnings, exceptions and captured logs.

One consequence worth stating: a column *named* `password` is structure and is
described; the password itself never is.

## 21. Safety budgets

| Option | Default | On reaching it |
|---|---|---|
| `max_file_size_bytes` | 256 MiB | `FileTooLargeError`, checked from filesystem metadata **before** opening |
| `max_rows_for_schema_inference` | 1000 | Sampling stops; the full file stays streamable |
| `max_columns` | 512 | `MalformedCSVError` |
| `max_field_length` | 128 KiB | Controlled error; bounds the `csv` module's own allocation |
| `max_errors` | 100 | `MalformedCSVError` after too many malformed rows |
| `max_pages` | 500 | Extraction stops; the true page count is still reported |
| `max_text_chars_per_page` | 200 000 | Page truncated and flagged |
| `max_total_text_chars` | 5 000 000 | Document-wide budget; per-page caps alone are insufficient |
| `max_pixels` | ~64 MP | `ImageDecodeError` — a decompression-bomb guard checked on the header, before decoding |

Path validation rejects directories, missing paths, unreadable files and
anything that is not a regular file (a FIFO or device node has no bounded
size).

## 22. Deterministic identity and idempotency

Same bytes + same options produce the same `content_hash`, the same `file_id`,
the same `SourceSchema`, the same structural hash and the same page ordering.
No timestamp enters identity or any hash.

Verified against the real PostgreSQL catalog:

| Action | Result |
|---|---|
| Ingest CSV → publish | `created=True`, `catalog_version=1` |
| Re-ingest unchanged → publish | `created=False`, `catalog_version=1` |
| Add rows of the same shape → publish | `created=False`, `catalog_version=1` |
| Add one column → publish | `created=True`, `catalog_version=2` |

with `SchemaDiff.added_fields == (("invoices", "currency"),)`.

## 23. Catalog integration

CSV publishes through the existing Phase 2 catalog with no architectural hack:
the catalog's `source_type` and `origin` columns are plain text with no
relational-only constraint, and `SourceType.CSV` is already in the frozen
Phase 1 vocabulary. `FileIngestionService.source_system()` builds a correctly
formed `SourceSystem`, and `ingest_and_publish()` forwards the schema.

Phase 6 duplicates **none** of the catalog's logic — idempotency,
`catalog_version` assignment, immutability and history remain Phase 2's.

**PDFs and images are deliberately not published.** The catalog stores
structural descriptions; a document has none, and publishing an empty schema
for one would put a meaningless row in the catalog and imply a capability that
does not exist. `ingest_and_publish()` raises `UnsupportedFileTypeError` for
them.

## 24. The source/canonical boundary

Phase 6 stops at the source level:

- **No `CanonicalRecord`** is built from a CSV row.
- **No `CanonicalDocument`** is built from a PDF or image. `CanonicalDocument`
  is a *canonical* artifact — it subclasses `CanonicalEnvelope`, carries an
  `erp:` record id, a `RecordType` and a `SensitivityLevel`, and lives in the
  module documented as "the canonical ERP representation every source
  converges on". Producing one here would mean the convergence had already
  happened. `ExtractedDocument` is the source-level stand-in, deliberately
  shaped so a later phase can build a `CanonicalDocument` from it.
- **No semantic mapping, no embeddings, no vector storage, no API.**

Enforced by AST tests over the package, not just by review.

## 25. Live verification status

**VERIFIED** on real files through the public `FileIngestionService.ingest()`
path.

| Source | Status | Evidence |
|---|---|---|
| CSV | **VERIFIED** | 14 committed fixture files plus generated cases; schema produced, rows streamed |
| PDF | **VERIFIED** | Multi-page text layer, page ordering, a hand-written raw-bytes PDF, corrupt and encrypted cases |
| Image | **VERIFIED** | PNG, JPEG, WEBP; properties and dimensions read from the real files |
| OCR | **VERIFIED** | Tesseract 5.5.0.20241111 — real text recovered from a PNG, a JPEG, and a scanned image-only PDF page |

## 26. Limitations

Honest constraints, not defects:

- **Type inference is a sample-derived observation**, bounded by
  `max_rows_for_schema_inference`. A column that is integer for 1000 rows and
  textual on row 5000 will be described as integer.
- **Requiredness is observed, not enforced.** A CSV declares no constraints.
- **Locale-dependent dates stay strings** (§10).
- **No delimiter beyond comma/semicolon/tab/pipe** is detected, though any
  single character can be supplied explicitly.
- **No statistical encoding detection.** UTF-8 and UTF-8-BOM are automatic;
  anything else must be stated.
- **Multi-frame images** report `frame_count`, but only the first frame is
  OCR'd.
- **OCR accuracy is Tesseract's**, and is not measured or corrected here.
- **Arrays of PDF tables are not detected.** Table extraction is a different
  problem from text extraction and is not attempted.
- **One file per call.** Enumerating a folder is the caller's decision.
- **Encrypted PDFs are refused**, never decrypted, even with a known password.

## 27. Phase 7 boundary

Not implemented here, by design:

- Swagger/OpenAPI and Postman ingestion.
- Source-to-canonical mapping, semantic classification, mapping execution.
- Canonical ERP transformation of rows or documents.
- Embeddings, Qdrant, hybrid tiered vector storage, RAG.
- REST/SOAP execution, an upload API, a UI, background workers.
- Cloud object storage, authentication and authorization.

Phase 6 ends where a file's structure has been described and its content made
available to the rest of the pipeline.
