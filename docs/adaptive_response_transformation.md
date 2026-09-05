# ERP-Aware Adaptive Multimodal Response Transformation (Phase 14)

## 1. Purpose

Phases 3–12 prepare ERP data for retrieval. Phase 14 closes the loop at the
other end.

When an ERP API has already been called and has already answered, something has
to turn what came back — a wrapped JSON envelope full of vendor abbreviations, a
scanned PDF, a photographed receipt — into context a language model can actually
use. That is this phase, and only that.

**What this phase does not do**, stated first because the boundary is the
design:

- It does **not** decide which ERP API to call. That is Member 2.
- It does **not** execute an ERP request, retry one, or authenticate to one.
- It does **not** generate an answer. That is Member 3.
- It does **not** use a language model to make any of its own decisions.

The input is a response that has already happened.

## 2. Architecture

```
  Member 2 executes the ERP call
            |
            v
     ResponseEnvelope        query + raw response + declared type
            |
            v
        detector             magic bytes > payload shape > content type
            |
    +-------+--------------------------------+
    v                                        v
 STRUCTURED                          IMAGE / DOCUMENT / BINARY
    |                                        |
 unwrap envelope                     ingestion.image_ingestion
    |                                ingestion.pdf_ingestion
 infer schema  (api_specs.inference)         + ai.chunking
    |                                        |
 map           (mapping.MappingService)      v
    |                                  AdaptedAsset
 transform     (transformation.Service)  (text + metadata, NEVER bytes)
    |                                        |
 relevance     <- the new mechanism          |
    |                                        |
 budgets                                     |
    +----------------+-----------------------+
                     v
              AdaptedResponse
                     |
                     v
     Member 3 puts it in front of a model
```

Only the relevance scorer is new. Everything else reuses machinery the pipeline
already had and already tests heavily — which is the point: an API response is
absorbed by the **same** ERP mapping engine that absorbs a CSV or a MongoDB
collection, rather than by a parallel one written for HTTP.

## 3. Response detection

Evidence is considered in a fixed order, strongest first:

| Order | Evidence | Why it ranks there |
|---|---|---|
| 1 | magic bytes | what the payload *is* |
| 2 | decoded payload structure | what the parsed body *is* |
| 3 | declared `Content-Type` | what the server *says* |
| 4 | explicit fallback | an honest `unknown`, never a guess |

Bytes outrank the declaration for the same reason file ingestion already refuses
to trust a filename. A legacy ERP that labels a PDF `application/json` is not
hypothetical, and believing the label would send binary into a JSON parser. When
the two disagree the bytes win **and the disagreement is reported**, so a caller
learns their ERP is mislabelling rather than silently receiving a different type
than they asked for.

A response has no filename, so extensions play no part at all.

## 4. Envelope unwrapping

`{"result": {...}, "success": true}` is not an ERP entity called `result`. The
business record is one level down, and which level is a **structural** question
that no schema declares.

The rule is structural rather than a list of vendor wrapper names:

> A mapping is an envelope when exactly **one** of its values is a record (or a
> list of records) and every other value is a scalar.

That catches `{"result": {...}, "success": true}` and `{"data": [...],
"count": 12}` without knowing either word, and it correctly **refuses** to
unwrap `{"invoice": {...}, "customer": {...}}`, where both values are records
and picking one would be a guess. A hint list of common metadata keys exists
only to break ties, never to decide alone.

The wrapper path is reported on the result (`report.wrapper_path`) so provenance
can state where the data was found, instead of presenting a nested object as if
it had been the whole body.

## 5. Structured adaptation

A live API response has no catalogued schema, so one is **observed** from the
payload using `api_specs.inference` — the same engine that describes a Postman
example or a MongoDB document. It is marked `SchemaOrigin.INFERRED` and is never
published to the catalog: it describes one response, not a source system.

From there the response goes through the existing chain unchanged:

```
inferred SourceSchema -> MappingService  -> MappingProfile
SourceRecord          -> TransformationService -> CanonicalRecord
```

So a response arrives canonicalised:

```json
{"result": {"inv_no": "INV-204", "cust_ref": "CUS-17",
            "total_amt": "45000.00", "curr": "LKR",
            "approval_status": "A", "row_version": 7}}
```

becomes

```json
{"invoice_id": "INV-204", "customer_id": "CUS-17",
 "amount": "45000.00", "currency": "LKR"}
```

with `entity_type: "invoice"`, `wrapper_path: ["result"]`, and a deterministic
`canonical_record_id` of `erp:finance_erp:invoice:inv-204`.

