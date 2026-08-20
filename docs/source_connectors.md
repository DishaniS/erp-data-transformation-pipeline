# Source Connector Framework

Project `R26-SE-034` — ERP-Aware Data Transformation Pipeline
Component owner: IT22267290

**Status: Phase 3 — connectivity only.** Everything described below as
"implemented" exists as tested, working code in `src/erp_pipeline/connectors/`.
Phase 3 proves the framework can safely **connect** to a source; it does not
know what is **inside** that source. See §16.

---

## 1. Purpose

Phase 1 defined how a source is described (`SourceSystem`, `SourceSchema`).
Phase 2 persisted those descriptions. Neither phase could actually reach a
live ERP database - there was no connection code anywhere in
`erp_pipeline`. This package is that missing piece: given valid runtime
connection information, it creates the right connector for the source's
technology, opens a connection, verifies it, reports safe non-schema
metadata, reports what the technology is capable of, and closes cleanly.

```
Heterogeneous ERP Source
        |
        v
 Source Connector          <- Phase 3 (this package)
        |
        v
 Schema Discovery          <- Phase 4 (relational) / Phase 5 (MongoDB)
        |
        v
   SourceSchema
        |
        v
  Schema Catalog            (Phase 2, already built)
        |
        v
     Mapping                <- future phase
        |
        v
 Canonical ERP Model
```

## 2. Supported source technologies

PostgreSQL, MySQL, SQL Server, MongoDB - the same four technologies
`SourceType` (Phase 1) already names. No fifth vocabulary was invented;
`erp_pipeline.connectors` reads `SourceType` directly from
`erp_pipeline.schemas.enums`.

## 3. SourceSystem vs ConnectionSettings

These answer two different questions and must never be confused:

| | `SourceSystem` (Phase 1) | `ConnectionSettings` (Phase 3) |
|---|---|---|
| Question | What is this source, logically? | How do I reach it, right now? |
| Fields | id, name, technology, environment | host, port, credentials, TLS, timeout |
| Carries credentials? | Never - the model refuses to construct with one | Yes - that is its entire purpose |
| Persisted? | Yes, in the schema catalog | **Never** |
| Lifetime | Long-lived, versionable identity | Constructed fresh per run |

`ConnectionSettings.require_compatible_source_system(source_system)` checks
the two agree on `source_system_id` and `source_type` before a connector is
built - a mismatch raises `ConnectorTypeMismatchError` immediately, before
any network activity.

## 4. Credential handling

- `ConnectionSettings.password` is a dataclass field declared
  `field(repr=False)`, so the auto-generated `repr()` omits it; `str()` has
  no separate override and falls back to that same safe `repr()`.
- `sanitized()` returns an explicit safe dictionary - `password_set: bool`
  instead of the value - the only sanctioned way to log or display a
  settings object.
- `driver_options` and `metadata` are scanned with the same credential-key
  denylist `erp_pipeline.schemas.validation` already uses for `SourceSystem`
  metadata, so a second secret cannot be smuggled in through a side channel.
- Connection URLs are built with `sqlalchemy.URL.create(...)` and are never
  rendered with `render_as_string(hide_password=False)` anywhere in this
  package; the one call site that must see the real password is the DBAPI
  driver itself, internal to SQLAlchemy's `create_engine`.
- `errors.redact_text()` strips any `user:password@` fragment from
  driver-produced error text before it is placed in a raised exception, as
  defense in depth even though the targeted drivers do not normally embed
  credentials in their own messages.
- `ConnectionSettings` is never accepted by any `CatalogRepository` method
  and never persisted - see §15.

## 5. Connector lifecycle

Every connector implements the same four operations plus context-manager
support:

```python
with ConnectorRegistry.create(settings) as connector:
    result = connector.test_connection()      # -> ConnectionTestResult, or raises
    metadata = connector.get_source_metadata() # -> SourceMetadata
    capabilities = connector.get_capabilities()# -> ConnectorCapabilities
# connector.close() called automatically on exit
```

- `close()` is idempotent - calling it any number of times is safe.
- Any operation after `close()` (except `close()` itself and
  `get_capabilities()`, which is static) raises `ConnectorClosedError`.
- `__enter__` on an already-closed connector raises `ConnectorClosedError`
  rather than silently reopening.

**Design decision - result vs exception.** `test_connection()` returns a
`ConnectionTestResult` only on success. Any operational failure - wrong
credentials, unreachable host, timeout, missing driver - raises a typed
`ConnectorError` subclass instead of returning a `success=False` result.
One consistent representation was chosen deliberately: a caller that wants
"did it work" writes a single `try/except`, rather than checking both a
return value's `.success` field *and* handling exceptions for the cases a
result object cannot represent (a missing driver has no sensible "result").

