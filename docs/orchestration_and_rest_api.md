# Orchestration and REST API (Phase 13)

## 1. Purpose

Phase 13 is the **control plane** around the research pipeline, not a new part
of it. It decides *which* existing phase runs, *in what order*, and records
*what happened*. It contains no discovery, mapping, transformation, validation,
chunking, embedding or tier-routing logic of its own.

The test that matters: if a route handler looks like it is computing something
about ERP data, the phase boundary has been broken.

## 2. Architecture

```
        client
          |
          v
      FastAPI /v1          validate, authenticate, shape the response
          |
          v
   OrchestrationService    plan -> persist -> enqueue -> 202
          |
          v
      JobExecutor          bounded worker pool
          |
          v
     PipelineRunner        stage by stage, halting on failure
          |
   +------+---------------------------+
   v                                  v
existing phase services           JobStore
(3-12)                            durable status + stage history
```

The orchestration package imports no web framework, so the entire pipeline can
be driven from a script or a test without starting a server.

## 3. API and versioning

Every domain route lives under `/v1`. There is no unversioned parallel API.
`/docs`, `/redoc` and `/openapi.json` are served by FastAPI itself.

## 4. Source registration

`POST /v1/sources` stores **structural** connection metadata: type, host, port,
database, username. `GET /v1/sources` and `GET /v1/sources/{id}` read it back.

## 5. Credentials

Phase 3 established that `ConnectionSettings` are runtime-only. Phase 13
preserves that.

A registered source carries a `credential_ref` — a **name**. At connect time the
name is handed to a `SecretProvider`, the secret is used to open a connection,
and it is discarded with it.

- A caller may supply `password` once at registration. It is typed `SecretStr`,
  moved straight into the secret provider, and never stored on the source.
- `RegisteredSource` has **no password field**, so there is nothing to forget to
  exclude from a response.
- Free-form `metadata` is filtered on write: keys that look like credentials
  (`password`, `token`, `api_key`, `dsn`, `uri`, …) are dropped.
- Providers redact themselves in `__repr__`, because keys leak through logs more
  often than through files.

Implementations: `EnvironmentSecretProvider` (reads `ERP_SECRET_<REF>`),
`InMemorySecretProvider` (tests and demos), `NullSecretProvider` (default).
This is not a vault, and does not pretend to be one.

## 6. Connection test

`POST /v1/sources/{id}/test` opens a Phase 3 connector, calls
`test_connection()`, and returns a sanitized result. A failed connection is a
**result, not a 500** — and only the exception *type* is reported, because the
message can embed a DSN.

## 7. Discovery

`POST /v1/sources/{id}/discover` delegates to Phase 4 (relational) or Phase 5
(MongoDB) and publishes to the Phase 2 catalog when one is configured. It
returns identifiers and counts — never sampled rows.

## 8-9. Uploads

| Endpoint | Phase | Returns |
|---|---|---|
| `POST /v1/files/csv` | 6 | schema id, columns, rows observed |
| `POST /v1/files/documents` | 6 | page count, extraction status, hash |
| `POST /v1/api-specs/openapi` | 7 | operations count, `endpoints_called: 0` |
| `POST /v1/api-specs/postman` | 7 | structural metadata only |

None of them echo content. A CSV endpoint that replayed rows would be an
accidental data-export endpoint; a document endpoint that returned text would
return the whole document.

**Upload safety.** Files stream to disk in 1 MiB chunks and abort the moment the
configured cap is exceeded (`413`), so an oversized upload cannot exhaust
memory. Each upload gets its own **generated directory**; the sanitized filename
sits inside it. The directory name is what guarantees isolation, so the filename
can be kept — which matters, because schema inference derives the entity name
from it. Absolute paths are never returned.

## 10. Schemas

`GET /v1/schemas/{id}` returns the stored `SourceSchema`. It never rebuilds one.

## 11. Mappings

`POST /v1/mappings/suggest` calls Phase 8 and returns coverage plus per-field
explainability.

**Ambiguity is never auto-approved.** When Phase 8 finds ambiguous fields it
emits no executable profile. Phase 13 still gives the mapping an id so a human
can address it, but files it as a **draft** — and `get_mapping_profile` refuses
to hand a draft to a pipeline (`409 MAPPING_REQUIRES_REVIEW`). The response
carries the tied candidates and their scores so the caller can actually decide.

