# Architecture Consolidation Report

**Component:** IT22267290 — ERP-Aware Data Transformation Pipeline
**Project:** SLIIT 4th Year Research, `R26-SE-034`
**Date:** 2026-08-21
**Scope:** consolidate three source packages into one authoritative production package, without losing functionality and without changing behaviour that was not required to change.

---

## 1. Before Architecture

```text
src/
├── erp_pipeline/        154 .py   50,875 lines   generic framework
├── bpi2020/              14 .py    5,914 lines   dataset-specific prototype
└── erp_integrations/      3 .py    1,148 lines   adapters between the two
```

### Why this was a problem

**1. The working demonstration and the research contribution were different codebases.**
`src/bpi2020/` could run end-to-end — import, clean, build cases, OCR documents, embed, upload to Qdrant, search, verify integrity. `src/erp_pipeline/` held the actual research contributions — the explainable mapping engine, the tiering router, drift analysis — but had never processed the dataset the research was motivated by. An examiner asking *"show me it working"* and an examiner asking *"show me the contribution"* got two different answers, and neither was complete.

**2. Genuinely useful capability was trapped in dataset-specific code.**
Process/case modelling (`case_id`, `process_type`, `activity_sequence`, `duration_days`, ordered events) and cross-store integrity verification existed **only** in `bpi2020`, hardcoded to five BPI table names. The audit identified both as capability the generic framework lacked and downstream members needed — Member 3 cannot validate a workflow transition without a process state, and nothing was checking that PostgreSQL, the tier state and Qdrant agreed.

**3. Duplicate subsystems invited drift.**
Two configuration loaders, two identity schemes (`case:`/`event:`/`document:` versus `erp:`), two Qdrant connection paths, two embedding text builders, two vector payload conventions, two content-hash functions. Each pair was individually justified at the time it was written; collectively they doubled the surface a reader had to learn and created six opportunities for the two halves to disagree silently.

**4. `erp_integrations` existed only because of the split.**
Its own module docstring said so: *"The generic engine must not depend on the frozen prototype … This module is the sanctioned integration adapter."* An adapter between two halves of one component is a symptom, not a design.

**5. The repository contradicted its own thesis.**
The research claim is that one generic framework can absorb heterogeneous ERP sources. A permanent second implementation for the first dataset is evidence against that claim.

---

## 2. Migration Matrix

Every file formerly under `src/bpi2020/` and `src/erp_integrations/`.

