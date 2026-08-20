# MongoDB Observed-Schema Inference

Phase 5 of the ERP-Aware Data Transformation Pipeline
(SLIIT R26-SE-034, component IT22267290).

**Terminology, stated once and meant throughout this document.** Phase 5 does
not discover "the MongoDB schema", because for an ordinary collection there is
no such thing to discover. It reports an **observed** — equivalently
**inferred**, or **sample-derived** — schema: what a bounded sample of
documents actually contained. Where a collection *does* carry a MongoDB
validator, its presence is reported separately and is never confused with the
sampled result (§18).

---

## 1. Purpose

Answer one question, honestly:

> **What structure was OBSERVED in this collection?**

Not:

> ~~What is guaranteed to exist in every future MongoDB document?~~

and not:

> ~~What does this field mean?~~ — semantic interpretation and canonical
> mapping are later phases.

The observed structure is then expressed in exactly the same generic contract
relational discovery produces, so that a downstream consumer never has to know
which paradigm a schema came from.

## 2. Why MongoDB is different

| | Relational (Phase 4) | MongoDB (Phase 5) |
|---|---|---|
| Source of truth | A declared catalog the server maintains | The documents themselves |
| What is read | Metadata (`information_schema`, `sys.*`, via the SQLAlchemy Inspector) | Data — a bounded sample of documents |
| Completeness | Total: every column is declared | Partial by construction: a sample |
| Optionality | `NOT NULL` is a constraint | Presence is a frequency in the sample |
| Types | One declared type per column | Whatever each document happened to store |
| Relationships | Foreign keys are declared and enforced | None are enforced |
| Correct verb | *discovered* | *inferred* / *observed* |

The consequence that drives every design decision below: a field absent from
500 sampled documents may exist in the 500 001st. No amount of sampling turns
an observation into a constraint, so Phase 5 records the evidence next to
every claim it makes.

## 3. Observed / inferred schema — what the words mean here

- **Observed** — this path, type or presence ratio was seen in the sample.
- **Inferred** — a conclusion drawn from observations under a documented,
  deterministic policy (for example "integer + decimal ⇒ decimal", §11).
- **Never claimed** — that the structure is complete, enforced, or stable.

`SourceSchema.origin` is `SchemaOrigin.INFERRED`, never `DISCOVERED`. The same
claim is repeated as data in schema, entity and field metadata
(`schema_claim: "observed"`), so a snapshot read straight out of the catalog
still states what kind of claim it is.

## 4. Architecture and MongoDBConnector integration

```
MongoDB
   |
   v
Phase 3 MongoDBConnector          erp_pipeline.connectors.mongodb
   |    create_database_handle()
   v
Phase 5 bounded inference         erp_pipeline.discovery.mongodb
   |                              erp_pipeline.discovery.mongodb_inference
   v
observed field/path/type statistics
   |
   v
SourceSchema                      erp_pipeline.schemas (Phase 1)
   |
   v
Phase 2 Schema Catalog            erp_pipeline.catalog
```

Phase 5 never constructs a `MongoClient`. The one additive, backward-compatible
change to Phase 3 is `MongoDBConnector.create_database_handle()`, the document
counterpart of `SQLAlchemyRelationalConnector.create_inspector()`: it returns
the `pymongo.database.Database` the connector is already bound to, so
credentials, TLS and timeouts stay owned by one validated place.

The package splits cleanly in two:

| Module | Responsibility | Driver access |
|---|---|---|
| `discovery/mongodb.py` | Collection discovery, sampling, `SourceSchema` assembly | All of it |
| `discovery/mongodb_inference.py` | Every structural rule: BSON typing, paths, arrays, presence, requiredness | None — pure |

That split is what makes every inference rule testable with plain Python
dicts, and it is enforced by test: `mongodb_inference.py` may not call a
driver method at all.

Public API:

```python
from erp_pipeline.discovery import (
    MongoInferenceOptions,
    MongoDBSchemaInference,     # .infer() -> SourceSchema, .summary()
    MongoDBInferenceService,    # .infer() / .infer_and_publish()
    infer_mongodb_schema,       # connector -> SourceSchema
)

schema = infer_mongodb_schema(connector, MongoInferenceOptions())
```

A relational connector passed to any of these raises
`UnsupportedDiscoverySourceError`, and vice versa.

## 5. Collection discovery

`list_collections()` is called once and supplies each collection's name, type
and options (including its validator) in a single metadata round trip.

