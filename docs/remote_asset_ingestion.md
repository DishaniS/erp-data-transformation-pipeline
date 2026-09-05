# Phase 8 — ERP Document URL / Remote Asset Processing

**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline for Legacy ERP Systems
**Student:** IT22267290 · Member 4 · R26-SE-034
**Status:** implemented and verified
**Date:** 2026-08-25

---

## 1. Problem

Phase 3 indexes a certificate stored as `employees.birth_certificate` (BYTEA).
A great many real ERPs store a **reference** instead:

```
employees.birth_certificate_url = "https://storage.example/EMP002-cert.pdf"
```

Structurally the two are the same document. Before this phase one was fully
retrievable and the other was an opaque string.

## 2. Member 4 / Member 2 boundary

This phase adds **static asset retrieval**, not ERP API execution.

| Member 4 may | Member 4 must not |
|---|---|
| fetch a document the ERP DATA explicitly points at | select or call ERP business APIs |
| validate, bound and classify those bytes | authenticate as Member 2 or hold ERP credentials |
| index the extracted text | execute business operations or MCP tools |
| | crawl, follow hyperlinks, or search the web |
| | act as an HTTP proxy or perform writes |

The distinction is that the URL is **data already in the row**, not an endpoint
this component chose. No `Authorization`, `Cookie` or ERP session token is ever
sent; if a signed URL carries access in its query string it is used for the
single fetch and then redacted from everything (§19).

## 3. Existing Phase 14 security components reused

`response_adaptation.assets` already implemented all of it, and Phase 8 uses it
rather than writing a second policy — a duplicate would be a second thing to
keep correct, and the weaker of the two becomes the one an attacker uses.

Reused unchanged: `UrlSafetyPolicy`, `validate_asset_url`, `fetch_asset`,
`_is_forbidden_address`, `default_resolver`, the `Fetcher`/`Resolver` injection
seams, and the named refusal rules.

**Not reused:** Phase 14's `_temp_file` extraction path. Phase 8 uses Phase 3's
in-memory route instead (§13).

## 4. Asset-field declaration design

A job option, following the existing `key_fields` pattern:

```json
{
  "job_type": "source_native_pipeline",
  "options": {
    "key_fields": ["employee_id"],
    "asset_url_fields": ["birth_certificate_url"]
  }
}
```

or, with an explicit document type:

```json
"asset_url_fields": {
  "birth_certificate_url": {"document_type": "birth_certificate"}
}
```

A bare string and a `field: "type"` shorthand are both accepted. Nothing else in
the row is fetchable, whatever it is named or contains.

## 5. Why URLs are not guessed

Two tempting rules, both rejected:

**Scanning values for `http`.** That turns an ordinary `website`, `notes` or
`comment` column into outbound traffic chosen by whoever wrote the row — the
SSRF position, reached through the database rather than through a request
parameter.

**Trusting column names.** `birth_certificate_url` is a naming convention, not
an authorisation. A rule keyed on `_url` would fetch `internal_admin_url` in
the same breath.

`test_an_undeclared_url_field_is_never_fetched` and
`test_a_column_named_url_is_not_enough_on_its_own` assert the fetcher is never
called.

## 6. Secure-by-default configuration

`UrlSafetyPolicy.enabled` defaults to **False**, and `PipelineServices` defaults
both `remote_asset_policy` and `remote_asset_fetcher` to `None`.

Three independent conditions must all be satisfied before a request happens:

1. a policy that is enabled,
2. a fetcher supplied by the deployment,
3. a field explicitly declared in the job.

Miss any one and the outcome is `remote_fetch_disabled`. The package ships **no
HTTP client at all** — `test_the_package_ships_no_http_client` asserts the
module imports none — so importing this code cannot cause a request even by
accident.

## 7. SSRF policy

Enforced **before a socket opens**, by Phase 14's validator:

```
scheme        https only by default; file/ftp/gopher/data/javascript/s3 refused
credentials   user:pass@host refused outright
host          must be present; optional strict allow-list
port          allow-list (80/443/8080/8443)
DNS           EVERY resolved address checked, not just the first
addresses     loopback, RFC1918, link-local, multicast, reserved, unspecified,
              and the IPv4-mapped IPv6 forms of all of them
size          ceiling enforced after fetch
redirects     bounded and re-validated
```

