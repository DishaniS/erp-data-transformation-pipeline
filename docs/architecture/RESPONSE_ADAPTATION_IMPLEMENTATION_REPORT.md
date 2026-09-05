# Phase 14 Implementation Report

**ERP-Aware Adaptive Multimodal Response Transformation**

| | |
|---|---|
| Component | ERP-Aware Data Transformation Pipeline (Member 4) |
| Student | IT22267290 |
| Project | R26-SE-034 |
| Package | `src/erp_pipeline/response_adaptation/` |
| Engine version | `1.0` |
| Status | **COMPLETE** |

---

## 1. Objective

Turn an **already-executed** ERP API response into context a language model can
use, without a language model being involved in any of the decisions.

The boundary is the design, so it is stated before anything else:

| | |
|---|---|
| Member 2 | decides which ERP API to call, and calls it |
| **Member 4 (this phase)** | **receives what came back and makes it usable** |
| Member 3 | puts the result in front of a model |

This phase never chooses an endpoint, never issues an ERP request, never
retries one, and never generates prose.

The problem it solves is concrete. An ERP returns:

```json
{"result": {"inv_no": "INV-204", "cust_ref": "CUS-17", "total_amt": "45000.00",
            "curr": "LKR", "approval_status": "A", "row_version": 7,
            "etl_batch_id": "B-99", "created_by": "svc_acct"},
 "success": true, "server_time": "2026-08-22T09:00:00Z"}
```

A model asked "how much is this invoice for?" should receive:

```json
{"invoice_id": "INV-204", "amount": "45000.00"}
```

Three things had to happen: the envelope had to be recognised as an envelope,
the vendor abbreviations had to become canonical ERP names, and the question had
to drive which fields survived.

---

## 2. Final architecture

```
  ResponseEnvelope  (query + raw response + declared content type)
          |
          v
     detector.py            magic bytes > payload shape > content type > fallback
          |
   +------+---------------------------------+
   v                                         v
STRUCTURED                          IMAGE / DOCUMENT / BINARY
   |                                         |
structured.py  unwrap envelope        assets.py
   |           infer schema  ........> ingestion.image_ingestion + ocr
   |           map           ........> ingestion.pdf_ingestion + ai.chunking
   |           transform                     |
   v                                         v
relevance.py   score against the query   AdaptedAsset
   |           (the new mechanism)      (text + metadata, NEVER bytes)
   v                                         |
formatter.py   budgets, truncation,          |
   |           sensitivity                   |
   +---------------+-------------------------+
                   v
            AdaptedResponse
```

`service.py` orchestrates; `models.py` holds every contract; `errors.py` types
every failure; `evaluation.py` holds the labelled dataset, the baselines and the
metrics.

**Only `relevance.py` is a new algorithm.** Everything else reuses machinery the
pipeline already had and already tests heavily.

---

## 3. Files added and changed

### Added — `src/erp_pipeline/response_adaptation/`

| File | Responsibility |
|---|---|
| `__init__.py` | Package exports |
| `models.py` | Every input/output contract, budgets, policy, metrics, provenance |
| `errors.py` | Typed error hierarchy; asset errors are explicitly non-fatal |
| `detector.py` | Response classification from bytes, structure and content type |
| `structured.py` | Envelope unwrapping, response schema inference, ERP mapping |
| `relevance.py` | **Deterministic query-to-field relevance scoring** |
| `formatter.py` | Payload assembly, budgets, truncation, sensitivity |
| `assets.py` | Images, PDFs, unsupported binary, SSRF-protected URL fetching |
| `service.py` | `ResponseAdaptationService.adapt()` |
| `evaluation.py` | Labelled dataset, RAW/GENERIC baselines, metrics, ablation |

### Added — elsewhere

| File | Responsibility |
|---|---|
| `src/erp_pipeline/api/routers_adaptation.py` | `POST /v1/responses/adapt` |
| `tests/erp_pipeline/response_adaptation/` (4 files) | 106 tests |
| `scripts/evaluate_response_adaptation.py` | Evaluation runner |
| `artifacts/response_adaptation_evaluation.json` | Measured evidence |
| `docs/adaptive_response_transformation.md` | Design document |

### Changed

