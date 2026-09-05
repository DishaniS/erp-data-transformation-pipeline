# Integration Stabilization Report

**Component:** IT22267290 — ERP-Aware Data Transformation Pipeline
**Project:** SLIIT 4th Year Research, `R26-SE-034`
**Date:** 2026-08-21 (suite verified 2026-08-22)
**Scope:** fix five existing integration-contract defects. No new features, no architectural refactor, no Phase 14.

---

## 1. Scope

Five defects, all identified in `IT22267290_FULL_CODEBASE_RESEARCH_AUDIT.md`, all of which would have affected downstream RAG, Member 1 governance, Member 2 MCP integration, the storage novelty claim, or runtime reliability.

| # | Defect | Audit reference | Status |
|---|---|---|---|
| 1 | A search hit could not be resolved back to its canonical record | §6.3b, Issue 2 | **FIXED** |
| 2 | `SearchRequest.filters` was published in the API contract and silently ignored | §17.2, Issue 8 (H1) | **FIXED** |
| 3 | The schema endpoint reported an empty string as every field's type | §25.3, Issue 8 (C3) | **FIXED** |
| 4 | Record sensitivity never reached the storage router | §6.3a, Issue 1 | **FIXED** |
| 5 | `erp-bootstrap` created only one of the four `erp_runtime` tables | §5.4, Issue 8 (H6) | **FIXED** |

Each of the five shares one failure mode: **a wrong answer returned with a 200 OK**. An unfiltered result labelled as filtered, an empty type labelled as a type, an id that resolves to nothing, a routing decision that ignored the record's classification, and a bootstrap that reported success while leaving three tables missing. None would have surfaced as an error; all would have surfaced as a downstream consumer quietly getting the wrong thing.

---

## 2. Files Changed

### Production code

| File | Change | Reason |
|---|---|---|
| `storage/models.py` | `StorageRecordMetadata` gains `canonical_record_id`, `source_system_id`, `source_entity`, `document_id`; all four added to `to_dict()` | Storage held no canonical reference, so a hit could not name its record. The other three are what retrieval filters match on |
| `storage/state.py` | Four nullable columns in `create_state_sql`; new `alter_state_sql()` (`ADD COLUMN IF NOT EXISTS`) and `create_state_filter_index_sql()`; `bootstrap_storage_schema` runs both; `save()` writes the columns with `COALESCE` on conflict; new `_optional_column()` used by `_row_to_metadata` | Additive migration for an existing research database, plus a read path that survives a database that has not been re-bootstrapped |
| `storage/filters.py` | **NEW.** `SearchFilters`, `FILTERABLE_FIELDS`, `UnknownFilterFieldError`, `InvalidFilterValueError` | One validated, closed filter vocabulary shared by the online and archive paths |
| `storage/hybrid_store.py` | `store()` accepts and carries the four identity fields; `SearchHit` gains `canonical_record_id` and `entity_type`; `search()` accepts `filters`; `_merge()` enriches from state and re-checks filters; `_search_cold()` filters before rehydration; `_authoritative_tiers` → `_state_by_vector`; new `_tier_search()` helper | Propagation, filtering, and hit enrichment |
| `storage/hot_tier.py`, `storage/warm_tier.py` | `search()` accepts `query_filter` and passes it to `query_points` | Server-side filtering, so the ANN search itself is constrained |
| `storage/migration.py` | `_payload_for()` adds the identity fields, omitting `None` | The vector payload is what a server-side filter matches |
| `storage/service.py` | `StorageProfile.from_metadata()`; `store()` derives a profile from the record when none is supplied; `search()` accepts `filters` | Sensitivity reaches the router; filters reach the store |
| `ai/service.py` | `CARRIED_IDENTITY_KEYS` and `_carried_identity()`; `_record()` copies the identity subset onto `EmbeddingRecord.metadata` | The hop that previously dropped the canonical reference and the sensitivity |
| `orchestration/service.py` | `store_vector()` derives a `StorageProfile` from the record's metadata | Orchestration supplies accurate metadata; it still never names a tier |
| `api/serialization.py` | **NEW.** `field_response`, `entity_response`, `relationship_response`, `schema_response` | One explicit contract→response mapping, replacing two hand-rolled `getattr` chains |
| `api/schemas.py` | `SearchHitResponse.canonical_record_id`; `SearchResponse.filters_applied`; new `SchemaFieldResponse`, `SchemaEntityResponse`, `SchemaRelationshipResponse`; `SchemaResponse.entities` typed, `.relationships` added; `SearchRequest.filters` documented | Typed, additive response contract |
| `api/routers_data.py` | `search()` validates and applies filters and returns the canonical id; `get_schema()` delegates to `schema_response()`; new `_enum_or_none()` | The two endpoint fixes |
| `runtime/bootstrap.py` | `bootstrap_all()` calls `bootstrap_runtime_persistence` alongside `bootstrap_record_schema` | The missing three tables |

