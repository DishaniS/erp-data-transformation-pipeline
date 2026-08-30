# Phase 7 — Schema Representation, Embedding and Semantic Retrieval

**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline for Legacy ERP Systems
**Student:** IT22267290 · Member 4 · R26-SE-034
**Status:** implemented and verified
**Date:** 2026-08-25

---

## 1. Problem

Phases 1–6 made ERP *content* retrievable: rows, BLOBs, uploaded documents. The
ERP's **structure** went as far as the catalog and stopped.

```
Legacy ERP / CSV  →  SourceSchema  →  erp_catalog  →  STOP
```

So *"which ERP table contains employee birth certificates?"* — the question
anyone integrating with a legacy system asks first — had no answer. The catalog
knew, exactly and completely, and nothing could ask it in words.

## 2. Previous catalog-only architecture

Discovery wrote `source_systems`, `schema_snapshots`, `source_entities`,
`source_fields` and `source_relationships` to PostgreSQL. Reaching that
knowledge required knowing a `schema_id` and issuing a structured query — which
presupposes knowing where to look, which is the thing being asked.

## 3. New schema-vector architecture

```
SourceSchema (already catalogued)
      ↓  source_entity_to_representations
AIRepresentation  (content_kind = schema)
      ↓  PERSIST_REPRESENTATIONS      ← the identical Phase 5 tail
      ↓  EMBED
      ↓  TIER_ROUTE  →  erp_vectors_hot / warm / COLD
      ↓
POST /v1/search  {"filters": {"content_kind": "schema"}}
      ↓
GET /v1/representations/{id}  →  the table's structure, in words
```

**No new collection.** Schema vectors live beside records and document chunks;
the distinction is `content_kind`, exactly as `document_chunk` was in Phase 4.

## 4. Representation granularity decision

Decided by **measurement**, not preference.

`SentenceTransformer.max_seq_length` for `all-MiniLM-L6-v2` is **256 tokens ≈
1,024 characters**. Text beyond that contributes *nothing* to the embedding.

That changes the question. One representation per entity is the obvious choice
and works for a small table — but a 200-column ERP table written as one
representation would **store all 200 columns and make roughly the first eight
findable**. The other 192 would sit in the text, look indexed, and never match
a query. That is worse than visible truncation, because nothing reports it.

**Chosen: one representation per entity, split into field groups only when the
entity exceeds the window.**

| entity | representations |
|---|---|
| 5-field `employees` | **1** (chunking invisible) |
| 60-field table | 7 |
| 200-field table | **23**, all 200 fields present |

Every chunk repeats the entity header, so a hit on the fifth field group still
says which table it is. A field definition is **never** split across chunks —
half a field block matches nothing and reads as corruption. Chunks report the
field range they cover, contiguous with no gaps or overlaps.

Field-level representations were rejected: they would multiply vectors ~20×,
and the entity representation already contains every field name, so
*"which table contains birth_certificate?"* matches without them.

## 5. Deterministic schema text format

```
Content Kind: ERP Schema
Source System: legacy_hr
Database: hrdb
Schema: public
Entity: employees
Entity Kind: table
Primary Key: employee_id

Fields:
- employee_id
  Source Type: VARCHAR(20)
  Normalized Type: string
  Primary Key: yes
  Required: yes
- birth_certificate
  Source Type: BYTEA
  Normalized Type: binary
  Nullable: yes

Relationships:
- employees.department_id -> departments.department_id (foreign_key)
```

No LLM, no external service, no sampled values, deterministic ordering and
labels, stable content hash. Field order follows the source's own `ordinal`
when discovery supplied one, declaration order otherwise — **never
alphabetical**, which would make the representation disagree with the table it
describes.

## 6. Structural fields included and excluded

**Included:** source system, database, schema, entity, entity kind, namespace,
description, primary key (composite in declared order), and per field: source
type, normalized type, primary key, unique, required/nullable, array, nested
path, semantic type (only when discovery determined one), description.

**Excluded, deliberately:** every business value. `employees` has a `salary`
column and that fact is structure; `250000` is data. A schema vector carrying
sampled rows would turn a metadata search into an unaudited data-export channel
and would leak values that never passed the sensitivity routing the record path
applies.

Nothing is inferred. `is_primary_key = false` means discovery did not declare
it a key — it is not an invitation to notice a column is called `employee_id`
and guess.

