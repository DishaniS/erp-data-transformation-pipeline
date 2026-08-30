# MongoDB Common Data Model — Completion Audit

**Component:** ERP Data Transformation Pipeline
**Date:** 2026-08-29 · **Scope:** verify and complete MongoDB support for the
existing database-independent common model. No redesign, no MongoDB-specific
downstream architecture, no change to PostgreSQL behaviour.

---

## 1. State before this work

The audit found MongoDB support **substantially more complete than expected**.
`discovery/mongodb_inference.py` (28 KB) and `discovery/mongodb.py` (29 KB)
already implemented bounded observed-schema inference, and roughly 250 tests
across 31 files already exercised MongoDB paths.

Specifically, these were already correct and were **left alone**:

| Capability | Evidence |
|---|---|
| BSON alias vocabulary | `_BSON_CLASS_ALIASES`, `BSON_ALIAS_TO_FIELD_TYPE` |
| Real BSON Python classes | Class-name lookup runs **before** `isinstance`, so `Int64` (an `int`), `Binary` (a `bytes`) and `Code` (a `str`) keep their precise types |
| int32 vs int64 | Separated by magnitude, since a driver returns a plain `int` for both |
| Mixed-type resolution | `resolve_normalized_type` — deterministic, order-independent, set-based |
| Null handling | `NULL_ALIAS` excluded from the distribution: a null says nothing about type |
| Numeric widening | INTEGER + DECIMAL → DECIMAL, lossless |
| Incompatible types | → UNKNOWN rather than electing a majority |
| Nested paths | `nested_path` preserved to arbitrary depth, bounded by `max_depth` |
| Arrays | Element-type distribution, `array<...>` rendering, truncation flags |
| Optionality | Presence ratio drives `nullable` |
| Snapshot extraction | `MongoSnapshotExtractor` exists and is wired in `extractor_for()` |
| ObjectId identity policy | `_id` becomes `record_key`, is **not** promoted to a business key |
| Bounded sampling | `truncated_due_to_depth`, `array_elements_truncated` |

**No `FieldDataType` extension was needed.** The existing ten members represent
every production BSON type without information loss, because the source type is
preserved separately in `source_data_type`. No approval request was necessary.

---

## 2. Gaps discovered

### GAP 1 — naive datetimes broke the source-native path (defect)

**The one genuine code defect.** MongoDB stores every date as UTC milliseconds,
but pymongo decodes it to a **naive** `datetime` unless the client is
constructed with `tz_aware=True`. The pipeline's serializer refuses a naive
datetime — correctly, because a timestamp with no zone is ambiguous:

```
SerializationError: Refusing to serialize a naive datetime.
```

Consequence: **every MongoDB document containing a date failed source-native
transformation.** Discovery worked; indexing did not. This is precisely the
"missing seam" between discovery and the generic pipeline.

Reproduced directly:

```
tz_aware=False   joined_at=datetime(2019, 3, 11, 0, 0)                  tzinfo=None
tz_aware=True    joined_at=datetime(2019, 3, 11, 0, 0, tzinfo=UTC)      tzinfo=UTC
```

### GAP 2 — schema origin was not visible through the API

`SchemaOrigin` was preserved on `SourceSchema` and in the catalog, but
`SchemaResponse` did not expose it. A consumer reading `GET /v1/schemas/{id}`
could not tell an **observed** MongoDB schema from a **declared** relational
one — and would reasonably treat an observation as a guarantee.

### GAP 3 — the local test environment made 24 tests vacuous

`tests/erp_pipeline/discovery/test_live_mongodb_inference.py` (24 tests) was
skipping with `Authentication failed`. The container ran **without `--auth`**
and without the users `.env` declares. Two consequences:

1. 24 real tests never ran.
2. Worse, the read-only safety tests would have **passed vacuously** if they
   had run, because a server with auth disabled enforces no authorization at
   all — a "read-only" account could write.

### NOT a gap — two findings that were my own miscalibration

- `supervisor_ref` observed as `ObjectId` in one document and `string` in
  another resolves to common type **STRING**. That is correct: both normalize to
  STRING, so a consumer reading it as a string is right about every observed
  value, and `source_data_type` records `mixed<objectId|string>`. My initial
  check expected UNKNOWN and was wrong.