| File | Change |
|---|---|
| `src/erp_pipeline/api/schemas.py` | Added 4 request/response models. Additive only. |
| `src/erp_pipeline/api/main.py` | Registered `responses_router`. Two lines. |
| `README.md` | Feature bullet, endpoint row, key-file rows |

**Phase 14 modified no pre-existing file other than the three listed above**,
and changed none of their existing behaviour. No pre-existing test was changed,
weakened or deleted by this phase. (Other files show as modified in `git status`
from the earlier consolidation and stabilisation tasks, which are not part of
Phase 14.)

---

## 4. Input contract

`ResponseEnvelope`:

| Field | Type | Notes |
|---|---|---|
| `query` | `str \| None` | The user's question. Omitted means no field is dropped for irrelevance. |
| `source_system_id` | `str` | Which ERP; used for the canonical record id. |
| `endpoint` | `str \| None` | Entity hint and provenance. |
| `http_status` | `int \| None` | Recorded. |
| `content_type` | `str \| None` | The server's claim. Bytes still outrank it. |
| `body` | `Any` | Decoded JSON body. |
| `raw` | `bytes \| None` | Bytes, for an image or PDF response. |
| `headers` | `Mapping[str, str]` | Allow-listed before storage. |
| `asset_urls` | `Sequence[AssetReference]` | Never fetched unless enabled. |
| `entity_hint` | `str \| None` | Overrides the endpoint-derived entity name. |
| `sensitivity` | `SensitivityLevel` | **Consumed, never inferred.** |

---

## 5. Output contract

`AdaptedResponse`:

| Field | Meaning |
|---|---|
| `response_type` | `structured` / `image` / `document` / `binary` / `unknown` |
| `entity_type` | Canonical entity, or `None` when the model has no vocabulary for it |
| `llm_ready` | **The payload to put in front of a model** |
| `assets` | Adapted images/documents/binaries. Text and metadata, never bytes. |
| `provenance` | Source, endpoint, status, engine version, config fingerprint, allow-listed headers, canonical record id |
| `transformation` | Measured input/output bytes and fields, latency, truncation |
| `report` | Detection, entity confidence, **per-field decisions with their signals**, removal counts, wrapper path |
| `warnings` | Everything that went partly wrong |
| `success` | False only when nothing usable could be produced |
| `is_partial` | Succeeded, but something inside it did not |

Ratios on `transformation` are **derived properties**, never supplied — a caller
cannot report a reduction that did not happen.

---

## 6. Structured response adaptation

### Envelope unwrapping — structural, not a name list

> A mapping is an envelope when exactly **one** of its values is a record (or a
> list of records) and every other value is a scalar.

This catches `{"result": {...}, "success": true}` and `{"data": [...],
"count": 12}` without knowing either word, and correctly **refuses** to unwrap
`{"invoice": {...}, "customer": {...}}` — two business objects side by side,
where picking one would be a guess. A hint list of common metadata keys breaks
ties only.

A wrapper key never becomes the ERP entity. The path is reported separately
(`report.wrapper_path`).

### Schema inference — reuse, not a third engine

A live API response has no catalogued schema, so one is observed from the
payload using `api_specs.inference` — which itself already reuses the MongoDB
document-inference engine. It is marked `SchemaOrigin.INFERRED` and **never
published to the catalog**: it describes one response, not a source system.

### Mapping — the existing chain, unchanged

```
inferred SourceSchema -> MappingService       -> MappingProfile
SourceRecord          -> TransformationService -> CanonicalRecord
```

Writing a second mapping engine here would have forked the ERP knowledge that
makes the whole component "ERP-aware" in the first place.

Verified behaviour: `inv_no`→`invoice_id`, `cust_ref`→`customer_id`,
`total_amt`→`amount` (converted to `Decimal`), `curr`→`currency`,
`approval_status`→`status`; `row_version` receives no canonical target;
`canonical_record_id` = `erp:finance_erp:invoice:inv-204`, deterministic across
runs.

---

## 7. Query relevance algorithm

**The phase's research contribution.**

### Not an LLM, and why

Asking a model "which fields matter?" would be non-deterministic between runs,
unattributable to any rule, and impossible to defend in an evaluation. Every
score here is a weighted sum of four bounded signals, and every field carries the
signals that produced it.

### The signals

