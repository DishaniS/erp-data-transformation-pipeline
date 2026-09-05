# Phase 9 — Near-Real-Time Synchronisation and Representation Lifecycle

**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline for Legacy ERP Systems
**Student:** IT22267290 · Member 4 · R26-SE-034
**Status:** implemented and verified
**Date:** 2026-08-25

---

## 1. Problem

Two gaps, one of them dangerous.

**Synchronisation was manual.** Incremental sync, watermarks and drift detection
all existed and all worked — but only when somebody posted a job.

**Replaced documents stayed searchable.** This is the dangerous one:

```
EMP002.birth_certificate = bytes A  ->  representation ai:document:…A
ERP replaces it with     = bytes B  ->  representation ai:document:…B
```

Different content means a different `document_id`, so a different
`representation_id`, so a different vector. Nothing overwrote anything and
**both remained searchable**, with nothing marking which was current. A query
for EMP002's certificate returned the superseded one beside the real one.

## 2. Previous manual architecture

`POST /v1/jobs {"job_type": "incremental_sync"}` → watermark load → extract →
transform → represent → persist → embed → route → watermark advance. Correct,
and entirely operator-driven.

## 3. Existing mechanisms reused

The audit found more already in place than expected, and Phase 9 reuses all of
it rather than building a second engine:

| already existed | Phase 9's use |
|---|---|
| `INCREMENTAL_SYNC` job and its whole pipeline | the scheduler submits it and does nothing else |
| watermark strategies (timestamp, monotonic id, composite, cursor, content hash) | untouched |
| `ChangeOperation.DELETE` and tombstoning | drives slot retirement |
| **safe watermark advancement** | already correct — see §8 |
| `HybridVectorStore._merge` state backstop | extended with one `is_current` check |
| `RepresentationStore.delete`, `storage.delete` | physical cleanup |
| idempotent `bootstrap` pattern | two new tables |

## 4. Scheduler architecture

A **trigger, and nothing else.**

```
tick()  ->  is scheduling enabled?          no  -> nothing
        ->  hold the lease?                 no  -> nothing (another instance leads)
        ->  for each CONFIGURED source:
              a sync already running?       yes -> skip
              interval elapsed?             no  -> skip
              in failure backoff?           yes -> skip
              submit INCREMENTAL_SYNC
              drift interval elapsed?       yes -> submit DRIFT_CHECK
```

It never queries a source, transforms a record, embeds anything or writes a
vector. A structural test asserts this: the module's AST contains no call to
`extract`, `transform`, `embed`, `upsert`, `store_vector` or `advanced_to`.

**Nothing starts on import.** No thread, no loop, no timer. The scheduler is
constructed explicitly and driven by `tick()`, and a test asserts the module
imports no `threading`, `asyncio`, `celery`, `kafka` or `pika`.

## 5. Configuration

```
ERP_SYNC_SCHEDULER_ENABLED      false      (default)
ERP_SYNC_SOURCES                ""         (default: none)
ERP_SYNC_INTERVAL_SECONDS       60
ERP_SYNC_DRIFT_INTERVAL_SECONDS 3600
ERP_SYNC_LEASE_SECONDS          120
```

Disabled by default, with no sources. A deployment that configures nothing polls
nothing, which is the safe reading of an absent configuration.

## 6. Source eligibility

**Opt-in per source.** Registering a source makes it usable; it is not consent
to poll it forever afterwards. A source absent from `ERP_SYNC_SOURCES` is never
scheduled, however many jobs have been run against it manually.

## 7. Multi-instance strategy

Two implementations, and a deployment picks the one matching its truth:

- **`SingleProcessLease`** always grants. Correct when the deployment genuinely
  runs one scheduler, and valuable because it makes that assumption **visible**
  rather than implied by the absence of any coordination.
- **`PostgresLease`** lets several instances run and one lead. Acquisition is a
  conditional `UPDATE` (win if unheld, already mine, or expired) plus an
  `INSERT … ON CONFLICT DO NOTHING`. The row's primary key serialises rivals, so
  simultaneous ticks produce exactly one leader.

A follower does nothing and says why. That is correct behaviour, not a failure.

## 8. Watermark semantics

**Already correct before Phase 9, and deliberately left alone.**
`IncrementalCoordinator` tracks a `safe_watermark` that advances only past
changes that actually succeeded — a failed record holds the watermark so the
next run revisits it. Phase 9 adds nothing here, because adding anything would
risk the property that matters: *a watermark must never advance past data that
was never indexed.*

