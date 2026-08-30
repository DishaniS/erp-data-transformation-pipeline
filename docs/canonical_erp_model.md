# Canonical ERP Data Model


> **Consolidation note (2026-08-21).** This document is a development record
> for its phase. It refers to `src/bpi2020/` and/or `src/erp_integrations/`,
> which no longer exist: both were consolidated into `src/erp_pipeline/`. The
> behaviour described is preserved, but the module paths below are historical.
> See `docs/architecture/ARCHITECTURE_CONSOLIDATION_REPORT.md`.

Project `R26-SE-034` — ERP-Aware Data Transformation Pipeline
Component owner: IT22267290

**Status: Phase 1 — contracts.** This document describes what Phase 1
introduced: the canonical model as a validated, tested, in-memory data
contract in `src/erp_pipeline/schemas/`. Everything described below as
"implemented" refers to that contract layer specifically. Everything
described as "future" does not exist yet and is named here only so the
contracts can be judged against what they will have to support.

Phase 2 subsequently added persistence for these contracts
(`src/erp_pipeline/catalog/`) — see §11 below and
[`docs/schema_catalog.md`](schema_catalog.md).

---

## 1. What "Canonical ERP Data Model" means

The component must eventually accept ERP information from PostgreSQL, MySQL,
SQL Server, MongoDB, CSV files, PDFs, scanned images, OpenAPI specifications
and Postman collections — and turn all of it into one representation that the
AI layer can reason about uniformly.

The canonical model is that one representation. It is a **logical** contract:
a set of typed structures with rules about identity, provenance, sensitivity
and versioning. It says what a normalized ERP record *is*, not where it lives.

Two things it is explicitly **not**:

- It is not a PostgreSQL schema. No table, column type, index or SQL statement
  appears anywhere in the model.
- It is not a copy of any particular ERP's structure. No source vendor's naming
  or typing leaks into the canonical side.

A canonical record is equally valid held in memory, written to JSONL, returned
from an API, or indexed in a vector store.

## 2. Logical model vs physical PostgreSQL storage

> "Our common database type" does **not** mean PostgreSQL.

PostgreSQL may later be chosen as the physical engine that stores canonical
records. That is a storage decision, made in a later phase, and it must not
change the model. The separation is enforced concretely:

| Concern | Where it lives | Phase |
|---|---|---|
| What a normalized ERP record is | `canonical_models.py` | 1 (done) |
| How a record is identified | `identity.py` | 1 (done) |
| How a record is written to disk | a future storage layer | later |
| Which engine holds it | a future storage layer | later |
| How it is embedded and indexed | a future AI layer | later |

The canonical models carry no `id` column, no table name, no connection detail,
no `qdrant_point_id` and no embedding field. A test asserts this
(`test_canonical_record_has_no_storage_or_vector_fields`). Where an external
system needs a UUID rather than a string key, one is *derived on demand* from
the record's identity rather than stored as a field.

## 3. Source-system representation

A source is described by five models, none of which can reach the source:

| Model | Represents |
|---|---|
| `SourceSystem` | one ERP system: id, name, technology, environment |
| `SourceSchema` | a versioned snapshot of that system's structure |
| `SourceEntity` | a table, view, collection, dataset, API schema or document set |
| `SourceField` | one field, with the vendor's type *and* the normalized type |
| `SourceRelationship` | a foreign key, reference, embedding, parent/child or inferred link |

Three design choices make this work across such different technologies:

**A primary key is never assumed.** MongoDB collections, CSV files, API payload
schemas and document sets frequently have none. `primary_key_fields` defaults
to empty and `has_primary_key` is a query, not a precondition.

**Vendor types are preserved verbatim.** `SourceField.source_data_type` keeps
`VARCHAR(100)`, `DECIMAL(12,2)`, `NVARCHAR(MAX)`, `ObjectId`, `array<object>`,
`string($date-time)` exactly as declared. `normalized_data_type` carries the
coarse cross-source classification separately. Collapsing the two would throw
away the precision a faithful type conversion needs, and it is unrecoverable
once lost.

**Nesting is first-class.** `SourceField.nested_path` locates a field inside a
MongoDB sub-document or an OpenAPI nested object, and `RelationshipType.EMBEDDED`
describes embedding as itself rather than as a degenerate foreign key.

**Credentials are structurally impossible.** `SourceSystem` has no host, port,
user or password field, and its `metadata` is validated against a
credential-key denylist, so a serialized `SourceSystem` is always safe to log,
diff or publish. Connection configuration belongs to a future connector/secrets
layer that references a system by `source_system_id`.

