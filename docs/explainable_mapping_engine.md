# Explainable Source-to-Canonical Mapping Engine

Project `R26-SE-034` — ERP-Aware Data Transformation Pipeline
Component owner: IT22267290

**Status: Phase 8 — implemented.** `src/erp_pipeline/mapping/`, 226 tests.
Every number in this document was measured from the implementation, not
estimated.

---

## 1. Purpose

Phase 8 answers exactly one question, for every field of every source:

> Which canonical ERP field is the best compatible target, how confident are
> we, and **why**?

It does not answer *"transform this record"*. That is Phase 9. Nothing in this
package can produce a `CanonicalRecord`, apply a value conversion or execute a
`TransformationRule` — a static test
(`test_no_transformation_is_ever_executed`) asserts it.

The engine's output is a set of **instructions**. Phase 9 is what follows them.

## 2. Architecture

```
PostgreSQL  MySQL  SQL Server  MongoDB  CSV  OpenAPI  Postman
                          |
                          v
                    SourceSchema                    (Phases 4-7)
                    SourceEntity
                    SourceField
                          |
                          v
                  Phase 8 Mapping Engine
                          |
          +---------------+---------------+
          |               |               |
       names           types           context
      aliases      compatibility    entity + path
          |               |               |
          +---------------+---------------+
                          v
             MappingCandidate + MappingEvidence
                          v
                  score / confidence / margin
                          v
         AUTO_SELECTED | AMBIGUOUS | REVIEW | UNMAPPED
                          v
              MappingProfile + FieldMapping        (Phase 1 contracts)
                          v
                 Phase 2 Schema Catalog
```

## 3. Source-independent design

There is **no branch on source technology anywhere in the package**. No
`if source_type is MYSQL`, no MongoDB special case, no OpenAPI carve-out. The
engine consumes `SourceSchema` / `SourceEntity` / `SourceField` and nothing
else.

This is only possible because Phases 4–7 did the work of making seven
technologies converge on one contract, and it is the central research claim of
this phase — so it is asserted statically, not merely stated:

| Test | Asserts |
|---|---|
| `test_the_engine_contains_no_source_specific_branch` | no source-technology identifier appears in a conditional |
| `test_the_mapping_package_never_imports_a_source_technology_module` | no import of `discovery`, `ingestion`, `api_specs`, `connectors` |
| `test_the_engine_only_reads_the_common_contract` | only the common contract types are consumed |
| `test_one_consumer_reads_every_source_without_knowing_its_technology` | one API call handles all six |

The provenance difference between sources stays honest — `DISCOVERED`,
`INFERRED`, `API_SPEC` — but it changes no mapping logic.

## 4. Canonical target model

### What actually existed

The repository deliberately had **no** canonical field vocabulary.
[`docs/canonical_erp_model.md`](canonical_erp_model.md) §4 states it outright:

> "Which keys belong in `normalized_data` for a given entity type is decided by
> a mapping profile, not by this contract."

`CanonicalRecord.entity_type` is an open normalized string and
`normalized_data` is an open JSON object, precisely so a new ERP domain object
needs no code change. That openness is correct for the *contract* layer — and
it leaves the mapping engine with nothing to aim at.

### What Phase 8 added, and why

`mapping/canonical_model.py` declares the **smallest** target vocabulary the
engine needs: 3 entities, 14 fields. It refuses to invent a large speculative
ERP ontology.

Every field records where its name came from, and this is **machine-checked**:

| Provenance | Meaning | Count |
|---|---|---|
| `REPOSITORY` | already used as a canonical field name in this repository | 6 |
| `PHASE_8_EXTENSION` | added by Phase 8, **with a stated reason** | 8 |

```
invoice        (repository) : invoice_id, customer_id, amount, currency,
                              status, issued_on
customer       (extension)  : customer_id, name, email, phone
purchase_order (repository) : purchase_order_id, supplier_id, amount, status
```

The `REPOSITORY` names are reused **verbatim** from
`docs/canonical_erp_model.md` §4 and from the Phase 1 cross-source
demonstration (`tests/erp_pipeline/test_cross_source_canonicalization.py`,
whose `EXPECTED_CANONICAL_DATA` is the authoritative canonical invoice). Not
one was renamed.