## 8. DNS / IP protection

The negative tests use **https** URLs whose *hostnames resolve* to forbidden
addresses, so they exercise the address check rather than being masked by the
scheme check. Each asserts `fetcher.called is False` — a refusal that still
opened a socket is not a refusal:

```
loopback v4 · loopback v6 · 169.254.169.254 · 10/8 · 192.168/16 · 172.16/12
link-local v6 · ::ffff:127.0.0.1 · 0.0.0.0        all refused, 0 sockets
```

A host resolving to **one public and one loopback** address is refused too — the
DNS-rebinding shape. A public address is permitted, so the negative tests cannot
pass by refusing everything.

**Boundary stated honestly:** validation resolves the hostname and checks the
addresses, then the injected fetcher connects. Between those two steps a DNS
entry could change (classic TOCTOU rebinding). Closing that requires pinning the
connection to a validated IP, which is a property of the *fetcher
implementation* a deployment supplies, not of this contract. Phase 8 does not
claim to have closed it.

## 9. Redirect protection

Every redirect target is re-validated against the full policy — an allowed host
redirecting to `169.254.169.254` is the standard bypass. A redirect when
`max_redirects` is 0 is refused outright, and the limit bounds loops.

## 10. Fetch budgets

Timeout, redirect count and `max_bytes` come from the policy. Oversized
responses are refused with `response_too_large` and produce no representation.
`Content-Length` is not trusted on its own: the body's actual length is checked.

## 11. MIME versus magic bytes

**The bytes decide.** The server's `Content-Type` is recorded as provenance and
used for exactly one thing: refusing content that is not an asset at all (§22).

| declared | actual | routed as |
|---|---|---|
| `image/jpeg` | PDF | **PDF** |
| `application/octet-stream` | PDF | **PDF** |
| `application/pdf` | ZIP | **unsupported** |
| `text/html` | HTML | **unsupported, before classification** |

## 12–13. Reuse and in-memory processing

After the fetch the origin stops mattering. The bytes go to Phase 3's
`extract_binary_asset`, which uses `FileSource.payload` — the in-memory route
Phase 3 introduced precisely so employee certificates are never written to
`%TEMP%` in plaintext. **No temporary files.** The result is a
`BinaryAssetResult`, the same type a BLOB produces, so detection, extraction,
OCR, chunking, attachment identity, persistence, embedding, storage and search
are literally the same code.

## 14–15. ERP parent and attachment identity

Identical to a BLOB attachment: `parent_record_id`, `business_key_name`,
`business_key_value`, `source_system_id`, `source_entity`, `source_field`,
`document_type`.

Content identity is the hash of the fetched **bytes**. Attachment identity is
Phase 3's `parent | source_field | chunk`. **The URL is not part of either.**

## 16. Same URL, different employees

```
EMP002.birth_certificate_url = URL X   ┐ same document_id
EMP003.birth_certificate_url = URL X   ┘ different representation_id, vector_id
```

Measured: **0 association collisions**, and the EMP002 filter never returns
EMP003's certificate.

## 17. URL changed, same content

`https://a/…` moved to `https://b/moved.pdf` serving identical bytes gives the
**same** `document_id` and the **same** representation — one employee, one
field, one attachment. The provenance records that the host changed. No
duplicate corpus entry.

## 18. Same URL, changed content

`document_id` is the content hash, **never** a hash of the URL, so re-fetching a
stable URL whose contents were amended produces new content identity and the new
text becomes searchable. The superseded vector is **not** removed — that is
Phase 9's job, and is recorded as a limitation.

## 19. URL redaction and provenance

An ERP asset URL is frequently a bearer credential:

```
https://storage.example/cert.pdf?token=SECRET&expires=1735689600
```

What is stored:

```
asset_origin           remote_url
source_url_scheme      https
source_url_host        storage.example
source_url_path        /cert.pdf
url_reference_hash     <sha256 of the FULL url, for correlation>
declared_media_type    application/pdf
detected_media_type    application/pdf
source_url_redirected  false
```

The query string is dropped **entirely** rather than trimmed — a truncated token
is still a leaked prefix. Embedded credentials are removed. A client exception's
message is never propagated (it can quote the URL); only a safe label survives,
and the refusal rule name says which policy stopped it.