### Tests

| File | Tests | Covers |
|---|---|---|
| `tests/erp_pipeline/storage/test_canonical_resolution.py` | 19 | Fix 1, unit level |
| `tests/erp_pipeline/storage/test_search_filters.py` | 38 | Fix 2, unit + store level |
| `tests/erp_pipeline/storage/test_sensitivity_routing.py` | 29 | Fix 4, including the security invariant |
| `tests/erp_pipeline/api/test_search_resolution_and_filters.py` | 25 | Fixes 1 + 2, **acceptance** |
| `tests/erp_pipeline/api/test_schema_contract_fields.py` | 26 | Fix 3, **acceptance** |
| `tests/erp_pipeline/runtime/test_bootstrap_completeness.py` | 34 | Fix 5, **acceptance**, incl. live PostgreSQL |
| **Total** | **171** | |

### Documentation

| File | Change |
|---|---|
| `README.md` | New "Resolving a search hit back to its record", "Retrieval filters" and "Sensitivity and storage placement" subsections; API table rows for `/v1/search` and `/v1/schemas/{id}` updated; known issue #2 marked **FIXED** |
| `artifacts/openapi_contract_snapshot.json` | Regenerated (by the existing test that owns it — see §8) |
| `docs/architecture/INTEGRATION_STABILIZATION_REPORT.md` | This document |

---

## 3. Search Resolution

### The two identifiers, and why they must stay separate

```text
representation_id   ai:invoice:erp_finance_erp_invoice_inv-001
canonical_record_id erp:finance_erp:invoice:inv-001
```

They are not two spellings of one thing.

- **`representation_id`** identifies *the thing that was embedded*. One canonical record can produce several — a document produces one representation per chunk — and it is the key the vector store, the tier state and the embedding cache are all keyed by.
- **`canonical_record_id`** identifies *the business record*. It is what `GET /v1/records/{id}` accepts and what a downstream consumer joins on.

They cannot be derived from one another. `make_representation_id` normalizes its input, and normalization replaces `:` with `_`:

```text
erp:finance_erp:invoice:inv-001
        ↓ normalize_identifier
erp_finance_erp_invoice_inv-001        ← the ':' positions are gone
```

A `source_system_id` may itself contain underscores, so splitting the normalized form is ambiguous. **Any attempt to reconstruct the canonical id by parsing would produce an id that resolves to nothing** — which is exactly the failure the audit found. The reference therefore travels forward, explicitly, at every hop.

### The chain, as implemented

```text
CanonicalRecord.record_id
   │  ai/representation.py: canonical_record_to_representation()
   ▼
AIRepresentation.metadata["canonical_record_id"]
   │  ai/service.py: _carried_identity()  ← the hop that previously dropped it
   ▼
EmbeddingRecord.metadata["canonical_record_id"]
   │  storage/hybrid_store.py: store()   (explicit argument wins; else carried)
   ▼
StorageRecordMetadata.canonical_record_id     → erp_vector_storage.vector_storage_state
   │                                          → and the Qdrant payload
   │  storage/hybrid_store.py: _merge()   (read from STATE, the authority)
   ▼
SearchHit.canonical_record_id
   │  api/routers_data.py: search()
   ▼
SearchHitResponse.canonical_record_id  →  GET /v1/records/{canonical_record_id}
```