| Old file | Classification | New location | Action | Reason |
|---|---|---|---|---|
| `bpi2020/common/config.py` | SUPERSEDED | `erp_pipeline/runtime/settings.py`, `erp_pipeline/catalog/config.py` | Removed | Both already implement the same canonical-name-with-deprecated-fallback pattern. Nothing unique remained. |
| `bpi2020/common/health.py` | SUPERSEDED | `erp_pipeline/api/routers.py:health_ready`, `erp_pipeline/runtime/database.py:check_connection` | Removed | The generic readiness endpoint probes PostgreSQL, Qdrant, the cold key and the embedding model, and reports which are merely *configured*. Strictly broader. |
| `bpi2020/common/stable_ids.py` | SUPERSEDED **+ 1 unique invariant** | `erp_pipeline/schemas/identity.py` | Invariant generalized, rest removed | `normalize_key_component`, the id builders, the UUIDv5 derivation and `compute_content_hash` all had generic equivalents. The **SERIAL-refusal invariant** did not — it became `looks_like_surrogate_key` / `require_business_key`. |
| `bpi2020/qdrant_connection.py` | SUPERSEDED | `erp_pipeline/runtime/settings.py:QdrantSettings`, `erp_pipeline/runtime/services.py:build_qdrant_client` | Removed | A second Qdrant connection system with its own env vars. The generic one already supports URL and host/port, redacts its key, and is used by all three tiers. |
| `bpi2020/storage/import_bpi_csv_to_old_db.py` | DATASET_SPECIFIC_DEMO | — | Removed | Seeded a *simulated* legacy database from CSVs so the prototype had something to read. The generic framework reads the CSVs directly through `FileIngestionService`, so the simulation step is unnecessary. |
| `bpi2020/storage/create_ai_native_db_schema.py` | SUPERSEDED | `erp_pipeline/runtime/bootstrap.py` | Removed | Created the prototype's own five tables. The generic bootstrap creates the five owned schemas the framework actually uses. |
| `bpi2020/transformation/clean_and_load_to_ai_db.py` | SUPERSEDED (config extracted) | `erp_pipeline/transformation/`, `examples/bpi2020/event_log_config.json` | Rules became configuration | Column detection, timestamp conversion and text cleaning are all generic capabilities. Which columns hold what is now data. |
| `bpi2020/transformation/build_ai_ready_cases.py` | **UNIQUE_AND_REUSABLE** | **`erp_pipeline/process/`** | **Generalized** | The single most valuable thing in the prototype. See §6. |
| `bpi2020/transformation/build_unified_bpi_knowledge_base.py` | SUPERSEDED | `erp_pipeline/ai/representation.py`, `erp_pipeline/process/service.py` | Removed | Combined cases and documents into one embeddable list. Both now project into `AIRepresentation` directly, which *is* the unified layer — with provenance the unified file did not carry. |
| `bpi2020/documents/parse_bpi_documents.py` | SUPERSEDED **+ 1 unique capability** | `erp_pipeline/ingestion/`, **`erp_pipeline/ingestion/document_classification.py`** | Classification generalized, rest removed | PDF/OCR/image extraction was already generic and better (page provenance, budgets, explicit OCR-unavailable state). `infer_document_type` had no generic equivalent. See §6. |
| `bpi2020/embeddings/generate_and_store_embeddings.py` | SUPERSEDED | `erp_pipeline/ai/service.py`, `ai/vector.py`, `erp_pipeline/storage/` | Removed | Generic embedding has batching, skip-if-unchanged, model fingerprinting, counter invariants and typed failure policy. The `text_for_ai`-in-payload convention was deliberately **not** carried over (§5). |
| `bpi2020/retrieval/search_erp_knowledge.py` | SUPERSEDED | `erp_pipeline/storage/hybrid_store.py:search`, `POST /v1/search` | Removed | A single-collection CLI search. The generic path searches HOT+WARM, de-duplicates across tiers by authoritative state, and offers opt-in cold rehydration. |
| `bpi2020/sync/realtime_incremental_sync.py` | SUPERSEDED | `erp_pipeline/sync/` | Removed | The generic coordinator has watermark strategies, drift detection, mapping-impact analysis, quarantine and safe checkpointing. The prototype poller had none of these and stopped at one table. |
| `bpi2020/verification/verify_cross_store_integrity.py` | **UNIQUE_AND_REUSABLE** | **`erp_pipeline/verification/`** | **Generalized** | See §7. |
| `bpi2020/embeddings/README.md` | OBSOLETE | — | Removed | Described the removed script. |
| `bpi2020/retrieval/README.md` | OBSOLETE | — | Removed | Described the removed script. |
| `erp_integrations/__init__.py` | TEMPORARY_INTEGRATION | — | Removed | Package existed only to hold the adapters. |
| `erp_integrations/bpi_case_cascade.py` | TEMPORARY_INTEGRATION | **`erp_pipeline/process/cascade.py`** | Generalized | `BpiAffectedCaseResolver` → `ProcessCaseResolver`; `BpiCaseRepresentationBuilder` → `ProcessCaseRepresentationBuilder`; `CaseKeyIndex` kept verbatim in concept; `InMemoryCaseAccess` → `InMemoryCaseEventSource`. |
| `erp_integrations/bpi_postgres_cascade.py` | TEMPORARY_INTEGRATION | `erp_pipeline/process/cascade.py` (protocols); PostgreSQL specifics removed | Partially generalized | The reusable half was the protocol implementation. The other half was SQL against the prototype's own `ai_ready_cases` / `cleaned_event_logs` tables, which no longer exist. `CaseEventSource` is the seam a deployment implements for its own schema. |

---

## 3. Final Architecture

```text
src/
└── erp_pipeline/                 154 → 160 .py files
    ├── schemas/          10 files   3,365 lines   frozen contracts + identity
    ├── catalog/           8 files   2,458 lines   versioned schema/mapping persistence
    ├── connectors/       11 files   1,617 lines   PostgreSQL · MySQL · SQL Server · MongoDB
    ├── discovery/         9 files   3,753 lines   relational catalog, Mongo inference, profiling
    ├── ingestion/        13 files   4,469 lines   CSV · PDF · image · OCR · classification  [+1]
    ├── api_specs/        11 files   5,283 lines   OpenAPI · Swagger · Postman (contracts only)
    ├── mapping/          12 files   4,339 lines   explainable source→canonical matching
    ├── transformation/   10 files   4,876 lines   rules, conversion, validation, quality
    ├── process/           7 files   1,724 lines   event logs → cases → process models   [NEW]
    ├── sync/             11 files   4,387 lines   watermarks, drift, impact, propagation
    ├── ai/               12 files   2,783 lines   representations, chunking, embeddings
    ├── storage/          16 files   5,762 lines   HOT/WARM/COLD routing, migration, cost
    ├── verification/      6 files   1,293 lines   cross-store integrity                  [NEW]
    ├── orchestration/    14 files   4,249 lines   job plans, stages, stores, secrets
    ├── api/               9 files   2,143 lines   FastAPI control plane (22 operations)
    ├── runtime/           7 files   1,739 lines   production composition and bootstrap
    ├── version.py
    └── __init__.py

examples/
└── bpi2020/
    └── event_log_config.json      the ONLY BPI-specific knowledge in the repository

scripts/
├── benchmark_tiered_storage.py       storage research benchmark (unchanged)
└── demos/
    └── run_bpi2020_demo.py        dataset demonstration, built entirely on erp_pipeline

data/
└── bpi2020/                       research dataset (gitignored, not redistributed)

docs/
├── architecture/                  this report
├── history/                       superseded audits
└── *.md                           per-phase design records
```

