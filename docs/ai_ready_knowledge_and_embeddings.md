# AI-Ready Knowledge and Generic Embeddings

Project `R26-SE-034` — ERP-Aware Data Transformation Pipeline
Component owner: IT22267290

**Status: Phase 11 — implemented and live-verified.** `src/erp_pipeline/ai/`,
154 tests. Every number below was measured.

---

## 1. Purpose

Phases 4–10 discover, map, transform, validate and incrementally track ERP
data. Phase 11 answers the next question:

> How do valid ERP records and documents become deterministic, traceable,
> model-versioned AI-consumable representations — and then embeddings?

## 2. Architecture

```
CanonicalRecord                     ExtractedDocument (PDF / image OCR)
      │                                        │
      ▼                                        ▼
deterministic text                    page-traced chunks
+ structured payload                           │
      │                                        ▼
      └──────────────┬──────────── AIRepresentation  (Phase 10's, reused)
                     ▼
               content_hash
                     ▼
              EmbeddingService        batched · streaming · skip-aware
                     ▼
               EmbeddingRecord
                     ▼
                VectorStore           handoff only — Phase 12 routes
```

## 3. AIReadyRepresentation — reused, not reinvented

Step 3 said inspect before inventing. Phase 10 already defines
`AIRepresentation` in `sync/propagation.py`, carrying exactly what was asked
for: representation id, entity type, content, content hash, source record ids,
metadata.

Critically, **Phase 10's skip-if-unchanged logic is built on that hash**. A
second near-identical `AIReadyRepresentation` with its own hash formula would
fork the convention, and the two would disagree the moment either changed — the
symptom being either "everything re-embeds every run" or "nothing ever
re-embeds", both silent.

So Phase 11 imports it, and `ai/hashing.py` re-exports Phase 10's
`representation_content_hash` rather than defining a hash of its own. Two tests
assert the identity of both objects.

Phase 11 adds only what genuinely did not exist: `DocumentChunk`,
`ChunkingConfig`, `EmbeddingRecord`, `EmbeddingStatus`, `EmbeddingOptions`,
`EmbeddingRunSummary`, and the model/vector abstractions.

## 4–7. Canonical record projection

```
Entity: Invoice
Source Entity: fin_invoice
Source System: erp_a
Amount: 2500.50
Currency: LKR
Customer Id: C001
Invoice Id: INV-001
Status: approved
```

- **Identity**: `ai:{entity_type}:{canonical_record_id}` — deterministic, no
  UUIDs, no timestamps. Measured: `ai:invoice:erp_erp_a_invoice_inv-001`.
- **Field order is alphabetical, not insertion order.** Less pretty, completely
  reproducible: two records with identical content built by different code paths
  must not produce different text and therefore different hashes.
- **Business content only.** ~28 operational keys (`mapping_id`,
  `transformation_engine_version`, `run_id`, `created_at`, `rules_applied`, …)
  are excluded from both the text and the hash, so an engine upgrade does not
  look like a content change.
- **Structure survives beside the text** — `content` keeps the typed payload,
  `metadata` keeps provenance for Phase 12 to route on.
- **Nulls are omitted**, so "absent" and "null" are not a semantic difference.
- **`Decimal` prints exactly**, never through `float`.
- **Bounded visibly**: over `max_characters` the text ends with
  `[content truncated]` rather than relying on the model's silent truncation.

## 8. Content hash

Deterministic SHA-256 over the semantic content, via Phase 10's helper. Volatile
keys are stripped at any depth. Measured: the same record projects to the same
hash every time; adding `run_id` / `duration` changes nothing; changing a
business field changes it.

## 9. Phase 10 compatibility

A test asserts the projected hash equals what Phase 10's own
`representation_content_hash` produces for the same inputs, so Phases 9, 10 and
11 agree on what "unchanged" means. Phase 10 was not modified.

## 10–14. Documents and chunking

`ExtractedDocument → page spans → DocumentChunk[] → AIRepresentation[]`.

The module **does not import `erp_pipeline.ingestion`** — it duck-types on
`pages` / `file`, and additionally unwraps Phase 6's `DocumentFileResult`. That
keeps the AI layer independent of how a document was obtained.

| Setting | Default |
|---|---|
| `max_characters` | 800 |
| `overlap_characters` | 100 |
| `min_characters` | 40 |
| `boundary_search_window` | 200 |

