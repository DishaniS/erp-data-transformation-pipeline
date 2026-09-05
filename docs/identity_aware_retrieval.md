# Phase 4 — Identity-Aware Metadata and Exact Retrieval Filtering

**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline for Legacy ERP Systems
**Student:** IT22267290 · Member 4 · R26-SE-034
**Status:** implemented and verified
**Date:** 2026-08-25

---

## 1. Problem

Phase 3 already recorded everything needed to answer *"give me EMP002's birth
certificate"*. It recorded it, carried it as far as the representation, and then
threw it away — every field below vanished before reaching the vector store:

```
content_kind  parent_record_id  source_field  business_key_name
business_key_value  document_type  page_start  page_end  chunk_index
```

So the system could find *a* birth certificate, but could not say **whose**. For
a pipeline whose entire premise is ERP-aware retrieval, that is the difference
between a demo and a component: a semantic search for "birth certificate"
returns EMP001's, EMP002's and EMP003's, ranked by an embedding that cannot tell
them apart — because the three documents are nearly identical text.

Worse, Phase 3 had gone to real trouble to keep two employees' copies of the
*same* certificate from overwriting each other. Without Phase 4 that separation
bought nothing: the vectors were distinct and unaddressable.

## 2. Architecture

```
User query
   ↓
SearchRequest  (query + closed-set filters)
   ↓
SearchFilters  validated, unknown fields REFUSED
   ↓
to_qdrant_filter()  →  server-side Filter(must=[…])
   ↓
HOT / WARM Qdrant          COLD: filters applied to state
  ANN + payload match         BEFORE any archive is decrypted
   ↓
_merge  deduplicate, re-check filters against authoritative state
   ↓
SearchHitResponse  identity + provenance, no content
```

Nothing about the collection strategy changed. `erp_vectors_hot` and
`erp_vectors_warm` plus the encrypted cold archive are exactly as before. Every
logical distinction Phase 4 introduces lives in **metadata and filters**, which
is the whole point: a separate collection per document type would have to be
created per ERP, per entity, forever.

## 3. Metadata flow before Phase 4

The trace, and the four places each field died:

```
AIRepresentation.metadata
     │  ai/representation.py     no content_kind, no business identity  ✗ (1)
     │  ai/chunking.py           no content_kind                        ✗
     ↓
CARRIED_IDENTITY_KEYS  ai/service.py — 7 keys, none of them new        ✗ (2)
     ↓
EmbeddingRecord.metadata
     ↓
hybrid_store identity()  4 keys read                                   ✗ (3)
     ↓
StorageRecordMetadata    fields did not exist                          ✗ (4)
     ↓
_payload_for             not written                                   ✗
     ↓
Qdrant payload           up to 11 keys
     ↓
FILTERABLE_FIELDS        5 fields
```

## 4. Metadata flow after Phase 4

Every stage carries the identity through, and the *rules* at each stage are
unchanged — only the lists grew:

```
AIRepresentation.metadata   content_kind + business identity added at source
     ↓
CARRIED_IDENTITY_KEYS       7 → 16
     ↓
EmbeddingRecord.metadata
     ↓
hybrid_store identity() / ordinal()
     ↓
StorageRecordMetadata       27 → 36 fields
     ↓
_payload_for                up to 11 → up to 20 keys
     ↓
Qdrant payload
     ↓
FILTERABLE_FIELDS           5 → 11   (+3 provenance-only)
```

A structural test pins the chain so it cannot silently break again:
`test_every_filterable_field_is_carried_from_the_representation` and
`test_every_filterable_field_exists_on_the_state_row`.

## 5. Generic business identity design

The obvious implementation is an `employee_id` filter. That would be wrong the
moment a second ERP entity appears, and this component's claim is ERP
independence. Phase 2 had already established the generic form, so Phase 4 uses
it as-is:

| entity | `business_key_name` | `business_key_value` |
|---|---|---|
| employees | `employee_id` | `EMP002` |
| machines | `machine_code` | `MCH-091` |
| suppliers | `supplier_id` | `SUP-88` |
| warehouse_stock | `warehouse_id\|product_id` | `WH-1\|P-77` |