Note that `amount` arrives as a converted `Decimal` rendered exactly, not as the
original string — type conversion is the transformation layer's job and it still
runs. And `row_version` is gone, which is the next section.

Two ERP systems spelling the same concept `cust_ref` and `KUNNR` both emit
`customer_id`. A field the canonical model has no word for is passed through
under its own source name rather than being lost.

## 6. Query-aware field selection

**This is the phase's new mechanism.** Nothing in the pipeline previously scored
a field against a natural-language question.

### Why not an LLM

Asking a model "which of these fields matter?" would be easy and would make the
result unmeasurable: non-deterministic between runs, unattributable to any rule,
and impossible to defend in an evaluation. Every score here is a weighted sum of
four bounded signals, and every field carries the signals that produced it, so a
reviewer can read *why* `row_version` was dropped.

### The four signals

| Signal | Weight | What it measures |
|---|---|---|
| `alias` | 0.45 | the query names the **canonical concept** this field maps to |
| `name` | 0.30 | the query names the **source field** literally |
| `entity` | 0.15 | this field belongs to the entity the response is about |
| `identity` | 0.10 | this field identifies the record |

`alias` is heaviest because it is what makes the mechanism ERP-aware rather than
lexical: a question about "the customer" matches `cust_ref` because the canonical
model states that this is one way ERP systems spell `customer_id`.

### Design decisions that matter

**Asymmetric matching.** Each lexical signal measures the overlap coefficient —
shared tokens over the *smaller* of the two token sets. A question is a sentence
and a field name is one or two words; symmetric Jaccard similarity would punish
every field for the length of the question that asked about it.

**The entity noun is discounted.** An invoice response has aliases like
`invoice_amount`, `invoice_date` and `invoice_status`. Left in, the word
"invoice" in a question would half-match all three at once and the lexical
signal would stop discriminating. Entity membership is already measured, once,
by the `entity` signal; counting it again would be the same evidence paid for
twice.

**Structural suffixes are discounted.** `merchant_name` is about a merchant;
`_name` says only that the value is a label. Without this, "which merchant"
covers one of `{merchant, name}` and scores 0.5 for a field it named exactly.

**A question-intent lexicon.** "How much is this invoice for" contains no form
of the word "amount", and no amount of field-name normalisation will connect
them — the gap is between a question and a schema, not between two schemas. A
small hand-authored table maps question phrasings onto ERP concepts
(`how much` → amount/total/price, `who` → customer/supplier/name, `overdue` →
due/date/status). It is kept separate from the mapping vocabulary because
declaring "much" a synonym of "amount" globally would corrupt every schema
mapping in the pipeline.

This lexicon is an **authored component of the method**, not an emergent result,
and the evaluation artifact reports its size.

**The threshold is derived, not picked.** A field that maps cleanly onto the
queried entity but that the question never mentions scores exactly
`entity / total` = 0.15. A threshold at or below that floor would admit every
well-mapped field regardless of the question, making query relevance decorative.
The default sits at 0.25 — above the floor, comfortably below any real alias or
name evidence.

### Preservation rules

**Mandatory identity fields** are selected before the budget is consulted. An
answer nobody can trace back to a record is not an answer. They still *count*
against the budget, so they shrink what is left rather than being free.

For entities the canonical model does not cover — a process case, a policy
document, a receipt — there is no `is_identifier` flag to consult. The engine
infers one from a name ending in `_id` / `_no` / `_number` **before** abbreviation
expansion (checking the raw form matters: the expansion maps `code` and `key`
onto `id`, which would make `currency_code` look like a record key). If no name
says so, it returns `None` rather than guessing — an identity invented from field
*order* would be wrong the moment a response is serialized alphabetically, which
is exactly how these arrive.

**Broad questions step aside.** "Give me the full customer record" names no
field, and scoring it field by field answers a question nobody asked. An explicit
term list (`everything`, `full`, `whole`, `details`, …) turns selection off for
that request.

**The no-signal fallback.** If a question matches nothing in the response, the
engine keeps everything rather than returning only the identity field. Returning
one field would be a confidently wrong answer that a caller cannot distinguish
from a genuinely empty record. This is the conservative failure: it costs
context, which is bounded and measurable, instead of costing recall, which is
not recoverable downstream. Every field in such a result is marked
`no_relevance_signal`, so the evaluation counts an abstention as an abstention
rather than crediting it as a successful selection.

## 7. Budgets and truncation

Relevance decides what is **worth** sending. A budget decides what **fits**.
They are separate steps because they fail differently: a field dropped for
irrelevance is a judgement that can be wrong, while a field dropped for space is
an arithmetic fact — and the evaluation must be able to attribute a missed field
to the right cause.

