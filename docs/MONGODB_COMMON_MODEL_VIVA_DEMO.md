# SQL and NoSQL Through One Common Model — Demo Script

**Duration:** 5–10 minutes · **Audience:** viva panel
**Message:** the technologies differ; after the discovery layer, the contract does not.

---

## The one-sentence claim

> A relational database **declares** its schema. MongoDB does not. The pipeline
> therefore performs **bounded observed-schema inference** over a sample of
> documents and maps the result into the *same* source-independent contracts —
> `SourceSchema`, `SourceEntity`, `SourceField`, `FieldDataType` — that the
> relational path produces. Everything downstream sees one contract.

**Do not say** *"MongoDB is schema-less so we converted it to SQL."* Nothing is
converted to SQL, and the pipeline records that a MongoDB schema was *observed*
rather than *declared*.

---

## Before you start (2 minutes, off-camera)

```bash
docker start erp-mongodb
```

```bash
.venv/Scripts/python.exe scripts/setup_mongodb_viva_demo.py
```

The seed prints the shape variation it created. It is idempotent — run it twice
and the database is identical.

Have ready: a terminal, and `docs/MONGODB_COMMON_DATA_MODEL_COMPLETION_AUDIT.md`
open at the parity table (§9).

---

## DEMO A — the relational path (90 seconds)

*"A relational source tells us its schema. We read the catalog."*

```bash
.venv/Scripts/python.exe -m pytest tests/erp_pipeline/discovery/test_structural_discovery.py -q
```

Point out on screen:

- Discovery **reads a declared catalog** — tables, columns, types, keys.
- It produces `SourceSchema` with `origin = discovered`.
- A column's `source_data_type` stays `VARCHAR`, `NUMERIC`, `BYTEA` — the
  vendor's own vocabulary is never thrown away.
- `normalized_data_type` is the common concept: STRING, DECIMAL, BINARY.

> "Two fields per column: what the source called it, and what it means. That
> second one is the common model."

---

## DEMO B — the MongoDB path (2 minutes)

*"MongoDB declares nothing. So we observe."*

```bash
.venv/Scripts/python.exe -m pytest tests/erp_pipeline/discovery/test_live_mongodb_inference.py -q
```

24 tests run against the live server. Then show the real inference:

```bash
.venv/Scripts/python.exe -m pytest tests/erp_pipeline/discovery/test_mongodb_common_model_parity.py -q
```

56 tests, using real `bson` classes. Talk over it:

- Inference samples documents and records **every BSON type it sees per field**.
- It produces the **same** `SourceSchema` / `SourceEntity` / `SourceField`.
- `origin = inferred`, `entity_kind = collection` — the pipeline knows this was
  observed, not declared, and says so through `GET /v1/schemas/{id}`.

Live discovery reports, from the demo database:

```
employees   21 fields   kind=collection   binary=['birth_certificate','profile_photo']
                                          arrays=['certifications','tags']  nested=6
invoices    12 fields   kind=collection   arrays=['lines']                  nested=3
```

**Three things worth pausing on:**

| What | Where to point |
|---|---|
| **Nesting is preserved, not flattened** | `employment.contract.probation_months` keeps `nested_path = ("employment", "contract")` |
| **The schema is honest about uncertainty** | `supervisor_ref` is an ObjectId in one document and a string in another → `mixed<objectId\|string>` |
| **A missing field is not a null field** | `email` is absent from one document and explicitly null in another → `nullable = True` |

---

## DEMO C — side by side (2 minutes) · **the key slide**

*"Same six business facts. Two completely different technologies."*

| Business concept | PostgreSQL says | MongoDB says | Common model |
|---|---|---|---|
| `employee_id` | `VARCHAR` | `string` | **STRING** |
| `name` | `VARCHAR` | `string` | **STRING** |
| `salary` | `NUMERIC` | `Decimal128` | **DECIMAL** |
| `active` | `BOOLEAN` | `bool` | **BOOLEAN** |
| `joined_at` | `TIMESTAMP` | `Date` | **DATETIME** |
| `birth_certificate` | `BYTEA` | `BinData` | **BINARY** |

```bash
.venv/Scripts/python.exe -m pytest tests/erp_pipeline/discovery/test_mongodb_common_model_parity.py -q -k parity
```

> "The left two columns stay different — deliberately. We never throw away what
> the source called it. The right column is what every stage after discovery
> actually consumes: mapping, transformation, embedding, storage routing,
> retrieval. One contract, two paradigms."

The `birth_certificate` row is the strongest one:

> "A `BYTEA` in PostgreSQL and a `BinData` in MongoDB both become BINARY — so
> the *same* multimodal pipeline detects the document type, runs OCR, and builds
> a representation. We wrote that pipeline once."