## 4. Canonical record structure

```json
{
  "record_id": "erp:finance_erp_pg:invoice:inv-001",
  "record_type": "structured_record",
  "source": {
    "source_system_id": "finance_erp_pg",
    "source_type": "postgresql",
    "source_entity": "fin_invoice",
    "source_record_key": "INV-001"
  },
  "schema_version": "1.0.0",
  "content_hash": "4d9312cb11c7e82ea85cc341ca352a68877de7e0bfc25b4cf970366f5b73d9e6",
  "sensitivity": "internal",
  "provenance": {
    "schema_id": "finance_erp_pg_public_v1",
    "schema_version": "1",
    "ingestion_method": "batch_extract",
    "original_record_id": "33871",
    "source_file_path": null,
    "page_number": null,
    "api_operation": null,
    "extracted_at": "2026-08-10T09:30:00Z",
    "metadata": {}
  },
  "created_at": "2026-08-10T12:00:00Z",
  "updated_at": "2026-08-10T12:00:00Z",
  "metadata": {},
  "entity_type": "invoice",
  "normalized_data": {
    "invoice_id": "INV-001",
    "customer_id": "CUS-44",
    "amount": 25000.0,
    "currency": "LKR",
    "status": "approved"
  },
  "text_for_ai": "Invoice INV-001 for customer CUS-44."
}
```

`entity_type` and `normalized_data` are both deliberately **open**.
`entity_type` is a normalized string, not an enum, because a framework whose
value is handling any ERP cannot ship a fixed list of business objects; adding
`maintenance_work_order` must require no code change. Which keys belong in
`normalized_data` for a given entity type is decided by a mapping profile, not
by this contract — the contract guarantees only that it is a JSON object.

## 5. Canonical document structure

```json
{
  "record_id": "erp:policy_library:document:f194b2d65c37b8c1c3d48c69",
  "record_type": "document",
  "source": {
    "source_system_id": "policy_library",
    "source_type": "pdf",
    "source_entity": "finance_policies",
    "source_record_key": "finance_reimbursement_policy.pdf"
  },
  "schema_version": "1.0.0",
  "content_hash": "…",
  "sensitivity": "internal",
  "provenance": {
    "ingestion_method": "file_upload",
    "source_file_path": "data/policies/finance_reimbursement_policy.pdf",
    "page_number": 1,
    "metadata": {}
  },
  "created_at": "2026-08-10T12:00:00Z",
  "updated_at": "2026-08-10T12:00:00Z",
  "metadata": {},
  "document_id": "f194b2d65c37b8c1c3d48c69",
  "title": "Finance Reimbursement and Payment Processing Policy",
  "document_type": "policy_document",
  "mime_type": "application/pdf",
  "text": "Finance Reimbursement and Payment Processing Policy …",
  "page_count": 6,
  "language": "en"
}
```

`CanonicalRecord` and `CanonicalDocument` share `CanonicalEnvelope` —
identity, provenance, sensitivity, hashing, versioning, timestamps — and
diverge where the meaning genuinely differs. A document's payload is text plus
rendering facts, not a set of normalized business fields; forcing extracted
text into a `normalized_data` dictionary would make both models poorer. The
shared base stops exactly where the shared meaning stops.

A PDF and a scanned image use the *same* contract and differ only in
provenance (`source_type: pdf` vs `image`, plus OCR details in
`provenance.metadata`).

**Chunking is not implemented.** The model is compatible with it — a future
chunk can reference this `record_id` as its parent and carry its own ordinal —
but no chunk model, splitter or overlap policy exists in Phase 1.

## 6. Stable identity strategy

```
erp:{source_system_id}:{entity_type}:{stable_source_key}
```

Every component is normalized: lower-cased, whitespace collapsed to `_`, any
character outside `[a-z0-9_.-]` replaced, repeated underscores collapsed.

**Why `source_system_id` is in the identifier.** A business key is only unique
*within* the system that issued it. Invoice `1001` in ERP A and invoice `1001`
in ERP B are different invoices, and an identifier that omits the system would
silently merge them — including in any vector store keyed on the derived UUID.

```
ERP A invoice 1001  ->  erp:erp_a:invoice:1001
ERP B invoice 1001  ->  erp:erp_b:invoice:1001
```

**Why the grammar is unambiguous.** Normalization removes `:` from every
component, so a hostile or accidental key containing colons cannot forge extra
components. `parse_canonical_id` recovers the three parts exactly.