### Honest absence

`canonical_record_id` is `null` when the stored vector genuinely has no canonical reference — it predates the field, or derives from no canonical record. It is never filled with a guess. Three behaviours protect that:

- `_payload_for()` **omits** a `None` identity key rather than writing `null`, because a key present-and-null and a key absent behave differently under a Qdrant match.
- `save()` uses `COALESCE(EXCLUDED.x, table.x)` so a later write that happens to carry no reference cannot erase one an earlier write established.
- `store()` falls back to the existing state row before defaulting to `None`.

---

## 4. Search Filters

### Supported fields

A closed set of five, all of which exist **both** on `StorageRecordMetadata` and in the vector payload — which is what lets the online and archive paths agree:

| Field | Validation |
|---|---|
| `entity_type` | non-empty string |
| `source_system_id` | non-empty string |
| `source_entity` | non-empty string |
| `sensitivity` | must be a `SensitivityLevel` member |
| `document_id` | non-empty string |

Equality only. No boolean composition, no ranges, no nesting. A general query language over a vector store is a large surface whose failure mode is silently returning the wrong subset; what a consumer actually needs is *only invoices*, *only this ERP*, *only this document*.

### Implementation path

```text
POST /v1/search { "filters": {...} }
   │  api/routers_data.py: SearchFilters.from_mapping()
   │     unknown field  → UnknownFilterFieldError → InvalidPipelineRequestError → 422
   │     bad enum value → InvalidFilterValueError → InvalidPipelineRequestError → 422
   ▼
StorageService.search(..., filters=...)
   ▼
HybridVectorStore.search(..., filters=...)
   ├── HOT   → SearchFilters.to_qdrant_filter() → query_points(query_filter=...)
   ├── WARM  → same
   ├── COLD  → SearchFilters.matches() applied to tier state BEFORE rehydration
   └── merge → re-checked against state as a backstop
```

Three properties worth naming:

1. **Pushed server-side, not post-filtered.** Over-fetching and trimming would silently return fewer than `top_k` matches whenever the filter is selective.
2. **Cold is filtered before decryption.** Correct *and* cheaper: a filtered-out archive is never rehydrated at all. There is no `HOT/WARM = filtered, COLD = unfiltered` inconsistency.
3. **State is the backstop.** `_merge()` re-checks each hit against tier state, so a tier whose payload disagrees with state — or a tier implementation that ignores the filter — cannot leak a non-matching hit. A tier without a `query_filter` parameter degrades to unfiltered rather than raising, and the backstop still enforces the filter.

### Refusal, not omission

```json
{
  "success": false,
  "error": {
    "code": "INVALID_PIPELINE_REQUEST",
    "message": "unsupported search filter(s): 'colour'. Supported fields are: entity_type, source_system_id, source_entity, sensitivity, document_id.",
    "detail": { "supported_filters": ["entity_type", "..."] }
  }
}
```

`422`, consistent with every other invalid-request path in this API. The response carries no hits at all — it must fail, not quietly fall back to an unfiltered search.

### Backward-compatibility note

Vectors written **before** this change carry `entity_type` and `sensitivity` in their payload (both already present) but not `canonical_record_id`, `source_system_id`, `source_entity` or `document_id`. A server-side filter on one of the three new fields will therefore not match those older points. Re-storing a vector repopulates its payload. The tier-state backstop is unaffected, since the columns are read from PostgreSQL.

---

## 5. Schema Contract Fix

### Before

```python
# api/routers_data.py — SourceField has no attribute named `data_type`
"data_type": str(getattr(field, "data_type", "")),
```

The default fired for every field of every schema, so the response was:

```json
{ "source_name": "total_amount", "data_type": "", "nullable": true }
```

A consumer generating typed ERP tooling received a syntactically valid document describing nothing — and a `200 OK`.

