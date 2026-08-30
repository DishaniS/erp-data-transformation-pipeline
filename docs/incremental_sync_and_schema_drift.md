# Incremental Sync and Schema Drift


> **Consolidation note (2026-08-21).** This document is a development record
> for its phase. It refers to `src/bpi2020/` and/or `src/erp_integrations/`,
> which no longer exist: both were consolidated into `src/erp_pipeline/`. The
> behaviour described is preserved, but the module paths below are historical.
> See `docs/architecture/ARCHITECTURE_CONSOLIDATION_REPORT.md`.

Project `R26-SE-034` — ERP-Aware Data Transformation Pipeline
Component owner: IT22267290

**Status: Phase 10 — implemented and live-verified.**
`src/erp_pipeline/sync/` (generic core) + `src/erp_integrations/` (BPI
production adapter), 213 tests. Every value below was measured, including the
BPI cascade proofs, which run against real PostgreSQL tables.

---

## 1. Purpose

Two independent questions:

1. **Data change** — what changed since the last successful sync, and how do we
   propagate *only* that through the downstream pipeline?
2. **Structural change** — did the schema change, and does that invalidate an
   active `MappingProfile`?

## 2. Architecture

```
DATA CHANGE                          STRUCTURAL CHANGE
Source changed                       Current SourceSchema + previous
     ↓                                        ↓
Incremental extraction               SchemaDiff (Phase 2, reused)
     ↓                                        ↓
Phase 9 TransformationService        Mapping impact analysis
     ↓                                        ↓
CanonicalRecord                      continue / review / BLOCK
     ↓
Canonical upsert (idempotent)
     ↓
Affected AI representations ONLY
     ↓
Rebuild → deterministic content_hash
     ↓
hash changed?  NO → skip embedding
               YES → embed → update the SAME vector identity
     ↓
Advance checkpoint — only past changes that finished every stage
```

## 3. The problem this fixes

The prototype's incremental path ends at
[realtime_incremental_sync.py:508](../src/bpi2020/sync/realtime_incremental_sync.py):
it upserts `cleaned_event_logs`, calls `update_sync_state()`, and stops.

Everything downstream already existed and was already content-hash aware —
`build_ai_ready_cases.py` resets `embedding_status='pending'` only when
`content_hash` changes, and `generate_and_store_embeddings.py` embeds only
pending rows under a deterministic `qdrant_point_id`. **What was missing was
the link**: case rebuilding was a whole-table batch script, so the only way to
refresh one case was to rebuild all 32,999.

Result: a source change was "synced" while the vector store still answered from
stale content.

The old `sync_state` table — `(source_table PK, last_synced_source_id BIGINT,
last_synced_at)` — is also too narrow: ID-only, no timestamp, no tie-break, no
per-entity granularity, and no record of which schema or mapping a checkpoint
was written against. Phase 10 leaves it untouched and uses its own store.

## 4. Sync state

Durable, per `(source_system_id, source_entity)`, in a dedicated `erp_sync`
PostgreSQL schema — **not** the Phase 2 catalog. The catalog stores what a
source *looks like*; sync state stores how far through its *data* we have read.
Conflating them would let a schema republish move a data checkpoint.

```
source_system_id · source_entity · strategy · watermark (JSONB)
watermark_field · tie_break_field · last_record_key
schema_id · schema_hash · mapping_id · transformation_engine_version
last_run_id · status · version · updated_at · metadata
```

`status` ∈ `new · active · failed · blocked · paused`. Only the first three
permit data processing.

## 5–6. Watermark strategies and tie-breakers

| Strategy | Use | Risk alone |
|---|---|---|
| `TIMESTAMP` | an updated-at column | **loses rows sharing a timestamp** |
| `MONOTONIC_ID` | a truly monotonic key | blind to in-place updates |
| `COMPOSITE` | `(timestamp, key)` — the correct SQL default | — |
| `SOURCE_CURSOR` | a source-issued token | — |
| `CONTENT_HASH` | whole files and API specs | artifact-level only |