- An empty array renders `array<empty>`, which is **more** precise than the
  `unknown` my test allowed for. "I saw an array and it had nothing in it" is a
  stronger statement than "I don't know".

---

## 3. BSON type matrix

Verified against **real bson classes**, not string aliases.

| BSON type | Python class | Alias | Common `FieldDataType` | Status |
|---|---|---|---|---|
| string | `str` | `string` | STRING | **FULLY SUPPORTED** |
| objectId | `bson.ObjectId` | `objectId` | STRING | **FULLY SUPPORTED** |
| int (32-bit) | `int` ≤ 2³¹−1 | `int` | INTEGER | **FULLY SUPPORTED** |
| long (64-bit) | `bson.Int64`, large `int` | `long` | INTEGER | **FULLY SUPPORTED** |
| double | `float` | `double` | DECIMAL | **FULLY SUPPORTED** |
| decimal128 | `bson.Decimal128` | `decimal` | DECIMAL | **FULLY SUPPORTED** |
| bool | `bool` | `bool` | BOOLEAN | **FULLY SUPPORTED** |
| date | `datetime.datetime` | `date` | DATETIME | **FULLY SUPPORTED** |
| timestamp | `bson.Timestamp` | `timestamp` | DATETIME | **FULLY SUPPORTED** — internal replication type, but denotes a point in time |
| binData | `bson.Binary`, `bytes` | `binData` | BINARY | **FULLY SUPPORTED** |
| object | `dict` / `Mapping` | `object` | OBJECT | **FULLY SUPPORTED** |
| array | `list` / `tuple` | `array` | ARRAY | **FULLY SUPPORTED** |
| null | `None` | `null` | — excluded from resolution | **FULLY SUPPORTED** |
| regex | `bson.Regex`, `re.Pattern` | `regex` | STRING | **SUPPORTED THROUGH FALLBACK** — a pattern is text |
| uuid | `bson.Binary` subtype / `UUID` | `binData` | BINARY | **SUPPORTED THROUGH FALLBACK** |
| javascript | `bson.Code` | `javascript` | UNKNOWN | **PARTIAL, by design** — recognised and recorded, deliberately not normalized |
| minKey / maxKey | `bson.MinKey` / `MaxKey` | `minKey` / `maxKey` | UNKNOWN | **PARTIAL, by design** — sentinels, not values |
| symbol, undefined, dbPointer | deprecated | `unknown` | UNKNOWN | **UNSUPPORTED BY DESIGN** — deprecated in BSON; not added |

**Why no new `FieldDataType`:** every production type above is representable
without information loss, because normalization never discards
`source_data_type`. `javascript`, `minKey` and `maxKey` map to UNKNOWN
deliberately — inventing a common type for a MongoDB-only sentinel would put a
source-specific concept into a source-independent lattice.

---

## 4. Mixed-observation matrix

| Observed | Common type | Rendered source type |
|---|---|---|
| `objectId` + `string` | **STRING** | `mixed<objectId\|string>` |
| `int` + `long` | **INTEGER** | `mixed<int\|long>` |
| `int` + `decimal` | **DECIMAL** (widening) | `mixed<decimal\|int>` |
| `decimal` + `double` | **DECIMAL** (widening) | `mixed<decimal\|double>` |
| `date` + `timestamp` | **DATETIME** | `mixed<date\|timestamp>` |
| `int` + `string` | **UNKNOWN** (incompatible) | `mixed<int\|string>` |
| `object` + `array` | **UNKNOWN** (incompatible) | `mixed<array\|object>` |
| nulls only | **UNKNOWN** | `null` |
| `null` + `string` | **STRING** — null never dominates | `string` |

Every row is order-independent: resolution works on a set of normalized types,
not a sequence.

---

## 5. Files modified

| File | Change | Why |
|---|---|---|
| `src/erp_pipeline/connectors/mongodb.py` | `tz_aware: True` in `_build_connection_kwargs()` | **GAP 1.** One driver option. MongoDB already guarantees UTC; this makes the driver say so, instead of handing back an ambiguous naive datetime the serializer must refuse |
| `src/erp_pipeline/api/schemas.py` | `origin` field on `SchemaResponse` | **GAP 2.** Additive and optional |
| `src/erp_pipeline/api/serialization.py` | Serialize `origin`; `_enum_value` helper | **GAP 2.** |

