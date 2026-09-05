# Phase 10 — Security and Sensitivity Hardening

**Component:** ERP-Aware Multimodal Data Transformation, Vector Indexing and Retrieval Pipeline for Legacy ERP Systems
**Student:** IT22267290 · Member 4 · R26-SE-034
**Status:** implemented and verified
**Date:** 2026-08-25

---

## 1. Problem

The pipeline could index an employee's birth certificate. It could not be *told*
that the certificate was sensitive.

`SensitivityLevel` existed with four values and a default of `INTERNAL`, and the
storage policy already constrained `RESTRICTED` — but nothing could set it. No
upload field, no job option, no per-field override. And the extracted text sat
in a plaintext database column, which Phase 5 recorded honestly at the time and
which mattered more once a caller could mark that text restricted.

## 2. Member 1 / Member 4 boundary

| Member 4 owns | Member 1 owns |
|---|---|
| sensitivity metadata and its propagation | end-user authorization |
| sensitivity-aware storage routing | RBAC, roles, approval workflow |
| safe persistence, logging, provenance | *"may this user see EMP002?"* |

**Sensitivity is data-handling metadata, not an authorization decision.** Phase
10 makes `sensitivity = restricted` arrive intact and accurate so a governance
layer can act on it. Nothing here asks who is calling.

A structural test enforces this: the new modules' ASTs contain no `user`,
`role`, `permission`, `rbac`, `authorize`, `approve` or `deny`.

## 3. Existing architecture (audit findings)

| # | question | answer |
|---|---|---|
| 1–2 | values / default | `public, internal, confidential, restricted` / **INTERNAL** |
| 3 | where it enters | only `CanonicalRecord.sensitivity` and a transformer argument |
| 4 | where it disappears | **upload had no input; job options carried none; schema representations carried none at all** |
| 5 | router | `on_premises_only_sensitivities = {RESTRICTED}` — real, but see §11 |
| 6–8 | upload / job / field override | **no, no, no** |
| 9 | text at rest | plaintext `TEXT` column |
| 10 | reusable crypto | AES-256-GCM + random 96-bit nonce in `cold_tier.py`; key-provider pattern parameterised by env var |
| 11 | Phase 14 temp files | yes |
| 12 | `upload_results` | full `ExtractedDocument`, unbounded |

## 4. Explicit sensitivity inputs

Three, all optional, all additive:

```
POST /v1/files/documents      sensitivity=restricted        (multipart form field)

POST /v1/jobs  options:
  "sensitivity": "confidential"                             job-wide
  "field_sensitivity": {"birth_certificate": "restricted"}  per field
  "asset_url_fields": {"…_url": {"sensitivity": "restricted"}}
```

**No content-based guessing.** Nothing infers `birth_certificate → RESTRICTED`
from a column name, a filename, OCR text or a model. A classifier that guessed
right most of the time would guess wrong sometimes, and a wrong classification
is worse than an absent one because it looks authoritative. No PII classifier
was added; that is out of scope by design.

An invalid value (`ultra_secret_magic`) is a **4xx**, not a silently ignored
field that leaves the document at the default.

## 5. Precedence: strictest wins, never a downgrade

No severity ordering existed, and alphabetical would have been actively wrong
(`confidential < internal < public < restricted`). One is now **declared
explicitly** in `schemas/sensitivity.py`, as a tuple rather than relying on enum
declaration order, so reordering the enum for readability cannot silently
reorder security decisions.

When several trusted declarations apply, **the strictest wins** — not the most
specific, not the most recent:

```
source = restricted, field = internal   ->  RESTRICTED
job = public, inherited = confidential  ->  CONFIDENTIAL
field = restricted, job = confidential  ->  RESTRICTED
```

The asymmetry is deliberate. Treating restricted data as internal is a
disclosure; treating internal data as restricted is an inconvenience. Those are
not comparable mistakes, so the tie-break goes to the one that cannot leak.