Composite keys needed no work: Phase 2 already joins declared key fields in
declared order with `COMPOSITE_KEY_SEPARATOR`, producing one stable string.
Phase 4 does not introduce a second representation of a composite key, because
two representations of one identity is precisely how a retrieval layer starts
returning the wrong row.

### Why carrying `business_key_value` is not a content leak

`EMP002` is plainly a value from the row, and the carried-metadata rule is
"structural facts only, never business content". The distinction is deliberate
and worth stating: `EMP002` is the record's **identity**, and it was already
travelling in `canonical_record_id`
(`erp:legacy_hr:employees:emp002`) — in a normalized form no caller can filter
on. Carrying the salary would be leaking content. Carrying the key that names
the row is what makes the row findable at all.

### Canonical records are not given a key they never had

Records from the mapping path (`invoice`, `customer`, `purchase_order`) carry no
business-key metadata, and none is invented for them. Deriving one would mean
guessing which canonical field looks key-like, and a filter matching a *guessed*
identity is worse than one returning nothing: the caller cannot distinguish "no
match" from "wrong match". They keep working through the original five filters,
and `business_key_value` reads as `None`.

## 6. `content_kind`

Two values exist, and the vocabulary is **closed**:

```
structured_record   the scalar ERP row
document_chunk      a chunk of an attached or uploaded document
```

Declared as `ContentKind` in `schemas/enums.py` and validated through the same
`_ENUM_FIELDS` mechanism `sensitivity` already used. `"schema"` is **refused**,
because accepting it would tell a caller the system holds schema vectors when it
does not. When they exist, the enum gains a member.

`content_kind` is orthogonal to `entity_type`. `entity_type` says what the data
is *about*; `content_kind` says what *shape* it is:

```
employees + structured_record    the EMP002 row
document  + document_chunk       a chunk of EMP002's certificate
```

Phase 4 set it at three sources, not one — an **uploaded** PDF's chunks are the
same kind of thing as one extracted from a BLOB, and a caller filtering for
document chunks must not get a different answer depending on how the document
arrived.

`entity_type` is untouched: source-native data keeps `employees`, plural, exactly
as Phase 2 chose. `test_entity_type_is_not_singularized` pins it.

## 7. `document_type`

Taken from the ERP column name, deterministically:

```
source_field = birth_certificate   →  document_type = birth_certificate
source_field = profile_photo       →  document_type = profile_photo
```

No classifier, no LLM, no inference from content. `birth_certificate` is what
the business calls it, and that is a fact rather than a guess.

## 8. `parent_record_id`

For a document chunk this is the ERP row the document hangs off:

```
parent_record_id    = erp:legacy_hr:employees:emp002
canonical_record_id = None
```

The two are kept **separate**, and neither overwrites the other. A chunk of
EMP002's certificate derives from no canonical record of its own; collapsing the
two would claim the chunk *is* the employee. Structured records are the mirror
image: `canonical_record_id` set, `parent_record_id` absent.

## 9. `source_field`

Stored and filtered **separately** from `document_type`, even though the two
hold identical values today:

```
source_field   where in the ERP record it came from
document_type  what business role the attachment plays
```

Nothing derives one from the other at read time, so the day a system stores its
contracts in a column called `attachment_3`, the two can diverge without a
migration. `test_source_field_filters_independently_of_document_type` pins them
as separate filters.

## 10. Page / chunk provenance

`page_start`, `page_end` and `chunk_index` are returned with every hit and are
**deliberately not filterable**. Two reasons, in order of weight:

1. **There is no query for them.** "Give me chunk 3 of something" is not a
   question a retrieval caller asks. They want the document; the ordinal is how
   the answer describes itself afterwards.
2. **The filter contract is string equality throughout.** `_validate_value`
   renders every value with `str()`. Page numbers are stored as the integers
   they are, so `page_start=1` would compare `"1"` against `1` and match
   nothing. The fix would be either to stringify the payload (losing the type)
   or to introduce typed filters — a real change to a load-bearing contract, for
   a query nobody makes.

