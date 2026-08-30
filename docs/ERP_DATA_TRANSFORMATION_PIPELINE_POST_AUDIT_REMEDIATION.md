# Post-Audit Remediation — ERP Data Transformation Pipeline

**Service:** ERP Data Transformation API
**Date:** 2026-08-29
**Scope:** remediation of five verified audit findings. No architectural change,
no feature removal, no research-metric change, no new Azure resources.

The audit itself is unchanged:
[`ERP_DATA_TRANSFORMATION_PIPELINE_COMPLETE_AUDIT_AND_HANDOFF.md`](ERP_DATA_TRANSFORMATION_PIPELINE_COMPLETE_AUDIT_AND_HANDOFF.md).

---

## Summary

| # | Finding | Status |
|---|---|---|
| 1 | Storage-location policy declared cloud tiers as on-premises | **FIXED** — locations configurable; restricted data now fails closed |
| 2 | Qdrant payload indexes existed only operationally | **FIXED** — both tiers ensure them idempotently from `FILTERABLE_FIELDS` |
| 3 | `httpsOnly: false` | **FIXED** — HTTPS-only enabled; HTTP now 301-redirects |
| 4 | Frontend service-key exposure risk | **VERIFIED SAFE + DOCUMENTED** — no key in browser code |
| 5 | CORS limited to localhost | **UNCHANGED BY DESIGN** — no production origin exists yet |
| — | API key rotation (compromised via copied cURL) | **DONE** — old key now 401, new key 200 |

**Regression: 3792 collected · 3729 passed · 0 failed · 0 errors · 63 skipped ·
30 warnings · 8:33.** Baseline was 3762/3699; **+30** is exactly the new test
file.

---

## FINDING 1 — Storage location and restricted data

### Root cause

`storage_policy.py` carried a module-level constant:

```python
DEFAULT_TIER_LOCATIONS = {HOT: ON_PREMISES, WARM: ON_PREMISES, COLD: ON_PREMISES}
on_premises_only_sensitivities = frozenset({SensitivityLevel.RESTRICTED})
```

The router genuinely enforced the constraint — it prohibits any tier whose
declared location is not `ON_PREMISES` for restricted data. But because all
three tiers were *declared* on-premises, the constraint excluded nothing.

Two contributing facts:

1. `build_storage_service()` in `runtime/services.py` passed **no policy**, so
   the default constant was always used.
2. `tier_locations` was a dataclass field with **no environment variable**, so a
   deployment could not correct it without editing code.

The source comment read *"All local today"* — true when written, false once HOT
and WARM moved to managed Qdrant. **A compliance control reading a stale
constant is worse than no control: it reports success while delivering
nothing.**

### What was already correct

Two mechanisms needed no change and were left alone:

- `StoragePolicyRouter.route()` **already fails closed** — when every tier is
  prohibited it raises `PolicyViolationError` rather than picking one.
- `StoragePolicy.__post_init__` **already refuses** a location map that omits a
  tier, with a clear message.

The gap was purely that locations were not configurable, so the existing
enforcement never engaged.

### Fix

A new `StorageLocationSettings` block reads the tier locations from the
environment, using the enum values the implementation **already defines** —
`on_premises` and `external`. No new location vocabulary was invented, and
Azure Files is **not** labelled on-premises.

Defaults are **inferred from fact, not assumed**:

| Tier | Default | Why |
|---|---|---|
| HOT | `external` when `ERP_QDRANT_MODE=cloud`, else `on_premises` | A cluster addressed by URL with an API key is not on-premises. Requiring an operator to declare that a second time is how the map went stale in the first place |
| WARM | same inference | same |
| COLD | `on_premises`, **must be declared** for cloud storage | A mounted cloud share and a local disk look identical from a filesystem path — this genuinely cannot be inferred |

`build_storage_service()` now constructs the policy with the deployment's real
locations. The Azure App Service declares all three `external`.

### Verified behaviour on the live deployment

Two controlled probes were uploaded to the deployed service:

| Probe | `sensitivity` | Job status | `vectors_stored` | `vectors_failed` |
|---|---|---|---|---|
| `probe_internal.pdf` | `internal` | **succeeded** | **1** | 0 |
| `probe_restricted.pdf` | `restricted` | **partial** | **0** | **1** |

Restricted data was **rejected, not stored**. Non-restricted data routes exactly
as before. Qdrant's restricted-point count was **1 before and 1 after** — the
pre-existing point only; the restricted probe created nothing.

**The meaning of RESTRICTED was not changed.** `requires_on_premises(RESTRICTED)`
is still `True`; a test pins it. Nothing was relabelled to make deployment pass,
and no restricted vector was moved between cloud tiers.

### Files changed