## 7. Relationship representation

Only relationships discovery actually supplied. Two similarly-named columns do
not imply a foreign key
(`test_no_relationship_is_invented_from_similar_column_names`).

Confidence is printed **only when below 1.0** — `confidence=1.00` on every
declared foreign key is noise. A relationship appears on *both* its entities,
since either is a reasonable starting point for a query.

A bug caught during implementation: `SourceSchema` validates relationship
endpoints against entity **normalized names**, not entity ids. Matching on
`entity_id` found nothing and silently dropped every relationship. Fixed, and
pinned by test.

## 8. Schema provenance

Every schema representation carries: `content_kind`, `source_system_id`,
`source_entity`, `schema_id`, `schema_name`, `schema_version`, `schema_hash`,
`entity_id`, `entity_kind`, `database_name`, `schema_chunk_index`,
`schema_chunk_count`.

`database_name` is kept **separate** from `schema_name` and never invented from
it: in PostgreSQL `public` is a schema *inside* a database, and collapsing them
loses which database a table lives in. Absent when discovery recorded none.

## 9. Identity and version semantics

The decisive audit finding:

| identifier | stability |
|---|---|
| `schema_id` | **content-addressed** — `{system}.{db}.{schema}.{hash[:12]}`, changes on every schema change |
| `entity_id` | **stable** — `{system}.{qualified_name}`, no hash |

Representation identity derives from **`entity_id`**, which gives exactly the
right behaviour without inventing a "latest" mechanism:

```
PostgreSQL catalog:  every historical snapshot, versioned   (unchanged)
Vector index:        the CURRENT structure, one per entity
```

Rediscovering a changed table produces a new `schema_id` (new catalog version,
history intact) and the **same** representation id, so the searchable structure
updates in place rather than accumulating `employees-v1`, `employees-v2`,
`employees-v3` as competing results.

## 10. Drift and update behaviour

An entity that **grows** updates in place: same representation id, new content
hash, new column searchable, old text gone.

An entity that **shrinks** leaves stale field groups behind — a 60-column table
needing seven chunks that drops to five columns leaves six describing columns
that no longer exist. Left alone they would keep answering questions about a
schema that changed, and a stale answer is indistinguishable from a current
one. The schema job therefore **prunes** representations past the entity's new
chunk count (bounded lookahead of 8).

**No automatic real-time schema synchronisation is claimed.** Refresh happens on
explicit discovery, CSV upload, or a manual re-index. Scheduled freshness is
Phase 8+.

## 11. Automatic indexing trigger

Two triggers, both calling the service layer directly — never an internal HTTP
request to this application's own API:

| trigger | effect |
|---|---|
| `POST /v1/sources/{id}/discover` | schema catalogued **and** indexed |
| `POST /v1/files/csv` | schema inferred, published **and** indexed |

`OrchestrationService.index_schema()` submits a `SCHEMA_PIPELINE` job and
returns `(job_id, status, error)`. It never raises: a schema that was discovered
and catalogued successfully is a real result, and losing it because indexing
could not be scheduled would be the wrong trade. Failures surface as a warning
naming the manual route.

Both responses gained optional `schema_index_job_id` and
`schema_indexing_status`.

## 12. CSV schema versus CSV rows

The distinction that matters most in this phase:

```
CSV upload  →  schema inferred  →  INDEXED automatically      ✓
CSV upload  →  rows             →  still require mapping      ✓ unchanged
```

A caller must not be able to learn that `INV-1` exists, or what it is worth, by
uploading a file and searching. Schema indexing is **not** a backdoor around
mapping review.

`test_a_csv_upload_never_indexes_its_rows` asserts that after a CSV upload the
only content kind present is `schema`, that `structured_record` is absent, and
that no row value appears in any stored representation.

## 13. Database-discovery behaviour

Discovery publishes to the catalog as before and now also indexes. Row data is
untouched — a discovered source still needs a structured or source-native job
before any of its records are indexed.

## 14. Representation persistence

Reuses `erp_runtime.ai_representations` unchanged. No `schema_text` table, no
`schema_representation_store`. A schema representation is an `AIRepresentation`
with `entity_type = "schema"`, persisted by the same store and resolved by the
same `GET /v1/representations/{id}` — which is the evidence that Phase 5's
generic contract was designed correctly.