- Each collection becomes one `SourceEntity` with
  `entity_kind = EntityKind.COLLECTION`.
- `namespace` is the database name — the role a schema plays for PostgreSQL.
- System collections (`system.*`) are excluded by default.
- Views are excluded by default (`include_views=False`); when included they
  are recorded with `collection_type: "view"`.
- Collections are processed in sorted order, so entity order never depends on
  server enumeration order.
- Collection names are case-sensitive in MongoDB, so `Orders` and `orders` can
  coexist and both normalize to `orders`. The collision is resolved
  deterministically (`orders`, `orders.2`) rather than failing the run.
- A collection that becomes unreadable mid-run is skipped with a warning; the
  rest of the database is still inferred.

Identity is deterministic and derived from names, never from iteration order,
a timestamp or a UUID:

```
entity_id = {source_system_id}.{database}.{collection}
```

## 6. Deterministic sampling

```python
collection.find(filter={}, sort=[("_id", 1)], limit=N)
```

- **Stable `_id` sort, bounded limit.** Every document has `_id` and it is
  always indexed, so this ordering is universally available and cheap.
- **`$sample` is deliberately not used.** A random sample would make two runs
  over an unchanged collection disagree — producing a different observed
  structure, a different schema hash and a spurious new catalog version every
  single time.
- **The filter is always empty.** Phase 5 is not a remote query tool, and a
  caller-supplied filter would also break reproducibility. A test asserts this
  at the AST level.
- **Fallback.** If the sorted read is rejected, the sample falls back to
  natural order, the caller gets a warning, and
  `sample.deterministic_sampling: false` is recorded in metadata. The weaker
  guarantee is reported, never quietly assumed.

## 7. Field-path inference

Every path observed in a document becomes one `SourceField`:

| Document | `source_name` | `nested_path` | `normalized_name` |
|---|---|---|---|
| `invoice` | `invoice` | `None` | `invoice` |
| `customer.id` | `id` | `("customer",)` | `customer.id` |
| `customer.contact.email` | `email` | `("customer", "contact")` | `customer.contact.email` |
| `items[].sku` | `sku` | `("items", "[]")` | `items_.sku` |

Hierarchy is preserved through Phase 1's existing `nested_path` semantics —
nothing is flattened into an ambiguous name, and no second schema model is
invented. The readable path is also carried verbatim in
`metadata["field_path"]` (`"items[].sku"`).

Two normalization details worth knowing:

- The `[]` element marker is not an identifier character, so it normalizes to
  a trailing underscore on the array's own segment: `items[].sku` →
  `items_.sku`. The exact path remains in `nested_path` and `field_path`.
- Distinct MongoDB paths can normalize to one name (`Amount`/`amount`,
  `_id`/`id`). Phase 1 requires uniqueness within an entity, so the first
  claimant in the fixed processing order keeps the plain name and later ones
  take `.2`, `.3`. `_id` is always processed first and therefore always wins
  `id`.

## 8. Nested documents

A nested document produces both the parent and its children:

```json
{"customer": {"id": 22, "contact": {"email": "…"}}}
```

```
customer                  object    presence 100%
customer.id               integer   presence 100%
customer.contact          object    presence 100%
customer.contact.email    string    presence 100%
```

The parent stays an `OBJECT` field in its own right, so "the customer object
was always present, but `customer.name` only half the time" is expressible.

## 9. Arrays

| Documents | `source_data_type` | `is_array` | Child paths |
|---|---|---|---|
| `{"tags": ["urgent"]}` | `array<string>` | `True` | — |
| `{"items": [{"sku": "A", "qty": 2}]}` | `array<object>` | `True` | `items[].sku`, `items[].qty` |
| `{"values": [1, "A", true]}` | `array<mixed<bool\|int\|string>>` | `True` | — |
| `{"tags": []}` | `array<empty>` | `True` | — |
| `{"matrix": [[1, 2]]}` | `array<array>` | `True` | — (not expanded) |

The parent always preserves that it is an array. Objects inside arrays are
expanded through the `[]` marker; arrays inside arrays record `array` as their
element type and stop, because a nested element marker would describe a shape
that carries no field names.

`max_array_elements_per_document` caps how many elements of any one array are
examined, so a pathological 100 000-element array cannot dominate the cost.
When it bites, the field records `array_elements_truncated: true`.