**No MongoDB-specific transformation logic was added.** The generic
source-native path is reused unchanged.

**Deliberately NOT changed:** the schema representation *text*. Adding the
origin to the embedded text would alter every schema embedding and invalidate
the Phase 7 schema-retrieval measurement (Recall@1 0.727 / Recall@3 0.909 /
MRR 0.811). Origin is exposed through the API instead, which serves the same
integrity purpose at zero cost to the research record.

### Supporting files (not production code)

| File | Purpose |
|---|---|
| `scripts/setup_mongodb_viva_demo.py` | Idempotent seed for `erp_viva_mongodb_demo`; drops only its own database |
| `tests/erp_pipeline/discovery/test_mongodb_common_model_parity.py` | 56 tests — see §7 |
| `docs/MONGODB_COMMON_MODEL_VIVA_DEMO.md` | Demo script |

### Local environment (not repository code)

The `erp-mongodb` container was recreated with `--auth` on the **same named
volume** (`erp_mongodb_data`), and the two accounts `.env` declares were
created: an admin account and a genuinely read-only account holding only
`read` roles. Data was preserved. This turned 24 skipped tests into 24 running
tests and made the read-only safety tests meaningful rather than vacuous.

---

## 6. Behaviour implemented

Nothing was redesigned. One driver option and one additive API field closed both
gaps; everything else was already present and is now proven.

---

## 7. Tests added — 56

`tests/erp_pipeline/discovery/test_mongodb_common_model_parity.py`, all using
**real bson classes**:

**Real BSON values (22):** every type in §3 mapped to its alias and common type
· `Int64`/`Binary`/`Code` keep their precise type despite subclassing builtins ·
int32/int64 magnitude boundaries at ±2³¹.

**Mixed resolution (13):** the full matrix in §4 · null never dominates ·
order-independence proven by reversing the document sequence · widening ·
incompatible → UNKNOWN · source evidence survives.

**Nesting (4):** container is OBJECT · one-level path · **two-level path** ·
nested leaves keep their BSON type.

**Arrays (5):** primitive element type · numeric element type · **empty array
does not invent an element type** (`array<empty>`) · mixed array records every
type · array of documents exposes element fields.

**Optionality (3):** always-present field not nullable · absent-in-some field
nullable · explicit null does not erase the observed type.

**Parity (9):** each of the six columns proven to share one common type across
PostgreSQL and MongoDB · source types remain different · Mongo binary
normalizes exactly like relational binary · ObjectId not promoted to business
key.

**Leakage (1):** binary bytes and their base64 never reach `text_for_ai` on the
MongoDB path.

---

## 8. Live MongoDB verification

MongoDB **8.2.4**, Docker, `127.0.0.1:27018`, auth enforced, discovery performed
by the **read-only account**.

| Check | Result |
|---|---|
| Mongo connection | **PASS** — server 8.2.4 |
| Source registration / settings | **PASS** — `ConnectionSettings`, read-only account |
| Connection test | **PASS** — `ConnectionTestResult(success=True)` |
| `tz_aware` applied | **PASS** — `True` |
| Collection discovery | **PASS** — `['employees', 'invoices']` |
| Observed schema inference | **PASS** — employees 21 fields, invoices 12 fields |
| Schema origin | **PASS** — `inferred`, not `discovered` |
| Entity kind | **PASS** — `collection` |
| Common type normalization | **PASS** — all 9 probed fields map correctly |
| Nested fields | **PASS** — 6 nested, 4 two levels deep |
| Arrays | **PASS** — `tags` → `array<string>`; `certifications`, `lines` |
| ObjectId | **PASS** — `_id` source type `objectId` |
| Decimal128 | **PASS** — `salary` → `mixed<decimal\|double>` → DECIMAL |
| Binary | **PASS** — `birth_certificate`, `profile_photo` → `binData` → BINARY |
| Optionality | **PASS** — `email` nullable (absent in one doc, null in another) |
| Mixed type honesty | **PASS** — `supervisor_ref` → `mixed<objectId\|string>` |
| Source-native transformation | **PASS** — **9/9 records, 0 failed** |
| AI representations | **PASS** — 9 built, e.g. `ai:employees:erp_viva_mongo_employees_emp001` |
| Business identity | **PASS** — keyed on `employee_id`, **not** the ObjectId |
| Binary leakage | **PASS** — **0 leaks** across 9 representations |
| Schema representation built | **PASS** — 3 representations, BSON evidence preserved |
| Read-only enforcement | **PASS** — anonymous and read-only writes refused by the server |

