# Schema Catalog

Project `R26-SE-034` — ERP-Aware Data Transformation Pipeline
Component owner: IT22267290

**Status: Phase 2 — persistence for the Phase 1 contracts.** Everything below
described as "implemented" exists as tested, working code in
`src/erp_pipeline/catalog/`. Everything described as "future" does not exist
yet.

---

## 1. Purpose

Phase 1 defined the canonical ERP data model as a set of in-memory Python
contracts (`SourceSystem`, `SourceSchema`, `MappingProfile`, ...). Those
contracts could be constructed and serialized to JSON, but nothing remembered
them between runs. The Schema Catalog is that memory: it makes source
descriptions

- **persistent** — written to PostgreSQL, survivable across process restarts
- **versioned** — every structural change to a source gets its own immutable
  snapshot, numbered in order
- **retrievable** — the latest snapshot, or any specific historical one, comes
  back as the exact same `SourceSchema` object type Phase 1 defined
- **comparable** — two snapshots can be diffed to see what changed
- **auditable** — nothing is silently overwritten; history is kept

## 2. Logical contracts vs PostgreSQL persistence

> "Our common database type" does not mean PostgreSQL — that principle from
> Phase 1 still holds.

The catalog's logical model is the Phase 1 dataclasses. PostgreSQL is the
physical engine chosen to store them *this phase*, addressed through the
project's existing pipeline database (`PIPELINE_DB_*`). Nothing about the
contracts assumes PostgreSQL:

- `erp_pipeline.schemas` still performs no I/O and imports nothing beyond the
  standard library — a test enforces this
  (`test_erp_pipeline_schemas_imports_no_third_party_package`).
- Every row the catalog stores round-trips back into an ordinary Phase 1
  object via the model's own constructor. The database schema is a
  persistence *representation*, not a second domain model (see §11).
- If a later phase changes the physical store, only
  `erp_pipeline.catalog.config`, `.schema`, and `.repository` would need to
  change. `erp_pipeline.schemas` and every caller of `CatalogRepository`
  would be unaffected.

## 3. The `erp_catalog` namespace

All catalog tables live in a dedicated PostgreSQL schema,
`erp_catalog`, inside the same database the rest of the pipeline uses
(`erp_ai_native_db` by default):

```
erp_ai_native_db
├── public                      <- unchanged BPI application tables
│   ├── cleaned_event_logs
│   ├── ai_ready_cases
│   └── ...
└── erp_catalog                 <- new in Phase 2
    ├── source_systems
    ├── schema_snapshots
    ├── source_entities
    ├── source_fields
    ├── source_relationships
    ├── mapping_profiles
    └── field_mappings
```

No BPI table was moved, renamed, or touched. `python -m
erp_pipeline.catalog.schema` (or `bootstrap_catalog(engine)`) creates the
namespace and every table idempotently — see §15.

The schema name itself is a fixed Python constant
(`erp_pipeline.catalog.config.CATALOG_SCHEMA_NAME`), never taken from user
input or interpolated from a `SourceSchema.schema_name` value — that field is
*data*, stored in a column, not a SQL identifier (see §13, security).

## 4. Table relationships

```
source_systems
   └─(1:N)→ schema_snapshots  (source_system_id)
                 ├─(1:N)→ source_entities   (schema_id)
                 │              └─(1:N)→ source_fields   (schema_id, entity_id)
                 └─(1:N)→ source_relationships  (schema_id)
   └─(1:N)→ mapping_profiles  (source_system_id)
                 │      ↘(0:1, optional)  schema_snapshots  (source_schema_id)
                 └─(1:N)→ field_mappings   (mapping_id)
```

Every foreign key uses PostgreSQL's default `NO ACTION` (no `ON DELETE
CASCADE` anywhere), and `CatalogRepository` exposes no delete method for any
table. A historical snapshot cannot be removed as a side effect of deleting
something else, or at all, through this API.

## 5. Source system registration

`SourceSystem` is registered with `CatalogRepository.save_source_system` /
`SchemaCatalogService.register_source_system`. Registration is an UPSERT keyed
on `source_system_id`:

- registering the same system again with identical content is a no-op
- descriptive fields (`name`, `description`, `environment`, `schema_version`,
  `metadata`) can be updated in place; `created_at` never changes on update
- **`source_type` can never change once registered.** Attempting to
  re-register `finance_erp_pg` as `mongodb` after it was registered as
  `postgresql` raises `SourceSystemIdentityConflictError`. `source_system_id`
  identifies one source technology for its lifetime — every schema snapshot
  filed under it assumes that technology never changed underneath it.

```python
from erp_pipeline.catalog.config import CatalogDatabaseSettings
from erp_pipeline.catalog.repository import CatalogRepository
from erp_pipeline.schemas import SourceSystem, SourceType

engine = CatalogDatabaseSettings.from_env().create_engine()
repo = CatalogRepository(engine)

