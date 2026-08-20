# API Specification Ingestion

Phase 7 of the ERP-Aware Data Transformation Pipeline
(SLIIT R26-SE-034, component IT22267290).

## 1. Purpose

Answer one question about a documented ERP API:

> **What structured data does this API expose or accept?**

Not:

> ~~How do we call it?~~

Phase 7 reads Swagger/OpenAPI specifications and Postman collections and turns
the data contracts they describe into the same generic `SourceSchema` that
PostgreSQL, MySQL, SQL Server, MongoDB and CSV produce.

## 2. Scope boundary

This is the sharpest boundary in the project, because the other side of it
belongs to a teammate.

| This component (Phase 7) | Integration / MCP component |
|---|---|
| Reads API documentation | Executes authorized ERP API calls |
| Understands endpoint contracts | REST/SOAP runtime invocation |
| Extracts request/response structures | Access-token acquisition, OAuth flows |
| Infers structure from Postman examples | API-key usage, retries, orchestration |
| Produces `SourceSchema` | MCP tool execution |

**Phase 7 sends no network request, to any endpoint, ever.** Not to a
documented server, and not to fetch a remote `$ref`. This is enforced by
static analysis over the package (§27), not by convention.

## 3. Architecture

```
Swagger / OpenAPI (JSON or YAML)      Postman collection (JSON)
            │                                     │
            ▼                                     ▼
      OpenAPI parser                        Postman parser
            │                              ┌──────┴──────┐
            │                        declared        saved
            │                        structure      examples
            │                              │             │
            │                              │      field inference
            │                              └──────┬──────┘
            ▼                                     ▼
       API contract model  (ApiOperation, ApiParameter, ApiResponse, …)
            │                                     │
            └──────────────────┬──────────────────┘
                               ▼
                         SourceSchema                (Phase 1)
                               │
                               ▼
                    Phase 2 Schema Catalog
```

Module layout:

| Module | Responsibility |
|---|---|
| `service.py` | Public entry point, format dispatch, `SourceSchema` assembly |
| `detection.py` | Format and version detection from document markers |
| `safety.py` | Path/size guards, safe JSON+YAML loading, warning budget |
| `references.py` | `$ref` resolution with cycle, depth and remote guards |
| `schema_conversion.py` | **Pure** OpenAPI Schema Object → `SourceField` tree |
| `inference.py` | JSON example → observed structure (wraps Phase 5's engine) |
| `openapi_parser.py` | OpenAPI 3.0/3.1 and Swagger 2.0 |
| `postman_parser.py` | Postman v2.0/v2.1 |
| `models.py` | Options and the API contract models |
| `errors.py` | The controlled error hierarchy |

The package never imports `bpi2020` — proven by test, including a subprocess
check.

## 4. Supported formats

| Format | Versions | Input |
|---|---|---|
| OpenAPI | 3.0.x, 3.1.x | `.json`, `.yaml`, `.yml` |
| Swagger | 2.0 | `.json`, `.yaml`, `.yml` |
| Postman collection | v2.0, v2.1 | `.json` |

An unsupported version raises `UnsupportedSpecVersionError` naming what was
declared; an unrecognized document raises `UnsupportedSpecFormatError`.
Postman v1 is refused rather than half-supported — its shape is fundamentally
different.

Public API:

```python
from erp_pipeline.api_specs import parse_api_spec, ApiSpecificationService

result = parse_api_spec("erp-openapi.yaml")
result.specification    # title, version, security schemes, variable names
result.operations       # ApiOperation per endpoint
result.schema           # Phase 1 SourceSchema
result.warnings

ApiSpecificationService(options).describe(path)            # detect only
ApiSpecificationService(options).parse_and_publish(path, catalog_service)
```

## 5. Detection

A filename tells you nothing here — teams export `swagger.json`, `api.yaml`
and `collection.json` interchangeably, and a Postman collection is JSON just
like an OpenAPI document. Detection reads the markers each format must
declare:

- `openapi: "3.x.y"` → OpenAPI 3
- `swagger: "2.0"` → Swagger 2
- `info.schema` naming a `getpostman.com` collection schema → Postman
- an `info` + `item` pair → Postman v2 exported without its schema URL,
  recorded as version `2.x` rather than assumed

JSON is attempted before YAML: every JSON document is also valid YAML 1.2, but
the JSON parser is stricter and gives better error positions.

## 6. Operations

Every path and every HTTP method (`GET HEAD POST PUT PATCH DELETE OPTIONS
TRACE`); unrecognized path-item keys such as `summary` or `servers` are ignored
safely. Each operation preserves `path`, `method`, `operationId`, `summary`,
`tags` and `deprecated`.

**Ordering is deterministic**: paths alphabetically, then methods in a fixed
enum order — never the document's key order, which would make a reparse look
like a change.

```
GET    /invoices        listInvoices
POST   /invoices        createInvoice
GET    /invoices/{id}   getInvoice
DELETE /invoices/{id}   cancelInvoice   (deprecated)
```

## 7. Parameters

Path, query, header and cookie parameters, from both the path item and the
operation. Swagger 2's `body` and `formData` locations are handled as their
OpenAPI 3 equivalents.

**Operation-level parameters override path-level ones** with the same
`(name, location)` pair, as the specification requires — getting this backwards
would apply the wrong requiredness or type to an endpoint.

A parameter model has no `value` field. A declared example or default in a
parameter is routinely a real customer id or an API key pasted into the docs.

## 8. Request bodies

OpenAPI 3: `requestBody.content[mediaType].schema`, per media type, with
`required` preserved. Swagger 2: a `body` parameter, plus `formData` fields
assembled into a synthetic object schema so form contracts reach
`SourceSchema` the same way any declared body does.

## 9. Responses

**Every declared status code**, not just `200` — a 4xx problem body is a
contract a consumer must handle. `2xx`, `4xx`, `5xx` and `default` are all
parsed, per media type, and `$ref`'d response objects are resolved.

Multiple content types are not collapsed: `application/json` and
`application/problem+json` produce separate response entries. A `text/plain`
body is recorded with its media type and **no** entity — a scalar contract has
no fields, and minting an empty entity for it would add a meaningless row.

## 10. Component schemas

OpenAPI 3 `components.schemas` and Swagger 2 `definitions` each become one
`SourceEntity` of kind `API_SCHEMA`, carrying object properties, arrays, nested
objects, `required`, nullability, enums, formats, descriptions and
`readOnly`/`writeOnly`.

Type normalization uses the **existing** `FieldDataType`; no competing API type
enum is introduced:

| Declared | `FieldDataType` | `source_data_type` |
|---|---|---|
| `string` | `STRING` | `string` |
| `string` + `date` | `DATE` | `string(date)` |
| `string` + `date-time` | `DATETIME` | `string(date-time)` |
| `string` + `binary`/`byte` | `BINARY` | `string(binary)` |
| `integer` (+`int32`/`int64`) | `INTEGER` | `integer(int64)` |
| `number` (+`float`/`double`) | `DECIMAL` | `number(double)` |
| `boolean` | `BOOLEAN` | `boolean` |
| `object` | `OBJECT` | `object` |
| `array` | `ARRAY` | `array` |
| unrecognized / absent | `UNKNOWN` | preserved verbatim |

OpenAPI 3.1 type arrays are understood: `["string","null"]` is a nullable
string; `["string","integer"]` has no honest common type and is `UNKNOWN`.

Nested paths use the **same vocabulary as Phase 5 and Phase 6** —
`customer.contact.email`, `lines[].sku` — so a path means the same thing
whether it came from a document store, a CSV or an API contract.

## 11. `$ref` resolution

Local pointers (`#/components/schemas/Invoice`, `#/definitions/Customer`) are
resolved from the in-memory document, with JSON-pointer escaping (`~0`, `~1`)
and percent-decoding handled.

| Situation | Behaviour |
|---|---|
| Local, resolvable | Resolved; the property is expanded in place AND linked |
| **Remote** (`https://…`, `common.yaml#/X`) | **Never fetched.** Warning `remote_reference_not_fetched`, entity `None` |
| Cycle (`Employee.manager → Employee`) | Expansion stops at the revisit; warning `circular_reference` |
| Beyond `max_reference_depth` | Expansion stops; warning `reference_depth_exceeded` |
| Dangling local pointer | Warning `unresolved_contract_reference`; nothing fabricated |

Cycles and depth limits **do not raise** — a recursive model still yields a
usable schema describing its first levels.

The two guards are complementary rather than interchangeable: a self-cycle is
caught by cycle detection regardless of the depth budget, while a chain of
distinct refs (`A→B→C→D`) can only be bounded by the depth budget.

A `$ref` between declared schemas also becomes a `SourceRelationship` of type
`EMBEDDED` with `confidence=1.0` — `EMBEDDED` rather than `REFERENCE` because
Phase 1 requires a key-based relationship to pair source and target fields one
to one, and a `$ref` names no target field. **No relationship is ever inferred
from a field name**: `customerId` produces nothing.

## 12. Composition

**`allOf` is merged.** The specification asserts the payload satisfies every
branch simultaneously, so their properties genuinely coexist; `required` is
unioned across branches. A property declared differently by two branches is a
conflict: the first declaration wins and `composition_conflict` is recorded,
rather than one silently overwriting the other.

**`oneOf`/`anyOf` are not merged and not resolved to one branch.** The payload
matches *one* alternative, so flattening them would describe a shape that never
occurs and picking the first would discard the rest. The field records:

```
source_data_type = "oneOf<BankAccount|CreditCard>"
metadata["variant_of"] = ["BankAccount", "CreditCard"]
```

The normalized type is the branches' shared type when they have one (two object
variants are still an object) and `UNKNOWN` otherwise (`anyOf<string|integer>`).
Branch properties are deliberately **not** expanded as fields.