### After

```json
{
  "source_name": "total_amount",
  "normalized_name": "total_amount",
  "source_data_type": "NUMERIC(12,2)",
  "normalized_data_type": "decimal",
  "nullable": true,
  "required": false,
  "is_primary_key": false,
  "is_unique": false,
  "is_array": false,
  "nested_path": null,
  "semantic_type": null,
  "description": null,
  "ordinal": 3
}
```

Both type views are reported and neither is collapsed into the other:

- **`source_data_type`** is the vendor's own declaration, verbatim. `NUMERIC(12,2)`, `NVARCHAR`, `ObjectId`, `TIMESTAMP WITH TIME ZONE`. Precision and vendor spelling are unrecoverable once discarded, and a tool generator needs them.
- **`normalized_data_type`** is the coarse cross-source classification the mapping engine reasons about.

An unrecognized vendor type keeps its own spelling while normalizing to `unknown`, so nothing is lost.

The relationship graph is now exposed as well, not merely counted — a consumer previously saw that an ERP *had* relationships but could not reconstruct them:

```json
"relationships": [
  { "relationship_id": "fk_invoice_customer",
    "relationship_type": "foreign_key",
    "from_entity": "fin_invoice", "from_fields": ["customer_ref"],
    "to_entity": "fin_customer",  "to_fields": ["customer_no"],
    "confidence": 1.0 }
]
```

Field names mirror the `SourceRelationship` contract exactly (`from_*` / `to_*`) rather than being renamed in transit.

### The structural fix

Serialization moved into `api/serialization.py` and reads attributes **explicitly**. The defensive `getattr(obj, name, default)` pattern is what converted a contract mismatch into an empty string; explicit access raises instead, and a test pins that:

```python
def test_the_serializer_fails_loudly_on_a_contract_change():
    with pytest.raises(AttributeError):
        field_response(NotASourceField())
```

---

## 6. Sensitivity Propagation

### The chain

```text
CanonicalRecord.sensitivity                      (declared by transformation)
   │  ai/representation.py
   ▼
AIRepresentation.metadata["sensitivity"]
   │  ai/service.py: _carried_identity()
   ▼
EmbeddingRecord.metadata["sensitivity"]
   │  orchestration/service.py: store_vector()
   │     StorageProfile.from_metadata(record.metadata)
   ▼
StorageProfile.sensitivity
   │  storage/service.py: store()
   ▼
StorageRoutingContext.sensitivity
   │  storage/vector_router.py: prohibited_tiers()  ← BEFORE score_tiers()
   ▼
HOT / WARM / COLD
```

Before this fix, `store_vector()` called `service.store(record)` with no profile, so `DEFAULT_PROFILE` applied and every record routed as `INTERNAL`. `prohibited_tiers()` returned `{}` on every call — the constraint engine was correct, tested, and **never exercised in the runtime**.

### Architecture preserved

Orchestration supplies metadata; it does not decide. `StorageProfile.from_metadata` is owned by the **storage** layer, and a test asserts orchestration never names a tier:

```python
def test_orchestration_never_names_a_tier():
    for forbidden in ("StorageTier.HOT", "StorageTier.WARM", "StorageTier.COLD"):
        assert forbidden not in orchestration_service_source
```

An explicit `profile=` argument still wins — a caller that supplies one has made a deliberate choice. An unrecognized sensitivity value is **refused**, not downgraded: silently treating an unknown label as `INTERNAL` is precisely how restricted data ends up in the wrong tier.

### The security invariant test

`tests/erp_pipeline/storage/test_sensitivity_routing.py` builds a topology the default policy does not have:

```text
HOT  = ON_PREMISES
WARM = EXTERNAL      ← weighted so it WINS on raw score
COLD = EXTERNAL
```

and then proves, through the `RoutingDecision` **evidence** rather than the outcome alone:

| Assertion | What it rules out |
|---|---|
| an `INTERNAL` record routes to WARM | that WARM was never a candidate — without this baseline, the next assertion proves nothing |
| a `RESTRICTED` record routes to HOT | the outcome |
| `decision.prohibited_tiers == {WARM, COLD}` | that WARM merely lost on points rather than being removed |
| the prohibited tier scores `0.0` **and** is flagged | that zeroing alone could be confused with "scored badly" |
| the prohibition reason names the sensitivity and the location | that the explanation is real |
| the constraint holds when WARM's weights are made overwhelming | the arithmetic path a penalty-based design eventually loses |
| a manual override to a prohibited tier raises `PolicyViolationError` | that an override could bypass compliance |
| `PUBLIC`/`INTERNAL`/`CONFIDENTIAL` still follow ordinary scoring | that the constraint became a blanket rule |

> **An honest observation.** `DEFAULT_POLICY` places all three tiers `ON_PREMISES`, so in a default deployment the on-premises constraint still prohibits nothing — there is nothing off-premises to prohibit. The mechanism is now reachable and proven; making it *bite* in a real deployment requires setting `tier_locations`. This is documented in the README rather than left implicit.

### Not implemented, deliberately

No automatic PII or sensitivity **inference**. This task propagates a value that a `CanonicalRecord` already declares. Inference remains open — see §11.

---

## 7. Bootstrap Fix

`bootstrap_all` now creates every object the application owns:

| Schema | Objects |
|---|---|
| `erp_catalog` | `source_systems`, `schema_snapshots`, `source_entities`, `source_fields`, `source_relationships`, `mapping_profiles`, `field_mappings` |
| `erp_sync` | `sync_state` |
| `erp_vector_storage` | `vector_storage_state` (+ the four added columns, + a filter index), `vector_tier_transitions`, `vector_access_stats` |
| `erp_orchestration` | `jobs`, `job_stages`, idempotency index |
| `erp_runtime` | `canonical_records`, **`registered_sources`**, **`uploads`**, **`mapping_drafts`** |

The three bold tables were previously created only by API startup. The fix folds `bootstrap_record_schema` and `bootstrap_runtime_persistence` into one step, because they populate the same schema and are equally required — splitting them across two code paths is what produced the gap.

### Idempotency, proved rather than asserted

```python
def test_every_ddl_statement_is_conditional(recorded):
    assert "if not exists" in statement.lower() or statement.startswith("select")

def test_bootstrap_never_drops_or_truncates(recorded):
    for forbidden in ("drop table", "drop schema", "truncate", "delete from"):
        assert forbidden not in recorded.sql
```

These run everywhere, with no database, by recording the DDL against a fake engine. The live tests then run `bootstrap_all` three times against a real PostgreSQL and confirm every table exists and every runtime store is usable — including the scenario the defect broke:

```python
def test_the_api_can_start_against_a_bootstrapped_database(bootstrapped, engine):
    # bootstrap first, then start with ERP_BOOTSTRAP_ON_STARTUP disabled
    assert client.get("/v1/sources").status_code == 200
```

Nothing is torn down afterwards — dropping these schemas would destroy research data.

---

## 8. API Changes

**Purely additive.** No operation added or removed, no field removed. Verified by diffing `artifacts/openapi_contract_snapshot.json` against its committed version:

```text
operations before/after : 22 / 22
operations added        : none
operations removed      : none
models added            : SchemaEntityResponse, SchemaFieldResponse,
                          SchemaRelationshipResponse
models removed          : none

SchemaResponse      : + relationships
SearchHitResponse   : + canonical_record_id
SearchResponse      : + filters_applied
```

Two changes need naming precisely, because "additive" is not the whole story:

1. **`SchemaResponse.entities` changed shape.** It was `array of free-form object`; it is now `array of SchemaEntityResponse`. Within each field object, the key `data_type` is **gone** and `source_data_type` / `normalized_data_type` have appeared. This is a **breaking change to a response field** in the strict sense. It is made deliberately, because the removed key was *always the empty string* — no consumer could have depended on its value, only on its presence, and a key that always says nothing is not a contract worth preserving. Documented here and covered by tests.