Verified across success, refusal, oversize, timeout and disabled paths, in
representation text, metadata, asset reports, warnings, canonical records, the
API surface and the vector payload: **secret URL leakage = 0**.

## 20. Failure and partial success

Distinct, named outcomes: `remote_fetch_disabled`, `invalid_url`,
`not_a_url_value`, `remote_fetch_refused`, `remote_fetch_failed`,
`response_too_large`, plus Phase 3's `unsupported_binary`, `unreadable`, `empty`.

Every one leaves the scalar record intact. A refused certificate is one field of
one row; the employee's name and department are still perfectly good data, and
failing the job over it would be a worse answer than reporting it. Nothing is
fabricated — a failed fetch produces no representation and no vector.

`null`, `""` and whitespace mean *no asset*, which is a fact rather than a
failure. A number, list or dict is refused as a type mismatch rather than
stringified into a request.

## 21–22. Both structured paths; schema excluded

Remote assets belong to the **source record**, so they work on both the
source-native and structured paths exactly as BLOBs do — the declaration is a
job option, not a property of how scalars are mapped.

`SCHEMA_PIPELINE` **never fetches**. A schema describes the *field*
`birth_certificate_url`; there is no row value to retrieve.
`test_a_schema_job_never_fetches_a_url_field` asserts the schema text contains
the field definition and the fetcher was never called.

Phase 6 uploads already hold bytes and do not go near this branch.

## 23. Asset URLs stay out of scalar AI text

A declared asset URL is a **pointer**, not content. Its literal value adds
nothing an embedding can use and may carry a signed token, so it is excluded
from the scalar `text_for_ai` exactly as binary bytes are, while the field stays
recorded as structure.

Only **declared** fields get this treatment — an undeclared `website` remains
ordinary scalar content.

## 24. Files changed

**New (4):** `ingestion/remote_assets.py`,
`scripts/evaluate_remote_asset_security.py`, two test files, this report.

**Modified (6):** `orchestration/multimodal.py` (one loop, two attachment
kinds), `orchestration/stages.py`, `orchestration/service.py`,
`orchestration/models.py` (one counter), `transformation/source_native.py`
(scalar exclusion).

`extract_record_assets` was refactored so the per-field body is one helper both
paths call, rather than forty duplicated lines.

## 25. Tests added

| file | tests |
|---|---|
| `tests/erp_pipeline/ingestion/test_remote_asset_ingestion.py` | 68 |
| `tests/erp_pipeline/api/test_remote_asset_pipeline.py` | 22 |

No test opens a socket. The recording fetcher is the instrument: refusal tests
assert it was **never called**, not merely that the result said no.

## 26. Mini-evaluation

22 cases: successful PDF/image/octet-stream/lying-MIME/signed URLs, a shared
URL across two employees, the same content behind two URLs, changed content
behind one URL, and refusals for private IP, cloud metadata, http, credentials,
disabled fetching, a non-URL, an undeclared field, a redirect to private,
oversize, timeout, 404, HTML and ZIP.

```
remote references attempted        21
requests permitted                 15
refused before any contact          6
successful extractions              9
representations indexed             9

private/internal targets contacted  0
wrong employee matches              0
association collisions              0
secret URL leakage                  0
raw binary / base64 leakage         0
HTML pages indexed                  0
raw URL in vector payload           False

policy validation  median 0.087 ms
fetch + extract    median 1.774 ms   p95 6.514 ms

GATES: PASS
```

Artifact: `artifacts/remote_asset_security_evaluation.json`.

### A measurement defect I fixed in my own evaluation

The first run reported **2 association collisions** and failed the gate. It was
the metric, not the pipeline.

I had counted `len(vector_ids) - len(set(vector_ids))`. EMP002's certificate
reaches this corpus three times — a plain URL, a shared URL and a moved URL, all
serving identical bytes — and all three *should* resolve to one representation:
same employee, same field, same content is one attachment, which is exactly the
idempotency the moved-URL case exists to demonstrate.

An association collision has meant one thing since Phase 3: **one vector claimed
by two different parents**. The metric now measures that, and the number is 0.
The implementation was never changed to produce it.

**On the timings:** the fetcher is injected and no socket is opened, so these
are in-process validation and extraction costs only. **No internet round trip is
included or simulated**, and calling them network latency would be false.

## 27. Targeted results