A `PHASE_8_EXTENSION` field **cannot be constructed without a reason** —
`CanonicalField.__post_init__` raises otherwise, and
`test_an_extension_without_a_reason_is_refused` proves it. This is what stops
Phase 8 quietly passing invented vocabulary off as established.

### It is configurable, not hard-coded

`DEFAULT_CANONICAL_MODEL` is a default, not a law.
`CanonicalTargetModel.from_dict` builds a model from a plain dictionary, so a
research run can supply its own vocabulary without editing any module. The
model's `model_id` and `version` travel into every mapping profile
(`erp_core@1.0`), so a stored mapping always states which target model it was
generated against.

## 5. Field normalization

Deterministic, and used **for scoring only** — `SourceField.source_name` and
`normalized_name` are never modified, because Phase 1 identity is frozen.

Measured output:

| Source spelling | Tokens | Normalized key |
|---|---|---|
| `customer_id` | `('customer', 'id')` | `customer_id` |
| `customerId` | `('customer', 'id')` | `customer_id` |
| `CustomerID` | `('customer', 'id')` | `customer_id` |
| `customer-id` | `('customer', 'id')` | `customer_id` |
| `CUSTOMER ID` | `('customer', 'id')` | `customer_id` |
| `cust_no` | `('customer', 'number')` | `customer_number` |
| `totalAmount` | `('total', 'amount')` | `total_amount` |
| `email_addr` | `('email', 'address')` | `email_address` |
| `tbl_customer` | `('customer',)` | `customer` |

Handled: case folding, camelCase/PascalCase splitting (acronym runs kept
intact), snake/kebab/space/dot separators, digit boundaries (`line1` →
`line`+`1`), noise tokens (`tbl`, `dim`, `staging`), and abbreviation expansion
from an **explicit, configurable table** (`cust`→`customer`, `amt`→`amount`).

No open-ended stemmer is used: an unpredictable expansion would be
unexplainable, which is the opposite of what this phase is for. A name is never
normalized away entirely.

Similarity is plain **Jaccard over token sets** — bounded to [0, 1],
order-insensitive (so `total_amount` matches `amount_total`), and explainable
to a reviewer as "2 of 3 tokens shared".

## 6. Explicit alias registry

Aliases live **on the canonical field**, not buried in scoring code, so they
are reviewable and versionable:

```python
CanonicalField(
    entity_type="customer", name="customer_id", data_type=STRING,
    required=True, is_identifier=True, provenance=REPOSITORY,
    aliases=("customer_ref", "customer_no", "customer_number", "cust_id",
             "cust_no", "customerid", "customercode", "customer_code",
             "client_id", "client_ref"),
)
```

The engine never *guesses* that `cust_no` means `customer_id`; it knows because
the registry says so, and reports the match as `explicit_alias` evidence
quoting the declaration. The alias index carries its own version, which feeds
the engine's configuration fingerprint.

Alias lookup is scoped to the field's own entity, so an alias shared by two
entities cannot silently credit the wrong one.

## 7. Datatype compatibility

A deterministic matrix over the **existing** `FieldDataType` — no new type
vocabulary. "Compatible" means *could a value of the source type be represented
as the target type without inventing or destroying information?*, which is what
makes it correctly asymmetric.

Measured matrix (subset):

| source \ target | integer | decimal | string | date | datetime | object | array |
|---|---|---|---|---|---|---|---|
| **integer** | exact | widening | lossy | lossy | lossy | incompatible | incompatible |
| **decimal** | lossy | exact | lossy | incompatible | incompatible | incompatible | incompatible |
| **string** | lossy | lossy | exact | lossy | lossy | incompatible | incompatible |
| **date** | incompatible | incompatible | lossy | exact | widening | incompatible | incompatible |
| **datetime** | incompatible | incompatible | lossy | lossy | exact | incompatible | incompatible |
| **object** | incompatible | incompatible | incompatible | incompatible | incompatible | exact | incompatible |
| **array** | incompatible | incompatible | incompatible | incompatible | incompatible | incompatible | exact |

The published matrix is **generated from `compare_types`**, not duplicated, so
it cannot drift from the implementation.

- **Array ↔ scalar** is a cardinality conflict in either direction.
- **Object ↔ scalar** is a structural conflict — a modelling error, not a
  conversion.
