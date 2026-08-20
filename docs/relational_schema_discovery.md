# Relational Schema Discovery

Project `R26-SE-034` — ERP-Aware Data Transformation Pipeline
Component owner: IT22267290

**Status: Phase 4 — implemented; live-verified on PostgreSQL only.**
Everything described as "implemented" exists as tested, working code in
`src/erp_pipeline/discovery/`. Live-verification status per engine is stated
honestly in §19 — MySQL and SQL Server are implemented and unit-verified but
have **not** been proven against a live server in this environment.

**MongoDB schema inference is NOT Phase 4.** It is Phase 5 and is explicitly
rejected by this package.

---

## 1. Purpose

Phase 3 answered *"how do I connect to this database?"*. Phase 4 answers
*"what relational structure exists inside it?"* — tables, columns, vendor
datatypes, nullability, defaults, primary keys, foreign keys, unique
constraints and indexes — and expresses the answer in the Phase 1
`SourceSchema` contract so the Phase 2 catalog can version and store it.

It does **not** answer *"what does this field mean?"* or *"how does it map to
our canonical ERP field?"* — those are later phases.

## 2. Architecture

```
Relational Database (PostgreSQL / MySQL / SQL Server)
        |
        v
Phase 3 Connector            erp_pipeline.connectors
        |                    (validated settings, pooled engine, lifecycle)
        v
Phase 4 Discovery            erp_pipeline.discovery   <- THIS PACKAGE
        |                    (SQLAlchemy Inspector -> generic contract)
        v
    SourceSchema             erp_pipeline.schemas (Phase 1 contract)
        |
        v
Phase 2 Schema Catalog       erp_pipeline.catalog
                             (idempotency, catalog_version, immutability)
```

Module layout:

| Module | Responsibility |
|---|---|
| `errors.py` | `DiscoveryError` hierarchy |
| `models.py` | `DiscoveryOptions`, profiling result models, system-namespace table |
| `type_mapping.py` | vendor datatype → `FieldDataType`, in one place |
| `relational.py` | the single cross-engine discovery algorithm |
| `profiling.py` | optional aggregate-only profiling |
| `service.py` | `RelationalDiscoveryService`, catalog handoff |

There is deliberately **no** `adapters/postgresql.py` / `mysql.py` /
`sqlserver.py`. SQLAlchemy's dialects already normalize introspection across
all three engines, so a per-engine adapter would have been three copies of the
same traversal. The only genuinely engine-specific data is centralized:
system namespaces in `models.SYSTEM_NAMESPACES`, namespace semantics in
`relational._discover_namespaces`, and the string-length function in
`profiling._length_function`.

## 3–5. PostgreSQL, MySQL and SQL Server support

All three go through the same code path. What differs:

| | PostgreSQL | MySQL | SQL Server |
|---|---|---|---|
| Namespaces | schemas inside a database → `SourceEntity.namespace` | none — "schema" *is* the database, so `namespace` is `None` | schemas inside a database → `SourceEntity.namespace` |
| Entity naming | `public.fin_invoice` | `invoices` (bare) | `dbo.invoiceheader` |
| Excluded by default | `pg_catalog`, `information_schema`, `pg_toast` | `information_schema`, `mysql`, `performance_schema`, `sys` | `sys`, `information_schema`, the `db_*` fixed roles |

MySQL is **not** forced into PostgreSQL's two-level shape (Step 5). Entities
qualify their name with a namespace only when the engine actually has one —
which is also what keeps `public.customer` and `sales.customer` from
colliding under the catalog's `(schema_id, normalized_name)` unique index.

## 6. SQLAlchemy Inspector

`sqlalchemy.inspect(engine)` is the single introspection layer, reached
through the connector's `create_inspector()`. Methods used:
`get_schema_names`, `get_table_names`, `get_view_names`, `get_columns`,
`get_pk_constraint`, `get_foreign_keys`, `get_unique_constraints`,
`get_indexes`, `get_table_comment`.

No raw `information_schema` or `sys.*` SQL is written anywhere in the package
— asserted by test. `relational.py` contains no SQL string literal at all.

## 7. SourceSchema output

One example entity, discovered (not hand-written):

```json
{
  "entity_id": "discovery_probe_pg.erp_disc_test.fin_invoice",
  "source_name": "fin_invoice",
  "normalized_name": "erp_disc_test.fin_invoice",
  "entity_kind": "table",
  "namespace": "erp_disc_test",
  "primary_key_fields": ["tenant_id", "invoice_no"],
  "fields": [
    {
      "source_name": "total",
      "normalized_name": "total",
      "source_data_type": "NUMERIC(18, 2)",
      "normalized_data_type": "decimal",
      "nullable": true, "required": false,
      "is_primary_key": false, "is_unique": false, "is_array": false,
      "nested_path": null, "semantic_type": null, "ordinal": 3,
      "metadata": {"source_column_name": "total"}
    }
  ],
  "metadata": {
    "composite_unique_constraints": [
      {"name": "uq_invoice_customer_date",
       "columns": ["customer_id", "issued_on"], "source": "unique_constraint"}
    ],
    "indexes": [{"name": "ix_invoice_approval", "columns": ["approval"], "unique": false}]
  }
}
```