There is no `src/bpi2020/` and no `src/erp_integrations/`.

---

## 4. Functionality Preserved

Every capability that existed before still exists. Where it moved, it became **more** capable, not less.

| Capability | Was | Now | Notes |
|---|---|---|---|
| Case identity, deterministic | `case:{process_type}:{case_id}` | `erp:{source_system}:{process_type}:{case_id}` | Gained the source-system component, so two ERP systems that number cases identically no longer collide. |
| Case assembly from events | BPI-only, five hardcoded tables | any event log, via `EventLogConfig` | |
| Ordered activity sequence | ✅ | ✅ | Ordering made deterministic for equal timestamps (was non-deterministic). |
| Unique activities, first-occurrence order | ✅ | ✅ | |
| Case duration in days | ✅ | ✅ | Plus `duration_seconds` and an explicit `is_complete`. |
| Total events, start/end timestamps | ✅ | ✅ | |
| Case content hash, change detection | ✅ | ✅ | Now also excludes `allowed_next_states`, so one new case elsewhere cannot invalidate every existing hash. |
| Case summary text for embedding | ✅ | ✅ | Now bounded with a visible truncation marker and a configurable shape. |
| **Current process state** | ✗ | ✅ | `current_state` — the last observed activity. |
| **Allowed next states** | ✗ | ✅ | From an observed directly-follows model, ranked by frequency. |
| **Business entity references on a case** | ✗ | ✅ | `entity_references`, configured per deployment. |
| One-changed-event cascade | adapter over BPI tables | `erp_pipeline/process/cascade.py` | Same behaviour; now source-agnostic. |
| Cross-store integrity verification | 10 BPI-specific checks against live stores | 18 typed `IntegrityCode`s against protocols | Runs in CI without a database; see §7. |
| SERIAL-as-identity refusal | `resolve_record_id` regex on `case_\d+` | `require_business_key` + `check_record_identity` | Now enforced at construction time, framework-wide. |
| Document type inference | if-chain of BPI keywords | weighted configurable rules with reported evidence | See §6. |
| PDF/image/OCR extraction | ✅ | ✅ (generic, pre-existing) | Generic version already had page provenance and explicit OCR-unavailable state. |
| Embedding generation | ✅ | ✅ (generic, pre-existing) | Generic version adds batching, skip-if-unchanged, model fingerprint, counter invariants. |
| Semantic search | single-collection CLI | `POST /v1/search` across tiers | |
| Incremental sync | one-table poller | full `sync/` engine | |

---

## 5. Functionality Removed

Removed only where a generic equivalent existed and was at least as capable.

| Removed | Why safe |
|---|---|
| Second configuration loader | Duplicated the canonical/legacy fallback pattern. `catalog/config.py` even said so in its own docstring. |
| Second health-check module | Generic readiness is broader and is exposed over HTTP. |
| Second Qdrant connection system | Generic settings support URL and host/port, redact the key, and drive all three tiers. |
| Second identity scheme (`case:`/`event:`/`document:`) | One grammar now covers all of them, and it parses back unambiguously. |
| Second content-hash function | Byte-identical properties; the generic one is used everywhere. |
| Second embedding text builder | Different projections of different inputs; both now go through `AIRepresentation`. |
| Simulated-legacy-database seeding | The framework reads the CSVs directly; the simulation step existed only to give the prototype a database to read. |
| Prototype's own five PostgreSQL tables | The framework has its own five schemas; the prototype's tables have no consumer left. |
| Unified JSON/JSONL knowledge-base builder | `AIRepresentation` is the unified layer, and carries provenance the file did not. |
| **`text_for_ai` in the vector payload** | **A deliberate non-migration.** The prototype stored full text in Qdrant, doubling storage and making the index a second copy of the corpus. The generic payload carries `canonical_record_id` instead, and the audit's recommended fix is to return text through a resolvable record lookup rather than to duplicate it. Copying the prototype's convention would have entrenched a defect. |
| `pandas` dependency | No module under `src/`, `scripts/` or `tests/` imports it any more. |

---

## 6. Process/Case Generalization

