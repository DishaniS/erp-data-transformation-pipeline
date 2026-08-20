# Generic Transformation and Validation Engine

Project `R26-SE-034` — ERP-Aware Data Transformation Pipeline
Component owner: IT22267290

**Status: Phase 9 — implemented.** `src/erp_pipeline/transformation/`, 299 tests.
Every value shown below was measured from the implementation.

---

## 1. Purpose

Phase 8 decided **what maps to what**. Phase 9 executes **how a source record
becomes a valid canonical record**.

The governing principle, and the reason most of this engine is about refusal:

> The pipeline never optimizes for "make every record succeed". A failed
> transformation is an acceptable, well-reported outcome. A silently corrupted
> ERP record is not.

So `"hello"` never becomes `0`. `"25.9"` never becomes `25`. `"approved"` never
becomes `True`. `"007"` never becomes `7`.

## 2. Architecture

```
Source Record  +  MappingProfile
          |
          v
   field extraction          missing != null
          v
  null / default handling    defaults substitute absence, never failure
          v
 TransformationRule execution   declared, data-only, no eval
          v
    type conversion          Decimal for money, never float
          v
     normalization           opt-in only; identifiers are not mutated
          v
  canonical assignment       nested targets, conflicts reported
          v
      validation             required / type / constraints / references
          |
  +-------+-------+
  |               |
VALID          INVALID
  |               |
  v               v
CanonicalRecord   DataQualityIssue
                      |
                      v
             reject / skip / threshold action
```

## 3. Phase 8 / Phase 9 boundary

| Phase 8 | Phase 9 |
|---|---|
| decides which source field becomes which canonical field | executes that decision against real values |
| reads schemas only, never data | reads data, never schemas |
| produces `MappingProfile` | consumes `MappingProfile`, produces `CanonicalRecord` |
| declares `TransformationRule`s structurally | executes them |

Phase 9 never re-derives a mapping. A source field the profile does not mention
is simply not mapped — asserted by `test_phase_9_never_reinvents_a_mapping`.

## 4. Public API

```python
transform_record(source_record, mapping_profile, options=None, context=None,
                 canonical_model=None, resolver=None, run_id=None)
    -> RecordTransformationResult

transform_records(source_records, mapping_profile, context, options=None, ...)
    -> TransformationRunSummary

TransformationService(canonical_model=None, options=None, resolver=None)
    .transform_record(record, profile, context, run_id=None)
    .transform_records(records, profile, context, run_id=None)
```

`context` is **required in practice**. `SourceReference` demands the source
technology, and the engine will not invent one: writing a false technology into
a record's permanent provenance is worse than making the caller state it.
Omitting it raises `TransformationConfigurationError`.

`TransformationRunSummary` exposes `run` (the frozen `TransformationRun`),
`successful_records`, `rejected_records`, `skipped_records`, `issues`, ratios,
and the threshold state.

## 5. Source-record abstraction

One shape only — `SourceRecord(values, record_key, ordinal, source_entity)`.
A PostgreSQL row, a MongoDB document, a Phase 6 `SourceRow` and an API payload
all become this **at the boundary**, before any transformation logic runs.

`SourceRecord.from_source_row(row)` adapts Phase 6's CSV rows by *structural
typing* — the transformation package imports no ingestion, discovery, api_specs
or connectors module, asserted by
`test_there_is_no_separate_csv_transformation_path`. There is no
`if source_type is ...` branch anywhere in the engine.

## 6. MappingProfile execution

Only **decided** mappings execute:

| Status | Executes? | Why |
|---|---|---|
| `AUTO_ACCEPTED` | ✅ | the engine chose it on strong evidence |
| `APPROVED` | ✅ | a human chose it |
| `SUGGESTED` | ❌ | nobody has decided it |
| `REVIEW_REQUIRED` | ❌ | explicitly awaiting a human |
| `REJECTED` | ❌ | explicitly declined |

Executing an undecided proposal would quietly promote a guess into production
data — exactly what Phase 8's conservatism was protecting against. A profile
with nothing executable raises `NO_FIELDS_MAPPED` rather than emitting an empty
record.