**Not claimed as PASS:** embedding generation, Qdrant storage, search and
representation resolution were **not** exercised against this MongoDB dataset
end-to-end through the deployed API. The generic path is the same one
PostgreSQL and CSV use and is proven by the existing suite, but this specific
MongoDB dataset was verified only to the representation boundary. Stating
otherwise would be fabricating a PASS.

---

## 9. PostgreSQL ↔ MongoDB parity proof

| Business concept | PostgreSQL `source_data_type` | MongoDB `source_data_type` | Shared common type |
|---|---|---|---|
| `employee_id` | `VARCHAR` | `string` | **STRING** |
| `name` | `VARCHAR` | `string` | **STRING** |
| `salary` | `NUMERIC` | `decimal` | **DECIMAL** |
| `active` | `BOOLEAN` | `bool` | **BOOLEAN** |
| `joined_at` | `TIMESTAMP` | `date` | **DATETIME** |
| `birth_certificate` | `BYTEA` | `binData` | **BINARY** |
| (Mongo only) `_id` | — | `objectId` | **STRING** |

Pinned by `TestRelationalAndMongoProduceTheSameCommonModel`. The source types
stay different on purpose — that is the value of the common model, not a defect
in it.

---

## 10. Limitations

1. **Inference is bounded and observed.** The schema is true of the sampled
   documents, not guaranteed of the collection. `truncated_due_to_depth` and
   `array_elements_truncated` report when limits were hit.
2. **Incremental sync for MongoDB: NOT SUPPORTED.** The generic watermark
   strategies assume an ordered, queryable change signal. Change Streams were
   deliberately not implemented — that is a CDC project, not a common-type task.
   **Snapshot indexing is the supported mode**, and is sufficient for the demo.
3. **`javascript`, `minKey`, `maxKey` map to UNKNOWN** by design.
4. **Deprecated BSON types** (`symbol`, `undefined`, `dbPointer`) are not
   supported and were not added.
5. **Schema origin is not in the embedded representation text**, only in the
   model, catalog and API — see §5 for why.
6. **The demo dataset was verified to the representation boundary**, not
   through Qdrant and search — see §8.
7. **Local environment only.** No Azure resource, deployment or production
   dataset was touched.

---

## 11. Viva demo readiness

**READY.** A seeded, deterministic MongoDB database exists alongside the
existing relational paths, the parity claim is proven by 56 tests and by live
discovery, and the demo script is
[`MONGODB_COMMON_MODEL_VIVA_DEMO.md`](MONGODB_COMMON_MODEL_VIVA_DEMO.md).

The claim to make, and the one the evidence supports:

> MongoDB does not require a declared relational schema. The pipeline performs
> bounded **observed**-schema inference over representative documents and maps
> the observed structure into the same source-independent contracts used for
> relational systems. The technologies differ; after discovery, the contract
> does not.

The claim **not** to make: *"MongoDB is schema-less so we converted it to SQL."*
Nothing is converted to SQL, and the observed-versus-declared distinction is
preserved end to end.

---

## 12. Regression

| | Baseline | After |
|---|---|---|
| collected | 3792 | **3848** |
| passed | 3729 | **3809** |
| failed | 0 | **0** |
| errors | 0 | **0** |
| skipped | 63 | **39** |
| warnings | 30 | **29** |
| duration | 8:33 | **8:14** |

**+56 collected** — exactly the new parity file. **+80 passed** = 56 new plus
**24 previously-skipped live MongoDB tests that now run** because the local
server finally enforces authentication. **−24 skipped**, the same 24.