| File | Change |
|---|---|
| `src/erp_pipeline/runtime/settings.py` | `StorageLocationSettings`, `STORAGE_LOCATION_VARIABLES`, field on `RuntimeSettings`, startup validation, `describe()` entry |
| `src/erp_pipeline/runtime/services.py` | `build_storage_service` now passes `StoragePolicy(tier_locations=…)` |

One change was **made and then reverted**: a `.get()` fallback in
`location_of()`. `__post_init__` already refuses an incomplete map, which is a
clearer single failure point than a silent default, so the plain lookup was
restored.

---

## FINDING 2 — Qdrant payload index durability

### Root cause

Managed Qdrant refuses a filtered search on an unindexed field with
`400 Bad Request: Index required but not found`. A local single-node Qdrant
accepts the same query and scans — which is why this never appeared in
development and appeared immediately on first cloud deployment.

The 13 indexes were created by a one-off operational script. A grep for
`create_payload_index` across `src/` returned nothing, and `ensure_collection()`
set only `VectorParams`. Any `ensure_collection(recreate=True)` would have
dropped them.

### Fix

A new module `storage/payload_indexes.py` derives the field list from
**`FILTERABLE_FIELDS`** — the same canonical tuple the filter builder and API
validation already use. No duplicate list is maintained; a second one would
eventually disagree, and the failure mode of that disagreement is a 400 on a
filter the API advertises.

All fields use the `keyword` schema, because every filterable field is matched
with `MatchValue` on a string and several are closed enums. No field is matched
by range, prefix or full text, so no other index type is warranted.

`ensure_payload_indexes()` handles every state safely: missing collection
(reported, nothing attempted), no indexes (all created), partial (only gaps),
fully indexed (nothing), repeated startup (idempotent). Failures are **reported,
not raised** — a tier that cannot add an index still stores and retrieves
vectors, and refusing to start would trade a degraded filter for a total outage.

**No collection is ever recreated to add an index**, and no new collection name
is introduced. `erp_vectors_hot` and `erp_vectors_warm` remain the only physical
collections.

### A defect I introduced and caught

The first version placed the index call only after `create_collection()`. The
`if not recreate: return` path returned **before** it — so an *existing*
collection, which is the state of every already-deployed cluster, would never
have been indexed. Fixed: both paths now ensure indexes. A test pins it
specifically.

### Files changed

| File | Change |
|---|---|
| `src/erp_pipeline/storage/payload_indexes.py` | **New** — `ensure_payload_indexes`, `required_payload_indexes` |
| `src/erp_pipeline/storage/hot_tier.py` | `ensure_collection` ensures indexes on both paths |
| `src/erp_pipeline/storage/warm_tier.py` | same |

### Verified

Live: `erp_vectors_hot` and `erp_vectors_warm` each report **13 payload
indexes**. A filtered search against the deployed API returned a correct hit
with `content_kind` and `source_system_id` filters applied.

---

## FINDING 3 — HTTPS only

**Root cause:** the App Service was created without `--https-only`, so it served
both HTTP and HTTPS. A client using `http://` would have transmitted its
`X-API-Key` in cleartext.

**Fix:** `az webapp update --https-only true` on the **existing** app. No new
resource, no FastAPI change.

**Verified:**
```
httpsOnly = true
http://…/v1/health/live  →  HTTP 301 → https://…/v1/health/live
```

---

## FINDING 4 — Frontend service-key security

**Verified safe.** `VITE_API_KEY` is declared in `frontend/src/vite-env.d.ts`
and left blank in `frontend/.env.example`, but **no source file reads it** —
confirmed by searching for `import.meta.env.VITE_API_KEY` across `frontend/src`.
No key is bundled today.

The separate frontend repository was **not modified**; only this repository's
small developer UI was inspected.

**The requirement, restated in the integration guide:**

```
Browser  ──user session──▶  Trusted backend / BFF  ──X-API-Key──▶  This API
```

`X-API-Key` is a server-to-server secret. A `VITE_*` variable is inlined into
the published bundle and readable by anyone who loads the page. This is
**separate from CORS** — CORS controls which origins a browser may call from; it
does nothing to protect a key already in the bundle.

---

## FINDING 5 — CORS

**Unchanged, deliberately.** The deployed value remains `http://localhost:5173`.

A repository search found **no production frontend origin** — `frontend/.env.example`
declares only `VITE_API_BASE_URL=http://127.0.0.1:8000`. Per instruction, the
localhost configuration is left as-is.

**To update later**, supply the exact production origin and run:

```bash
az webapp config appsettings set -g rg-erp-data-transformation -n erp-data-transformation-api-ju0h8k --settings "ERP_API_CORS_ORIGINS=http://localhost:5173,https://<your-frontend-origin>"
```

Never `*`, and never a wildcard with credentials.

---

## API key rotation

The previous `ERP_API_KEY` was exposed in a copied Swagger cURL and treated as
compromised.