### What was dataset-specific, and what replaced it

| Prototype | Generic |
|---|---|
| `cleaned_event_logs` table name | `CaseEventSource` protocol |
| `normalized_case_id` column | `EventLogConfig.case_id_field` |
| `normalized_activity` column | `EventLogConfig.activity_field` |
| `event_timestamp` column | `EventLogConfig.timestamp_field` |
| `process_type` column | `EventLogConfig.process_type_field` **or** a constant |
| `record_data` JSON blob | `EventLogConfig.attribute_fields` / `excluded_fields` |
| pandas `DataFrame` grouping | `group_events()` over `ProcessEvent` |
| `case:{process_type}:{case_id}` | `make_case_record_id()` → one canonical grammar |

### New package

```text
src/erp_pipeline/process/
├── models.py             EventLogConfig · CaseSummaryConfig · ProcessEvent
│                         ProcessCase · ProcessModel · make_case_record_id
├── event_normalizer.py   row → ProcessEvent; timestamp coercion; blank handling
├── case_builder.py       ordering · sequences · durations · summaries
│                         build_process_model · apply_process_model
├── cascade.py            one changed event → one rebuilt case
├── service.py            ProcessCaseService + projections to the framework contracts
├── errors.py
└── __init__.py
```

### The projection that makes it work

```text
raw event rows
  → ProcessEvent          normalized against an EventLogConfig
  → ProcessCase           ordered, timed, with current_state
  → ProcessModel          directly-follows → allowed_next_states
  → CanonicalRecord(record_type=CASE)
  → AIRepresentation      →  the EXISTING embedding, storage and search path
```

Once a case is an `AIRepresentation`, **no downstream code knows it is a case**. That is the whole point of the generalization: no case-specific branch exists anywhere in `ai/`, `storage/`, `orchestration/` or `api/`.

### Capabilities the audit asked for that now exist

The audit (§25.4, §40-B3) identified `current_state`, `allowed_next_states`, `entity_references` and `activity_sequence` as the fields Member 3's workflow engine would need and the framework did not have. All four now exist, and `allowed_next_states` is derived from an observed directly-follows model rather than declared.

### Design decisions worth recording

- **Event ordering is `(timestamp, ordinal)`, not timestamp alone.** Two events sharing a timestamp would otherwise order non-deterministically, producing a non-deterministic activity sequence and therefore a non-deterministic content hash — which would re-embed the entire log on every rebuild.
- **`ordinal` is excluded from the content hash.** It moves when the source reloads.
- **`allowed_next_states` is excluded from the content hash.** It is a property of the *process*, not of the case; including it would mean one new case invalidated every existing case's vector.
- **An unparseable timestamp yields `None`, not a dropped event.** Dropping the event would silently shorten the case.
- **A blank activity keeps the event.** It still counts toward the event total and the case timeline.
- **Mixing two cases in one `build_case` call raises.** Interleaving two process instances is silently wrong rather than loudly wrong, so it is made loud.

---

## 7. Cross-Store Verification Generalization

### From ten hardcoded checks to a typed contract

The prototype's script performed ten checks, each written against BPI's own tables, its unified JSONL file, and a live Qdrant. It could only run against a fully populated live environment.

```text
src/erp_pipeline/verification/
├── models.py            IntegrityCode (18) · IntegritySeverity · IntegrityIssue
│                        VerificationReport (verdict DERIVED from findings)
├── record_integrity.py  pure checks over the contracts - no store needed
├── cross_store.py       protocol-reached scans + InMemoryVectorIndex
├── service.py           IntegrityVerificationService
├── errors.py
└── __init__.py
```

### What it detects

| Category | Codes |
|---|---|
| Identity | `MALFORMED_RECORD_ID`, `SURROGATE_KEY_IDENTITY`, `DUPLICATE_RECORD_ID` |
| Presence | `CANONICAL_RECORD_MISSING`, `REPRESENTATION_MISSING`, `EMBEDDING_MISSING`, `VECTOR_MISSING` |
| Agreement | `CONTENT_HASH_MISMATCH`, `CANONICAL_REFERENCE_MISMATCH`, `MODEL_ID_MISMATCH`, `DIMENSION_MISMATCH`, `VECTOR_ID_MISMATCH`, `TIER_METADATA_MISMATCH`, `ENTITY_TYPE_MISMATCH` |
| Orphans | `ORPHANED_VECTOR`, `ORPHANED_TIER_STATE` |
| Embedding state | `EMBEDDING_NOT_GENERATED`, `EMBEDDING_STALE` |

### Why it is better than what it replaced

