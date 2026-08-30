# Final Component Compliance Audit — Member 4

**Student:** IT22267290 · **Project:** R26-SE-034 · **Member:** 4
**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and
Retrieval Pipeline for Legacy ERP Systems
**Audit date:** 2026-08-25 · **Phase:** 12 (final evaluation and freeze)

**Prior readiness:** 58 / 100, from
`IT22267290_REVISED_COMPONENT_REQUIREMENT_COMPLIANCE_AUDIT.md` (2026-08-24,
untracked at repository root), which enumerated R1–R38 and scored the component
against the revised scope. This audit re-evaluates all 38 requirements against
the **final** code and does not reuse that audit's reasoning or its per-area
scores.

---

## 1. Audit basis

Every status below is asserted against the **final code**, and each row names
the module, the API or job that exposes it, the test that pins it, and the
evaluation artifact that measured it. Where evidence is weaker than the status
might suggest, the row says so.

Measured facts at freeze:

| | |
|---|---|
| Production Python files / LOC | 193 / 66,682 |
| Test files / LOC | 164 / 58,905 |
| Full regression | 3730 collected · 3667 passed · 0 failed · 0 errors · 63 skipped |
| Frontend tests | 26 passed (2 files, vitest 2.1.9) |
| REST operations | 24 (OpenAPI 3.1.0, service 1.0) |
| JobTypes / PipelineStages / ContentKinds / filters | 7 / 19 / 3 / 13 |
| Qdrant collections | `erp_vectors_hot`, `erp_vectors_warm`, encrypted COLD archive |
| PostgreSQL | 5 schemas, 20 tables |
| Embedding | `all-MiniLM-L6-v2`, 384-D, local, no LLM |

## 2. The collection-architecture question, settled

The earlier audit's expectation of `image_vectors` / `pdf_vectors` /
`schema_vectors` / `employee_vectors` collections is **not** met, and should not
be. The final architecture is deliberate:

> **physical collections = storage tiers.  logical data kinds = metadata.**

HOT (float32, RAM), WARM (int8, on-disk) and COLD (gzip + AES-256-GCM) separate
vectors by *access economics*, which is the only axis on which a vector store's
physical layout actually pays. Modality and entity are carried as filterable
metadata — `content_kind`, `entity_type`, `document_type`, `entity_kind` and
nine more.

Why one-collection-per-modality would be worse here:

- A cross-modal query ("everything about EMP002") would need a fan-out across
  collections and a client-side merge, replacing one filtered search with N.
- Tier migration would have to move a vector *between* modality collections to
  change its temperature, or every modality would need its own three tiers —
  12 collections to express what 3 already express.
- Adding a modality would become a schema migration rather than a new enum
  value.

The technical purpose of "suitable collections" — that a query can be
restricted to the right subset, cheaply and exactly — is met by
`FILTERABLE_FIELDS`, verified by Phase 4 (0 wrong-identity matches over 14
queries) and Phase 7 (0 wrong-source matches under exact filters).

**Status: NOT REQUIRED / REJECTED DESIGN**, with the rejection justified rather
than assumed.

## 3. The 38 revised requirements

Legend: **F** fully satisfied · **P** partially satisfied · **N** not satisfied
· **X** not required / rejected design.

### Discovery and structured data

| # | Requirement | St | Evidence | Remaining limitation |
|---|---|---|---|---|
| R1 | DB connectivity | **F** | `connectors/` — postgresql, mysql, sqlserver, mongodb; `POST /v1/sources`, `/v1/sources/{id}/test` | SQL Server implemented, live verification deferred |
| R2 | Schema/table/column discovery | **F** | `discovery/relational.py`, `discovery/mongodb_inference.py`; `POST /v1/sources/{id}/discover`; `GET /v1/schemas/{id}` | MongoDB inference is sampling-based; live tests skip without a server |
| R3 | Relationships | **F** | `catalog` table `source_relationships`; FK extraction in `discovery/relational.py` | Endpoints validated against `normalized_name`; no inferred (non-declared) relationships |
| R4 | Structured extraction | **F** | `ingestion/`, EXTRACT stage; `structured_pipeline` job | — |
| R10 | Normalization | **F** | `transformation/`, canonical models; VALIDATE stage | — |
| R19 | Structured-record indexing | **F** | `content_kind=structured_record`; Phase 12 CASE 1 (3/3 records) | Requires a declared key — see §4 |

### Multimodal preparation