## 6. Connector registry

```python
connector = ConnectorRegistry.create(settings)
```

replaces an `if postgres... elif mysql... elif sqlserver... elif mongo...`
chain. Built-in dispatch:

| `SourceType` | Connector |
|---|---|
| `POSTGRESQL` | `PostgreSQLConnector` |
| `MYSQL` | `MySQLConnector` |
| `SQL_SERVER` | `SQLServerConnector` |
| `MONGODB` | `MongoDBConnector` |

Each entry is a lazy loader function, not an already-imported class -
`ConnectorRegistry.create()` only imports `connectors.mysql` (for example)
when a MySQL connector is actually requested, and that module only imports
`pymysql` inside a method, not at module load time. An unregistered/unknown
`SourceType` raises `ConnectorConfigurationError` naming the registered
types; there is no silent fallback to a different connector.

## 7. PostgreSQL connector

`PostgreSQLConnector` uses `psycopg2` via SQLAlchemy Core - both already
required by this project since Phase 0, so there is no optional dependency
to guard. `test_connection()` runs `SELECT 1` plus `SELECT version()`
(best-effort); `get_source_metadata()` reports the driver name/version via
the imported `psycopg2` module. TLS is requested via `sslmode` when
`ssl_enabled=True`. **Live-verified** against the project's existing BPI
source database - see §14.

## 8. MySQL connector

`MySQLConnector` uses `PyMySQL`, a pure-Python DBAPI driver chosen
specifically so a real driver can be exercised in tests without any
compiled system dependency. Imported lazily inside
`_ensure_driver_available()`; its absence raises `ConnectorDependencyError`
naming the missing package and the install command. **Unit/mock-verified**;
see §14 for why full live verification was not claimed.

## 9. SQL Server connector

`SQLServerConnector` uses `pyodbc` via SQLAlchemy's `mssql+pyodbc` dialect.
Two independent things can be missing, and this connector distinguishes
them before ever attempting a connection:

1. the `pyodbc` Python package itself (`ConnectorDependencyError` naming the
   package)
2. the named ODBC driver registered with the system's ODBC driver manager -
   installing the Python package does **not** install this. The driver name
   is read from `ConnectionSettings.driver_options["driver"]`, defaulting to
   `"ODBC Driver 18 for SQL Server"` rather than being hardcoded, so a
   deployment with a different installed driver version is not locked out.
   `pyodbc.drivers()` is checked and an unregistered name raises
   `ConnectorDependencyError` listing what *is* installed - never a raw
   `pyodbc` stack trace.

**Unit/mock-verified**; the ODBC-driver-name check was additionally verified
against this development machine's real, installed ODBC driver list.

## 10. MongoDB connector

`MongoDBConnector` uses `pymongo`, imported lazily. Not a subclass of the
relational base - MongoDB has its own client/session model, no SQL dialect.
`test_connection()` calls `admin.command("ping")`, the documented safe,
read-only connectivity check; version metadata comes from the equally
read-only `server_info()`. Does **not** sample documents, does **not**
enumerate collections as `SourceEntity` objects, does **not** infer a
schema - all three are explicitly Phase 5. **Unit/mock-verified**.

## 11. Capabilities

`ConnectorCapabilities` reports what the underlying **technology** supports,
never what `erp_pipeline` has implemented. Every connector's capability
report carries a `notes` field stating this explicitly. Two attributes
deliberately differ across the three relational connectors rather than
being blanket-true:

- `supports_namespaces` - `True` for PostgreSQL and SQL Server (both have
  schemas nested inside a database); `False` for MySQL, where a "schema" IS
  the database with no separate namespace layer.
- All other relational flags (`supports_transactions`,
  `supports_primary_keys`, `supports_foreign_keys`,
  `supports_incremental_key_extraction`) are `True` for all three, and
  `supports_nested_documents` is `False` for all three.

MongoDB reports `document_database=True`, `relational=False`,
`supports_foreign_keys=False`, `supports_nested_documents=True`, and
`supports_incremental_key_extraction=True` (an `ObjectId` embeds a creation
timestamp and is typically monotonically increasing per collection - useful
to a future incremental-extraction phase, not implemented here).

## 12. Error model