| Signal | Weight | Measures |
|---|---|---|
| `alias` | 0.45 | the query names the **canonical concept** this field maps to |
| `name` | 0.30 | the query names the **source field** literally |
| `entity` | 0.15 | the field belongs to the entity the response is about |
| `identity` | 0.10 | the field identifies the record |

`score = Σ(signal × weight) / Σ(weight)`, in [0, 1].

`alias` is heaviest because it is what makes the mechanism ERP-aware rather than
lexical.

### Four decisions that shaped the result

1. **Asymmetric matching.** The overlap coefficient — shared tokens over the
   *smaller* set. A question is a sentence and a field name is one or two words;
   symmetric Jaccard would punish every field for the length of the question.

2. **The entity noun is discounted.** An invoice response has aliases
   `invoice_amount`, `invoice_date`, `invoice_status`. Left in, "invoice" in a
   question half-matched all three and the lexical signal stopped discriminating.
   Entity membership is already measured once by the `entity` signal.

3. **Structural suffixes are discounted.** `merchant_name` is about a merchant;
   `_name` says only that the value is a label.

4. **A question-intent lexicon (31 entries).** "How much is this invoice for"
   contains no form of the word "amount". The gap is between a question and a
   schema, not between two schemas, so field-name normalisation can never close
   it. Kept separate from the mapping vocabulary because declaring "much" a
   synonym of "amount" globally would corrupt every schema mapping in the
   pipeline.

   **This lexicon is an authored component of the method, not a result.** Its
   size is reported in the artifact.

### The threshold is derived

A field that maps cleanly onto the queried entity but that the question never
mentions scores exactly `entity / total` = **0.15**. Any threshold at or below
that floor admits every well-mapped field regardless of the question, making
query relevance decorative. The default is **0.25** — above the floor, well
below any real alias or name evidence (≥ 0.5 coverage is worth 0.225 alone).

### Preservation rules

- **Mandatory identity fields** are selected *before* the budget is consulted.
  They still count against it.
- **Identity inference** for entities outside the canonical model: a name ending
  in `_id`/`_no`/`_number` **before** abbreviation expansion. Checking the raw
  form matters — the expansion maps `code` and `key` onto `id`, which would make
  `currency_code` look like a record key. Returns `None` rather than guessing;
  an identity inferred from field *order* would be wrong the moment a response is
  serialized alphabetically, which is how these arrive.
- **Broad questions step aside.** "Give me the full customer record" names no
  field; scoring it field by field answers a question nobody asked.
- **No-signal fallback.** A question matching nothing keeps everything, marked
  `no_relevance_signal`. Returning only the identity field would be a
  confidently wrong answer indistinguishable from an empty record. This trades
  context, which is bounded and measurable, against recall, which is not
  recoverable downstream. The marker means the evaluation counts an abstention
  as an abstention rather than crediting it as a successful selection.

### Determinism

Ordering is `(not mandatory, -score, source_field)`. The name tie-break is what
stops two equally-scored fields swapping places between runs — an evaluation
cannot be reproducible without it. Two scorer instances with the same
configuration always agree; this is asserted by test.

---

## 8. Image, PDF, URL and binary handling

Nothing but text, dimensions, page counts and hashes leaves this phase. **Raw
bytes are never placed in the output contract** — asserted by test.

| Kind | Extracted via | `llm_directly_readable` |
|---|---|---|
| image | `ingestion.image_ingestion` + `ingestion.ocr` | **true** |
| document | `ingestion.pdf_ingestion` + `ai.chunking` | false |
| unsupported binary | described only | false |
| refused URL | placeholder recorded | false |

For images, OCR text is carried **alongside** the image, not instead of it, so a
caller with a vision-capable model is not forced to accept a lossy transcription
of a document it could have read.

An unsupported or corrupt payload is **not an error**. A response whose JSON
adapted correctly is not discarded because it also carried a ZIP; the caller gets
a truthful description saying the content is unavailable, which is what stops a
model inventing its contents.

Two defects found and fixed during implementation:

- **Windows cleanup masking the real error.** A parser that fails to open a file
  may still hold a handle; `unlink` in a `finally` then raised `PermissionError`,
  which *replaced* the extraction error that actually mattered — turning "this
  PDF is corrupt" into "permission denied".
- **Unannounced truncation.** The PDF ingestor applies its own character budget
  before this code sees the text. When it truncated, the asset reported
  `truncated=False` — a shortened document presented as complete, breaking the
  one invariant the flag exists to enforce.