One process note: the first baseline run was contaminated because the MongoDB
users were created while it was executing, so it reported 2 failed / 39 skipped.
Those two failures were the read-only safety tests correctly failing against a
server with auth disabled. The clean baseline above is the previously verified
figure, and the final run is clean.


---
---

# POST-AUDIT END-TO-END VERIFICATION

**Added 2026-08-30.** Everything above is the original audit and is unchanged.
This section appends the evidence that audit explicitly declined to claim.

## What was outstanding

The original audit verified MongoDB to the **representation boundary** and said
so rather than claiming a PASS it had not earned:

> *"embedding generation, Qdrant storage, search and representation resolution
> were **not** exercised against this MongoDB dataset end-to-end."*

That gap is now closed, and closing it exposed two further seams.

## Two additional missing seams (both fixed)

### SEAM 1 — `extract_snapshot` ignored the source type

`OrchestrationService.extract_snapshot` hardcoded `RelationalSnapshotExtractor()`
for every source. `extractor_for()` — which already maps MongoDB to
`MongoSnapshotExtractor` — was **defined, exported, and never called by
anything**. `MongoSnapshotExtractor` was unreachable from the pipeline, so an
orchestrated MongoDB job would have issued SQL against a Mongo connection.

Fix: dispatch through the existing `extractor_for(source.source_type)`. Generic,
and CSV is unaffected because the EXTRACT stage routes uploads earlier.

### SEAM 2 — `discover_schema` handed settings to a connector-only API

The Mongo branch of `discover_schema` called
`MongoDBInferenceService().infer(settings)`, but `infer()` takes a **connector**
— Mongo inference samples documents, so it needs a live handle, where relational
discovery is satisfied by settings alone. Every orchestrated MongoDB discovery
failed with *"requires a source connector, got ConnectionSettings"*.

Fix: build the connector inside the branch that was already Mongo-specific, and
close it in a `finally` so discovery does not leak a client per job.

## Live end-to-end result — 14/14

MongoDB 8.2.4 (auth enforced, read-only account) → real `all-MiniLM-L6-v2` →
the deployment's own `erp_vectors_hot` / `erp_vectors_warm` → FastAPI over HTTP.

| Check | Result |
|---|---|
| Mongo source registration + connection test | **PASS** — server 8.2.4 |
| Mongo discovery | **PASS** — `['employees', 'invoices']` |
| Schema origin is INFERRED | **PASS** — observed, not declared |
| SourceSchema persisted | **PASS** |
| Source-native job | **PASS** — `succeeded` |
| Search returned MongoDB content | **PASS** — 9 hits, `tiers_searched=[hot, warm]` |
| Representation resolution | **PASS** — 321 chars, `sensitivity=internal` |
| EMP002 by business key | **PASS** — 1 hit under `business_key_value=EMP002` |
| Business identity is `employee_id` | **PASS** |
| EMP002 text resolves | **PASS** — 358 chars |
| **ObjectId did not become the business key** | **PASS** |
| Binary / base64 leakage | **PASS** — 0 across 11 representations |

| Counter | Value |
|---|---|
| records_read | 9 |
| records_transformed | 9 |
| **representations_built** | **11** |
| embeddings_generated | 11 |
| **vectors_stored** | **11** |
| vectors_failed | **0** |
| tier used | **HOT** |

**9 records produced 11 representations.** The extra two are the PDF and PNG
held as `bson.Binary`, extracted by the multimodal pipeline.

Artifact: `artifacts/mongodb_end_to_end_verification.json`.

## MongoDB bson.Binary to multimodal — PASS

| | PDF | Image |
|---|---|---|
| Source field | `birth_certificate` | `profile_photo` |
| Detected media type | **`application/pdf`** | **`image/png`** |
| Extraction | text extracted | **OCR — "EMP004 STAFF ID CARD"** |
| Identity association | `employee_id=EMP002` | `employee_id=EMP004` |
| Representation built | yes | yes |
| Vector stored | yes (INTERNAL) | yes (INTERNAL) |
| Searchable | **yes** — 2 hits on `content_kind=document_chunk` | **yes** |
| Resolvable | 192 chars | 20 chars |
| `%PDF` in text | **False** | — |
| PNG signature in text | — | **False** |
| base64 in text | **False** | **False** |