- **Runs without infrastructure.** Stores are reached through `CanonicalRecordSource`, `TierStateSource` and `VectorIndexSource` protocols, so 54 tests prove the whole rule set in about a second. The prototype's checks needed a populated PostgreSQL and a live Qdrant, so they were never part of CI.
- **Recomputes rather than trusts.** A stored `content_hash` is compared against a freshly computed one. Comparing a stored value against itself catches nothing.
- **Failures and warnings are distinguished.** A record embedded but not yet stored is normal mid-run; a vector the index does not have is not. The prototype treated everything as a failure.
- **The verdict is derived, never asserted.** `VerificationReport.passed` is computed from the findings, so a report cannot be declared green while carrying failures.
- **Findings are bounded.** Diagnostic text is capped at 300 characters, so a report can never become a second copy of the data it is checking.
- **A missing store reports zero checks, not a pass.** Reporting "passed" for a question that was never asked would be the most dangerous possible answer.

### Reused, not reinvented

No new identity scheme and no new metadata model. `check_vector_identity` calls the framework's own `vector_id_for`; `check_record_identity` calls `parse_canonical_id` and `looks_like_surrogate_key`; `check_metadata_agreement` compares `StorageRecordMetadata` against `EmbeddingRecord` field by field.

---

## 8. BPI Demonstration

BPI Challenge 2020 is now **dataset, configuration and demo script** — nothing else.

| Artefact | What it is |
|---|---|
| `data/bpi2020/` | The dataset. Gitignored, not redistributed. |
| `examples/bpi2020/event_log_config.json` | **The only BPI-specific knowledge in the repository.** Column names, process names, and this dataset's extra document keywords. Tracked. |
| `scripts/demos/run_bpi2020_demo.py` | Runs the dataset through `erp_pipeline`. Implements no ETL, no case building, no identity, no embedding, no storage and no retrieval of its own. |

### Verified working (2026-08-21)

```text
$ python scripts/demos/run_bpi2020_demo.py --limit 3000

source system : bpi2020_travel_expenses
  DomesticDeclarations.csv             661 cases      3000 events   13 activities
  InternationalDeclarations.csv        349 cases      3000 events   27 activities
  PermitLog.csv                        290 cases      3000 events   37 activities
  PrepaidTravelCost.csv                391 cases      3000 events   26 activities
  RequestForPayment.csv            not present

cases              : 1691
events             : 12000
representations    : 1691
identity issues    : 0

documents classified by the generic classifier:
  declaration_approval_policy.pdf          policy_document          conf=1.00
  finance_reimbursement_policy.pdf         policy_document          conf=1.00
  travel_claim_policy.pdf                  policy_document          conf=1.00
  approval_form_scan_001.png               approval_form            conf=1.00
  invoice_travel_claim_001.png             claim                    conf=0.50
  travel_receipt_001.png                   receipt                  conf=1.00
```

With `--store`, the same run also embeds locally, shows the tier-routing decision for every case, and — when a vector store is configured — stores, retrieves and verifies:

```text
embedding model    : sentence-transformers/all-MiniLM-L6-v2 (dim 384)
vectors generated  : 134
tier routing       : {'hot': 134} (policy decision only - nothing was written)
reason codes       : {'initial_placement': 134}
example decision   : hot - hot scored 0.325 driven by recency=1.00, criticality=0.25, latency=0.25
vector storage     : unavailable - ConfigurationError: the cold tier is enabled
                     but ERP_COLD_ARCHIVE_KEY is not set
```

That last line is the demo behaving correctly: it reports the environment's real configuration gap (a pre-existing issue the audit already recorded) instead of inventing infrastructure. **Tier routing is demonstrated offline**, because routing is pure computation over the record's own metadata — so the storage research is shown even with no vector database present.

### The claim this substantiates

> The framework can process the dataset that motivated the prototype, using only generic code, and a different event log requires a different configuration file rather than a code change.

---

## 9. Deleted Generated Files

| Path | Was it tracked? | Action |
|---|---|---|
| `frontend/dist/` | **No** — already ignored by `frontend/.gitignore:2` | Deleted from disk. Regenerated by `npm run build`; verified to rebuild cleanly. |
| `frontend/tsconfig.tsbuildinfo` | **No** — ignored by `.gitignore:312` | Deleted from disk. |
| `.pytest_cache/` | No | Deleted. |
| 35 × `__pycache__/` directories | No | Deleted. |
| `*.pyc` | No | Deleted. |
| `.agents/` | No — was completely empty | Removed. |
| `src/erp_pipeline/api/routers/`, `src/erp_pipeline/api/schemas/` | No — stray empty directories, pre-existing | Removed. |
| `var/pytest-readme-verify-20260818-0028/` | No | **Not removed** — the directory has restrictive ACLs and refuses deletion. Harmless and gitignored. Left in place; see §12. |