2. **`SearchHitResponse.record_id` is retained and now mirrors `canonical_record_id`.** It previously duplicated `representation_id` and was therefore unusable. Keeping the field means an existing consumer is not broken; making it mirror the canonical id means it now actually resolves.

**Artifact regeneration:** `artifacts/openapi_contract_snapshot.json` is regenerated by an existing test (`tests/erp_pipeline/api/test_document_and_live_http.py:358`) as part of the normal suite — that is this project's convention, so no manual step was taken. The previous version is preserved in Git history at the consolidation commit.

---

## 9. Database Changes

**Additive only.** One table, four nullable columns, one index.

```sql
-- erp_vector_storage.vector_storage_state
ALTER TABLE ... ADD COLUMN IF NOT EXISTS canonical_record_id TEXT;
ALTER TABLE ... ADD COLUMN IF NOT EXISTS source_system_id    TEXT;
ALTER TABLE ... ADD COLUMN IF NOT EXISTS source_entity       TEXT;
ALTER TABLE ... ADD COLUMN IF NOT EXISTS document_id         TEXT;

CREATE INDEX IF NOT EXISTS vector_storage_state_filter_idx
    ON ... (entity_type, source_system_id, sensitivity);
```

Safety properties:

- **Every column is NULLABLE.** A row written before the column existed genuinely has no value; inventing one would be worse than reporting the absence.
- **`ADD COLUMN IF NOT EXISTS`**, run from `bootstrap_storage_schema`, so the migration is idempotent and an existing research database gains the columns without being dropped or rebuilt.
- **`COALESCE` on upsert**, so a re-store that carries no reference cannot erase one already recorded.
- **`_optional_column()` on read**, so a database that has not been re-bootstrapped since the change returns `None` rather than raising `KeyError` on every read.
- **No existing column altered, no data migrated, no table rebuilt.**

Verified against the live research database: all four columns present, three consecutive bootstraps clean, tier-state round-trip carrying a canonical id succeeds.

---

## 10. Tests

```text
Before:   2703 passed, 26 skipped, 0 failed, 0 errors   (2729 collected, 30:03)
After:    2874 passed, 26 skipped, 0 failed, 0 errors   (2900 collected, 26:48)

Collected:      +171
New tests:      +171 across 6 new files
Changed tests:  0    - no existing test was modified or deleted
Skipped:        26   - unchanged; the same environment-gated live tests
Failures:       0
Errors:         0
```

The delta reconciles exactly: 2900 - 2729 = 171, which is the sum of the six
new files below. Not one existing test was edited, renamed or removed.

### New tests by fix

| Fix | File | Tests |
|---|---|---|
| 1 — search resolution | `storage/test_canonical_resolution.py` | 19 |
| 2 — filters | `storage/test_search_filters.py` | 38 |
| 4 — sensitivity | `storage/test_sensitivity_routing.py` | 29 |
| 1 + 2 acceptance | `api/test_search_resolution_and_filters.py` | 25 |
| 3 acceptance | `api/test_schema_contract_fields.py` | 26 |
| 5 acceptance | `runtime/test_bootstrap_completeness.py` | 34 |

### Acceptance tests required by the brief

| Required | Test | Result |
|---|---|---|
| `CanonicalRecord → … → search → GET record` | `test_the_canonical_id_resolves_through_the_record_endpoint` and `test_the_resolved_record_is_the_one_the_vector_came_from` | ✅ |
| invoice + customer vectors, filter returns invoices only | `test_filtering_by_entity_type_returns_only_that_type`, `test_filtering_actually_reduces_the_result_set` | ✅ |
| restricted record, external tier wins on score, constraint applied first, examined via `RoutingDecision` | `test_the_external_tiers_are_prohibited_not_merely_outscored` and six neighbours | ✅ |
| bootstrap → all objects → bootstrap again → stores initialize | `test_live_bootstrap_creates_every_required_table`, `test_live_bootstrap_is_idempotent`, five store-usability tests | ✅ (live PostgreSQL) |

### Regression check