Restarting resumes from persisted sync state; the scheduler holds no watermark
of its own and could not reset one if it tried.

## 9. Retry and failure semantics

A failed submission increments a per-source failure count and delays the next
attempt, doubling to a ceiling of 32× the interval and resetting on success. A
legacy database that is down is retried with decreasing frequency rather than
hammered once per interval forever. No retry platform was introduced.

A tick never raises: a scheduler that dies on one bad source stops
synchronising every other source too.

## 10. Drift scheduling

Separate, much longer interval (default 1 hour against 60 seconds). Schema
discovery is far more expensive than a watermark query and must not run every
few seconds. Omitting the drift interval disables drift scheduling entirely.

## 11. Logical representation identity

The central idea. What changed is not the document — it is what occupies a
**slot** in the ERP:

| kind | slot key | built from |
|---|---|---|
| structured record | `record:<canonical_record_id>` | Phase 1 identity |
| DB / remote attachment | `attachment:<parent_record_id>\|<source_field>` | Phase 3 metadata |
| declared upload | `attachment:<key_name>=<key_value>\|<role>` | Phase 6 identity |
| schema entity | `schema:<entity_id>` | Phase 7 identity |

Every part is metadata earlier phases already record. Nothing employee-specific
appears anywhere, and **no representation id changed** — they are load-bearing
in Qdrant, the representation store, search results, evaluations and tests, and
redefining them to carry lifecycle would churn all of that to express something
orthogonal.

**An anonymous uploaded document has no slot, deliberately.** Two unrelated PDFs
uploaded with no ERP association are not versions of each other, and guessing
that they are would make one silently disappear.

## 12. Current representation-set design

A slot holds a **set** — a contract is several chunks — so the registry is
`(logical_key, representation_id)` with `generation`, `is_current`,
`cleanup_pending`, `superseded_at/by` and `sync_run_id`.
`erp_runtime.representation_lifecycle`, identity and state only, **never text**:
the text lives once in `ai_representations` and copying it would create a second
thing to keep consistent.

## 13. Replacement ordering

```
build B -> persist B -> embed B -> route B -> promote B -> supersede A -> delete A
```

`LIFECYCLE_COMMIT` runs **last**, in all five pipeline tails. A failure at any
earlier stage means it never runs, so A stays current and searchable. The
inverse order — delete A, then fail to build B — leaves nothing searchable, and
is what the stage placement exists to prevent.

## 14. Failed new version

A remains current. Verified end to end: nothing about A is touched until B has
been persisted, embedded and stored.

## 15. Failed old delete — the interesting case

Superseding marks **state first**, deletes **after**. If the delete fails:

```
lifecycle result   {superseded: 1, stale_vectors_removed: 0, stale_cleanup_deferred: 1}
partial reason     "1 superseded vector(s) could not be removed and are
                    excluded from search pending reconciliation"

stale vector still physically present   True
stale vector returned by search         False   <-- the property that matters
recorded for reconciliation             True
```

`StorageRecordMetadata.is_current` is the backstop, checked in `_merge` on the
same terms filters already are. PostgreSQL is authoritative about what is
current, and physical cleanup is allowed to lag without ever making a stale
answer visible.

`is_current` is **not** a search filter. It is an internal correctness property,
deliberately not exposed for callers to set.

## 16. Reconciliation

`cleanup_pending` marks entries whose physical removal failed;
`pending_cleanup()` lists them for retry. No new service and no new JobType —
search is already correct, so this is a backlog rather than an outage.

## 17. Structured record update and delete

**Update needs no version set.** A structured representation id derives from the
canonical record id, so an update upserts in place — measured in the evaluation
as `promoted 0, superseded 0` for a department change. Phase 9 unified lifecycle
only where it was actually required, exactly as DR17 asks.

**Delete** retires the slot: every representation in it becomes non-current and
is queued for cleanup, attachments included.

## 18. DB BLOB replacement

The headline case. Measured: version A superseded, physically removed, and
absent from search; version B current. **0 stale hits.**

Shrink and grow both work: a 6-chunk document replaced by a 1-chunk one
supersedes all 5 excess chunks (measured `superseded 5, removed 5`), and the
reverse promotes every new chunk.