**No database SERIALs.** `stable_source_key` must be a business or natural key.
A source's auto-increment id or ObjectId may be recorded in
`provenance.original_record_id` for traceability, but it never enters identity.
This is the Phase 0 lesson carried forward: a rebuilt table re-issues SERIALs,
and anything identified by one silently detaches from every file and vector
derived from it.

**Derived UUIDs.** `make_deterministic_uuid(record_id)` produces a UUIDv5 for
external systems that accept only integers or UUIDs. The human-readable
`record_id` remains the authoritative identity; the UUID is a projection for
one consumer.

**Relationship to Phase 0.** `src/bpi2020/common/stable_ids.py` is untouched
and remains the identity contract for the BPI prototype. `erp_pipeline` restates
the same *principles* independently and never imports `bpi2020`, so the
framework carries no dependency on a source-specific prototype. The two are
compatible in the ways that matter and provably separate where they must be:

| | Phase 0 (`bpi2020`) | Phase 1 (`erp_pipeline`) |
|---|---|---|
| Prefixes | `event:` `case:` `document:` | `erp:` |
| Normalization | `normalize_key_component` | `normalize_identifier` — byte-identical, asserted by test |
| UUID derivation | `uuid5(NAMESPACE_URL, "bpi2020/…")` | `uuid5(NAMESPACE_URL, "erp_pipeline/…")` — same algorithm, different namespace |
| Content hash | sha256 over `{record_id, text_for_ai, metadata}` | sha256 over `{record_id, text_for_ai, content}` — same determinism properties, different envelope |

The disjoint prefixes mean records from both schemes can coexist in one store
without collision. The identical normalization means the two can never drift
into producing different ids for the same input.

## 7. Provenance

Provenance answers: *where did this canonical record come from?* It is split
across two typed models rather than one loose dictionary.

`SourceReference` (required) is the identity-bearing part — which system, which
technology, which entity, which business key. These are the inputs to the
canonical id.

`RecordProvenance` (optional) is the ingestion detail — which schema snapshot
described the record, how it was obtained, and where relevant which file page,
which API operation, or the source's own original record id.

Two exclusions are deliberate:

- **No raw record contents.** Provenance is a pointer, not a second copy of the
  data. Duplicating payloads would multiply both storage and the blast radius
  of a leak.
- **No credentials**, enforced by the same denylist used for `SourceSystem`.

## 8. Sensitivity

Every canonical artifact carries a `SensitivityLevel` — `public`, `internal`,
`confidential` or `restricted` — defaulting to `internal` rather than to the
most permissive value.

Phase 1 makes the classification *explicit and mandatory*; it does not enforce
handling policy. Enforcement (redaction before embedding, access control on
retrieval, exclusion from certain indexes) belongs to the phases that actually
move data. Related, `DataQualityIssue.original_value_summary` is length-bounded
and `summarize_value(..., redact=True)` reports only a value's shape, so a
quality report cannot quietly become a less-protected copy of sensitive data.

## 9. Model versioning

Four constants live in `src/erp_pipeline/version.py` and nowhere else:
`CANONICAL_MODEL_VERSION`, `SOURCE_MODEL_VERSION`, `MAPPING_MODEL_VERSION`,
`RUN_MODEL_VERSION`. A test asserts no model module defines its own literal.

Semantics:

| Change | Meaning |
|---|---|
| PATCH | Documentation or validation-message clarification. Consumers unaffected. |
| MINOR | Backwards-compatible addition — a new optional field or enum member. Old records stay valid; unknown fields must be ignored, not rejected. |
| MAJOR | Breaking change — a field removed, renamed, made required, or changed in type or meaning; or **any change to an identity rule**. Stored records cannot be assumed readable and the breaking phase must supply a migration. |

Identity rules are MAJOR by definition: changing how a record id is derived
silently re-identifies every stored record and every derived vector.

Records persist the version that produced them, so a future reader can branch
on it. **No migration framework exists in Phase 1** — the version is made
explicit and recorded, nothing more.

## 10. How every source technology converges on this model

```
Heterogeneous ERP Source
        |
        v
   Source Model            SourceSystem / SourceSchema / SourceEntity
                           SourceField / SourceRelationship
        |
        v
[future Mapping Engine]    consumes MappingProfile + FieldMapping
                           (contracts defined, engine NOT built)
        |
        v
 Canonical ERP Model       CanonicalRecord / CanonicalDocument
                           stable identity + provenance + sensitivity
        |
        v
[future Physical Storage / AI Processing]
```