The composite predicate, generated verbatim:

```sql
WHERE (updated_at > :wm_ts OR (updated_at = :wm_ts AND id > :wm_tie))
ORDER BY updated_at, id
LIMIT :batch_size
```

The `ORDER BY` is not decoration — without it `LIMIT` selects an arbitrary
subset and the resulting watermark means nothing.

**Proved, in memory and against live PostgreSQL:** three rows share a
timestamp, a batch of 2 ends between them, and the next run returns exactly the
third. A timestamp-only watermark would ask for `> 10:00:00` and never see it.

**SQL safety:** values are always bound parameters; identifiers cannot be bound,
so they are validated against `^[A-Za-z_][A-Za-z0-9_]*$` and *refused* rather
than escaped.

## 7–9. Checkpoints, guarantee, idempotency

The watermark advances only to the last change that completed **every** stage,
and never past one that did not. Reading a row does not move it; a failed
vector write does not move it.

> **Guarantee: at-least-once delivery with idempotent downstream upserts.**
> Explicitly **not** exactly-once. Canonical storage, the embedding model and
> the vector store are independent systems with no shared transaction, and
> claiming atomicity across them would be false.

Replay is safe because every downstream write is keyed by a deterministic
identity: canonical `record_id`, `vector_id = uuid5(representation_id)`, and a
`SourceChange.idempotency_key` derived from system + entity + record +
watermark — never from a run UUID.

Failure policies: `BLOCK` (default — stop at the first failure), `QUARANTINE`
(collect everything, checkpoint still stops before the earliest failure),
`SKIP` (explicitly accepts losing the change).

## 10–13. Extraction, Phase 9, canonical upsert, affected representations

One `IncrementalExtractor` protocol — no `PostgresSyncEngine`/`MySQLSyncEngine`
split. Implementations: `RelationalIncrementalExtractor` (covers PostgreSQL,
MySQL and SQL Server via standard SQL), `ConnectorIncrementalExtractor` (through
the Phase 3 read-only seam), `InMemoryChangeSource`, `ContentHashChangeSource`.

Transformation is **Phase 9's `TransformationService`**, reused. A static test
asserts the sync package contains no `convert(`, `_to_decimal` or
`validate_record`.

`CanonicalRecordStore.upsert` is idempotent by contract. The affected-set
question goes to an `AffectedRepresentationResolver` — the coordinator never
assumes, so no BPI case semantics leak into it.

## 14–16. Content hash, embedding, vectors

The hash covers exactly the semantic content sent for embedding, built on the
frozen `schemas.identity.compute_content_hash`. Volatile keys (`run_id`,
`updated_at`, `embedding_status`, `watermark`, …) are stripped at any depth.

```
existing hash == rebuilt hash  →  DO NOT embed
existing hash != rebuilt hash  →  embed, then upsert the SAME vector id
```

The comparison needs a durable record of what was last embedded — the
`RepresentationHashLedger`. In this repository the BPI adapter maps it onto the
existing `ai_ready_cases.content_hash` column rather than introducing a second
source of truth.

The ledger is written **last**, so a failure at any earlier stage leaves the old
hash in place and the retry genuinely redoes the work.

## 17–19. Retry, recovery, concurrency

Measured: a vector-write failure leaves the checkpoint unmoved, the next run
retries the same change, and after both runs there are exactly 101 canonical
records and 101 vectors — no duplicates.

Concurrency is optimistic: every write asserts the `version` it read, enforced
by the database (`SELECT … FOR UPDATE` then a version compare). Two runs cannot
both advance one entity. No lock service, and nothing pretending to be atomic
across stores.

## 20. Metrics

```
changes_read · changes_processed · changes_failed · changes_skipped
canonical_upserts · canonical_deletes
representations_resolved · rebuilt · changed · unchanged
embeddings_generated · embeddings_skipped
vectors_upserted · vectors_deleted
watermark_before · watermark_after · checkpoint_advanced
duration_seconds · status
```

Invariants, asserted: `changes_read == processed + failed + skipped`, and
`embedding_candidates == generated + skipped`.

