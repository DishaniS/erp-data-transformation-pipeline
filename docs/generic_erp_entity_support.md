# Phase 2 — Generic ERP Entity Support

**ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline**
Member 4 · IT22267290 · Project R26-SE-034

| | |
|---|---|
| Phase | 2 of the final completion plan |
| Nature | **Additive.** The canonical path is untouched. |
| Baseline (after Phase 1) | 3071 collected · 3008 passed · 63 skipped · 0 failed |
| Date | 2026-08-22 |

---

## 1. Problem

The structured pipeline could only index entities the curated canonical model
covers — `invoice`, `customer`, `purchase_order`. Everything else in a real ERP
was unreachable:

```
employees · suppliers · assets · departments · warehouses
product_master · vendor_ledger · machine_maintenance_records · custom_table_xyz
```

For those, `MappingService` produced no profile, `run_map` raised
`MAPPING_EMPTY`, and the job died before extraction. An employee record could
never become an `AIRepresentation`, so it could never be embedded, stored or
searched.

**The trap in fixing this** is that "mapping produced no profile" has *two*
completely different causes:

| Cause | Correct response |
|---|---|
| No canonical entity claims this table | index it on its own terms |
| A canonical entity claims it, but fields are **ambiguous** | **stop — a human must decide** |

Treating the second as the first would have destroyed the refusal mechanism
that makes the mapping engine trustworthy — and would have done it silently.

---

## 2. Architecture selected

```
ERP source entity
       │
   schema discovery
       │
       ├─────────────────────────────┬──────────────────────────────┐
       │                             │                              │
 canonical entity matched     canonical entity matched        NO canonical
 and fields decided           but fields AMBIGUOUS            entity matches
       │                             │                              │
  MAP → MappingProfile        REFUSED — human decides        SOURCE_NATIVE_GUARD
       │                             │                              │
  TransformationService              ▼                    SourceNativeTransformer
       │                     PUT /v1/mappings/{id}                  │
       └─────────────────────────────┴──────────────────────────────┘
                                     │
                              CanonicalRecord
                                     │
                              AIRepresentation
                                     │
                                  embedding
                                     │
                        HOT / WARM / COLD  (unchanged)
```

Two paths converge on **one** `CanonicalRecord`, and everything downstream is
shared: representation, embedding, tier routing, the record store, search and
`GET /v1/records/{id}`.

---

## 3. Why the canonical mapping was preserved

The mapping engine's most defensible property is that it **refuses** when two
canonical targets tie. On the 68-label benchmark that refusal is measured:
correct-refusal rate **1.0** over 8 negative labels, auto-selection precision
**1.0 (60/60)**.

A generic fallback triggered by "mapping failed" would have made that refusal
meaningless — every ambiguous field would simply have been indexed under its
source name, and the human decision the engine demanded would never happen.

So admission is decided by a **different question**, answered by an existing
contract: `MappingResult.unmatched_entities`, documented as *"source entities no
canonical entity could be matched to"*. That is a statement about **vocabulary
coverage**, not about whether mapping succeeded.

| Situation | `unmatched_entities` | Source-native |
|---|---|---|
| `invoices`, mapping clean | absent | **REFUSED** — use `structured_pipeline` |
| `invoices`, 3 fields ambiguous | absent | **REFUSED** — resolve the mapping |
| `employees` | present | **admitted** |

---

## 4. Source-native representation design

`src/erp_pipeline/transformation/source_native.py` — `SourceNativeTransformer`.

Fields keep the names the source gave them, because there is no canonical
concept to rename them to. Values are converted using the **discovered
schema's own declared types**, reusing `type_converter.convert` rather than
repeating conversion rules — so a source-native record gets exactly the same
`Decimal`, date and boolean handling a canonical record does. The only
difference is where the target type comes from: a mapping profile for the
canonical path, a `SourceField` for this one.

**Measured output** for an `employees` row:

```
record_id   : erp:legacy_hr:employees:emp002
entity_type : employees
data        : {"department": "Finance", "employee_id": "EMP002",
               "job_title": "Accountant", "name": "Nimal Silva"}
metadata    : {"source_native": true,
               "business_key_name": "employee_id",
               "business_key_value": "EMP002"}
```

and the **existing** representation builder, unchanged:

```
Entity: Employees
Source Entity: employees
Source System: legacy_hr
Department: Finance
Employee Id: EMP002
Job Title: Accountant
Name: Nimal Silva
```