| Source | How the source model holds it | What a future phase must build |
|---|---|---|
| PostgreSQL / MySQL / SQL Server | `EntityKind.TABLE`, declared columns, `FOREIGN_KEY` relationships, vendor types preserved | catalog introspection |
| MongoDB | `EntityKind.COLLECTION`, `nested_path` for sub-documents, `EMBEDDED` relationships, no primary key required | collection sampling and shape inference |
| CSV | `EntityKind.DATASET`, header text in `source_name` and a normalized name beside it, `SchemaOrigin.UPLOADED` | header/type inference |
| PDF / image | `SourceType.PDF` / `IMAGE`, `CanonicalDocument` with page and OCR provenance | parsing and OCR (already exists for BPI in `bpi2020`) |
| OpenAPI | `EntityKind.API_SCHEMA`, `SchemaOrigin.API_SPEC`, `$ref` in `namespace`, `string($date-time)` preserved | specification parsing |
| Postman | `EntityKind.API_SCHEMA`, `SchemaOrigin.INFERRED`, request metadata, shapes inferred from examples | collection parsing |

Each of these is proven representable by a test in
`tests/erp_pipeline/test_source_models.py`. No parser is implemented.

The convergence itself is demonstrated in
`tests/erp_pipeline/test_cross_source_canonicalization.py`: four ERP systems
describe the same invoice with four incompatible field sets, and all four
produce byte-identical `normalized_data` while keeping distinct provenance and
four non-colliding identities.

## 11. What Phase 1 itself deliberately did not implement

This section describes Phase 1's own scope: `src/erp_pipeline/schemas/`, the
pure contract layer. **Phase 2 subsequently added persistence, schema catalog
storage, and deserialization** — see the "Phase 2 added" note after each item
below and [`docs/schema_catalog.md`](schema_catalog.md) for the full design.
Nothing in this section describes a defect; it describes a deliberate
phase boundary that has since moved.

**Connectivity and discovery** — still not implemented. No PostgreSQL, MySQL,
SQL Server or MongoDB connector; no `INFORMATION_SCHEMA` introspection; no
MongoDB sampling; no CSV parser; no OpenAPI or Postman parser.
*(Phase 2 added the schema repository these connectors will eventually write
into — see below — but not the connectors themselves.)*

**Mapping** — still not implemented. No mapping engine, no automated
suggestion, no AI-assisted matching, no type-conversion engine.
`MappingProfile`, `FieldMapping` and `TransformationRule` define what such an
engine will consume, and a transformation is an operation name plus a JSON
config — never a code string, so nothing can be passed to `eval`, `exec` or a
shell.
*(Phase 2 added persistence for these contracts — mapping profiles can be
saved and reloaded exactly as defined — but nothing executes a
`TransformationRule`. No mapping engine exists.)*

**Execution** — still not implemented. No orchestrator, no ETL execution, no
schema-drift detection, no hybrid storage tiering.

**Interfaces** — still not implemented. No FastAPI, no REST endpoints, no
frontend.

**Persistence and schema catalog** — *implemented by Phase 2, not Phase 1.*
Phase 1 itself introduced only in-memory, I/O-free contracts with no way to
store or retrieve them. Phase 2 (`src/erp_pipeline/catalog/`) subsequently
added:
- PostgreSQL-backed schema catalog persistence, in a dedicated `erp_catalog`
  namespace
- versioned, immutable schema snapshot persistence (`SourceSystem` /
  `SourceSchema` / `SourceEntity` / `SourceField` / `SourceRelationship`)
- mapping-profile persistence (`MappingProfile` / `FieldMapping` /
  `TransformationRule`)

See [`docs/schema_catalog.md`](schema_catalog.md) for the full design,
including what Phase 2 itself still does not implement (live discovery,
connectors, a mapping engine).

**Serialization direction** — *deserialization was added by Phase 2, not
Phase 1.* Phase 1's `erp_pipeline/schemas/serialization.py` provides only
`model → JSON`. Reading contracts back (`JSON → model`) is implemented
separately in `erp_pipeline/schemas/deserialization.py`, added when Phase 2
needed to reconstruct persisted contracts from catalog rows. Both modules
remain pure and I/O-free — deserialization builds models via explicit
constructors only, with no `eval`/`exec`/`pickle`/dynamic class loading.

**No change to Phase 0.** `src/bpi2020/` is untouched and remains the working,
stabilized prototype.