## 21–25. Schema drift

Rediscovery uses **Phases 4/5/6/7 unchanged** — a static test asserts the sync
package contains no `inspect(`, `get_columns`, `INFORMATION_SCHEMA` or
`list_collections`. Comparison uses **Phase 2's `compare_schemas`/`SchemaDiff`**
— a static test asserts there is no second diff engine.

Drift types: `ENTITY_ADDED/REMOVED`, `FIELD_ADDED/REMOVED`,
`FIELD_TYPE_CHANGED`, `FIELD_NULLABILITY_CHANGED`,
`FIELD_REQUIREDNESS_CHANGED`, `PRIMARY_KEY_CHANGED`,
`FIELD_ARRAYNESS_CHANGED`, `RELATIONSHIP_ADDED/REMOVED`.

Status: `NO_DRIFT · NON_BREAKING_DRIFT · REVIEW_REQUIRED · BLOCKED`.

**Classification consults the mapping**, not only database convention. This cuts
both ways: a removed column feeding a required canonical target **blocks**; a
removed column nobody maps **does not** — a false alarm teaches people to ignore
the gate.

Type compatibility is **Phase 8's `compare_types`**, not restated. And a
DECIMAL → STRING change is *not* waved through because Phase 9 could parse the
text: the source's declared contract changed, and that must be visible.

Measured, Proof C (`amount` DECIMAL→STRING, `tax_amount` added):

```
FIELD_TYPE_CHANGED  amount   decimal → string  →  MAPPING_REVIEW_REQUIRED
                                                  (source string vs canonical
                                                   decimal is now lossy)
FIELD_ADDED         tax_amount                 →  UNMAPPED_NEW_FIELD
status: REVIEW_REQUIRED
```

Measured, Proof D (`amount` removed):

```
FIELD_REMOVED  amount  →  MAPPING_INVALID  (its canonical target is required)
status: BLOCKED  →  no data is processed, sync state persisted as `blocked`
```

A block is only ever cleared deliberately, via `clear_block()`.

## 26. Live PostgreSQL verification

15 live tests against a real database, in an isolated `erp_sync_test` schema
created and dropped by the fixtures. **The real BPI source tables are never
touched.**

| Proof | Result |
|---|---|
| baseline 100 rows | 100 canonical, 100 vectors |
| checkpoint persisted in PostgreSQL | watermark `INV-100`, version > 0 |
| INSERT one row | read 1, transformed 1, upsert 1, rebuild 1, embed 1, vector 1 |
| — and not the other 100 | rebuild_calls == 1 |
| `NUMERIC(14,2)` → canonical | `Decimal("2500.50")` |
| UPDATE one row | 1 change, 100 canonical, 100 vectors (same identities) |
| metadata-only UPDATE | embeddings 0, vectors 0, skipped 1 |
| equal timestamps across a batch boundary | third row returned next run |
| `ADD COLUMN tax_amount` | `UNMAPPED_NEW_FIELD`, `NON_BREAKING_DRIFT` |
| `DROP COLUMN amount` | `BLOCKED` |
| concurrent checkpoint advance | `CheckpointConflictError` |
| state lives in `erp_sync` | not in `erp_catalog` |

## 27. Other source status

| Source | Status |
|---|---|
| PostgreSQL | ✅ **LIVE VERIFIED** — incremental and drift |
| MySQL | ⏸ **TESTED, NOT LIVE** — same `RelationalIncrementalExtractor`, same standard SQL; no isolated MySQL test database was provisioned, so no live claim is made |
| MongoDB | ⏸ **TESTED, NOT LIVE** — configured `updated_at` + `_id` tie-break fits the same contract; Change Streams are deliberately not required |
| SQL Server | ⏸ **DEFERRED** from Phase 4; the strategy fits the same public contract. **Not live verified.** |
| CSV / PDF / image | ✅ `ContentHashChangeSource` — artifact-level change detection |
| OpenAPI / Postman | ✅ spec hash → reparse → diff → impact. **No endpoint is ever called.** |

