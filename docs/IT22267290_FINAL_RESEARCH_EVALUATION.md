# Final Research Evaluation — Member 4

**Student:** IT22267290 · **Project:** R26-SE-034
**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and
Retrieval Pipeline for Legacy ERP Systems
**Date:** 2026-08-25

This document is about *evidence*. Implementation description lives in
[`FINAL_COMPONENT_TECHNICAL_REPORT.md`](FINAL_COMPONENT_TECHNICAL_REPORT.md);
requirement status lives in
[`IT22267290_FINAL_COMPONENT_COMPLIANCE_AUDIT.md`](IT22267290_FINAL_COMPONENT_COMPLIANCE_AUDIT.md).

---

## 1. Evaluation questions

| | Question | Where answered |
|---|---|---|
| EQ1 | Can source-to-canonical field mapping be automated with explainable evidence, and does it refuse rather than guess when ambiguous? | §4 |
| EQ2 | Does tiered vector storage preserve retrieval fidelity across HOT/WARM/COLD? | §5 |
| EQ3 | Can heterogeneous ERP binaries be prepared for retrieval without leaking bytes or mis-associating documents? | §6 |
| EQ4 | Does exact ERP identity filtering combined with semantic ranking return the right record? | §7 |
| EQ5 | Can every current search hit be resolved to its actual text? | §8 |
| EQ6 | Does an uploaded document become searchable with no second call? | §9 |
| EQ7 | Can ERP *structure* be retrieved semantically, and where does it fail? | §10 |
| EQ8 | Can declared remote assets be fetched without becoming an SSRF vector? | §11 |
| EQ9 | Is near-real-time freshness bounded and correct? | §12 |
| EQ10 | Is declared sensitivity propagated end-to-end and enforced at rest? | §13 |
| EQ11 | Does the component integrate with three other members without absorbing them? | §14 |
| EQ12 | Does ERP-aware adaptation reduce context while preserving the fields an answer needs? | §15 |
| EQ13 | Does the whole component work end to end? | §16 |

## 2. Experimental environment

| | |
|---|---|
| Python | 3.13.9 |
| Platform | Windows 11 (10.0.26200) |
| Embedding | `sentence-transformers/all-MiniLM-L6-v2`, 384-D, local |
| sentence-transformers / torch | 5.6.1 / 2.13.0 |
| FastAPI / Pydantic / SQLAlchemy | 0.141.1 / 2.13.4 / 2.0.51 |
| PyMuPDF / Pillow / pytesseract | 1.28.0 / 12.3.0 / 0.3.13 |
| Tesseract | 5.5.0.20241111 (available) |
| cryptography / pytest | 50.0.0 / 9.1.1 |
| Qdrant client | 1.18.0 |
| **LLM calls** | **0 — in every phase, no exceptions** |

**Infrastructure at final-run time:** Qdrant **not reachable**, MongoDB **not
reachable**, PostgreSQL **not configured** (`AI_DB_*` absent), Tesseract
**available**. This accounts for all 63 skips (§17) and means the Phase 12
consolidation ran against an in-process vector tier. The Phase 12 *storage
benchmark* artifact was produced earlier against a **live** Qdrant at
`localhost:6333` and is reported as such.

Unavailable infrastructure is an environment fact, not a failed implementation
claim.

## 3. Datasets and corpora

| Corpus | Size | Provenance |
|---|---|---|
| Mapping benchmark | 68 labels (60 positive, 8 negative), 18 alias-independent | Hand-labelled by the author |
| Storage benchmark | 500 records, 40 queries, 384-D | Synthetic, hand-declared ground truth |
| Response adaptation | 68 cases, 149 relevant + 225 irrelevant labelled fields | Hand-labelled from each question *before* any method ran |
| Schema retrieval | 4 source systems, 24 entities, 95 fields, 22 queries | Synthetic multi-vendor |
| Multimodal | 6 rows, 11 binary values | Synthetic BLOB corpus |
| Identity retrieval | 9 representations, 14 queries | Synthetic, 3 employees + composite keys |
| Remote assets | 22 cases, 21 references | Synthetic; **no sockets opened** |
| Synchronisation | 8 source changes | Synthetic, injected clock |
| Sensitivity | 7 assignments, 4 classes | Synthetic |
| Integration | 21 scenarios, 7 fixtures | Recorded representative payloads |
| Final consolidation | 30 scenarios across 10 cases | Synthetic, end-to-end |

Every corpus is **synthetic and author-constructed**. This is the single
largest threat to validity and is discussed in §17.

## 4. Mapping evaluation (C1) — `test_mapping_benchmark.py`