Character-based and openly so: the tokenizer is available but its limits differ
per model, and a budget a reader can verify by counting is worth more here than
a derived one they cannot. 800 characters sits well inside MiniLM's window.

**Page provenance is preserved** (`page_start` / `page_end`), so a retrieval
answer can cite a page. **Chunk ids encode the chunking configuration**, because
chunk 3 at 800 characters is not chunk 3 at 400 — without that, a config change
would silently overwrite unrelated vectors. Boundary selection prefers
paragraph → sentence → line → space, by fixed `rfind`, so it cannot tie
differently on two runs. A document shorter than `min_characters` is kept as one
chunk rather than silently dropped.

## 15–18. Embedding model

| Fact | Measured value |
|---|---|
| model_id | `sentence-transformers/all-MiniLM-L6-v2` |
| dimension | **384** |
| library_version | `sentence-transformers 5.6.1` |
| normalizes_output | **true** |
| loads per service instance | **1** |

Nothing outside `embedding.py` / `model_registry.py` imports
`sentence_transformers` — asserted by an AST test.

**Normalization was measured, and it contradicted the obvious assumption.**
`normalize_embeddings` defaults to `False`, yet the output norm is 1.0000 — the
model's own pipeline ends in a `Normalize` module. The first draft of this code
asserted the opposite; the test failed and the claim was corrected.
`cosine_similarity` still divides by the norms: it costs nothing measurable, it
keeps the function correct for models that do *not* normalize, and it keeps the
definition of cosine in one visible place. What is avoided is asking the *model*
to normalize a second time.

Dimension is asked of the model and verified against every vector produced; a
mismatch raises `EmbeddingDimensionError` rather than surfacing as a driver
error.

## 19–23. Embedding records, identity, batching

`EmbeddingRecord` = embedding_id · representation_id · content_hash · model_id ·
dimension · status · vector · reason.

**Identity is `hash(representation_id + model_id)`** — deliberately *not*
content-derived, so changed content **updates** one logical embedding rather
than minting a new one, while a model change produces a distinguishable one.

`embed_many` consumes an **iterable** and materializes one batch at a time.
Measured: 10 representations at `batch_size=4` produce encode calls of sizes
`[4, 4, 2]`; at `batch_size=64`, exactly one call of 10.

## 24–25. Skip / re-embed policy

```
force                 -> embed
no previous record    -> embed
content_hash moved    -> embed
model_id changed      -> embed      (even with identical content)
otherwise             -> SKIPPED_UNCHANGED
```

Measured:

```
first embedding           : generated
same content, same model  : skipped_unchanged
changed content           : generated
same content, other model : generated
```

Statuses: `GENERATED · SKIPPED_UNCHANGED · EMPTY_CONTENT · FAILED`. Nothing is
hidden — a failure is a record with a reason, not a silent omission.

## 26–28. Failures, empty content, budget

`CONTINUE` (default) records the failure and keeps going; `FAIL_FAST` raises.
Empty or whitespace-only content becomes `EMPTY_CONTENT` and is **never
embedded**: a vector of nothing is a valid vector pointing nowhere, and storing
one quietly pollutes retrieval. Failure reasons name exception types and
dimensions — never business content.

## 29–30. Text builder and sensitivity

The builder is generic: nothing in it knows what an invoice is, which is why
the same code serves invoices, customers, purchase orders, payments and
anything a later mapping profile introduces.

**No claim is made that embedding content is non-sensitive.** ERP records
contain sensitive business data by definition. The representation carries the
canonical `sensitivity` classification forward into its metadata and into the
vector payload, so Phase 12 can route on it. No heuristic sensitivity detector
was invented.

## 31–32. Vector store

`VectorStore` protocol: `upsert_embedding` · `delete_embedding` ·
`get_metadata`. Vector identity is Phase 10's `vector_id_for` (uuid5), so an
update replaces one point rather than accumulating.

`QdrantVectorStore.collection_name` is a **required** constructor argument —
defaulting it to a deployment's collection would put an accidental production
write one forgotten argument away. A test asserts no default, and another
asserts the literal `bpi2020_erp_knowledge` appears nowhere in the package's
code.