---

## If asked: "does the MongoDB data actually get indexed, or just discovered?"

Show the measured result:

```
source-native job       : succeeded
records read / transformed : 9 / 9
AI representations      : 11 built  (9 structured + 2 from bson.Binary)
embeddings / vectors    : 11 / 11 stored, 0 failed
search hits             : 9
binary leakage          : 0
```

The 9 → 11 step is worth naming out loud: the two extra representations are the
PDF and the PNG held as `bson.Binary`, extracted by the same multimodal pipeline
a PostgreSQL `BYTEA` goes through.

Two points worth making unprompted:

- **The identity is `employee_id`, not the ObjectId.** `_id` is stable, but it
  is MongoDB's identifier, not the ERP's business key. It is kept as provenance.
- **Binary never reaches the embedded text.** The PDF bytes in
  `birth_certificate` go to the document pipeline; zero bytes and zero base64
  reach `text_for_ai`.

---

## Likely questions, and honest answers

**"Isn't inferring a schema just guessing?"**
No — it is reporting. The schema says exactly what was observed in the sampled
documents, and flags when a limit was hit (`truncated_due_to_depth`). Where the
evidence is contradictory it returns UNKNOWN rather than electing a majority. An
inferred schema never claims more certainty than the sample supports.

**"What if a field has different types in different documents?"**
Three outcomes, all deterministic. Types that mean the same thing merge
(`int` + `long` → INTEGER). Compatible numerics widen (`int` + `decimal` →
DECIMAL, lossless). Genuinely incompatible types (`int` + `string`) resolve to
UNKNOWN, and the distribution `mixed<int|string>` is preserved so a human can
see why.

**"Does MongoDB support incremental sync?"**
**No — and that is deliberate.** Snapshot indexing is supported; Change Streams
were not implemented, because that is a CDC project rather than a common-model
task. It is documented as unsupported rather than overstated.

**"How much of MongoDB's type system do you handle?"**
Every production BSON type: string, objectId, int32, int64, double, Decimal128,
bool, date, timestamp, binData, object, array, null, regex, uuid. `javascript`,
`minKey` and `maxKey` are recognised and recorded but map to UNKNOWN, because
inventing a common type for a MongoDB-only sentinel would put a source-specific
concept into a source-independent lattice. Deprecated types are not supported.

**"Did you have to add a new common type for MongoDB?"**
No. The existing ten `FieldDataType` members represent every production BSON
type without loss, because normalization never discards `source_data_type`. That
is the test of whether a common model is actually common.

---

## Closing line

> "Discovery is the only layer that knows what a MongoDB is. Everything after it
> — mapping, transformation, OCR, embedding, tiered storage, retrieval — was
> written once and consumes one contract. Adding MongoDB did not require a
> second pipeline; it required a second way of *learning* a schema."

---

## LIVE API DEMO — 5 minutes

Everything below uses routes confirmed in the deployed OpenAPI document. All
credentials are placeholders; the demo data is synthetic.

**Setup (before the viva, not during):**

```bash
docker start erp-mongodb
```

```bash
.venv/Scripts/python.exe scripts/setup_mongodb_viva_demo.py
```

Or run the whole proof in one command and talk over the output:

```bash
.venv/Scripts/python.exe scripts/verify_mongodb_end_to_end.py
```

That prints 14 checks and writes
`artifacts/mongodb_end_to_end_verification.json`.

---

### Step 1 — Register the MongoDB source · `POST /v1/sources`

```json
{
  "name": "viva_mongo",
  "source_type": "mongodb",
  "host": "localhost",
  "port": 27018,
  "database": "erp_viva_mongodb_demo",
  "username": "<YOUR_READONLY_USER>",
  "password": "<YOUR_VALUE>",
  "auth_database": "admin"
}
```

**201 Created** → `source_id`. The password goes into the secret provider and is
dropped; it never reaches the source record or any response.

> Say: *"A read-only account. Discovery must never need write access — and the
> test suite proves the server refuses writes from it."*

---

### Step 2 — Test connectivity · `POST /v1/sources/{source_id}/test`

No body. Returns `success`, `server_version`, `latency_ms`.

---

### Step 3 — Discover the observed schema · `POST /v1/sources/{source_id}/discover`

No body. This is the interesting one.

> Say: *"A relational source would have its schema read from a catalog. There is
> no catalog here, so the pipeline samples documents and reports what it
> observed."*

---

### Step 4 — Read the schema · `GET /v1/schemas/{schema_id}`

Point at three things in the response:

| Field | Value | Why it matters |
|---|---|---|
| `origin` | `"inferred"` | Not `"discovered"`. The API states that this schema was **observed**, so a consumer never mistakes a sample for a guarantee |
| `entity_kind` | `"collection"` | Not `"table"` |
| `source_data_type` | `objectId`, `decimal`, `binData`, `array<string>` | MongoDB's own vocabulary, preserved |
| `normalized_data_type` | `string`, `decimal`, `binary`, `array` | The common model |

---

### Step 5 — Index the documents · `POST /v1/jobs` → **202**

```json
{
  "job_type": "source_native_pipeline",
  "source_id": "viva_mongo",
  "schema_id": "<schema_id from step 4>",
  "entity": "employees",
  "options": {
    "key_fields": ["employee_id"],
    "sensitivity": "internal"
  }
}
```

> Say: *"`key_fields` is required. MongoDB's `_id` is stable, but it is
> MongoDB's identifier, not the ERP's business key. We index on `employee_id`
> and keep `_id` as provenance."*

**Sensitivity is `internal` deliberately** — the storage policy restricts
`restricted` data to on-premises tiers, and this deployment's Qdrant is cloud.
That rule is respected, not relaxed.

---

### Step 6 — Poll · `GET /v1/jobs/{job_id}`

The measured result from the live run:

```
status                   succeeded
records_read             9
records_transformed      9
representations_built    11
embeddings_generated     11
vectors_stored           11
vectors_failed           0
```

> Say: *"Nine documents produced eleven representations. The extra two are the
> PDF and the PNG stored as `bson.Binary` — the multimodal pipeline extracted
> them, and one of them went through OCR."*

---

### Step 7 — Search · `POST /v1/search`

```json
{
  "query": "employee in the finance department",
  "filters": {
    "content_kind": "structured_record",
    "source_system_id": "viva_mongo"
  }
}
```

For the EMP002 identity demonstration:

```json
{
  "query": "Nimal Silva finance senior accounts officer",
  "filters": {
    "content_kind": "structured_record",
    "source_system_id": "viva_mongo",
    "business_key_name": "employee_id",
    "business_key_value": "EMP002"
  }
}
```

For the documents extracted out of `bson.Binary`:

```json
{
  "query": "certificate of employment synthetic demo registry",
  "filters": {
    "content_kind": "document_chunk",
    "source_system_id": "viva_mongo"
  }
}
```

For the schema itself:

```json
{
  "query": "Which collection contains employee birth certificate information?",
  "filters": {
    "content_kind": "schema",
    "source_system_id": "viva_mongo"
  }
}
```

---

### Step 8 — Resolve · `GET /v1/representations/{representation_id}`

Returns the AI-ready text, identity, provenance and sensitivity.

> Say: *"Search gave identity and provenance, not text. The text comes from
> PostgreSQL in a second call — which is why Qdrant holds no raw content."*

---

### The measured live result

| Check | Result |
|---|---|
| Mongo connection, registration, discovery | **PASS** |
| Schema origin `inferred` | **PASS** |
| Source-native job | **succeeded** |
| Representations / embeddings / vectors | **11 / 11 / 11**, 0 failed |
| Search returned MongoDB content | **9 hits** |
| Representation resolution | **PASS** |
| EMP002 by business key, not ObjectId | **PASS** |
| PDF from `bson.Binary` extracted and searchable | **PASS** |
| PNG from `bson.Binary` **OCR'd** ("EMP004 STAFF ID CARD") | **PASS** |
| Binary / base64 leakage | **0** |
| Schema-vector search returns `employees` | **PASS** |

**14 / 14 checks passed** — `artifacts/mongodb_end_to_end_verification.json`.

---

### If asked about GridFS

> *"Not supported, and it fails safely rather than silently. A GridFS database
> discovers `fs.files` and `fs.chunks` as ordinary collections, and a GridFS
> reference is typed `objectId → STRING`, so the pipeline never tries to extract
> a half-file. Assembling GridFS files would be a bounded piece of work in the
> multimodal layer — the demo uses `bson.Binary`, which is fully supported and
> proven end to end."*

---

## Reference

| | |
|---|---|
| Full audit | [`MONGODB_COMMON_DATA_MODEL_COMPLETION_AUDIT.md`](MONGODB_COMMON_DATA_MODEL_COMPLETION_AUDIT.md) |
| Seed script | `scripts/setup_mongodb_viva_demo.py` |
| Parity tests | `tests/erp_pipeline/discovery/test_mongodb_common_model_parity.py` |
| Live tests | `tests/erp_pipeline/discovery/test_live_mongodb_inference.py` |
| Demo database | `erp_viva_mongodb_demo` — 9 employees, 5 invoices, entirely synthetic |