| # | Requirement | St | Evidence | Remaining limitation |
|---|---|---|---|---|
| R5 | Binary detection | **F** | `ingestion/detection.py`, `binary_assets.py`; magic-byte detection | Unsupported types refused, not guessed (CASE 10, 415) |
| R6 | Image handling | **F** | `ingestion/image_ingestion.py`; in-memory `FileSource.payload` | — |
| R7 | URL handling | **F** | `ingestion/remote_assets.py`; Phase 8 artifact: 0 private targets contacted, 0 secret URL leakage | Ships **disabled**; no HTTP client bundled; unchanged URLs are not re-fetched |
| R8 | PDF handling | **F** | `ingestion/pdf_ingestion.py` (PyMuPDF) | Corrupt PDFs reported, never invented (CASE 10, 422) |
| R9 | OCR | **F** | `ingestion/ocr.py`; Tesseract 5.5.0 resolved via `TESSERACT_CMD`/`TESSERACT_PATH` | Standard OCR — engineering integration, not a research claim |
| R11 | Chunking | **F** | `ai/chunking.py`; 256-token model window ≈ 1024 chars measured | Chunk size is bounded by MiniLM's window, not by document semantics |
| R35 | DB BLOB → document routing | **F** | `orchestration/multimodal.py`, MULTIMODAL_EXTRACT stage; Phase 3: 7 indexed, 0 collisions, 0 leakage | — |

### Representations, embeddings, storage

| # | Requirement | St | Evidence | Remaining limitation |
|---|---|---|---|---|
| R12 | AI-ready representations | **F** | `ai/representation.py`, `attached_documents.py`, `schema_representation.py` | Deterministic by design; no generative summarisation |
| R13 | Embeddings | **F** | `ai/embedding.py`, `all-MiniLM-L6-v2`, 384-D, local | 384-D limits datatype-vocabulary retrieval (Phase 7) |
| R14 | Qdrant indexing | **F** | `storage/hot_tier.py`, `warm_tier.py`, `cold_tier.py` | No payload indexes — research scale |
| R15 | Collection architecture | **X** | Tiers-as-collections; see §2 | Deliberate rejection, justified |
| R16 | Metadata | **F** | `StorageRecordMetadata` (~44 fields); 13 filterable + 3 provenance-only | — |
| R17 | Schema knowledge indexing | **F** | `schema_pipeline` job; `content_kind=schema`; Phase 7 artifact | Recall@1 = 0.727 |
| R18 | Document representations | **F** | `content_kind=document_chunk`; page/chunk provenance | — |
| R26 | Provenance | **F** | `RecordProvenance`; page_start/page_end/chunk_index/document_id; Phase 5: 0 provenance mismatches over 58 hits | — |

### Retrieval

| # | Requirement | St | Evidence | Remaining limitation |
|---|---|---|---|---|
| R20 | Exact record retrieval | **F** | `GET /v1/records/{id}`; exact filters; Phase 4: 0 wrong-identity matches | — |
| R21 | Semantic retrieval | **F** | `POST /v1/search`, cosine over 384-D | Ranking quality varies by query type (Phase 7) |
| R22 | Filter support | **F** | 13 filterable fields; unknown filters rejected, not ignored | Exact-match only; no ranges or negation |
| R23 | Business/document identity retrieval | **F** | `business_key_name` + `business_key_value` + `document_type`; Phase 12 CASE 2 | Identity is **declared**, never inferred |
| R28 | Retrieval API | **F** | 24 operations; OpenAPI 3.1.0; Phase 11 contract tests | — |
| R38 | Extracted text retrieval | **F** | `GET /v1/representations/{id}`; Phase 5: 58/58 hits resolved, 0 unresolvable | Text is resolved in a **second** call — never in the hit |

### Synchronisation and lifecycle

| # | Requirement | St | Evidence | Remaining limitation |
|---|---|---|---|---|
| R24 | Incremental sync | **F** | `sync/coordinator.py`, 5 watermark strategies; `incremental_sync` job | Polling; `safe_watermark` only advances past successes |
| R25 | Schema drift | **F** | `sync/drift.py`; `drift_check` job; Phase 12 CASE 6 (field gained, none lost) | Drift detection, not automatic remediation |
| R27 | Update/delete consistency | **F** | `orchestration/lifecycle.py`; `is_current` backstop; Phase 9: 0 wrong-current-version hits | Physical deletion may lag; correctness does not |
| R31 | Real-time / near-real-time | **P** | `orchestration/scheduler.py`; Phase 9: interval 5 s + 0.877 ms median processing | **Not CDC.** Freshness is bounded by the interval. Partial *by design*, and the wording is constrained accordingly |