"Not declared" and "declared public" stay distinguishable, so a missing
configuration never becomes the least restrictive answer.

## 6–10. Propagation

Measured across every content kind, one ERP row carrying several classes at
once:

```
EMP002 structured record         internal     (default, undeclared)
EMP002 birth_certificate         restricted   (field override)
EMP002 profile_photo             confidential
EMP002 employment_contract       confidential
EMP002 remote certificate        restricted
employees schema                 restricted
EMP002 certificate replacement   restricted   (lifecycle replacement)
```

**0 wrong assignments, 0 downgrades, 0 propagation failures.** No cross-field
contamination: updating the certificate's class does not touch the contract's.

**Schema representations carried no sensitivity at all before this phase** — a
gap from Phase 7. They now carry a declared class, and are *not* assumed public:
a table layout discloses what an organisation holds.

## 11. Storage policy enforcement

Audited, not assumed. `StoragePolicy.on_premises_only_sensitivities` contains
`RESTRICTED` and the constraint is genuinely enforced.

**But it is not currently binding**, and saying so matters: `DEFAULT_TIER_LOCATIONS`
places HOT, WARM *and* COLD all `ON_PREMISES`, so no tier is excluded for any
classification today. The constraint would bind the moment a deployment moved
COLD off-premises — which is exactly the configuration the field exists for. The
routing research contribution was not rewritten; no defect was found.

## 12. Lifecycle and sync behaviour

A Phase 9 replacement carries the **new** version's class. A certificate
promoted from `internal` to `restricted` becomes restricted; the obsolete lower
label is not inherited. Tier movement and scheduled sync carry sensitivity
through unchanged — it travels on `StorageRecordMetadata` like every other
identity fact.

## 13–16. Representation-store protection

### The decision

Phase 5 recorded plainly that representation text is not inside the encrypted
COLD archive. That was honest then and became a real gap once a caller could
mark that text restricted.

**Implemented: AES-256-GCM at the application layer for text classified
`CONFIDENTIAL` or above.**

`PUBLIC` and `INTERNAL` are deliberately excluded. Encrypting the whole corpus
would make every existing row unreadable without a key no current deployment
has, turn a missing key into a total outage rather than a contained refusal, and
buy nothing for content already handled as non-sensitive.

### Crypto and keys

Reuses the project's existing primitives — `AESGCM` from `cryptography`, a fresh
`os.urandom` 96-bit nonce per encryption, and the cold tier's key-provider
pattern including its `__repr__` suppression so a key cannot reach a traceback.
No home-grown cryptography; no XOR, base64-as-encryption, ECB or static IV.

**A dedicated key**, `ERP_REPRESENTATION_ENCRYPTION_KEY`, separate from the
cold-archive key: one key for two purposes means rotating it for either reason
forces both, and compromising one context hands over the other.

### Fail closed

If a classification requires encryption and no usable key is configured,
**persistence fails**. There is no plaintext fallback. Because Phase 5 persists
*before* embedding, that failure also means the vector never becomes searchable:
the document is **absent rather than exposed**, which is the correct direction
for a security control to fail in.

A missing key is a *contained* refusal — non-sensitive representations continue
to store and resolve normally.

### Legacy rows

Encryption is marked by an `encv1:` value prefix rather than a separate column,
so a row written before Phase 10 needs **no migration** to stay readable. A
value without the prefix passes straight through. No destructive migration, and
existing plaintext rows are not retroactively encrypted (§28).

### Content hash and the API

`content_hash` still describes the **plaintext** — hashing randomised ciphertext
would destroy content identity. `GET /v1/representations/{id}` returns the same
text whether the row is encrypted or not: **encryption is a storage concern and
callers decrypt nothing.**

Verified: the same plaintext encrypts differently each time; a wrong key or a
tampered ciphertext **fails** rather than returning plausible garbage (GCM
authenticates); a malformed envelope never echoes its contents.

## 17. Qdrant content separation

Unchanged and re-verified. **Qdrant text findings = 0.** Encryption did not
become an excuse to move text anywhere.