Presence for a path inside an array counts **documents**, not elements: three
matching elements in one document is one document. Total values seen is
reported separately as `values_observed`.

## 10. BSON type normalization

Observed BSON types are preserved verbatim in `source_data_type` using
MongoDB's own `$type` alias vocabulary, and mapped into the **existing**
`FieldDataType` enum. Phase 5 adds no enum member.

| BSON alias | `FieldDataType` | Note |
|---|---|---|
| `string` | `STRING` | |
| `objectId` | `STRING` | 24-char hex form; exact type kept in `source_data_type` |
| `regex` | `STRING` | |
| `int`, `long` | `INTEGER` | `long` covers `Int64` and out-of-32-bit-range ints |
| `double`, `decimal` | `DECIMAL` | `decimal` covers `Decimal128` |
| `bool` | `BOOLEAN` | |
| `date`, `timestamp` | `DATETIME` | |
| `binData` | `BINARY` | covers `bytes`, `Binary`, UUID |
| `object` | `OBJECT` | |
| `array` | `ARRAY` | |
| `javascript`, `minKey`, `maxKey` | `UNKNOWN` | recognized, but honestly unmapped |
| anything unrecognized | `UNKNOWN` | never guessed |
| `null` | — | not a type; see §11 |

Driver types are recognized **by class name before** any `isinstance` check,
because several subclass a builtin — `Int64` is an `int`, `Binary` is `bytes`,
`Code` is a `str`. An isinstance-first order would silently lose the precise
BSON type. This also means `discovery` never imports `bson` or `pymongo`.

## 11. Mixed-type handling

MongoDB fields can be inconsistent, and Phase 5 never hides it. The full
distribution is preserved:

```json
"bson_type_distribution": {"int": 1, "string": 1, "double": 1},
"mixed_types": true,
"mixed_type_resolution": "unknown"
```

The resolution policy is deterministic and pessimistic:

| Observed | Result | Why |
|---|---|---|
| only nulls | `UNKNOWN` | a null reveals no type |
| one type | that type | |
| `int` + `long`, `date` + `timestamp` | the shared normalized type | they already agree |
| `INTEGER` + `DECIMAL` | `DECIMAL` | widening is lossless — every observed value really is a decimal |
| `int` + `string`, `object` + `array` | `UNKNOWN` | no type is true of every observed value; electing the majority would state something false about the minority |

`source_data_type` renders as `mixed<double|int|string>` — alias names sorted,
so the rendering depends only on *which* types were seen, never on how many.
That matters because this string feeds the structural hash (§13).

## 12. Presence, optionality, required and nullable

For every path:

| Statistic | Meaning |
|---|---|
| `documents_sampled` | Size of the sample this claim rests on |
| `present_count` | Documents containing the path |
| `missing_count` | `documents_sampled - present_count` |
| `null_count` | Observed values that were explicitly null |
| `presence_ratio` | `present_count / documents_sampled` |
| `null_ratio` | `null_count / values_observed` |
| `values_observed` | Values seen (differs from `present_count` inside arrays) |

**This is observed presence, not database-level optionality.**

The requiredness policy:

```
required = (present_count == documents_sampled) AND (null_count == 0)
nullable = NOT required
```

That is **observed requiredness** — a statement about the sample, never a
MongoDB constraint. Two honest consequences follow, and neither is papered
over:

- A one-document sample makes every observed field `required`. Correct, and
  exactly why `documents_sampled` travels with every claim.
- A collection validator that declares `invoice` required does **not** make
  `invoice` required here if a sampled document lacked it. Validator rules are
  never folded into observed requiredness (§18).

## 13. Identity, structural hash and what is *not* hashed

Snapshot identity is content-addressed, exactly as in Phase 4:

```
schema_name = database                 (stable logical scope, or the sorted
                                        include_collections list)
schema_id   = {source_system_id}.{database}.{schema_name}.{hash[:12]}
```

so unchanged structure → identical id → still version 1, and changed
structure → new id → version N+1. No timestamp, no UUID, no iteration order.

The hash is `SourceSchema.compute_schema_hash()` — the existing Phase 1
algorithm, not a second MongoDB-specific one. Deciding what counts as
structural was the substantive design question:

**Structural (hashed).** Field existence, `source_data_type`,
`normalized_data_type`, `nested_path`, `is_array`, `is_primary_key`,
`is_unique`, and `required` / `nullable`.