| Budget | Default | Behaviour when exceeded |
|---|---|---|
| `max_fields` | 24 | lowest-scoring fields dropped, reason recorded |
| `max_output_characters` | 8000 | fields removed from the end of the relevance order |
| `max_value_characters` | 2000 | the value is clipped with a visible marker |

**Characters, not tokens.** This project ships no tokenizer, and adding one would
mean shipping a model's vocabulary to make a budget decision. Characters are the
honest available proxy: exact, dependency-free, and monotonic in tokens.

**Every cut is announced.** A silently shortened payload is worse than a long
one, because a model given half a record has no way to know it. Numbers are never
clipped — `45000.00` cut to `450` is not a shorter amount, it is a wrong one.

Budget removals are written back into the decision report, so a report can never
claim a field was selected while the payload lacks it.

## 8. Sensitivity

**Consumed, never inferred.** The engine reads the classification the response
already carries and applies the caller's policy to it. It does not examine values
to decide whether they look sensitive; guessing would produce a classification
nothing else in the pipeline agrees with.

When a response's level is blocked, the payload is withheld but the field *names*
are still reported. A caller learns that data exists and was withheld, which is a
different fact from it not existing.

## 9. Images, documents and binary

Nothing but text, dimensions, page counts and hashes leaves this phase. **Raw
bytes are never placed in the output contract** — that would push a base64 blob
through the API layer, the logs and the LLM context, which is the exact cost this
phase exists to remove.

| Kind | Extracted via | `llm_directly_readable` |
|---|---|---|
| image | `ingestion.image_ingestion` + `ingestion.ocr` | **true** — a model can take it as-is |
| document (PDF) | `ingestion.pdf_ingestion` + `ai.chunking` | false — what reaches the model is the text |
| unsupported binary | nothing; described only | false |
| refused URL | nothing; placeholder recorded | false |

For images, OCR text is carried **alongside** the image rather than instead of
it, so a caller with a vision-capable model is not forced to accept a lossy
transcription of a document it could have read.

An unsupported binary is **not** an error. A response whose JSON adapted
correctly should not be discarded because it also carried a ZIP attachment; the
caller receives a truthful description saying the content is unavailable, which
is what stops a model inventing its contents. The same applies to a corrupt image
or an encrypted PDF: the asset degrades to `unsupported_binary` with the reason
recorded.

## 10. Outbound URL safety (SSRF)

An asset URL is chosen by the **ERP system**, not by us. Fetching it
unconditionally would turn this service into a request proxy sitting inside the
network perimeter — the classic SSRF position, where `http://169.254.169.254/`
returns cloud credentials and `http://127.0.0.1:5432/` reaches a database that
trusts local connections.

Controls, all applied **before a socket is opened**:

- **Fetching is disabled by default.** The safe configuration is the one an
  operator gets by forgetting to configure anything.
- **No HTTP client ships with the package.** The fetcher is injected, so
  importing this code can never cause a request, and "no fetcher configured" is a
  refusal rather than an accidental fetch.
- Scheme allow-list (`https` only by default) — this is also what refuses
  `file://`, `ftp://` and `gopher://`.
- Port allow-list (80, 443, 8080, 8443).
- Optional host allow-list, the strongest control and the one a production
  deployment should use.
- Credentials in the URL are refused.
- **Every** resolved address is checked, not just the first — a DNS entry mixing
  a public and a loopback address would otherwise pass validation and then
  connect to whichever the OS chose.
- Loopback, RFC1918 private space, link-local (cloud metadata), multicast,
  reserved and unspecified ranges are blocked, **including their IPv4-mapped
  IPv6 forms** (`http://[::ffff:127.0.0.1]/` is how a naive check is bypassed).
- Redirects are re-validated, not followed blindly. An allowed host redirecting
  to the metadata endpoint is the standard way an SSRF filter that only checks
  the first URL gets bypassed.
- Size limit and timeout on every fetch.

Each refusal carries a **named rule**, so an operator learns which setting to
change rather than only that the fetch did not happen. A refusal never fails the
whole adaptation: it becomes a warning and a placeholder asset.

No test in this phase touches the network. The resolver and fetcher are both
injected.

## 11. Provenance

Headers are **allow-listed**, never copied wholesale. A deny-list has to
anticipate every header that might carry a secret and gets it wrong the first
time an ERP invents `X-Vendor-Session`. Provenance is stored and logged, so
anything reaching it must have been chosen deliberately.

