# Phase 11 — Integration Readiness Report

**Date:** 2026-08-25
**Scope rule observed:** Member 4 only. No other member's repository was
modified. A sibling repository (`low-code-workflow-engine`) exists in the
workspace and was **not read or touched**.

This report records what changed and why. The contract itself lives in
[`group_integration_contract.md`](group_integration_contract.md)
and is not repeated here.

---

## 1. Baseline

| | Before | After |
|---|---|---|
| collected | 3616 | **3730** |
| passed | 3553 | **3667** |
| failed | 0 | **0** |
| errors | 0 | **0** |
| skipped | 63 | **63** |

**+114 collected, +114 passed** — exactly the new integration suite, with
nothing auto-parametrized. Skips unchanged at 63.

Unchanged structural facts: **24 API operations · 13 filterable fields ·
7 job types · 3 content kinds**. No new endpoint, no new Qdrant collection, no
new JobType.

## 2. Read-only audit

The audit came first, and it changed the plan. Every route the brief listed as
integration-critical already existed:

```
GET  /v1/capabilities          POST /v1/files/csv         POST /v1/files/documents
GET  /v1/schemas/{id}          POST /v1/mappings/suggest  PUT  /v1/mappings/{id}
POST /v1/jobs                  GET  /v1/jobs/{id}         POST /v1/search
GET  /v1/records/{id}          GET  /v1/representations/{id}
POST /v1/responses/adapt
```

**No route required by integration was missing.** Findings from the audit:

- **API key middleware** already correct: `X-API-Key`, constant-time
  comparison, all mutating methods guarded, health endpoints public,
  `protect_reads` configurable. Not weakened.
- **CORS** already default-safe: `cors_origins` empty, credentials off. Not
  weakened.
- **Sensitivity** already exposed on both `SearchHitResponse.metadata` and
  `RepresentationResponse`. Contract L needed a test, not code.
- **Frontend** carries no embedded key: `VITE_API_KEY` is declared in
  `vite-env.d.ts` and blank in `.env.example`, and no source file reads it.
- **The collection limitation is real but not silent** — the first record is
  adapted and the caller is warned with the total count.
- **`GET /v1/capabilities` could not describe the integration surface.** It
  advertised source types, file types, job types and content kinds, but nothing
  a partner could use to discover response adaptation, representation
  resolution, semantic search, automatic document indexing, remote assets or
  sensitivity handling — and it collapsed *supported* and *enabled* into single
  booleans.

## 3. Production files changed

Three files. Every other Phase 11 deliverable is tests, fixtures, a harness and
documentation.

| File | Change |
|---|---|
| `api/schemas.py` | new `CapabilityStatus` model; `integration_capabilities` field on `CapabilitiesResponse` |
| `api/routers.py` | `_integration_capabilities()` computing status from actual wiring |
| `orchestration/service.py` | pass declared `sensitivity` into `DocumentAttachment` |
| `storage/service.py` | `state_store if ... is not None else ...` instead of `or` |

## 4. Why each production change was necessary

### 4.1 Capabilities — a proven description gap

Members 2 and 3 could not discover the capabilities they depend on. The new
block reports each as three facts — `supported`, `enabled`, `detail` — with
every `enabled` derived from a service actually being present.

The two booleans are separate because collapsing them is how an integration
contract starts lying. Remote asset fetching is implemented and ships disabled;
a partner told only `true` would plan around a feature that refuses every call
it makes. A bare application honestly reports nearly everything disabled, while
`response_adaptation` and `sensitivity_metadata` — which need no services —
stay enabled.

The pre-existing fields were left exactly as they were, so a consumer written
against Phase 13 keeps working.

### 4.2 Uploaded-document sensitivity — a declaration that was dropped

`POST /v1/files/documents` accepted a `sensitivity` form field, validated it
against the enum, and refused a typo with 422. `DocumentAttachment` had a
`sensitivity` field and emitted it into metadata. **The line joining them did
not exist.** An upload declaring `restricted` was accepted, validated, and then
indexed with no classification at all.

Phase 10 exercised the DB-BLOB and job-option channels and measured propagation
as passing; the HTTP form-field channel was never followed end-to-end. It looked
complete from both ends and was broken in the middle — which is the specific
class of defect an integration phase exists to find.

Consequence before the fix: a restricted birth certificate indexed as if it
carried no classification, so nothing downstream — routing, encryption
selection, or the governance layer reading `sensitivity` off a hit — saw it as
restricted. One line, no behaviour change for anything that was already working.

### 4.3 The state store that was silently replaced

`StorageService.__init__` read:

```python
self._state = state_store or InMemoryTierStateStore()
```

`InMemoryTierStateStore` defines `__len__`. **An empty one is falsy** — and an
empty one is exactly what a caller passes at startup. So the caller's store was
discarded and a private one substituted. Nothing failed loudly: writes
succeeded, searches worked, and the state simply was not where the caller had
put it.

It surfaced only in the restart contract test, because a restart is the first
moment anything looks at the caller's store again. After the rebuild, every
search hit resolved to `representation_id: None` and the endpoint returned 500.

An AST scan of the production package found this as the **only** occurrence of
the pattern across twelve classes that can be falsy when empty. `PostgresTierStateStore`
defines no `__len__`, so production Postgres deployments were unaffected; the
defect bit in-memory and single-process configurations. Fixed with `is not None`
and pinned by a test that asserts the premise (`assert not store`) before
asserting the fix.

## 5. Contract fixes

Three of my own expectations were wrong, and the API was right each time. All
three are now documented facts rather than corrected assertions:

- `POST /v1/jobs` returns **202 Accepted**, not 201. The job is accepted, not
  finished.