## 8. Datatype normalization

Two values are always produced and never conflated: `source_data_type` (the
vendor's own spelling, precision and scale preserved verbatim) and
`normalized_data_type` (the common `FieldDataType`).

| Vendor types | Common |
|---|---|
| `VARCHAR`, `NVARCHAR`, `TEXT`, `LONGTEXT`, `CHAR`, `UUID`, `UNIQUEIDENTIFIER`, `XML`, `ENUM` | `STRING` |
| `SMALLINT`, `INTEGER`, `INT`, `BIGINT`, `TINYINT`, `SERIAL` | `INTEGER` |
| `NUMERIC`, `DECIMAL`, `MONEY`, `SMALLMONEY`, `REAL`, `FLOAT`, `DOUBLE` | `DECIMAL` |
| `BOOLEAN`, `BOOL`, `BIT` | `BOOLEAN` |
| `DATE` | `DATE` |
| `TIMESTAMP`, `TIMESTAMPTZ`, `DATETIME`, `DATETIME2`, `DATETIMEOFFSET`, `SMALLDATETIME` | `DATETIME` |
| `JSON`, `JSONB`, `HSTORE` | `OBJECT` |
| `ARRAY` | `ARRAY` |
| `BYTEA`, `VARBINARY`, `BLOB`, `IMAGE` | `BINARY` |
| anything unrecognized | `UNKNOWN` |

Classification trusts SQLAlchemy's own generic type hierarchy first and falls
back to vendor type-name matching. **`TINYINT(1)` normalizes to `INTEGER`,
not `BOOLEAN`** — MySQL uses `TINYINT(1)` both for `BOOLEAN` and for a real
small integer, and guessing from display width would misclassify genuine
integer columns. When a column is truly declared `BOOLEAN`, SQLAlchemy
reflects a `Boolean` type object and the class-based path resolves it
correctly.

## 9–11. Keys, relationships, constraints

**Primary keys** — single and composite, with column order preserved.
`SourceEntity.primary_key_fields` and `SourceField.is_primary_key` are kept
consistent. Tables with no primary key remain valid; keys are never
fabricated. A PK column is forced non-nullable (Phase 1 rejects a nullable
PK).

**Foreign keys** — each declared constraint becomes one `SourceRelationship`
with `relationship_type=FOREIGN_KEY` and `confidence=1.0` (a declared
constraint is fact, not inference). Single-column, composite, self-referencing
and cross-schema FKs are all supported. Nothing is inferred: a column named
`customer_id` with no constraint produces no relationship. An FK pointing
outside the discovered scope is omitted with a warning, because Phase 1
requires every relationship to reference a declared entity.

**Unique constraints** — `SourceField.is_unique` is set **only** for
single-column uniqueness. A composite constraint does not mark its member
columns individually unique, because that would assert something false about
the data.

*Documented limitation:* Phase 1's `SourceField.is_unique` is per-field and
cannot express "these N columns are unique together". Rather than modify a
frozen Phase 1 contract, composite uniqueness is preserved losslessly in
`SourceEntity.metadata["composite_unique_constraints"]` as structured,
JSON-safe data (name, ordered columns, and whether it came from a constraint
or a unique index). PostgreSQL and SQL Server back a UNIQUE constraint with
an index, so the same rule is reported by two Inspector calls; entries are
deduplicated on the ordered column list.

## 12. Indexes

Stored in `SourceEntity.metadata["indexes"]` — name, ordered column list,
unique flag. No `SourceField` is created for an index. **Functional-index
expression bodies are deliberately not captured**: an expression can embed
literal values, and this metadata is stored in a catalog that must stay free
of data content. Such expressions are reported only as
`expression_column_count`.

## 13–14. Defaults, nullability, comments

Column defaults are captured as **schema metadata only** —
`SourceField.metadata["column_default"]`, stored verbatim as a string.
`CURRENT_TIMESTAMP`, `nextval('seq'::regclass)`, `0`, `'active'` are recorded;
none is ever parsed, evaluated or executed.

`required` means *the source itself demands a value*: not nullable **and** no
default to fall back on. A `NOT NULL` column with a default is `nullable=False,
required=False`.

Table and column comments become `description` when the dialect exposes them.
Failure to obtain comments never fails discovery.

## 15–16. Identity and structural hash

Phase 2 treats `schema_id` as an **immutable snapshot identity** (reusing one
for different content raises `SchemaIdentityConflictError`) and `schema_name`
as the **logical scope** it versions within. Phase 4 respects that split:

```
schema_name  = stable logical scope       "public"
schema_id    = content-addressed snapshot "finance_erp_pg.bpi2020_old_erp_db.public.a1f71f07cbc4"
entity_id    = "{source_system_id}.{namespace}.{table}"
```

Including a prefix of the structural hash in `schema_id` is what gives both
required behaviours at once:

- unchanged database → identical `schema_id` → idempotent, still version 1
- changed database → new `schema_id` → catalog version N+1

Nothing in any identifier depends on a timestamp, discovery order, or
randomness. The structural hash is the existing Phase 1
`SourceSchema.compute_schema_hash()` — no separate algorithm — which since the
Phase 0–2 audit fix also covers `semantic_type`.

## 17–18. Optional profiling

**OFF by default and never required for structural discovery.** When enabled,
only aggregates are collected:

| Level | Statistic |
|---|---|
| Table | row count |
| Column | null count / null percentage, distinct count, numeric min/max, min/max/average length |

**Privacy rules.** There is no `SELECT col FROM …`, no `LIMIT n` row fetch, no
`SELECT *`. `ColumnProfile` has no field capable of holding a value — no
sample, no mode, no "most common value" — asserted structurally by test.
Numeric `MIN`/`MAX` is issued **only against numeric columns**, never against
text, binary or temporal ones, so a bound can never surface an email address,
a customer name, or an invoice description.

*Stated precisely:* a numeric min/max **is** a number drawn from the column —
that is inherent to reporting a bound, and Step 17 explicitly permits it. The
guarantee is that no **text or otherwise identifying content** ever leaves the
source, which is why MIN/MAX is type-restricted.

**Budget.** Defaults: 20 tables, 200 queries. Exceeding either stops
profiling, marks the summary `partial`, and records a note — discovery still
succeeds. `strict_budget=True` raises `ProfilingBudgetExceeded` instead, for
callers who want that. Individual profiling failures are recorded on the
result, never raised.

Profiling is supplemental: `ProfilingSummary` lives outside `SourceSchema`, so
enabling it never changes the structural hash.

## 19. Live verification status

| Engine | Status |
|---|---|
| **PostgreSQL** | **LIVE VERIFIED.** Read-only against the real BPI source database (5 tables, 252 columns) plus an isolated probe schema exercising composite PKs, composite/self-referencing FKs, unique constraints, indexes, defaults, comments and 11 vendor datatypes. |
| **MySQL** | **LIVE VERIFIED.** Read-only against the Sakila sample database using a `read_only` account holding only `SELECT, SHOW VIEW`. See `tests/erp_pipeline/discovery/test_live_mysql_discovery.py`. |
| **SQL Server** | Implemented; unit/mock-verified. No reachable SQL Server instance (port 1433 closed). Having the `pyodbc` package and an ODBC driver installed is **not** the same as having a server. **LIVE DISCOVERY NOT VERIFIED.** |

SQL Server is additionally exercised through a `FakeInspector` reproducing
exactly the shapes those dialects return, including `mysql.ENUM`,
`mysql.DECIMAL`, `mssql.NVARCHAR`, `mssql.MONEY` and `mssql.BIT` type objects.
That proves the algorithm and type mapping, not the live connection.

## 20. Schema Catalog publishing

`RelationalDiscoveryService.discover_and_publish(connector, catalog_service)`
runs discovery and hands the `SourceSchema` to Phase 2. Phase 4 duplicates
**none** of the catalog's versioning logic — idempotency, `catalog_version`
assignment, snapshot immutability and history remain entirely Phase 2's
responsibility.

Verified live: discover → publish → version 1; rediscover unchanged →
version stays 1; add one column to the isolated test schema → version 2 with
`SchemaDiff.added_fields == (("erp_disc_test.customer", "email"),)`.

## 21. Limitations and the paradigm boundary

Not implemented by *relational* discovery, by design:

- **MongoDB schema inference and document sampling** — a separate entry point.
  A MongoDB connector passed to `discover_schema()` still raises
  `UnsupportedDiscoverySourceError`; document inference is Phase 5's
  `infer_mongodb_schema()`, documented in
  [`mongodb_schema_inference.md`](mongodb_schema_inference.md). The two read
  fundamentally different things — declared metadata versus sampled documents
  — and converge only on the shared `SourceSchema` output.
- **Semantic interpretation.** `semantic_type` is always `None`; Phase 4 never
  guesses that `email` is an email address.
- **Mapping** — no suggestions, no scoring, no execution. Phase 8.
- **Row extraction, ETL, incremental extraction, schema-drift polling.**
- **CSV / OpenAPI / Postman parsing**, REST API, UI, embeddings, vector storage.
- Composite uniqueness is preserved in entity metadata rather than as a
  first-class Phase 1 field (see §11).
- Views are discoverable but excluded by default (`include_views=False`).