Re-run at freeze:

| Metric | Value |
|---|---|
| Top-1 accuracy | **1.0** |
| Top-3 recall | **1.0** |
| Auto-selection precision | **1.0** (60/60) |
| Automatic coverage | **0.8824** |
| Correct refusal rate | **1.0** |
| Alias-independent top-1 | **1.0** (18/18 labels the alias registry never declared) |

**Why these numbers are not vacuous.** The corpus contains **8 negative
labels** — fields with no correct canonical target. A mapper that returned a
constant, or that always guessed its best candidate, would score 0 on correct
refusal. And 18 labels are matched by fields the alias registry never declared,
so the weighted matcher cannot be passing by lookup table alone.

**Automatic coverage is 0.8824, not 1.0, and that is the point.** 11.76% of
fields are routed to human review instead of being mapped automatically. A
system that mapped everything would score higher on coverage and worse on
trustworthiness.

**Limitation.** The corpus is author-labelled, and the same author wrote the
matcher. Top-1 = 1.0 on 68 labels is a ceiling effect: the benchmark is not
hard enough to discriminate between good and excellent matchers.

## 5. Storage fidelity evaluation (C3) — `tiered_storage_benchmark.json`

Ran against **live Qdrant**, 500 records, 40 queries, 384-D real model output.

| Measurement | Value |
|---|---|
| Cross-tier top-5 overlap (HOT vs WARM) | **1.0** |
| Cold round-trip | **lossless** |
| WARM → COLD movement | 2.1466 ms/vector |
| COLD → WARM movement | 33.4803 ms/vector |
| Embedding time (500 records) | 5.23 s |

**What this does and does not show.** It measures **fidelity** — that int8
quantisation and encrypted archival do not change which vectors come back. It
does **not** measure retrieval accuracy against an information-need. Reporting
"top-5 overlap = 1.0" as a retrieval-accuracy result would be a category error,
and this report does not make that claim.

The artifact itself carries a comparability warning: `cold_single_rehydration`
is a fetch-by-id, not a similarity search, and the cold footprint figure covers
a different scope (header, nonce, GCM tag, compression) from the comparable
vector-component proxy.

## 6. Multimodal evaluation (C2) — `multimodal_extraction_evaluation.json`

| Measurement | Value |
|---|---|
| Rows / binary values present | 6 / 11 |
| Documents indexed | 7 |
| Skipped (unsupported/unreadable) | 4 |
| OCR assets | 1 |
| Association collisions | **0** |
| Binary/base64 leakage | **0** (12,731 surface bytes audited) |
| All document representations typed | **true** |

Supplemented by Phase 6 (uploads: 7 attempted, 6 accepted, 6 automatic jobs, 0
manual calls) and Phase 8 (remote: 21 references, 15 permitted, 6 refused
before contact, 9 extractions).

**Contribution boundary.** PDF parsing (PyMuPDF) and OCR (Tesseract) are
**standard technologies**, and this report does not claim them as novel. The
research-relevant part is what surrounds them: the decision that a document's
identity is its *attachment* (`parent | source_field | chunk_id`) rather than
its content, which is what makes 0 collisions achievable when the same
certificate is attached to two employees.

## 7. Identity retrieval evaluation (C4) — `identity_retrieval_evaluation.json`

| Gate | Value |
|---|---|
| Queries attempted | 14 |
| Wrong identity matches | **0** |
| Wrong document-type matches | **0** |
| Content-kind leakage | **0** |
| Incomplete provenance | **0** |
| HOT/WARM parity failures | **0** |
| Unknown filters accepted | **0** |
| Latency (median / p95 / max) | 0.2293 / 0.8438 / 0.9238 ms |

Latency is **in-process**, against an in-process tier. It is not a Qdrant
latency measurement and must not be quoted as one.

**Contribution boundary.** Filtered vector search is ordinary. What is being
evaluated is the *identity model* — that `business_key_name` + `business_key_value`
are one declaration in two fields, that identity is declared rather than
inferred from filenames or OCR text, and that an unknown filter is rejected
rather than silently ignored. The zero-valued gates are the evidence.

## 8. Representation resolution evaluation — `representation_resolution_evaluation.json`

| Gate | Value |
|---|---|
| Search hits attempted | 58 |
| Search hits resolved | **58** |
| Unresolvable hits | **0** |
| Wrong text resolutions | **0** |
| Wrong parent identities | **0** |
| Chunk provenance mismatches | **0** |
| Lookup median / p95 | 0.251 / 0.5125 ms |
| Search-and-resolve median / p95 | 0.4581 / 0.7636 ms |

