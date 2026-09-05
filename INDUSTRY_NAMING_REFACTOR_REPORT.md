# INDUSTRY NAMING REFACTOR REPORT

**Project:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline
**Date:** 2026-08-31
**Scope:** NAMING-ONLY refactor of phase-based file and folder names into domain-based industry names.
**Method:** every name derived from the file's actual responsibility, read from its own docstring/header — never from the illustrative examples in the brief.

---

## 1. COMPLETE OLD → NEW RENAME TABLE

**41 files renamed. Zero folders required renaming** (no directory carried a phase number).
All renames performed with `git mv`, so git records each as a true rename (`R`) and file history is preserved.

### 1.1 Scripts (12)

| Old path | New path | Reason (from the file's own header) |
|---|---|---|
| `scripts/evaluate_phase3_multimodal.py` | `scripts/evaluate_multimodal_extraction.py` | "database BLOBs through to vectors… binary/base64 leakage and association collisions" — measures multimodal extraction |
| `scripts/evaluate_phase4_identity_retrieval.py` | `scripts/evaluate_identity_retrieval.py` | "can retrieval name the ERP record it found?" — identity-filtered retrieval precision |
| `scripts/evaluate_phase5_representation_resolution.py` | `scripts/evaluate_representation_resolution.py` | "can every search hit be turned back into its text?" |
| `scripts/evaluate_phase6_automatic_indexing.py` | `scripts/evaluate_automatic_document_indexing.py` | "does an upload index itself, correctly?" + time-to-searchable |
| `scripts/evaluate_phase7_schema_retrieval.py` | `scripts/evaluate_schema_retrieval.py` | "questions about its own structure" — Recall@1/@3/MRR over a schema corpus |
| `scripts/evaluate_phase8_remote_assets.py` | `scripts/evaluate_remote_asset_security.py` | "declared remote ERP assets, **safely**… every 'refused' result is a policy decision" — this is a security/policy evaluation, not a throughput one |
| `scripts/evaluate_phase9_sync_freshness.py` | `scripts/evaluate_sync_freshness.py` | "near-real-time synchronisation freshness" |
| `scripts/evaluate_phase10_security_sensitivity.py` | `scripts/evaluate_security_sensitivity.py` | "sensitivity handling and content protection" |
| `scripts/evaluate_phase11_group_integration.py` | `scripts/evaluate_integration_contract.py` | "the four-member integration, measured" — counts contract-boundary violations |
| `scripts/evaluate_phase12_final_component.py` | `scripts/evaluate_consolidated_component.py` | "a **CONSOLIDATION** run, not a replacement for the specialist evaluations" — named for what it is, avoiding the banned "final" |
| `scripts/run_phase12_benchmark.py` | `scripts/benchmark_tiered_storage.py` | "the hybrid tiered storage benchmark against live infrastructure" — verb-first, matches its role as a benchmark rather than an evaluation |
| `scripts/run_phase14_response_adaptation_evaluation.py` | `scripts/evaluate_response_adaptation.py` | response-adaptation evaluation; normalised to the `evaluate_*` convention |

### 1.2 Artifacts (13)

| Old path | New path | Reason |
|---|---|---|
| `artifacts/phase3_multimodal_evaluation.json` | `artifacts/multimodal_extraction_evaluation.json` | Output of the multimodal-extraction evaluation |
| `artifacts/phase4_identity_retrieval_evaluation.json` | `artifacts/identity_retrieval_evaluation.json` | Output of the identity-retrieval evaluation |
| `artifacts/phase5_representation_resolution_evaluation.json` | `artifacts/representation_resolution_evaluation.json` | Output of the representation-resolution evaluation |
| `artifacts/phase6_automatic_document_indexing_evaluation.json` | `artifacts/automatic_document_indexing_evaluation.json` | Output of the automatic-indexing evaluation |
| `artifacts/phase7_schema_retrieval_evaluation.json` | `artifacts/schema_retrieval_evaluation.json` | Output of the schema-retrieval evaluation |
| `artifacts/phase8_remote_asset_evaluation.json` | `artifacts/remote_asset_security_evaluation.json` | Output of the remote-asset **security** evaluation |
| `artifacts/phase9_sync_freshness_evaluation.json` | `artifacts/sync_freshness_evaluation.json` | Output of the sync-freshness evaluation |
| `artifacts/phase10_security_sensitivity_evaluation.json` | `artifacts/security_sensitivity_evaluation.json` | Output of the security/sensitivity evaluation |
| `artifacts/phase11_group_integration_evaluation.json` | `artifacts/integration_contract_evaluation.json` | Output of the integration-contract evaluation |
| `artifacts/phase12_final_component_evaluation.json` | `artifacts/consolidated_component_evaluation.json` | Output of the consolidated component evaluation |
| `artifacts/phase12_storage_benchmark.json` | `artifacts/tiered_storage_benchmark.json` | **Content-verified:** holds HOT/WARM/COLD latency, footprint and recall figures |
| `artifacts/phase13_openapi.json` | `artifacts/openapi_contract_snapshot.json` | **Content-verified:** a generated OpenAPI 3.1 document — 22 paths / 25 operations / `ApiKeyAuth` |
| `artifacts/phase14_response_adaptation_evaluation.json` | `artifacts/response_adaptation_evaluation.json` | Output of the response-adaptation evaluation |

### 1.3 Documentation (14)

| Old path | New path | Reason (from the document's own H1) |
|---|---|---|
| `docs/phase1_contract_correctness_stabilization.md` | `docs/api_contract_correctness.md` | "Contract and Correctness Stabilization" — the subject is API contract correctness |
| `docs/phase2_generic_erp_entity_support.md` | `docs/generic_erp_entity_support.md` | "Generic ERP Entity Support" |
| `docs/phase3_database_blob_multimodal_pipeline.md` | `docs/database_blob_multimodal_pipeline.md` | "Database BLOB → PDF / Image / OCR → Vector Pipeline" |
| `docs/phase4_identity_aware_retrieval.md` | `docs/identity_aware_retrieval.md` | "Identity-Aware Metadata and Exact Retrieval Filtering" |
| `docs/phase5_representation_content_resolution.md` | `docs/representation_content_resolution.md` | "Representation Persistence and Document Content Resolution" |
| `docs/phase6_automatic_document_indexing.md` | `docs/automatic_document_indexing.md` | "Automatic Uploaded Document Indexing" |
| `docs/phase7_schema_vector_retrieval.md` | `docs/schema_vector_retrieval.md` | "Schema Representation, Embedding and Semantic Retrieval" |
| `docs/phase8_remote_asset_ingestion.md` | `docs/remote_asset_ingestion.md` | "ERP Document URL / Remote Asset Processing" |
| `docs/phase9_near_real_time_sync_and_lifecycle.md` | `docs/near_real_time_sync_and_lifecycle.md` | "Near-Real-Time Synchronisation and Representation Lifecycle" |
| `docs/phase10_security_and_sensitivity.md` | `docs/security_and_sensitivity.md` | "Security and Sensitivity Hardening" |
| `docs/phase11_group_integration_contract.md` | `docs/group_integration_contract.md` | "Four-Member Group Integration Contract" |
| `docs/phase11_integration_readiness_report.md` | `docs/integration_readiness_report.md` | "Integration Readiness Report" |
| `docs/phase14_adaptive_response_transformation.md` | `docs/adaptive_response_transformation.md` | "ERP-Aware Adaptive Multimodal Response Transformation" |
| `docs/architecture/PHASE14_IMPLEMENTATION_REPORT.md` | `docs/architecture/RESPONSE_ADAPTATION_IMPLEMENTATION_REPORT.md` | Implementation report for response adaptation; SCREAMING_CASE retained to match its two sibling reports in `docs/architecture/` |

### 1.4 Tests (2)

| Old path | New path | Reason (from the test module's own docstring) |
|---|---|---|
| `tests/erp_pipeline/api/test_phase1_contract_correctness.py` | `tests/erp_pipeline/api/test_response_contract_correctness.py` | "Each test pins one defect that shipped a FALSE STATEMENT to a client… only became apparent in the **response body**". Named `test_response_contract_correctness` rather than `test_api_contract_correctness` because `test_api_contract.py` already exists in the same directory and the two would be confusable. |
| `tests/erp_pipeline/storage/test_phase10_update_compatibility.py` | `tests/erp_pipeline/storage/test_content_update_tier_compatibility.py` | "A content update must reach a record wherever it currently lives — including WARM and COLD" |

---

## 2. FILES ACTUALLY RENAMED

**41 / 41 proposed renames executed.** Verified by `git status`:

```
renames recorded by git (R):   41
```

| Category | Count |
|---|---|
| Scripts | 12 |
| Artifacts | 13 |
| Documentation | 14 |
| Tests | 2 |
| Folders | 0 (none carried a phase number) |
| **Source modules under `src/`** | **0 — `src/` contained no phase-named file** |

Because `src/` had no phase-named module, **no Python import statement anywhere required modification**. The renamed scripts are standalone entry points, and the renamed tests are collected by pytest rather than imported by name.

---

## 3. REFERENCES UPDATED

**247 path references across 44 files.**

| Area | Files | What changed |
|---|---|---|
| `scripts/` | 12 | Each script's `ARTIFACT = ROOT / "artifacts" / "…"` path constant, and its `python scripts/…` usage line |
| `tests/` | 3 | `test_document_and_live_http.py` (**writes** the OpenAPI snapshot), `test_runtime_hardening.py` (asserts artifacts are not gitignored), `test_cold_retrieval_benchmark.py` (reads the storage benchmark) |
| `docs/` | 22 | Inline path references and markdown links |
| Root `.md` | 5 | `README.md` + the four `IT22267290_*.md` audits |
| `frontend/src` | 1 | `types.ts` header comment citing the OpenAPI snapshot |

### Verification of the code changes

Excluding the one file already modified by the *previous* task:

```
added lines: 30   removed lines: 30    → pure 1:1 substitution
```

Every changed line in Python is one of exactly three shapes:
- `ARTIFACT = ROOT / "artifacts" / "<name>.json"` — a path constant
- `python scripts/<name>.py` — a usage line inside a docstring
- a prose path reference inside a docstring

**No statement, expression, assertion, condition or algorithm was altered.**

---

## 4. PHASE-BASED NAMES INTENTIONALLY RETAINED

| # | Retained | Location | Why |
|---|---|---|---|
| 1 | **`erp_phase12_bench_` Qdrant collection prefix** | `scripts/benchmark_tiered_storage.py:68` (`PREFIX`), `:114` (temp dir prefix) | **This is a runtime constant, not a filename.** It names Qdrant collections created and dropped at run time. Renaming it would change deployment/runtime behaviour and touch Qdrant naming — both explicitly out of scope (§8, §9). **Flagged for your review.** |
| 2 | **Document H1 titles** — e.g. `# Phase 10 — Security and Sensitivity Hardening` | all renamed `docs/*.md` | §6: "Do not rewrite document content." The filename is now domain-based; the title remains an accurate historical record of when the work was done. |
| 3 | **Historical prose** — "During Phase 14…", "Phase 9 validation outcome", "the Phase 12 benchmark was not re-run" | source comments, docstrings, docs | §8: the goal is removing structural naming, not erasing research history. No mass replacement of the word "Phase" was performed. |
| 4 | **`docs/history/AUDIT_REPORT_2026-08-18.md`** — 19 old-filename references left intact | `docs/history/` | This is an **archived, dated point-in-time audit** in an explicit `history/` folder. Rewriting it to cite filenames that did not exist on 2026-08-18 would falsify a historical record. Its references are inline code spans, not navigable markdown links, so nothing is "broken" in the navigation sense. **Flagged for your decision.** |
| 5 | **Class/function/constant names** | throughout `src/` | No public or internal identifier contains a phase number requiring a rename. None were changed. |

---

## 5. TEST RESULTS

### 5.1 Collection

| | Before | After |
|---|---|---|
| Tests collected | 3,890 | **3,890** |

Identical — no test was lost, duplicated or made uncollectable by the renames.

### 5.2 Focused run — the renamed tests plus every test referencing a renamed artifact

```
tests/erp_pipeline/api/test_response_contract_correctness.py
tests/erp_pipeline/storage/test_content_update_tier_compatibility.py
tests/erp_pipeline/api/test_document_and_live_http.py
tests/erp_pipeline/runtime/test_runtime_hardening.py
tests/erp_pipeline/storage/test_cold_retrieval_benchmark.py

→ 83 passed, 7 skipped
```

### 5.3 Full regression

```
3851 passed, 39 skipped, 0 failed, 0 errors — 8:05
```

**Exactly matches the pre-refactor baseline of 3851 / 39.**

### 5.4 An intermediate run that did *not* match — and why

The first post-refactor full run returned **3827 passed / 63 skipped**. That is 24 fewer passes than baseline, so it was investigated rather than accepted.

**Cause: the local `erp-mongodb` Docker container exited mid-run** (`Exited (255) 11 minutes ago`), which is unrelated to this refactor. Evidence:

1. `docker ps -a` showed the container had died during the run.
2. Restarting it and re-running the MongoDB-dependent suites gave **198 passed, 0 skipped**.
3. A `-rs` run afterwards showed **39 skips, all `localhost:6333` Qdrant** (the `erp-qdrant` container has been down 4 days) and **zero MongoDB skips**.
4. The arithmetic closes exactly: 39 (Qdrant, the standing baseline) + 24 (MongoDB, transient) = 63.
5. The definitive re-run with MongoDB restored returned **3851 / 39** — the baseline.

The 39 standing skips are environmental (local Qdrant container stopped) and were present in the baseline too.

### 5.5 Additional validation performed

| Check | Result |
|---|---|
| Files/folders still phase-named | **0** |
| Python compile check on all 12 renamed scripts | **12/12 OK**, each pointing at its correctly renamed artifact |
| Relative markdown links across all 100+ `.md` files | 48 checked, **1 broken — pre-existing** |
| Dangling references to old filenames | Only `docs/history/` (intentional, §4) and stale `.pyc` caches (regenerated automatically) |
| OpenAPI artifact regenerated by its own test | Written to `artifacts/openapi_contract_snapshot.json`; old filename did **not** reappear |
| API contract after regeneration | **22 paths / 25 operations / `ApiKeyAuth`** — identical to before |
| `src/` touched by this refactor | **No** — the only `src/` change is `hybrid_store.py` from the previous task |

**The one broken markdown link is pre-existing and out of scope:** `docs/incremental_sync_and_schema_drift.md` → `../src/bpi2020/sync/realtime_incremental_sync.py`. That target was removed in commit `c27ff04` (BPI prototype removal). Confirmed not caused here — the string `bpi2020` appears nowhere in the rename map, and `git diff` shows **0 lines changed** in that document. Left untouched per "do not perform any other cleanup".

---

## 6. CONFIRMATION THAT FUNCTIONAL CONTENT WAS NOT CHANGED

| Guarantee | Evidence |
|---|---|
| No business logic changed | Code diff is 30 removed / 30 added, all path constants or docstring paths |
| No algorithms changed | No statement, expression or control-flow line appears in the diff |
| No API behaviour changed | Regenerated OpenAPI: 22 paths, 25 operations, `ApiKeyAuth` — unchanged |
| No schemas changed | No file under `src/erp_pipeline/schemas/` or `api/schemas.py` modified |
| No tests/assertions changed | Only three path **constants** updated (`ARTIFACT = …`); zero assertion bodies touched |
| No configuration values changed | No `.env*`, `pyproject.toml`, `requirements.txt`, `Dockerfile` or `.dockerignore` modified |
| No deployment behaviour changed | `Dockerfile` untouched; `CMD`, `EXPOSE`, `HEALTHCHECK` unchanged; nothing deployed |
| No secrets touched | No `.env*` or `.azure-*` file read, opened or modified. **`.azure-oldkey` was never read.** |
| No functionality deleted | 41 renames, 0 deletions |
| No research evidence removed | All 14 artifacts present under new names; JSON contents byte-identical (git records renames, not rewrites) |
| Audit findings not mixed in | The lifecycle and security findings from `FULL_CODEBASE_STRUCTURE_AUDIT.md` were **not** acted on, as instructed |

---

## SUMMARY

```
FILES RENAMED:              41
  scripts                   12
  artifacts                 13
  docs                      14
  tests                      2
  folders                    0
  src/ modules               0  (none were phase-named)

REFERENCES UPDATED:        247  across 44 files
  scripts                    24
  tests                       5
  docs                      ~150
  root .md                  ~60
  frontend                    1

TESTS:            3851 passed, 39 skipped, 0 failed, 0 errors
                  (identical to the pre-refactor baseline)
COLLECTION:       3890 before → 3890 after

FUNCTIONAL LOGIC CHANGED:   NO
API CONTRACT CHANGED:       NO
DEPLOYMENT CHANGED:         NO
SECRETS TOUCHED:            NO
```

### Items flagged for your review (not actioned)

1. **`erp_phase12_bench_`** — Qdrant collection prefix in `scripts/benchmark_tiered_storage.py`. Retained because renaming it changes runtime behaviour and Qdrant collection naming, both out of scope. If you want it renamed (e.g. to `erp_tiered_bench_`), that is a behavioural change and should be a separate, deliberate task.
2. **`docs/history/AUDIT_REPORT_2026-08-18.md`** — 19 old-filename references retained to preserve an archived record. Say the word if you would rather they were updated.