---

## 9. SSRF and security controls

An asset URL is chosen by the **ERP system**, not by us. Fetching it
unconditionally would make this service a request proxy inside the network
perimeter.

All applied **before a socket is opened**:

| Control | Default |
|---|---|
| Fetching enabled | **off** — the safe config is what you get by doing nothing |
| HTTP client shipped | **none** — the fetcher is injected, so importing this code can never cause a request |
| Scheme allow-list | `https` only (this is what refuses `file://`, `ftp://`, `gopher://`) |
| Port allow-list | 80, 443, 8080, 8443 |
| Host allow-list | optional; the strongest control |
| Credentials in URL | refused |
| Address checking | **every** resolved address, not just the first |
| Blocked ranges | loopback, RFC1918, link-local (cloud metadata), multicast, reserved, unspecified — **including IPv4-mapped IPv6 forms** |
| Redirects | re-validated, not followed blindly |
| Size / timeout | enforced |

Each refusal carries a **named rule**, so an operator learns which setting to
change. A refusal never fails the whole adaptation.

**Header handling.** Provenance headers are **allow-listed**, never copied
wholesale. A deny-list must anticipate every header that might carry a secret and
gets it wrong the first time an ERP invents `X-Vendor-Session`. Asserted by test:
`Authorization`, `X-Api-Key` and `Cookie` values do not appear anywhere in the
serialized output.

**No test in this phase touches the network.** Resolver and fetcher are injected.

**Sensitivity is consumed, never inferred.** No value inspection, no guessing.

---

## 10. Reuse of existing modules

| Reused | For |
|---|---|
| `api_specs.inference` | JSON payload → observed field structure |
| `mapping.MappingService` | source schema → canonical mapping profile |
| `mapping.canonical_model` | the ERP alias vocabulary the `alias` signal reads |
| `mapping.normalization` | one tokenizer for questions and field names alike |
| `transformation.TransformationService` | source record → canonical record |
| `ingestion.detection` | the magic-byte signature table |
| `ingestion.hashing` | content identity |
| `ingestion.image_ingestion`, `ingestion.pdf_ingestion`, `ingestion.ocr` | extraction |
| `ai.chunking` | page-anchored document text |
| `schemas.serialization.to_json_value` | exact `Decimal` rendering, existing convention |
| `api.security`, `api.responses` | auth and error contract, unchanged |

**No second mapping engine. No second inference engine. No second tokenizer. No
second JSON converter.**

---

## 11. `POST /v1/responses/adapt`

One endpoint, deliberately. Splitting it per response type would push the
detection decision onto the caller — who is handing over bytes precisely because
they do not know what those bytes are.

- Binary responses arrive as `body_base64`.
- Per-request budgets via `options`; omitted values keep the deployment's
  configured value rather than resetting to a library default.
- Returns **200 with `partial: true`** when adaptation only partly succeeded. A
  refused asset URL or a truncating budget is not an HTTP error: the fields that
  *did* adapt are still the answer.
- **422** when the request itself cannot be interpreted.
- Inherits the existing mutating-route API-key rule (verified:
  `requires_key("POST", "/v1/responses/adapt", False)` → `True`).
- The route decodes, delegates, and serialises. No adaptation logic.

---

## 12. Test results

### Phase 14 targeted suite

```
tests/erp_pipeline/response_adaptation/  ->  106 passed
```

| File | Covers |
|---|---|
| `test_detection_and_structured.py` | detection order, byte-vs-header conflict, envelope unwrapping (including refusal), leaf counting, schema inference, canonical mapping, deterministic ids, type conversion |
| `test_relevance_and_budgets.py` | signal behaviour, alias-only selection, intent expansion, determinism, mandatory preservation, budget interaction, fallbacks, sensitivity blocking, report capping |
| `test_assets_and_url_safety.py` | 15 parametrised SSRF refusal cases, multi-address DNS, redirect re-validation, real PNG/PDF extraction, no-bytes-in-output, degradation |
| `test_service_and_api.py` | end-to-end adaptation, measured metrics, header redaction, partial success, ablation switch, determinism, HTTP route |

Two failures surfaced on the first targeted run and were resolved honestly:

1. **`test_extracted_text_is_bounded`** — a **real defect**: unannounced
   truncation (§8). Fixed in `assets.py`.
2. **`test_the_canonical_alias_is_what_matches_not_the_string`** — a **wrong
   test premise**. It asserted the `name` signal could not match `cust_ref` for
   the query "customer". It can and should: the pipeline's own abbreviation
   table expands `cust`→`customer`. The test was rewritten to isolate the real
   claim — with the `name` weight set to zero, the canonical vocabulary alone
   still selects the field — which is stronger evidence, not weaker.

### Full regression

```
2943 passed, 63 skipped, 0 failed, 0 errors   in 402.95s
```

| | Baseline (pre-Phase-14) | After Phase 14 |
|---|---|---|
| Collected | 2900 | **3006** (+106, exactly the Phase 14 tests) |
| Passed | 2874 | 2943 |
| Skipped | 26 | 63 |
| **Failed** | **0** | **0** |
| **Errors** | **0** | **0** |

**Zero failures and zero errors.** Collection grew by exactly 106 — the Phase 14
suite and nothing else.

**On the skip delta (26 -> 63).** Three verified facts bound it:

1. Collection grew by exactly **106**, the Phase 14 test count — no test was
   added or removed anywhere else.
2. The Phase 14 suite run in isolation is **106 passed, 0 skipped** — none of
   the additional skips originate in this phase.
3. **0 failed, 0 errors** — no test moved from passing to failing.

A per-test skip-reason breakdown (`pytest -rs`) confirms it. **All 63 skips are
live-service connection failures**, reconciling exactly:

| Reason | Skips |
|---|---|
| MongoDB unreachable at `localhost:27018` | 24 |
| Qdrant unreachable at `localhost:6333` (6 test modules) | 37 |
| Live schema-discovery / drift check could not reach its source | 2 |
| **Total** | **63** |

Not one skip comes from `tests/erp_pipeline/response_adaptation/`, and not one
is code-related. The baseline's lower figure was recorded in an earlier session
with more of that infrastructure running. This is an **environmental**
difference, not a code regression.

---

## 13. Evaluation dataset

**68 labelled cases** across six response families.

| Category | Cases |
|---|---|
| invoice | 24 |
| customer | 14 |
| purchase_order | 11 |
| receipt | 7 |
| document | 6 |
| process_case | 6 |

**149** labelled relevant fields, **225** labelled irrelevant fields.

Response shapes deliberately include the hard cases:

- vendor abbreviations (`inv_no`, `cust_ref`, `total_amt`)
- **SAP-style opaque mnemonics** (`BELNR`, `KUNNR`, `NETWR`, `WAERS`) — the
  hardest honest test of the ERP-awareness claim, since there is no lexical
  similarity to fall back on
- spelled-out names with no envelope
- nested contact blocks (dotted paths)
- list envelopes
- **entities the canonical model does not cover** (process case, policy
  document, receipt) — included rather than quietly left out, because that is
  where the method is weakest

Labels are written against **source** field names, since that is what every
method sees.

**Limitation, stated plainly:** single annotator (the component author). Labels
were written from each question before any method was run, but no
inter-annotator agreement can be reported from one annotator. Payloads are
synthetic — realistic in shape and vocabulary, not drawn from a live ERP.

---

## 14. RAW baseline

The response verbatim. What a system does with no adaptation at all.

Recall is 1.0 by construction — it keeps everything, so it can never drop a
relevant field. It also removes nothing, reduces nothing, and costs the full
payload.

---

## 15. GENERIC baseline

Envelope unwrapped and flattened. No ERP vocabulary, no query awareness. This is
a competent engineer's first attempt.

**It is given the envelope unwrapping deliberately.** Withholding it would make
the baseline a straw man, and unwrapping is not the contribution being claimed.

---

## 16. PROPOSED method

Unwrap → canonical ERP mapping → deterministic query relevance → mandatory
identity preservation → budgets.

---

## 17. Experimental results

All three methods evaluated with **exactly the same** field matcher, leaf-field
counting rule, gold labels, case set and timing method.