Provenance records the source system, endpoint, HTTP status, content type,
adaptation time, engine version, **configuration fingerprint**, sensitivity, the
allow-listed headers, the canonical record id and the source entity.

## 12. The endpoint

```
POST /v1/responses/adapt
```

One endpoint, deliberately. Adaptation is one operation with one input and one
output; splitting it per response type would push the detection decision onto
the caller, who is the party least able to make it — they are handing over bytes
precisely because they do not know what those bytes are.

Binary responses arrive as `body_base64`, which is why the single endpoint can
still handle an image or a PDF.

It returns **200 even when adaptation only partly succeeded**. A refused asset
URL or a truncating budget is reported in `warnings` and `partial`, not as an
HTTP error, because the fields that *did* adapt are still the answer the caller
needs. A 422 means the request itself could not be interpreted.

The route is thin: it decodes, delegates, and serialises. It contains no
adaptation logic.

## 13. Partial success

A response can carry perfectly good JSON and an image URL that policy refuses to
fetch. Discarding the JSON over the image would be the wrong trade every time,
so asset failures become warnings on a successful result. `success` is false only
when nothing usable could be produced at all.

## 14. Measured results

Full numbers, per-category breakdowns and per-case detail:
`artifacts/response_adaptation_evaluation.json`.

68 labelled cases across six response families. Three methods, one matcher, one
field-counting rule.

| Method | Relevant recall | Irrelevant removed | Context reduction | p95 latency |
|---|---|---|---|---|
| RAW | 1.0000 | 0.0000 | 0.0000 | 0.0004 ms |
| GENERIC | 1.0000 | 0.0000 | 0.1433 | 0.0763 ms |
| **ERP-aware adaptive** | **0.9799** | **0.6089** | **0.5004** | 24.05 ms |

The proposed method removes 61% of the fields labelled irrelevant and halves the
serialized context, at a cost of 2% recall — three missed fields across 68 cases,
each named and classified in the artifact.

Tests: **106 Phase 14 tests, all passing**. Full suite after Phase 14:
**2943 passed, 63 skipped, 0 failed, 0 errors** (collection grew by exactly 106).

Ablation (query relevance on vs off, everything else identical):

| Arm | Recall | Context reduction |
|---|---|---|
| with query relevance | 0.9799 | 0.5004 |
| without query relevance | 1.0000 | 0.1673 |

Query relevance is responsible for essentially all of the context reduction, and
for all of the recall loss. That is the trade the mechanism makes, stated plainly
rather than averaged away.

## 15. Honest limitations

1. **Recall is not perfect.** Three cases lose a relevant field. Two are query
   vocabulary gaps ("from whom", and "who" not reaching a process `resource`);
   one is an ERP vocabulary gap (SAP mnemonic `BELNR` is not in the canonical
   alias lists and has no `_id`/`_no` suffix for the identity heuristic). None
   were fixed after the fact, because extending the vocabulary in response to
   observed failures would be fitting it to the test set.
2. **Single annotator.** The labels are the component author's. No
   inter-annotator agreement can be reported from one annotator.
3. **Synthetic payloads.** Realistic in shape and vocabulary, but not drawn from
   a live ERP.
4. **The intent lexicon is hand-authored.** It is part of the method, not a
   result, and its size is reported.
5. **Characters are a proxy for tokens.** Monotonic, but not a token count.
6. **Only the first record of a collection is adapted**, with a warning. This
   phase adapts a record, not a result set.
7. **Latency is ~16 ms median**, dominated by rebuilding an inferred schema and
   mapping profile per response. There is no caching of mapping profiles across
   responses from the same endpoint; that is the obvious optimisation and it was
   not implemented.

## 16. Integration contract for Member 2

Member 2 supplies a `ResponseEnvelope` (or the equivalent JSON body):

| Field | Required | Meaning |
|---|---|---|
| `query` | recommended | the user's question; omitted means no field is dropped for irrelevance |
| `source_system_id` | recommended | which ERP; used for the canonical record id |
| `endpoint` | recommended | used as an entity hint and recorded in provenance |
| `http_status` | optional | recorded |
| `content_type` | optional | the server's claim; bytes still outrank it |
| `body` | one of | the decoded JSON body |
| `body_base64` | one of | bytes, for an image or PDF response |
| `headers` | optional | allow-listed before storage; secrets are dropped |
| `asset_urls` | optional | never fetched unless the deployment enables it |
| `entity_hint` | optional | overrides the endpoint-derived entity name |
| `sensitivity` | optional | consumed, never inferred |

Member 2 must **not** send credentials in `headers` expecting them to be used;
they are dropped. Member 2 must **not** expect this service to call an ERP.