An intermediate run of `storage` + `ai` + `api` after the production changes but before the new tests: **310 passed, 0 failed** (26:42). No existing behaviour changed.

### Environmental note

The live-integration tests in this suite depend on a reachable PostgreSQL, MySQL, MongoDB and Qdrant. The consolidation baseline itself recorded one live-Qdrant timeout as a *failure* in one run and a pass in the next, on identical code. Any single non-pass in a live test should be checked against a re-run before being treated as a code defect.

---

## 11. Remaining Known Issues

Unchanged by this task, and deliberately out of its scope.

| Issue | Source | Note |
|---|---|---|
| **Sensitivity is never inferred** | Audit Issue 4 | The value now propagates correctly, but nothing *derives* it. `SourceField.semantic_type` is still `None` from every producer, so every record is `INTERNAL` unless a caller sets it. This is the highest-leverage remaining item |
| **The default policy has no off-premises tier** | §6 above | `tier_locations` places all three tiers `ON_PREMISES`, so the on-premises constraint prohibits nothing by default |
| **Generated mapping profiles are not durable** | Audit Issue 3 / README #3 | Still held in `PipelineServices.mapping_cache`; lost on restart |
| **Extraction is PostgreSQL-only** | Audit Issue 5 / README #1 | `drivername="postgresql+psycopg2"` hard-coded in two places |
| **Extraction is capped at 5,000 rows** | Audit Issue 7 | No paginated full-table snapshot |
| **`GET /v1/schemas/{id}?version=` is ignored** | README #13 | Accepted and unused. Not fixed here: it is a retrieval-semantics feature, not a contract defect, and would need catalog version lookup |
| **The process cascade has no runtime entry point** | Consolidation R2 | Library only; no `JobType` composes it |
| **`StorageRecordMetadata` orphan detection** | Consolidation R4 | Now partially addressed — `canonical_record_id` exists, so `verify_tier_state` can detect an orphaned tier-state row. The verification service does not yet read it by default |
| **No retries anywhere** | Audit §32 | A transient Qdrant failure still fails a record permanently |
| **Old vectors lack the new payload keys** | §4 above | Filters on the three new payload fields will not match vectors stored before this change until they are re-stored |
| **Stale `egg-info`** | Consolidation R7 | Refresh with `pip install -e .` at your convenience |

---

## 12. Phase 14 Readiness

```text
Phase 14 response adaptation:
READY
```

Phase 14 — ERP-Aware Adaptive Multimodal Response Transformation — needs to take a retrieval result and turn it into an adapted response. That requires four things from this layer, and all four now hold:

1. **A hit must name its record.** Response adaptation has to fetch the underlying business content to transform it. Before this task a hit carried an id that resolved to nothing; now `canonical_record_id` round-trips through `GET /v1/records/{id}`. Without this, Phase 14 would have had to reconstruct ids by parsing — the exact defect this task removed.
2. **Retrieval must be scopeable.** Query-aware context selection needs to constrain candidates by entity type, source system or document before adapting them. `filters` now works and is refused rather than ignored when wrong.
3. **Field types must be real.** Adapting a response for a consumer means knowing whether a field is a `NUMERIC(12,2)` or a string. The schema endpoint now reports both the vendor type and the normalized type instead of an empty string.
4. **Sensitivity must be visible at retrieval time.** An adaptive response layer must be able to see that a hit is `restricted` before deciding what to emit. Sensitivity now propagates into storage state, is returned in hit metadata, and is filterable.

Two caveats a Phase 14 design should account for rather than be surprised by:

- **Sensitivity is declared, not inferred.** Every record is `INTERNAL` unless something sets it. Phase 14 must not assume the classification is meaningful yet.
- **Search returns no text.** A hit carries identity, score, tier and provenance — never content. Phase 14 fetches content through `GET /v1/records/{id}`, which is the correct boundary (the vector index must not become a second copy of the corpus), but it is a second round trip and a bulk-resolution endpoint may be worth adding.

Nothing in Phase 14 is started. This task ends here.