**Incidental (metadata, not hashed).** `documents_sampled`, `present_count`,
`null_count`, presence ratios, type-distribution counts, estimated document
counts, partial flags, warnings.

Raising `max_documents_per_collection` from 500 to 1000 over a structurally
uniform collection therefore changes every count and produces **no** new
catalog version. But a field that stops being always-present flips `required`,
which *is* structural — and that is a genuine change in the observed schema,
so a new version is correct.

These changes all move the hash: a new field, a removed field, a type change,
a nested-structure change, and an object becoming an array.

## 14. `_id` handling

`_id` is recorded as the collection's identifier when the sample gives no
reason to doubt it — present in every sampled document and never null, which
is what MongoDB itself guarantees:

```
is_primary_key = True,  is_unique = True,  nullable = False,  required = True
source_name = "_id",  normalized_name = "id",  source_data_type = "objectId"
entity.primary_key_fields = ("id",)
```

`_id` is always emitted first, which also guarantees it wins the name `id`
against a field literally called `id`.

Only the field *definition* is exposed. No document `_id` value ever appears
in the schema, metadata, summary, logs or errors (§16), and no other field is
ever marked unique — distinctness within a sample is not a uniqueness
constraint.

## 15. Relationships: none are guessed

`SourceSchema.relationships` is **always empty** for MongoDB.

MongoDB enforces no cross-collection foreign keys, so Phase 5 fabricates none:

- A field named `customer_id`, `user_id` or `invoice_id` is a **name**, not
  evidence.
- A field holding an `ObjectId` — even one that really does point at another
  collection — is an ObjectId field and nothing more.
- DBRef and reference resolution are not inferred.

Embedded documents are represented as nested fields on the owning entity,
which is truthful and needs no invented second entity. The reason is recorded
as data in `schema.metadata["relationship_inference"]`, not only here.

## 16. Privacy: no sampled value ever leaves

Phase 5 is the one part of this framework that reads **data** rather than
metadata, which makes it the one part that could leak business content into a
published catalog. The design answer is that a value's *type* is counted and
the value itself is discarded immediately:

- The accumulators hold integers only. `FieldObservation` has no attribute
  capable of holding a value — every field is a count, a ratio, a path or a
  flag.
- Field **names** are structure and are reported (a field called `password` is
  described); field **values** never are.
- Validator bodies are not stored: they can embed literal business values
  (allowed enum members, bounds).
- Errors and warnings pass through the connector layer's `redact_text`.

`tests/erp_pipeline/discovery/test_mongodb_privacy.py` seeds documents whose
every value is a unique sentinel — an email address, a password, an IBAN, an
invoice number — and asserts that none appears in the serialized schema, the
summary, any field's metadata, the warnings, or an error message.

## 17. Safety limits

All conservative by default, so a first run against a production document
store cannot become a full scan.

| Option | Default | On reaching it |
|---|---|---|
| `max_documents_per_collection` | 500 | Sample stops; size reported, no coverage claimed |
| `max_total_documents` | 5000 | Later collections become empty entities flagged `sample_budget_exhausted` |
| `max_depth` | 8 | Parent stays `OBJECT`/`ARRAY`, marked `truncated_due_to_depth` |
| `max_fields_per_collection` | 500 | New paths stop being recorded; result marked `partial` with a warning |
| `max_array_elements_per_document` | 50 | Field marked `array_elements_truncated` |

**A limit never raises and never silently discards.** Hitting a budget marks
the result explicitly partial and records a warning — an inference run that
hit a budget has still learned something true.

Sampling honesty: `documents_sampled` and MongoDB's cheap
`estimated_document_count` are reported side by side, with
`sample.full_scan: false`. The two are deliberately never combined into a
coverage percentage, which would imply a completeness claim a sample cannot
make.

## 18. Collection validator metadata

Where a collection *does* declare a validator, its **presence** is reported:

```json
"validator_present": true,
"validator_parsed": false,
"validation_level": "strict",
"validation_action": "error"
```

Explicitly **not** done:

- The validator's JSON Schema is **not parsed** into `SourceSchema`.
  `validator_parsed: false` states this in the data itself.
- Validator rules are **not** merged into observed requiredness (§12).
- The validator body is **not** stored (§16).
- No validator code or expression is executed.

The Phase 5 output remains an observed/inferred schema even when a validator
exists. Translating a declared validator into a `SourceSchema` would be a
genuinely different feature — reading an authority rather than sampling — and
it is not implemented.