## 18. Phase 14 temp-file hardening

Phase 8 found that Phase 14's asset extraction still wrote fetched bytes to a
temporary file — the same concern Phase 3 fixed, in the one place that had not
been revisited. An ERP response asset is a scanned invoice or a signed
certificate, and spilling it to `%TEMP%` puts it on disk in plaintext outside
every control the rest of the pipeline applies.

Both call sites now use Phase 3's in-memory `FileSource.payload`. The temp-file
helpers were **removed** rather than left as a loaded gun someone re-points at a
certificate later.

**A transport change, not a behaviour change** — same extractors, same options,
same output. Verified two ways:

- the module's AST contains no `mkstemp`, `_temp_file` or `NamedTemporaryFile`;
- running the adaptation and counting the temp directory before and after gives
  **0 files**.

**Phase 14's published metrics are byte-identical.** Re-running its evaluation
produced 63 differences, **all of them timing fields and zero non-timing
differences** — recall, irrelevance removal, field reduction, context reduction,
the ablation, and the three documented failures (`po-05`, `proc-02`, `sap-04`)
all unchanged. The artifact was then **restored** to its pre-Phase-10 bytes,
since the brief forbids overwriting it.

## 19. Upload cache hardening

Phase 6 recorded `upload_results` as an unbounded in-process cache; Phase 6 also
made it load-bearing. By Phase 10 that is two problems: it grows without limit,
and what it grows with is extracted document text — every certificate, contract
and payslip the service has seen, in memory indefinitely.

Now a **bounded LRU** (`ERP_UPLOAD_CACHE_MAX_ENTRIES`, default 32) that **cannot
be configured unlimited**: zero or negative is clamped to 1, because honouring
it would restore exactly the behaviour being removed.

It remains an optimisation, never authoritative. An evicted entry costs one
re-extraction from the upload still on disk — which is what happened before the
cache existed — so eviction and restart are ordinary, not failure modes.

## 20–21. Redaction, logging and error safety

Phase 8's URL redaction re-verified: **0 secret leakage** with planted markers
(`SUPER_SECRET_SIGNED_URL_TOKEN`, `TEST_ENCRYPTION_KEY_MARKER`, raw key bytes)
audited across representation metadata, asset reports, warnings and the vector
payload.

Extracted content does not appear in errors: a malformed envelope reports that
it is malformed without quoting it, and a decryption failure says the key is
wrong or the value was altered without echoing either.

## 22. Files changed

**New (4):** `schemas/sensitivity.py`,
`orchestration/representation_crypto.py`,
`scripts/evaluate_security_sensitivity.py`, one test file, this report.

**Modified (8):** `orchestration/document_identity.py` (declared sensitivity +
validation), `api/routers_data.py` (form field),
`orchestration/multimodal.py` (per-field resolution),
`orchestration/stages.py` and `orchestration/service.py` (option plumbing,
bounded cache), `ai/schema_representation.py` (schema sensitivity),
`orchestration/representation_store.py` (encrypt/decrypt),
`response_adaptation/assets.py` (in-memory extraction, temp-file removal).

## 23. Tests added

`tests/erp_pipeline/orchestration/test_sensitivity_and_security.py` — **54
tests**: severity ordering, precedence and no-downgrade, per-field classes,
encryption round-trip, nonce randomisation, wrong-key and tamper refusal,
fail-closed with no key, legacy plaintext compatibility, bounded cache and
eviction, Phase 14 temp-file absence (source *and* measured), and the Member 1
boundary.

## 24. Mini-evaluation

```
assignments attempted              7      restricted plaintext DB findings   0
wrong sensitivity assignments      0      plaintext inside ciphertext        False
silent downgrades                  0      Qdrant text findings               0
propagation failures               0      secret leakage                     0
                                          decryption mismatches              0
Phase 14 temporary plaintext files 0      upload cache max observed          4 / 4

encrypted 6 · plaintext 1
encrypt 0.047 ms · decrypt 0.022 ms   (in-process, NOT database or network latency)

GATES: PASS
```