## 7. Nested extraction

Dotted paths, on both sides:

```
source "customer.contact.email"  ->  values["customer"]["contact"]["email"]
target "contact.email"           ->  normalized_data["contact"]["email"]
```

A path running into a non-mapping reports `MISSING` rather than raising — a
`KeyError` escaping would take down records that are perfectly fine.

**`MISSING` and `NULL` are different facts** and produce different codes:

| Situation | Outcome | Code | Target |
|---|---|---|---|
| source never sent the field | `MISSING` | `SOURCE_FIELD_MISSING` | left **absent** |
| source sent the field as null | `NULL` | `SOURCE_VALUE_NULL` | written as `None` |

That difference is what lets validation report `REQUIRED_FIELD_MISSING`
(nothing produced a value) rather than `NULL_NOT_ALLOWED` (the source said
null) — different problems with different fixes.

Array element access (`lines[].sku`) is **not** supported: nothing in the
mapping contract expresses which element is meant, and picking one would be a
guess.

### Target path conflicts

A scalar is never silently replaced by an object. Step 7's example, measured:

```
normalized_data already holds  customer = "ABC"
next mapping targets           customer.email
-> TARGET_PATH_CONFLICT, and "ABC" is untouched
```

Assigning the same target twice is also a conflict.

## 8. Type conversion

One central converter; the validator confirms rather than duplicates it.

| Target | Accepts | Refuses |
|---|---|---|
| STRING | str (identity), int/Decimal/float, date/datetime | bool, object/array (unless declared), bytes |
| INTEGER | int, exactly-integral float/Decimal, integral text | `"25.9"`, `"hello"`, NaN/Inf, **bool** |
| DECIMAL | Decimal, int, float (via `str`), numeric text | non-numeric text, NaN/Inf, bool |
| BOOLEAN | declared literals only | `"approved"`, `1`/`0` unless configured |
| DATE | `date`, ISO date text | `datetime`, timestamp text, ambiguous forms |
| DATETIME | aware/naive `datetime`, ISO text, `date` | ambiguous forms |
| BINARY | `bytes` → base64, valid base64 text | arbitrary text |
| OBJECT / ARRAY | mapping / list | scalars, and **strings for ARRAY** |

`bool` is refused for INTEGER because `bool` is an `int` subclass in Python —
accepting it silently would turn a flag into 0/1 with nobody declaring it.
A string is refused for ARRAY because strings are iterable, and an accidental
character-by-character array is easy to produce and hard to notice.

## 9. Decimal safety

Money is `Decimal`, never `float`.

```python
convert("2500.50", DECIMAL) -> Decimal("2500.50")
convert(2500.50,   DECIMAL) -> Decimal("2500.50")   # via str(), not binary
```

`Decimal(2500.50)` would capture the binary approximation. Serialization keeps
it exact — the frozen Phase 1 serializer renders `Decimal` as a **string**:

```json
{"normalized_data": {"amount": "2500.50"}}
```

`NaN`/`Infinity` are refused even when policy permits them, because the
canonical model rejects them at serialization — allowing them here would only
move the failure somewhere less informative.

A thousands separator is ambiguous (`"1,234"` is 1234 in one locale and 1.234 in
another) and is refused unless `allow_thousands_separator` is declared.

## 10. Boolean policy

Defaults are deliberately tiny: `"true"` / `"false"` and real booleans.
`1`/`0` require `allow_integer_forms`; `"yes"`/`"no"` require declaring them.
A literal declared as both true and false is refused at configuration time.

## 11. Date and time policy

**ISO-8601 only by default.** `03/04/2026` is refused — it is 3 April in one
country and 4 March in another, and no schema says which. Declaring
`date_formats=("%d/%m/%Y",)` or a `date_parse` rule supplies the missing
evidence.

**Canonical datetime is UTC-aware.** This follows the frozen contract rather
than inventing a convention: `to_rfc3339` converts every aware datetime to UTC
with a `Z` suffix and *rejects naive datetimes outright*, so a canonical
datetime that was not aware could not be serialized at all. An offset-bearing
input keeps its instant and normalizes to UTC. A naive input is treated as UTC
by default — recorded as an explicit, inspectable decision
(`assume_utc_when_naive`), not a hidden assumption.