- A new 48-character key was generated locally. **Its value was never printed,
  logged, written to Markdown, or committed.**
- Updated in the Azure App Service setting and in `.env.azure` (git-ignored).
- `ERP_QDRANT_API_KEY`, `ERP_COLD_ARCHIVE_KEY` and
  `ERP_REPRESENTATION_ENCRYPTION_KEY` were **not** rotated — no evidence of
  exposure.

**Verified against the live deployment:**

| Request | Result |
|---|---|
| Missing key | **401** |
| **Old key** | **401** |
| Wrong key | **401** |
| New key | **200** |

---

## Restricted-vector inventory

One restricted point exists in Qdrant Cloud. Business values are redacted.

| Field | Value |
|---|---|
| Point id | `e173324b-d04c-5358-a214-4fed64702372` |
| Representation | `ai:document:employee_id_<redacted>_birth_certificate_e11158b5…c00000.56dc47b2` |
| | *(the representation id embeds the business key, so it is redacted here too; the full id is recoverable from the deployment)* |
| Source system / entity | `legacy_hr` / `documents` |
| Business identity | `employee_id=<redacted:4449b616>` |
| Document type | `birth_certificate` |
| Content kind | `document_chunk` |
| Collection / tier | `erp_vectors_hot` / HOT |

`erp_vectors_warm` contains **0** restricted points.

**Provenance:** this point was created during the Azure deployment task as an
OCR verification probe, before the location policy was corrected. It is test
material, not production HR data.

### Cleanup executed — 2026-08-29, on explicit approval

**Pre-deletion verification.** All five attributes were re-checked against the
live point immediately before deletion, and the delete script carried assertion
guards so a mismatch would abort rather than remove the wrong vector:

| Check | Expected | Actual | |
|---|---|---|---|
| Point id | `e173324b-d04c-5358-a214-4fed64702372` | identical | MATCH |
| `sensitivity` | `restricted` | `restricted` | MATCH |
| `source_system_id` | `legacy_hr` | `legacy_hr` | MATCH |
| `source_entity` | `documents` | `documents` | MATCH |
| `document_type` | `birth_certificate` | `birth_certificate` | MATCH |
| Identified OCR probe | yes | yes | MATCH |

**Result.**

| | Before | After |
|---|---|---|
| Restricted points in `erp_vectors_hot` | **1** | **0** |
| Restricted points in `erp_vectors_warm` | **0** | **0** |
| **Total restricted in cloud** | **1** | **0** |
| `erp_vectors_hot` total points | 5 | 4 (delta exactly 1) |
| Payload indexes per collection | 13 | 13 (unaffected) |

**Collection cleaned:** `erp_vectors_hot` only. One point, by explicit id.

**Not touched:** the authoritative PostgreSQL representation, upload metadata,
job history, schema/catalog records, and every other Qdrant point. Qdrant still
holds exactly the same five collections — nothing was created or removed at
collection level.

**Authoritative representation preserved — verified.**
`GET /v1/representations/{id}` still returns **HTTP 200** with
`content_kind: document_chunk`, `document_type: birth_certificate`,
`sensitivity: restricted` and its text intact. The data was never lost; only its
cloud vector was withdrawn.

**No cloud re-index performed.** The representation was **not** moved to WARM or
COLD. With no eligible on-premises tier it remains unindexed, which is the
correct outcome — relocating it between cloud tiers would have been relabelling,
not remediation.

**No longer reachable by vector search — verified.** A filtered search for
`content_kind=document_chunk, document_type=birth_certificate` returns **0
hits**, and **0 restricted hits**.

### Post-cleanup probes

| Probe | `sensitivity` | Job status | `vectors_stored` | `vectors_failed` |
|---|---|---|---|---|
| Restricted, post-fix | `restricted` | **partial** | **0** | **1** |
| Internal, post-fix | `internal` | **succeeded** | **1** | 0 |

The restricted probe was **blocked and created no Qdrant point**. The internal
probe indexed normally and was returned by a filtered search
(`tiers_searched: [hot, warm]`, model `all-MiniLM-L6-v2`), confirming ordinary
Qdrant operation is unaffected.

**Final cloud state:** `erp_vectors_hot` 5 points, **all `internal`**, 0
restricted; `erp_vectors_warm` 0 points, 0 restricted.

---

## Final security posture

| Control | State |
|---|---|
| Transport | **HTTPS-only**, HTTP 301-redirects |
| API authentication | `X-API-Key`, constant-time, rotated; old key rejected |
| Read protection | `ERP_API_PROTECT_READS=true` |
| CORS | Explicit origin list, never `*` |
| Restricted data | **Fails closed** — no on-premises tier, so refused |
| Tier locations | Declared truthfully as `external`; Azure Files not mislabelled |
| Representation encryption | AES-256-GCM at CONFIDENTIAL+, separate key, fail-closed |
| COLD archive | gzip + AES-256-GCM, separate key |
| Qdrant | Cloud only; **0 localhost calls** in 86 logged requests |
| Payload indexes | 13 per collection, now created by code |
| Frontend key | Not present in browser source |
| PostgreSQL | TLS required, Azure-services-only firewall |