```
ConnectorError
├── ConnectorConfigurationError   invalid/missing settings, unknown SourceType
├── ConnectorDependencyError      driver package or ODBC driver unavailable
├── ConnectorConnectionError      host unreachable / refused / unclassified
├── ConnectorAuthenticationError  credentials rejected by the source
├── ConnectorTimeoutError         connection or operation exceeded its timeout
├── ConnectorTypeMismatchError    connector/settings/SourceSystem technology disagree
└── ConnectorClosedError          operation attempted after close()
```

A caller that catches `ConnectorError` has caught every connector failure.
`__cause__` always preserves the original driver exception for debugging.
Relational drivers are classified by a shared, text-based heuristic
(`relational.classify_relational_error`) since psycopg2/PyMySQL/pyodbc each
have their own message vocabulary; an unrecognized message becomes
`ConnectorConnectionError` rather than risking a wrong, more specific label.
Every classified message passes through `redact_text()` first.

## 13. Dependency handling

`import erp_pipeline.connectors` never requires `pymysql`, `pyodbc`, or
`pymongo` - proven by a subprocess test that blocks all three via
`sys.modules` before importing the package. Only *using* the affected
connector (calling `test_connection()`, `get_source_metadata()`, or
anything that opens a session) triggers the lazy import and, if the
dependency is absent, raises `ConnectorDependencyError`. PostgreSQL support
is never affected by MySQL/SQL Server/MongoDB driver availability, or vice
versa.

## 14. Live-verification status

| Technology | Status |
|---|---|
| PostgreSQL | **LIVE VERIFIED** - against the project's existing BPI source database (`BPI_OLD_DB_*` / `ERP_SOURCE_DB_*`), read-only (`SELECT 1`, `SELECT version()`), nothing written |
| MySQL | Implemented, unit/mock-verified. A real MySQL server was found listening on the default port on the development machine, but no application credentials for it are configured anywhere in this project; one authentication attempt against it (with an intentionally wrong password) was used to prove error normalization live, but this is **not** a successful connectivity proof. **Live server not configured.** |
| SQL Server | Implemented, unit/mock-verified. No reachable SQL Server instance was found. **Live server not configured.** |
| MongoDB | Implemented, unit/mock-verified. A port was open on the default MongoDB port, but it did not speak the MongoDB wire protocol (the driver's own ping handshake failed/timed out) - not a real MongoDB service. **Live server not configured.** |

Setting up real demo MySQL/SQL Server/MongoDB instances is future work, not
a Phase 3 defect - the connectors are fully implemented and exercised
through mocked driver-level behavior that faithfully reproduces success,
authentication-failure, timeout, and missing-dependency conditions.

## 15. Relationship with the Schema Catalog

`erp_pipeline.catalog` and `erp_pipeline.connectors` are independent
siblings under `erp_pipeline`, both depending on `erp_pipeline.schemas`, and
neither importing the other. The catalog persists `SourceSystem` and
`SourceSchema`; it has never seen and will never accept a
`ConnectionSettings` object - `CatalogRepository.save_source_system`
expects the shape `SourceSystem` defines, which `ConnectionSettings` does
not share (no `.name`, no `.metadata` in the same sense), so passing one
fails immediately rather than silently persisting connector runtime data.
The `erp_catalog` PostgreSQL schema (Phase 2) defines no
password/token/connection_url/api_key column anywhere - verified directly
against the live table definitions.

## 16. Phase 3 limitations - explicit

Nothing in this list exists. None of it is a defect - it is Phase 4/5's job.

- **No schema discovery.** No `INFORMATION_SCHEMA` scanning, no `sys.*`
  catalog views, no table/column/primary-key/foreign-key enumeration for any
  relational connector.
- **No MongoDB inference.** No document sampling, no collection enumeration
  as `SourceEntity` objects, no field-type inference.
- **No mapping, no ETL, no row extraction, no incremental extraction.**
  `supports_incremental_key_extraction` is a capability *flag*, not an
  implemented extraction mechanism.
- **No arbitrary query execution.** No connector exposes
  `execute_sql`/`execute`/`run_query` or any other general-purpose way to
  run caller-supplied SQL or a MongoDB command beyond the fixed, safe
  `ping`/version calls this package makes internally. This framework is a
  connection boundary, not a remote query tool.
- **No REST API, no FastAPI, no frontend.**

Phase 4 discovers relational schemas (PostgreSQL/MySQL/SQL Server). Phase 5
infers MongoDB schemas. Both will construct `SourceSchema` objects and hand
them to the already-built Phase 2 catalog - this package's job ends at a
verified, closed connection.