The architectural claim under test: a representation must never become
searchable without resolvable content. 58/58 is the whole result.

## 9. Automatic upload evaluation — `automatic_document_indexing_evaluation.json`

| Gate | Value |
|---|---|
| Uploads attempted / accepted | 7 / 6 |
| Automatic jobs created / completed / failed | 6 / 6 / 0 |
| **Manual job calls required** | **0** |
| Wrong identity matches | **0** |
| Upload → searchable (median / p95 / max) | 28.765 / 47.503 / **1142.399** ms |

**The max is 40× the median, and that is OCR.** A text PDF is parsed; a scanned
image is rasterised and passed to Tesseract. Quoting the median alone would
describe only the cheap path.

## 10. Schema retrieval evaluation (C6) — `schema_retrieval_evaluation.json`

| Metric | Value |
|---|---|
| Source systems / entities / fields | 4 / 24 / 95 |
| Queries | 22 |
| **Recall@1** | **0.7273** |
| **Recall@3** | **0.9091** |
| **MRR** | **0.8106** |
| Business-value leakage | **0** |
| Schema text in Qdrant | **false** |
| Index per source (median) | 238.56 ms |
| Query median / p95 | 22.463 / 29.945 ms |

**The failures are kept.** Queries phrased around **entity and field names**
rank well; queries phrased around **vendor datatype vocabulary** rank worse.
The cause is the embedding model: `all-MiniLM-L6-v2` is trained on general
English, where a token like `VARBINARY` or `BLOB` carries little semantic
neighbourhood, and 384 dimensions leave little room for a vocabulary the model
never learned.

**No post-hoc vocabulary fitting was performed.** Adding datatype synonyms
after seeing which queries failed would have raised Recall@1 and destroyed the
result's meaning — the benchmark would then measure the author's memory of the
failures, not the system's retrieval. This is recorded as a limitation, not
repaired.

## 11. Remote asset security evaluation — `remote_asset_security_evaluation.json`

| Gate | Value |
|---|---|
| References attempted | 21 |
| Permitted / refused before contact | 15 / 6 |
| **Private or internal targets contacted** | **0** |
| Wrong employee matches | **0** |
| Association collisions | **0** |
| Secret URL leakage | **0** |
| HTML pages indexed | **0** |
| Raw binary/base64 leakage | **0** |
| Policy validation median | 0.0872 ms |

**Method caveat, stated plainly:** the fetcher was an **injected recorder — no
network, no sockets opened**. This evaluates the *policy*, not real network
behaviour. Elapsed times cover validation and extraction only; no internet
round trip is included or simulated.

Re-verified at freeze in Phase 12 CASE 3: 5/5 unsafe targets (loopback,
link-local metadata, RFC1918, `file://`, credentials-in-URL) refused before
contact, 0 contacted.

## 12. Synchronisation and freshness evaluation (C7) — `sync_freshness_evaluation.json`

| Gate | Value |
|---|---|
| Source changes | 8 |
| **Permanently missed** | **0** |
| Wrong current-version hits | **0** |
| Watermark regressions | **0** |
| Cross-parent deletion errors | **0** |
| Duplicate concurrent syncs | **0** |
| Scheduler ticks / syncs submitted | 10 / 10 |
| Processing median / p95 / max | 0.877 / 1.54 / 1.954 ms |
| Configured interval | 5.0 s |

**Required wording:** *near-real-time freshness, bounded by the configured
synchronisation interval plus processing latency* — measured here as **5.0 s +
0.877 ms (median)**.

This is **not** CDC and **not** database replication. Nothing sleeps in the
evaluation; the clock is injected.

## 13. Sensitivity and security evaluation — `security_sensitivity_evaluation.json`

| Gate | Value |
|---|---|
| Sensitivity assignments attempted / correct | 7 / 7 |
| Wrong assignments / silent downgrades | **0 / 0** |
| Propagation failures | **0** |
| Restricted plaintext findings | **0** |
| Encrypted / plaintext representations | 6 / 1 |
| Algorithm | AES-256-GCM |
| Encrypt / decrypt median | 0.0472 / 0.0216 ms |

Cryptographic timings are **in-process**, not database or network latency.

Resolution rule: **strictest wins**. Where several trusted declarations apply,
the most restrictive is chosen — treating restricted data as internal is a
disclosure, while the reverse is an inconvenience.

## 14. Four-member integration evaluation — `integration_contract_evaluation.json`