## 19. Remote asset lifecycle

Phase 8's remote documents are attachments, so they use the identical slot and
the identical replacement path. Same URL with changed bytes produces new content
identity and supersedes the old version; a changed URL serving identical bytes
produces the same representation and no churn.

**Freshness boundary, stated explicitly (DR23 option B):** *unchanged remote URL
content is refreshed only when the ERP row is reprocessed or an explicit
re-index is performed.* A watermark detects **row** changes; it cannot see a
remote server replacing bytes behind a URL that did not change. A periodic
remote refresh was considered and **not** implemented: it would re-fetch every
declared asset on a timer with no way to tell whether anything changed, and
signed URLs expire, so the cost and failure rate are real while the benefit is
speculative. This is a limitation, not a silence.

## 20. Uploaded document replacement

An upload carrying a declared business identity and document type occupies a
slot, so re-uploading replaces. An anonymous upload does not, and is never
guessed to be a replacement.

## 21. Schema lifecycle

Schema chunks are slot-managed by `entity_id`. An entity that shrinks from 23
field groups to 3 supersedes the other 20; one that grows promotes all of them.

**PostgreSQL catalog history is untouched.** Vector-current-state cleanup and
catalog history are different things: the catalog keeps every snapshot, the
index holds the current structure.

## 22. COLD behaviour

A superseded COLD item is marked non-current in authoritative state and is
therefore excluded from search whether or not the archive has been cleaned.
Nothing is decrypted to decide currency — the state row answers that — and
encryption is unweakened.

## 23. Representation content lifecycle

**Policy B: superseded text is retained** with lifecycle metadata and is never
returned as current. Chosen deliberately for a research component, where being
able to inspect what a superseded version said is worth more than the storage,
and where deleting text on supersession would make a mistaken supersession
unrecoverable. Retention is *not* claimed to be a managed archive: there is no
retention window and no expiry.

## 24. Historical direct resolution

**Policy A: `GET /v1/representations/{id}` still resolves a superseded id.**
Search correctness is mandatory and is enforced; direct resolution by an id a
caller already holds is a different question, and returning the text of an id
that exists is more useful than a 404 that hides it. The response carries no
"this is current" claim, so nothing is asserted that would be false.

## 25. Files changed

**New (5):** `orchestration/lifecycle.py`, `orchestration/scheduler.py`,
`scripts/evaluate_sync_freshness.py`, two test files, this report.

**Modified (7):** `orchestration/models.py` (stage + counters),
`orchestration/planner.py` (stage in all five tails),
`orchestration/stages.py` (`run_lifecycle_commit`),
`orchestration/service.py` (registry field), `orchestration/__init__.py`,
`storage/models.py` (`is_current`, `logical_key`), `storage/state.py` (DDL,
migration, upsert, read-back), `storage/migration.py`,
`storage/hybrid_store.py` (the backstop), `api/main.py`,
`runtime/bootstrap.py`.

## 26. Tests added

| file | tests |
|---|---|
| `tests/erp_pipeline/orchestration/test_sync_scheduler.py` | 29 |
| `tests/erp_pipeline/orchestration/test_representation_lifecycle.py` | 31 |

No test sleeps: the clock is injected. No test opens a socket.

**Two existing tests were extended, not relaxed** — the source-native and schema
tail assertions now list `LIFECYCLE_COMMIT`, because the point of those tests is
that the tails match each other, and they still do.

## 27. Freshness mini-evaluation

Eight timed source changes at a configured interval of 5 seconds.

```
change                                 promo  sup  del  stale       ms
  EMP002 initial                           2    0    0      0      1.1
  EMP003 initial                           2    0    0      0      0.8
  EMP002 department Finance -> Audit       0    0    0      0      0.8
  EMP002 certificate A -> B                1    1    1      0      1.5
  EMP002 certificate -> long               1    1    1      0      2.0
  EMP002 certificate long -> short         1    5    5      0      0.9
  EMP004 inserted                          2    0    0      0      0.8
  EMP004 re-synced unchanged               0    0    0      0      0.6

source changes permanently missed  0    cross-parent deletion errors  0
wrong current-version hits         0    unresolvable current hits     0
duplicate concurrent source syncs  0    idempotence violations        0
submissions while disabled         0

processing  median 0.9 ms   p95 1.5 ms   max 2.0 ms
GATES: PASS
```

