# Hybrid Tiered Vector Storage (Phase 12)

Phase 12 decides **where each embedding lives** and proves the trade-off with
measurements rather than assertions. Phase 11 produces vectors; Phase 12 places
them in HOT, WARM or COLD, moves them when the evidence changes, and can explain
every placement.

All numbers below come from `artifacts/tiered_storage_benchmark.json`, produced
by `scripts/benchmark_tiered_storage.py` against a live Qdrant with 500 real
MiniLM embeddings.

---

## 1. What makes this research rather than folder structure

A tiering system is easy to fake. Three directories named hot, warm and cold,
one `if age > 180: cold`, and the diagram looks the same. Three things separate
this implementation from that:

**Hard constraints are applied before scoring, not as a penalty.** A prohibited
tier is removed from the candidate set entirely. A penalty can always be
outvoted by a large enough cost advantage; removal cannot. This is what makes
"restricted data never leaves the premises" a guarantee instead of a
preference.

**The tiers differ measurably.** WARM is server-verified int8 scalar
quantization with on-disk vectors. COLD is really gzip-compressed and really
AES-256-GCM encrypted. Every claim is read back from the server or the
filesystem, never assumed from the write call succeeding.

**Every measurement is labelled with how it was obtained.** Cold bytes are
`MEASURED`. Qdrant bytes are a `PROXY` with a stated formula. Cost multipliers
are `ESTIMATED` assumptions. They are never presented as the same kind of
number.

---

## 2. The three tiers

| | HOT | WARM | COLD |
|---|---|---|---|
| Backend | Qdrant | Qdrant | encrypted files |
| Precision | float32 | int8 quantized | float32 (lossless) |
| Vectors on disk | no (RAM) | yes | yes |
| Searchable | yes | yes | no (rehydrate first) |
| Verified how | server config read back | server config read back | decrypt and compare |

HOT and WARM are both live Qdrant collections, so their search scores are
commensurable and can be merged into one ranking. COLD is not an index at all —
it is a set of encrypted archives, and reaching it means rehydrating.

---

## 3. Routing

Routing runs in two stages, and the order is the point.

**Stage 1 — prohibition.** Constraints that are not negotiable remove tiers from
consideration: `RESTRICTED` sensitivity forbids any tier whose location is
external; an unexpired retention date, a legal hold, a `LOW_LATENCY`
requirement, or `CRITICAL` business criticality each forbid COLD.

**Stage 2 — scoring.** The surviving tiers are scored on four normalized
factors — recency, access frequency, dormancy and age — with per-tier weights
that sum to 1.0 so scores are comparable across tiers. The highest score wins.

Every decision carries its factor contributions, and the weighted contributions
reconstruct the total exactly (asserted in the tests). An explanation that
cannot be recomputed is decoration, not a reason.

### Hysteresis

Two mechanisms stop a record oscillating between tiers:

- **Minimum residence** — a record that has just moved will not move again for
  `minimum_residence_days` (7), regardless of score.
- **Margins** — a promotion must beat the incumbent tier by `promotion_margin`
  (0.10) and a demotion by `demotion_margin` (0.15). Ties keep the record where
  it is.

### Two calibration traps, both found by measurement

The policy was tuned three times against real routing distributions, and two of
the iterations fixed real defects:

1. **Brand-new records scored COLD.** A record created seconds ago had never
   been read, so dormancy looked infinite and archived it immediately. Fixed by
   bounding dormancy by age — nothing can be dormant longer than it has existed.
2. **Recently-read old records scored COLD.** Age dominated the cold score, so
   a two-year-old record someone read this morning was archived. Fixed by
   reweighting cold toward dormancy (0.65) over age (0.35).

---

## 4. Migration

Migration is ordered so that the source copy is never the casualty of a failure:

1. **Read** the record from its current tier.
2. **Write** it to the destination.
3. **Verify** it arrived — for COLD this decrypts the archive and compares
   components; for HOT/WARM it confirms presence and dimension.
4. **Retire** the source copy. Only now.
5. **Commit** the state change, guarded by the record's version.
6. **Audit** the transition.

If any step before 4 fails, the source is untouched and `MigrationError` carries
`source_intact=True`. A record can be in two tiers briefly; it can never be in
none.

**Why HOT/WARM verification is structural rather than exact.** WARM stores int8
quantized vectors by design, so comparing components exactly against the source
would fail on correct data. COLD is lossless, so its verification is exact — and
that is the strongest check in the engine.

### Concurrency