- `UNKNOWN` (mixed-type Mongo fields, empty CSV columns, Postman query params)
  is neither compatible nor incompatible: it is *unproven*, scores 0.30, and
  never carries a candidate over the threshold on its own.

Only `INCOMPATIBLE` **vetoes** auto-selection. `LOSSY` and `UNKNOWN` are
permitted but score low enough that they rarely clear the bar unaided.

## 8. Entity context

Keeps `supplier.email` from beating `customer.email`. It *influences* the score
rather than gating it — a source entity called `tbl_cust_mast` should nudge the
customer entity ahead, not veto every other target.

| Situation | Score |
|---|---|
| normalized names identical | 1.00 |
| declared entity alias | 0.95 |
| source contains every canonical token (`fin_customer` → `customer`) | 0.85 |
| source contained by canonical (`customer` → `customer_account`) | 0.75 |
| partial token overlap | Jaccard |
| no signal at all | 0.50 (neutral) |

Neutral rather than zero, deliberately: a CSV called `export_2026_q1` says
nothing about its business object, and punishing it would make every one of its
fields unmappable.

## 9. Nested-path context

`customer.contact.email` inside a MongoDB `invoices` collection maps to
`customer.email`, not to an invoice field — measured:

```
source_context : customer.contact.email
target_context : customer.email
shared_tokens  : ['customer', 'email']
path score     : 0.666667      -> auto_selected
```

A nested path can name a different business object than its container does, and
that is ordinary rather than exotic. So a canonical entity whose name appears in
a field's **path** is brought into scope alongside the matched entity. This
widens on *evidence*, never on a guess, and flat fields are unaffected (they
score a neutral 0.50).

## 10. Candidate generation

For each (source field, canonical field) pair the engine produces a score and
its evidence. A pair with **no name evidence at all** produces no candidate —
entity and path context are corroborating signals, not licence to map an
unrelated field. Without that gate, every column in a table matched to
`invoice` would yield weak candidates purely for being in that table.

Ordering is deterministic: descending score, then qualified target name, so
ties resolve alphabetically rather than by dictionary order.

Measured, for `cust_no` in entity `fin_customer`:

```
customer.customer_id    score=0.89   confidence=high   name=explicit_alias
-> auto_selected
```

## 11. Deterministic scoring

```
score = 0.50 * name
      + 0.20 * type
      + 0.20 * entity
      + 0.10 * path
```

Each component is in [0, 1] and the weights sum to 1.0 (enforced —
`ScoringWeights.__post_init__` raises otherwise), so the total is in [0, 1] and
directly comparable against the thresholds.

| Weight | Value | Rationale |
|---|---|---|
| name | 0.50 | the dominant signal; an alias is a human's own statement that two names mean the same thing |
| type | 0.20 | corroborating, not deciding — half of any ERP is strings; a type *conflict* is handled by the veto, not this weight |
| entity | 0.20 | what keeps `supplier.email` from winning; disambiguates more real cases than type does |
| path | 0.10 | refines within an entity; three of six technologies produce flat fields |

Name-evidence scores: exact `1.00`, normalized-exact `0.98`, explicit alias
`0.94`, token overlap = Jaccard (floor `0.25`).

**This is a ranking score, not a probability.** It has not been calibrated
against observed correctness, and this phase does not claim it has. It is
called a *mapping score* / *matching confidence score* — never "94% likely
correct".

## 12. Explainability

No candidate exists without the evidence that produced it. There is no code
path returning a number without its reasoning.

A real, measured explanation:

```
cust_no -> customer.customer_id (score 0.89, high):
  the canonical field declares 'cust_no' as an explicit alias;
  type string -> string is an exact match;
  entity context 0.85;
  path context 0.5
```

decomposing to:

```json
{
  "name_match": {"kind": "explicit_alias", "score": 0.94,
                 "matched_alias": "cust_no",
                 "shared_tokens": ["customer"]},
  "type_compatibility": {"source_type": "string", "target_type": "string",
                         "compatibility": "exact", "score": 1.0},
  "entity_context": {"score": 0.85, "source_context": "fin_customer",
                     "target_context": "customer"},
  "nested_path_context": {"score": 0.5, "source_context": "cust_no",
                          "target_context": "customer.customer_id"}
}
```

score components: `name 0.47 + type 0.20 + entity 0.17 + path 0.05 = 0.89`.

A **refusal explains itself too** — see §14 and §15.