**Honest capability limits.** A source with no update marker cannot report
updates, and a source with no soft-delete flag cannot report deletions — a
hard-deleted row is simply absent from every query. Both are reported as limits
rather than guessed at; `classify_operation` only emits `DELETE` when an
explicit flag says so.

## 28. BPI cascade repair

Two modules, both a **separate top-level package** (`src/erp_integrations/`),
not under `erp_pipeline` — a frozen Phase 1 test enforces that nothing there
imports `bpi2020`.

| Module | Role |
|---|---|
| `bpi_case_cascade.py` | the `CaseDataAccess` contract, `CaseKeyIndex`, and an in-memory implementation used for the fast multi-case demonstration |
| `bpi_postgres_cascade.py` | the **production** adapter: real SQL over `cleaned_event_logs` / `ai_ready_cases`, real one-case rebuild, real embedding, real Qdrant |

```
BEFORE:  raw event → cleaned_event_logs → STOP
AFTER:   changed cleaned event
         → normalized_case_id            (indexed lookup)
         → that case's events only       (indexed range)
         → build_case_document(...)      (the BATCH builder, reused)
         → UPSERT ai_ready_cases         (the BATCH statement, reused)
         → content_hash vs PREVIOUS hash
         → embed only if it moved        (real model, real payload)
         → same frozen Qdrant point id
```

### The batch builder is reused, not reimplemented

`build_ai_ready_cases.build_case_document` is **already per-case** — the batch
script groups by `normalized_case_id` and calls it once per group. A one-case
rebuild therefore needs no new case-building logic at all, only the right rows
and the same function. Equivalence holds by construction, and is additionally
asserted against real rows: the batch path (whole table → groupby → build) and
the incremental path (one indexed query → build) produce identical
`case_record_id`, `content_hash`, `case_summary`, `total_events` and
`case_json`.

The upsert is the batch statement verbatim, including its rule that
`embedding_status` drops to `'pending'` only when `content_hash` genuinely
changed.

### The hash-ordering trap

The coordinator calls `builder.rebuild()` **before** `ledger.get_hash()`. Since
the rebuild writes `ai_ready_cases`, a naive ledger would read back the value
just written and every case would look unchanged — silently disabling
re-embedding entirely. So the rebuild captures the previous hash into
`PreviousHashRegistry` before it writes, mirroring the batch script, which also
loads existing hashes before upserting.

### Live proof (isolated `bpi_cascade_test` schema, real PostgreSQL)

The schema is built from the **real DDL** of both tables and dropped afterwards.
The production 270,211-event / 32,999-case baseline is never read or written.

```
available cases          : 60          available events : 240
case_record_id           : case:requestforpayment:livecase-0001
vector point id (before) : 2e8f6e5a-4f44-550e-8d2f-fe62dc707a29

CHANGED CONTENT
  source changes read    : 1        cases rebuilt        : 1
  affected cases         : 1        other cases rebuilt  : 0
  case upserts (SQL)     : 1        case-event queries   : 1
  event rows read        : 5        (not 240)
  hash changed           : True
  embeddings generated   : 1        vectors updated      : 1
  point id (after)       : 2e8f6e5a-4f44-550e-8d2f-fe62dc707a29  (stable)
  total cases still      : 60

UNCHANGED AI CONTENT (operational-only edit to record_data)
  case re-evaluated      : 1
  hash unchanged         : True
  embeddings generated   : 0        vector updates       : 0
```

The unchanged-content case is genuine, not contrived: the batch builder's
`content_hash` covers identity, the case summary and the metadata that reaches
the vector payload — deliberately **not** the raw per-event `attributes`. So
editing `record_data` is a real source change whose AI-ready content is
unchanged. An at-least-once **replay** of the same event is proved to behave
identically: `changes_read 1, embeddings_generated 0, embeddings_skipped 1`.

### Identity and vectors

Identity delegates to the prototype's own frozen `make_case_record_id`,
`compute_content_hash` and `make_qdrant_point_id`, so an incrementally rebuilt
case is byte-identical to a batch-rebuilt one. If they disagreed, every
incremental run would look like a content change and re-embed everything.