## 13. Security metadata

Declared schemes are recorded **descriptively**: name, type (`apiKey`, `http`,
`oauth2`, `openIdConnect`), location, the header/query parameter *name*, the
HTTP scheme (`bearer`/`basic`), and OAuth **flow names**.

Deliberately not recorded: any credential, and any OAuth endpoint URL — a token
URL is an address this phase must never visit, so it is not stored at all.
`ApiSecurityScheme` has no field capable of holding a secret, which a test
asserts structurally.

## 14–17. Postman: collections, folders, requests, variables

A Postman collection **declares no types at all**. A request body is a payload
someone once sent; a saved response is a payload the server once returned. So
every structural claim from Postman is an observation, and the schema says so
(§23).

- **Folders** are preserved as `operation.folder_path`, nested arbitrarily
  deep. `Invoices/Reporting/Monthly Totals` keeps its full path, and
  `Invoices/Get Invoice` stays distinct from `Customers/Get Invoice`.
- **Item order is preserved** rather than sorted: a collection's order is
  authored, and the file's order is itself fixed, so determinism is kept.
- **URLs** normalize to a path template from either a raw string or the
  structured object form. Protocol, host and query values are dropped;
  `/invoices/:id` and `/invoices/{{invoiceId}}` survive as templates. No URL is
  ever contacted and no environment file is ever loaded.
- **Variables** are recorded by **name only** — from the `variable` array and
  from `{{…}}` occurrences in URLs, query values and headers. What `{{apiToken}}`
  resolves to is never read into anything returned.

## 18. Headers and auth redaction

Header **names** and enabled state are kept; header **values** never are.
Names matching a sensitive list (`Authorization`, `X-API-Key`, `Cookie`,
`X-Auth-Token`, …) are additionally flagged `is_sensitive_name=True`, so a
consumer knows the endpoint expects a credential without ever receiving one.

Postman auth blocks contain live credentials. Only the **type** is recorded
(`bearer`, `basic`, `apikey`, `oauth2`).

**Scripts are never executed.** `prerequest` and `test` scripts are JavaScript;
only `script_present=True` is recorded. Their contents are not read, evaluated
or mined for behaviour.

## 19. Request bodies

| Mode | Handling |
|---|---|
| `raw` (JSON) | Parsed for STRUCTURE; fields and types inferred, values discarded |
| `raw` (invalid JSON) | Warning `invalid_json_body`; nothing fabricated |
| `urlencoded` / `formdata` | Fields inferred from parameter names, all `STRING` — a form encoding transmits text, so this is a fact about the encoding, not a guess |
| file field | Recorded as a binary input; the referenced local file is **never opened** |
| `graphql` | Recorded with its media type; a query string is not a JSON data contract |

## 20. Response-example inference

The headline Postman capability. With no declared schema, saved responses are
the only description of the contract:

```
saved response → safe JSON parse → field/path inference → SourceEntity
```

For an example containing an invoice, the parser produces:

```
invoiceId    STRING     customer            OBJECT
customerId   STRING     customer.contact.email  STRING
totalAmount  INTEGER    lines               ARRAY  array<object>
status       STRING     lines[].sku         STRING
settled      BOOLEAN    lines[].quantity    INTEGER
note         UNKNOWN    tags                ARRAY  array<string>
```

and stores **none** of `INV-1`, `CUS-5`, `5000`, `PAID`.

A root-level array is described by its **elements** (`root_type: array`), which
is where the fields are.

Responses are grouped by **status code** before combining: a 200 body and a 404
body are different contracts, and merging them would describe a response that
never occurs.

