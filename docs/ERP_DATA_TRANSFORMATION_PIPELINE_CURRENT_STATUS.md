# ERP Data Transformation Pipeline — Current Status

**Audited:** 2026-08-29 · read-only · nothing modified

---

## Completion status

| Area | Status |
|---|---|
| Data discovery, transformation, mapping | **COMPLETE** |
| Multimodal preparation (BLOB, PDF, image, OCR) | **COMPLETE** |
| Schema representation and indexing | **COMPLETE** |
| Embeddings (384-D, local, no LLM) | **COMPLETE** |
| Qdrant HOT / WARM | **COMPLETE** |
| COLD encrypted archive | **COMPLETE** |
| Search + representation resolution | **COMPLETE** |
| Response adaptation | **COMPLETE** |
| Sensitivity metadata and encryption | **COMPLETE** |
| Synchronisation / lifecycle | **PARTIAL by design** — polling, not CDC |
| Remote asset ingestion | **CONFIGURATION-DEPENDENT** — ships disabled |
| API + OpenAPI/Swagger auth | **COMPLETE** |
| Azure deployment | **DEPLOYED AND VERIFIED** |
| Research evaluation | **COMPLETE WITH LIMITATIONS** |

---

## Deployment status

| | |
|---|---|
| Base URL | `https://erp-data-transformation-api-ju0h8k.azurewebsites.net` |
| Swagger | `.../docs` — Authorize button working |
| Region | Southeast Asia |
| Resource group | `rg-erp-data-transformation` |
| Image | `erp-data-transformation-api:v2` on App Service B1 Linux |
| PostgreSQL | Flexible Server B1ms, PG 16, TLS required, Azure-services-only firewall |
| Qdrant | Qdrant Cloud — `erp_vectors_hot`, `erp_vectors_warm`, 13 payload indexes each |
| COLD | Azure Files share mounted at `/mnt/erp-cold` |

**Verified live:** 42 Qdrant Cloud requests / **0 to localhost** · OCR
`ocr_used: true` · PostgreSQL `ready` with 3 jobs persisted across restart ·
auth 401/401/200 · Swagger shows 22 padlocked operations and 2 public.

---

## Test status

```
collected : 3762
passed    : 3699
failed    : 0
errors    : 0
skipped   : 63
warnings  : 30
duration  : 640.94s (10:40)
```

Frontend: **26 passed** (2 files, vitest).

All 63 skips are local infrastructure availability (37 Qdrant, 24 MongoDB,
1 live discovery, 1 live pipeline stage). None hides a failure.

---

## Known limitations

**Three open findings**

1. **Storage-location policy is stale** — `DEFAULT_TIER_LOCATIONS` declares
   HOT/WARM/COLD `ON_PREMISES`, but HOT and WARM are Qdrant Cloud. The
   `on_premises_only_sensitivities = {RESTRICTED}` constraint is genuinely
   enforced but excludes nothing, so restricted vectors and their identity
   metadata reside in third-party cloud. Text is **not** exposed (it stays
   encrypted in PostgreSQL). `tier_locations` has no environment variable.
2. **Qdrant payload indexes are not created by code** — the 13 keyword indexes
   per collection were created operationally. `ensure_collection(recreate=True)`
   would drop them and break filtered search with `400`.
3. **`httpsOnly: false`** on the App Service — HTTP is not redirected, so a
   client using `http://` would send its API key in cleartext.

**Design limitations** (documented, not defects)

- Collection responses adapt the **first record only**, with a warning.
- Business-payload content is **not** secret-scanned (transport metadata is).
- CSV upload indexes the **schema**, never the rows; rows need `key_fields`.
- Search returns **no text** — resolve separately.
- **Exact-match filters only** — no ranges, OR or negation.
- Sync is **polling, not CDC**; hard-delete observability is connector-dependent.
- Remote assets ship **disabled**; unchanged URLs are not re-fetched.
- Schema Recall@1 = 0.727; datatype-vocabulary queries measurably weaker.
- Cold start ~2–3 minutes on B1 (`alwaysOn: false`).
- Evaluation corpora are **synthetic, small, single-annotator**; no significance
  testing; no downstream LLM answer-quality study; other components are
  **fakes** in tests.

---

## Remaining integration work

| # | Item | Owner | Blocking? |
|---|---|---|---|
| 1 | Provide the production frontend origin so CORS can be extended | Frontend | **Yes**, for browser use |
| 2 | Stand up a trusted backend/BFF to hold the API key | Frontend | **Yes**, for browser use |
| 3 | Decide on FINDING 1 before indexing real restricted data | Governance + owner | **Yes**, for restricted data |
| 4 | Set `httpsOnly: true` | Owner | Recommended |
| 5 | Make payload-index creation durable in `ensure_collection` | Owner | Recommended |
| 6 | Confirm whether ERPBridge needs multi-record adaptation | ERPBridge | Only if lists are used |
| 7 | Enable `alwaysOn` or accept cold starts | Owner | Optional |

---

## What another team needs before integrating

1. **The base URL** — `https://erp-data-transformation-api-ju0h8k.azurewebsites.net`
2. **The `X-API-Key` value** — delivered out of band, never in a repository,
   never in browser code
3. **Their production origin registered** in `ERP_API_CORS_ORIGINS` (browser
   callers only)
4. **[`ERP_DATA_TRANSFORMATION_PIPELINE_INTEGRATION_GUIDE.md`](ERP_DATA_TRANSFORMATION_PIPELINE_INTEGRATION_GUIDE.md)** — role-specific instructions
5. **[`ERP_DATA_TRANSFORMATION_PIPELINE_API_HANDOFF.md`](ERP_DATA_TRANSFORMATION_PIPELINE_API_HANDOFF.md)** — every endpoint with examples
6. **Awareness of four behaviours**: `POST /v1/jobs` returns **202**; search
   returns **no text**; CSV indexes the **schema only**; binary adaptation puts
   text in **`assets[0].text`**

---

## Verdict

**YES, WITH CONDITIONS.**

The service is deployed, verified end-to-end, and its contracts are stable and
tested. ERPBridge and Frontend can begin integrating immediately against the
deployed URL.

The conditions are:

1. **Restricted production data must not be indexed** until FINDING 1 is
   resolved or formally accepted in writing. Non-restricted data is unaffected.
2. **The frontend must not hold the API key in browser code.** A trusted backend
   is required before any browser-facing use.
3. **CORS must be extended** with the real frontend origin.
4. **Do not recreate the Qdrant collections** without recreating the 13 payload
   indexes, or filtered search will fail.
