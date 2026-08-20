# Forensic Audit — ERP-Aware Data Transformation Pipeline

**Repository:** `C:\research\erp-data-transformation-pipeline`
**Branch:** `main` @ `d1d5f33`
**Audit date:** 2026-08-18
**Method:** read-only inspection of the working tree and git history. No file was modified. Every claim below carries a `file:line` or a shown command. Where something could not be verified it is marked `UNVERIFIED`.

---

## 0. Executive summary — read this first

Three findings dominate everything else in this report.

**1. This repository contains two different codebases, and the audit brief describes only the smaller one.**

| Tree | Files | Lines | Character |
|---|---|---|---|
| `src/erp_pipeline/` | 154 `.py` | 50,774 | Generic, configuration-driven ERP framework: connectors, schema discovery, mapping engine, transformation, sync, tiered vector storage, job orchestration, REST API |
| `src/bpi2020/` | 14 `.py` | 5,914 | Dataset-specific BPI Challenge 2020 batch prototype |
| `src/erp_integrations/` | 3 `.py` | 1,148 | Adapters between the two |
| `tests/` | 121 `.py` | 41,174 | 2,593 collected tests |

The brief's governing question — "is this config-driven or hardcoded?" — has **two different answers**. `src/bpi2020` is heavily hardcoded to one dataset. `src/erp_pipeline` is genuinely configuration-driven, with a registered-source model, automatic schema discovery, an explainable mapping engine, and a 20-endpoint REST control plane. Judging the repository only by `src/bpi2020` would materially misstate its completeness.

**2. 96.9% of the Python source is not in version control.**

```bash
git ls-files '*.py' | wc -l     # 9
git status --porcelain --untracked-files=all | grep '^??' | grep -c '\.py$'   # 284
```

Git tracks 21 files total, of which 9 are `.py` (4,488 lines). The entire `erp_pipeline` framework, all 121 test files, all 13 design documents, `pyproject.toml`, the frontend, and the benchmark artifacts are **untracked**. All 9 tracked Python files also carry uncommitted modifications. **No file in this repository currently matches its committed state.** The last commit, `d1d5f33`, predates essentially all the engineering described in the README.

This is the single most severe finding in the audit. It is a governance failure, not a code failure, and it makes almost every Part 3 requirement moot: there is nothing to review, branch, or protect because the work was never committed.

**3. The canonical *case* model the brief asks about does not exist in the generic framework.**

The brief asks whether `case_id`, `process_type`, `current_state`, `entities`, `timeline`, `allowed_next_states`, `documents`, `freshness`, and `source_version` are produced. The generic canonical model (`src/erp_pipeline/schemas/canonical_models.py`) is a *record/document* model, not a *case/process-state* model. Its default vocabulary is `invoice` / `customer` / `purchase_order` (`src/erp_pipeline/mapping/canonical_model.py:361-497`). The case-shaped output exists only in the BPI prototype, and even there `current_state`, `allowed_next_states`, `entities`, `freshness`, and `source_version` are **absent**. Downstream governance and orchestration components that depend on those fields are blocked.

---

# PART 1 — REPOSITORY INVENTORY

## 1.1 Directory Structure

Command: `find . -maxdepth 4 -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/__pycache__/*' -not -path '*/.venv/*' -not -path '*/dist/*' | sort`

```text
erp-data-transformation-pipeline/
├── .agents/                       # no tracked content
├── .claude/settings.local.json
├── artifacts/                     # 2 generated evidence files (untracked)
├── data/
│   ├── bpi2020/{raw,cleaned,ai_ready,ai_ready_documents,unified,documents,images}
│   └── raw/                       # empty
├── docs/                          # 13 phase design documents (untracked)
├── frontend/
│   ├── src/{api,pages,test}
│   └── dist/, node_modules/
├── scripts/                       # 1 benchmark runner
├── src/
│   ├── bpi2020/{common,documents,embeddings,retrieval,storage,sync,transformation,verification}
│   ├── erp_integrations/
│   ├── erp_pipeline/{ai,api,api_specs,catalog,connectors,discovery,ingestion,mapping,
│   │                 orchestration,runtime,schemas,storage,sync,transformation}
│   └── erp_data_transformation_pipeline.egg-info/
├── tests/
│   ├── erp_pipeline/{ai,api,api_specs,catalog,connectors,discovery,ingestion,mapping,
│   │                 orchestration,runtime,storage,sync,transformation}
│   └── fixtures/{api_specs,ingestion,transformation}
└── var/                           # runtime scratch, gitignored
```

One line per top-level directory, stating what it actually contains:

| Directory | Actual contents |
|---|---|
| `artifacts/` | `phase12_storage_benchmark.json` (measured storage/latency/recall evidence), `phase13_openapi.json` (generated 20-path REST contract). Untracked, despite README claiming they are tracked. |
| `data/` | ~2.8 GB of BPI 2020 raw CSVs and generated JSON/JSONL. Entirely gitignored (`.gitignore:300`). Only 3 PDFs and 3 PNGs are tracked. |
| `docs/` | 13 markdown design documents, one per pipeline phase. Untracked. |
| `frontend/` | React 18 + Vite + TypeScript upload client. One screen (`pages/Upload.tsx`). Untracked; `package.json` is additionally gitignored. |
| `scripts/` | One file, `run_phase12_benchmark.py` (719 lines), producing the storage benchmark artifact. |
| `src/bpi2020/` | Dataset-specific batch prototype: CSV import, cleaning, case building, document parsing, embedding, retrieval, incremental poller, integrity verifier. |
| `src/erp_integrations/` | BPI-to-generic cascade adapters. Defined but **not composed by any runtime entry point** (see 2.9). |
| `src/erp_pipeline/` | The generic framework: 14 sub-packages covering connectors, discovery, ingestion, mapping, transformation, sync, AI/embedding, tiered storage, catalog, orchestration, runtime composition, and the REST API. |
| `tests/` | 91 `test_*.py` files plus fixtures and conftests; 2,593 collected tests. |
| `var/` | Runtime scratch (uploads, cold archive, pytest temp). Gitignored. |

## 1.2 File Census

Command: `find . -type f ... | sed -E 's/.*\.([A-Za-z0-9]+)$/\1/' | sort | uniq -c | sort -rn`

| Extension | Count | Total lines | Observed purpose |
|---|---|---|---|
| `.py` | 293 | 99,729 | All application, framework, and test code |
| `.json` | 32 | 70,348,573 | Generated BPI datasets (~2.8 GB), test fixtures, 2 artifacts |
| `.csv` | 20 | 270,263 | BPI raw event logs + ingestion test fixtures |
| `.md` | 17 | 6,658 | README (45 KB) + 13 phase docs + 3 package READMEs |
| `.yaml` | 11 | 522 | OpenAPI test fixtures only |
| `.jsonl` | 8 | — | Generated BPI line-delimited outputs |
| `.ts` | 7 | 706 | Frontend API client, types, tests |
| `.tsx` | 3 | 245 | Frontend React components |
| `.txt` | 5 | 191 | `requirements.txt` + egg-info metadata |
| `.pdf` / `.png` | 3 / 3 | — | BPI policy documents and scanned images (tracked) |
| `.toml` | 1 | 29 | `pyproject.toml` |

**Python line distribution:**

| Tree | Files | Lines |
|---|---|---|
| `src/erp_pipeline` | 154 | 50,774 |
| `tests` | 121 | 41,174 |
| `src/bpi2020` | 14 | 5,914 |
| `src/erp_integrations` | 3 | 1,148 |
| `scripts` | 1 | 719 |

**Specifically requested counts:**

| Type | Count |
|---|---|
| `.py` files | 293 |
| `.ipynb` notebooks | **0** |
| `.sql` files | **0** — all DDL is inline Python string literals |
| `.json`/`.yaml`/`.toml` **configuration** files | **0** — no YAML/TOML/JSON configuration file exists anywhere. The 32 `.json` and 11 `.yaml` files are all data, test fixtures, or generated artifacts. `pyproject.toml` is packaging metadata, not runtime configuration. **All runtime configuration is environment variables.** |
| Raw data files | 20 CSV + 8 JSONL + 3 PDF + 3 PNG |

**Primary language and frameworks, with evidence:**

- **Python 3.11+** — `pyproject.toml:8` (`requires-python = ">=3.11"`). 99,729 lines across 293 files.
- **FastAPI 0.141.1 + Uvicorn 0.52.3** — `requirements.txt:18-19`; imported at `src/erp_pipeline/api/main.py:21-24`.
- **SQLAlchemy 2.x** — `requirements.txt:2`; `sqlalchemy.engine.URL` at `src/erp_pipeline/runtime/settings.py:103-112`.
- **pandas** — `requirements.txt:1`; the BPI prototype's transformation engine (`src/bpi2020/transformation/clean_and_load_to_ai_db.py:37`).
- **sentence-transformers** — `requirements.txt:8`; loaded at `src/erp_pipeline/ai/embedding.py:136`.
- **qdrant-client** — `requirements.txt:9`; `src/bpi2020/embeddings/generate_and_store_embeddings.py:25`.
- **Database drivers**: `psycopg2-binary`, `PyMySQL`, `pyodbc`, `pymongo` — `requirements.txt:3,11,12,13`.
- **React 18.3 + Vite 5.4 + TypeScript 5.6 + Vitest 2.1** — `frontend/package.json`.
- **pytest** — `requirements.txt:10`; `pyproject.toml:26-27` sets `testpaths = ["tests"]`.

## 1.3 Entry Points

| Entry point | File:line | What it starts | Order | Can it run? |
|---|---|---|---|---|
| `erp-api` console script | `pyproject.toml:18` → `runtime/application.py:106` | Full production API: validate config → bootstrap schemas → build services → uvicorn | Self-contained | **Yes**, if `PIPELINE_DB_PASSWORD` is set and PostgreSQL is reachable. `require_valid()` (`settings.py:321`) refuses to start otherwise and prints every problem at once. |
| `erp-bootstrap` console script | `pyproject.toml:19` → `runtime/bootstrap.py:main` | Creates the five owned PostgreSQL schemas | Before first API start | **Partially** — README known issue #2 (`README.md:676`): creates `canonical_records` but does not call `bootstrap_runtime_persistence`, so `registered_sources`, `uploads`, `mapping_drafts` are created only by API startup. |
| `python -m erp_pipeline.api` | `api/__main__.py:13-14` | Delegates to `runtime.application:run` | Same as `erp-api` | Yes |
| `uvicorn erp_pipeline.runtime.application:app` | `application.py:103` (`_LazyApp`) | ASGI target; builds app on first request | Same | Yes |
| `bpi2020/storage/import_bpi_csv_to_old_db.py` | `__main__` | 5 CSVs → `*_raw` tables | **Step 1** | Requires CSVs under `data/bpi2020/raw/` (gitignored — must be obtained separately) |
| `bpi2020/storage/create_ai_native_db_schema.py` | `__main__` | Creates BPI tables | **Step 2** | Yes, given DB + credentials |
| `bpi2020/transformation/clean_and_load_to_ai_db.py` | `main()` | Raw → `cleaned_event_logs` | **Step 3** | Yes |
| `bpi2020/transformation/build_ai_ready_cases.py` | `main()` @ 599 | `cleaned_event_logs` → `ai_ready_cases` | **Step 4** | Yes; refuses to proceed if step 3 produced nothing (line 631) |
| `bpi2020/documents/parse_bpi_documents.py` | `__main__` | PDFs/images → `ai_ready_documents` | **Step 4b** | Requires Tesseract for images |
| `bpi2020/transformation/build_unified_bpi_knowledge_base.py` | `__main__` | Cases + docs → unified JSON/JSONL | **Step 5** | Yes |
| `bpi2020/embeddings/generate_and_store_embeddings.py` | `__main__` | Unified → Qdrant | **Step 6** | Requires Qdrant + MiniLM cache |
| `bpi2020/retrieval/search_erp_knowledge.py` | `__main__` | CLI semantic search | After 6 | Requires Qdrant |
| `bpi2020/sync/realtime_incremental_sync.py` | `__main__` | Polls raw tables → `cleaned_event_logs` | After 3; runs continuously | Yes, but stops at cleaned events (2.5) |
| `bpi2020/verification/verify_cross_store_integrity.py` | `__main__` | Cross-store PASS/FAIL audit | After 6 | Yes |
| `scripts/run_phase12_benchmark.py` | `__main__` | Produces the storage benchmark artifact | Independent | Requires Qdrant + model |
| `npm run dev` / `build` / `test` | `frontend/package.json` | Vite dev server / build / vitest | Independent | **No, from a clean clone** — `frontend/package.json` is gitignored (3.6) |
| Docker `CMD`/`ENTRYPOINT` | — | **MISSING** — no Dockerfile or compose file anywhere |
| `Makefile` targets | — | **MISSING** — no Makefile |
| Notebooks | — | **MISSING** — zero `.ipynb` files |

### Critical question: is there a single orchestrating entry point?

**Both answers apply, to the two different codebases.**

**Generic framework — YES.** `src/erp_pipeline/runtime/application.py:21` (`create_production_app`) is a real composition root: it validates configuration (`:37`), bootstraps schemas (`:41-54`), assembles services (`:56`), and wires a durable job orchestrator with a PostgreSQL job store (`:58-63`). A pipeline run is requested over HTTP (`POST /v1/jobs`, `api/routers_data.py:463`) and executed stage-by-stage by `PipelineRunner.run` (`orchestration/pipeline.py:117`) against a stage graph chosen by the planner (`orchestration/planner.py:52-86`). **A human does not order the stages; the planner does.**

**BPI prototype — NO.** It is nine scripts a human must run in sequence. There is no orchestrator, no Makefile, no shell wrapper.