The vector adapter writes into the **existing** `bpi2020_erp_knowledge`
collection under the frozen point id; updating a case replaces its point rather
than adding one. Static tests assert the adapter contains no
`create_collection`, `recreate_collection` or `VectorParams`, and that the batch
embedding script is untouched.

### Embedding

`BpiEmbeddingUpdater` reuses `generate_and_store_embeddings.build_embedding_text`
and `make_qdrant_payload`, so a single-record embed sends the same text and
stores the same payload the batch embedder would. The model is loaded once, not
per record. Verified live against the real cached
`sentence-transformers/all-MiniLM-L6-v2`: 384-dimensional, deterministic across
repeat calls.

**Qdrant was not running during this verification.** The vector adapter is
therefore proved against a recording double that distinguishes "same point
replaced" from "second point added", and the live Qdrant test skips rather than
being faked.

One real defect was found and fixed here: `make_case_record_id` normalizes
(`CASE-0001` → `case:bpi2020:case-0001`), so recovering the case id by parsing
the identifier queried a case that does not exist — indistinguishable from
"this case was deleted", which would have dropped a live vector. A
`CaseKeyIndex` now carries the source's own key, and the production resolver
additionally takes `process_type` from the event row rather than a constant,
since it is part of the identity.

## 29. Privacy

Allowed in reports: source system, entity, record key, stage, issue code,
watermark, counts, schema field names. Never: source rows, emails, invoice
values, account details, OCR text, credentials.

`SourceChange.payload` holds the raw record **in memory** for Phase 9 and is
excluded from `to_dict()`. Sentinel tests assert `SECRET_*` values never reach a
run summary, a quarantine report, the persisted sync state, a serialized change
or a log — while confirming they *do* reach the canonical record, which is the
engine's job.

Nothing is logged at all during a sync.

## 30. Limitations

1. **MySQL, MongoDB and SQL Server are not live-verified.** The contract and
   adapters exist and are tested; no live claim is made.
2. **Hard deletes are undetectable** without a soft-delete flag or a source-side
   change feed.
3. **`cleaned_event_logs` has no `updated_at` column**, so the BPI cascade
   watermarks on its monotonic `id`. That detects new events for a case — the
   dominant real scenario for an append-mostly event log — but not an in-place
   edit of an existing row. An edit is still handled correctly once the case is
   re-resolved; it just is not self-detecting.
4. **A collection with no update marker is not incrementally syncable** — the
   framework says so rather than degrading to a silent full reload.
4. **No outbox.** Multi-stage propagation is made safe by the
   checkpoint-stops-before-failure rule plus idempotent writes, not by a durable
   per-stage work queue. A stage that fails *after* a successful vector write
   but *before* the ledger write causes one redundant re-embed on retry — safe,
   but not free.
5. **Concurrency is per-entity optimistic locking**, not distributed
   coordination.
6. **Output accumulation is unbounded** — every `ChangeResult` is retained for
   the summary. Batches are bounded, so this is bounded in practice.
7. **Qdrant was not reachable during verification.** The vector adapter is
   proved against a recording double and the live Qdrant test skips. The point
   identity, payload and collection all come from the existing frozen helpers,
   so the remaining risk is operational rather than logical.
8. **Drift rediscovery is caller-driven.** The service compares schemas it is
   given; it does not schedule or trigger rediscovery.

## 31. Phase 11 boundary

Phase 10 does **not** implement: a new mapping engine, a new transformation
engine, a new canonical contract, a complete embedding architecture, hybrid
hot/warm/cold tier routing, RAG, semantic search, a UI, an orchestration REST
API, ERP REST/SOAP execution, MCP runtime, or Kafka/Debezium CDC.

It reaches the vector layer through interfaces so Phase 11/12 can generalize
the embedding and hybrid-tier architecture without touching this engine — a
static test asserts the generic core imports no vector database and mentions no
`qdrant` vocabulary.