Non-JSON saved responses (HTML, XML, text, binary) are recorded with their
media type and **no invented fields**. XML parsing is not attempted, and SOAP
is not Phase 7.

## 21. Multiple examples

Two saved responses for the same status code are **combined**, not
compared-and-discarded:

| Example A | Example B | Result |
|---|---|---|
| `{"id": 1, "status": "PAID"}` | `{"id": "2", "status": "FAILED", "message": …}` | |

```
id       json_type_distribution {integer: 1, string: 1}   → UNKNOWN, mixed_types
message  presence 0.5                                     → not required
status   presence 1.0                                     → required
```

Structural disagreement between examples is information, not noise. Hiding it
would let a later phase assume a consistency the API does not have.

**This reuses Phase 5's `DocumentStructureInference` verbatim** rather than
writing a second inference engine. That engine already accumulates nested
paths, arrays, presence counts, type distributions and depth/field budgets over
JSON-shaped objects, is pure, imports no driver, and is covered by the Phase 5
suite. Only a JSON type-name renderer is added on top (`int`→`integer`,
`double`→`number`, `bool`→`boolean`) so inferred contracts read in the same
vocabulary as declared ones.

## 22. `SourceSchema` conversion

```
SourceSchema
  └── SourceEntity (entity_kind = API_SCHEMA)
       └── SourceField (nested_path, source_data_type, normalized_data_type, …)
```

- One entity per reusable component schema; inline schemas get their own.
- **Request and response stay distinct contracts.** `CreateInvoiceRequest` has
  no `invoiceId`; `Invoice` does. Merging same-named fields would describe
  neither correctly.
- **Deterministic naming** for inline schemas, derived only from method, path,
  direction, status and media type:
  `POST_invoices_request`, `GET_orders_response_200_json`. Never a counter or a
  UUID, which would make every reparse look like a change.
- **"An array of Invoice" links to the one Invoice entity** with
  `is_collection=True` on the response, rather than minting a near-duplicate
  entity whose fields could drift.
- **No primary keys, no uniqueness, no semantic types.** An API contract
  declares no database keys, and `customerNumber` gets a *type*, never a
  meaning.

Operation↔structure linkage survives conversion in `ApiOperation`
(`request_entity_ids`, `response_entity_ids`) and in
`schema.metadata["operations"]`, so the catalog keeps it too.

## 23. Schema origin semantics

Phase 1 provides three origins, and Phase 7 uses two of them honestly:

| Source | `SchemaOrigin` | Why |
|---|---|---|
| OpenAPI / Swagger | **`API_SPEC`** | A declared contract — neither discovered from a live system nor inferred from samples. Phase 1 has an origin for exactly this. |
| Postman | **`INFERRED`** | The collection declares no types; everything structural was observed from examples. |

Because `SourceSchema.origin` is a single schema-level value but a collection
can mix sources of knowledge, every **entity** additionally records
`structure_origin`: `declared`, `inferred_from_examples`, or
`inferred_from_parameters`. Provenance is never flattened into one convenient
answer.

## 24. Deterministic identity

```
content_hash = SHA-256 over the specification bytes
spec_id      = file.sha256.<hex>
schema_name  = filename stem            (stable scope)
schema_id    = {source_system_id}.{schema_name}.{structural_hash[:12]}
```

Identity is reused from Phase 6's `hashing` module rather than reimplemented,
so the same bytes get the same id whichever phase reads them — two independent
SHA-256 implementations would eventually drift.

`schema_name` deliberately excludes the content hash: it is the stable scope
Phase 2 versions within, so an edited spec must increment its history rather
than starting a new one. No timestamp, path or UUID enters identity.

## 25. Structural hash

The existing `SourceSchema.compute_schema_hash()` is used — no second hashing
algorithm.

**Structural** (hashed): entity existence, field existence, `source_data_type`,
`normalized_data_type`, `nested_path`, `required`/`nullable`, `is_array`,
relationships.

**Incidental** (metadata, not hashed): descriptions, summaries, examples,
`examples_observed`, operation indexes, warnings, parse timestamps.

So rewriting an API's prose produces **no** new catalog version, while a new
field, a removed field or a type change does.

## 26. Catalog integration

Verified against the real PostgreSQL catalog:

| Action | Result |
|---|---|
| Parse OpenAPI → publish | `created=True`, `catalog_version=1` |
| Reparse unchanged → publish | `created=False`, `catalog_version=1` |
| Change only descriptions → publish | `created=False`, `catalog_version=1` |
| Add a declared field → publish | `created=True`, `catalog_version=2` |
| Parse Postman → publish | `created=True`, `catalog_version=1` |
| Reparse unchanged → publish | `created=False`, `catalog_version=1` |
| Add a field to a saved example → publish | `created=True`, `catalog_version=2` |