> **Correction to the prior audit.** `IT22267290_FULL_CODEBASE_RESEARCH_AUDIT.md` §3.2 claimed `frontend/dist/` and `frontend/tsconfig.tsbuildinfo` were *"tracked in Git"*. That was wrong — both were already ignored and were never tracked. Verified with `git ls-files frontend/` and `git check-ignore -v`. The audit has been corrected in place.

### Preserved (Phase 19)

`IT22267290_FULL_CODEBASE_RESEARCH_AUDIT.md`, `artifacts/tiered_storage_benchmark.json`, `artifacts/openapi_contract_snapshot.json`, `scripts/benchmark_tiered_storage.py`, all of `docs/`, all of `tests/`, and all of `data/bpi2020/`. **No research evidence was destroyed. The Phase 12 benchmark was not re-run**, so its measured figures are untouched.

---

## 10. Documentation Changes

| File | Change |
|---|---|
| `README.md` | "Two implementation tracks" → one implementation. Repository tree, modules table (added `process`, `verification`; removed `bpi2020`, `erp_integrations`), architecture diagram, boundaries list, identity section, features list, technology table (pandas removed), status table, environment-variable descriptions, known issues #6 and #9, important-files table, development guidance, and project summary. The BPI run instructions were replaced with the demo. |
| `src/erp_pipeline/__init__.py` | **Rewritten.** Previously claimed *"This package currently contains ONLY data contracts … no database connections, no SQL, no file parsing, no network calls"* — the audit's finding M8/stale-documentation item. Now describes the actual 16-package framework and its boundaries. |
| `AUDIT_REPORT.md` | Moved to `docs/history/AUDIT_REPORT_2026-08-18.md` with a header marking it superseded and listing its two now-resolved findings. Content preserved unaltered. |
| `IT22267290_FULL_CODEBASE_RESEARCH_AUDIT.md` | Two corrections applied in place (the `frontend/dist/` tracking claim, §9 above). Otherwise untouched. |
| `docs/canonical_erp_model.md`, `docs/incremental_sync_and_schema_drift.md` | Consolidation note added at the top; they reference module paths that no longer exist. Bodies preserved as development records. |
| `.gitignore` | Added `!examples/` exceptions so dataset **configuration** is tracked while dataset **data** stays ignored; documented `frontend/dist/` at the root level. The broad `*.json`/`*.csv` rules were **kept** — they exist to keep company datasets out — with narrow exceptions, which is the pattern the file already used. |
| `pyproject.toml` | `packages.find.include` reduced to `["erp_pipeline*"]`. |
| `requirements.txt` | `pandas` removed, with a comment explaining why. No version was changed. |
| `docs/architecture/ARCHITECTURE_CONSOLIDATION_REPORT.md` | This document. |

---

## 11. Testing

### Baseline (recorded before any change, this machine, this session)

```text
2574 passed, 1 failed, 26 skipped, 1 error   in 2171.26s (36:11)

FAILED tests/erp_pipeline/sync/test_bpi_embedding_vector_adapters.py::test_live_qdrant_point_identity_is_stable
ERROR  tests/erp_pipeline/storage/test_live_tiers.py::test_only_one_tier_holds_the_record_after_a_migration
```

Both non-passes were **live Qdrant Cloud timeouts** (`ResponseHandlingException: timed out`), not code defects. The audit's earlier clean run of 2,576 passed on the same commit, so these are environmental flakiness in the live-integration tests.

### After consolidation

```text
2703 passed, 26 skipped, 5 warnings   in 1803.98s (30:03)

0 failed, 0 errors
```

The two live-Qdrant non-passes are gone: one belonged to a retired BPI test file, and the other (`test_live_tiers.py`) passed on this run — confirming it was a transient timeout rather than a defect.

### Difference

```text
Before:  2574 passed,  1 failed,  26 skipped,  1 error   (2602 collected)
After:   2703 passed,  0 failed,  26 skipped,  0 errors  (2729 collected)

Collected: +127
```

Reconciled exactly:

```text
+209   new tests across 7 new files
-108   retired tests across 5 retired files + 3 in-place removals
  -1   test_cross_source_and_benchmark.py: 3 BPI-comparison tests removed,
       2 process-representation tests added
 +27   test_identity_and_serialization.py grew from 48 to 75 collected:
       the frozen corpus now parametrizes two tests over 18 values instead
       of one, and the single "shares Phase 0 properties" hash test was
       split into five explicit property tests
-----
+127   ✓ matches the observed change
```

**No test disappeared to make the build green.** The one failure and one error in the baseline were environmental (live Qdrant Cloud timeouts); neither was silenced, and the file containing the failing test was retired because its subject — the prototype's embedding uploader — no longer exists.

### Where the tests went