`PUT /v1/mappings/{id}` feeds those decisions back **through Phase 8** as
`MappingOverride`s rather than patching the profile, so the engine's own
validation still applies: a target the canonical model does not define is
refused by the engine that owns the model.

`POST /v1/mappings/{id}/validate` reports executability and moves no data.

## 12-15. Jobs, stages and the planner

Job statuses: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `PARTIAL`,
`INTERRUPTED`.
Stage statuses: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `SKIPPED`,
`NOT_APPLICABLE`.

The planner computes the stage graph from the source's **capabilities**, before
any work starts:

```
structured   DISCOVER -> MAP -> EXTRACT -> TRANSFORM -> VALIDATE
             -> LOAD -> AI_BUILD -> EMBED -> TIER_ROUTE

structured   MAP -> EXTRACT -> ... -> TIER_ROUTE
(CSV)        DISCOVER is NOT_APPLICABLE: the schema arrived with the upload

document     INGEST -> AI_BUILD -> EMBED -> TIER_ROUTE
             MAP and TRANSFORM are NOT_APPLICABLE: a document has no columns

incremental  DRIFT_CHECK -> EXTRACT_CHANGED -> TRANSFORM -> VALIDATE
             -> LOAD -> AI_BUILD -> EMBED -> TIER_UPDATE

api spec     PARSE_SPEC -> SCHEMA -> MAP
             EXTRACT is NOT_APPLICABLE: the endpoints are never called
```

Asking an OpenAPI source for records is refused **synchronously** with
`422 UNSUPPORTED_CAPABILITY`; no job row is created for an impossible request.

`NOT_APPLICABLE` is recorded explicitly so a reader can tell "this pipeline has
no MAP stage" from "MAP never ran".

### VALIDATE does not re-validate

Phase 9 validates during transformation. The `VALIDATE` stage exists because the
public contract lists it and operators want quality reported separately from
conversion — it **interprets Phase 9's outcome**. A second validator here could
disagree with the one that actually gated the data.

## 16-17. Job persistence

`JobStore` has two implementations: `InMemoryJobStore` (tests) and
`PostgresJobStore` (real use), in schema `erp_orchestration` with tables `jobs`
and `job_stages`. The schema name is parameterised so tests bootstrap a
throwaway namespace instead of creating production tables.

Job rows hold identifiers, statuses, counts and timings — everything persisted
is something you could paste into a bug report.

## 18. Partial and failure semantics

When a stage fails, later stages are marked `SKIPPED` and the job is `FAILED`.
Nothing downstream runs — a pipeline that carried on past a failed `TRANSFORM`
would embed records that never transformed, which looks like success.

`PARTIAL` is used when work completed but records were dropped. Reporting
`SUCCEEDED` while five records were rejected would hide exactly what an operator
needs to see. If Phase 9's configured quality threshold is breached, the job is
`FAILED`, not `PARTIAL`.

### Restart policy

On startup, jobs left `RUNNING` by a dead process are marked **`INTERRUPTED`**,
and the stage that was mid-flight is marked `FAILED`. They are never silently
promoted to succeeded, and never left `RUNNING` forever. Stages that genuinely
finished are preserved.

### Retry

`POST /v1/jobs/{id}/retry` re-submits a failed or interrupted job as a **new**
job. `INCREMENTAL_SYNC` is excluded: Phase 10 advances watermark state as it
runs, so a generic replay could reprocess or skip changes.

### Counters

`records_read`, `records_transformed`, `records_failed`, `records_skipped`,
`representations_built`, `embeddings_generated`, `embeddings_skipped`,
`vectors_stored`, `vectors_failed`, `chunks_built`, `operations_parsed`.

A counter no phase service reported is **absent**, never a confident `0`.

## 19-20. Incremental sync and drift

Both delegate to Phase 10. Phase 13 adds no watermark, checkpoint, drift or
content-hash logic; it triggers the run and surfaces the metrics.

## 21-22. Embeddings and storage

`AI_BUILD` uses Phase 11's representation builder and chunker. `EMBED` uses
`EmbeddingService` — orchestration never touches `SentenceTransformer` directly.
`TIER_ROUTE` hands each embedding to Phase 12 with routing metadata and lets it
choose; the words HOT, WARM and COLD never appear as a decision in orchestration
code.

## 23-24. Search