## 19. SourceSchema output

The final, authoritative result is the same Phase 1 contract every other
source produces:

```
SourceSchema (origin = INFERRED)
  └── SourceEntity (entity_kind = COLLECTION, namespace = database)
       └── SourceField (nested_path, is_array, source_data_type, …)
```

There is no `MongoCollectionSchema` or `MongoFieldSchema` competing with it.
The supplemental models — `FieldObservation`, `CollectionInferenceSummary`,
`MongoInferenceSummary`, `MongoDiscoveryResult` — are exactly that:
supplemental, aggregate-only, and deliberately kept *outside* `SourceSchema`
so sample statistics cannot perturb the structural hash.

`tests/erp_pipeline/discovery/test_cross_paradigm_demonstration.py` proves
PostgreSQL, MySQL, SQL Server and MongoDB schemas serialize through an
identical contract shape and can be consumed by one loop that never asks which
source produced them.

## 20. Schema Catalog integration

```python
result, snapshot = MongoDBInferenceService().infer_and_publish(connector, catalog)
```

Phase 5 duplicates **none** of Phase 2's logic — idempotency,
`catalog_version` assignment, snapshot immutability and history remain
entirely the catalog's responsibility.

Verified live against a real MongoDB server and the real PostgreSQL catalog:

| Action | Result |
|---|---|
| Infer → publish | `created=True`, `catalog_version=1` |
| Re-infer unchanged → publish | `created=False`, `catalog_version=1` |
| Widen the sample budget, same structure → publish | `created=False`, `catalog_version=1` |
| Add one document with new fields → publish | `created=True`, `catalog_version=2` |

with

```
SchemaDiff.added_fields == {("drift_probe", "customer.name"),
                            ("drift_probe", "approved")}
SchemaDiff.removed_fields == ()
SchemaDiff.added_entities == ()
```

## 21. Live verification status

**LIVE VERIFIED.** MongoDB 8.2.4, in an isolated Docker container bound to
`127.0.0.1:27018`.

| Item | Value |
|---|---|
| Database | `erp_phase5_test` (created and dropped by the test fixture) |
| Collections | `customers`, `invoices`, `payments`, `purchase_orders`, `validated_ledger`, `drift_probe` |
| Documents sampled | 10 across the 5 in-scope collections |
| Coverage exercised | `ObjectId`, `Decimal128`, `datetime`, `Binary`, arrays of objects and primitives, empty arrays, three-level nesting, mixed `int`/`string`/`double`, explicit nulls, optional fields, a real collection validator |
| Privilege separation | Fixtures seed as `phase5_admin`; **discovery runs as `phase5_reader`**, holding only the `read` role. A test proves that account's writes are refused by the server. |

Configuration lives in `.env` under `MONGO_PHASE5_*` (see `.env.example`); the
live tests **skip** — never fail, never fake — when it is unset or the server
is unreachable.

## 22. Limitations

Honest constraints of this approach, not defects:

- **A sample is not a proof.** Everything here describes the documents read.
- **Requiredness is observed, not enforced.** See §12.
- **A validator is detected, not translated.** See §18.
- **No relationships are inferred.** See §15.
- **Arrays of arrays are not expanded** — the element type is recorded and
  expansion stops (§9).
- **Depth and field budgets truncate** pathological documents, explicitly
  flagged rather than silently (§17).
- **Presence-derived `required`/`nullable` are structural**, so a change in
  observed presence that crosses the always-present boundary legitimately
  produces a new catalog version (§13).
- **Natural-order fallback** loses reproducibility when the `_id` sort is
  rejected; this is reported, not hidden (§6).
- **Blank and unnameable field keys** cannot be represented as Phase 1 field
  names; they are skipped or given a deterministic content-derived name, with
  a note in either case.
- **Cross-database inference** is out of scope: one connector describes one
  database.

## 23. Phase 6 boundary

Not implemented here, by design:

- Source-to-canonical field mapping, semantic typing, mapping execution.
- Document transformation into `CanonicalRecord`; any generic ETL.
- Incremental extraction and schema-drift polling.
- Validator-schema translation into `SourceSchema`.
- CSV upload, PDF/OCR, Swagger/OpenAPI, Postman parsing.
- Embeddings, Qdrant, hybrid storage, REST API, UI, RAG.

Phase 5 ends where the observed structure has been published to the catalog.