Payload: representation_id, entity_type, content_hash, model_id, dimension,
source record ids, canonical id, source system/type, sensitivity, and document
page range. **Text is excluded by default** — including it would double storage
and make the index a second copy of the corpus.

## 33–36. Live Qdrant

**LIVE VERIFIED.** The project's own stopped `bpi-qdrant` container was started
(`docker start bpi-qdrant`) — not recreated, not deleted, volumes intact. Its
collection list was empty, so nothing existing was at risk.

All generic live tests use an isolated `erp_phase11_embeddings_test`
collection, created and dropped by the fixture. The production collection is
never written.

| Live proof | Result |
|---|---|
| insert | point count 1 |
| same identity, changed content | count still **1**, stored `content_hash` updated |
| point id across updates | identical |
| delete | count 0 |
| metadata round-trip | representation_id / entity_type / source_system_id returned |
| wrong dimension vs real collection | `EmbeddingDimensionError`, count still 0 |
| full pipeline: record → representation → embedding → vector | 1 generated, 1 upserted, count 1 |

### The Phase 10 skip is closed

Phase 10 left one skipped Qdrant test. It skipped for two reasons: the server
was down, **and** the test depended on the production collection existing —
which made the proof hostage to whether anyone had run the batch pipeline.

Both were addressed honestly: the server is now running, and the test owns an
isolated `bpi_cascade_vector_test` collection so it actually exercises the live
service. It now inserts, rewrites the same point id with changed content,
asserts the count stays 1 and the payload updated, then drops its collection.
The skip condition for an unreachable server was **kept** — it was not deleted.

**Repository regression is now 0 failed, 0 skipped.**

## 37–38. Payload and dimension safety

Covered above. Dimension is checked against the collection's configured size
*before* the write, and the error names both numbers.

## 39–40. BPI compatibility

`build_embedding_text`, `make_qdrant_payload` and `make_qdrant_point_id` are
untouched, and the BPI script imports nothing from `erp_pipeline` — asserted by
test.

**The generic text deliberately differs from BPI's, and that is documented
rather than hidden.** BPI's builder emits `Record type / Title / Process type /
Content` over a *unified* record; the generic builder emits labelled canonical
fields over a *CanonicalRecord*. They are different projections of different
inputs, so equality is not expected. Phase 10's BPI cascade continues to use the
BPI builder, so no production behaviour changed. Migrating it would be a
separate, explicitly tested change.

## 41–44. Cross-source proof

| Origin | Result |
|---|---|
| PostgreSQL record | ✅ |
| MySQL record | ✅ |
| MongoDB record | ✅ |
| CSV record | ✅ |
| OpenAPI-shaped record | ✅ |
| real multi-page PDF (Phase 6) | ✅ chunked, page-traced, embedded |
| real OCR image (Phase 6, Tesseract) | ✅ embedded |

All through **one** `EmbeddingService`. Static tests assert the package contains
no source-technology branch, imports no discovery/connectors/api_specs/ingestion
module, and never imports `bpi2020`.

## 45. Similarity sanity check — measured

```
related   invoice/invoice            0.9808
related   billing wording            0.6456
unrelated invoice/purchase-order     0.5400
unrelated invoice/unrelated prose    0.0645