| Metric | RAW | GENERIC | **ERP-aware adaptive** |
|---|---|---|---|
| Relevant field recall | 1.0000 | 1.0000 | **0.9799** |
| Cases with perfect recall | 1.0000 | 1.0000 | **0.9559** |
| Irrelevant field removal | 0.0000 | 0.0000 | **0.6089** |
| Field reduction | 0.0000 | 0.1168 | **0.4736** |
| Context reduction (bytes) | 0.0000 | 0.1433 | **0.5004** |
| Adaptation success rate | 1.0000 | 1.0000 | **1.0000** |
| Median latency | 0.0002 ms | 0.0409 ms | **15.83 ms** |
| p95 latency | 0.0004 ms | 0.0763 ms | **24.05 ms** |

Absolute totals: 16,049 input bytes → 8,018 output bytes; 625 input leaf fields
→ 329 output fields; 146 of 149 relevant fields kept; 137 of 225 irrelevant
fields removed.

Per category (proposed method):

| Category | n | Recall | Context reduction |
|---|---|---|---|
| customer | 14 | 1.0000 | 0.4706 |
| receipt | 7 | 1.0000 | 0.4742 |
| document | 6 | 1.0000 | 0.2057 |
| invoice | 24 | 0.9818 | 0.6053 |
| purchase_order | 11 | 0.9583 | 0.5401 |
| process_case | 6 | 0.9231 | 0.4220 |

**The proposed method is worse than both baselines on recall.** That is reported
first rather than buried: it removes 61% of the labelled noise and halves the
serialized context, at the cost of 2% recall — three missed fields across 68
cases. Both baselines achieve perfect recall trivially, by not making a decision.

Two fairness defects were found and fixed before these numbers were accepted:

1. **The field matcher penalised RAW.** RAW never unwraps, so its path to a
   nested contact address is `customer.contact.email`, while the other two
   unwrap first and reach it at `contact.email`. Exact string matching scored
   RAW as *missing fields it plainly contained*. RAW's measured recall rose from
   0.973 to the correct 1.000 once the matcher was unified.
2. **Field counting flattered RAW.** Counting top-level keys credited RAW with a
   70% "field reduction" for handing over an untouched three-key envelope
   wrapping ten leaves. Leaf counting on both sides for every method corrected
   this to the truthful 0.0.

Both corrections moved numbers **against** the proposed method's apparent
advantage.

---

## 18. Ablation

**COMPLETED.** One ablation, isolating the single mechanism this phase
contributes. Unwrapping, canonical mapping and budgets are identical across both
arms, so the difference is attributable.

| Arm | Recall | Irrelevant removed | Field reduction | Context reduction |
|---|---|---|---|---|
| With query relevance | 0.9799 | 0.6089 | 0.4736 | 0.5004 |
| Without query relevance | 1.0000 | 0.0000 | 0.1168 | 0.1673 |

Query relevance accounts for essentially **all** of the context reduction
(0.1673 → 0.5004) and for **all** of the recall loss (1.0 → 0.9799).

The residual 0.1673 without relevance is what unwrapping and canonical mapping
contribute on their own — real, but a third of the total.

---

## 19. Research interpretation

**What the results support.**

Deterministic, ERP-vocabulary-driven field selection removes roughly 61% of
labelled irrelevant fields and halves serialized context while retaining 98% of
relevant fields, with no language model, no learned component, and a fully
explainable per-field decision trail. The ablation shows the query-relevance
mechanism is responsible for the majority of that reduction rather than it being
an artefact of unwrapping.

**What the results do not support.**

- No claim of superiority over an LLM-based selector. None was compared.
- No claim about downstream answer quality. Context reduction is measured;
  whether it improves a model's answers was not tested and would need a
  different experiment.
- No claim of statistical significance. 68 cases, one annotator, synthetic
  payloads.
- Latency (~16 ms median) is measured on a single machine, single process,
  no warm-up excluded.

**The honest framing.** The contribution is that ERP canonical vocabulary,
already built for schema mapping, is *reusable as a query-understanding
resource* — a question about "the customer" reaches `cust_ref` and `KUNNR`
through the same alias table that maps them for storage. That reuse is what
makes the method ERP-aware rather than a generic keyword filter, and it is
measurable precisely because it is deterministic.

---

## 20. Limitations