## 13. Confidence levels and auto-selection

| Band | Condition | Meaning |
|---|---|---|
| HIGH | score ≥ 0.75 | eligible for auto-selection |
| MEDIUM | score ≥ 0.50 | candidate requiring review |
| LOW | below | never auto-selected |

Nothing is a magic constant; every threshold lives on `MappingOptions` and is
configurable, and `test_thresholds_are_not_magic_constants_in_the_code` asserts
it.

Auto-selection requires **three independent gates to all pass**:

```
1. type is not INCOMPATIBLE          (veto)
2. score >= high_threshold           (0.75)
3. margin over runner-up >= 0.05     (ambiguity margin)
```

Failing any one produces `REVIEW_REQUIRED` or `AMBIGUOUS` — a result a human
can act on — rather than a confident-looking guess.

Default configuration, verbatim:

```json
{"high_threshold": 0.75, "medium_threshold": 0.5, "ambiguity_margin": 0.05,
 "minimum_candidate_score": 0.2, "max_candidates_per_field": 5,
 "require_type_compatibility_for_auto": true, "detect_target_collisions": true,
 "single_target_per_source_field": true,
 "weights": {"name": 0.5, "type": 0.2, "entity": 0.2, "path": 0.1}}
```

## 14. Ambiguity

Measured — a CSV column `total` in `export_2026_q1`:

```
total: invoice.amount (0.82) and purchase_order.amount (0.82)
       differ by 0.0, below the required margin of 0.05
outcome = AMBIGUOUS, selected = None
```

Both candidates are preserved for review. Choosing one automatically would be a
coin toss dressed up as a decision.

## 15. Unmapped fields

A source field with no acceptable candidate stays unmapped. 100% coverage is
never forced.

```
legacy_internal_flag_74   unmapped :: no canonical target scored above the
                                      minimum candidate threshold of 0.2
row_version               unmapped :: no canonical target scored above the
                                      minimum candidate threshold of 0.2
```

An unmapped field is **better than an incorrect mapping**, and it does not
reach the profile.

## 16. Target collisions and one-to-many

Two source fields auto-selected onto one canonical target is not silently
accepted — at most one is the real identifier, and choosing wrongly corrupts
every record:

```
2 source fields ['cust_no', 'customer_number'] were all selected for
customer.customer_id; kept 'cust_no' and flagged the rest for review

cust_no           auto_selected
customer_number   review_required
```

Resolution is deterministic (highest score, ties by source path). A **manual
override is never demoted** — a human has already decided.

One source field is never automatically mapped to several targets
(`single_target_per_source_field`). Phase 1's `FieldMapping` can express
one-to-many, but only when a human specifies it.

## 17. Coverage

Three levels, all measured — this is **mapping** coverage, not data quality.

```json
{"total_fields": 4, "mapped_fields": 3, "ambiguous_fields": 0,
 "unmapped_fields": 1, "review_required_fields": 0,
 "coverage_ratio": 0.75, "ambiguity_rate": 0.0, "unmapped_rate": 0.25,
 "all_required_targets_covered": true,
 "entities": [{"source_entity": "fin_customer",
               "target_entity_type": "customer",
               "total_fields": 4, "mapped_fields": 3,
               "coverage_ratio": 0.75,
               "missing_required_targets": [],
               "required_target_coverage_complete": true}]}
```

**Required-target coverage** reports canonical required fields that nothing maps
onto. A profile failing it is *not transformation-ready*, and Phase 9 must not
be told otherwise.

## 18. MappingProfile and FieldMapping

The persisted output is the **existing Phase 1 contract**, unmodified. No
`UniversalMappingProfile`, no competing model. One profile per
(source entity → canonical entity), which is the contract's own scoping.

```
mapping_id         : demo_pg.fin_customer.customer.85250aea4fd5
source_entity      : fin_customer -> target_entity_type: customer
status             : suggested
metadata:
  canonical_model_identity : erp_core@1.0
  mapping_engine_version   : 1.0
  alias_index_version      : 1.0
  config_fingerprint       : h=0.75,m=0.5,a=0.05,min=0.2,typeveto=1,
                             w(0.5,0.2,0.2,0.1)/norm@1.0/abbr=1/syn=1/
                             noise=1/alias@1.0/model=erp_core@1.0
  source_schema_hash       : <sha256 of the schema snapshot>
  applied_to_data          : false

FieldMapping: cust_no       -> customer_id  conf=0.89 status=auto_accepted
FieldMapping: customer_name -> name         conf=0.89 status=auto_accepted
FieldMapping: email_addr    -> email        conf=0.89 status=auto_accepted
```