system = SourceSystem(
    source_system_id="finance_erp_pg",
    name="Finance Legacy ERP",
    source_type=SourceType.POSTGRESQL,
    environment="research",
)
repo.save_source_system(system)                      # registers
retrieved = repo.get_source_system("finance_erp_pg")  # -> SourceSystem
```

`SourceSystem` still forbids credentials structurally (Phase 1's rule) — the
catalog stores exactly what the model allows, nothing more.

## 6. Schema snapshots

Every `SourceSchema` saved to the catalog becomes a **snapshot**: one
immutable row in `schema_snapshots`, plus one row per entity, field, and
relationship it contains, all inserted in a single transaction (§9).

Two version numbers appear side by side and mean different things:

| Field | Meaning |
|---|---|
| `SourceSchema.schema_version` (stored as `source_schema_version`) | Whatever version the *source itself* supplies or the discovery process assigned. Phase 1 default: `"1"`. Not managed by the catalog. |
| `catalog_version` | A monotonically increasing integer this catalog assigns within `(source_system_id, schema_name)`. Starts at 1, increments only when content actually changes. |

Example: `finance_erp_pg` / `public` could be saved with
`source_schema_version="1"` five times as the discovery tool reruns, but if
the structure never changes, `catalog_version` stays at `1` throughout.

## 7. Immutability

A `schema_id` is an identity that, once written, may never point at different
content:

- Saving the *same* `SourceSchema` object (same `schema_id`, same structure)
  again is a no-op — the existing row is returned, no new version.
- Saving a *different* `SourceSchema` object with a **new** `schema_id` but
  structurally **identical** content to the current latest snapshot in scope
  is also deduplicated — the existing snapshot is returned and the new id is
  discarded. This handles the realistic case where a discovery run mints a
  fresh `schema_id` every time even though nothing on the source changed.
- Saving a `SourceSchema` under a `schema_id` **already on file with different
  content** raises `SchemaIdentityConflictError`. Changed content always needs
  a new `schema_id` — the catalog will never silently rewrite history.

## 8. Structural hashes

`SourceSchema.compute_schema_hash()` (Phase 1) is the source of truth. The
catalog **recomputes it server-side on every save** and never trusts a
caller-supplied `schema_hash` value — a save with a stale or hand-edited hash
still gets the correct one persisted. The hash covers entities, fields, and
relationships; it deliberately excludes timestamps and free-form `metadata`
(documentation, not structure), so re-describing an unchanged source at a
later time — or attaching new notes to it — never registers as a schema
change.

## 9. Catalog versions

Assigned by `CatalogRepository.save_schema_snapshot` (`erp_pipeline.
catalog.versioning.next_catalog_version`): 1 if the `(source_system_id,
schema_name)` scope is empty, otherwise `latest + 1`. The whole save — the
snapshot row, every entity, every field, every relationship — happens inside
one `engine.begin()` block. SQLAlchemy commits on normal exit and rolls back
automatically on any exception, so a failure partway through (a duplicate
`entity_id`, a lost connection) leaves **no partial snapshot** — verified by a
live test that deliberately breaks a save halfway through and checks zero rows
remain.

## 10. Schema comparison

`erp_pipeline.catalog.versioning.compare_schemas(old, new)` is pure — it reads
two already-loaded `SourceSchema` objects and returns a `SchemaDiff`. It never
touches PostgreSQL and never looks at a timestamp.

Detected:
- added / removed entities (by `normalized_name`)
- added / removed fields, per entity (by `normalized_name`)
- changed field attributes (`source_data_type`, `normalized_data_type`,
  `nullable`, `required`, `is_primary_key`, `is_unique`, `is_array`,
  `nested_path`, `semantic_type`)
- added / removed relationships (matched by **structure** —
  `(type, from_entity, from_fields, to_entity, to_fields)` — the same key
  `compute_schema_hash()` itself uses, not by `relationship_id`, since a
  fresh discovery run may mint a new id for the same real-world relationship)

**Rename candidates are conservative, never confirmed.** A generic system
watching arbitrary sources cannot know that a removed field and an added field
are "the same field renamed" — that's a claim about intent. `SchemaDiff.
possible_rename_candidates` is populated only when the evidence is
unambiguous: exactly one field removed and exactly one added *in the same
entity*, agreeing on normalized type and array-ness. Two removals plus two
additions, or a removal in one entity paired with an addition in another,
produce plain added/removed entries and no candidate.

**Breaking-change classification** (`SchemaDiff.breaking_level`, one of
`non_breaking` / `potentially_breaking` / `breaking`) is a best-effort
structural signal, not a business-compatibility guarantee:

| Change | Level |
|---|---|
| Entity removed | breaking |
| Field removed | breaking |
| `normalized_data_type` changed | breaking |
| Primary-key status changed | breaking |
| New required, non-nullable field | potentially_breaking |
| Field became non-nullable | potentially_breaking |
| Relationship removed | potentially_breaking |
| Entity added, optional field added, relationship added | non_breaking |

## 11. Mapping profile persistence

`MappingProfile`, `FieldMapping`, and `TransformationRule` can be saved and
retrieved exactly as Phase 1 defined them — no field was added or renamed to
make persistence work. A profile is not modelled as an immutable history the
way schema snapshots are: saving the same `mapping_id` again replaces its
field mappings (an UPSERT + delete-and-reinsert of `field_mappings` in one
transaction), matching a reviewer editing a single document in place rather
than filing a new version every time.

**No mapping engine exists.** Nothing in this package or in Phase 1 executes
a `TransformationRule`. Each rule is stored exactly as an operation name plus
a JSON config (e.g. `{"operation": "cast", "config": {"to": "decimal"}}`) in a
JSONB column, and is only ever round-tripped, never interpreted.

A profile binds to one known schema snapshot through the Phase 1 model's own
optional `source_schema_id` field — validated against `schema_snapshots` when
present. No repository-level binding metadata was added; the existing
contract field does the job.

## 12. Security — no credentials

The rule Phase 1 established for `SourceSystem` extends unchanged: no
password, API secret, token, or connection string may ever be stored, because
the Phase 1 model itself refuses to construct with one (a credential-shaped
metadata key raises `ValidationError` before the catalog ever sees the
object). The catalog adds:

- catalog database credentials come from `PIPELINE_DB_*` /
  `AI_DB_*`-deprecated-fallback and are never printed, logged, or returned by
  any function — only `host:port/database` (`CatalogDatabaseSettings.
  safe_target`).
- every query is parameterized through SQLAlchemy Core; no string formatting
  ever builds a SQL statement.
- `SourceSchema.schema_name` and every other user/source-supplied string is
  treated as *data*, bound as a parameter — never interpolated into SQL, and
  never used as a PostgreSQL schema/table identifier. The one fixed
  identifier, `erp_catalog`, is a Python constant.

## 13. Environment variables

Canonical: `PIPELINE_DB_HOST` / `PIPELINE_DB_PORT` / `PIPELINE_DB_NAME` /
`PIPELINE_DB_USER` / `PIPELINE_DB_PASSWORD` — the same names Phase 0
established for the pipeline database, since the catalog lives in that same
database. `AI_DB_*` is accepted as a deprecated fallback with a printed
warning, exactly mirroring Phase 0's rule: canonical wins when both are set
and they disagree. `erp_pipeline.catalog.config` restates this independently
of `bpi2020.common.config` rather than importing it — `erp_pipeline` never
depends on `bpi2020`.

## 14. Future integration

None of the following exists yet. This section names them only so the
catalog's design can be judged against what it will eventually have to
support.

- **Database discovery** (PostgreSQL/MySQL/SQL Server introspection,
  `INFORMATION_SCHEMA` scanning) would construct a `SourceSchema` object and
  call `save_schema_snapshot` — the catalog does not care how the object was
  built.
- **MongoDB schema inference** likewise ends at a `SourceSchema` with
  `EntityKind.COLLECTION` entities and `nested_path`-bearing fields, already
  representable (proven in Phase 1's tests).
- **CSV upload** and **OpenAPI/Postman parsing** are the same story —
  Phase 1 already proved the contracts can represent all of these; Phase 2
  proved they can be persisted and losslessly reconstructed once built.
- **A mapping engine** would read `MappingProfile` rows this package already
  stores and interpret `TransformationRule` operations it implements — no
  schema change to `mapping_profiles` / `field_mappings` is anticipated.
- **Schema-diff-driven notifications or automatic re-mapping** would consume
  `compare_schemas` output; the diff structure already carries what such a
  consumer would need (added/removed/changed, breaking level).

## 15. Phase 2 limitations — explicit

- **No delete API.** By design (§4), not an oversight.
- **No schema-drift polling or monitoring.** Saving a snapshot is triggered by
  a caller; nothing watches a source and calls it automatically.
- **No REST API, no FastAPI.** `CatalogRepository` / `SchemaCatalogService`
  are plain Python classes over a SQLAlchemy engine.
- **Mapping profile "versioning" is UPSERT-in-place, not immutable history**
  like schema snapshots. If per-mapping-profile history becomes a
  requirement, it is a deliberate future addition, not a bug in the current
  design.
- **Rename detection is a heuristic, not an inference engine.** It reports
  candidates under narrow, unambiguous conditions and nothing else; a
  same-entity swap involving more than one field pair is reported as plain
  additions and removals.
- **Breaking-change classification is structural only.** It cannot know
  whether a "breaking" type change actually breaks any real consumer — it is
  a signal for a human or a later automated gate to act on, not a guarantee.

## Bootstrap and verification commands

```powershell
# One-time (and idempotent) setup of the erp_catalog schema and tables:
$env:PYTHONPATH = "src"
python -m erp_pipeline.catalog.schema

# Integrity check - connectivity, tables, orphan rows, version/hash
# duplicates, unambiguous latest-snapshot resolution:
python -m erp_pipeline.catalog.verify
```

Both commands print only host/port/database — never a credential — and exit
non-zero on any integrity failure, so `verify` is usable as a CI gate.