Tier state carries a `version`. The mutator (`with_tier`) increments it and the
store verifies it on write. Two schedulers acting on the same record means one
receives `ConcurrencyConflictError` rather than both writing — which is how a
vector ends up in two tiers or none.

---

## 5. Hybrid search

`HybridVectorStore.search()` queries HOT and WARM, merges by score, and
deduplicates by representation id. COLD is excluded unless `include_cold=True`,
because searching it means building a temporary index over decrypted archives —
a cost that should never be silent.

The store refuses to construct if HOT and WARM disagree on dimension: merging
scores from vectors of different dimension produces a ranking that looks fine
and means nothing.

Cold deep search builds a throwaway collection and deletes it in a `finally`
block. A leaked temporary index is a slow disaster, so the live test asserts the
server's collection list is unchanged afterwards.

---

## 6. Benchmark results

500 records, five ERP entity types, 40 hand-labelled queries, real
`all-MiniLM-L6-v2` embeddings (384-d, measured as already L2-normalized).

### How COLD is searched

Encrypted archives are never searched in place. The cold retrieval path is:

```
encrypted archive corpus
        -> read
        -> decrypt / decompress / deserialize
        -> populate an isolated temporary Qdrant collection
        -> run the SAME 40 queries
        -> destroy the temporary collection
```

All three tiers are therefore evaluated on the identical 500 vectors, the
identical 40 labelled queries, the same dimension and the same distance metric.

### COLD rehydration cost (one-time, for the whole 500-record corpus)

| Stage | Time |
|---|---|
| Archive read (I/O) | 103.06 ms |
| Decrypt + decompress + deserialize | 255.88 ms *(derived)* |
| Temporary index population | 8976.74 ms |
| **rehydration_total_ms** | **9335.69 ms** (18.67 ms/record) |

The derived row is full-rehydrate elapsed minus pure-read elapsed, with the page
cache warmed by an untimed pass first so both terms see the same cache state.

**Index population dominates — 96% of the total.** Decryption is not what makes
cold access expensive; rebuilding the index is.

### Latency

| Operation | Median | p95 |
|---|---|---|
| HOT search | 11.01 ms | 24.51 ms |
| WARM search | 16.45 ms | 33.20 ms |
| COLD search, after rehydration | 15.35 ms | 34.99 ms |
| COLD single-record rehydration | 14.73 ms | 23.45 ms |

**COLD total access latency** is deliberately reported as two terms, never one:

- one-time preparation: **9335.69 ms**
- per query thereafter: **15.35 ms**
- so the first query costs ~**9351 ms**, and every subsequent query ~15 ms

Once rehydrated, COLD queries at roughly HOT/WARM speed — but reaching that
state costs nine seconds for this corpus. **COLD is not equivalent to an
always-online ANN tier**, and the single median figure that would suggest
otherwise is not reported anywhere.

### Retrieval quality — the headline result

| Tier | Recall@1 | Recall@3 | Recall@5 | Top-5 overlap vs HOT |
|---|---|---|---|---|
| HOT | 0.150 | 0.475 | 0.550 | — |
| WARM | 0.150 | 0.475 | 0.550 | 1.000 |
| COLD | 0.150 | 0.475 | 0.550 | 1.000 |

COLD's recall is **measured on the rehydrated corpus**, not inferred from the
lossless round trip. Those are different properties: a lossless round trip
proves the bytes returned intact, while recall also depends on the index, the
metric and the query. Inferring one from the other would be exactly the kind of
unverified claim this phase is meant to avoid.

**int8 quantization cost nothing measurable on this corpus** — identical recall
at every cut-off and identical top-5 ordering, for a quarter of the vector
payload. That is the central trade-off result.

Recall is scored against hand-declared labels, never against HOT's ranking.
Using HOT as ground truth would make it perfect by definition and would measure
agreement rather than quality.

**On the absolute recall numbers.** R@1 of 0.15 is low, and honestly so: the
corpus is 500 structurally near-identical ERP records sharing statuses,
currencies and amount formats, queried by paraphrase. It is a deliberately hard
retrieval task. The comparison between tiers is the finding; the absolute value
is a property of this corpus and should not be read as a general retrieval
result.

**Vector round-trip integrity is reported separately and still holds:** maximum
component deviation across 50 archives was exactly `0.0` after serialize →
compress → encrypt → decrypt → decompress → deserialize. This is kept alongside
the recall measurement, not in place of it.

### Footprint

| Tier | Comparable proxy | Basis |
|---|---|---|
| HOT | 1536 B/record | 384 x float32 |
| WARM | 384 B/record | 384 x int8 |
| COLD | 1536 B/record | 384 x float32 inside the archive |