Only **selected** decisions become `FieldMapping`s. An ambiguous or unmapped
field is deliberately absent rather than present with a low confidence: a
profile is a set of instructions, and an instruction nobody has decided on is
not one. The full picture lives in `MappingResult.decisions`.

`status` reuses the existing `MappingStatus` vocabulary — engine-selected is
`AUTO_ACCEPTED`, a human decision is `APPROVED`. The **whole profile** is only
ever `SUGGESTED`: no profile auto-approves itself.

`transformations` is always empty. Deciding a value needs a date parse is a
mapping decision; choosing the format is a transformation decision, and that is
Phase 9's.

Every `FieldMapping.metadata` carries a compact evidence summary, so a profile
reloaded from the catalog still explains itself without the supplemental
objects.

## 19. Manual overrides

A human decision supersedes the engine's suggestion — but is validated, not
trusted blindly:

```
override: cust_no -> customer.phone  (reason: "legacy column reuse")

outcome                = manual_override
selected               = customer.phone
FieldMapping.status    = approved            (vs auto_accepted)
metadata.selection     = manual_override
```

The engine's own suggestions are kept beside the override so the disagreement
stays visible. An override is **refused** when it names an unknown canonical
target (`CanonicalTargetNotFoundError`), an unknown source field
(`SourceFieldNotFoundError`), or a type that cannot convert
(`InvalidMappingOverrideError`) — a mapping that cannot convert would fail in
Phase 9, and it is far cheaper to reject it here.

`target=None` forces a field to stay unmapped, which is a legitimate review
decision. **No override ever modifies the `SourceSchema`.**

A `RejectedCandidate` is suppressed for the rest of the reviewed context, so
re-running does not keep re-proposing what a human already declined.

## 20. Persistence

Uses the **existing Phase 2 catalog** — `save_mapping_profile` /
`get_mapping_profile`. No second mapping store is introduced and no versioning
logic is added on top; the catalog owns upsert semantics.

Proven against the live `erp_catalog` PostgreSQL schema: profile save, profile
round-trip, field mappings round-tripping *with their evidence*, republishing
idempotency, and the target-model identity surviving persistence.

## 21. Determinism

Same schema + same canonical model + same configuration ⇒ same candidate
ordering, same scores, same selections, same profile identity. No randomness,
no learned model, no external call — `test_the_engine_uses_no_randomness`
asserts no RNG import.

Profile identity is a hash of what genuinely determines the mapping's content:

```
source_system_id + source_schema_id + source_schema_hash
+ source_entity + target_entity_type
+ canonical model identity + engine version + config fingerprint
```

Explicitly **not** timestamp, UUID or filesystem path. Measured — changing only
the schema hash changes the identity:

```
schema_hash 000...  ->  pg.fin_customer.customer.f6a853a0fc5a
schema_hash aaa...  ->  pg.fin_customer.customer.1442acaa7df6
```

## 22. Schema and target-model evolution

Adding a field preserves the existing mappings and generates a candidate for
the new one — measured:

```
V1 selected: ['customer.customer_id', 'customer.name']
V2 selected: ['customer.customer_id', 'customer.email', 'customer.name']
```

Unrelated mappings are never silently invalidated. Coverage change is reported.

Every profile records `canonical_model_identity` (`erp_core@1.0`), so a V1
mapping can never be mistaken for one generated against V2.

## 23. Privacy and offline operation

Mapping is **schema-based**. Allowed: field names, entity names, types,
declared structural metadata. Never consulted: business row values, MongoDB
document values, Postman example values, credentials, OCR text.

The supplemental models are *structurally* incapable of holding a value — every
field is a name, a type, a score or a count.

Enforced by test:

| Test | Asserts |
|---|---|
| `test_mapping_needs_no_business_values` | mapping succeeds from schema alone |
| `test_no_secret_reaches_a_mapping_profile` | planted secrets in schema metadata never surface |
| `test_no_schema_free_text_leaks_into_the_result` | descriptions do not leak into results |
| `test_no_error_message_carries_schema_free_text` | errors expose no free text |
| `test_nothing_is_logged_during_mapping` | no logging side channel |
| `test_the_candidate_model_cannot_hold_a_value` | structural proof |