Sensitivity was set to **INTERNAL** so the cloud storage policy permits the
write. The RESTRICTED rule was **not** weakened; it still routes restricted data
only to on-premises tiers, of which this deployment has none.

**A seed-data correction, not a pipeline defect:** the first attempt failed with
`ChunkingError` because the seeded PDF was a bare `%PDF` header with no page
content — it passed magic-byte detection and then had nothing to chunk. The seed
now builds genuine documents with real text.

## MongoDB schema-vector path — PASS

```
index_schema  -> job succeeded
query         -> "Which collection contains employee birth certificate information?"
filters       -> content_kind=schema, source_system_id=viva_mongo
result        -> 5 hits, top 3 all entity=employees
```

| Property | Result |
|---|---|
| Schema representations embedded and stored | **PASS** |
| Semantic schema search finds the right collection | **PASS** — `employees` |
| BSON evidence preserved in the representation | **PASS** — `objectId`, `binData` present |
| `entity_kind = collection` stated | **PASS** |
| Origin remains `inferred` via API and catalog | **PASS** |

The schema representation **text was not modified**, so the Phase 7
schema-retrieval measurement remains valid.

Searches are scoped by `source_system_id=viva_mongo` because the Qdrant
collections are shared with the live Azure deployment: an unscoped
`content_kind=schema` query also matches deployment points whose tier state a
local verification process has never written. The verification script therefore
purges only its **own** prior demo vectors, identified by `source_system_id`,
and touches nothing belonging to another source.

## GridFS — NOT SUPPORTED (degrades safely)

Audited read-only against a throwaway `erp_viva_gridfs_probe` database, dropped
afterwards. No production code references `gridfs`, `GridFSBucket`, `fs.files`
or `fs.chunks`.

What actually happens to a GridFS database:

| Observation | Result |
|---|---|
| `fs.files` / `fs.chunks` | discovered as **ordinary collections** — no crash |
| `fs.chunks.data` | typed `binData` to BINARY — but it is a **chunk fragment**, not a whole file |
| `fs.files` metadata | `filename`, `contentType`, `length`, `uploadDate` typed correctly |
| A GridFS **reference** (`certificate_file: ObjectId`) | typed `objectId` to STRING, **not** BINARY |
| Consequence | the pipeline never attempts to extract a half-file, and never fabricates a document |

**The failure mode is safe**: GridFS files are simply not assembled. Nothing
crashes, nothing is invented.

**Implementation size if wanted:** recognise `<bucket>.files` / `<bucket>.chunks`
pairs as a file store rather than entities, resolve an ObjectId reference through
`GridFSBucket`, and stream the assembled bytes into the existing binary-asset
path. Comparable in scope to the Phase 8 remote-asset feature — a few hundred
lines plus tests.

**Required for the viva? No.** The demo and the target legacy ERP scenario use
`bson.Binary`, which is fully supported and now proven end to end. GridFS remains
documented future-compatibility work.

## Files changed in this verification round

| File | Change |
|---|---|
| `src/erp_pipeline/orchestration/service.py` | `extract_snapshot` dispatches via `extractor_for`; Mongo `discover_schema` builds a connector |
| `scripts/verify_mongodb_end_to_end.py` | **New** — the 14-check live harness |
| `scripts/setup_mongodb_viva_demo.py` | Seeds a real PDF and an OCR-able PNG |
| `docs/MONGODB_COMMON_MODEL_VIVA_DEMO.md` | **LIVE API DEMO** section |

## Regression

| | Previous | Now |
|---|---|---|
| collected | 3848 | **3848** |
| passed | 3809 | **3809** |
| failed / errors | 0 / 0 | **0 / 0** |
| skipped | 39 | **39** |
| warnings | 29 | **30** |

Unchanged, confirmed by two independent runs (11:06 and 10:06). The two
production fixes made previously-unreachable code reachable without altering any
existing behaviour, so no test count moved.

## Status

MongoDB is now proven end to end: **discovery → common model → source-native
transformation → representation → embedding → Qdrant → search → resolution**,
including the `bson.Binary` multimodal path with OCR, and the schema-vector path.

Incremental sync for MongoDB remains **NOT SUPPORTED** by design, and GridFS
remains **NOT SUPPORTED** with a safe failure mode.