### Why the result is still a `CanonicalRecord` (Design Requirement 12)

Because the contract says it may be. `CanonicalRecord` documents `entity_type`
as *"an **open normalized string** — invoice, customer, purchase_order,
goods_receipt, **whatever the domain needs**… a new ERP domain object requires
no change to this file"*, and `normalized_data` as an open JSON object whose
keys *"are decided by a mapping profile, not by this contract"*. It even names
`goods_receipt`, which is **not** in `DEFAULT_CANONICAL_MODEL`.

"Canonical" in that contract means **normalized and technology-independent**,
not "drawn from the curated vocabulary". Reusing it is therefore honest rather
than convenient — and it is what avoids a parallel model, a parallel store and
a parallel search path.

### Why plurals are not singularised

`entity_type` is the source entity's own `normalized_name` — `employees`, not
`employee`. English pluralisation is irregular (`address` → `addres`,
`company` → `companie`), and a wrong guess would be **baked into a stable
record id** that later runs would keep reproducing. The canonical model relates
`invoices` to `invoice` through **declared aliases**; an uncovered entity has
none, so its own name is the only non-guessed answer. This matches the
framework's existing refusal to guess surrogate keys, sensitivity or ambiguity.

---

## 5. Identity strategy

Preference order, and why each rank sits where it does:

| Rank | Source | Why |
|---|---|---|
| 1 | `key_fields` in the job options | An explicit human decision outranks anything inferred |
| 2 | `SourceEntity.primary_key_fields` | The source system's own declaration — a fact, not a guess. Composite keys joined in **declared order** |
| 3 | `SourceRecord.record_key` | The extractor's own key — **only if it is not a bare number** |
| — | *(nothing)* | **Refused.** The first column of a table is not a key just because it is first |

Composite keys are supported: `warehouse_stock` keyed on
`warehouse_id + product_id` yields `erp:wms:warehouse_stock:wh-1_p-77`, with the
original `WH-1|P-77` preserved verbatim in metadata.

### A defect found during implementation, and fixed

The first working version keyed a CSV-uploaded `employees` file on
`erp:file_source:employees:1` — a **row number**. CSV schemas declare no primary
key (deliberately: *"a distinct-looking column is not one"*), so the fallback
took the extractor's record key, which for a CSV row is its position.

That is exactly the surrogate-key problem the framework already refuses: the id
would change whenever the file was reordered, silently re-identifying every
record and orphaning every stored vector. The fallback now applies the
framework's own `looks_like_surrogate_key` guard and **refuses**, telling the
caller to declare `key_fields`.

**Measured, both branches:**

```
no key_fields   -> 0 records indexed, warning names the reason, no id invented
key_fields=["employee_id"]
                -> erp:file_source:employees:emp002
                   erp:file_source:employees:emp003
                   both resolvable via GET /v1/records/{id}
```

---

## 6. Binary safety behaviour

A BLOB base64-encoded into the AI text would be embedded as thousands of
meaningless characters, displacing the fields that carry actual meaning. So a
field whose **discovered schema** says `normalized_data_type = BINARY` is:

- **excluded** from `normalized_data` and therefore from `text_for_ai`
- **recorded** in `metadata["binary_fields"]`
- **reported** on the job as a warning, so nobody assumes it was read

**Measured** for `employees` with a JPEG `birth_certificate`:

```
data keys                 : ['employee_id', 'name']
metadata.binary_fields    : ['birth_certificate']
base64 in text_for_ai     : False
raw bytes in text_for_ai  : False
EMP002 / Nimal Silva      : still present
```

Binary detection is read **from the schema**, not sniffed from values —
discovery already normalised BYTEA, LONGBLOB, VARBINARY, IMAGE and `binData`
onto `FieldDataType.BINARY`, and re-deciding it here would be a second, weaker
answer to a question already answered.

**Reading the content — magic bytes, OCR, PDF extraction, chunking — is Phase 3
and is not done here.**

---

## 7. Orchestration / API changes

**No new endpoint.** `POST /v1/jobs` already expresses the operation.

| Addition | Why |
|---|---|
| `JobType.SOURCE_NATIVE_PIPELINE` | Its own job type rather than a flag, so a caller cannot arrive here by accident. Choosing it is a **statement** that no canonical vocabulary applies |
| `PipelineStage.SOURCE_NATIVE_GUARD` | Distinct from `MAP` because it produces an **admission decision**, not a profile. Overloading `MAP` would have muddied its meaning |
| `SourceNativeNotPermittedError` → **409** | A conflict, not a bad request: the job is well-formed, but this entity already belongs to the canonical path |
| `options.key_fields` | The explicit identity decision for sources that declare no key |