with `SchemaDiff.added_fields` naming `("invoice", "currency")` and
`("get_invoice_response_200", "discountcode")` respectively. Phase 7 duplicates
none of the catalog's versioning logic.

## 27. No-network guarantee

Enforced by static analysis over every module in the package:

- **No networking import**: `requests`, `httpx`, `aiohttp`, `urllib3`,
  `socket`, `ssl`, `http.client`, `urllib.request`, … (`httpx` *is* installed
  in this environment, so the assertion is real, not vacuous).
- **No network call**: `urlopen`, `HTTPSConnection`, `ClientSession`,
  `connect`, `getresponse`, …
- **No credential vocabulary**: `acquire_token`, `refresh_token`,
  `authenticate`, …
- **No execution entry point** in the public API.
- A subprocess check that importing the package loads no HTTP client module.
- A live test that monkeypatches `socket.socket` to raise, then parses every
  URL-bearing fixture — proving no socket is opened even accidentally.

Remote `$ref`s are recorded as unresolved (§11). The package also writes
nothing to disk.

## 28. Privacy and resource limits

**Retained** (declared structure): schema, field, parameter and header *names*;
types, formats, required flags, nullability; enum members (a declared
constraint, bounded); security scheme names and types; authored descriptions
(bounded).

**Never retained** (data): OpenAPI `example`/`examples` payloads — recorded only
as `example_present: true`; Postman header values, variable values, auth
credentials, query parameter values; a documented server URL's query string.

The distinction: a *name* is part of the contract every consumer must know; a
*value* is one caller's data that happened to be pasted into the documentation.

Sentinel tests plant synthetic secrets in every such position and assert they
are absent from schemas, summaries, field metadata, warnings, exceptions,
captured logs and the catalog.

Limits (all configurable):

| Option | Default | On reaching it |
|---|---|---|
| `max_spec_size_bytes` | 16 MiB | `SpecFileError`, checked before reading |
| `max_operations` | 2000 | `SpecLimitExceededError` |
| `max_schemas` | 2000 | `SpecLimitExceededError` |
| `max_fields_per_schema` | 500 | Entity marked `partial` |
| `max_nesting_depth` | 12 | Expansion stops |
| `max_reference_depth` | 8 | Warning, expansion stops |
| `max_examples_per_operation` | 20 | Warning, remainder not examined |
| `max_example_body_bytes` | 1 MiB | Warning, body not parsed |
| `max_enum_values` | 100 | Truncated, `enum_truncated: true` |
| `max_warnings` | 200 | Bounded, suppressed count tracked |

**YAML is loaded only through `yaml.safe_load`.** A document containing
`!!python/object/apply` is refused with a distinct `UnsafeSpecContentError`
before parsing — a spec attempting it is a security event, not a typo. A static
test asserts no unsafe loader appears anywhere in the package.

## 29. Limitations

Honest constraints, not defects:

- **Postman types are observations**, bounded by however many examples were
  saved. One saved response makes every field look required.
- **`oneOf`/`anyOf` branches are named, not expanded.** Their properties are
  not available as fields, by design.
- **`allOf` conflicts keep the first declaration** and record the conflict
  rather than attempting a merge policy.
- **Remote `$ref`s are never resolved**, so a spec split across files is
  described only in part.
- **XML, SOAP and GraphQL bodies are not structurally parsed** — media type
  only.
- **`discriminator`, `not`, `patternProperties` and `additionalProperties`
  schemas are not expanded.**
- **Only the first level of an array-of-array is described.**
- **Server URL templating** (`{region}` variables) is recorded verbatim, not
  expanded.
- **One document per call.** A multi-file specification bundle is the caller's
  problem to assemble.

## 30. Phase 8 boundary

Not implemented here, by design:

- Source-to-canonical semantic mapping. `customerNumber` is **not** mapped to
  `canonical.customer_id`, and `totalAmt` is **not** mapped to
  `canonical.invoice.total`.
- Mapping-profile execution, `CanonicalRecord` / `CanonicalDocument` creation.
- Embeddings, Qdrant, RAG.
- REST/SOAP execution, token acquisition, MCP tool execution — the teammate's
  component.
- A REST API or UI over any of this.

Phase 7 ends where the API's data contracts have been described and published
to the catalog. Phase 8 uses those standardized structures for semantic mapping
into the canonical ERP model.