Artifact: `artifacts/sync_freshness_evaluation.json`.

### A measurement I had to correct

The first run showed the "re-synced unchanged" case superseding a
representation, which would have meant idempotence was broken. It was the
fixture: PyMuPDF stamps a creation time into every PDF, so regenerating "the
same" certificate produced different **bytes**, a different `document_id` and a
genuine replacement. The case was measuring the fixture, not the pipeline.
Rendered PDFs are now cached, the case re-presents identical bytes, and the
result is `promoted 0, superseded 0` with an explicit `idempotence_violations`
gate. The implementation was not changed.

### On the latency numbers

These are **in-process pipeline processing times**, with an inline executor and
a deterministic test model. They are not a CDC latency benchmark, and the
end-to-end figure adds the configured interval.

## 28. Targeted results

`sync`, `orchestration`, `storage`, `api`, `runtime`, `verification`:
**940 passed, 29 skipped, 0 failed** (after extending the two tail assertions).

## 29. Full regression

| | collected | passed | failed | errors | skipped |
|---|---|---|---|---|---|
| baseline (after Phase 8) | 3500 | 3437 | 0 | 0 | 63 |
| after Phase 9 | **3562** | **3499** | **0** | **0** | **63** |

`3499 passed, 63 skipped, 30 warnings in 355.04s (0:05:55)`

The **+62** is fully accounted for:

- **+60** new tests (29 scheduler + 31 lifecycle)
- **+2** automatically parametrized: `test_bootstrap_creates_every_runtime_table`
  iterates `REQUIRED_TABLES["erp_runtime"]`, which gained
  `representation_lifecycle` and `scheduler_lease`.

**Skips are unchanged at 63.** No test was skipped to avoid a failure. No new
test sleeps — the scheduler clock is injected — so the suite duration is
unaffected by the scheduling work.

## 30. Existing artifact impact

All eight prior artifacts unchanged. Only
`sync_freshness_evaluation.json` was created. No previous evaluation
corpus was altered.

## 31. Known limitations

1. **Not CDC.** Polling with a watermark; freshness is bounded by interval plus
   processing time.
2. **Hard deletes depend on the connector.** A `updated_at` or monotonic-id
   watermark cannot observe a row that simply vanished. Where the change model
   reports deletes, slots retire; where it cannot, they do not. No universal
   delete detection is claimed, and no periodic full-key reconciliation scan was
   added.
3. **Unchanged remote URL content is not periodically refreshed** (§19).
4. **Superseded representation text is retained indefinitely** — no retention
   window (§23).
5. **`PostgresLease` is a lease, not consensus.** A partitioned instance whose
   lease expires mid-tick could overlap with a new leader; the running-job check
   is the second line of defence.
6. **Reconciliation is a listing plus a retry**, not a scheduled sweep.
7. **Latency figures are in-process** (§27).
8. Carried forward and still open, per the brief's out-of-scope list: Phase 6's
   unbounded `upload_results` cache, Phase 14's temporary-file asset extraction,
   and Phase 8's DNS TOCTOU boundary.

## 32. Explicit Phase 10+ exclusions

Confirmed absent: sensitivity inference, PII/document classification,
authorization rules, Phase 14 temp-file redesign, frontend scheduler or sync
UI, Member 1/2 final integration, LLM answer generation.

**No new endpoint. No new JobType. No new Qdrant collection. No new search
filter.**

## 33. Final synchronisation claim

> **Near-real-time synchronisation of indexed AI-ready ERP representations with
> source changes, through bounded scheduled incremental synchronisation.**
>
> With a configured interval of 5 seconds, the measured median processing time
> from job start to searchable-current was 0.9 ms (p95 1.5 ms, max 2.0 ms) in
> the evaluation environment. End-to-end freshness is bounded by the configured
> interval plus that processing time. These figures are in-process and are not
> generalisable to other deployments.

Explicitly **not** claimed: true real-time replication, or CDC-based
synchronisation.

The distinction that holds:

```
Member 2 live ERP API   authoritative current transactional state
Member 4 Qdrant index   synchronised AI retrieval corpus, freshness bounded by
                        interval + processing latency
```

---

*See also: [Phase 8 — Remote Asset Ingestion](remote_asset_ingestion.md),
[Phase 7 — Schema Vector Retrieval](schema_vector_retrieval.md).*