`source_record_ids` is empty: a schema describes structure and derives from no
ERP row.

## 15. Embedding and storage reuse

The same local 384-dimensional `all-MiniLM-L6-v2` and the same
`EmbeddingService`. No second model, no different dimension — a different
dimension would force a separate collection and break the architecture. No
tuning to the evaluation corpus.

The same `StoragePolicyRouter` and the same HOT/WARM/COLD tiers.

## 16. Qdrant payload

Structural identity only:

```json
{
  "representation_id": "…", "content_hash": "…", "model_id": "…",
  "dimension": 384, "entity_type": "schema", "sensitivity": "internal",
  "content_kind": "schema", "source_system_id": "legacy_hr",
  "source_entity": "employees", "schema_name": "public",
  "entity_kind": "table", "schema_id": "sch_hr_1",
  "schema_version": "1", "entity_id": "legacy_hr.public.employees",
  "schema_chunk_index": 0
}
```

No schema text, no DDL dump, no sampled values. Verified:
**schema text in Qdrant = false**.

## 17. Filter contract

**11 → 13 filterable fields.** Two added, not six:

| added | why |
|---|---|
| `schema_name` | scoping to `public` / `dbo` / `sales` is a real question |
| `entity_kind` | closed enum (`table` / `view` / `collection` / …) |

**Deliberately not filterable**, though returned with every schema hit:
`schema_id`, `schema_version`, `schema_hash`, `entity_id`, `database_name`,
`schema_chunk_index`.

`schema_id` is the clearest case: it is a content-addressed *snapshot* id that
changes whenever the schema changes, so a caller filtering on one they read
yesterday gets nothing today and cannot distinguish that from "no such schema".
`source_system_id` + `schema_name` expresses what people actually ask and stays
true across versions.

Both new fields follow the full Phase 4 chain — representation metadata →
`CARRIED_IDENTITY_KEYS` → `StorageRecordMetadata` → state columns →
`_payload_for` → `_merge` backstop → `SearchHitResponse`. The structural test
`test_every_filterable_field_exists_on_the_state_row` covers them, so the
"Qdrant matched but the state backstop dropped it" failure cannot recur.

Unknown filters and undefined content kinds are still refused with 422.
`content_kind = "schema"` is now accepted **because schema representations
exist**; `schema_table` and `magic_schema` still are not.

### Capability advertisement

`GET /v1/capabilities` gained `content_kinds`, derived from the `ContentKind`
enum so it cannot drift from what the filter contract accepts — a capability
list that could claim a kind the system refuses would be worse than none.
`job_types` already advertises `schema_pipeline` automatically, for the same
reason: it is generated from the enum.

## 18. Search → resolve workflow

```
POST /v1/search
  {"query": "Which ERP table contains employee birth certificates?",
   "filters": {"content_kind": "schema"}}
        ↓  rank 1: legacy_hr / public / employees
GET /v1/representations/{id}
        ↓  birth_certificate · Source Type: BYTEA · Normalized Type: binary
```

No new endpoint was required.

## 19. Large-schema behaviour

Measured, per §4: 200 fields → 23 representations, **0 fields lost**, every
chunk within the model's window, no field definition split, contiguous field
ranges. Relationships ride with the first chunk — they describe the entity, and
repeating them on every chunk would spend the window that fields need.

## 20. Security and business-value leakage

Structure only. Enforced three ways:

1. The builder is only ever handed schema models — `test_the_builder_cannot_read_rows`
   inspects the AST for data-access calls and for imports of `sqlalchemy`,
   `psycopg2`, `pymongo`, `csv` or `pathlib`. (A text search was tried first and
   produced a false positive on the word "sampled" in the module's own docstring
   explaining why it does not sample.)
2. Representation text is asserted free of planted business values.
3. The Qdrant payload carries identity only.

Sensitivity uses existing default propagation. **No inference was added** —
classifying a schema is Phase 10's decision, not a side effect here.

## 21. Files changed

**New (4):**

| file | purpose |
|---|---|
| `ai/schema_representation.py` | the deterministic schema text builder |
| `scripts/evaluate_phase7_schema_retrieval.py` | mini-evaluation |
| `tests/erp_pipeline/ai/test_schema_representation.py` | builder tests |
| `tests/erp_pipeline/api/test_schema_search.py` | retrieval tests |
| `docs/phase7_schema_vector_retrieval.md` | this report |