Artifact: `artifacts/security_sensitivity_evaluation.json`.

## 25. Targeted results

`api`, `orchestration`, `ingestion`, `response_adaptation`, `storage`:
**1175 passed, 27 skipped, 0 failed** after fixing one defect of my own —
the transform stage passed a `sensitivity` argument the service did not accept,
which failed four source-native tests until the service signature was extended.

## 26. Full regression

| | collected | passed | failed | errors | skipped |
|---|---|---|---|---|---|
| baseline (after Phase 9) | 3562 | 3499 | 0 | 0 | 63 |
| after Phase 10 | **3616** | **3553** | **0** | **0** | **63** |

`3553 passed, 63 skipped, 30 warnings in 433.90s (0:07:13)`

The **+54** is exactly the new test file. Nothing was auto-parametrized this
time: the new modules live in `schemas/` and `orchestration/`, outside the
ingestion package whose read-only invariant iterates over production modules.

**Skips are unchanged at 63.** No test was skipped to avoid a failure. In
particular the Phase 14 suite (106 tests) passes unchanged after its extraction
path was moved in-memory.

## 27. Existing artifact impact

All nine prior artifacts unchanged. `response_adaptation_evaluation.json`
was regenerated during verification and then **restored byte-for-byte** from a
backup taken beforehand. Only
`security_sensitivity_evaluation.json` was created.

## 28. Known limitations

1. **Member 4 does not authorize users.** Sensitivity is metadata; the decision
   is Member 1's.
2. **API-key possession remains service-level trust.** There is no per-caller
   identity in this component.
3. **The `RESTRICTED` on-premises constraint is not currently binding** — all
   three tiers are on-premises in the default deployment (§11).
4. **Existing plaintext rows are not retroactively encrypted.** No backfill
   migration was implemented; re-indexing a source encrypts its representations
   as it rewrites them.
5. **Database backups and disk encryption remain deployment controls.**
   Application-layer encryption protects the column value, not the whole estate.
6. **Key rotation is not implemented.** One key, no versioned key identifier in
   the envelope beyond `encryption_version`.
7. **`PUBLIC` and `INTERNAL` text is stored in plaintext**, by the decision in
   §13 rather than by omission.
8. **The upload cache is still ephemeral** — bounded now, but lost on restart,
   which is correct for a cache.
9. **DNS TOCTOU remains a deployment-fetcher responsibility** (Phase 8). No
   custom HTTP stack was built to close a theoretical claim.
10. **Hard-delete observability remains connector-dependent** (Phase 9).
11. **Remote signed URLs may expire** before a later re-index.

## 29. Explicit Phase 11+ exclusions

Confirmed absent: frontend sensitivity configuration, search UI or schema
browser; Member 1 runtime orchestration; Member 2 final integration; group
workflow UI; LLM answer generation; final research benchmark consolidation.

**No new endpoint. No new JobType. No new Qdrant collection. No new search
filter** — the existing `sensitivity` filter already works and was not expanded.

## 30. Final security claims

Claimed, because measured:

> The pipeline supports **explicit sensitivity classification** and propagates it
> end-to-end through AI representation, storage routing, vector metadata,
> synchronisation and retrieval.
>
> **Restricted and confidential AI-ready representation text is encrypted at
> rest** using AES-256-GCM with a per-record random nonce and a dedicated key,
> with no plaintext fallback when the key is absent.

**Not claimed:** HIPAA or GDPR compliance, zero trust, end-to-end encryption, or
that the system is "secure". None of those were established here, and Phase 10
does not establish them.

---

*See also: [Phase 9 — Sync and Lifecycle](near_real_time_sync_and_lifecycle.md),
[Phase 8 — Remote Asset Ingestion](remote_asset_ingestion.md),
[Phase 5 — Representation Content Resolution](representation_content_resolution.md).*