The required order, **inferred from the code** (each script's source and target tables):

1. `import_bpi_csv_to_old_db.py` — CSV → `*_raw` tables in `bpi2020_old_erp_db`
2. `create_ai_native_db_schema.py` — creates `cleaned_event_logs`, `ai_ready_cases`, `ai_ready_documents`, `transformation_logs`, `sync_state`
3. `clean_and_load_to_ai_db.py` — reads `RAW_TABLES` (`:80`), writes `cleaned_event_logs` (`:312`)
4. `build_ai_ready_cases.py` — reads `cleaned_event_logs` (`:620`), writes `ai_ready_cases` (`:391`)
   4b. `parse_bpi_documents.py` — writes `ai_ready_documents` (`:325`)
5. `build_unified_bpi_knowledge_base.py` — reads both (`:413`, `:431`)
6. `generate_and_store_embeddings.py` — reads unified output, writes Qdrant
7. `search_erp_knowledge.py`, `verify_cross_store_integrity.py` — consumers

**Where that order is documented:** it **is** documented, at `README.md:551-578`, as an explicit ordered list of PowerShell commands. This is not an undocumented order. It is additionally enforced defensively at runtime: `build_ai_ready_cases.py:608` calls `check_postgres(..., required_tables=('cleaned_event_logs','ai_ready_cases'))`, and `:631-639` refuses to proceed and writes a `failed` row to `transformation_logs` when the upstream table is empty.

## 1.4 Dependency Map

**Manifests found:** `requirements.txt`, `pyproject.toml`, `frontend/package.json`, `frontend/package-lock.json`. No `environment.yml`, no `Pipfile`, no Python lockfile.

`pyproject.toml:11-14` deliberately declares `dependencies = []`, with a comment stating `requirements.txt` is the single source of truth so the two lists cannot drift. That is a defensible choice, documented in place.

| Manifest | Declared | Pinned | Unpinned |
|---|---|---|---|
| `requirements.txt` | 18 | 3 (`fastapi==0.141.1`, `uvicorn==0.52.3`, `python-multipart==0.0.32`) | 15 |
| `pyproject.toml` | 0 (intentional) | — | — |
| `frontend/package.json` | 9 (2 runtime, 7 dev) | 0 exact (caret ranges) | but `package-lock.json` exists |

**Pinned fraction: 3/18 = 16.7%.** Confirms README known issue #10 (`README.md:684`).

**Packages imported in code but absent from the manifest:** none found on the runtime path. Verified:

| Import | Manifest entry |
|---|---|
| `dotenv` | `python-dotenv` (`requirements.txt:4`) |
| `fitz` | `pymupdf` (`:5`) |
| `PIL` | `pillow` (`:7`) |
| `yaml` | `PyYAML` (`:14`) |
| `cryptography` | `cryptography` (`:15`), used by the cold tier |
| `pytesseract` | `pytesseract` (`:6`) |

One gap, developer-facing only: **`pytest-timeout` is neither installed nor declared.** Confirmed empirically — `python -m pytest --timeout=600` returned `error: unrecognized arguments: --timeout=600`. No code imports it, so this affects test ergonomics, not runtime.

## 1.5 Configuration Surface

Every configuration mechanism in the repository:

| Mechanism | Present? | Detail |
|---|---|---|
| `.env` | Yes (gitignored, never committed — verified in 3.6) | 30 variables, **all legacy names** |
| `.env.example` | Yes, tracked | 161 lines, 44 variables, heavily commented |
| `frontend/.env.example` | Yes | `VITE_API_BASE_URL`, `VITE_API_KEY` |
| `config.*` / YAML / TOML / JSON config | **None** | No configuration file format is supported anywhere |
| `python-dotenv` loading | Yes | `bpi2020/common/config.py:75`; `erp_pipeline/runtime/settings.py:351-358` (`override=False` — real environment always wins) |

Only two modules read `os.getenv`/`os.environ` directly in a generic way; everything else routes through them. Full enumeration:

Command: `grep -rn 'os\.getenv\|os\.environ' src/ --include='*.py'` → 17 call sites, listed below by variable.

### Environment variable table

Required-at-startup means: `RuntimeSettings.require_valid()` (`runtime/settings.py:321`) or `PostgresSettings._from_prefix` (`bpi2020/common/config.py:180`) refuses to proceed without it.

| Variable | Read at (file:line) | Default | In `.env.example`? | Required at startup? |
|---|---|---|---|---|
| `ERP_API_HOST` | `api/config.py:61` | `127.0.0.1` | Yes (105) | No |
| `ERP_API_PORT` | `api/config.py:62` | `8000` | Yes (106) | No |
| `ERP_API_KEY` | `api/config.py:63` | `None` | Yes (108) | **Conditionally** — required for non-loopback bind (`settings.py:294-302`) |
| `ERP_API_PROTECT_READS` | `api/config.py:64` | `false` | Yes (110) | No |
| `ERP_API_CORS_ORIGINS` | `api/config.py:58` | `""` (closed) | Yes (112) | No |
| `ERP_API_MAX_UPLOAD_BYTES` | `api/config.py:70` | `67108864` | Yes (113) | No |
| `ERP_API_UPLOAD_DIR` | `api/config.py:72` | `var/uploads` | Yes (114) | Validated non-empty (`settings.py:311`) |
| `ERP_SQL_SERVER_LIVE_VERIFIED` | `api/config.py:73` | `false` | Yes (116) | No |
| `PIPELINE_DB_HOST` | `runtime/settings.py:90` via `_db_setting` | `localhost` | Yes (25) | No |
| `PIPELINE_DB_PORT` | `runtime/settings.py:91` | `5432` | Yes (26) | No |
| `PIPELINE_DB_NAME` | `runtime/settings.py:92` | `erp_ai_native_db` | Yes (27) | **Yes** (`settings.py:280`) |
| `PIPELINE_DB_USER` | `runtime/settings.py:93` | `postgres` | Yes (28) | **Yes** (`settings.py:282`) |
| `PIPELINE_DB_PASSWORD` | `runtime/settings.py:94` | none | Yes (29) | **Yes** (`settings.py:284`) |
| `AI_DB_*` (5) | `runtime/settings.py:54-56` (legacy fallback) | — | Documented as legacy (24) | No |
| `ERP_QDRANT_ENABLED` | `runtime/settings.py:163` | `true` | Yes (128) | No |
| `ERP_QDRANT_URL` | `runtime/settings.py:153` | `None` | Yes (130) | No |
| `ERP_QDRANT_HOST` | `runtime/settings.py:154` | `localhost` | Yes (131) | No |
| `ERP_QDRANT_PORT` | `runtime/settings.py:155` | `6333` | Yes (132) | No |
| `ERP_QDRANT_API_KEY` | `runtime/settings.py:156` | `None` | Yes (133) | No |
| `ERP_QDRANT_HOT_COLLECTION` | `runtime/settings.py:157` | `erp_vectors_hot` | Yes (134) | No |
| `ERP_QDRANT_WARM_COLLECTION` | `runtime/settings.py:159` | `erp_vectors_warm` | Yes (135) | No |
| `ERP_QDRANT_DIMENSION` | `runtime/settings.py:161` | `384` | Yes (136) | No |
| `ERP_QDRANT_TIMEOUT_SECONDS` | `runtime/settings.py:162` | `60` | Yes (137) | No |
| `ERP_COLD_ENABLED` | `runtime/settings.py:208` | `true` | Yes (140) | No |
| `ERP_COLD_ARCHIVE_DIR` | `runtime/settings.py:210` | `var/cold-archive` | Yes (141) | No |
| `ERP_COLD_ARCHIVE_KEY` | `runtime/settings.py:216`; `storage/cold_tier.py:108,113` | none | Yes (146) | **Yes when cold enabled** (`settings.py:304-309`) |
| `ERP_BOOTSTRAP_ON_STARTUP` | `runtime/settings.py:261` | `true` | Yes (123) | No |
| `ERP_EMBEDDING_ENABLED` | `runtime/settings.py:262` | `true` | Yes (156) | No |
| `ERP_EXECUTOR_WORKERS` | `runtime/settings.py:263` | `2` | Yes (155) | No |
| `ERP_ALLOW_INSECURE_BIND` | `runtime/settings.py:264` | `false` | Yes (161) | No |
| `ERP_SECRET_<REF>` | `orchestration/secrets.py:50,53` | none | Documented pattern (151-152) | Per credentialed source |
| `TESSERACT_CMD` | `ingestion/ocr.py:85`; `bpi2020/common/config.py:240` | PATH discovery | Yes (69) | No |
| `ERP_SOURCE_DB_{HOST,PORT,NAME,USER,PASSWORD}` | `bpi2020/common/config.py:186` | `localhost`/`5432`/`bpi2020_old_erp_db`/`postgres`/— | Yes (15-19) | **PASSWORD yes** (`config.py:180`) |
| `VECTOR_DB_{URL,API_KEY,HOST,PORT,TIMEOUT_SECONDS,RECREATE_COLLECTION}` | `bpi2020/common/config.py:43-48` | see `.env.example` | Yes (40-51) | No |
| `VECTOR_COLLECTION` | `bpi2020/common/config.py:236` | `bpi2020_erp_knowledge` | Yes (42) | No |
| `EMBEDDING_MODEL_ID` | `bpi2020/common/config.py:226` | `sentence-transformers/all-MiniLM-L6-v2` | Yes (57) | No |
| `EMBEDDING_BATCH_SIZE` | `bpi2020/common/config.py:231` | `64` | Yes (58) | No |
| `SYNC_POLL_INTERVAL_SECONDS` | `bpi2020/sync/realtime_incremental_sync.py:82` | `10` | Yes (93) | No |
| `SYNC_BATCH_SIZE` | `bpi2020/sync/realtime_incremental_sync.py:83` | `1000` | Yes (94) | No |
| `MONGO_PHASE5_*` (8) | test fixtures only | — | Yes (81-88) | Live tests only |
| `VITE_API_BASE_URL` | `frontend/src/api/client.ts` | `http://127.0.0.1:8000` | `frontend/.env.example` | No |
| `VITE_API_KEY` | **declared but never read** — only `frontend/src/vite-env.d.ts:5` | — | `frontend/.env.example:16` | No — see 2.9 |

**Coverage of `.env.example`: complete.** Every variable read by code appears in `.env.example` (or, for `ERP_SECRET_<REF>`, its pattern is documented at lines 148-152). No orphan reads found.

**A notable inversion:** the real `.env` contains **only legacy variable names** (`DB_*`, `BPI_OLD_DB_*`, `AI_DB_*`, `QDRANT_*`, `EMBEDDING_MODEL`, `TESSERACT_PATH`, `MYSQL_SAKILA_PASSWORD`, `MONGO_PHASE5_*`) and **none** of the Phase 13 runtime variables (`ERP_API_*`, `ERP_QDRANT_*`, `ERP_COLD_*`). The alias layer at `bpi2020/common/config.py:32-52` and `runtime/settings.py:48-56` makes the BPI scripts and the pipeline database work anyway, but the generic runtime would start on defaults with cold storage enabled and no key — which `require_valid()` correctly refuses (`settings.py:304`). See 5.3.

### What fraction of behaviour is configuration versus source?

The honest answer differs sharply by codebase, so a single number would mislead. Derivation, using the 19 stages of section 2.1 as the unit of behaviour:

**Generic framework (`erp_pipeline`).** Source identity, connection details, credentials, entity selection, field mapping, and type conversion are supplied at runtime and never read from source literals:

- Connection details arrive per source via `POST /v1/sources` and are persisted in `registered_sources`; passwords are never stored, only a `credential_ref` resolved to `ERP_SECRET_<REF>` (`orchestration/secrets.py:50-53`).
- Which entity is extracted is chosen from the *discovered* schema, not named in code — `orchestration/extraction.py:64-82` rejects any entity not present in the discovered schema.
- Field mapping is generated by the mapping engine and persisted as a `MappingProfile`; the target vocabulary itself is loadable from a plain dict (`mapping/canonical_model.py:295-341`, `from_dict`).
- Infrastructure (DB, Qdrant, cold tier, workers, auth) is 100% environment-driven — 31 variables, zero machine-specific literals (`runtime/settings.py:10-14` states this and the code honours it).

**Counting the 19 stages: 14 are parameterised entirely by configuration and runtime input. 3 are partly fixed in source** (extraction driver, embedding model, embedding dimension — see 2.3). **2 do not exist.** That is **14/17 = 82% of the implemented stages configuration-driven.**

**BPI prototype (`bpi2020`).** Only infrastructure is configurable (hosts, ports, database names, credentials, collection name, model id, batch sizes — 21 variables). Every *semantic* decision is a source literal: which tables exist (`clean_and_load_to_ai_db.py:80-86`), which column holds the case id (`:134-142`), which holds the activity (`:156-162`), which are timestamps (`:176-185`) or amounts (`:191-202`), which currency symbols to strip (`:228-230`), and every target table name. **Counting the same way: 0 of the 9 semantic stages are configuration-driven; roughly 21 infrastructure settings are.** The prototype is approximately **30% configuration-driven** (infrastructure) and **0% configuration-driven** (schema semantics).
---

# PART 2 — DATA PIPELINE FUNCTIONAL AUDIT

## 2.1 Pipeline Stage Map

Two independent implementations exist for most stages. Both are assessed. `G` = generic (`erp_pipeline`), `B` = BPI prototype (`bpi2020`).

| # | Stage | Implementing file(s) | Input | Output | Triggered by | Status |
|---|---|---|---|---|---|---|
| 1 | Source ingestion / raw replica | **G** `orchestration/extraction.py:105-237` (relational/Mongo/CSV snapshot extractors); `ingestion/csv_ingestion.py`, `pdf_ingestion.py`, `image_ingestion.py` — **B** `bpi2020/storage/import_bpi_csv_to_old_db.py` | Registered source or uploaded file / 5 BPI CSVs | `SourceRecord` tuples / `*_raw` tables | `POST /v1/jobs` stage `EXTRACT`; `POST /v1/files/csv` / manual script | **IMPLEMENTED** (both) |
| 2 | Schema discovery / profiling | **G** `discovery/relational.py:57-758`, `discovery/mongodb_inference.py`, `discovery/profiling.py:72-325` — **B** heuristic only, `clean_and_load_to_ai_db.py:133-204` | Live DB connection | `SourceSchema` with entities, fields, keys, relationships | `POST /v1/sources/{id}/discover` (`routers.py:376`) | **IMPLEMENTED** (G) / **PARTIAL** (B — keyword heuristics, see 2.4) |
| 3 | Cleaning | **G** `transformation/normalizer.py` — **B** `clean_and_load_to_ai_db.py:110-130, 207-240` | Raw records | Normalized values | `TRANSFORM` stage / script | **IMPLEMENTED** (both) |
| 4 | Normalization | **G** `mapping/normalization.py`, `transformation/type_converter.py` — **B** `clean_and_load_to_ai_db.py:93-107` (column names), `:207` (timestamps), `:220` (amounts) | Cleaned records | Typed canonical values | `TRANSFORM` | **IMPLEMENTED** (both) |
| 5 | Field mapping (source → canonical) | **G** `mapping/engine.py`, `scoring.py`, `aliases.py`, `service.py` — **B** none | `SourceSchema` + `CanonicalTargetModel` | `MappingProfile` with per-field evidence | `POST /v1/mappings/suggest` (`routers_data.py:341`), `MAP` stage | **IMPLEMENTED** (G) / **MISSING** (B — no mapping layer; columns pass through into a JSONB blob) |
| 6 | State-code translation | — | — | — | — | **MISSING (both).** No module translates source status values into canonical states. Grep for state-machine vocabulary across `src/` returns only `status` as a free-text canonical *field* (`mapping/canonical_model.py:391-397`), never a translation table. The BPI case document carries `activity_sequence` (raw activity strings) but no translated state. |
| 7 | Case construction | **B** `build_ai_ready_cases.py:244-340`, grouping at `:643` — **G** none | `cleaned_event_logs` | `ai_ready_cases` rows + case JSON | Manual script | **PARTIAL** — implemented, but only in the BPI prototype and dataset-specific (groups on the literal columns `process_type`, `normalized_case_id`). The generic framework has **no case concept at all**. |
| 8 | Timeline / event sequence | **B** `build_ai_ready_cases.py:251-260` (sort), `:267` (events), `:279` (`activity_sequence`) | Grouped events | Ordered `events[]` + `activity_sequence[]` | Manual script | **PARTIAL** — real ordered timeline with timestamps, BPI-only |
| 9 | Allowed-next-state derivation | — | — | — | — | **MISSING (both).** No process-state machine exists anywhere. Grep across all 293 `.py` files finds no `allowed_next_state`, no transition table, no state graph. |
| 10 | Document extraction / parsing | **G** `ingestion/pdf_ingestion.py`, `image_ingestion.py`, `ocr.py` — **B** `documents/parse_bpi_documents.py` | PDFs / PNGs | Extracted text + document records | `POST /v1/files/documents` (`routers_data.py:114`) / script | **IMPLEMENTED** (both) |
| 11 | Document-to-case linking | — | — | — | — | **MISSING.** `parse_bpi_documents.py` writes `ai_ready_documents` with its own `document_record_id`; `build_unified_bpi_knowledge_base.py:413,431` unions cases and documents into one flat list but establishes **no linkage** between a document and the case it evidences. No foreign key, no join column, no `evidence_ids`. |
| 12 | Embedding generation | **G** `ai/embedding.py:90-250`, `ai/service.py` — **B** `embeddings/generate_and_store_embeddings.py:170-185` | Canonical text | 384-dim vectors | `EMBED` stage / script | **IMPLEMENTED** (both) |
| 13 | Vector store upsert | **G** `storage/hot_tier.py`, `warm_tier.py`, `cold_tier.py`, `hybrid_store.py` — **B** `generate_and_store_embeddings.py:506-517` | Vectors + payload | Qdrant points | `TIER_ROUTE` stage / script | **IMPLEMENTED** (both) |
| 14 | Incremental synchronisation | **G** `sync/coordinator.py:125-580`, `extractor.py`, `state.py` — **B** `sync/realtime_incremental_sync.py` | Watermark + source rows | Propagated canonical records / `cleaned_event_logs` | `INCREMENTAL_SYNC` job type | **IMPLEMENTED** (G, PostgreSQL only) / **PARTIAL** (B — stops at cleaned events, see 2.5) |
| 15 | Freshness tracking | **G** `sync/state.py` (watermarks) — **B** `sync_state` table (`create_ai_native_db_schema.py:146-150`) | — | `last_synced_source_id`, `last_synced_at` | Sync run | **PARTIAL** — freshness is tracked **per source table**, never per case or per record. No `is_stale` flag exists anywhere. A downstream consumer cannot ask "is this case stale?" |
| 16 | Data lineage recording | **G** `schemas/canonical_models.py:86-144` (`RecordProvenance`: schema id/version, ingestion method, original record id, file path, page number, API operation, extracted-at) — **B** `case_json.record_source`, `source_database`, `source_table_layer` (`build_ai_ready_cases.py:299-301`) | — | Provenance on every record | Automatic | **IMPLEMENTED** (G — a genuinely thorough provenance model) / **PARTIAL** (B — three hardcoded literal strings) |
| 17 | Transformation logging | **B** `transformation_logs` table, written at `clean_and_load_to_ai_db.py:479`, `build_ai_ready_cases.py:542`, `parse_bpi_documents.py:409`, `generate_and_store_embeddings.py:409`, `realtime_incremental_sync.py:356` — **G** `orchestration/job_store.py` (`PostgresJobStore`) with per-stage `StageRun` records | Stage outcome | Durable log rows | Every stage/script | **IMPLEMENTED** (both) |
| 18 | Data quality metrics | **G** `transformation/quality.py:90-162`, `mapping/coverage.py:36-137`, `ai/evaluation.py:87-159` | Records / mappings / vectors | Issue counts by severity, coverage ratios, retrieval accuracy | `VALIDATE` stage; benchmark scripts | **IMPLEMENTED** (G) — see 2.12 |
| 19 | Query/service API | **G** `api/routers.py`, `api/routers_data.py` — 20 endpoints — **B** `retrieval/search_erp_knowledge.py` (local CLI only) | HTTP requests | JSON responses | uvicorn | **IMPLEMENTED** (G) — see 2.7 |

**Tally:** 12 IMPLEMENTED · 4 PARTIAL · 3 MISSING. Arithmetic in 5.1.

## 2.2 Canonical Model Verification

Two distinct "canonical models" exist, and neither is the case model the brief describes.

### (a) Generic canonical contract — `src/erp_pipeline/schemas/canonical_models.py`

`CanonicalEnvelope` (`:147-207`), shared by `CanonicalRecord` (`:210`) and `CanonicalDocument`:

| Field | Type | Populated from | Always populated? |
|---|---|---|---|
| `record_id` | `str` | `make_canonical_record_id` (`identity.py`) | **Yes** — `require_text` at `:170` |
| `record_type` | `RecordType` enum | Constructor | **Yes** — `:171` |
| `source` | `SourceReference` | Constructor; typed, not a dict (`:181-186` rejects dicts) | **Yes** |
| `schema_version` | `str` | `CANONICAL_MODEL_VERSION` (`:159`) | **Yes** — this is the `source_version` equivalent |
| `content_hash` | `str \| None` | `compute_content_hash` | No — optional |
| `sensitivity` | `SensitivityLevel` | Default `INTERNAL` (`:161`) | **Yes** |
| `provenance` | `RecordProvenance \| None` | Ingestion layer | No |
| `created_at` / `updated_at` | aware `datetime` | `utc_now` (`:163-164`) | **Yes** — `require_aware_datetime` |
| `metadata` | `Mapping` | Caller | Yes (defaults `{}`); credential-key denylist enforced (`:177`) |
| `normalized_data` | JSON object | Mapping profile | `CanonicalRecord` only |
| `entity_type` | open `str` | Mapping profile | `CanonicalRecord` only |

**Default target vocabulary** — `src/erp_pipeline/mapping/canonical_model.py:505-547`, model id `erp_core@1.0`:

| Entity | Fields (file:line) |
|---|---|
| `invoice` | `invoice_id` (:361), `customer_id` (:369), `amount` (:377), `currency` (:385), `status` (:391), `issued_on` (:398) |
| `customer` | `customer_id` (:414), `name` (:422), `email` (:434), `phone` (:447) |
| `purchase_order` | `purchase_order_id` (:462), `supplier_id` (:476), `amount` (:484), `status` (:497) |

This vocabulary is **replaceable without editing code** — `CanonicalTargetModel.from_dict` (`:295-341`) builds a model from a plain dictionary, and `model_id`/`version` travel onto every generated mapping profile.

### (b) BPI case record — `src/bpi2020/transformation/build_ai_ready_cases.py:295-340`

The actual field set produced, extracted verbatim from `case_json` (`:295-310`) and the returned row (`:330-340`):

| Field | Type | Populated from | Always populated? |
|---|---|---|---|
| `case_record_id` | `str` | `make_case_record_id(process_type, case_id)` (`:293`) | **Yes**; run-level uniqueness enforced at `:655-661` |
| `content_hash` | `str` (sha256) | `compute_content_hash` (`:316`) | **Yes** |
| `case_id` | `str` | `first_row["normalized_case_id"]` (`:264`) | **Yes** — query filters `IS NOT NULL` (`:621`) |
| `process_type` | `str` | `first_row["process_type"]` (`:265`) | **Yes** — group key |
| `record_source` | `str` | **literal** `"bpi_challenge_2020"` (`:299`) | Yes (constant) |
| `source_database` | `str` | **literal** `"erp_ai_native_db"` (`:300`) | Yes (constant) |
| `source_table_layer` | `str` | **literal** `"cleaned_event_logs"` (`:301`) | Yes (constant) |
| `total_events` | `int` | `len(events)` (`:278`) | **Yes** |
| `start_timestamp` / `end_timestamp` | ISO str \| None | `min`/`max` of event timestamps (`:275-276`) | **No** — `None` when no event carries a parseable timestamp |
| `duration_days` | `float \| None` | `calculate_duration_days` (`:281`) | **No** — `None` if either bound missing |
| `activity_sequence` | `list[str]` | `get_activity_sequence` (`:279`) | Yes; **may be empty** (`:181` filters null/`"None"`/`"nan"`) |
| `unique_activities` | `list[str]` | `get_unique_activities` (`:280`) | Yes; may be empty |
| `events` | `list[dict]` | `clean_event_record` per row (`:267`) | **Yes**, ≥1 by construction |
| `events[].event_record_id` | `str` | Deterministic event id | Yes |
| `events[].activity` | `str \| None` | `normalized_activity` | No |
| `events[].timestamp` | ISO str \| None | `safe_timestamp` (`:147`) | No |
| `events[].attributes` | `dict` | Full source row JSONB (`:150`) | Yes (may be `{}` — `safe_json_load` returns `{}` on parse failure, `:93`) |
| `ai_text` / `case_summary` | `str` | `build_case_summary` (`:202-241`) | **Yes** |

### Canonical field checklist

| Required field | Produced? | Where | Ever null/empty in practice? |
|---|---|---|---|
| `case_id` | **Yes** (B only) | `build_ai_ready_cases.py:264` | No — `WHERE normalized_case_id IS NOT NULL` (`:621`) |
| `process_type` | **Yes** (B only) | `:265` | No — group key |
| `current_state` | **ABSENT** | — | — |
| `entities.employee_id` | **ABSENT** as a canonical field | Present only as an untyped key inside `events[].attributes` if the source CSV had such a column | Unknown/unreliable |
| `entities.amount` | **ABSENT** as a canonical field | `clean_and_load_to_ai_db.py:190-204` detects amount-like columns and cleans them, but they land in the opaque `record_data` JSONB; never promoted to a named canonical field | — |
| `entities.currency` | **ABSENT** | Currency symbols are **stripped and discarded** (`clean_and_load_to_ai_db.py:228-230` removes `€` and `$` without recording which was seen) | — |
| `timeline` (ordered, timestamped) | **Yes** (B only) | `events[]` sorted at `:251-260`; `activity_sequence` at `:279` | Timestamps may be `None`; ordering then falls back to `id` (`:258`, `na_position="last"`) |
| `allowed_next_states` | **ABSENT** | — | — |
| `documents` / `evidence_ids` | **ABSENT** | Documents exist in a separate table with no link (stage 11) | — |
| `freshness.last_synced_at` | **ABSENT from the case record**. Exists only per source table in `sync_state` (`create_ai_native_db_schema.py:148`) | — | — |
| `freshness.is_stale` | **ABSENT** | — | — |
| `source_version` | **Partially** — `schema_version` exists on the generic envelope (`canonical_models.py:159`); the BPI case record has **no version field**, only three constant source literals | — | — |

### Canonical fields explicitly absent, and which downstream component each blocks

| Absent field | Blocked consumer |
|---|---|
| `current_state` | **Governance** — cannot evaluate a policy against a case's present state; and **orchestration** cannot decide what to do next. This is the single most damaging absence. |
| `allowed_next_states` | **Orchestration** — cannot constrain the AI to legal transitions. The safety property the whole system exists to provide is unimplementable without it. |
| `entities.{employee_id, amount, currency}` | **Governance** — cannot apply value thresholds, approval limits, or separation-of-duty rules; **bridge** cannot populate an ERP write. |
| `documents` / `evidence_ids` | **Governance** — cannot cite evidence for a decision; **retrieval** cannot ground an answer in the policy document that applies. |
| `freshness.{last_synced_at, is_stale}` | **All three** — no consumer can tell whether it is acting on current data. |

## 2.3 Hardcoded Values Census

Severity: **HIGH** = blocks onboarding a different ERP source without editing code. **MEDIUM** = blocks running in a different environment. **LOW** = cosmetic.

### HIGH severity

| # | Value | file:line | Should be instead |
|---|---|---|---|
| 1 | `{"DomesticDeclarations.csv": "domestic_declarations_raw", ...}` — 4 filename→table pairs | `bpi2020/storage/import_bpi_csv_to_old_db.py:25-28` | A source manifest supplied per organisation |
| 2 | `RAW_TABLES` — 5 table→process-type pairs | `bpi2020/transformation/clean_and_load_to_ai_db.py:80-86` | Schema mapping from the admin interface |
| 3 | `RAW_TABLES` — the same 5 pairs, duplicated | `bpi2020/sync/realtime_incremental_sync.py:73-79` | Same config as #2; duplication means two files must change together |
| 4 | Case-id column candidates `case_concept_name, case_id, case, declaration_id, request_id, permit_id, id` | `clean_and_load_to_ai_db.py:134-142` | Configured field mapping |
| 5 | Activity column candidates `concept_name, activity, event, task, action` | `:156-162` | Configured field mapping |
| 6 | Timestamp keyword list (8 terms) | `:176-185` | Configured field types |
| 7 | Amount keyword list (10 terms) | `:191-202` | Configured field types |
| 8 | Currency symbols `€`, `$` stripped and discarded | `:228-230` | Configured currency handling; the symbol should be **retained** as `entities.currency`, not deleted |
| 9 | `"bpi_challenge_2020"` as `record_source` | `build_ai_ready_cases.py:299` | Registered source id |
| 10 | `"erp_ai_native_db"` as `source_database` in the record body | `:300` | Runtime configuration |
| 11 | `"cleaned_event_logs"` as `source_table_layer` in the record body | `:301` | Runtime configuration |
| 12 | Target table names `cleaned_event_logs`, `ai_ready_cases`, `ai_ready_documents`, `transformation_logs`, `sync_state` as SQL literals across 7 files | `create_ai_native_db_schema.py:56,91,128,146,152`; `clean_and_load_to_ai_db.py:312,457,479`; `build_ai_ready_cases.py:368,391,519,542`; `parse_bpi_documents.py:301,325,409`; `generate_and_store_embeddings.py:314,322,342,351,409`; `realtime_incremental_sync.py:307,322,356,415`; `verify_cross_store_integrity.py:165,179,182,185`; `build_unified_bpi_knowledge_base.py:345,413,431` | A schema/table configuration object |
| 13 | Grouping keys `["process_type", "normalized_case_id"]` | `build_ai_ready_cases.py:643` | Configured case-grouping rule |
| 14 | `drivername="postgresql+psycopg2"` on the **generic** extraction path — blocks MySQL/SQL Server extraction despite both having connectors and discovery | `orchestration/service.py:202`, `:404` | Driver selected from the registered source's `source_type` |
| 15 | `DEFAULT_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"` with **no environment read anywhere in `erp_pipeline`**; `_LazyEmbeddingService` constructs `SentenceTransformerModel()` with no argument | `ai/embedding.py:42`; `runtime/services.py:273` | An `ERP_EMBEDDING_MODEL_ID` variable. `.env.example:57` documents `EMBEDDING_MODEL_ID`, but **only `bpi2020` honours it** (`bpi2020/common/config.py:226`) |

**Total HIGH severity items: 15.**

### MEDIUM severity

| Value | file:line | Should be instead |
|---|---|---|
| `drivername="postgresql+psycopg2"` (4 further sites) | `bpi2020/common/config.py:201`; `catalog/config.py:170`; `connectors/postgresql.py:35`; `runtime/settings.py:106` | Acceptable where the class *is* the PostgreSQL adapter (`connectors/postgresql.py`); questionable in `runtime/settings.py` |
| `UPSERT_BATCH_SIZE = 500` | `build_ai_ready_cases.py:72` | Configurable batch size |
| `UPSERT_BATCH_SIZE = 2000` | `clean_and_load_to_ai_db.py:73` | Configurable |
| Chunk size `5000` in prune temp-table insert | `build_ai_ready_cases.py:510`; `clean_and_load_to_ai_db.py:~450` | Configurable |
| `OUTPUT_DIR = PROJECT_ROOT / "data" / "bpi2020" / "ai_ready"` | `build_ai_ready_cases.py:57` | Configurable output path |
| `OUTPUT_DIR = .../ "cleaned"` | `clean_and_load_to_ai_db.py:54` | Configurable |
| Output filenames `bpi2020_ai_ready_cases.json/.jsonl` | `build_ai_ready_cases.py:347-348` | Configurable |
| Default DB names `bpi2020_old_erp_db`, `erp_ai_native_db` | `bpi2020/common/config.py:186,191`; `runtime/settings.py:83,92` | Defaults only, overridable — acceptable |
| Default collection `bpi2020_erp_knowledge` | `bpi2020/common/config.py:236` | Overridable via `VECTOR_COLLECTION` — acceptable |
| `erp_vectors_hot` / `erp_vectors_warm` | `runtime/settings.py:144-145` | Overridable — acceptable |
| Dimension `384` default | `runtime/settings.py:146,161` | Overridable via `ERP_QDRANT_DIMENSION` — acceptable |
| Timestamp format `"%Y-%m-%dT%H:%M:%SZ"` | `build_ai_ready_cases.py:111`; `clean_and_load_to_ai_db.py:215` | ISO 8601 is a reasonable fixed choice — borderline LOW |
| Summary truncation `unique_activities[:10]` | `build_ai_ready_cases.py:234` | Configurable |

### LOW severity

| Value | file:line |
|---|---|
| `API_VERSION = "1.0"`, `API_PREFIX = "/v1"`, `API_TITLE` | `api/config.py:18-20` |
| `max_page_size = 100`, `default_page_size = 50` | `api/config.py:43-44` |
| Progress print interval `% 1000` | `build_ai_ready_cases.py:652` |
| Validation error truncation `errors()[:20]` | `api/main.py:212` |
| Duplicate-id report slice `[:5]` | `build_ai_ready_cases.py:659` |

### The decisive question

> If a second organisation with a completely different ERP schema were onboarded tomorrow, how many source files would need editing, and which ones?

**The answer depends entirely on which of the two codebases is used, and this is the most important distinction in the audit.**

**Path A — via the generic framework (`erp_pipeline`): 0 files for the core path, 2 files for full coverage.**

Onboarding is a runtime operation, not a code change:

1. `POST /v1/sources` registers connection details and a `credential_ref` (`api/routers.py:263`).
2. `POST /v1/sources/{id}/discover` profiles the unknown schema automatically (`routers.py:376` → `discovery/relational.py:74`).
3. `POST /v1/mappings/suggest` generates a mapping profile with per-field evidence (`routers_data.py:341`).
4. `PUT /v1/mappings/{id}` applies human overrides (`routers_data.py:360`).
5. `POST /v1/jobs` runs the pipeline (`routers_data.py:463`).

**Zero source files** need editing for a PostgreSQL source. Two files need editing if the new source is MySQL, SQL Server, or MongoDB, because extraction is driver-locked:

- `src/erp_pipeline/orchestration/service.py:202` (`_sqlalchemy_factory`)
- `src/erp_pipeline/orchestration/service.py:404` (`_incremental_engine`)

Both hardcode `postgresql+psycopg2`. This is the same defect the README documents as known issue #1 (`README.md:675`). One further file would need editing to change the embedding model: `src/erp_pipeline/runtime/services.py:273`.

**So: 0 files (PostgreSQL), 1 file / 2 call sites (any other database), 2 files (other DB + different embedding model).**

**Path B — via the BPI prototype (`bpi2020`): 7 files.**

1. `src/bpi2020/storage/import_bpi_csv_to_old_db.py` — filename→table map (`:25-28`)
2. `src/bpi2020/storage/create_ai_native_db_schema.py` — table DDL (`:56-160`)
3. `src/bpi2020/transformation/clean_and_load_to_ai_db.py` — `RAW_TABLES` (`:80`), all four column-detection heuristics (`:133-204`), currency symbols (`:228`)
4. `src/bpi2020/sync/realtime_incremental_sync.py` — duplicated `RAW_TABLES` (`:73-79`)
5. `src/bpi2020/transformation/build_ai_ready_cases.py` — grouping keys (`:643`), source literals (`:299-301`)
6. `src/bpi2020/transformation/build_unified_bpi_knowledge_base.py` — source table names (`:413,431`)
7. `src/bpi2020/verification/verify_cross_store_integrity.py` — table names (`:165-185`)

**The correct reading:** the repository has *already solved* the onboarding problem in `erp_pipeline`, and `bpi2020` is a retained baseline rather than the intended onboarding path — a framing the README states explicitly (`README.md:128-152`, "Stabilized source-specific prototype"). The remaining Goal A gap is not "the pipeline is hardcoded"; it is that **the generic path cannot yet extract from any database except PostgreSQL**, and **the generic path has no case model** (2.2), so it cannot produce the output the downstream components need.

## 2.4 Schema Handling Assessment

**The code supports (a), (b), and (c) simultaneously, in different modules. (d) does not apply.**

**(c) — Source schema unknown, inferred/profiled automatically. IMPLEMENTED, generic framework.**

- **Relational discovery** — `discovery/relational.py:74` (`discover`). Uses SQLAlchemy `Inspector` to enumerate namespaces (`:128`), tables and views (`:194`), columns with types (`:312`), and foreign-key relationships (`:381-495`). Coverage: PostgreSQL, MySQL, SQL Server (SQL Server mock-tested only — `README.md:661,681`).
- **Key detection** — `_single_column_unique_names` (`:660`) and `_composite_unique_constraints` (`:686`) derive uniqueness from primary keys, unique constraints, and unique indexes. This is real key inference, not a guess.
- **Type mapping** — `discovery/type_mapping.py` maps vendor types to a `FieldDataType` enum. Unrecognised types degrade gracefully (observed live: `SAWarning: Did not recognize type 'geometry' of column 'location'` during `test_live_mysql_discovery`, which still passed).
- **Column profiling** — `discovery/profiling.py:72` (`profile_schema`), `:212` (`_profile_column`). Computes per-column statistics under an explicit **query budget** (`_Budget`, `:50-69`) so profiling an unknown production database cannot run away. This is a genuinely thoughtful piece of engineering.
- **MongoDB observed-schema inference** — `discovery/mongodb_inference.py`. Infers a schema from sampled documents where no declared schema exists.
- **CSV type inference** — `ingestion/csv_inference.py`, with delimiter/encoding detection in `ingestion/detection.py`.

**(a) — Schema known and mapped via configuration. IMPLEMENTED, generic framework.** The mapping engine (`mapping/engine.py`, `scoring.py`) proposes source→canonical field mappings with explicit evidence; `aliases.py` holds an explicit, reviewable alias registry (`canonical_model.py:87-90` states the engine never guesses — a match is reported as `explicit_alias` evidence). Human overrides persist via `PUT /v1/mappings/{mapping_id}` (`routers_data.py:360`). The canonical target vocabulary is itself loadable from a dict (`canonical_model.py:295`).

**(b) — Schema known and mapped via hardcoded logic. This is the BPI prototype.** `clean_and_load_to_ai_db.py:133-204` implements four keyword-based heuristic detectors. Their coverage, precisely:

| Detector | Strategy | Coverage |
|---|---|---|
| `detect_case_id_column` (`:133`) | 7 exact-name candidates in priority order, then a substring fallback requiring both `"case"` and `"id"` in the column name (`:148-150`), else `None` | Finds a case id in any schema using one of 7 names or a `*case*id*` pattern. Returns `None` otherwise, and the row is then dropped by the `IS NOT NULL` filter downstream. |
| `detect_activity_column` (`:155`) | 5 exact candidates, then substring fallback on `"activity"` or `"concept"` (`:168-170`) | Same shape |
| `detect_timestamp_columns` (`:175`) | Substring match against 8 keywords; returns **all** matches | Over-inclusive by design; `convert_timestamps` (`:207`) only rewrites a column when `pd.to_datetime` succeeds for at least one value (`:214`), so false positives are self-correcting |
| `detect_amount_columns` (`:190`) | Substring match against 10 keywords | Over-inclusive; conversion strips `,`, `€`, `$` |

This is genuine schema-uncertainty handling, not a fixed schema — but it is English-keyword-driven and would fail on a non-English ERP or on opaque column names (`FIELD001`), with no configuration escape hatch.

**Column-name conflict handling — IMPLEMENTED, and it counts as schema-uncertainty handling.** A dedicated fixture `tests/fixtures/ingestion/duplicate_headers.csv` exists, alongside `blank_header.csv`, `empty.csv`, `header_only.csv`, `malformed.csv`, `mixed_types.csv`, `nulls.csv`, `sentinels.csv`, `utf8_bom.csv`, and four delimiter variants (`pipe`, `semicolon`, `tab`, `quoted`). These are exercised by `tests/erp_pipeline/ingestion/test_csv_ingestion.py` and `test_csv_inference.py`. Separately, `mapping/canonical_model.py:162-171` rejects a canonical entity that declares the same field name twice, and `normalize_column_name` (`clean_and_load_to_ai_db.py:93`) collapses punctuation so `Case:ID` and `case id` converge — which itself can *create* collisions, and the BPI script does not detect that case. **Duplicate-header handling is implemented in the generic ingestion path and absent from the BPI path.**

## 2.5 Acquisition Strategy

**Both full replica and incremental extract are implemented.** Which one runs is selected by **job type**, not by editing code.

| Strategy | Where the decision is made | Selectable via configuration? |
|---|---|---|
| Full snapshot | `orchestration/planner.py:189` — `STRUCTURED_PIPELINE` produces `(DISCOVER, MAP) + STRUCTURED_TAIL` where `STRUCTURED_TAIL` begins with `EXTRACT` (`:52-59`) | **Yes** — `job_type` in the `POST /v1/jobs` body |
| Incremental | `planner.py:122` — `INCREMENTAL_SYNC` produces `INCREMENTAL_STAGES` beginning `DRIFT_CHECK, EXTRACT_CHANGED` (`:69-77`) | **Yes** — same field |
| BPI full reload | `clean_and_load_to_ai_db.py:542` — `SELECT * FROM "{source_table}"` | **No** — fixed in code |
| BPI incremental | `realtime_incremental_sync.py` — id-watermark poller | **No** — separate script, fixed |

### Generic incremental sync (`erp_pipeline/sync/`)

- **Checkpoint storage:** `sync/state.py`, persisted to PostgreSQL via the `erp_sync` schema. `CheckpointConflictError` (`sync/errors.py:48`) exists specifically to detect concurrent writers.
- **Driving field:** configured per source in `ExtractionConfig` (`sync/extractor.py:71-149`). Watermark columns and ordering columns are declared, not assumed, and every identifier passes `validate_identifier` (`:59`) before reaching SQL — SQL-injection defence on an identifier that cannot be parameterised.
- **Failure mid-sync:** `IncrementalCoordinator._quarantine` (`sync/coordinator.py:487`) isolates a failing change rather than aborting the batch; `_tombstone` (`:361`) records deletions explicitly. The checkpoint advances per processed change, so a crash resumes from the last committed watermark rather than replaying the batch.
- **Idempotency:** yes. Canonical ids are deterministic (`schemas/identity.py`), so re-processing the same source row produces the same `record_id` and upserts rather than duplicates. Verified by `tests/erp_pipeline/sync/test_watermarks_and_state.py` and `test_incremental_propagation.py`.
- **Limitation:** the production extractor is PostgreSQL-only (`orchestration/service.py:404`), and MongoDB incremental sync is explicitly rejected. Documented at `README.md:675`.

### BPI incremental sync (`realtime_incremental_sync.py`)

- **Checkpoint storage:** table `sync_state` (`create_ai_native_db_schema.py:146-150`) — `source_table` PRIMARY KEY, `last_synced_source_id BIGINT`, `last_synced_at TIMESTAMPTZ`. Read at `:304-317`, written by UPSERT at `:320-345`.
- **Driving field:** a monotonically increasing `source_row_id` integer. **This is the weak point** — it detects INSERTs only. An UPDATE to an already-synced row does not change its id and is therefore **never propagated**, and a DELETE is never noticed. The generic coordinator handles both; this poller does not.
- **Failure mid-sync:** the checkpoint is advanced after the batch write. A crash between the write and the checkpoint update causes the batch to be re-read on restart — safe, because the write is an UPSERT on `event_record_id`.
- **Idempotency:** **yes, no duplicates.** Writes go through `INSERT ... ON CONFLICT (event_record_id) DO UPDATE` (`:415-440`), and `event_record_id` is deterministic (`event:{source_system}:{source_entity}:{source_record_key}`, `clean_and_load_to_ai_db.py:19-21`).
- **Documented limitation:** the poller writes only `cleaned_event_logs`. It does not rebuild cases, the unified file, or vectors — so after a sync, `ai_ready_cases` is stale until `build_ai_ready_cases.py` is re-run by hand. Confirmed by absence of any downstream call in the file, and stated at `README.md:680`.

## 2.6 Vector Store Integration

| Property | Generic framework | BPI prototype |
|---|---|---|
| Vector database | Qdrant | Qdrant |
| Connection config | `ERP_QDRANT_*` (9 vars), `runtime/settings.py:150-164` | `VECTOR_DB_*` (6 vars), `bpi2020/common/config.py:43-49` |
| Collection name(s) | `erp_vectors_hot`, `erp_vectors_warm` (`settings.py:144-145`), both overridable | `bpi2020_erp_knowledge` (`config.py:236`), overridable via `VECTOR_COLLECTION` |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2` — **hardcoded**, no env read (`ai/embedding.py:42`, `runtime/services.py:273`) | Same model, but **configurable** via `EMBEDDING_MODEL_ID` (`config.py:226`) |
| Dimension | 384, `ERP_QDRANT_DIMENSION` (`settings.py:161`); asserted against the loaded model rather than assumed (`ai/embedding.py:24` docstring, enforced `:237`) | 384, validated against the live collection (`generate_and_store_embeddings.py:296-303`) — refuses to write on mismatch |
| Tiers | **HOT** (full precision, RAM) / **WARM** (int8 quantized) / **COLD** (encrypted files at rest) — `storage/{hot_tier,warm_tier,cold_tier,hybrid_store}.py` | Single collection |

**Exact text embedded — BPI**, `generate_and_store_embeddings.py:170-185`:

```
Record type: {record_type}
Title: {title}
[Process type: {process_type}]      # only when present
[Document type: {document_type}]    # only when present
Content: {text_for_ai}
```

where `text_for_ai` for a case is the natural-language `case_summary` built at `build_ai_ready_cases.py:202-241` — a sentence naming the case id, process type, event count, first and last activity, start/end timestamps, duration in days, and up to 10 unique activities.

**Metadata stored alongside each vector** — `make_qdrant_payload`, `:188-229`: `record_id`, `unified_record_id`, `record_type`, `source_system`, `source_entity`, `stable_source_key`, `content_hash`, `schema_version`, `source_table`, `source_record_id`, `title`, `primary_reference`, `text_for_ai`, plus any present of `case_id`, `process_type`, `total_events`, `start_timestamp`, `end_timestamp`, `document_id`, `document_name`, `document_type`, `source_file_path`, `text_length`.

**Is the embedded text meaningful for retrieval?** **Yes, with one significant caveat.** The summary is a genuine natural-language rendering carrying the process type, activity vocabulary, temporal extent, and duration — all of which are semantically retrievable. The caveat: it embeds **activity names but no business entities**. Amounts, currencies, and employee identifiers are cleaned by `clean_and_load_to_ai_db.py:190-240` and then buried in `events[].attributes`, never surfaced into `text_for_ai`. A query such as "declarations over €5,000 rejected by the budget owner" cannot match on the amount, because no amount is in the embedded string. This is the retrieval-side consequence of the missing `entities` canonical field (2.2).

**Upsert idempotency: yes, verified in code.**

- BPI: `client.upsert` (`:514`) with deterministic point ids derived from `record_id` — re-running overwrites the same points. `--recreate-collection` (`:108-111`) is opt-in and defaults false; `.env.example:49-51` documents this explicitly.
- Generic: `deterministic_uuid()` (`canonical_models.py:201-207`) projects `record_id` to a stable UUIDv5 for stores requiring UUID point ids.
- Guard rail: `ensure_qdrant_collection` (`:269-305`) refuses to proceed when an existing collection's vector size disagrees with the model's output, rather than writing corrupt vectors.

## 2.7 Service/API Surface

**A service layer exists.** This is the opposite of the brief's contingency: the data is *not* reachable only by local scripts.

Evidence: `artifacts/phase13_openapi.json` declares **20 paths**; all are registered at `api/main.py:220-232`.

| Method + path | Handler (file:line) | Request → Response | Reachable from an entry point? |
|---|---|---|---|
| `GET /v1/health/live` | `routers.py:72` | — → `HealthResponse` | Yes |
| `GET /v1/health/ready` | `routers.py:83` | — → readiness incl. redacted settings (`settings.describe()`) | Yes |
| `GET /v1/capabilities` | `routers.py:216` | — → supported source types, formats, job types | Yes |
| `POST /v1/sources` | `routers.py:263` | `SourceCreate` (connection + `credential_ref`) → `SourceResponse` | Yes |
| `GET /v1/sources` | `routers.py:303` | — → `list[SourceResponse]` | Yes |
| `GET /v1/sources/{source_id}` | `routers.py:315` | — → `SourceResponse` | Yes |
| `POST /v1/sources/{source_id}/test` | `routers.py:324` | — → connectivity result | Yes |
| `POST /v1/sources/{source_id}/discover` | `routers.py:376` | — → `SourceSchema` | Yes |
| `POST /v1/files/csv` | `routers_data.py:72` | multipart upload → tabular ingestion result | Yes |
| `POST /v1/files/documents` | `routers_data.py:114` | multipart upload → document ingestion result | Yes |
| `POST /v1/api-specs/openapi` | `routers_data.py:180` | OpenAPI/Swagger doc → parsed contract | Yes |
| `POST /v1/api-specs/postman` | `routers_data.py:198` | Postman collection → parsed contract | Yes |
| `GET /v1/schemas/{schema_id}` | `routers_data.py:223` | — → `SourceSchema`. **Accepts a `version` query parameter it does not use** (`README.md:687`) | Yes |
| `POST /v1/mappings/suggest` | `routers_data.py:341` | schema id + target model → `MappingProfile` with evidence | Yes |
| `PUT /v1/mappings/{mapping_id}` | `routers_data.py:360` | overrides → updated profile | Yes |
| `POST /v1/mappings/{mapping_id}/validate` | `routers_data.py:416` | — → coverage + validation report | Yes |
| `POST /v1/jobs` | `routers_data.py:463` | `JobRequest` → `JobResponse` | Yes |
| `GET /v1/jobs` | `routers_data.py:499` | — → `list[JobResponse]` | Yes |
| `GET /v1/jobs/{job_id}` | `routers_data.py:519` | — → `JobResponse` incl. per-stage status | Yes |
| `POST /v1/jobs/{job_id}/retry` | `routers_data.py:526` | — → `JobResponse` | Yes |
| `POST /v1/search` | `routers_data.py:551` | `SearchRequest{query, top_k, include_cold}` → `SearchResponse{query_model, dimension, hits[], tiers_searched, deep_search_used, took_ms}` | Yes |
| `GET /v1/records/{record_id}` | `routers_data.py:620` | — → canonical record | Yes |

**Cross-cutting properties, verified in code:**

- **Auth:** shared API key via `X-API-Key` (`api/main.py:139-163`). Mutating routes protected by default; reads optionally (`requires_key`, `api/security.py`). Constant-time comparison (`keys_match`). The key is never logged or echoed (`api/config.py:83-91` overrides `__repr__`).
- **Request tracing:** every response carries `X-Request-ID` (`main.py:136,166`).
- **Error contract:** typed domain errors mapped to status codes (`main.py:175-194`), plus a 422 validation shape (`:196-218`).
- **CORS:** closed unless configured; never wildcard-with-credentials (`main.py:116-125`).

**Exported Python functions intended for other components:** the `__init__.py` of each phase package defines an explicit `__all__` (e.g. `orchestration/__init__.py:202-204`). `erp_pipeline` never imports `bpi2020` — a boundary rule stated at `README.md:720` and confirmed by grep.

**CLI commands:** `erp-api`, `erp-bootstrap` (`pyproject.toml:18-19`), plus the nine BPI scripts and `scripts/run_phase12_benchmark.py`.

**The gap that matters:** the API exposes **generic canonical records** (`GET /v1/records/{record_id}`). It exposes **no case endpoint**, because the generic framework has no case model (2.2). The BPI case data — the only case-shaped output that exists — is reachable **only** by querying `ai_ready_cases` in PostgreSQL directly or running `search_erp_knowledge.py` locally. **For the case data the downstream components actually need, the brief's contingency does hold: there is no service interface.**

## 2.8 Data Flow Trace

### (a) One source record — raw ingestion to stored vector (BPI path, the only path that reaches a case vector)

| # | Hop | file:line | Flags |
|---|---|---|---|
| 1 | CSV read into pandas | `import_bpi_csv_to_old_db.py:25-28` | **HARDCODED** filename→table map |
| 2 | Written to `{name}_raw` in `bpi2020_old_erp_db` | same file | — |
| 3 | `SELECT * FROM "{source_table}"` | `clean_and_load_to_ai_db.py:542` | **HARDCODED** `RAW_TABLES` (`:80`) |
| 4 | Column names normalized | `:93-107` | Can silently collide (2.4) |
| 5 | Text cleaned; `""`/`nan`/`none`/`null`/`na`/`n/a` → `None` | `:110-121` | — |
| 6 | Case-id column detected | `:133-152` | **HARDCODED** 7 candidates; **returns `None` on failure** |
| 7 | Activity column detected | `:155-172` | **HARDCODED** 5 candidates |
| 8 | Timestamps converted | `:207-217` | **HARDCODED** 8 keywords; unparseable → `NaT` → `None` |
| 9 | Amounts converted | `:220-240` | **HARDCODED** 10 keywords; **`€`/`$` stripped and discarded** |
| 10 | `event_record_id` computed | `common/stable_ids.py` | Deterministic — good |
| 11 | UPSERT into `cleaned_event_logs` | `:312-337` | Idempotent |
| 12 | **TERMINATION POINT** — rows whose case id was `None` are excluded | `build_ai_ready_cases.py:621` (`WHERE normalized_case_id IS NOT NULL`) | **Path can end here with no output and no count of what was dropped** |
| 13 | Grouped by `(process_type, normalized_case_id)` | `:643` | **HARDCODED** grouping keys |
| 14 | Events sorted by timestamp then id | `:251-260` | `None` timestamps sort last |
| 15 | `case_summary` built | `:202-241` | Truncates to 10 activities (`:234`) |
| 16 | `content_hash` computed | `:316-328` | Excludes volatile SERIALs — deliberate and correct |
| 17 | Duplicate `case_record_id` check | `:655-661` | **Fails loudly** — raises `RuntimeError`. Good. |
| 18 | JSON/JSONL written | `:343-358` | **HARDCODED** path and filenames |
| 19 | UPSERT into `ai_ready_cases`; `embedding_status='pending'` only when hash changed | `:390-433` | Idempotent and efficient |
| 20 | Obsolete rows pruned | `:486-529` | Explicit and reported |
| 21 | Unified with documents | `build_unified_bpi_knowledge_base.py:413,431` | **HARDCODED** table names |
| 22 | Embedding text built | `generate_and_store_embeddings.py:170-185` | **Amount/currency/employee absent from the text** |
| 23 | Encoded by MiniLM | `:~500` | — |
| 24 | Payload built | `:188-229` | — |
| 25 | `client.upsert` to Qdrant | `:514-517` | Idempotent |
| 26 | Postgres `embedding_status` updated; asserts exactly one row affected | `:314-353` | **Fails loudly** — good |

**Swallowed errors on this path:**

- `safe_json_load` returns `{}` on `json.JSONDecodeError` (`build_ai_ready_cases.py:93-94`) — a corrupt `record_data` blob silently becomes an empty attribute set. **Fails open.**
- `make_json_safe` wraps `pd.isna` in `try/except Exception: pass` (`build_ai_ready_cases.py:123-125`) — narrow, guarding a pandas type quirk. Low risk.
- Same pattern at `build_unified_bpi_knowledge_base.py:107-109`.
- No bare `except:` exists anywhere in `src/` (`grep -rn 'except:' src/` → no matches).

**Early-termination points:** step 12 (no case id), and `build_ai_ready_cases.py:631-639` (zero input rows → logs `failed`, returns cleanly).

**TODO/FIXME/NotImplemented on this path:** none. Repository-wide there are **zero** `TODO`/`FIXME`/`XXX`/`HACK` comments and exactly five `NotImplementedError`s, all abstract-method hooks with concrete subclasses (verified in 2.9).

### (b) A retrieval query, input to results

**Path 1 — generic REST search** (`POST /v1/search`):

| # | Hop | file:line | Flags |
|---|---|---|---|
| 1 | Request validated | `api/schemas.py` (`SearchRequest`) | — |
| 2 | Auth check | `api/main.py:139-163` | Reads unprotected unless `ERP_API_PROTECT_READS` |
| 3 | Guard: embedding + storage present | `routers_data.py:563-566` | **Fails closed** — raises `InvalidPipelineRequestError` |
| 4 | Query encoded — **triggers lazy model load** | `routers_data.py:569` → `runtime/services.py:268-273` | **HARDCODED** model id |
| 5 | Tiered search | `routers_data.py:571` → `storage/hybrid_store.py` | — |
| 6 | Per-hit metadata loaded | `routers_data.py:578` | `getattr(..., None)` defaults — a missing store returns `None` silently rather than erroring |
| 7 | Response assembled with `query_model`, `dimension`, `tiers_searched`, `took_ms` | `:596-610` | Honest: no vectors returned, deep-search cost disclosed (`:603-608`) |

**Path 2 — BPI CLI** (`src/bpi2020/retrieval/search_erp_knowledge.py`, 254 lines): local `argparse` CLI, encodes the query, queries the `bpi2020_erp_knowledge` collection, prints hits. **Not reachable over HTTP by any other component.**

## 2.9 Placeholder & Fabrication Detection

This section found **far less** than a repository of this size would normally yield. Repository-wide scans:

```bash
grep -rn 'TODO\|FIXME\|XXX\|HACK' src/ --include='*.py'   # 0 matches
grep -rn 'except:' src/ --include='*.py'                   # 0 matches
grep -rn 'NotImplementedError' src/ --include='*.py'       # 5 matches
```

| Finding | file:line | What it fabricates | What would make it real |
|---|---|---|---|
| `_LazyEmbeddingService.model_id` returns the literal `"sentence-transformers/all-MiniLM-L6-v2"` when the model is not yet loaded | `runtime/services.py:288-289` | Reports a model identity **without consulting any model or configuration**. If the model were ever changed, `/v1/health/ready` and `/v1/capabilities` would report the wrong one until first use. Currently self-consistent only because the id is hardcoded in two places. | Read the id from configuration in both places, and return the configured value rather than a literal |
| `_LazyEmbeddingService.dimension` returns literal `384` when unloaded | `runtime/services.py:295-296` | Same class of defect for dimension | Same |
| `SnapshotExtractor.extract` raises `NotImplementedError` with **no `# pragma: no cover - overridden` marker**, unlike the four other abstract hooks | `orchestration/extraction.py:99-102` | Nothing — verified as a genuine abstract base with three concrete subclasses (`RelationalSnapshotExtractor:105`, `MongoSnapshotExtractor:174`, `CsvSnapshotExtractor:208`) and callers that always instantiate a subclass (`service.py:191,223`). Reported only because it is stylistically inconsistent with its siblings. | Nothing functionally; add the pragma for consistency |
| `VITE_API_KEY` declared in `frontend/src/vite-env.d.ts:5` and `frontend/.env.example:16`, **never read by any code** | frontend | Presents an API-key capability the client does not have. Setting it produces no header and no error. | Read it in `frontend/src/api/client.ts` and send `X-API-Key` |
| `erp_integrations` cascade classes are fully implemented but composed by **no runtime entry point** | `src/erp_integrations/bpi_case_cascade.py`, `bpi_postgres_cascade.py` (1,148 lines) | Nothing fabricated — but 1,148 lines of production-shaped code are unreachable outside tests. Verified: grep for `bpi_case_cascade` outside `src/erp_integrations/` returns only test files. | A CLI or runtime that composes the cascade with the poller — exactly what `README.md:680` says is missing |
| `safe_json_load` returns `{}` on parse failure | `build_ai_ready_cases.py:93-94` | An empty attribute dict that is indistinguishable from a genuinely empty record | Count and report parse failures |

**Explicitly checked for and NOT found:**

- **No** constants returned where a metric, score, or count is expected. Every metric traced (`mapping/coverage.py:36`, `ai/evaluation.py:87,150`, `transformation/quality.py:154`) computes from real inputs.
- **No** random or time-derived values presented as measurements. `uuid.uuid4()` appears once (`api/main.py:136`) for a request id, with a comment (`:132-135`) explicitly stating domain identity must never be derived from it.
- **No** sample/mock data returned from production functions. `DeterministicTestModel` (`ai/embedding.py:255`) is clearly named and confined to tests.
- **No** functions returning success without performing the operation. The opposite pattern is present: `generate_and_store_embeddings.py:10` documents that every UPDATE asserts exactly one row was affected and raises otherwise.
- **No** empty `pass`/`return None` bodies on production paths.

**The benchmark artifact is unusually honest.** `artifacts/phase12_storage_benchmark.json` carries a `claim_safety` object separating `measured` (cold archive bytes, compression ratio, all latency samples, recall, int8 read-back, rehydration fidelity) from `proxy` (per-tier payload bytes, formula stated) from `estimated` (cost multipliers) from `not_claimed` (monetary savings, production-scale performance, generalization). The cost model states outright that its multipliers are "EXPERIMENTAL ASSUMPTIONS, not prices and not measurements." This is the correct treatment of research evidence and is worth preserving.

## 2.10 Idempotency & Re-runnability

| Stage | Second run on identical input | Evidence |
|---|---|---|
| BPI CSV import | **Destructive by design — drops and recreates each raw table.** `df.to_sql(..., if_exists="replace")`. Idempotent in outcome (same CSV → same table contents, because `source_row_id` is reassigned deterministically from CSV row order) but it discards any manual edits. The script prints an explicit warning listing every table it will replace before doing so | `import_bpi_csv_to_old_db.py:149-156`; rationale and warning at `:168-175` |
| BPI schema creation | **Clean no-op** — `CREATE TABLE IF NOT EXISTS` throughout | `create_ai_native_db_schema.py:56,91,128,146,152` |
| BPI cleaning → `cleaned_event_logs` | **Clean overwrite, no duplicates** — `INSERT ... ON CONFLICT (event_record_id) DO UPDATE`; obsolete rows removed by an explicit reported prune | `clean_and_load_to_ai_db.py:312-337`, prune `:430-460` |
| BPI case build → `ai_ready_cases` | **Clean overwrite, no duplicates, SERIAL stable** — UPSERT on `case_record_id`; `embedding_status` reset to `'pending'` **only when `content_hash` actually changed** (`:428-432`), so unchanged cases keep their vector linkage. Prune at `:486-529`. Duplicate ids within a run raise `RuntimeError` (`:655-661`) | `build_ai_ready_cases.py:390-433` |
| BPI document parse | **Clean overwrite** — same UPSERT + conditional-reset pattern | `parse_bpi_documents.py:325-365` |
| BPI unified build | **Clean overwrite** — regenerates JSON/JSONL files wholesale | `build_unified_bpi_knowledge_base.py:413,431` |
| BPI embedding → Qdrant | **Clean overwrite, no duplicate vectors** — `client.upsert` with deterministic point ids; `--recreate-collection` opt-in, default false | `generate_and_store_embeddings.py:514`; `.env.example:49-51` |
| BPI incremental sync | **No duplicates** — UPSERT on `event_record_id`; checkpoint replay is safe | `realtime_incremental_sync.py:415-440` |
| Generic bootstrap | **Clean no-op** — create-if-missing DDL. **No migration framework**, so it will not evolve an existing table | `runtime/bootstrap.py`; `README.md:685` |
| Generic pipeline job | **No duplicates** — deterministic canonical ids (`schemas/identity.py`) drive upserts into `canonical_records` | `orchestration/record_store.py` |
| Generic incremental sync | **No duplicates** — watermark + deterministic ids | `sync/coordinator.py`; `tests/.../test_watermarks_and_state.py` |
| Generic job store | **Interrupted jobs are recovered, not silently re-run** — a job left `RUNNING` by a dead process is marked `INTERRUPTED` at startup | `api/main.py:78-89` |

**Assessment: idempotency is a designed property of this repository, not an accident.** Every write path examined uses UPSERT on a deterministic key. The one blanket-delete pattern that used to exist was explicitly replaced by scoped, reported prunes — the code says so in place (`build_ai_ready_cases.py:488-492`, `clean_and_load_to_ai_db.py:430`, `parse_bpi_documents.py:315`). This is the strongest area of the codebase.

## 2.11 Error Handling & Failure Modes

| Failure scenario | Current behaviour | file:line | Closed or open? |
|---|---|---|---|
| Pipeline DB unreachable at API startup | `require_valid()` raises `ConfigurationError`; `run()` prints the problem list and returns exit code 2 | `runtime/settings.py:321-328`; `application.py:116-123` | **CLOSED** |
| Pipeline DB misconfigured (missing name/user/password) | Startup refused, every missing variable named at once | `settings.py:277-290` | **CLOSED** |
| Non-loopback bind without an API key | Startup refused unless `ERP_ALLOW_INSECURE_BIND=true` | `settings.py:294-302` | **CLOSED** — the single best safety decision in the codebase |
| Cold tier enabled without an encryption key | Startup refused; archives are never written unencrypted | `settings.py:304-309` | **CLOSED** |
| Source DB unreachable (BPI scripts) | `check_postgres` runs first and prints a diagnostic; SQLAlchemy raises on connect | `bpi2020/common/health.py`; called `build_ai_ready_cases.py:608` | **CLOSED** |
| Required table missing (BPI) | `check_postgres(required_tables=...)` reports before work begins | `build_ai_ready_cases.py:608` | **CLOSED** |
| Malformed row / unparseable timestamp | Coerced to `None` via `errors="coerce"`, row retained | `clean_and_load_to_ai_db.py:212`; `build_ai_ready_cases.py:106` | **OPEN** — no count of coerced values is recorded |
| Malformed `record_data` JSON | Returns `{}`, row retained with empty attributes | `build_ai_ready_cases.py:93-94` | **OPEN — flagged.** Indistinguishable from a genuinely empty record |
| Missing case-id column | `detect_case_id_column` returns `None`; rows silently excluded downstream | `clean_and_load_to_ai_db.py:152`; filtered at `build_ai_ready_cases.py:621` | **OPEN — flagged.** The most consequential open failure: an entire source table can vanish from the output with no error and no count |
| Missing activity column | Returns `None`; `activity_sequence` ends up empty; summary says "unknown starting activity" | `:172`; `build_ai_ready_cases.py:216-217` | **OPEN** — but visible in the output text |
| Encoding error (CSV) | Generic path detects encoding/BOM explicitly; fixtures `utf8_bom.csv`, `malformed.csv` prove handling | `ingestion/detection.py`; `tests/fixtures/ingestion/` | **CLOSED** (generic) |
| Vector DB unreachable | BPI: qdrant-client raises, script aborts. Generic: `/v1/search` guard raises `InvalidPipelineRequestError` when storage is absent | `routers_data.py:563-566` | **CLOSED** |
| Vector dimension mismatch | Refuses to write; names both sizes and the model | `generate_and_store_embeddings.py:298-303` | **CLOSED** |
| Embedding model load failure | Raises typed `EmbeddingModelUnavailableError` naming the model | `ai/embedding.py:136-140`, `ai/errors.py` | **CLOSED** |
| Postgres/Qdrant linkage drift | Every status UPDATE asserts exactly one row affected; zero rows raises | `generate_and_store_embeddings.py:10`, `:314-353` | **CLOSED** |
| Empty result set (no cleaned records) | Logs `failed` to `transformation_logs`, prints remedy, returns | `build_ai_ready_cases.py:631-639` | **CLOSED** |
| Duplicate case ids generated in one run | Raises `RuntimeError`, refuses to write | `build_ai_ready_cases.py:655-661` | **CLOSED** |
| Partial run interruption (generic) | Jobs left `RUNNING` marked `INTERRUPTED` at next startup, never treated as successful | `api/main.py:78-89` | **CLOSED** |
| Stage failure mid-pipeline | Remaining stages marked `SKIPPED`, not run; job status computed from stage outcomes | `orchestration/pipeline.py:129-140`, `:222` | **CLOSED** |
| Interrupted-job recovery itself fails | Logged via `LOGGER.exception`, startup continues | `api/main.py:88-89` | **OPEN by design** — documented in place: startup must not die on recovery |
| Unhandled server exception | Typed handler; 5xx logged with request id; no internal detail leaked | `api/main.py:184-194` | **CLOSED** |
| Schema discovery: unrecognised column type | Warning recorded, discovery continues (observed live: `geometry` type) | `discovery/relational.py:226`, `_safe_inspect:606` | **OPEN by design** — warnings are surfaced via `.warnings` (`:66`) |
| Sync: one bad change record | Quarantined, batch continues | `sync/coordinator.py:487-511` | **OPEN by design**, quarantine is recorded |

**Paths that fail OPEN, ranked by consequence:**

1. **Missing case-id column** (`clean_and_load_to_ai_db.py:152` → `build_ai_ready_cases.py:621`) — silently drops every row of an affected table. No count, no warning, no log entry. On a new ERP whose case column is named something outside the 7 candidates, the pipeline would report success and produce zero cases from that table.
2. **Malformed `record_data`** (`build_ai_ready_cases.py:93`) — silently empties attributes.
3. **Coerced timestamps/amounts** — no instrumentation of how many values failed to parse.

All three are *rejection accounting* failures: the pipeline drops data without counting it. This is also the root cause of the 2.12 gap.

## 2.12 Data Quality & Evaluation Instrumentation

| Metric | Computed? | Where | Logged? |
|---|---|---|---|
| Record counts in/out | **Yes** | `transformation_logs.total_input_records` / `total_output_records`, written at `clean_and_load_to_ai_db.py:479`, `build_ai_ready_cases.py:542,681`, `parse_bpi_documents.py:409`, `generate_and_store_embeddings.py:409`, `realtime_incremental_sync.py:356` | Yes — durable table |
| New / changed / unchanged split | **Yes** | `build_ai_ready_cases.py:437-455`, reported `:673-679` | Yes |
| Obsolete rows pruned | **Yes** | `build_ai_ready_cases.py:486-529` | Yes |
| **Rejection counts and reasons** | **NO** | — | **ABSENT.** Rows dropped for a missing case id, values coerced to `None`, and JSON blobs that failed to parse are never counted. This is the single biggest instrumentation gap. |
| Completeness | **Partially** | `mapping/coverage.py:36-137` computes canonical-field coverage for a mapping profile | Yes, in the validate response |
| Duplicate rates | **Yes (as a hard gate)** | `build_ai_ready_cases.py:695-706` detects duplicate case ids; `create_ai_native_db_schema.py:394-396` counts NULL identity keys | Raises rather than reports a rate |
| Document-to-case linkage rate | **NO** | — | **ABSENT** — no linkage exists to measure (stage 11) |
| Retrieval precision/recall | **Yes** | `ai/evaluation.py:110-159` — `RetrievalReport.top1_accuracy` (`:128`), `top3_hit_rate` (`:134`); `SimilarityReport.separation` (`:63`) | Yes, and persisted to `artifacts/` |
| Mapping accuracy | **Yes** | `tests/erp_pipeline/mapping/test_mapping_benchmark.py` | Printed during the test run |
| Sync latency | **NO** | `sync_state.last_synced_at` records a timestamp but no lag is computed | **ABSENT** |
| Storage/latency benchmark | **Yes** | `scripts/run_phase12_benchmark.py` → `artifacts/phase12_storage_benchmark.json` | Yes |
| Cross-store integrity | **Yes** | `verification/verify_cross_store_integrity.py` (695 lines) — calculated PASS/FAIL across PostgreSQL, files, and Qdrant | Yes |

### Can this component produce quantitative results for a research evaluation chapter?

**Yes. It already has.** Both figures below were produced during this audit, not quoted from documentation.

**Mapping benchmark** — emitted by `pytest` during the full-suite run (`tests/erp_pipeline/mapping/test_mapping_benchmark.py`):

```
labelled mappings       : 68 (60 positive, 8 negative)
top-1 accuracy          : 1.0
top-3 recall            : 1.0
auto-selection precision: 1.0 (60/60)
automatic coverage      : 0.8824
ambiguity rate          : 0.0
unmapped rate           : 0.0882
correct refusal rate    : 1.0
alias-independent top-1 : 1.0 (18/18 labels the alias registry never declared)
```

The `alias-independent` line matters: 18 of the labels were **not** in the alias registry, and the engine still achieved top-1 = 1.0 on them, so the result is not merely the registry echoing itself.

**Storage/retrieval benchmark** — `artifacts/phase12_storage_benchmark.json`, corpus of 500 real MiniLM vectors, 40 queries:

| Metric | HOT | WARM (int8) | COLD (encrypted) |
|---|---|---|---|
| recall@1 | 0.15 | 0.15 | 0.15 |
| recall@3 | 0.475 | 0.475 | 0.475 |
| recall@5 | 0.55 | 0.55 | 0.55 |
| search median | 11.01 ms | 16.45 ms | 15.35 ms (post-rehydration) |
| search p95 | 24.51 ms | 33.20 ms | 34.99 ms |

Plus: hot↔warm top-5 overlap 1.0, cold↔hot top-5 overlap 1.0, cold vector round-trip lossless (max component deviation 0.0), one-time rehydration 9,335.7 ms for 500 records (18.67 ms/record).

**The defensible research claim** is that tiering (int8 quantization, encrypted cold archival with rehydration) costs **no retrieval quality** — recall is identical across all three tiers and top-5 ordering overlap is 1.0 — at a stated latency cost. That is a genuine, publishable result.

**What is missing to complete an evaluation chapter:**

| Needed | Status |
|---|---|
| Rejection/loss accounting (rows dropped, values coerced, parses failed) | **Absent** — no counter exists (2.11) |
| Document-to-case linkage rate | **Absent** — no linkage exists |
| Sync latency (source-commit → vector-visible) | **Absent** — no end-to-end timer |
| Onboarding effort comparison (the Goal A thesis claim) | **Absent** — no second ERP source has been onboarded, so the central claim is unmeasured |
| Transformation coverage on the BPI corpus | **Absent** — coverage is computed for generic mapping profiles, and the BPI path has no mapping profile |
| An absolute recall@1 of 0.15 needs context | The corpus, query construction, and label basis are documented in the artifact, but 0.15 is low enough that it needs a stated baseline to be interpretable |

## 2.13 Test Coverage Reality Check

Commands and results:

```bash
.venv/Scripts/python.exe -m pytest --collect-only -q     # 2593 tests collected in 24.83s
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
# 2567 passed, 26 skipped, 5 warnings in 2107.65s (0:35:07)
```

**The suite runs, and it passes. 2,567 passed, 26 skipped, 0 failed, 35 minutes.**

| Measure | Value |
|---|---|
| Test files | 91 `test_*.py` (121 `.py` in `tests/` including conftests and fixtures) |
| Test functions | 2,135 |
| Collected tests (incl. parametrization) | 2,593 |
| Passed | 2,567 |
| Skipped | 26 |
| Failed | **0** |
| `assert` statements | 4,248 (~2.0 per test function) |
| Test code | 41,174 lines — **81% of the size of the framework it tests** |
| Files using mocks (`unittest.mock`/`MagicMock`/`monkeypatch`) | **15 of 91 (16.5%)** |

**Do the tests mock everything meaningful? No — the opposite.** Only 16.5% of test files use any mocking at all. Evidence that tests exercise real behaviour:

- **Live database tests actually connect.** `test_live_mysql_discovery.py` produced a genuine SQLAlchemy warning from a real MySQL server during the run: `SAWarning: Did not recognize type 'geometry' of column 'location'` at `discovery/relational.py:226`. A mocked inspector cannot emit that.
- **Real file fixtures.** 14 CSV fixtures covering BOM, delimiters, duplicate headers, blank headers, malformed rows, sentinels, and mixed types; 27 OpenAPI/Postman fixtures including malformed, malicious-YAML, recursive-reference, and enum-overflow cases.
- **Real model output.** The storage benchmark embeds a 500-record corpus with the actual MiniLM model (`vectors_are_real_model_output: true` in the artifact).
- **Metric-producing tests.** The mapping benchmark computes accuracy from 68 hand-labelled mappings, including 8 negatives that test *correct refusal* — a test that a constant-returning function would fail.

**Would tests still pass if the function under test returned a constant?** For the substantive suites, no:

- `test_mapping_benchmark.py` — a constant mapper would fail the 8 negative labels (`correct refusal rate: 1.0`) and the 18 alias-independent labels.
- `test_stable_identity.py` — asserts that rebuilding produces the *same* id and that changing content produces a *different* hash. A constant fails the second class of assertion; a random value fails the first.
- `tests/erp_pipeline/mapping/test_canonical_model.py` — machine-checks that every field claiming `REPOSITORY` provenance is genuinely used elsewhere in the repository. This test polices the codebase's own honesty.
- `test_mongodb_privacy.py:169-174` — plants sentinel passwords and asserts they never appear in output. A no-op redactor fails.

**Trivially-passing tests:** a small number of `to_dict`/round-trip serialization assertions are structural rather than behavioural. These are a minority and are reasonable for a contract layer whose serialized shape *is* the contract.

**Can the tests run at all?** Yes, from this working tree, with no additional setup — verified end to end. The 26 skips are live-service tests that gate themselves on availability (e.g. `pytest.skip(f"Catalog PostgreSQL unavailable: {exc}")` at `test_live_mysql_discovery.py:308`), which is correct behaviour rather than a gap. Notably, several live tests did **not** skip, meaning a live MySQL instance was reachable during the run.

**One dependency gap:** `pytest-timeout` is neither installed nor declared, so `--timeout` is unavailable. Given that the full suite takes 35 minutes, a per-test timeout would be a worthwhile addition.

**The critical caveat:** all 41,174 lines of this test suite are **untracked in git**. None of this verification is preserved in version control.
---

# PART 3 — REPOSITORY GOVERNANCE AUDIT

**Framing note that applies to every item below.** `git ls-files | wc -l` returns **21**. Of 293 Python files, **9 are tracked**; 284 are untracked. All 9 tracked files also carry uncommitted modifications. The ~93,000 lines of framework, test, and documentation work described in Parts 1 and 2 **has never been committed**. Most governance requirements below are therefore not merely unmet — they are unmeasurable, because the artefacts they would govern are outside version control.

## 3.1 Commit Granularity & Message Convention — **NON-COMPLIANT**

Commands: `git log --oneline -n 200`, `git log --stat -n 50`, `git shortlog -sne --all`

**Total commits: 13** (`git rev-list --count HEAD`).

**Commits per author:**

| Author | Commits | % |
|---|---|---|
| `DishaniS <dishanialuthwaththa@gmail.com>` | 12 | 92.3% |
| `Dishani Authwaththa <146338103+DishaniS@users.noreply.github.com>` | 1 | 7.7% |

**Files-changed-per-commit distribution:**

| Statistic | Value |
|---|---|
| Minimum | 1 |
| Median | 6 |
| Maximum | 55 |
| Mean | 9.9 |

**Five largest commits:**

| Files | Hash | Message |
|---|---|---|
| 55 | `9624a82` | Restructure repository for BPI 2020 ERP transformation pipeline |
| 10 | `9e43dff` | Add ERP data extraction script and initial CSV outputs |
| 10 | `d1d5f33` | Remove deprecated policy and image documents, along with extraction and transformation scripts |
| 9 | `8ccf373` | Add initial data files and update requirements |
| 8 | `9673974` | Add ERP data cleaning and normalization step |

**Commits touching >20 files:** exactly one — `9624a82` (55 files), a repository restructure. A restructure legitimately touches many files, so this is the least objectionable kind of large commit.

**Messages under 15 characters:** one — `"Initial commit"` (14 characters).

**Messages matching `wip|update|fix|test|asdf|temp|changes|commit`, quoted verbatim:**

- `"Initial commit"` — matches `commit`
- `"Add initial data files and update requirements"` — matches `update`

Both are substantive; neither is a throwaway message. No `wip`, `asdf`, `temp`, or bare `fix` messages exist.

**Conventional Commits usage: 0%.**

```bash
git log --format='%s' | grep -cE '^(feat|fix|docs|refactor|test|chore|style|perf|build|ci)(\(.+\))?:'   # 0
```

Zero of 13 messages use a Conventional Commits prefix.

**Issue references: 0.**

```bash
git log --format='%s%n%b' | grep -cE '#[0-9]+'   # 0
```

**Specific gap.** Message *quality* is actually reasonable — messages average 57 characters, are descriptive, and use imperative mood. The failures are: (a) no Conventional Commits convention, 0%; (b) no issue linkage, 0%; (c) commit *granularity is unmeasurable* because 96.9% of the work was never committed. The real defect is not how the 13 commits are written, it is that there are only 13.

## 3.2 Branching Strategy — **NON-COMPLIANT**

Commands: `git branch -a`, `git log --graph --oneline --all -n 60`

```
* main
  remotes/origin/HEAD -> origin/main
  remotes/origin/main
```

**Every branch:**

| Branch | Conforms to a structured convention? |
|---|---|
| `main` | N/A — the trunk |
| `remotes/origin/main` | N/A |

**No feature, bugfix, hotfix, docs, or release branch has ever existed.** The graph is a single unbroken linear chain of 13 commits with no branch or merge point.

**Which strategy does history match?** None of the named strategies. It is **ad-hoc single-branch development**. It superficially resembles trunk-based development, but trunk-based development requires small, frequent, tested commits to trunk with CI gating — here there are 13 commits total, no CI, and 96.9% of the work uncommitted. Calling it trunk-based would be generous to the point of inaccuracy.

**`develop` branch:** does not exist.

**Stale branches (30+ days):** none, because no branch other than `main` exists.

**Merged-but-undeleted branches:** none.

**Does observed practice match the README's stated strategy?** **The README states no branching strategy at all.** `grep -niE 'branch|merge procedure|pull request' README.md` returns zero matches across all 733 lines. There is no stated strategy to compare against.

## 3.3 Main Branch Stability — **NON-COMPLIANT**

**Branch protection:** cannot be verified from the local repository; no `.github/` directory exists to hold a ruleset, and there is no evidence of protection in any tracked file. **NOT-CONFIGURED** as far as the repository shows.

**`CODEOWNERS`:** absent (`ls CODEOWNERS` → No such file or directory; no `.github/CODEOWNERS` either).

**Commits pushed directly to `main` without a PR or merge commit: all 13.**

```bash
git log --merges --oneline    # (no output — zero merge commits)
```

All 13, with author and date:

| # | Hash | Date | Author | Subject |
|---|---|---|---|---|
| 1 | `d1d5f33` | — | DishaniS | Remove deprecated policy and image documents, along with extraction and transformation scripts |
| 2 | `a1bfacc` | — | DishaniS | Add embedding generation and storage script for BPI 2020 ERP knowledge |
| 3 | `9624a82` | — | DishaniS | Restructure repository for BPI 2020 ERP transformation pipeline |
| 4 | `f64eb7c` | — | DishaniS | Add near-real-time incremental sync and data transformation scripts for BPI 2020 ERP |
| 5 | `5d049e5` | — | DishaniS | Add summary of record counts in unified AI knowledge base creation |
| 6 | `3a5f290` | — | DishaniS | Build unified AI-ready knowledge base |
| 7 | `c4a709a` | — | DishaniS | Add image and policy document parsing scripts for OCR and PDF extraction |
| 8 | `a1d814e` | — | DishaniS | Add policy document parsing and AI-ready conversion |
| 9 | `0d44932` | — | DishaniS | Add ERP-aware transformation for AI-ready records |
| 10 | `9673974` | — | DishaniS | Add ERP data cleaning and normalization step |
| 11 | `9e43dff` | — | DishaniS | Add ERP data extraction script and initial CSV outputs |
| 12 | `8ccf373` | — | DishaniS | Add initial data files and update requirements |
| 13 | `da2aacb` | — | Dishani Authwaththa | Initial commit |

(Fewer than 20 exist, so all are listed.)

**Does `main` currently run?**

- **Conflict markers:** none. `grep -rn '<<<<<<<\|>>>>>>>' src/ tests/` returns no matches.
- **Syntax and imports:** the working tree is sound — the full test suite collected 2,593 tests and passed 2,567 with 0 failures. Import errors or syntax errors would have surfaced at collection.
- **However, `main` as committed is not the code that was tested.** The tested code is the working tree, of which 96.9% is untracked. A fresh `git clone` would yield 21 files: 9 BPI scripts, a README describing a system that is not present, and no tests. **`git clone` + `pytest` on committed `main` would collect zero tests, and the 9 BPI scripts would fail on `from bpi2020.common.config import PostgresSettings` because `src/bpi2020/common/` is untracked.** Verified: `src/bpi2020/common/` appears as `??` in `git status`, while `build_ai_ready_cases.py:53` imports from it.

**In other words: committed `main` is broken.** The tracked BPI scripts import a package that was never committed.

**CI gating merges:** none exists (3.7).

## 3.4 Pull Request Process — **NOT-CONFIGURED**

| Item | Finding |
|---|---|
| `.github/pull_request_template.md` | **Absent** — no `.github/` directory exists at all |
| Merge commits referencing a PR | **0** — `git log --merges --oneline` produces no output |
| Ratio of PR merges to direct pushes on `main` | **0 : 13** |
| Review/approval evidence in the repository | **None.** No `Reviewed-by:`, `Approved-by:`, or `Co-authored-by:` trailer appears in any commit body |

**Specific gap:** no pull-request process exists in any form. Every change went straight to `main` unreviewed.

## 3.5 Documentation Requirements — **PARTIAL**

The README is 733 lines and is, on technical content, unusually thorough and unusually honest. On *governance* content it is entirely silent.

| Required section | Present? | Line | Note |
|---|---|---|---|
| Project overview | **Yes** | `README.md:5` (`## Overview`), plus `:21` (`## Problem the Project Solves`) | Substantive |
| Repository structure | **Yes** | `:128` (`## Repository Structure`) | Annotated tree; **contains one false claim** (below) |
| Setup and installation | **Yes** | `:476` (`## Installation`), `:464` (`## Prerequisites`) | Verified below — **would fail** |
| Branching strategy | **ABSENT** | — | No mention anywhere in 733 lines |
| Branch naming rules | **ABSENT** | — | — |
| Commit message convention | **ABSENT** | — | — |
| Merge procedure | **ABSENT** | — | — |
| **Dated change log** | **ABSENT** | — | See below |
| Environment variable documentation | **Yes** | `:433-462` | **Excellent** — a 30-row table covering every variable, with purpose and required/optional status. Cross-checked against 1.5: complete and accurate. |
| Team ownership map | **ABSENT** | — | No owner is named for any component |

### Would following the installation instructions literally work?

Steps at `README.md:480-496`, each verified against the filesystem:

| Step | Command | Works? |
|---|---|---|
| 1 | `py -3.11 -m venv .venv` | Yes |
| 2 | `.\.venv\Scripts\Activate.ps1` | Yes |
| 3 | `python -m pip install -r requirements.txt` | Yes — file exists and is tracked |
| 4 | `python -m pip install -e .` | **From this working tree yes; from a clean clone NO** — `pyproject.toml` is untracked |
| 5 | `Copy-Item .env.example .env` | Yes — tracked |
| 6 | `Set-Location frontend; npm ci` | **NO — fails.** `npm ci` requires `package.json` and `package-lock.json`; both are **gitignored** by the global `*.json` rule at `.gitignore:10`. Verified: `git check-ignore -q frontend/package.json` → exit 0 (ignored). |
| 7 | `Copy-Item .env.example .env` (frontend) | **NO** — `frontend/.env.example` is untracked |

**The first step that fails from a clean clone is step 4** (`pip install -e .`, no `pyproject.toml`), and the first step that fails *even in this working tree* is step 6 for anyone who re-clones.

**One false claim in the structure section.** `README.md:150` states: *"The small evidence files under `artifacts/` are tracked."* They are not — `git status` reports `?? artifacts/`. Both `phase12_storage_benchmark.json` and `phase13_openapi.json` are untracked. The `.gitignore` negation rules at `:283-284` (`!artifacts/`, `!artifacts/*.json`) correctly *un-ignore* them (verified: `git check-ignore -q artifacts/phase13_openapi.json` → exit 1, not ignored), so the intent was right — but the files were never `git add`ed. Intent without the commit.

### Dated change log

**ABSENT.** There is no dated change log in the README, and no `CHANGELOG.md` exists at the repository root.

- Entry count: **0**
- Most recent entry date: **N/A**
- Cross-check of 5 entries against `git log` merges: **not possible** — there are no entries, and there are no merges to cross-check against (`git log --merges` is empty).

## 3.6 Secrets & Ignore Hygiene — **PARTIAL (security: clean; usability: broken)**

### Does `.gitignore` exist, and what does it cover?

Yes — 300 lines, tracked. It is a standard Python `.gitignore` extended with three deliberate, well-commented project sections:

- **Broad data exclusions** (`:10-13`): `*.json`, `*.jsonl`, `*.csv` — stated purpose is keeping company datasets and DB exports out of the repository.
- **Narrow negations** (`:275-292`): re-includes `artifacts/*.json` and `tests/fixtures/**/*.{csv,json,yaml,yml}`, with the comment *"Without these a fresh clone cannot run the suite."* Verified working: `git check-ignore -q tests/fixtures/api_specs/openapi_3_basic.json` → not ignored.
- **Runtime exclusions** (`:295-300`): `var/`, `uploads/`, `cold-archive/`, `*.erpcold`, `data/raw/`, `data/bpi2020/`.

### Secret files ever committed

```bash
git log --all --full-history --name-only | grep -iE '\.env$|\.pem$|\.key$|credentials*|secret*|id_rsa|\.sqlite$|\.db$'
```

**No output. No secret file has ever been committed, including in deleted history.** `.env` is correctly ignored (`.gitignore:233`) and has never been tracked (`git ls-files --error-unmatch .env` → `did not match any file(s) known to git`).

### Credential patterns in tracked files

```bash
git ls-files -z | xargs -0 grep -nIiE 'password *= *["\x27][^"\x27]{3,}|api_key *= *...|Bearer [A-Za-z0-9]|postgres(ql)?://[^:]+:[^@]+@'
```

**No output — zero hits across all 21 tracked files.**

Scanning the whole working tree (including untracked code) returns ~20 hits, **all of which are test sentinels whose purpose is to prove redaction works**, for example:

- `tests/erp_pipeline/api/conftest.py:24-25` — `SECRET_DB_PASSWORD = "SECRET_DB_PASSWORD_13981"`, `SECRET_API_KEY = "SECRET_API_KEY_88221"`
- `tests/erp_pipeline/discovery/test_mongodb_privacy.py:169` — `password = "SENTINEL-PASSWORD-9f3a"`, planted specifically to assert it never appears in output
- `tests/erp_pipeline/connectors/test_scope_and_security.py:246` — `("postgresql://admin:hunter2@localhost:5432/db", "hunter2")`, a redaction test case

**No real credential appears anywhere in the repository.** The source code is also disciplined about this at runtime: `ApiSettings.__repr__` (`api/config.py:83-91`), `DatabaseSettings.__repr__` (`runtime/settings.py:123-128`), `QdrantSettings.__repr__` (`:183-190`), and `describe()` (`:114-121`) all redact. `RecordProvenance.metadata` is validated against a credential-key denylist (`canonical_models.py:99-101, 132`). Registered sources store a `credential_ref` name, never a password (`.env.example:148-152`).

### Tracked files over 5 MB

**None.** Largest blobs in all history:

| Size | Path |
|---|---|
| 2.4 MB | `data/bpi2020/images/travel_receipt_001.png` |
| 2.4 MB | `data/bpi2020/images/approval_form_scan_001.png` |
| 2.0 MB | `data/bpi2020/images/invoice_travel_claim_001.png` |
| 1.3 MB | `data/raw/product.csv` (deleted, still in history) |
| 1.3 MB | `data/processed/product_extracted.csv` (deleted, still in history) |
| 1.3 MB | `data/cleaned/product_cleaned.csv` (deleted, still in history) |

All under the 5 MB threshold. The ~2.8 GB of generated BPI JSON in the working tree (including a 1,060 MB `bpi2020_ai_ready_cases.json` and a 1,084 MB unified file) is **correctly excluded** by `.gitignore:300`. This is the right call and was handled well.

### Does `.env.example` cover every variable from 1.5?

**Yes — complete.** Every variable read by code appears, and `ERP_SECRET_<REF>` has its pattern documented at `:148-152`. The file is 161 lines, extensively commented, and explains the canonical/legacy alias policy at `:5-8`.

### The one real hygiene defect

**`.gitignore:10` (`*.json`) is too broad and silently excludes files the project needs.** Verified:

| File | Ignored? |
|---|---|
| `frontend/package.json` | **YES — ignored** |
| `frontend/package-lock.json` | **YES — ignored** |
| `frontend/tsconfig.json` | **YES — ignored** |

The negation list at `:283-292` re-includes `artifacts/` and `tests/fixtures/` but **was never extended to `frontend/`**. The consequence is concrete: `README.md:493` instructs the reader to run `npm ci`, and `README.md:684` claims *"The frontend does have `package-lock.json`"* — but neither file can ever reach the repository. **The frontend is uninstallable from a clean clone.**

## 3.7 CI/CD & Automated Quality Gates — **NOT-CONFIGURED**

```bash
ls .github            # No such file or directory
find . -maxdepth 3 \( -name '*.yml' -o -name '*.yaml' \) -not -path '*/node_modules/*' -not -path '*/.venv/*'
# (only tests/fixtures/api_specs/*.yaml — test fixtures, not workflows)
```

| Item | Finding |
|---|---|
| `.github/workflows/` | **Does not exist** |
| Any CI configuration (GitLab, Jenkins, CircleCI, Azure) | **None found** |
| Build runs automatically on PR | **No** |
| Lint runs automatically on PR | **No** — and no linter is configured at all (no `.flake8`, `.ruff.toml`, `setup.cfg`, or `[tool.ruff]`/`[tool.black]` in `pyproject.toml`, despite `# noqa: BLE001` and `# noqa: SLF001` comments in source implying a linter was once used) |
| Test runs automatically on PR | **No** |
| Any check *required* before merge | **No** |
| Dockerfile / compose | **None** — confirms README's own status line `Docker/CI/deployment automation — 🔴 Not implemented` (`README.md:671`) |

**Statement required by the brief:** with no CI of any kind, **"main is always deployable" is currently unenforceable by automation.** Nothing prevents a commit that fails to import, fails its tests, or exposes a secret from landing on `main`. The 35-minute local test suite is strong evidence of quality, but it runs only when a human chooses to run it, and its results are not recorded anywhere in the repository.

## 3.8 Contribution Traceability — **PARTIAL**

```bash
git shortlog -sne --all
    12  DishaniS <dishanialuthwaththa@gmail.com>
     1  Dishani Authwaththa <146338103+DishaniS@users.noreply.github.com>
```

| Author identity | Commits | % |
|---|---|---|
| `DishaniS <dishanialuthwaththa@gmail.com>` | 12 | 92.3% |
| `Dishani Authwaththa <146338103+DishaniS@users.noreply.github.com>` | 1 | 7.7% |

**Inconsistent name/email for the same person: YES, confirmed.** Both identities are the same individual — the second is the GitHub `noreply` address for the account `DishaniS`, used for the `Initial commit` (`da2aacb`), which is the signature of a repository created through the GitHub web UI. The display names differ (`DishaniS` vs `Dishani Authwaththa`). This is minor and easily fixed with a `.mailmap`.

**Evidence of one person committing another's work:** **none found.** All 13 commits are BPI-2020 work by a single author. No commit adds files belonging to another team component, so the "large commit adding another component's files without a `Co-authored-by:` trailer" pattern does not arise. No `Co-authored-by:` trailer appears in any commit — correctly, since there is no co-authored work.

**Does each team component have commits from its stated owner?** **Unanswerable, and that is itself the finding.** This repository contains one component of a stated four-component system. No `CODEOWNERS`, ownership table, or team map exists in the repository (3.5), so no component-to-owner mapping is recorded anywhere. The other three components are not present in this repository (3.10).

**The dominant traceability failure is not attribution — it is absence.** 96.9% of the Python work in this working tree has no commit, and therefore no author, no date, and no traceable history. Whoever wrote the 50,774-line `erp_pipeline` framework has no recorded contribution for it.

## 3.9 Versioning & Milestones — **NON-COMPLIANT**

```bash
git tag -l    # (no output)
```

| Item | Finding |
|---|---|
| Tags | **Zero.** No tag has ever been created |
| Semantic versioning in git | **Not used** — no tags to version |
| Semantic versioning elsewhere | **Yes, in file content only** — `pyproject.toml:6` declares `version = "0.13.0"`, and `src/erp_pipeline/version.py` defines `CANONICAL_MODEL_VERSION` (imported at `canonical_models.py:52`). The `0.13.0` value tracks the project's 13 internal "phases" |
| Tag marking a submission or demo milestone | **None** |
| `CHANGELOG.md` | **Absent** |

**Specific gap:** the project has a meaningful internal version (`0.13.0`) and a versioned canonical model contract, but **neither is anchored to a commit**. There is no way to check out "the state at Phase 12" or "the submission version". `pyproject.toml` itself is untracked, so even the `0.13.0` string is not in version control.

## 3.10 Repository Structure Decision — **Single-component repository**

**This is a single-component repository**, not a monorepo. It contains one of the four components of the stated system: the ERP-Aware Data Transformation Pipeline (`README.md:3-19`). The governance, orchestration, and bridge components are **not present**.

The internal split between `src/bpi2020`, `src/erp_pipeline`, and `src/erp_integrations` is a *layering* boundary within one component, not four team components. That boundary is real and enforced:

- **`erp_pipeline` never imports `bpi2020`.** Stated as a rule at `README.md:720` and verified by grep — no such import exists.
- `erp_integrations` is the designated one-way adapter between them (`README.md:141`).
- `src/erp_pipeline/mapping/canonical_model.py:133-138` documents refusing to let generic code inherit the BPI vector collection by accident — evidence the boundary is actively defended, not merely declared.

**How are integration contracts with the other three components stored or referenced?**

| Contract mechanism | Present? |
|---|---|
| Generated REST contract | **Yes** — `artifacts/phase13_openapi.json`, 20 paths. This is a real, machine-readable integration contract and the strongest integration asset the component has. It is **untracked**. |
| Canonical model contract | **Yes** — `src/erp_pipeline/schemas/canonical_models.py` with an explicit `CANONICAL_MODEL_VERSION`, plus `docs/canonical_erp_model.md`. Both **untracked**. |
| Contract for the *case* model the other components need | **ABSENT** — no case schema is defined anywhere (2.2) |
| Shared schema package, submodule, or published artifact | **None** |
| Reference to the other three components' repositories | **None** — the other components are never named, linked, or versioned anywhere in the repository |

**Specific gap:** the component publishes a versioned OpenAPI contract, which is the right mechanism, but (a) it is not committed, (b) it is not published anywhere the other three teams can consume it, and (c) it does not expose the case-shaped data those teams actually need.
---

# PART 4 — INTENT VS REALITY

## 4.1 Claims Inventory

The README is the dominant claim source. It is unusually accurate: it contains its own 15-item "Known Issues and Technical Debt" section (`README.md:673-689`) and a status table that already marks several areas 🟡 or 🔴. Most claims below verify as stated. The exceptions are listed and are consequential.

| # | Claim | Source (file:line) | How verified | Status | Evidence |
|---|---|---|---|---|---|
| 1 | "Connectors for PostgreSQL, MySQL, SQL Server, and MongoDB" | `README.md:41` | Read `connectors/` | **IMPLEMENTED** | `connectors/{postgresql,mysql,sqlserver,mongodb}.py` all present, 11 files / 1,617 lines |
| 2 | SQL Server "not live-verified" | `README.md:661,681` | Read status table | **IMPLEMENTED (as an honest caveat)** | Backed by `ERP_SQL_SERVER_LIVE_VERIFIED` defaulting false (`api/config.py:47`) so `/v1/capabilities` reports the truth |
| 3 | "Declared relational schema discovery and bounded MongoDB observed-schema inference" | `README.md:42` | Read + ran tests | **IMPLEMENTED** | `discovery/relational.py:74`; `discovery/mongodb_inference.py`; live MySQL discovery tests passed during the run |
| 4 | "CSV ingestion with streaming, delimiter/encoding handling" | `README.md:43` | Read + fixtures | **IMPLEMENTED** | `ingestion/csv_ingestion.py`, `detection.py`; 14 CSV fixtures incl. BOM, tab, pipe, semicolon, malformed |
| 5 | "PDF and image extraction with page provenance and optional Tesseract OCR" | `README.md:44` | Read | **IMPLEMENTED** | `ingestion/{pdf,image}_ingestion.py`, `ocr.py`; `RecordProvenance.page_number` (`canonical_models.py:108`) |
| 6 | "OpenAPI 3.x, Swagger 2.0, and Postman parsing **without calling documented endpoints**" | `README.md:45`, `api/main.py:62-64` | Read + fixtures | **IMPLEMENTED** | `api_specs/` 11 files / 5,283 lines; 27 fixtures incl. `malicious_yaml.yaml`; no HTTP client in the parse path |
| 7 | "Explainable source-to-canonical mapping with confidence, ambiguity, and collision reporting" | `README.md:46` | Ran benchmark | **IMPLEMENTED** | Measured during this audit: top-1 = 1.0, ambiguity rate = 0.0, correct refusal = 1.0 over 68 labels |
| 8 | "Incremental synchronization with watermarks, drift detection, affected-record propagation, durable state" | `README.md:48` | Read | **IMPLEMENTED (PostgreSQL only)** | `sync/` 11 files / 4,387 lines; `coordinator.py:125`, `drift.py:357`, `state.py`. Driver limit acknowledged at `README.md:675` |
| 9 | "Local `all-MiniLM-L6-v2` embeddings; **no remote AI or LLM fallback**" | `README.md:49` | Grep for LLM/API clients | **IMPLEMENTED** | No OpenAI/Anthropic/HTTP-inference client anywhere; `api/main.py:64` repeats the guarantee; `/v1/search` returns records, not prose (`routers_data.py:557-559`) |
| 10 | "Hot/warm Qdrant storage plus gzip/AES-256-GCM encrypted cold archives" | `README.md:50` | Read + artifact | **IMPLEMENTED** | `storage/` 16 files / 5,762 lines; benchmark confirms lossless encrypted round-trip (max deviation 0.0) |
| 11 | "FastAPI orchestration, durable jobs, idempotency keys, readiness checks, structured errors" | `README.md:51` | Read + OpenAPI artifact | **IMPLEMENTED** | 20 endpoints; `PostgresJobStore` (`application.py:61`); `Idempotency-Key` header (`api/main.py:124`); typed error handlers (`main.py:175-218`) |
| 12 | "A deliberately small React frontend for CSV and document uploads" | `README.md:52` | Read | **IMPLEMENTED** | `frontend/src/pages/Upload.tsx`, single screen, as described |
| 13 | "Every cross-layer business record uses deterministic identity and content hashing. PostgreSQL sequence values and request/job UUIDs are not used as business identifiers." | `README.md:37` | Read + tests | **IMPLEMENTED** | `schemas/identity.py`; `bpi2020/common/stable_ids.py`; `api/main.py:132-135` states the rule in place; `test_stable_identity.py::test_legacy_serial_record_id_is_rejected` enforces it |
| 14 | "The deployable runtime currently completes database extraction and incremental jobs only for PostgreSQL" | `README.md:9`, `:675` | Read | **IMPLEMENTED (accurate self-report)** | `orchestration/service.py:202,404` hardcode `postgresql+psycopg2` exactly as claimed |
| 15 | "`erp_pipeline` never imports `bpi2020`" | `README.md:720` | Grep | **IMPLEMENTED** | `grep -rn 'from bpi2020\|import bpi2020' src/erp_pipeline/` → no matches |
| 16 | "It has no user accounts, role system, Docker deployment, or CI configuration" | `README.md:19` | Filesystem | **IMPLEMENTED (accurate)** | No `.github/`, no Dockerfile; auth is a single shared key (`api/security.py`) |
| 17 | Bootstrap CLI gap — `erp-bootstrap` does not create `registered_sources`, `uploads`, `mapping_drafts` | `README.md:676` | Read | **IMPLEMENTED (accurate self-report)** | `application.py:41-46` calls `bootstrap_all` **and** `bootstrap_runtime_persistence`; the CLI path calls only the former |
| 18 | Frontend cannot use protected routes; `VITE_API_KEY` never read | `README.md:678` | Grep | **IMPLEMENTED (accurate self-report)** | `VITE_API_KEY` appears only in `frontend/src/vite-env.d.ts:5` and `frontend/.env.example:16`; never in `client.ts` |
| 19 | "`GET /v1/schemas/{schema_id}` accepts a `version` query parameter but ... without using that parameter" | `README.md:687` | Read | **IMPLEMENTED (accurate self-report)** | `routers_data.py:223` |
| 20 | "Most requirements are unpinned ... The frontend does have `package-lock.json`" | `README.md:684` | Read + `git check-ignore` | **PARTIAL — second half misleading** | 3/18 pinned is correct. `package-lock.json` exists on disk but is **gitignored** (`.gitignore:10`), so it cannot reach a clone |
| 21 | **"The small evidence files under `artifacts/` are tracked."** | `README.md:150` | `git status` | **FALSE** | `git status` → `?? artifacts/`. Both artifact files are untracked. The `.gitignore` negations (`:283-284`) correctly un-ignore them, but they were never `git add`ed |
| 22 | Installation: "Install the frontend from its lockfile: `npm ci`" | `README.md:491-493` | `git check-ignore` | **FALSE from a clean clone** | `git check-ignore -q frontend/package.json` → exit 0. `npm ci` cannot run |
| 23 | Status table: "BPI import, cleaning, case/document building, unified output — ✅ Implemented" | `README.md:654` | Read | **IMPLEMENTED** | All scripts present and coherent |
| 24 | Status table: "Generic contracts and deterministic identity — ✅ Implemented" | `README.md:657` | Ran tests | **IMPLEMENTED** | `schemas/` 10 files / 3,309 lines + passing identity tests |
| 25 | Status table: "Orchestration and REST API — ✅ Implemented" | `README.md:668` | Read + artifact | **IMPLEMENTED** | 20 endpoints, durable jobs, retries, interrupted-job recovery |
| 26 | Implicit throughout: this component produces an "AI-ready knowledge layer" the other three components consume | `README.md:5-9`, project brief | Read canonical model | **PARTIAL — the central unstated gap** | The generic framework's canonical model is a *record/document* model (`canonical_models.py:210`) with an `invoice`/`customer`/`purchase_order` vocabulary (`mapping/canonical_model.py:505-547`). **No case model, no `current_state`, no `allowed_next_states`, no `entities`, no `freshness`** (2.2). The README never claims these exist — but it also never flags their absence, and they are what the downstream components need |

**Summary: of 26 claims, 22 verify as IMPLEMENTED, 2 are FALSE (#21, #22), 2 are PARTIAL (#20, #26).** The two false claims are both about *what is in git*, not about what the code does — which is precisely consistent with the repository's central defect.

## 4.2 Stated Intent Not Yet Built

| Evidence of intent | Where | What it implies was intended | What remains |
|---|---|---|---|
| `erp_integrations/bpi_case_cascade.py`, `bpi_postgres_cascade.py` — 1,148 lines of complete, tested cascade classes composed by **no runtime entry point** | `src/erp_integrations/`; absence confirmed by grep outside the package | A production path where a BPI sync automatically rebuilds cases → unified output → vectors | A CLI or runtime that composes the cascade with the poller. Stated as remaining at `README.md:680` |
| `VITE_API_KEY` declared in types and `.env.example`, read nowhere | `frontend/src/vite-env.d.ts:5`; `frontend/.env.example:16` | The frontend was meant to authenticate against protected routes | Read the variable in `frontend/src/api/client.ts` and send `X-API-Key` |
| `version` query parameter accepted and ignored | `api/routers_data.py:223` | Schema retrieval was meant to be version-aware | Wire the parameter into the catalog lookup |
| `CanonicalTargetModel.from_dict` exists and is documented as the configurability mechanism, but **nothing in the repository ever calls it with an external file** | `mapping/canonical_model.py:295-341` | Loading a per-organisation canonical vocabulary from JSON/YAML | A config-loading path and an admin surface to supply it. Currently only `DEFAULT_CANONICAL_MODEL` is used |
| `SnapshotExtractor` has Relational, Mongo, and CSV subclasses, but `orchestration/service.py:202,404` can only build a PostgreSQL engine | `orchestration/extraction.py:105,174,208` vs `service.py` | All three extractors were meant to be reachable | Select the driver from the registered source's `source_type`. `README.md:722` lists this as remaining work |
| `ERP_SQL_SERVER_LIVE_VERIFIED` flag exists, defaults false | `api/config.py:47,73` | SQL Server support was meant to be live-verified before being advertised | A live SQL Server verification run |
| `# noqa: BLE001`, `# noqa: SLF001` comments in source with **no linter configured** | `api/main.py:88`; `tests/.../test_mongodb_privacy.py:169` | A linter (ruff or flake8) was once in use or intended | Add the linter config and wire it into CI |
| `pyproject.toml` declares `[project.scripts]` but is untracked | `pyproject.toml:16-19` | The package was meant to be pip-installable by others | Commit `pyproject.toml` |
| `.gitignore` negation block re-including `artifacts/` and `tests/fixtures/`, with a comment explaining a fresh clone needs them | `.gitignore:275-292` | The author intended the artifacts and fixtures to be committed | `git add` them — the ignore rules already permit it |
| 13 `docs/*.md` phase design documents, none referenced from any code | `docs/` | A documented phase-by-phase design record | They exist and are substantive; they are simply untracked |
| **No commented-out code blocks, no empty descriptively-named functions, no unused-import clusters** were found | repository-wide | — | The usual "abandoned intent" signals are **absent**. This codebase does not carry dead scaffolding |

**Unmerged branches:** none exist (3.2), so no intent is parked in a branch.

**Referenced-but-absent files: none.** Every file named in the README's "Important Files" table (`README.md:693-713`) was checked and exists, including `src/erp_pipeline/storage/storage_policy.py`, `orchestration/planner.py`, `catalog/schema.py`, `bpi2020/common/stable_ids.py`, and both `artifacts/` files.

## 4.3 Undocumented Behaviour

Behaviour the code performs that the README does not mention. Several are documented *in the source* but not in user-facing documentation.

| Behaviour | file:line | Documented in README? |
|---|---|---|
| **`import_bpi_csv_to_old_db.py` DROPS AND RECREATES four source tables** via `to_sql(if_exists="replace")` | `:149-156` | **No.** The README lists it as step 1 of the BPI pipeline (`:558`) with no warning. The *script itself* is honest — it prints a warning naming every table it will replace (`:172-175`) and explains the rationale in a comment (`:168-171`) — but a reader following the README would not know |
| **`build_ai_ready_cases.py` DELETEs rows** from `ai_ready_cases` whose case no longer exists in the source (default behaviour; `--keep-obsolete` disables it) | `:486-529`, `:667-671` | **No.** Explained thoroughly in the docstring (`:30-31`) and printed at runtime, but absent from the README |
| **`clean_and_load_to_ai_db.py` DELETEs obsolete rows** from `cleaned_event_logs` by the same pattern | `:430-460` | **No** |
| **Startup creates database schemas by default** (`ERP_BOOTSTRAP_ON_STARTUP` defaults `true`) — the API executes DDL against the configured PostgreSQL on first start | `runtime/settings.py:239`; `application.py:41-54` | **Partially** — the variable is documented (`README.md:456`) and `:545` advises keeping it true; the DDL side effect is implied but never stated plainly |
| **Rows with no detectable case id are silently dropped**, with no count | `clean_and_load_to_ai_db.py:152` → `build_ai_ready_cases.py:621` | **No.** This is the most consequential undocumented behaviour — silent data loss (2.11) |
| **Currency symbols are stripped and discarded**, losing which currency a value was denominated in | `clean_and_load_to_ai_db.py:228-230` | **No** |
| **Network access on first embedding use** — a partial MiniLM cache can trigger a Hugging Face download | `ai/embedding.py:136` | **Yes** — `README.md:472,682` both flag it. Not a gap |
| **The API writes uploaded files to disk** at `ERP_API_UPLOAD_DIR` (default `var/uploads`) | `api/config.py:72`; `orchestration/upload_store.py` | **Partially** — the variable is documented; the write side effect is implied |
| **The cold tier writes encrypted archive files** to `ERP_COLD_ARCHIVE_DIR` (default `var/cold-archive`) | `runtime/settings.py:203-211` | **Yes** — documented, including the warning that losing the key makes archives permanently unreadable (`.env.example:142-146`) |
| **Outbound connections**: PostgreSQL, Qdrant (incl. Qdrant Cloud over TLS), MongoDB, MySQL, SQL Server, Hugging Face | throughout | **Yes** — `README.md:610` (`## External Services`) |
| **`_LazyEmbeddingService` reports a model id and dimension without loading the model** | `runtime/services.py:288-296` | **No** — see 2.9 |
| **Deprecation warnings are printed to stdout** when a legacy environment variable name is used | `bpi2020/common/config.py:79-82,125-129` | **Partially** — the alias policy is documented (`README.md:435`; `.env.example:5-8`); that it prints warnings is not |
| **A temporary Qdrant collection is created and populated during cold-tier search** (`include_cold=true`), costing ~9.3 s of one-time rehydration | `artifacts/phase12_storage_benchmark.json` (`total_cold_access_latency`) | **Yes, and well** — the API response itself carries `deep_search_note` explaining the cost (`routers_data.py:603-608`) |

**Assessment.** The undocumented behaviours cluster into two groups. The **destructive-write group** (table replace, two prune-deletes, startup DDL) is documented in source comments and runtime warnings but not in the README — a real documentation gap, though not a hidden behaviour in the deceptive sense. The **silent-data-loss group** (dropped rows, discarded currency, uncounted coercions) is genuinely undocumented anywhere and is the more serious of the two.
---

# PART 5 — SYNTHESIS

## 5.1 Completion Assessment

Denominator: the 19 pipeline stages from 2.1. A stage counts as IMPLEMENTED if it exists, is reachable from a real entry point, and does what it claims — in **either** codebase.

| Status | Count | Stages |
|---|---|---|
| **IMPLEMENTED** | **12** | 1 source ingestion · 2 schema discovery · 3 cleaning · 4 normalization · 5 field mapping · 10 document extraction · 12 embedding generation · 13 vector store upsert · 14 incremental sync · 17 transformation logging · 18 data quality metrics · 19 query/service API |
| **PARTIAL** | **4** | 7 case construction (BPI-only, dataset-specific) · 8 timeline construction (BPI-only) · 15 freshness tracking (per-table only, no `is_stale`) · 16 data lineage (thorough in generic, three literals in BPI) |
| **MISSING** | **3** | 6 state-code translation · 9 allowed-next-state derivation · 11 document-to-case linking |

**Arithmetic:**

```
12 IMPLEMENTED + 4 PARTIAL + 3 MISSING = 19 stages

Strict (IMPLEMENTED only):            12 / 19 = 63.2%
Weighted (PARTIAL counts as 0.5):  (12 + 2) / 19 = 73.7%
```

**Overall completion: 63.2% strict, 73.7% weighted.**

Two qualifications that pull in opposite directions and should be stated rather than averaged away:

- **Upward:** the implemented stages are implemented *well*. 2,567 tests pass, idempotency is a designed property throughout, error handling fails closed on every configuration and infrastructure fault, and the codebase contains zero TODO/FIXME markers and zero bare excepts. This is not 63% of a prototype; it is 63% of a carefully engineered system.
- **Downward:** the 3 MISSING stages are not peripheral. State-code translation, allowed-next-state derivation, and document-to-case linking are precisely the stages that produce the *safety* properties the four-component system exists to provide. A pipeline that cannot say what state a case is in, what transitions are legal, or which document evidences a decision cannot support an AI orchestrator operating an ERP safely — which is the stated purpose.

**Split by codebase, because the single figure conceals a real asymmetry:**

| Codebase | Implemented stages | Note |
|---|---|---|
| `erp_pipeline` (generic) | 12 of 19 | Missing all 4 case-oriented stages (7, 8, 9, 11) plus 6 and 15 |
| `bpi2020` (prototype) | 9 of 19 | Has case + timeline; missing mapping (5), and all of 6, 9, 11, 15, 19 |

**Neither codebase alone completes the pipeline, and they do not compose.** The generic framework has the configurability but no case model; the BPI prototype has the case model but no configurability. `erp_integrations` was built to bridge them and is wired to nothing (2.9).

## 5.2 Configuration-Driven Readiness Score

**How many HIGH-severity hardcoded values exist?** **15** (2.3). Distribution: 13 in `src/bpi2020`, 2 in `src/erp_pipeline` (`orchestration/service.py:202,404` driver lock; `ai/embedding.py:42` + `runtime/services.py:273` model lock).

**How many source files would need editing to onboard a second, differently-shaped ERP source?**

| Path | Files to edit | Which |
|---|---|---|
| Generic framework, PostgreSQL source | **0** | Onboarding is entirely runtime: `POST /v1/sources` → `/discover` → `/mappings/suggest` → `PUT /mappings/{id}` → `POST /v1/jobs` |
| Generic framework, MySQL / SQL Server / MongoDB source | **1** | `src/erp_pipeline/orchestration/service.py` (two call sites: `:202`, `:404`) |
| Generic framework + a different embedding model | **2** | above, plus `src/erp_pipeline/runtime/services.py:273` |
| Generic framework + case-shaped output | **new module required** | No case construction exists in `erp_pipeline` at all — this is new code, not an edit |
| BPI prototype | **7** | `import_bpi_csv_to_old_db.py`, `create_ai_native_db_schema.py`, `clean_and_load_to_ai_db.py`, `realtime_incremental_sync.py`, `build_ai_ready_cases.py`, `build_unified_bpi_knowledge_base.py`, `verify_cross_store_integrity.py` |

**The headline number is 0 or 1, not 7** — provided the generic framework is the onboarding path, which is what the architecture intends and what the README states. The BPI prototype's 7 files are a retained baseline, explicitly labelled as such (`README.md:139`, "Stabilized source-specific prototype"). Judging Goal A by the prototype would be judging the wrong artefact.

**Which pipeline stages are configuration-driven today, and which are dataset-specific?**

| Configuration-driven (14) | Dataset-specific / code-fixed (5) |
|---|---|
| Source ingestion — connection + entity from registered source | Case construction — grouping keys literal (`build_ai_ready_cases.py:643`) |
| Schema discovery — fully automatic | Timeline construction — BPI columns only |
| Cleaning | Embedding model — no env read (`runtime/services.py:273`) |
| Normalization | Extraction driver — PostgreSQL only (`service.py:202,404`) |
| Field mapping — profile + overridable target vocabulary | Freshness — per-table only, schema fixed |
| Document extraction | |
| Embedding generation (batch/dimension configurable) | |
| Vector upsert — collections, dimension, tiers all env-driven | |
| Incremental sync — watermark columns declared in `ExtractionConfig` | |
| Transformation logging | |
| Data quality metrics | |
| Query/service API — host, port, auth, CORS, page size all env | |
| Lineage/provenance | |
| Cold archival — dir + key env-driven | |

**14 of 19 = 74% of stages configuration-driven; 5 of 19 = 26% dataset-specific or code-fixed.**

**Minimum set of changes to make the pipeline config-driven end to end:**

1. **Select the extraction driver from the registered source's `source_type`** — `orchestration/service.py:202` and `:404`. This is the single highest-leverage change: it unlocks MySQL, SQL Server, and MongoDB, all of which already have working connectors and discovery.
2. **Add `ERP_EMBEDDING_MODEL_ID` and read it** in `runtime/services.py:273`, and remove the two hardcoded fallbacks at `services.py:289,296`.
3. **Add a case-construction stage to `erp_pipeline`** driven by a configured grouping rule (case-key field, process-type field, timestamp field, activity field) rather than the literal `["process_type", "normalized_case_id"]`. This is new code and the largest item.
4. **Add a configurable state-translation table** (source status value → canonical state) supplied per organisation — stage 6, currently MISSING.
5. **Load `CanonicalTargetModel` from a configuration file** — the loader already exists (`mapping/canonical_model.py:295`); nothing calls it with external input.
6. Retire the BPI hardcoding by routing BPI through the generic path, rather than editing its 13 HIGH-severity literals in place.

Items 1, 2, and 5 are small. Items 3 and 4 are the real work, and they are the same work that closes the Goal B gap in 5.3.

## 5.3 Downstream Integration Readiness

**Could the other three components consume this component's output today? Partially — for generic records, yes; for cases, no.**

**Is there a service interface, or only a local database?**

There **is** a service interface: 20 REST endpoints (2.7), a generated OpenAPI contract (`artifacts/phase13_openapi.json`), API-key auth, request-id tracing, typed errors, and durable jobs. This is a genuine control plane, not a script collection.

But it exposes the wrong shape. `GET /v1/records/{record_id}` returns a `CanonicalRecord` (`entity_type` + `normalized_data`). **No endpoint returns a case.** The only case-shaped data in the system lives in the BPI `ai_ready_cases` PostgreSQL table, reachable only by direct SQL or the local `search_erp_knowledge.py` CLI. **For the data the downstream components actually need, the answer is: local database only.**

**Is the output schema defined and versioned, or ad-hoc?**

| Output | Defined? | Versioned? |
|---|---|---|
| `CanonicalRecord` / `CanonicalDocument` | **Yes** — typed dataclasses with validation (`canonical_models.py`) | **Yes** — `CANONICAL_MODEL_VERSION = "1.0.0"` (`version.py:40`) stamped on every record (`canonical_models.py:159`) |
| REST API | **Yes** — generated OpenAPI, 20 paths | **Yes** — `API_VERSION = "1.0"`, `/v1` prefix (`api/config.py:18-19`) |
| Mapping profiles | **Yes** | **Yes** — carry `model_id@version` (`mapping/canonical_model.py:32-36`) |
| **BPI case record** | **No** — an ad-hoc dict literal at `build_ai_ready_cases.py:295-310`. No dataclass, no validation, no schema file | **No** — carries `record_source`/`source_database`/`source_table_layer` string constants but **no version field** |

So: the generic contracts are well-defined and versioned; **the case output — the only case output that exists — is ad-hoc and unversioned.**

**Which canonical fields are missing, and which component is blocked by each?**

| Missing field | Blocked component | Consequence |
|---|---|---|
| `current_state` | **Governance** + **Orchestration** | Governance cannot evaluate policy against the case's present state; orchestration cannot decide the next action. **The most damaging single absence.** |
| `allowed_next_states` | **Orchestration** | Cannot constrain the AI to legal transitions — the core safety property is unimplementable |
| `entities.employee_id` | **Governance** | No separation-of-duty or approver checks |
| `entities.amount`, `entities.currency` | **Governance** + **Bridge** | No value thresholds or approval limits; bridge cannot populate an ERP write. Currency is actively destroyed at `clean_and_load_to_ai_db.py:228-230` |
| `documents` / `evidence_ids` | **Governance** + **Retrieval** | Cannot cite evidence; cannot ground an answer in the applicable policy document |
| `freshness.last_synced_at`, `freshness.is_stale` | **All three** | No consumer can tell whether it is acting on current data |
| `source_version` on the case | **All three** | No contract-version negotiation is possible for case data |

**Present and usable today:** `case_id`, `process_type`, `timeline` (ordered events with timestamps), plus deterministic identity and content hashing across all layers — which is a genuinely valuable foundation.

**Could someone start this component from a clean checkout using only the README?**

**No.** Walking `README.md:476-538` step by step against a hypothetical `git clone`:

| Step | README line | Outcome |
|---|---|---|
| `py -3.11 -m venv .venv` | 481 | ✅ |
| `.\.venv\Scripts\Activate.ps1` | 482 | ✅ |
| `pip install -r requirements.txt` | 484 | ✅ — `requirements.txt` is tracked |
| `pip install -e .` | 485 | ❌ **FIRST FAILURE — `pyproject.toml` is untracked.** No build backend, no `[project]` metadata, no `erp-api`/`erp-bootstrap` console scripts |
| `Copy-Item .env.example .env` | 486 | ✅ — tracked |
| `cd frontend; npm ci` | 492-493 | ❌ `package.json` and `package-lock.json` are gitignored (`.gitignore:10`) |
| `python -m erp_pipeline.api` | 518 | ❌ `src/erp_pipeline/` is entirely untracked — the package does not exist in a clone |
| BPI pipeline scripts | 557-578 | ❌ The 9 scripts *are* tracked, but they import `bpi2020.common.config` (`build_ai_ready_cases.py:53`) and `src/bpi2020/common/` is **untracked**. Every script fails on import |

**The first step that fails is `pip install -e .` (README.md:485).** Every subsequent step also fails. A clean checkout yields 21 files: 9 BPI scripts that cannot import, a 733-line README describing a system that is not present, `.env.example`, `.gitignore`, `requirements.txt`, and 6 binary data files.

**This is not a documentation gap. The README is accurate about a working tree that was never committed.**

## 5.4 Research Evaluation Readiness

**What quantitative results can this component produce today?**

These are not projections — both were produced during this audit.

**1. Explainable mapping accuracy** (`tests/erp_pipeline/mapping/test_mapping_benchmark.py`, emitted during the full suite run):

| Metric | Value |
|---|---|
| Labelled mappings | 68 (60 positive, 8 negative) |
| Top-1 accuracy | 1.0 |
| Top-3 recall | 1.0 |
| Auto-selection precision | 1.0 (60/60) |
| Automatic coverage | 0.8824 |
| Ambiguity rate | 0.0 |
| Unmapped rate | 0.0882 |
| Correct refusal rate | 1.0 |
| Alias-independent top-1 | 1.0 (18/18 labels absent from the alias registry) |

**2. Tiered vector storage: retrieval quality and latency** (`artifacts/phase12_storage_benchmark.json`; 500 real MiniLM vectors, 40 queries, 5 entity types):

| Metric | HOT | WARM (int8) | COLD (AES-256-GCM) |
|---|---|---|---|
| recall@1 | 0.15 | 0.15 | 0.15 |
| recall@3 | 0.475 | 0.475 | 0.475 |
| recall@5 | 0.55 | 0.55 | 0.55 |
| Search median | 11.01 ms | 16.45 ms | 15.35 ms |
| Search p95 | 24.51 ms | 33.20 ms | 34.99 ms |

Plus: hot↔warm and cold↔hot top-5 overlap = 1.0; cold vector round-trip lossless (max component deviation 0.0); one-time rehydration 9,335.7 ms / 500 records = 18.67 ms per record.

**3. Test-suite evidence:** 2,567 passing tests, 26 skipped, 0 failing, 35:07 wall clock — a defensible verification claim, with 41,174 lines of test code against 50,774 lines of framework.

**4. Cross-store integrity:** `verification/verify_cross_store_integrity.py` produces a calculated PASS/FAIL across PostgreSQL, files, and Qdrant.

**The strongest publishable claim available today:** hybrid tiering — int8 quantization and encrypted cold archival with rehydration — costs **zero retrieval quality** (identical recall at all three tiers, top-5 ordering overlap 1.0, lossless round-trip) at a stated and measured latency cost. The benchmark's `claim_safety` block, which explicitly partitions `measured` / `proxy` / `estimated` / `not_claimed`, is exactly the discipline an evaluation chapter needs.

**What is missing, per requested metric:**

| Metric | Status | What is needed |
|---|---|---|
| **Retrieval precision/recall** | **Available** — recall@1/3/5 over 40 queries | recall@1 = 0.15 is low enough to need a stated baseline and a larger, more representative query set to be interpretable |
| **Data quality metrics** | **Largely missing** | No rejection accounting exists anywhere (2.11, 2.12). Rows dropped for a missing case id, values coerced to `None`, and failed JSON parses are never counted. Add counters at `clean_and_load_to_ai_db.py:152`, `:212`, `:232`, and `build_ai_ready_cases.py:93`, and write them to `transformation_logs` |
| **Transformation coverage** | **Partially available** | `mapping/coverage.py:36` computes coverage for generic mapping profiles. The BPI corpus has no mapping profile, so no coverage figure exists for the only dataset actually processed end to end |
| **Sync latency** | **Missing** | `sync_state.last_synced_at` stores a timestamp but no lag is computed. Needs an end-to-end timer: source commit → vector visible |
| **Document-to-case linkage rate** | **Missing** | No linkage exists to measure (stage 11) |
| **Onboarding effort comparison** | **Missing — and this is the thesis-critical gap** | Goal A's central claim is that a new organisation needs only configuration. **No second ERP source has ever been onboarded.** Until one is, the claim is architectural, not empirical. The cheapest credible experiment: onboard the MySQL `sakila` database that live tests already reach, and record files edited (expected: 1) and configuration supplied |

**Bottom line:** the component can produce a solid evaluation chapter on **retrieval quality under tiering** and **explainable mapping accuracy** today. It cannot yet produce the **onboarding-effort** or **data-quality** chapters, and onboarding effort is the one the project's central thesis rests on.

## 5.5 Governance Compliance Scorecard

| Requirement | Status | Gap severity | Single most important corrective action |
|---|---|---|---|
| **3.1** Commit granularity & message convention | **NON-COMPLIANT** | **HIGH** | Commit the 284 untracked Python files in coherent, scoped commits — one per phase package — using Conventional Commits prefixes |
| **3.2** Branching strategy | **NON-COMPLIANT** | **MEDIUM** | Adopt and document a strategy in the README (GitHub Flow is the right fit for a solo research repo), then use `feature/*` branches for subsequent work |
| **3.3** Main branch stability | **NON-COMPLIANT** | **HIGH** | Committed `main` is broken — the 9 tracked BPI scripts import the untracked `src/bpi2020/common/`. Fix by committing the missing packages, then verify with a clean-clone smoke test |
| **3.4** Pull request process | **NOT-CONFIGURED** | **MEDIUM** | Add `.github/pull_request_template.md` and route all future work through PRs, even self-merged ones |
| **3.5** Documentation requirements | **PARTIAL** | **MEDIUM** | Technical documentation is strong; governance documentation is entirely absent. Add five README sections: branching strategy, branch naming, commit convention, merge procedure, dated change log. Correct the false claim at `README.md:150` |
| **3.6** Secrets & ignore hygiene | **PARTIAL** | **MEDIUM** | Security is **clean** — no secret ever committed, no credential in any tracked file, no file over 5 MB. The defect is usability: `.gitignore:10` (`*.json`) silently excludes `frontend/package.json` and `package-lock.json`. Add `!frontend/package.json` and `!frontend/package-lock.json` to the negation block at `:283-292` |
| **3.7** CI/CD & quality gates | **NOT-CONFIGURED** | **HIGH** | Add `.github/workflows/ci.yml` running `pytest` on push and PR. Without it, "main is always deployable" is unenforceable |
| **3.8** Contribution traceability | **PARTIAL** | **MEDIUM** | Two git identities for one person — add a `.mailmap`. The larger issue is that 96.9% of the work has no recorded author at all; fixed by 3.1 |
| **3.9** Versioning & milestones | **NON-COMPLIANT** | **MEDIUM** | Zero tags exist. After committing, tag `v0.13.0` to match `pyproject.toml:6`, and add `CHANGELOG.md` |
| **3.10** Repository structure | **PARTIAL** | **MEDIUM** | Single-component repo with clean internal boundaries (`erp_pipeline` never imports `bpi2020` — verified). Gap: the OpenAPI contract is not committed or published to the other three teams |

**Summary: 0 COMPLIANT · 4 PARTIAL · 4 NON-COMPLIANT · 2 NOT-CONFIGURED.**

Every single governance failure traces to one root cause: **the work was never committed.** Fix that, and 3.1, 3.3, and 3.8 resolve immediately; 3.2, 3.4, 3.7, and 3.9 become achievable in an afternoon.

## 5.6 Prioritised Action List

### Group A — Blocks integration

| # | Action | Effort |
|---|---|---|
| A1 | **Commit everything.** `git add pyproject.toml docs/ tests/ artifacts/ scripts/ src/erp_pipeline/ src/erp_integrations/ src/bpi2020/common/ src/bpi2020/verification/ src/bpi2020/qdrant_connection.py frontend/` in scoped commits. Then `git add -u` for the 15 modified files. Verify with `git clone` to a temp dir + `pip install -e .` + `pytest --collect-only`. **Nothing else in this list matters until this is done.** | **3 h** |
| A2 | Add `!frontend/package.json` and `!frontend/package-lock.json` to the `.gitignore` negation block at `:283-292`, then commit both files. Verify `npm ci` from a clean clone | **0.5 h** |
| A3 | **Define a canonical case contract.** Add `CanonicalCase` to `src/erp_pipeline/schemas/canonical_models.py` as a typed dataclass with `case_id`, `process_type`, `current_state`, `entities` (`employee_id`, `amount`, `currency`), `timeline`, `allowed_next_states`, `evidence_ids`, `freshness` (`last_synced_at`, `is_stale`), and `schema_version`. Stamp `CANONICAL_MODEL_VERSION`. This is the contract the other three components need | **8 h** |
| A4 | **Implement state-code translation** (stage 6). A configurable `source status value → canonical state` table, supplied per registered source, applied during `run_transform` (`orchestration/stages.py:172`) | **8 h** |
| A5 | **Implement allowed-next-state derivation** (stage 9). A configurable process state machine keyed by `process_type`, populating `allowed_next_states` from `current_state`. Without this the orchestrator cannot be constrained to legal transitions | **12 h** |
| A6 | **Implement case construction in `erp_pipeline`** (stage 7 generic). A new `run_case_build` stage driven by a configured grouping rule (case-key field, process-type field, timestamp field, activity field), replacing the literal `["process_type", "normalized_case_id"]` at `build_ai_ready_cases.py:643` | **16 h** |
| A7 | **Implement document-to-case linking** (stage 11). Add `evidence_ids` population by matching document metadata to cases; expose the linkage rate as a metric | **8 h** |
| A8 | **Unlock non-PostgreSQL extraction.** Select the driver from the registered source's `source_type` in `_sqlalchemy_factory` (`orchestration/service.py:202`) and `_incremental_engine` (`:404`). Connectors and discovery already exist for MySQL, SQL Server, MongoDB | **4 h** |
| A9 | Add `GET /v1/cases/{case_id}` and `POST /v1/cases/search` to `api/routers_data.py`, returning the A3 contract. Regenerate `artifacts/phase13_openapi.json` and commit it | **6 h** |
| A10 | Add `ERP_EMBEDDING_MODEL_ID`; read it in `runtime/services.py:273`; delete the hardcoded fallbacks at `services.py:289` and `:296` | **1 h** |
| A11 | Wire `erp_integrations` cascade to a real entry point so a BPI sync rebuilds cases → unified → vectors. 1,148 lines of tested code are currently unreachable | **4 h** |
| A12 | Publish the OpenAPI contract to the other three teams — commit `artifacts/phase13_openapi.json` and reference it from the README | **1 h** |

**Group A total: ~71.5 hours**

### Group B — Blocks research evaluation

| # | Action | Effort |
|---|---|---|
| B1 | **Add rejection accounting.** Counters at `clean_and_load_to_ai_db.py:152` (no case-id column), `:212` (timestamp coercion failures), `:232` (amount coercion failures), and `build_ai_ready_cases.py:93` (JSON parse failures). Write all four to `transformation_logs`. This closes the largest data-quality gap and the worst fail-open path simultaneously | **4 h** |
| B2 | **Run the onboarding-effort experiment.** Onboard the MySQL `sakila` database that live tests already reach, via the generic path. Record: files edited, configuration keys supplied, wall-clock time. This produces the empirical evidence for Goal A, currently the thesis's weakest point | **6 h** (after A8) |
| B3 | **Add sync-latency instrumentation.** Measure source-commit → vector-visible; emit to `transformation_logs` and expose on `/v1/health/ready` | **4 h** |
| B4 | **Strengthen the retrieval benchmark.** recall@1 = 0.15 needs a stated baseline (random / BM25) and a larger query set. Extend `scripts/run_phase12_benchmark.py` | **6 h** |
| B5 | **Stop discarding currency.** `clean_and_load_to_ai_db.py:228-230` strips `€`/`$` without recording them. Capture the symbol into `entities.currency` | **2 h** |
| B6 | Compute transformation coverage for the BPI corpus by routing it through a mapping profile | **4 h** (after A6) |

**Group B total: ~26 hours**

### Group C — Blocks governance compliance

| # | Action | Effort |
|---|---|---|
| C1 | Add `.github/workflows/ci.yml`: on `push` and `pull_request`, run `pip install -r requirements.txt && pip install -e . && pytest -q`. Exclude `test_live_*` from CI (they need live services). Makes "main is deployable" enforceable | **2 h** |
| C2 | Add five governance sections to the README: branching strategy (GitHub Flow), branch naming (`feature/`, `bugfix/`, `docs/`), commit convention (Conventional Commits), merge procedure, and a **dated change log** table | **3 h** |
| C3 | Correct the false claim at `README.md:150` ("artifacts are tracked") — true only after A1 | **0.1 h** |
| C4 | Add `.github/pull_request_template.md` and `CODEOWNERS` naming the component owner | **1 h** |
| C5 | Add `.mailmap` unifying `DishaniS <dishanialuthwaththa@gmail.com>` and `Dishani Authwaththa <146338103+DishaniS@users.noreply.github.com>` | **0.2 h** |
| C6 | Tag `v0.13.0` matching `pyproject.toml:6`; add `CHANGELOG.md` | **1 h** |
| C7 | Configure a linter — add `[tool.ruff]` to `pyproject.toml` (the `# noqa: BLE001` / `# noqa: SLF001` comments imply one was intended) and run it in CI | **2 h** |
| C8 | Pin the remaining 15 dependencies in `requirements.txt`, or adopt `pip-tools` / `uv` for a lockfile | **2 h** |
| C9 | Add a `LICENSE` file — none exists, flagged by the README's own known issue #15 (`README.md:689`) | **0.5 h** |
| C10 | Document the destructive behaviours in the README: `to_sql(if_exists="replace")` table drops (`import_bpi_csv_to_old_db.py:149`), the two prune-deletes, and startup DDL | **1 h** |

**Group C total: ~12.8 hours**

---

**Grand total: ~110 hours.**

**A1 is the highest-priority action in this entire report.** It costs 3 hours, and until it is done every other engineering achievement documented here — 50,774 lines of framework, 2,567 passing tests, 20 REST endpoints, two measured benchmarks — exists only on one machine and is one disk failure from being lost entirely.