A genuine page-range query would want `>=` / `<=` anyway, which this contract
does not express, so it would be a designed addition rather than a line added to
a tuple. They are exported as `PROVENANCE_ONLY_FIELDS` so the distinction is
explicit rather than an omission, and
`test_provenance_fields_are_not_filterable` asserts the refusal.

Structured records report `None` for all three rather than a fabricated `0`.

## 11. Qdrant payload changes

Metadata only. No raw bytes, no base64, no OCR text, no `text_for_ai`.

**Document chunk:**
```json
{
  "representation_id": "ai:document:…", "embedding_id": "…",
  "content_hash": "…", "model_id": "…", "dimension": 384,
  "entity_type": "document", "sensitivity": "internal",
  "content_kind": "document_chunk",
  "source_system_id": "legacy_hr", "source_entity": "employees",
  "source_field": "birth_certificate",
  "parent_record_id": "erp:legacy_hr:employees:emp002",
  "business_key_name": "employee_id", "business_key_value": "EMP002",
  "document_id": "13bdaeaf…", "document_type": "birth_certificate",
  "page_start": 1, "page_end": 1, "chunk_index": 0
}
```

**Structured record** — and note what is *absent*:
```json
{
  "representation_id": "ai:employees:erp_legacy_hr_employees_emp002",
  "entity_type": "employees", "content_kind": "structured_record",
  "canonical_record_id": "erp:legacy_hr:employees:emp002",
  "source_system_id": "legacy_hr", "source_entity": "employees",
  "business_key_name": "employee_id", "business_key_value": "EMP002",
  "sensitivity": "internal"
}
```

`_payload_for` omits null keys, and that is load-bearing rather than tidy: a
payload key present-and-null and a key absent behave differently under a Qdrant
`must` match. A structured record has no `document_type`, so the key is absent,
so a `document_type` filter correctly excludes it.

### Qdrant payload indexes

**None exist, for any filter field — before or after Phase 4.** The adapter has
never created them. Phase 4 therefore introduces no inconsistency, and adding
them only for the new fields would create one. Qdrant applies payload filters
without an index by scanning payloads within the candidate set, which is correct
and adequate at research corpus size. Recorded in §24 as a scaling limitation.

## 12. PostgreSQL state changes

Nine nullable columns on `erp_vector_storage.vector_storage_state`, added
through the mechanism the table already had:

```sql
content_kind TEXT, parent_record_id TEXT, source_field TEXT,
business_key_name TEXT, business_key_value TEXT, document_type TEXT,
page_start INTEGER, page_end INTEGER, chunk_index INTEGER
```

Registered in `STATE_ADDED_COLUMNS`, applied by `alter_state_sql()` as
`ADD COLUMN IF NOT EXISTS`, so `bootstrap` stays idempotent and an existing
research database gains them without being dropped.

Writes use `COALESCE(EXCLUDED.x, table.x)` — the same rule the existing identity
columns use — so a re-store that happens not to carry a value cannot erase one an
earlier write established.

A new index serves the identity questions:

```sql
CREATE INDEX vector_storage_state_identity_idx
    ON … (business_key_value, content_kind, document_type)
```

Separate from the existing `(entity_type, source_system_id, sensitivity)` index
because these are far more selective and are asked together, and leading with
`business_key_value` lets the common single-key lookup use the index prefix.

### Why these had to be in PostgreSQL, not only in Qdrant

`HybridVectorStore._merge` re-checks every filter against the authoritative state
row, as a backstop against a payload and a state row disagreeing. A field
present in the payload but absent from `StorageRecordMetadata` would read as
`None` there, compare unequal, and **silently drop every hit the tier had
already matched correctly** — a filter that works in Qdrant and returns nothing.
`test_every_filterable_field_exists_on_the_state_row` makes that impossible to
reintroduce.

There is one value per field, in one place. Qdrant's payload is derived from the
state row by `_payload_for`; it is not a second source of truth.

## 13. Search filter contract

**5 → 11 filterable fields**, still a closed set, still equality-only:

```
entity_type   source_system_id   source_entity   sensitivity   document_id
content_kind  parent_record_id   source_field
business_key_name   business_key_value   document_type
```

Plus 3 provenance-only fields that are returned and never matched.

The headline request:

```json
{
  "query": "birth certificate details",
  "filters": {
    "business_key_name": "employee_id",
    "business_key_value": "EMP002",
    "document_type": "birth_certificate",
    "content_kind": "document_chunk"
  }
}
```

Unknown fields are still **refused, not ignored** — the property that makes the
whole contract trustworthy, since a silently dropped filter returns a
plausible-looking unfiltered result. `UnknownFilterFieldError` names both the
offending field and the supported set; the API surfaces it as 422.

## 14. Qdrant filter construction

Unchanged in mechanism, and still genuinely server-side:

```python
Filter(must=[FieldCondition(key=f, match=MatchValue(value=v)) …])
```

`must` is AND. There is no OR, and none was introduced.
`test_the_identity_filter_is_pushed_into_the_tier` asserts the condition reaches
the tier rather than being trimmed in Python afterwards, and
`test_one_wrong_constraint_empties_the_result` asserts a contradiction returns
nothing rather than the union.

## 15. HOT / WARM / COLD behaviour

One contract, three paths, and no per-tier special cases:

| tier | how filters apply |
|---|---|
| HOT | pushed into Qdrant as a server-side `Filter` |
| WARM | identical — same builder, same conditions |
| COLD | matched against tier **state** before rehydration |

COLD required no Phase 4 work and gained the new filters for free: it filters on
`StorageRecordMetadata` via `SearchFilters.matches`, and those fields now exist
there. A filtered-out archive is still never decrypted at all — the cheapest
possible way to honour a filter.

Parity is measured, not assumed: the mini-evaluation builds the same corpus in
HOT and in WARM and compares the result signature of four queries
(**0 parity failures**), and `test_both_tiers_receive_the_same_filter` asserts
both backends receive identical conditions.

## 16. Old-vector compatibility

Vectors indexed before Phase 4 have no new metadata. The behaviour is defined
rather than left to chance:

- **They do not crash anything.** `_optional_column` (and a new `_optional_int`)
  treat an absent column and a NULL identically, so a database not
  re-bootstrapped since Phase 4 reads back `None` rather than raising.
- **The original five filters still find them.**
- **The new filters do not claim them.** A pre-Phase-4 vector genuinely has no
  `content_kind`; a Qdrant `must` condition on a key its payload lacks excludes
  it, which is correct. Inventing a value would put it in a result set it does
  not belong to.
- **An unfiltered search still returns them.**

`test_a_vector_stored_before_phase_4_does_not_break_search` pins all four.

**To populate old records**, re-run the pipeline for the affected source: the
representation ids are deterministic, `store()` overwrites the same state row and
the same vector id, and `COALESCE` preserves anything the re-run does not carry.
No bespoke migration script exists, and none is claimed.

## 17. API response changes

`SearchHitResponse` gains no new *fields* — its `metadata` mapping already
existed, and now carries the identity and provenance:

```json
{
  "representation_id": "ai:document:…",
  "entity_type": "document", "score": 0.87, "tier": "hot",
  "metadata": {
    "content_kind": "document_chunk",
    "source_system_id": "legacy_hr", "source_entity": "employees",
    "source_field": "birth_certificate",
    "parent_record_id": "erp:legacy_hr:employees:emp002",
    "business_key_name": "employee_id", "business_key_value": "EMP002",
    "document_id": "13bdaeaf…", "document_type": "birth_certificate",
    "page_start": 1, "page_end": 1, "chunk_index": 0
  }
}
```

Response keys are always **present**, `None` where they do not apply — the
opposite of the vector payload, and deliberately so. A caller reading a response
should not have to infer meaning from an absent key; a Qdrant `must` match
depends on absence. The two encodings serve different consumers.

**No extracted text.** `test_no_document_text_is_returned_yet` asserts the
certificate's contents are not in the response.

### An N+1 removed rather than widened

The endpoint called `services.storage.state.load(hit.representation_id)` **once
per hit**, while `_merge` had already batch-loaded exactly those rows to
re-check filters. Rather than adding nine more per-hit reads, `SearchHit` now
carries the state row `_merge` already had, and the endpoint reads from it. The
per-hit load survives only as a fallback. Phase 4 returns strictly more metadata
with strictly fewer queries.