| Gate | Value |
|---|---|
| Scenarios attempted / passed | 21 / 21 |
| Failed integration scenarios | **0** |
| **Member 4 ERP executions** | **0** |
| **Member 4 policy decisions** | **0** |
| Denied ERP operations executed | **0** |
| Wrong identity results | **0** |
| Unresolvable current hits | **0** |
| Credential leakage | **0** |
| Cross-member boundary violations | **0** |
| OpenAPI critical operation misses | **0** |
| Member 2 ERP executions expected / actual | 5 / 5 |

In-process latency: upload → searchable ~189 ms; search → resolved ~1315 ms
(includes first-query model warm-up); `/v1/responses/adapt` ~21 ms.

**Method caveat:** Members 1, 2 and 3 are **test doubles**, not real
implementations. These results establish that Member 4's contracts are coherent
and its boundaries hold; they cannot establish that a real Member 2 will shape
its requests the way the fixtures do.

The zero for *Member 4 ERP executions* is **structural, not merely observed**:
an AST scan proves no HTTP client (`requests`, `httpx`, `aiohttp`) and no MCP
library is imported anywhere in the production package.

## 15. Adaptive response transformation (C5) — `response_adaptation_evaluation.json`

68 cases, 149 relevant and 225 irrelevant labelled fields, three arms.

| Metric | **ERP-aware adaptive** | Generic | Raw |
|---|---|---|---|
| Relevant field recall | **0.979866** | 1.0 | 1.0 |
| Cases with perfect recall | **0.955882** | 1.0 | 1.0 |
| Irrelevant field removal | **0.608889** | 0.0 | 0.0 |
| Field reduction | **0.4736** | 0.1168 | 0.0 |
| Context reduction | **0.500405** | 0.143311 | 0.0 |
| Adaptation success | **1.0** | 1.0 | 1.0 |
| Latency mean / median / p95 | 16.49 / **15.8268** / **24.0542** ms | — | — |

**The trade-off is the finding.** The adaptive arm halves context
(0.500) and removes 60.9% of labelled-irrelevant fields, at the cost of 2.0% of
relevant fields — 3 fields across 68 cases. The generic arm keeps everything
(recall 1.0) and reduces context by only 14.3%. Neither arm dominates; the
result is a quantified exchange rate, not a win.

**The three failures are preserved, named, and not tuned:**

| Case | Entity | Missed field |
|---|---|---|
| `sap-04` | invoice | `BELNR` |
| `po-05` | purchase_order | `supplier_no` |
| `proc-02` | process_case | `resource` |

All three are opaque or abbreviated source field names — exactly where a
deterministic lexicon has least to work with. Adding them to the lexicon after
seeing them fail would convert a measurement into a memory.

Context reduction is measured on **bytes of canonical JSON, not tokens**: this
project ships no tokenizer, and an invented token count would be a guess.

## 16. Final end-to-end evaluation — `consolidated_component_evaluation.json`

30 scenarios across 10 cases, all passing; all 16 hard gates at zero.

| Case | Scenario | Result |
|---|---|---|
| 1 | Structured ERP: schema inferred, rows *not* auto-indexed, 3/3 indexed after an explicit keyed job, retrievable, resolvable | PASS |
| 2 | Restricted certificate: indexed, exact identity retrieval, correct text, no cross-employee contamination | PASS |
| 3 | Remote assets: 5/5 unsafe targets refused before contact, 0 contacted | PASS |
| 4 | Upload: indexed with no second call, searchable, resolvable | PASS |
| 5 | Schema query: only schemas returned, structure carried | PASS |
| 6 | Schema update: new field current, no existing field lost | PASS |
| 7 | Document replacement: B current, A not returned as current | PASS |
| 8 | Sensitivity: restricted on hits, no text in vector payload, no binary/base64, AES-256-GCM round-trip | PASS |
| 9 | Live ERP: adapted, executed once by Member 2, credentials redacted, binary → asset | PASS |
| 10 | Failures: unsupported binary (415), corrupt PDF (422), missing key fails closed, blocked URL, failed replacement preserves previous current version, ERP error adapted without retry | PASS |

Counts: 3 structured records, 4 documents, 1 schema representation, 10 search
queries, 9 hits resolved, 0 unresolvable.

In-process latency: BLOB upload → searchable 155.0 ms; identity search 2.76 ms;
upload → searchable 28.565 ms; `/v1/responses/adapt` 27.175 ms.

**One correction worth recording.** CASE 7 initially failed with a stale
current-version hit. The cause was the *evaluation harness*, which had never
wired Phase 9's lifecycle registry — so `LIFECYCLE_COMMIT` had nowhere to record
which version was current. Wiring it produced 30/30 with no production change.
Worth stating because it is also a genuine deployment fact: a deployment that
omits the lifecycle service gets exactly that behaviour, and both versions stay
current.