Offline and local:

| Test | Asserts |
|---|---|
| `test_no_module_imports_a_network_or_ai_dependency` | no `requests`/`httpx`/`openai`/`anthropic`/… |
| `test_importing_the_package_loads_no_ai_or_network_module` | nothing loaded at import time |
| `test_no_embedding_or_vector_vocabulary_exists` | no embedding/Qdrant vocabulary |
| `test_the_mapping_package_does_not_import_sqlalchemy` | no direct DB access |
| `test_the_package_has_no_bpi2020_import` | no dependency on the frozen prototype |

No LLM client, no remote embeddings, no external ontology service, no network
of any kind.

## 24. Evaluation benchmark

`tests/erp_pipeline/mapping/test_mapping_benchmark.py`.

The expected mappings were written **by hand, before running the engine
against them**, from what each source field means in ERP terms. They are not
engine output relabelled as ground truth — that would measure only whether the
engine agrees with itself.

**68 labels: 60 positive, 8 negative**, spanning PostgreSQL snake_case, MySQL
camelCase, MongoDB nested paths, CSV abbreviations, OpenAPI camelCase and
Postman inferred names. `EXPECTED_UNMAPPED` matters as much as the positive
labels: a benchmark that only rewarded coverage would push the engine toward
guessing, which is the failure mode this phase exists to avoid.

## 25. Actual metrics

Measured, not estimated:

```
labelled mappings         : 68 (60 positive, 8 negative)
top-1 accuracy            : 1.0
top-3 recall              : 1.0
auto-selection precision  : 1.0 (60/60)
automatic coverage        : 0.8824
ambiguity rate            : 0.0
unmapped rate             : 0.0882
correct refusal rate      : 1.0
alias-independent top-1   : 1.0 (18/18 labels the alias registry never declared)
```

**How to read these honestly.** This is a small synthetic corpus, not a
production SLA, and perfect scores on 68 hand-written labels are a statement
about a controlled benchmark rather than about ERP data in general. The two
figures that carry the most weight are:

- **auto-selection precision 1.0** — everything the engine chose automatically
  was right. This is the number that matters for a conservative engine.
- **alias-independent top-1 1.0 on 18 labels** — the registry could have been
  overfitted to the corpus; on the 18 spellings it never declares, matching
  still succeeds through normalization and token evidence. Without this figure
  the other numbers would mostly be measuring the alias table.

Automatic coverage of 0.8824 is *deliberately* below 1.0: the residual 11.76%
is 6 fields correctly left unmapped and 2 correctly deferred to review.

## 26. Limitations

1. **The corpus is synthetic and small** (68 labels). Metrics are regression
   guards and visibility, not generalization claims.
2. **The score is uncalibrated.** It ranks; it is not a probability.
3. **Jaccard ignores token order** and weights all tokens equally
   (`total_amount` ≡ `amount_total`). Accepted because it is one weighted
   component, never the whole score.
4. **The canonical vocabulary is minimal** — 3 entities, 14 fields. A real ERP
   deployment supplies its own via `from_dict`.
5. **Abbreviation and synonym tables are hand-maintained.** A wrong entry
   silently corrupts every mapping that touches it, which is why they are kept
   small and explicit.
6. **No semantic/embedding matching.** Deliberate: Phase 8 is offline and
   deterministic. Semantic augmentation is separate future research.
7. **SQL Server live verification is still deferred** (Phase 4), so no mapping
   run has been executed against a live SQL Server schema — though the contract
   it produces is the same one the engine already consumes.

## 27. Phase 9 boundary

Phase 8 produces a `MappingProfile`. It does **not**:

- create a `CanonicalRecord` from business rows
- execute ETL or apply a mapping to any value
- execute a `TransformationRule` (declared rules are inspected structurally
  only — `test_a_declared_transformation_is_inspected_but_never_run`)
- clean data, sync incrementally, generate embeddings, write Qdrant vectors,
  build an API or UI, or call any LLM

Every one of those is asserted absent by a static test, and every generated
profile carries `"applied_to_data": false` as data, so a stored profile cannot
be mistaken for something already applied.

Phase 9 executes what Phase 8 decided.