1. **Three recall failures**, each named and classified in the artifact:

   | Case | Missed | Cause |
   |---|---|---|
   | `sap-04` | `BELNR` | insufficient ERP vocabulary — SAP mnemonics are not in the canonical alias lists, and `BELNR` has no `_id`/`_no` suffix for the identity heuristic |
   | `po-05` | `supplier_no` | insufficient query vocabulary — "from whom"; the lexicon has "who" but not its objective inflection |
   | `proc-02` | `resource` | insufficient query vocabulary — "who" does not reach process-mining's actor field |

   **None were fixed after the fact.** Extending the vocabulary in response to
   observed failures would be fitting it to the test set, and the resulting
   number would not mean anything. They are reported instead.

2. **Single annotator**, no inter-annotator agreement.
3. **Synthetic payloads.**
4. **The intent lexicon is hand-authored** (31 entries) — part of the method,
   not an emergent result.
5. **Characters are a proxy for tokens.** Monotonic, but not a token count. This
   project ships no tokenizer and adding one would mean shipping a model's
   vocabulary to make a budget decision.
6. **Only the first record of a collection is adapted**, with a warning.
7. **No mapping-profile caching.** ~16 ms median latency is dominated by
   rebuilding an inferred schema and mapping profile per response. Caching per
   endpoint is the obvious optimisation; it was not implemented, because it was
   not in scope.
8. **The canonical model covers three entities** (invoice, customer, purchase
   order). Process cases, documents and receipts run the passthrough path and
   rely on the identity heuristic.

---

## 21. Member 2 integration contract

Member 2 executes the ERP call and POSTs the result:

```
POST /v1/responses/adapt
X-API-Key: <key>

{
  "query": "How much is invoice INV-204 for?",
  "source_system_id": "finance_erp",
  "endpoint": "/api/invoices/INV-204",
  "http_status": 200,
  "content_type": "application/json",
  "body": { ...the ERP response, exactly as received... }
}
```

**Member 2 must:**

- send the response **exactly as received**, including the envelope — unwrapping
  is this phase's job and doing it early loses the wrapper path
- send the user's `query` when field selection is wanted; omitting it keeps
  every field
- send `body_base64` instead of `body` for image or PDF responses
- state `sensitivity` when the data carries a classification

**Member 2 must not:**

- expect this service to call an ERP system
- send credentials in `headers` expecting them to be used — they are dropped
- treat `success: true` as "everything worked"; check `partial` and `warnings`
- expect an asset URL to be fetched unless the deployment has enabled fetching
  and configured a fetcher

**Member 3 consumes** `llm_ready` as the model context, and `report` /
`provenance` when the answer needs to be explained or traced.

---

## 22. Final completion status

| Requirement | Status |
|---|---|
| Response type detection (5 kinds, magic bytes + structure + content type) | COMPLETE |
| Structured adaptation reusing `mapping`/`transformation`/`schemas` | COMPLETE |
| Nested/envelope unwrapping by structure, not names | COMPLETE |
| Query-aware, deterministic, explainable field selection | COMPLETE |
| Mandatory identity/provenance preservation | COMPLETE |
| Configurable budgets with explicit truncation | COMPLETE |
| Sensitivity-aware output (consumed, never inferred) | COMPLETE |
| Image bytes via existing ingestion + OCR | COMPLETE |
| SSRF-protected URL fetching | COMPLETE |
| PDF via existing ingestion + chunking | COMPLETE |
| Safe unsupported-binary fallback | COMPLETE |
| Measured transformation metrics | COMPLETE |
| `POST /v1/responses/adapt` | COMPLETE |
| 60–100 case labelled dataset | COMPLETE (68) |
| RAW / GENERIC / PROPOSED comparison | COMPLETE |
| Evaluation script and artifact | COMPLETE |
| Query-relevance ablation | COMPLETE |
| Documentation | COMPLETE |

**Phase 14: COMPLETE.**

Full suite: **2943 passed, 63 skipped, 0 failed, 0 errors.** Phase 14 targeted
suite: **106 passed, 0 skipped.** Details and the skip-delta explanation in §12.

The Phase 13 OpenAPI artifact (`artifacts/openapi_contract_snapshot.json`) is regenerated
from the live application by an existing contract test, so it now contains
`/v1/responses/adapt` and the `adaptResponse` operation. That test also enforces
operation-id uniqueness and asserts the artifact carries no planted secrets; both
still pass.

No secrets, credentials, `.env` values, database passwords, Qdrant keys, large
generated files or local datasets were added or staged. Nothing was committed.