### Security, integration, evaluation

| # | Requirement | St | Evidence | Remaining limitation |
|---|---|---|---|---|
| R29 | Frontend upload | **F** | `POST /v1/files/documents`; Phase 6: 6/6 automatic jobs, 0 manual calls | — |
| R30 | Member 2 runtime integration | **F** | `POST /v1/responses/adapt`; Phase 11: 21/21 scenarios, M4 ERP executions 0 | Collection responses adapt the first record only |
| R32 | Sensitivity | **F** | `schemas/sensitivity.py`, `representation_crypto.py`; Phase 10: 0 wrong assignments, 0 downgrades | Metadata + storage protection — **not** authorization |
| R33 | Failure/recovery | **F** | Partial-status jobs, `POST /v1/jobs/{id}/retry`, fail-closed encryption; Phase 12 CASE 10 (6/6) | Never retries an ERP business request — deliberate |
| R34 | Evaluation | **F** | 11 evaluation scripts, 12 artifacts, multidimensional evidence table | Corpora are synthetic and small; see §6 |
| R36 | Upload → automatically searchable | **F** | Phase 6 artifact; Phase 12 CASE 4 | CSV rows deliberately excluded |
| R37 | Chunk/page payload | **F** | `page_start`, `page_end`, `chunk_index` on hits and resolutions | — |

**Tally: 36 FULLY SATISFIED · 1 PARTIALLY SATISFIED (R31) · 0 NOT SATISFIED ·
1 NOT REQUIRED (R15).**

R31 is partial in the precise sense that near-real-time polling is not
change-data-capture. It is not a gap in delivery — CDC was explicitly out of
scope — so it is scored as a bounded capability, and every claim about it is
worded to match.

## 4. What closed the previous gaps

The 2026-08-24 audit scored thirteen areas. Its own diagnosis was that the
shortfall was **a scope change, not a quality problem** — the revised component
definition asked for four capabilities the original never targeted. Those four
are exactly what Phases 3–11 built.

| Area | Then | Now | What changed |
|---|---|---|---|
| Legacy DB discovery | 92 | **90** (A) | Unchanged capability; deductions now name the SQL Server and MongoDB caveats explicitly |
| Structured transformation | 90 | **95** (B) | Source-native entities; keyed admission |
| Multimodal extraction (engine) | 85 | **93** (C) | Engines unchanged; in-memory extraction, no temp files |
| **DB BLOB / document handling** | **10** | **~95** (C) | Phase 3: detection → routing → OCR → chunk → vector. 7 indexed, 0 collisions, 0 leakage |
| **Schema vector indexing** | **0** | **85** (D) | Phase 7: `schema_pipeline`, `content_kind=schema`. R@1 0.727 / R@3 0.909 / MRR 0.811 |
| Document vector indexing | 55 | **~95** (C/F) | Phase 6 automatic indexing (0 manual calls) + Phase 5 text and page provenance |
| Qdrant architecture | 75 | **90** (E) | Tier design confirmed and justified; deductions for no payload indexes |
| Semantic retrieval | 70 | **~95** (F) | Combined with exact filters; 0 wrong identities |
| **Exact identity retrieval** | **25** | **~97** (F) | Phase 4: allow-list grew 5 → 13 fields **including business identifiers**; the precise gap that audit named |
| Incremental freshness | 60 | **90** (G) | Phase 9: scheduler, watermarks, current-version lifecycle, stale suppression |
| Frontend integration | 35 | **100** (I) | Phase 11: 114 contract tests, 21/21 scenarios over HTTP |
| Member 2 integration | 95 | **100** (I) | Phase 11 contract tests; 0 ERP executions by Member 4 |
| Research evaluation | 60 | **80** (J) | 12 artifacts, 11 evaluators; deductions for synthetic and small corpora |

Mapped to concrete deliverables:

| Previous gap | Closed by | Evidence |
|---|---|---|
| Prototype-only case/timeline construction | Removed from production; generic source-native entities replace it | `src/bpi2020` and `src/erp_integrations` no longer in the production tree |
| Freshness per table only, no staleness concept | Phase 9 current-version lifecycle + `is_current` | Phase 9: 0 wrong-current-version hits |
| No document/BLOB path | Phases 3, 6, 8 | 3 artifacts, 0 leakage, 0 collisions |
| No schema knowledge in vectors | Phase 7 | Recall@1 0.727 / Recall@3 0.909 / MRR 0.811 |
| No content resolution | Phase 5 | 58/58 hits resolved |
| Sensitivity metadata only, plaintext at rest | Phase 10 AES-256-GCM, fail-closed | 0 restricted plaintext findings |
| No cross-member contract | Phase 11 | 114 tests, 21/21 scenarios, 9 gates at zero |
| OpenAPI not published | `artifacts/phase13_openapi.json` regenerated from its own test | 24 operations |
| Uploaded `sensitivity` silently dropped | Phase 11 one-line fix | Phase 12 CASE 8 |
| Caller's tier-state store silently replaced | Phase 11 `is not None` fix | Restart contract test |