(The batch load itself is `state.list_all()`, which is a full scan. That is
pre-existing, out of Phase 4's scope, and recorded in §24.)

## 18. Files changed

**New (2):**

| file | purpose |
|---|---|
| `scripts/evaluate_identity_retrieval.py` | mini-evaluation |
| `docs/identity_aware_retrieval.md` | this report |

**Modified (9):**

| file | change |
|---|---|
| `schemas/enums.py` | `ContentKind` enum |
| `ai/attached_documents.py` | constants point at the enum |
| `ai/representation.py` | `content_kind` + `_business_identity()` |
| `ai/chunking.py` | `content_kind` for uploaded chunks |
| `ai/service.py` | `CARRIED_IDENTITY_KEYS` 7 → 16 |
| `storage/models.py` | 9 fields on `StorageRecordMetadata` |
| `storage/state.py` | DDL, `STATE_ADDED_COLUMNS`, upsert, read-back, identity index, `_optional_int` |
| `storage/hybrid_store.py` | `identity()`/`ordinal()` carry, `SearchHit.state` |
| `storage/migration.py` | `_payload_for` writes the new keys |
| `storage/filters.py` | `FILTERABLE_FIELDS` 5 → 11, `PROVENANCE_ONLY_FIELDS`, `ContentKind` validation |
| `api/routers_data.py` | `_hit_metadata()`, N+1 removed |

## 19. Tests added

| file | tests |
|---|---|
| `tests/erp_pipeline/storage/test_identity_aware_retrieval.py` | 40 |
| `tests/erp_pipeline/api/test_identity_search_contract.py` | 19 |

Everything in the storage file goes through the **production write path** — real
representations, real carry rules, real `_payload_for`, real
`HybridVectorStore.store`. A test that hand-wrote a payload would prove only
that the test could write a payload.

The one that matters most is
`test_two_employees_sharing_one_certificate_are_filtered_apart`: Phase 3 stopped
the two from overwriting each other's vector, and this is where that separation
either becomes usable or turns out to have been pointless.

**Two existing tests were extended, not relaxed.**
`test_every_supported_field_is_accepted` and
`test_the_qdrant_filter_matches_the_payload_key_names` both enumerate every
filterable field — the second carries the comment *"a new filterable field was
added without extending this test"* and failed exactly as designed. The two now
share one `EVERY_FILTER` definition so they cannot drift apart. No test was
weakened to obtain a green run.

## 20. Mini-evaluation

`scripts/evaluate_identity_retrieval.py` — 9 representations across 3
employees (two sharing certificate bytes) and 2 composite-key records; 14
identity-filtered queries; the same corpus built in HOT and in WARM.

```
representations indexed  9
filterable fields        11
queries attempted        14

wrong-identity matches      0
wrong-document-type matches 0
content-kind leakage        0
incomplete provenance       0
HOT/WARM parity failures    0
unknown filters refused     7  (accepted 0)
raw-content leakage         0

latency  median 0.085 ms   p95 0.155 ms

GATES: PASS
```

Artifact: `artifacts/identity_retrieval_evaluation.json`.

Two notes on reading these numbers honestly:

- **`emp002 photo` returns 0 hits.** The profile photo is a blank image that OCR
  found no text in, so Phase 3 correctly indexed no chunk for it. Zero is the
  right answer; there is nothing to retrieve.
- **The first filtered query originally measured ~1,900 ms.**
  `to_qdrant_filter` imports the Qdrant client lazily, so the first call paid for
  the import. Timing it would have put a number in this report that describes an
  import rather than a search, so the harness now warms that path before
  measuring. The latencies above are of an in-process filter-aware tier and
  measure the **filter and merge path**, not Qdrant network time.
- **These figures carry real run-to-run variance.** A second run of the same
  harness on the same machine gave median 0.229 ms / p95 0.844 ms. At
  sub-millisecond scale the measurement is dominated by scheduling noise, so
  the useful reading is the order of magnitude — the filter and merge path costs
  well under a millisecond for this corpus — not the specific digits. A
  meaningful latency number needs a real Qdrant round-trip and a corpus large
  enough for the ANN search to dominate, which is a benchmark this phase does
  not attempt.

## 21. Targeted results

`storage`, `ai`, `api`, `sync`, `orchestration`, `transformation`:

```
1050 passed, 37 skipped, 0 failed in 243.54s
```

Phase 3's own evaluation was re-run under Phase 4 code and its gates still hold:
`leakage=0  collisions=0  orphans=0  →  PASS`.

## 22. Full regression

| | collected | passed | failed | errors | skipped |
|---|---|---|---|---|---|
| baseline (after Phase 3) | 3164 | 3101 | 0 | 0 | 63 |
| after Phase 4 | **3223** | **3160** | **0** | **0** | **63** |

`3160 passed, 63 skipped, 30 warnings in 357.69s (0:05:57)`

The **+59** is exactly the new tests: 40 + 19. Unlike Phase 3, nothing was
auto-parametrized this time — the Phase 4 modules live outside the ingestion
package the read-only invariant iterates over.

Skips are unchanged at 63 — no test was skipped to avoid a failure, and no
infrastructure-dependent skip changed state.

## 23. Research artifact impact

| artifact | status |
|---|---|
| `tiered_storage_benchmark.json` | unchanged (Aug 14) — `git diff` empty |
| `response_adaptation_evaluation.json` | unchanged (Aug 22) |
| `multimodal_extraction_evaluation.json` | re-run to confirm Phase 3 gates still hold; values identical |
| `openapi_contract_snapshot.json` | regenerated by its own test, as on every run; contains no filter-field enumeration, so the API schema is structurally unchanged |
| `identity_retrieval_evaluation.json` | **new** |

No prior artifact was overwritten to make Phase 4 look better. Phase 14's three
documented recall failures (`po-05`, `proc-02`, `sap-04`) are untouched.

## 24. Known limitations

1. **No Qdrant payload indexes.** None existed before Phase 4 either. At a
   corpus size where filtered ANN latency matters, `business_key_value`,
   `content_kind` and `document_type` are the fields that would want them.
2. **`_state_by_vector` calls `state.list_all()`** — a full scan of the state
   table per search. Pre-existing, untouched, and the obvious next performance
   fix (a `WHERE vector_id IN (…)` lookup).
3. **Canonical entities have no business key**, by choice (§5). They are
   reachable only through the original five filters until a deterministic
   derivation exists.
4. **Equality only.** No ranges, no `IN`, no negation, no OR. A page-range or
   multi-employee query is not expressible.
5. **Old vectors are excluded from the new filters** until re-indexed (§16).
6. **`content_kind` is closed to two values.** Adding `schema` requires the enum
   to change, which is intentional.
7. **Measured latency is of the in-process filter path**, not a Qdrant
   round-trip; it bounds the overhead Phase 4 adds, not end-to-end search time.

## 25. Explicit Phase 5+ exclusions

Confirmed absent:

```
GET /v1/representations/{id}        document text retrieval API
OCR text in SearchResponse          full text_for_ai in Qdrant
automatic frontend document indexing
schema representations / embeddings / vectors
database URL fetching
sync scheduler / stale-vector cleanup
new sensitivity inference
frontend search UI
```

No new endpoint was added. No new Qdrant collection was created.

## 26. EMP002 readiness

```
EMP002 structured ingestion                WORKING
EMP002 birth-certificate BLOB ingestion    WORKING
EMP002 OCR / PDF extraction                WORKING
EMP002 embedding                           WORKING
EMP002 vector storage                      WORKING
EMP002 exact identity filter               WORKING
birth_certificate document-type filter     WORKING
structured_record vs document_chunk        WORKING
page / chunk provenance                    WORKING

EMP002 document content resolution         NOT YET — PHASE 5
```

`POST /v1/search` with the four EMP002 filters returns only representations
attached to `EMP002.birth_certificate`, each naming its `representation_id`,
`document_id`, `parent_record_id`, `document_type`, page and chunk provenance,
score and tier — and none of the certificate's text.

---

*See also: [Phase 3 — Database BLOB Multimodal Pipeline](database_blob_multimodal_pipeline.md),
[Phase 2 — Generic ERP Entity Support](generic_erp_entity_support.md).*