`ingestion`, `response_adaptation`, `api`, `transformation`, `orchestration`,
`storage`, `ai`, `sync`, `runtime`:

```
1890 passed, 39 skipped, 0 failed in 372.16s
```

## 28. Full regression

| | collected | passed | failed | errors | skipped |
|---|---|---|---|---|---|
| baseline (after Phase 7) | 3408 | 3345 | 0 | 0 | 63 |
| after Phase 8 | **3500** | **3437** | **0** | **0** | **63** |

`3437 passed, 63 skipped, 30 warnings in 350.22s (0:05:50)`

The **+92** is fully accounted for:

- **+90** new tests (68 + 22, §25)
- **+2** automatically parametrized. `test_no_production_module_writes_to_the_
  filesystem` and `test_files_are_only_ever_opened_for_reading` iterate over
  every production module in the ingestion package, so `remote_assets.py` came
  under the read-only invariant without anyone having to remember to add it —
  and **passed**, which is an independent confirmation of §13: the remote path
  writes no temporary files. The same mechanism caught Phase 3's temp-file
  implementation when it did.

**Skips are unchanged at 63.** No test was skipped to avoid a failure, and no
test in this phase requires a network — every fetch is injected.

## 29. Existing artifact impact

| artifact | status |
|---|---|
| `tiered_storage_benchmark.json` | unchanged |
| `response_adaptation_evaluation.json` | unchanged |
| `multimodal_extraction_evaluation.json` | unchanged |
| `identity_retrieval_evaluation.json` | unchanged |
| `representation_resolution_evaluation.json` | unchanged |
| `automatic_document_indexing_evaluation.json` | unchanged |
| `schema_retrieval_evaluation.json` | unchanged — the corpus was **not** touched to improve its datatype retrieval |
| `remote_asset_security_evaluation.json` | **new** |

## 30. Known limitations

1. **Fetching is disabled by default** and requires a policy, a fetcher and an
   explicit field declaration.
2. **No production HTTP client ships with this package.** A deployment supplies
   one; its quality (connection pinning, proxy behaviour, TLS verification) is a
   deployment concern.
3. **DNS TOCTOU is not closed** (§8). Validation resolves and checks; the
   fetcher connects. Pinning the socket to a validated address belongs to the
   fetcher implementation.
4. **Authenticated ERP URLs are unsupported.** No `Authorization`, cookie or
   session token is sent, by design.
5. **Relative URLs are unsupported.** `/documents/cert.pdf` is refused rather
   than resolved against a guessed host.
6. **No HTML, no crawling, no web search.**
7. **Stale vectors remain** after remote content changes, until Phase 9.
8. **A signed URL may expire** before a later re-index, which will then fail —
   correctly, and visibly.
9. **Remote availability affects indexing**, and there is no guarantee the asset
   is still identical to what was indexed.
10. **No CDC or automatic refresh.**
11. **Outbound network policy remains a deployment concern** — egress
    filtering complements this, it is not replaced by it.
12. **Phase 14's own asset path still writes temp files.** Out of scope here and
    deliberately untouched, but it is the same concern Phase 3 fixed and is
    worth a future pass.

## 31. Explicit Phase 9+ exclusions

Confirmed absent:

```
incremental-sync scheduler / continuous polling / CDC
stale document or vector cleanup
new sensitivity inference or PII classification
frontend URL configuration, search or schema UI
Member 1 / Member 2 integration
LLM answer generation
```

**No new endpoint. No new JobType. No new Qdrant collection. No new filter
field** — remote origin is provenance, and the existing 13 filters already scope
it.

## 32. EMP002 readiness

```
employees.birth_certificate      = BLOB        WORKING   (Phase 3)
uploaded EMP002_certificate.pdf                WORKING   (Phase 6)
employees.birth_certificate_url  = https://…   WORKING   (Phase 8,
                                                          when enabled and the
                                                          URL passes policy)
```

```
Legacy ERP → EMP002 → birth_certificate_url
  → policy validation → bounded fetch → magic-byte detection
  → extraction / OCR → chunking → representation persistence
  → embedding → Qdrant → EMP002 exact search → certificate text
```

---

*See also: [Phase 7 — Schema Vector Retrieval](schema_vector_retrieval.md),
[Phase 3 — Database BLOB Multimodal Pipeline](database_blob_multimodal_pipeline.md).*