### No single "system accuracy"

The dimensions above are not commensurable. Mapping accuracy (1.0 over 68
labels), schema Recall@1 (0.727 over 22 queries), relevance recall (0.980 over
374 labelled fields), top-5 tier overlap (1.0 over 40 queries) and leakage
counts (0) measure different things against different denominators. Averaging
them would produce a number with no defensible definition, and none is
reported.

## 17. Threats to validity

**Construct validity**

- **Storage fidelity ≠ retrieval accuracy.** Top-5 overlap of 1.0 says tiers
  agree, not that answers are right. Kept separate throughout.
- **Byte-based context reduction is a proxy for token cost.** No tokenizer
  ships with this project.
- **In-process latency is not production latency.** Phases 4, 5, 6, 7, 9, 10,
  11 and 12 measure an in-process tier with no network. Only the Phase 12
  storage benchmark ran against live Qdrant.

**Internal validity**

- **Single annotator.** The Phase 14 labels were written by the component
  author. Labels were fixed from each question *before* any method ran, which
  removes outcome-driven labelling but not the author's own framing.
- **Self-authored benchmarks.** The mapping corpus was built by the person who
  wrote the matcher. The 8 negatives and 18 alias-independent labels are
  mitigations, not a substitute for an independent corpus.
- **No inter-annotator agreement** is reported anywhere, because there was only
  one annotator.

**External validity**

- **Every corpus is synthetic.** No production ERP data was used. Real ERP
  schemas are larger, dirtier, and more inconsistently named.
- **Small samples.** 22 schema queries, 14 identity queries, 8 source changes,
  7 sensitivity assignments. For zero-valued correctness gates this is
  informative; for *rates* it is thin.
- **Research scale.** 500 vectors is three or more orders of magnitude below a
  production ERP corpus. Nothing here predicts filter performance at scale,
  particularly with no Qdrant payload indexes.
- **Members 1–3 are fakes.** Integration results describe contract coherence,
  not real interoperability.

**Statistical conclusion validity**

- **No significance testing.** The three-arm Phase 14 comparison reports point
  estimates over 68 cases with no confidence intervals. The differences are
  large (0.500 vs 0.143 context reduction), but no claim of statistical
  significance is made.
- **Ceiling effects.** Several metrics sit at 1.0, which means the corpus
  cannot discriminate above that point.

**Not evaluated at all**

- **No downstream LLM answer-quality study.** Whether halved context improves
  or degrades a final generated answer is unmeasured. The component's claim
  stops at *AI-ready content*.
- **No adversarial evaluation** of the mapping matcher or the SSRF policy
  beyond the enumerated cases.
- **No concurrent-load or multi-tenant testing.**

## 18. Limitations register

Consolidated in
[`FINAL_COMPONENT_TECHNICAL_REPORT.md` §29](FINAL_COMPONENT_TECHNICAL_REPORT.md).
The evaluation-specific ones are §17 above.

## 19. Summary

| Dimension | Headline | Strength |
|---|---|---|
| Mapping | Top-1 1.0, refusal 1.0, coverage 0.8824 | Strong, ceiling-limited |
| Storage fidelity | Top-5 overlap 1.0, cold lossless | Moderate, live Qdrant |
| Multimodal | 0 collisions, 0 leakage | Property-strong, sample-weak |
| Identity retrieval | 0 wrong identities over 14 queries | Property-strong, sample-weak |
| Resolution | 58/58 resolved | Strong |
| Automatic indexing | 0 manual calls, 6/6 jobs | Strong |
| Schema retrieval | R@1 0.727, R@3 0.909, MRR 0.811 | Moderate, failures kept |
| Remote assets | 0 private contacts, 0 URL leakage | Policy-strong, no network |
| Synchronisation | 0 missed, 0 wrong-version, 5 s + 0.877 ms | Property-strong |
| Sensitivity | 0 downgrades, 0 plaintext findings | Property-strong |
| Integration | 21/21, 9 gates zero | Strong for contracts |
| Response adaptation | recall 0.980, context −50.0%, p95 24.05 ms | Strong design, single annotator |
| Final end-to-end | 30/30, 16 gates zero | Strong coverage, in-process |

**Research evidence readiness: READY WITH LIMITATIONS.** The evidence supports
every claim the component makes, at the strength stated for each — no claim
rests on a measurement that was not taken, and no failed result was repaired
after the fact.