Stage plan — the tail is **byte-for-byte the structured tail**:

```
CSV  : source_native_guard → extract → transform → validate → load
                           → ai_build → embed → tier_route
live : discover → source_native_guard → … (same tail)
```

`MAP` appears in `not_applicable` with a stated rationale, so the job record
explains why it did not run.

### Request

```json
POST /v1/jobs
{
  "job_type": "source_native_pipeline",
  "source_id": "src_hr",
  "schema_id": "legacy_hr.employees.a1b2c3",
  "upload_id": "upl_…",
  "options": { "key_fields": ["employee_id"] }
}
```

---

## 8. Files changed

### Added

| File | Purpose |
|---|---|
| `src/erp_pipeline/transformation/source_native.py` | The transformer, identity resolution, binary guard |
| `tests/erp_pipeline/transformation/test_source_native.py` | 27 tests |
| `tests/erp_pipeline/api/test_source_native_pipeline.py` | 16 tests |
| `docs/generic_erp_entity_support.md` | This report |

### Changed — all additive

| File | Change |
|---|---|
| `src/erp_pipeline/orchestration/models.py` | `JobType.SOURCE_NATIVE_PIPELINE`, `PipelineStage.SOURCE_NATIVE_GUARD` |
| `src/erp_pipeline/orchestration/planner.py` | `SOURCE_NATIVE_TAIL`, `_source_native()`, dispatch |
| `src/erp_pipeline/orchestration/stages.py` | `run_source_native_guard`, `_run_source_native_transform`, one branch in `run_transform`, handler registration |
| `src/erp_pipeline/orchestration/service.py` | `transform_source_native()` |
| `src/erp_pipeline/orchestration/errors.py` | `SourceNativeNotPermittedError` |
| `src/erp_pipeline/orchestration/__init__.py` | export |
| `src/erp_pipeline/api/responses.py` | status 409 |

**No existing function's behaviour was altered.** `run_map`, `MappingService`,
`TransformationService`, `canonical_record_to_representation`, the embedding
service, the storage router and Phase 14 are untouched.

---

## 9. Tests added

**43 new tests, all passing.**

| File | Tests | Covers |
|---|---:|---|
| `test_source_native.py` | 27 | B, C, D, E, F, H — uncovered entity, arbitrary entities, composite keys, identity refusal, row-number refusal, binary guard, stability, provenance, business-key preservation |
| `test_source_native_pipeline.py` | 16 | A, G — planner shape, capability rules, **the bypass gate**, admission, record resolution, canonical path untouched, capabilities advertisement |

---

## 10. Targeted test results

```
tests/erp_pipeline/transformation/test_source_native.py      27 passed
tests/erp_pipeline/api/test_source_native_pipeline.py        16 passed

mapping + transformation + orchestration + ai + storage + sync + api
                                          1240 passed, 37 skipped in 230.35s
```

---

## 11. Full regression

| | After Phase 1 | After Phase 2 |
|---|---|---|
| Collected | 3071 | **3114** (+43, exactly the new tests) |
| Passed | 3008 | **3051** (+43) |
| Skipped | 63 | **63** (unchanged) |
| **Failed** | **0** | **0** |
| **Errors** | **0** | **0** |
| Duration | 405.94s | 314.86s |

**Zero failures, zero errors, and the skip count is unchanged.** No new skip was
introduced and no existing test was modified.

---

## 12. Existing research artifact impact

**None. No artifact was overwritten and none needed to be.**

### Mapping benchmark — identical

| Metric | Before | After |
|---|---|---|
| top-1 accuracy | 1.0 | **1.0** |
| top-3 recall | 1.0 | **1.0** |
| auto-selection precision | 1.0 (60/60) | **1.0 (60/60)** |
| automatic coverage | 0.8824 | **0.8824** |
| ambiguity rate | 0.0 | **0.0** |
| unmapped rate | 0.0882 | **0.0882** |
| correct refusal rate | 1.0 | **1.0** |
| alias-independent top-1 | 1.0 (18/18) | **1.0 (18/18)** |

### Phase 14 evaluation — identical