mean related   0.8132
mean unrelated 0.3022
separation     0.5110
```

The direction is right and the separation is clear. Note the invoice ↔ purchase
order pair at 0.54 — structurally similar ERP documents are genuinely close in
embedding space, which is exactly why no universal similarity threshold is
asserted.

## 46–48. Retrieval benchmark — measured

Hand-declared labels, never generated from the model.

```
corpus size : 8   (invoices, customers, purchase orders, payments)
queries     : 8
top-1       : 1.0   (8/8)
top-3       : 1.0   (8/8)
```

**Read honestly.** Eight synthetic records with distinctive identifiers is a
regression and sanity benchmark, not evidence of production retrieval quality.
Its real job is to fail loudly if the representation builder stops including
business content, the model is swapped for one that does not understand the
domain, or a normalization bug flattens every similarity. The asserted
thresholds are deliberately modest (top-3 ≥ 0.75, top-1 ≥ 0.5) so the test
catches regressions without manufacturing a flattering number.

### Performance (real model, measured)

| batch_size | embedded | duration | throughput | avg latency |
|---|---|---|---|---|
| 8 | 64 | 0.916 s | 69.9 /s | 14.31 ms |
| 64 | 64 | 0.561 s | **114.1 /s** | **8.77 ms** |

Batching is worth roughly 1.6× here, which is why the default is 64 — matching
the existing BPI pipeline's default so throughput stays comparable.

## 49–53. Fingerprint, determinism, normalization, floats

`ModelFingerprint` records model_id, dimension, library version *when it can
actually be read*, and measured normalization. An unknown revision is reported
as `None` rather than guessed.

Determinism is asserted within tolerance (`|a−b| < 1e-6`) rather than requiring
byte identity, because floating-point execution is not guaranteed byte-identical
across backends. Vectors are plain `float` — never `Decimal`, which belongs to
money, not to embeddings.

## 54. Privacy

| Surface | Guarantee |
|---|---|
| `EmbeddingRecord.__repr__` | no vector, no text |
| `EmbeddingRecord.to_dict()` | vector omitted unless `include_vector=True` |
| `EmbeddingRunSummary.to_dict()` | counts, ids and model facts only |
| `DocumentChunk.to_dict()` / `repr` | text omitted by default |
| vector payload | no text unless explicitly configured; no credentials |
| failure reasons | exception types and dimensions only |
| logs | nothing is logged at all |

Sentinel tests plant `SECRET_*` values and assert they never reach any of the
above — while confirming they *do* reach the embedding text, which is the job.

## 55–56. No external AI, controlled errors

No `openai`, `anthropic`, `cohere`, `google`, `mistralai`, `ollama`, `requests`,
`httpx`, `aiohttp` or `urllib` import anywhere in the package, and no
`api_key` / `bearer ` / `api.openai` vocabulary. If the local model cannot load,
`EmbeddingModelUnavailableError` is raised — there is no fallback that would
ship ERP content off the machine.

Errors: `AIError`, `EmbeddingError`, `EmbeddingModelUnavailableError`,
`EmbeddingDimensionError`, `EmptyAIContentError`, `ChunkingError`,
`VectorStoreError`, `AIConfigurationError`.

## 57. Counters

```
representations_read == generated + skipped + failed + empty
```

Asserted by test, alongside throughput, average latency, batch size and vector
upsert/failure counts.

## 58. Phase 10 integration

`Phase11EmbeddingUpdater` and `Phase11VectorRecordStore` satisfy Phase 10's
`EmbeddingUpdater` and `VectorRecordStore` protocols, so the incremental cascade
gets a real model and a real vector store **without importing
`sentence_transformers` or `qdrant_client`**. A test drives Phase 10's
`PropagationPipeline` end to end through them, and another asserts the
dependency direction: `ai → sync`, never `sync → ai`.

The adapter deliberately does **not** apply its own skip check — Phase 10 has
already compared hashes by the time it calls, and two independent skip policies
would eventually disagree.

## 59. Phase 12 boundary

No `hot_tier`, `warm_tier`, `cold_tier`, `tier_policy`, `tier_routing`,
`quantization`, `migrate_tier`, `cold_snapshot` or `archive_vector` anywhere in
the package's code, and no tier vocabulary in its public API. Asserted by two
static tests. Phase 11 decides **what** gets embedded and **how**; Phase 12 will
decide **where** the vectors live.

## Limitations

1. **The retrieval benchmark is 8 synthetic records.** A sanity check, not a
   production quality claim.
2. **Chunking is character-based**, not tokenizer-aware. Transparent and
   verifiable, but a token-budget model would pack slightly more per chunk.
3. **The BPI text builder and the generic builder differ**, deliberately. No
   migration of the production BPI path is attempted here.
4. **Qdrant's collection had no pre-existing data** in this container, so the
   live proof exercises an isolated collection rather than a populated one.
5. **No sensitivity detection.** The canonical classification is carried
   forward; nothing infers one.
6. **`normalizes_output` is probed with one string.** True for this model; a
   model that normalized conditionally would need a richer probe.
7. **Embedding output accumulates in memory** for the run summary. Input
   streams; output does not.

## Phase 12 boundary

Phase 12 owns hot/warm/cold tier policy, sensitivity-based routing,
age/access-based routing, tier migration, quantization strategy, cold snapshots
and hybrid cost/latency policy. None of it is implemented here.