---

## Regression

| | Before | After |
|---|---|---|
| collected | 3762 | **3792** |
| passed | 3699 | **3729** |
| failed | 0 | **0** |
| errors | 0 | **0** |
| skipped | 63 | **63** |
| warnings | 30 | **30** |
| duration | 10:40 | **8:33** |

**+30 collected, +30 passed** — exactly
`tests/erp_pipeline/storage/test_tier_locations_and_payload_indexes.py`. Skips
unchanged. No existing test was modified.

Focused suites: storage + runtime + api → **693 passed, 29 skipped, 0 failed**.

### Tests added (30)

**Finding 1 (configuration):** defaults on-premises without cloud Qdrant ·
HOT/WARM inferred `external` from cloud mode · COLD must be declared ·
explicit declaration overrides inference · malformed location refused ·
tier map uses the real enum · incomplete map refused at construction.

**Finding 1 (routing):** restricted rejected when every tier is external ·
restricted never lands in HOT/WARM/COLD (parametrised per tier) · restricted
succeeds with a real on-premises tier · restricted routes only to the on-prem
tier in a mixed deployment · public/internal/confidential still route normally ·
`requires_on_premises(RESTRICTED)` unchanged · strictest-wins unchanged ·
default still `internal`.

**Finding 2:** field list is the canonical `FILTERABLE_FIELDS` (13) · all
created on a bare collection · only gaps on a partial one · nothing when
complete · idempotent on repeat · missing collection reported not raised ·
failure reported without stopping others · "already exists" counts as present ·
**both tiers index an existing collection** · no new collection name introduced.

---

## Azure changes

| Change | Detail |
|---|---|
| Image | `erp-data-transformation-api:v3` built in the **existing** ACR |
| Deployment | Same App Service, restarted |
| `httpsOnly` | `false` → **`true`** |
| `ERP_API_KEY` | Rotated (value never displayed) |
| `ERP_STORAGE_HOT_LOCATION` | **`external`** (new) |
| `ERP_STORAGE_WARM_LOCATION` | **`external`** (new) |
| `ERP_STORAGE_COLD_LOCATION` | **`external`** (new) |
| Resources created | **NONE** — same five |
| PostgreSQL / Qdrant collections / COLD share | **Unchanged** |

---

## Post-deployment verification

| Check | Result |
|---|---|
| `/docs` | 200 |
| `/v1/health/live` · `/v1/health/ready` | 200 · `ready: true` |
| Dependencies | postgresql ✓ · job_store ✓ · embedding_model ✓ · cold_archive ✓ · vector_storage `hot=up, warm=up, cold=up` |
| Auth: missing / old / wrong / new | 401 / 401 / 401 / **200** |
| HTTPS | `httpsOnly=true`; HTTP → 301 |
| Qdrant | 86 cloud requests, **0 localhost** |
| Payload indexes | 13 on HOT, 13 on WARM |
| Filtered search | 1 hit, model `all-MiniLM-L6-v2`, tiers `[hot, warm]` |
| Non-restricted indexing | **succeeded**, `vectors_stored=1` |
| Restricted indexing | **partial**, `vectors_stored=0`, `vectors_failed=1` |
| New restricted points | **0** |
| OCR / embedding | operational — capabilities report all enabled |

---

## Remaining limitations

**Unchanged from the audit** (not in this remediation's scope): collection
responses adapt the first record only · business-payload content is not
secret-scanned · CSV indexes the schema, not rows · search returns no text ·
exact-match filters only · polling not CDC · remote assets ship disabled ·
schema Recall@1 0.727 · cold start 2–3 minutes · synthetic evaluation corpora.

**New consequence of this remediation, by design:**

> **Restricted data cannot be indexed on the current Azure deployment.** There
> is no on-premises tier, and the policy requires one. This is the correct
> outcome, not a regression — previously it was stored in cloud while the policy
> claimed otherwise. To index restricted data, either provide an on-premises
> tier and declare it `on_premises`, or make a documented, deliberate decision to
> relax `on_premises_only_sensitivities`.

**Still outstanding:**

1. CORS needs the production frontend origin.
2. The frontend must stand up a trusted backend before browser use.
3. ~~The one pre-existing restricted Qdrant point awaits deletion approval.~~
   **Closed 2026-08-29** — deleted on approval. Restricted points in cloud: **0**. The authoritative PostgreSQL representation was preserved and was not re-indexed anywhere.