`POST /v1/search` embeds the query with Phase 11 and retrieves with Phase 12's
`HybridVectorStore`. **No LLM, no generated answer** — it returns retrieved
records, not prose about them.

**No vector is ever returned.** A search endpoint that returned embeddings would
be an embedding-export endpoint; a test asserts no long float array appears in
any response body.

`include_cold` defaults to `false` because cold search rehydrates archives. When
enabled, the response sets `deep_search_used` and explains the cost rather than
hiding it.

## 25. Records

`GET /v1/records/{id}` returns the canonical record's own serialization
(`to_json_dict`) — Phase 13 does not invent a second representation. The payload
carries business values; no vector, no credential.

## 26. Health and capabilities

- `GET /v1/health/live` — is the process alive? Checks **nothing external**. If
  liveness depended on Qdrant, a vector-database outage would get the API killed
  and restarted, which fixes nothing and loses in-flight jobs.
- `GET /v1/health/ready` — are the **configured** dependencies usable? A
  deployment with no vector store is not unready; it simply cannot serve search.
- `GET /v1/capabilities` — source types, file types, spec formats, job types,
  tiers, model id and dimension, plus explicit **limitations**, including that
  SQL Server live verification remains deferred.

## 27. Error contract

```json
{
  "success": false,
  "error": {
    "code": "SOURCE_NOT_FOUND",
    "message": "Source was not found.",
    "request_id": "…"
  }
}
```

| Status | When |
|---|---|
| 400 | unsafe upload name |
| 401 | missing or wrong API key |
| 404 | source, schema, mapping, job, record or upload not found |
| 409 | idempotency conflict, mapping needs review, retry not supported |
| 413 | upload too large |
| 415 | unsupported upload type |
| 422 | invalid payload, unsupported capability |
| 503 | secret or dependency unavailable |

Not everything is a 500. An unexpected exception's *text* is never echoed — it
could contain a connection string or a row value — only its type.

## 28. Request ids

Every request carries an `X-Request-ID` (supplied or generated). A random UUID
is right here because a request is an **operational** event. Domain identity
elsewhere in this pipeline is deterministic and must never be derived from it.

## 29. Idempotency

`POST /v1/jobs` accepts an `Idempotency-Key`. The same key with the same payload
returns the existing job; the same key with a different payload is `409`. The
key is never used as domain identity.

## 30-32. Security

- Optional API key (`X-API-Key`), compared in **constant time**, required for
  mutating routes and optionally for reads. Never logged, never echoed, never
  written into the OpenAPI document.
- Health endpoints are always public — a liveness probe needing a credential is
  a probe that pages you for the wrong reason.
- Default bind is **127.0.0.1**. Binding publicly must be deliberate.
- CORS is **closed** by default; never a wildcard with credentials.

Production deployment would require a real gateway and identity provider. This
is a local research API and does not pretend otherwise.

## 33. Logging

Structured context: `request_id`, `job_id`, `stage`, `source_id`, `status`,
`duration`. Never source rows, document text, vectors, passwords or keys.
Credential and upload routes do not log request bodies.

## 34. Generated OpenAPI

`artifacts/phase13_openapi.json` is exported **from the running app**. There is
no second hand-written specification to drift from it. A test asserts every
mandatory route is present, operation ids are unique, and no planted secret
appears anywhere in it.

## 35. Teammate integration boundary

Other components consume this API and nothing deeper. They should depend on
`/v1` routes and `error.code` values, not on the orchestration package.

## 36. Limitations

- Incremental sync requires a configured Phase 10 target and extractor for the
  source; submitting it without one returns a controlled capability error.
- The canonical record store is content-addressed JSON; querying records by
  business attributes is not offered.
- SQL Server support is implemented but live verification remains deferred.
- The executor is an in-process thread pool. It is bounded and honest about it,
  but it is not a distributed queue.
- CSV schema inference takes the entity name from the filename, so a file named
  for its canonical entity (`invoice.csv`) maps cleanly where a generic name
  would not.

## 37. Phase 14 boundary

Phase 13 does **not** do, and Phase 14 may: runtime REST or SOAP ERP endpoint
execution, MCP runtime, RAG answer generation, any LLM integration, or a
frontend. Uploaded API specifications are parsed as contracts and their
endpoints are never called — the API *server* here must not be confused with ERP
API *execution*.