## 5. Weighted readiness score

Weights are the brief's, unchanged. Each score is evidence-led; deductions are
named rather than absorbed.

| | Category | Weight | Score | Deductions |
|---|---|---|---|---|
| A | Legacy ERP discovery / connectivity | 10 | **9.0** | SQL Server live verification deferred (−0.5); MongoDB inference is sampling-based and its 24 tests skip without a server (−0.5) |
| B | Structured transformation | 10 | **9.5** | Source-native indexing requires a declared key when a schema has no primary key — correct, but a real integration step (−0.5) |
| C | Multimodal ERP preparation | 15 | **14.0** | Remote fetching ships disabled and unchanged URLs are not re-fetched (−0.5); chunking bounded by the 256-token window (−0.5) |
| D | Schema AI / vector support | 10 | **8.5** | Recall@1 = 0.727; datatype-vocabulary queries measurably weaker (−1.5) |
| E | Embedding / vector storage | 10 | **9.0** | No Qdrant payload indexes; filter cost is research-scale (−0.5); tier fidelity measured, production-scale latency not (−0.5) |
| F | Retrieval / identity / content resolution | 15 | **14.5** | Exact-match filters only — no ranges or negation (−0.5) |
| G | Synchronisation / lifecycle | 10 | **9.0** | Polling, not CDC (−0.5); hard-delete observability is connector-dependent (−0.5) |
| H | Security / sensitivity | 10 | **9.0** | Business-payload content is not secret-scanned (−0.5); on-premises tier constraint not currently binding (−0.25); `protect_reads` defaults off (−0.25) |
| I | Cross-member integration | 5 | **5.0** | None — 21/21 scenarios, all gates zero, no boundary violation |
| J | Research evaluation / evidence | 5 | **4.0** | Synthetic corpora, small samples, single annotator for Phase 14, in-process latencies (−1.0) |
| | **TOTAL** | **100** | **91.5** | |

### **FINAL COMPONENT READINESS = 91.5 / 100** (previous: 58 / 100)

The 8.5 points withheld are all real and all documented: schema datatype
retrieval (−1.5), evaluation-corpus strength (−1.0), and seven smaller bounded
limitations. None is a defect; each is a limit of what was measured or a
deliberate scope decision.

## 6. Evidence strength — an honest grading

Not all 91.5 points rest on equally strong evidence.

| Dimension | Corpus | Strength |
|---|---|---|
| Mapping (C1) | 68 hand-labelled, 8 negatives | **Strong** — negatives make a constant mapper fail |
| Response adaptation (C5) | 68 cases, 374 labelled fields, 3-arm ablation | **Strong design, weak sampling** — single annotator (the author) |
| Schema retrieval (C6) | 4 systems, 24 entities, 95 fields, 22 queries | **Moderate** — 22 queries is a small denominator |
| Storage fidelity (C3) | 500 records, 40 queries, live Qdrant | **Moderate** — measures *fidelity*, not retrieval accuracy |
| Identity retrieval (C4) | 14 queries, 9 representations | **Weak sampling, strong property** — 0 is the only passing value |
| Multimodal (C2) | 6 rows, 11 binary values | **Weak sampling, strong property** — leakage/collision gates |
| Synchronisation (C7) | 8 source changes | **Weak sampling, strong property** |
| Security (Phase 10) | 7 assignments, 4 classes | **Weak sampling, strong property** |
| Integration (Phase 11) | 21 scenarios, 114 tests | **Strong for contracts**, fakes not real members |
| Final consolidation (Phase 12) | 30 scenarios, 10 cases | **Strong coverage, in-process** |

The pattern is deliberate: where a metric is a *rate*, the corpus needs to be
large; where it is a *count that must be zero*, one violation falsifies the
claim and a small corpus is informative.

## 7. Compliance conclusion

All 16 final hard gates pass. 36 of 38 requirements are fully satisfied, one is
bounded by design, one is a justified design rejection. No requirement is
unsatisfied.

**Readiness: 91.5 / 100.**