`DATETIME → DATE` is refused: discarding a time component is lossy and must be
declared.

## 12. Null handling

Four states are kept distinct: missing, `None`, empty string, configured
marker.

`null_markers` is **empty by default**. Treating `"N/A"` or `"NULL"` as null is
a real need but also a destructive guess — a status column may legitimately
contain the string `"NONE"`. Same for `empty_string_is_null`, off by default.

## 13. Defaults

Declared per canonical target, applied **only when the source field is missing
or null**, and applied **before** conversion.

That ordering is the whole design. Step 15's example, measured:

```
defaults = {"amount": Decimal("0")},  source total_amt = "hello"
-> TYPE_CONVERSION_FAILED, record rejected, amount is NOT 0
```

A default substitutes an absence. It never rescues a failure.

## 14. Enum mapping

Exact lookup only — no case-insensitive fallback, no trimming, no fuzzy match.
An enum table declares that these exact codes mean these exact things.

```
{"P": "PENDING", "C": "COMPLETED"}
"P"       -> "PENDING"
"p"       -> UNKNOWN_ENUM_VALUE       (exact means exact)
"UNKNOWN" -> UNKNOWN_ENUM_VALUE
```

`on_unknown` may be `issue` (default), `fallback` (requires a declared
fallback) or `passthrough`.

## 15. Normalization

**Every operation is off by default.** Lower-casing `AB-001` changes a primary
key; stripping a space inside a name changes a person's name. Both surface
months later as a broken join, and neither is recoverable from the canonical
record.

Available when declared: trim, case (lower/upper), Unicode form
(NFC/NFD/NFKC/NFKD), internal-whitespace collapse — optionally scoped to named
target fields. Applied in a fixed, documented order: Unicode → whitespace →
trim → case. Strings only.

## 16. TransformationRule execution

Exactly the **twelve frozen `TransformationOperation` members**, dispatched
through a closed registry:

```
cast  concat  constant  copy  date_parse  default
enum_map  nested_path  redact  rename  split  trim
```

`test_every_frozen_operation_is_implemented` asserts the registry equals the
enum — no more, no less.

Operations the brief mentions conceptually but the frozen enum does not
declare — `lowercase`, `uppercase`, `coalesce` — were **not** added to it.
Amending a frozen Phase 1 contract to suit this phase is what the brief
forbids, so those capabilities arrive as configuration instead: case folding
through `NormalizationPolicy`, coalesce through `ComputedField`.

**No `eval`, `exec`, `compile` or `__import__` anywhere in the package** —
proven by an AST walk over every module. `TransformationRule.config` is
validated as a JSON object by Phase 1, which structurally excludes callables
before this engine ever sees one.

An operation the engine cannot execute raises `UnsupportedOperationError`
rather than being skipped. Silently ignoring a declared step would produce
records that look successful and are wrong.

## 17. Computed fields

Allow-listed operations only: `concat`, `coalesce`, `constant`. No expression
language.

```python
ComputedField(target_field="name", operation=CONCAT,
              sources=("first_name", "last_name"), separator=" ")
```

Inputs resolve from the source record first, then from the candidate canonical
record — so a computed field may build on a mapped value or on another computed
field.

**Dependency cycles are refused at construction time**, before record 1:

```
Computed fields form a dependency cycle: name -> phone -> name.
There is no evaluation order that satisfies them, so the configuration is
refused rather than evaluated in an arbitrary one.
```

Evaluation order is a topological sort with declaration order breaking ties, so
it is stable across runs. A computed field reading a *source* field of the same
name is not a cycle.

## 18. CanonicalRecord creation

The frozen Phase 1 contract, built through `CanonicalRecord.from_source`.
Measured:

```
record_id       : erp:erp_a:invoice:inv-001
entity_type     : invoice
normalized_data : {'invoice_id': 'INV-001', 'customer_id': 'C001',
                   'amount': Decimal('2500.50')}
content_hash    : e8cb48985844bef7cc98bcc3...
source          : erp_a / postgresql / fin_invoice / INV-001
provenance      : schema_id=erp_a.public.v1, ingestion_method=batch_extract,
                  original_record_id=1
metadata        : mapping_id, transformation_engine_version,
                  transformation_config, canonical_model_identity,
                  validation_profile_version, rules_applied
```

### normalized_data shape

Keys are **bare field names scoped by `entity_type`**, following the
repository's own convention ([canonical_erp_model.md](canonical_erp_model.md)
§4) and the fact that `MappingProfile` is scoped to one `target_entity_type`.

So one source record feeding two canonical entities is **two profiles producing
two records**, not one record with entity-keyed nesting:

```
{"cust_no": "C001", "total_amt": "2500.50"}
  + customer profile -> CanonicalRecord(entity_type="customer",
                                        {"customer_id": "C001", ...})
  + invoice  profile -> CanonicalRecord(entity_type="invoice",
                                        {"amount": Decimal("2500.50"), ...})
```

Nested targets *within* an entity are fully supported (`contact.email` →
`{"contact": {"email": ...}}`).

### Identity

Deterministic, derived from the record's own business key — the canonical
identifier field where the entity declares one, otherwise the source record's
key. Never a UUID4, never a timestamp. A record with no business key is
**reported** (`RECORD_IDENTITY_MISSING`), because no identity is better than an
invented one.

## 19-22. Validation

Two authorities, deliberately separate:

- **`CanonicalEntity`** (Phase 8) — declares `required` and `data_type`. Never
  overridden.
- **`ValidationProfile`** (Phase 9 configuration) — allowed values, ranges,
  lengths, patterns, reference sets. Phase 1 has no vocabulary for any of
  these, which is why supplemental configuration was necessary.

| Check | Code | Notes |
|---|---|---|
| required field present | `REQUIRED_FIELD_MISSING` | from the canonical model |
| non-null | `NULL_NOT_ALLOWED` | model `required`, or declared `nullable=False` |
| type after conversion | `DATATYPE_MISMATCH` | confirms the transformer's work |
| allowed values | `INVALID_ALLOWED_VALUE` | reported, never replaced |
| numeric/temporal range | `OUT_OF_RANGE` | only where declared |
| length / pattern | `INVALID_IDENTIFIER` | no country or tax format assumed |
| duplicates | `DUPLICATE_RECORD` | only where keys declared |
| references | `REFERENCE_NOT_FOUND` / `REFERENCE_NOT_CHECKED` | see §23 |

**No invented business rules.** An absent constraint is not checked. The engine
never decides on its own that an amount should be non-negative or that a
customer id looks like `C\d+`. A mapping's own `target_type` never overrides
the canonical model — the model is what the target *is*, the profile is what
Phase 8 *believed*.

## 23. Duplicate and reference validation

Duplicate detection is **off** unless `duplicate_key_fields` is declared — the
engine never infers which fields identify a record. Policies: `REJECT`
(default), `SKIP`, `WARN`, `ALLOW`. **Nothing is deduplicated silently** — even
`ALLOW` leaves a `DUPLICATE_RECORD` issue.

References go through a resolver interface, so `validator.py` carries no
database coupling (asserted by test — no `sqlalchemy`, `psycopg`, `pymongo`,
`connect(` or `cursor`). Three outcomes, not two:

| Resolver says | Result |
|---|---|
| value present | valid |
| value absent | `REFERENCE_NOT_FOUND` (ERROR — rejects) |
| no resolver, or unknown set | `REFERENCE_NOT_CHECKED` (WARNING) |

An unverified reference is **never** reported as valid. It is also not an error:
it says nothing about the data, only about the checking.

## 24. DataQualityIssue

The frozen Phase 1 contract, built in exactly one place (`quality.py`) so the
privacy guarantee is auditable rather than scattered.

Measured, for `amount = "hello"`:

```
[error   ] TYPE_CONVERSION_FAILED   field=total_amt
  source field 'total_amt' could not be converted to canonical target
  'amount' (decimal): the source text is not a number and cannot be read
  as a decimal
[error   ] REQUIRED_FIELD_MISSING   field=amount
  canonical field 'invoice.amount' is required but no mapping produced a
  value for it
```

No value appears in either message. Codes come from the `IssueCode` enum, never
from an exception class name, so downstream grouping is stable. Every code has
a declared default severity, and `issue_id` is content-derived and therefore
deterministic.

## 25. Rejected and skipped records

`RejectedRecord.reasons` is **structurally guaranteed non-empty** —
`__post_init__` raises otherwise, so "failed for unknown reason" is impossible
rather than merely tested for.

```json
{"record_reference": "ordinal=9",
 "reasons": ["TYPE_CONVERSION_FAILED", "REQUIRED_FIELD_MISSING"],
 "source_entity": "fin_invoice", "ordinal": 9, "mapping_id": "inv.profile",
 "issue_codes": ["TYPE_CONVERSION_FAILED", "REQUIRED_FIELD_MISSING"],
 "issue_count": 2}
```

The original record may be **retained in memory** for remediation, but
`to_dict()` never serializes it unless a caller explicitly passes
`include_source_values=True`.

`SKIPPED` is distinct from `REJECTED`: a skipped record is a deliberate
non-transformation (duplicate under SKIP policy, filtered), never counted as
successfully transformed.

## 26. Quality thresholds

**Every numeric limit defaults to `None` — not enforced.** This is a deliberate
refusal to invent a number: there is no defensible universal "5% failures is
acceptable" for ERP migration, and shipping one as a default would dress an
arbitrary choice up as a standard.

The single enabled default is `stop_on_critical_issue=True`, because CRITICAL
means the *engine* is in trouble, not the data.

Available: `max_failed_records`, `max_failure_ratio`, `max_error_issues`,
`max_warning_issues`, `max_duplicate_ratio`, `minimum_success_ratio`. These are
**operational defaults**; a research run should set them explicitly.

Measured breach — Step 37's worked example:

```
100 read, 90 transformed, 10 failed, max_failure_ratio = 0.05
failure_ratio      : 0.1
threshold_exceeded : True
run status         : failed
reason             : "failure ratio 0.1 exceeds the configured maximum of 0.05"
```

A run that breached a threshold is **never** reported as succeeded. An empty
batch breaches nothing and succeeds.

## 27. Fail-fast vs continue

`CONTINUE` is the default: keep going, reject bad records, collect every issue.
Stopping a whole load because record 12 has a bad date hides the other 40
problems a reviewer needs to see at once.

`FAIL_FAST` stops at the first breach and abandons the iterator. Counters
reflect what was actually read. A CRITICAL issue stops the run under either
policy when `stop_on_critical_issue` is set.

## 28. TransformationRun metrics

The frozen contract, populated unmodified:

```
records_read / records_transformed / records_failed / records_skipped
warning_count / error_count / started_at / completed_at / duration_seconds
status / mapping_id / message / metadata
```

`error_count` folds CRITICAL into ERROR — the contract has two counters and a
critical finding is certainly not a warning; the exact critical count is on the
summary. Duration is measured with a **monotonic** timer; timestamps are
operational metadata and never feed identity.

The summary adds ratios (`success_ratio`, `failure_ratio`, `skip_ratio` — all
dividing by `records_read`), `critical_count`, `quality_issue_count`,
`threshold_exceeded`, `stopped_early` and `counters_balance`.

**The invariant** (Step 41), asserted across ordinary batches, duplicate
batches and fail-fast runs:

```
records_read == records_transformed + records_failed + records_skipped
```

## 29. Transactional record behaviour

The candidate record is assembled in full, validated as a whole, and only then
emitted. Any blocking issue rejects the record and its partial data is
discarded. There is no half-transformed record in `successful_records`.

## 30. Privacy

Sentinels (`SECRET_CUSTOMER_93821`, `SECRET_ACCOUNT_22118`,
`SECRET_EMAIL_44519`) are planted in source records and asserted absent from
every diagnostic surface:

| Surface | Guarantee |
|---|---|
| `DataQualityIssue` (message, JSON, `repr`) | no value |
| rejection report `to_dict()` | no value |
| `TransformationRunSummary.to_dict()` | no value |
| `TransformationRun` JSON | no value |
| logs | nothing logged at all |
| exception messages | fields, types and rules only |
| record audit metadata | rule **names** only, no before/after values |

They *do* appear in the `CanonicalRecord` — that is the engine's job.

`include_value_diagnostics` is off by default, and even when on it emits
`summarize_value(..., redact=True)` — `<redacted str length=5>`, type and length
only. **There is deliberately no option that puts a raw value into an issue.**

## 31. Determinism

Same record + same profile + same options ⇒ same `normalized_data`, same
`record_id`, same `content_hash`, same issue codes, same field ordering, same
outcome. The package imports no `random`, `secrets` or `uuid`.

Operational timings differ between runs and affect nothing: identity is derived
from the business key, not the clock. The default `run_id` is derived from the
mapping and configuration rather than a timestamp, so two runs over identical
inputs are field-by-field comparable; callers needing per-execution uniqueness
pass their own.

## 32. Streaming

`transform_records` consumes an **iterable** and never calls `list()` on it.
Proven: a 1000-record generator under FAIL_FAST with `max_failed_records=0`
pulls exactly **1** record before stopping. A generator that raises if advanced
past record 3 completes cleanly with `records_read == 2`.

## 33. Cross-source proof

Five technologies, five different field names and value types, one service, one
canonical result — measured:

| Source | Field names | Value type |
|---|---|---|
| PostgreSQL | `invoice_no`, `customer_ref`, `total_amount` | `Decimal` |
| MySQL | `invoiceId`, `customerId`, `total` | `float` |
| MongoDB | `invoice.id`, `customer.id`, `invoice.total` | nested `str` |
| CSV | `inv_no`, `cust_no`, `total_amt` | `str` |
| OpenAPI | `invoiceId`, `customerId`, `totalAmount` | `str` |

All five produce identical `normalized_data`:

```python
{"invoice_id": "INV-001", "customer_id": "C001", "amount": Decimal("2500.50")}
```

…with five **non-colliding** `record_id`s and five honest, distinct
`source_type` provenances. The CSV case streams through Phase 6's real
`iter_records()`, not a hand-built dictionary.

## 34. Limitations

1. **No array element mapping.** `lines[].sku` is unsupported; the mapping
   contract cannot express which element is meant.
2. **One profile, one entity.** A source record feeding several canonical
   entities requires one profile per entity. This follows the frozen
   `MappingProfile` scope rather than working around it.
3. **BINARY targets hold base64 text**, forced by the frozen serializer's
   refusal of `bytes`. Lossless, but the representation is pinned.
4. **Duplicate detection is per-run and in-memory** — it holds one key string
   per transformed record. Cross-run deduplication is not Phase 9's job.
5. **No cross-record or aggregate validation.** Every rule is per record.
6. **Reference resolution ships only an in-memory implementation.** A
   database-backed resolver satisfies the same protocol but belongs to the
   orchestration path.
7. **`ValidationProfile` is Phase 9 configuration, not a persisted contract.**
   It is versioned and fingerprinted into run metadata but is not stored in the
   Phase 2 catalog.
8. **Accumulation is unbounded**: successful records, rejections and issues are
   all held in memory for the summary. Streaming input is incremental; output
   is not.

## 35. Phase 10 boundary

Phase 9 does **not** implement: schema discovery, mapping candidate generation,
incremental synchronization, change data capture, polling, schema-drift
monitoring, embeddings, Qdrant, hybrid vector storage, semantic search, RAG,
REST API or UI, ERP endpoint execution, MCP runtime, or orchestration workers.

It also writes nothing anywhere — no database, no file, no vector store, no
network — asserted statically. Canonical records are returned to the caller;
where they are stored is a later phase's decision.

Phase 10 adds incremental sync and schema-drift detection on top of what
Phase 9 transforms.