Every metric across all three methods unchanged; the three documented failures
(`po-05`, `proc-02`, `sap-04`) unchanged.

This is expected and is the point: Phase 2 added a path for entities the
benchmark never contained. **No benchmark vocabulary or evaluation data was
touched.** `DEFAULT_CANONICAL_MODEL` still holds exactly 3 entities and 14
fields — no `employee` entity was added.

---

## 13. Known limitations

1. **A CSV-uploaded entity requires explicit `key_fields`.** CSV declares no
   primary key, and the row number is refused. This is a deliberate refusal, not
   an oversight — but it means the caller must know which column identifies a
   record.
2. **`entity_type` is the plural source name** (`employees`, not `employee`).
   Honest, but it means a source-native record and a canonical one use different
   naming conventions.
3. **Business identity is in metadata, not a Qdrant filter.** Preserved as
   `business_key_name` / `business_key_value` for Phase 4; not yet filterable.
4. **Binary fields are recorded but not read.** Phase 3.
5. **Conversion failures fall back to the source value as text**, with a note in
   `metadata["conversion_notes"]`. This path exists for data the framework has no
   model for, so dropping a value for not fitting an *inferred* type would lose
   information — but the loss of typing is visible rather than silent.
6. **The guard runs the mapping engine on every source-native job** to decide
   admission. Correct, but it means the job cannot run without a mapping service
   configured.
7. **Search relevance for source-native records is untested at scale.** They
   enter the normal index; whether `Find employee EMP002` ranks well is a
   retrieval question Phase 4's filters address directly.

---

## 14. Explicit Phase 3+ exclusions

Confirmed **not** implemented:

```
BLOB → image/PDF detection            NOT ADDED
BLOB OCR                              NOT ADDED
BLOB document chunking                NOT ADDED
BLOB embedding                        NOT ADDED
business identity Qdrant filters      NOT ADDED
document_type filters                 NOT ADDED
page/chunk payload fields             NOT ADDED
representation text retrieval endpoint NOT ADDED
automatic document upload indexing    NOT ADDED
schema vectors                        NOT ADDED
URL-column fetching                   NOT ADDED
sync scheduler                        NOT ADDED
new sensitivity workflows             NOT ADDED
frontend search                       NOT ADDED
```

`FILTERABLE_FIELDS` is still the same 5 fields. Qdrant collections are still
`erp_vectors_hot` / `erp_vectors_warm` plus the cold archive — **no new
collection was added.** No new endpoint was added.

---

## 15. EMP002 readiness after Phase 2

```
EMP002 STRUCTURED ENTITY SUPPORT:      WORKING
EMP002 BIRTH-CERTIFICATE BLOB SUPPORT: NOT YET — PHASE 3
```

**Working now**, verified end-to-end through `POST /v1/jobs`:

```
employees table (or CSV) → source_native_guard (admitted)
  → extract → transform → validate → load → ai_build → embed → tier_route
  → erp:file_source:employees:emp002
  → GET /v1/records/erp:file_source:employees:emp002 → 200
```

With an embedding service configured, the measured job produced
`records_read: 2 · records_transformed: 2 · representations_built: 2 ·
embeddings_generated: 2`, and `tier_route` succeeded. Vectors reached the
storage layer; `vectors_failed: 2` only because no Qdrant was configured in that
harness.

**Not working yet:** the `birth_certificate` BLOB is recorded in
`metadata["binary_fields"]` and deliberately not opened. OCR, chunking and
document indexing are Phase 3.

---

## 16. Full regression

```
collected: 3114
passed:    3051
failed:    0
errors:    0
skipped:   63     (identical to Phase 1)
duration:  314.86s (0:05:14)
```

### Preservation verification

| Check | Result |
|---|---|
| Canonical entities | `('invoice', 'customer', 'purchase_order')`, 14 fields — **unchanged** |
| `employee` added to canonical model? | **No** — the requirement was explicitly not solved that way |
| `FILTERABLE_FIELDS` | 5 fields — **unchanged** (Phase 4 owns this) |
| REST operations | **23** — unchanged, no new endpoint |
| `JobType` | 5 → **6**, the one addition |
| Qdrant collections | `erp_vectors_hot` / `erp_vectors_warm` — **unchanged** |
| Mapping benchmark | all 8 metrics **identical** |
| Phase 14 evaluation | all metrics **identical**; 3 documented failures unchanged |

**Nothing was committed.**