| Retired file | Tests | Replaced by | Tests |
|---|---|---|---|
| `tests/test_stable_identity.py` | 28 | `tests/erp_pipeline/test_vector_identity.py` | 24 |
| `tests/erp_pipeline/sync/test_bpi_cascade_repair.py` | 21 | `tests/erp_pipeline/process/test_case_cascade.py` | 19 |
| `tests/erp_pipeline/sync/test_bpi_embedding_vector_adapters.py` | 19 | same file + `test_case_building.py` | — |
| `tests/erp_pipeline/sync/test_live_bpi_cascade.py` | 22 | **not replaced** — see below | 0 |
| `tests/test_pipeline_integration.py` | 15 | **not replaced** — see below | 0 |
| 3 BPI-comparison tests in `test_cross_source_and_benchmark.py` | 3 | 2 process-representation tests in the same file | 2 |
| **Retired total** | **108** | | |

| New file | Tests |
|---|---|
| `tests/erp_pipeline/process/test_process_models_and_events.py` | 45 |
| `tests/erp_pipeline/process/test_case_building.py` | 31 |
| `tests/erp_pipeline/process/test_case_cascade.py` | 19 |
| `tests/erp_pipeline/verification/test_record_integrity.py` | 35 |
| `tests/erp_pipeline/verification/test_cross_store.py` | 19 |
| `tests/erp_pipeline/ingestion/test_document_classification.py` | 36 |
| `tests/erp_pipeline/test_vector_identity.py` | 24 |
| **New total** | **209** |

`tests/erp_pipeline/test_identity_and_phase0_compatibility.py` was renamed to `test_identity_and_serialization.py` and its five prototype-comparison tests were replaced with a **frozen-corpus** regression: `normalize_identifier`'s output is now pinned byte-for-byte against literal expectations. This is strictly stronger than the agreement test it replaced — two implementations compared against each other could have drifted together; a literal expectation cannot.

### Two test files were deliberately **not** replaced

Both tested the *prototype's own PostgreSQL tables* (`ai_ready_cases`, `ai_ready_documents`, `cleaned_event_logs`, `transformation_logs`) against a live database. Those tables are not produced by any code path any more, so the tests have no subject.

- `test_live_bpi_cascade.py` (22 tests) — live incremental propagation over BPI tables. The equivalent generic behaviour is covered by `tests/erp_pipeline/sync/test_live_postgresql_sync.py` and `tests/erp_pipeline/runtime/test_live_incremental_and_drift.py`, both of which remain.
- `test_pipeline_integration.py` (15 tests) — live Phase-0 linkage between the prototype's tables, its unified JSONL file, and its Qdrant collection.

**This is a genuine reduction in live-integration coverage of ~37 tests, and it is stated rather than hidden.** The behaviour they proved (deterministic identity, stable vector ids, hash-driven skip, cascade correctness) is now proved by the 209 new tests — but in-process, against protocols, rather than against a live database. That trade is a net gain in CI reproducibility and a net loss in end-to-end live assurance. Closing it properly means adding a live process-cascade test against a generic schema; see §12.

---

## 12. Remaining Risks

| # | Risk | Severity | Detail |
|---|---|---|---|
| R1 | **Live process-cascade coverage was lost, not replaced** | Medium | ~37 live-integration tests retired with the prototype's tables. The cascade is proved in-process but has never run against a real database in its generic form. |
| R2 | **The process cascade has no runtime entry point** | Medium | `erp_pipeline/process/cascade.py` implements the Phase 10 protocols and is tested, but no CLI or `JobType` composes it with a live incremental poller. It is a library, not a service. Recorded as README known issue #6. |
| R3 | **`ProcessCaseService` is not wired into orchestration** | Medium | There is no `JobType.PROCESS_PIPELINE`. Cases are reachable from the demo and from library code, not from `POST /v1/jobs`. Deliberately out of scope: adding a job type is a feature, not a consolidation. |
| R4 | **`StorageRecordMetadata` still lacks `canonical_record_id`** | Medium | `verify_tier_state` can detect an orphaned tier-state entry only when the canonical id is recorded, which the current contract does not do. Adding the column is a schema change and was correctly out of scope; this is the audit's Issue 2 and remains open. |
| R5 | **The demo's `--store` path is unverified end-to-end here** | Low | It could not run because `ERP_COLD_ARCHIVE_KEY` is unset in this environment — a pre-existing configuration gap, unchanged by this work. The code path is exercised by the storage test suite; the demo's own use of it is not. |
| R6 | **`var/pytest-readme-verify-20260818-0028` could not be deleted** | Low | Restrictive ACLs. Gitignored and harmless; needs an elevated `Remove-Item` or a permissions change. |
| R7 | **`src/erp_data_transformation_pipeline.egg-info/` is stale** | Low | It still lists `erp_integrations` as a package. Left alone deliberately: refreshing it means re-running `pip install -e .`, and the brief forbids installing during this task. It has no runtime effect — the removed package is gone from disk either way. Refresh it with `pip install -e .` at your convenience. |
| R8 | **Document classification is unmeasured** | Low | Configurable, evidence-reporting and tested for behaviour, but its accuracy on a labelled corpus is not measured. |
| R9 | **`ProcessModel` is descriptive, not a discovery algorithm** | Low | It reports observed directly-follows relations. It makes no claim to be process mining in the α-algorithm / inductive-miner sense, and the docstring says so. |