- A source-native job needs a **registered source** (422 otherwise), and an
  inferred CSV schema has **no primary key** — so rows are refused with
  `"no usable record identity"` rather than indexed by row position. Declaring
  `options.key_fields` makes all rows transform. This refusal is correct: a
  row's position changes the moment a row is inserted above it.
- A **binary response puts its text in `assets[0].text`**, not `llm_ready`,
  which is `{}` with `partial: true`. A PDF has no structured fields for
  field-selection to select. Member 2 would have hit this on day one.

## 6. Integration fixtures

`tests/erp_pipeline/integration/fixtures/` — seven synthetic JSON payloads, no
secrets, no large binaries:

`member1_allow.json`, `member1_deny.json`,
`member1_allow_with_conditions.json`, `member2_employee_response.json`,
`member2_invoice_response.json`, `member2_collection_response.json`,
`member2_error_response.json`.

`fakes.py` holds `FakeMember1`, `FakeMember2`, `FakeMember3` and
`FakeLegacyErp`. They live in the **test tree only**; a structural test proves
no production module imports them, and none of the forbidden client classes
exists in `src/erp_pipeline`.

They are counters more than simulations, because the interesting assertions of
this phase are arithmetic.

## 7. Tests added

114 tests across five files. All drive the **HTTP surface** — no test reaches
into `OrchestrationService` or a store to make a step work.

| File | Tests | Covers |
|---|---|---|
| `test_member3_contracts.py` | 23 | A, B, C, D, L |
| `test_member2_contracts.py` | 20 | E — JSON, binary, collection bound, no re-execution |
| `test_group_flows.py` | 19 | F, G, H, I + live-vs-indexed distinction |
| `test_integration_security.py` | 28 | J, K, M, P + frontend key exposure |
| `test_integration_contracts.py` | 24 | N, O, R |

Notable ones: `test_csv_business_rows_are_not_searchable_before_a_job_runs`
(the admission invariant), `test_member4_still_returns_the_restricted_content`
(the Member 1 boundary, asserted as a test rather than promised in prose), and
`test_every_advertised_capability_maps_to_a_real_contract` (which is why the
capability list cannot quietly grow marketing entries).

## 8. Mini-evaluation

`scripts/evaluate_integration_contract.py` →
`artifacts/integration_contract_evaluation.json`

**21/21 scenarios passed. All nine gates at zero.** Member 2 ERP executions
expected/actual: **5 / 5**.

In-process latency: upload → searchable ~189 ms; search → resolved ~1315 ms
(includes first-query model warm-up); `/v1/responses/adapt` ~21 ms. These are
harness measurements and are labelled as such — no fake Member 1 or Member 2
timing is folded into any claim about production ERP latency.

One self-caught harness defect: the first version summed Member 2 executions at
each call site and forgot one fake, reporting 5 expected against 4 actual. That
looked like a missing ERP call rather than a missing addition. Replaced with a
single sum over every fake.

## 9. Full regression

```
collected: 3730
passed:    3667
failed:    0
errors:    0
skipped:   63
duration:  460.94s (7:40)
```

**+114 / +114**, exactly the new suite. **No skip-count change** — every new
test runs in-process against a wired application, so nothing new depends on
infrastructure that might be absent.

Targeted suites (integration, api, response_adaptation, storage,
orchestration): **900 passed, 27 skipped, 0 failed** in 162.53s.

## 10. Existing research artifact impact

No prior evaluation artifact was overwritten. MD5-verified: only
`integration_contract_evaluation.json` was created; all ten earlier
artifacts are byte-identical with their original timestamps.

`artifacts/openapi_contract_snapshot.json` regenerates from its own test on every run —
pre-existing behaviour, and it is **not** in the preserved list. After Phase 11
it carries the `CapabilityStatus` schema (3 references) and the
`integration_capabilities` field (1), because that is what the API now serves.
A generated contract snapshot that did *not* change here would be the problem:
it is the artifact all four members integrate against.

`response_adaptation_evaluation.json` was backed up before the full
regression and verified byte-identical afterwards — its evaluation was not
re-run in this phase, so no restore was needed.

No Phase 1–10 behaviour regressed. Phase 14's suite passes unchanged.

## 11. Remaining integration risks

1. **A credential inside an ERP business payload is passed through as
   content.** Transport metadata is redacted; response content is not.
   Adaptation is faithful by design and no content classifier was added
   (Phase 10 forbade one, and a filter catching `db_password` but not `dbPwd`
   reads as protection without being it). *Mitigation: Member 2 must not
   return credentials in ERP business payloads.*
2. **Collection responses adapt the first record only.** Declared, not silent.
   If Member 2's actual tools turn out to need list results for the demo, that
   is a separate implementation with its own tests and evaluation.
3. **The fakes are fakes.** Members 1, 2 and 3 do not exist in this workspace,
   so these contracts are proven against recorded representative payloads. A
   real Member 2 may shape its requests differently; the binary-response
   `assets[].text` finding is exactly the kind of mismatch that surfaces on
   first contact.
4. **`protect_reads` defaults to `False`.** Reads are unauthenticated unless a
   deployment enables it. Deliberate and documented, but it is a decision the
   group must make consciously before any public deployment.
5. **A browser calling Member 4 directly** would require embedding the service
   key. The BFF architecture is documented and tested; the risk is that a demo
   shortcut becomes the default.
6. **Indexed freshness** is bounded by the sync poll interval, so a
   transactional fact must come through Member 2.

## 12. Phase 12 readiness

Nothing in Phase 12's scope was touched: no benchmark was rewritten, no failed
retrieval query was tuned, no prior artifact was rewritten, no thesis claim was
generated, no readiness score was produced, and the repository was not frozen.

Member 4 is ready for Member 3 frontend integration, Member 2 ERPBridge/MCP
integration, and Member 1's governance architecture — without having absorbed
any of their responsibilities.