**Modified (10):**

| file | change |
|---|---|
| `schemas/enums.py` | `ContentKind.SCHEMA` |
| `storage/filters.py` | `schema_name`, `entity_kind`; `EntityKind` enum validation |
| `storage/models.py` | 6 schema provenance fields |
| `storage/state.py` | DDL, `STATE_ADDED_COLUMNS`, upsert, read-back |
| `storage/migration.py` | payload keys |
| `storage/hybrid_store.py` | carry-forward |
| `ai/service.py` | `CARRIED_IDENTITY_KEYS` |
| `orchestration/models.py` | `SCHEMA_PIPELINE`, two counters |
| `orchestration/planner.py` | `SCHEMA_STAGES`, `_schema_index` |
| `orchestration/stages.py` | schema branch in `AI_BUILD`, pruning |
| `orchestration/service.py` | `index_schema()` |
| `api/schemas.py`, `api/routers.py`, `api/routers_data.py` | response fields, triggers, resolution fields |

## 22. Tests added

| file | tests |
|---|---|
| `test_schema_representation.py` | 42 |
| `test_schema_search.py` | 36 |

`test_schema_search.py` uses the **real** embedding model. A deterministic
stand-in would make every ranking assertion vacuous.

**Five existing tests were updated, all for the same legitimate reason:** they
asserted `content_kind = "schema"` was *refused*, which was correct in Phase 4
because schema vectors did not exist. They now assert a value that still does
not exist (`magic_schema`, `schema_table`), preserving the closed-vocabulary
guarantee rather than removing it.

One Phase 6 test was **sharpened rather than relaxed**:
`test_a_csv_upload_still_stops_at_schema_inference` asserted "no jobs, no
representations", which Phase 7 legitimately changes. It is now
`test_a_csv_upload_never_indexes_its_rows` and asserts the invariant that
actually matters — structure indexed, rows never — plus a new
`test_a_csv_upload_does_index_its_schema` for the addition.

## 23. Mini-evaluation

Four source systems, 24 entities, 95 fields, 24 representations, mixed dialects
(PostgreSQL, MySQL, SQL Server, MongoDB), deliberate decoys, and two systems
that both have an `employees` table. **22 queries fixed before the run and not
edited afterwards.**

```
Recall@1  0.727        business-value leakage            0
Recall@3  0.909        unresolvable schema hits          0
MRR       0.811        duplicate current representations 0
                       wrong-source under exact filter   0
                       schema text in Qdrant             False
                       reindex changed count             24 -> 24
                       other content kinds reachable     0 / 0

index per source  median 238.6 ms
schema query      median 22.5 ms   p95 29.9 ms
resolve           median  3.3 ms

GATES: PASS
```

Artifact: `artifacts/phase7_schema_retrieval_evaluation.json`.

### The failures, reported rather than tuned away

**Two queries missed the top 5 entirely, and both are datatype queries:**

```
X  "Which employee field stores binary document data?"
X  "table with a VARBINARY column for scanned manuals"
~  "decimal columns holding monetary amounts on invoices"   rank 3
```

The pattern is consistent and worth stating plainly: **the model matches entity
and field NAMES far better than type vocabulary.** `BYTEA`, `VARBINARY(MAX)`
and `binary` are near-meaningless tokens to a general-purpose sentence encoder,
while `employees` and `birth_certificate` are strong signals. A query whose only
distinguishing content is a type name has little to match on.

The first one is additionally ambiguous by construction: two systems both have
an `employees` table and the query names neither, so its strongest signal —
"employee" — cannot discriminate. Measured behaviour, unfiltered:
`legacy_payroll.employees` first, `legacy_hr.employees` third.

Neither query was reworded and no vocabulary was tuned after seeing them fail.
`test_a_datatype_query_finds_the_binary_column_within_the_top_three` asserts the
recall the system actually delivers and documents the rank-1 miss in its
docstring.

The three relationship and six entity-purpose queries all ranked 1 or 2, and
the headline query ranks **1**.

**On the latency figures:** the embedding model is real; the vector store is an
in-process tier, **not a Qdrant server**. These numbers bound representation and
query construction cost, not production network latency.

## 24. Targeted tests

`discovery`, `catalog`, `ingestion`, `ai`, `mapping`, `storage`,
`orchestration`, `api`, `sync`, `runtime`, `response_adaptation`,
`verification`:

```
2254 passed, 63 skipped, 0 failed in 382.90s
```

## 25. Full regression

| | collected | passed | failed | errors | skipped |
|---|---|---|---|---|---|
| baseline (after Phase 6) | 3329 | 3266 | 0 | 0 | 63 |
| after Phase 7 | **3408** | **3345** | **0** | **0** | **63** |

`3345 passed, 63 skipped, 30 warnings in 392.85s (0:06:32)`

The **+79** is fully accounted for:

- **+78** new tests (42 builder + 36 retrieval)
- **+1** net from sharpening the Phase 6 CSV test: one test was renamed and a
  second added, so the pair now asserts both halves of the distinction —
  structure indexed, rows never.

**Skips are unchanged at 63.** No test was skipped to avoid a failure, and the
two datatype-query retrieval failures are asserted at their true recall rather
than skipped or removed.

**A note on the stated baseline.** This phase's brief gave 3293/3230 as the
baseline, which is the figure from *before* Phase 6 completed. The measured
post-Phase-6 baseline is 3329/3266/63, and that is what the table above compares
against.

## 26. Existing artifact impact

| artifact | status |
|---|---|
| `phase12_storage_benchmark.json` | unchanged |
| `phase14_response_adaptation_evaluation.json` | unchanged |
| `phase3_multimodal_evaluation.json` | unchanged |
| `phase4_identity_retrieval_evaluation.json` | unchanged |
| `phase5_representation_resolution_evaluation.json` | unchanged |
| `phase6_automatic_document_indexing_evaluation.json` | unchanged |
| `phase13_openapi.json` | regenerated by its own test, as on every run |
| `phase7_schema_retrieval_evaluation.json` | **new** |

No prior artifact was re-run or overwritten. Phases 3–6 are re-verified by their
own suites inside the full regression.

## 27. Known limitations

1. **Datatype queries are the weak category** (§23). Recall@1 0.727 overall is
   carried down by them; name-based queries perform substantially better.
2. **No cross-source disambiguation without a filter.** Two `employees` tables
   and an unqualified query is genuinely ambiguous; `source_system_id` resolves
   it, and callers must know to use it.
3. **Resolving a wide table's chunk returns that field group, not the whole
   table.** The chunk identifies the entity, and a caller wanting every column
   must fetch the other chunks or read the catalog.
4. **A shrunk entity is pruned with a bounded lookahead of 8 chunks.** A table
   dropping more than eight field groups in one revision needs an explicit
   re-index.
5. **No scheduled freshness.** Refresh is on discovery, CSV upload, or manual
   re-index — Phase 8+ owns polling.
6. **Schema sensitivity uses the existing default.** No classification added.
7. **No Qdrant payload indexes**, consistent with Phase 4's recorded deferral.
8. **Latency measured in-process**, not against a Qdrant server (§23).
9. **`SCHEMA_MAX_CHARACTERS` is tied to one model's window.** A different
   embedding model would want a different value; it is a constant, not derived
   at runtime from `max_seq_length`.

## 28. Explicit Phase 8+ exclusions

Confirmed absent:

```
database URL detection / document-reference fetching / arbitrary HTTP retrieval
sync scheduler / continuous polling / CDC
stale document cleanup
new sensitivity inference or classification
frontend schema browser / semantic search UI / four-member integrated UI
LLM answer generation
```

**No new endpoint.** One new `JobType` (`schema_pipeline`), reachable through
the existing `POST /v1/jobs`. **No new Qdrant collection.**

## 29. Revised scope readiness

```
Structured ERP data indexing      WORKING
DB BLOB multimodal indexing       WORKING
Uploaded document indexing        WORKING
Identity-aware retrieval          WORKING
Document content resolution       WORKING
Schema vector indexing            WORKING
Semantic schema retrieval         WORKING
```

```
"Which ERP table contains employee birth certificates?"
        ↓  content_kind = schema
legacy_hr / public / employees            ← rank 1
        ↓
birth_certificate · BYTEA · binary · nullable
```

---

*See also: [Phase 6 — Automatic Document Indexing](phase6_automatic_document_indexing.md),
[Phase 5 — Representation Content Resolution](phase5_representation_content_resolution.md),
[Phase 4 — Identity-Aware Retrieval](phase4_identity_aware_retrieval.md).*