### Final comparison

| Tier | Search / access | p95 | R@1 | R@3 | R@5 | Footprint | Cost proxy |
|---|---|---|---|---|---|---|---|
| HOT | 11.01 ms | 24.51 ms | 0.150 | 0.475 | 0.550 | 1536 B/rec | 1.00 |
| WARM | 16.45 ms | 33.20 ms | 0.150 | 0.475 | 0.550 | 384 B/rec | 0.10 |
| COLD | 15.35 ms *(after 9335.69 ms one-time rehydration)* | 34.99 ms | 0.150 | 0.475 | 0.550 | 1536 B/rec proxy; 4644 B/rec measured archive | 0.05 |

COLD's access figure **includes archive rehydration and index preparation** as a
separate one-time term and is therefore not directly equivalent to the
always-online ANN query latency of HOT and WARM.

Cold archive as measured on disk: **4644 B/record**.

**These two figures must not be combined.** The proxy counts vector components
only. The measured archive size covers a different scope — header, nonce, GCM
tag and the compressed metadata payload. Dividing one by the other compares
different content.

### Cost proxy

`normalized_cost = storage_bytes x resource_multiplier`, with multipliers
HOT 1.00, WARM 0.40, COLD 0.05.

Relative to HOT: WARM **0.10**, COLD **0.05**.

These are **normalized units, not money**. The multipliers are experimental
assumptions, stated in the artifact so a reader can substitute their own and
recompute. No cloud tariff, vendor quote or currency is implied anywhere.

### Routing distribution

Across all 500 contexts: HOT 158, WARM 165, COLD 177.

Reason codes: `initial_placement` 158, `low_access_demotion` 230,
`age_demotion` 112.

### The restricted-data invariant

Under the default topology all three tiers are on-premises, so the constraint
holds trivially and proves nothing. The benchmark therefore re-runs the same 500
contexts with COLD configured as **external**:

- restricted records placed in the external tier: **0**
- non-restricted records still placed there: **161**

The second number matters as much as the first. Without it, a constraint that
excluded everything would look identical to one that works.

### Movement

100 vectors: WARM→COLD **2.04 ms/vector**, COLD→WARM **31.37 ms/vector**.

Rehydration is roughly 15x the cost of archiving, which is the expected shape:
archiving compresses and encrypts once, while rehydration decrypts, decompresses
and re-indexes.

---

## 7. Security

- **AES-256-GCM** via `cryptography`, with a fresh 12-byte nonce per archive
  (asserted unique across archives — nonce reuse under GCM is a catastrophic,
  silent break).
- **No invented crypto.** No XOR, no base64-as-encryption, no ECB.
- **Keys are injected**, never hard-coded, never committed, never written beside
  the archive, never printed, and never placed in metadata. Key provider
  `__repr__` is redacting, because keys leak through tracebacks more often than
  through files.
- **Tampering is detected**, not silently accepted — a single flipped byte
  raises `ColdArchiveIntegrityError`, and the corrupt archive is **preserved**
  for investigation rather than deleted.

### A disclosure worth stating plainly

The archive **header is deliberately cleartext** so archives can be inventoried,
audited and garbage-collected without decrypting every file. That means the
representation id, embedding id, entity type, model id, content hash and sizes
are readable on disk without the key. The business payload and the vector
components are not.

This is a real, bounded disclosure. A test pins the header's key set so it
cannot quietly grow to carry business data.

---

## 8. Boundaries

Phase 12 decides where a vector lives. It does not orchestrate, serve or
converse. Enforced by test:

- no hosted language model (no OpenAI, Anthropic, Gemini, Cohere, inference
  clients) — the prototype runs locally
- no `bpi2020` import — Phase 12 is generic infrastructure, not a BPI fork
- no hard-coded collection name
- no Phase 13 surface (no FastAPI, Flask, uvicorn, routes, LangChain)

The boundary scans strip docstrings via AST before matching, because this
documentation legitimately names the things the code must not contain.

---

## 9. Reproducing

```bash
python scripts/benchmark_tiered_storage.py
```

Creates its own collections prefixed `erp_phase12_bench_` and deletes them
afterwards. The production BPI collection is never opened, read or written.
The corpus is fully deterministic — no RNG — so two runs are comparable.

Tests:

```bash
python -m pytest tests/erp_pipeline/storage -q
```

Live Qdrant tests skip loudly with a reason naming the unreachable host rather
than passing silently.