---

## 13. Remaining BPI References

Exhaustive search of the repository, excluding `.git/`, `.venv/` and `node_modules/`.

### Production code — none

```bash
grep -rn "^\s*\(from\|import\)\s\+\(bpi2020\|erp_integrations\)" --include=*.py src/ tests/ scripts/
# (no matches)
```

```text
>>> import bpi2020            → ModuleNotFoundError
>>> import erp_integrations   → ModuleNotFoundError
>>> import erp_pipeline       → OK
```

### Every remaining textual reference, and why it is legitimate

| Location | Reference | Legitimate because |
|---|---|---|
| `data/bpi2020/**` | dataset files | It is the research dataset. Gitignored except three sample PDFs and three PNGs. |
| `examples/bpi2020/event_log_config.json` | dataset configuration | The one intended home for dataset knowledge. |
| `scripts/demos/run_bpi2020_demo.py` | demo script | Named after the dataset it demonstrates. |
| `src/erp_pipeline/__init__.py` (2) | points at the demo and `examples/` | Tells a reader where the dataset lives and states that the package does not depend on it. |
| `README.md` (~20) | architecture note, tree, demo section, dataset instructions | Explains the consolidation and how to run the demonstration. |
| `docs/history/AUDIT_REPORT_2026-08-18.md` | many | Historical document, explicitly marked superseded. |
| `IT22267290_FULL_CODEBASE_RESEARCH_AUDIT.md` | many | The audit that motivated this work, describing the pre-consolidation state. |
| `docs/*.md` | several | Per-phase development records; the two that reference removed paths carry a consolidation note. |
| `docs/architecture/ARCHITECTURE_CONSOLIDATION_REPORT.md` | many | This document. |
| Test files (~15) | `"bpi2020" not in source` assertions | **Deliberately kept as regression guards.** They now assert the absence of a package that no longer exists, which is exactly what should stay true. |
| Test docstrings | "migrated from …" | Records where migrated coverage came from. |
| `.env.example`, `.gitignore` | `BPI_OLD_DB_*` legacy names, `data/` rules | Deprecated environment fallbacks still honoured, and dataset ignore rules. |

**There is no unexplained production dependency on BPI Challenge 2020.**

---

## 14. Verification Performed

| Check | Result |
|---|---|
| `import erp_pipeline` | ✅ |
| `import bpi2020` / `import erp_integrations` | ✅ both `ModuleNotFoundError` |
| No production imports of removed packages | ✅ zero matches |
| FastAPI app constructs and generates OpenAPI | ✅ 22 operations, unchanged |
| `RuntimeSettings.from_environment()` loads | ✅ 1 problem, identical to baseline (pre-existing cold-key gap) |
| Boundary/architecture tests | ✅ 334 passed |
| New process tests | ✅ 95 passed |
| New verification tests | ✅ 54 passed |
| New classification tests | ✅ 36 passed |
| Migrated identity tests | ✅ 99 passed |
| Frontend `npm test` | ✅ 26 passed |
| Frontend `npm run build` | ✅ succeeded, 35 modules |
| BPI demo end-to-end | ✅ 1,691 cases, 12,000 events, 0 identity issues, 6 documents classified |
| Phase 12 benchmark artifact | **not re-run** — measured evidence untouched |
| Git state | no commits made; no history rewritten |

---

## 15. What Was Deliberately Not Done

Per the brief's Phase 31 and the "zero unnecessary behavioural changes" rule:

- No `response_adaptation/` package, no multimodal response transformation, no Phase 14.
- No changes to API behaviour, routes, request/response shapes, or the generated OpenAPI contract.
- No changes to any database schema or DDL.
- No changes to vector payload contracts.
- No changes to deterministic ID derivation for any pre-existing record type.
- No re-run of the Phase 12 benchmark.
- No dependency version changes (one genuinely unused dependency removed).
- No commits — the working tree is left staged-and-modified for review.
- No fixes to audit issues that were not required for consolidation: search-hit resolution, `SearchRequest.filters`, the schema endpoint's empty field types, the `erp_runtime` bootstrap gap, and sensitivity inference all remain open, exactly as the audit recorded them.
