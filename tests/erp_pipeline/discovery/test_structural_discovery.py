"""Structural discovery: tables, columns, keys, constraints, indexes.

Uses FakeInspector so every structural edge case is deterministic and needs no
live database. The same code path runs against real PostgreSQL in
test_live_postgresql_discovery.py.
"""

import pytest
from sqlalchemy import types as sqltypes

from erp_pipeline.discovery.errors import UnsupportedDiscoverySourceError
from erp_pipeline.discovery.models import DiscoveryOptions
from erp_pipeline.discovery.relational import RelationalSchemaDiscovery, discover_schema
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, RelationshipType, SchemaOrigin, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceRelationship, SourceSchema

from tests.erp_pipeline.discovery.fakes import FakeInspector, FakeRelationalConnector, column


def _simple_inspector(**overrides):
    defaults = dict(
        schema_names=["public"],
        tables={"public": ["customer"]},
        columns={
            ("public", "customer"): [
                column("id", sqltypes.Integer(), nullable=False),
                column("name", sqltypes.String(120)),
            ]
        },
        primary_keys={("public", "customer"): {"constrained_columns": ["id"], "name": "customer_pkey"}},
    )
    defaults.update(overrides)
    return FakeInspector(**defaults)


# ============================================================
# Source rejection (Step 3)
# ============================================================

def test_mongodb_connector_is_rejected():
    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.connectors.mongodb import MongoDBConnector

    connector = MongoDBConnector(
        ConnectionSettings(
            source_system_id="mongo_sys", source_type=SourceType.MONGODB,
            host="localhost", port=27017, database="db",
        )
    )
    with pytest.raises(UnsupportedDiscoverySourceError, match="Phase 5"):
        RelationalSchemaDiscovery(connector)
    connector.close()


def test_non_connector_is_rejected():
    with pytest.raises(UnsupportedDiscoverySourceError):
        RelationalSchemaDiscovery({"not": "a connector"})


# ============================================================
# Output is always the generic Phase 1 contract (Step 36)
# ============================================================

def test_output_is_a_source_schema_with_phase1_nested_models():
    connector = FakeRelationalConnector(_simple_inspector())
    schema = discover_schema(connector)

    assert isinstance(schema, SourceSchema)
    assert schema.origin is SchemaOrigin.DISCOVERED
    assert all(isinstance(e, SourceEntity) for e in schema.entities)
    assert all(isinstance(f, SourceField) for e in schema.entities for f in e.fields)
    connector.close()


# ============================================================
# Tables, namespaces, ordering
# ============================================================

def test_tables_are_discovered_with_namespace():
    connector = FakeRelationalConnector(_simple_inspector())
    schema = discover_schema(connector)

    assert len(schema.entities) == 1
    entity = schema.entities[0]
    assert entity.source_name == "customer"
    assert entity.namespace == "public"
    assert entity.normalized_name == "public.customer"
    assert entity.entity_kind is EntityKind.TABLE
    connector.close()


def test_same_table_name_in_two_namespaces_does_not_collide():
    inspector = FakeInspector(
        schema_names=["public", "sales"],
        tables={"public": ["customer"], "sales": ["customer"]},
        columns={
            ("public", "customer"): [column("id", sqltypes.Integer(), nullable=False)],
            ("sales", "customer"): [column("id", sqltypes.Integer(), nullable=False)],
        },
    )
    connector = FakeRelationalConnector(inspector)
    schema = discover_schema(connector)

    names = {e.normalized_name for e in schema.entities}
    assert names == {"public.customer", "sales.customer"}
    assert len({e.entity_id for e in schema.entities}) == 2
    connector.close()


def test_mysql_uses_bare_table_names_without_a_namespace():
    """MySQL has no namespace level inside a database - it must not be forced
    into PostgreSQL semantics (Step 5)."""
    inspector = FakeInspector(
        tables={None: ["invoices"]},
        columns={(None, "invoices"): [column("invoice_number", sqltypes.String(20), nullable=False)]},
        dialect_name="mysql",
    )
    connector = FakeRelationalConnector(inspector, source_type=SourceType.MYSQL)
    schema = discover_schema(connector)

    entity = schema.entities[0]
    assert entity.namespace is None
    assert entity.normalized_name == "invoices"
    connector.close()


def test_column_order_is_preserved_via_ordinal():
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={
            ("public", "t"): [
                column("first", sqltypes.String(10)),
                column("second", sqltypes.Integer()),
                column("third", sqltypes.Boolean()),
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    schema = discover_schema(connector)

    fields = schema.entities[0].fields
    assert [f.source_name for f in fields] == ["first", "second", "third"]
    assert [f.ordinal for f in fields] == [0, 1, 2]
    connector.close()


def test_views_excluded_by_default_and_included_on_request():
    inspector_kwargs = dict(
        tables={"public": ["t"]},
        views={"public": ["v"]},
        columns={
            ("public", "t"): [column("a", sqltypes.Integer())],
            ("public", "v"): [column("b", sqltypes.Integer())],
        },
    )

    connector = FakeRelationalConnector(FakeInspector(**inspector_kwargs))
    assert {e.source_name for e in discover_schema(connector).entities} == {"t"}
    connector.close()

    connector = FakeRelationalConnector(FakeInspector(**inspector_kwargs))
    schema = discover_schema(connector, DiscoveryOptions(include_views=True))
    kinds = {e.source_name: e.entity_kind for e in schema.entities}
    assert kinds == {"t": EntityKind.TABLE, "v": EntityKind.VIEW}
    connector.close()


def test_system_schemas_are_excluded_by_default():
    inspector = FakeInspector(
        schema_names=["public", "pg_catalog", "information_schema"],
        tables={"public": ["t"], "pg_catalog": ["pg_class"], "information_schema": ["tables"]},
        columns={("public", "t"): [column("a", sqltypes.Integer())]},
    )
    connector = FakeRelationalConnector(inspector)
    schema = discover_schema(connector)

    assert {e.namespace for e in schema.entities} == {"public"}
    connector.close()


def test_include_and_exclude_table_filters():
    inspector = FakeInspector(
        tables={"public": ["keep", "drop_me"]},
        columns={
            ("public", "keep"): [column("a", sqltypes.Integer())],
            ("public", "drop_me"): [column("a", sqltypes.Integer())],
        },
    )
    connector = FakeRelationalConnector(inspector)
    schema = discover_schema(connector, DiscoveryOptions(exclude_tables=["drop_me"]))
    assert {e.source_name for e in schema.entities} == {"keep"}
    connector.close()


# ============================================================
# Column properties: types, nullability, defaults, required
# ============================================================

def test_vendor_type_preserved_and_normalized_separately():
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={("public", "t"): [column("amount", sqltypes.Numeric(18, 2))]},
    )
    connector = FakeRelationalConnector(inspector)
    field = discover_schema(connector).entities[0].fields[0]

    assert "18" in field.source_data_type and "2" in field.source_data_type
    assert field.normalized_data_type is FieldDataType.DECIMAL
    connector.close()


def test_nullable_and_required_semantics():
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={
            ("public", "t"): [
                column("required_col", sqltypes.String(10), nullable=False),
                column("optional_col", sqltypes.String(10), nullable=True),
                column("defaulted_col", sqltypes.String(10), nullable=False, default="'active'"),
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    fields = {f.source_name: f for f in discover_schema(connector).entities[0].fields}

    assert fields["required_col"].nullable is False
    assert fields["required_col"].required is True

    assert fields["optional_col"].nullable is True
    assert fields["optional_col"].required is False

    # NOT NULL but with a default: the source can supply a value, so the
    # caller is not required to.
    assert fields["defaulted_col"].nullable is False
    assert fields["defaulted_col"].required is False
    connector.close()


def test_defaults_are_preserved_as_strings_and_never_evaluated():
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={
            ("public", "t"): [
                column("a", sqltypes.DateTime(), default="CURRENT_TIMESTAMP"),
                column("b", sqltypes.Integer(), default="nextval('seq'::regclass)"),
                column("c", sqltypes.Integer(), default="0"),
                column("d", sqltypes.String(10), default="'active'::character varying"),
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    fields = {f.source_name: f for f in discover_schema(connector).entities[0].fields}

    assert fields["a"].metadata["column_default"] == "CURRENT_TIMESTAMP"
    assert fields["b"].metadata["column_default"] == "nextval('seq'::regclass)"
    assert fields["c"].metadata["column_default"] == "0"
    assert isinstance(fields["d"].metadata["column_default"], str)
    connector.close()


def test_semantic_type_is_never_inferred_in_phase_4():
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={
            ("public", "t"): [
                column("email", sqltypes.String(255)),
                column("customer_id", sqltypes.Integer()),
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    for field in discover_schema(connector).entities[0].fields:
        assert field.semantic_type is None
    connector.close()


def test_relational_columns_have_no_nested_path():
    connector = FakeRelationalConnector(_simple_inspector())
    for field in discover_schema(connector).entities[0].fields:
        assert field.nested_path is None
    connector.close()


def test_column_comment_becomes_description():
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={("public", "t"): [column("a", sqltypes.Integer(), comment="the a column")]},
        table_comments={("public", "t"): {"text": "the t table"}},
    )
    connector = FakeRelationalConnector(inspector)
    entity = discover_schema(connector).entities[0]

    assert entity.description == "the t table"
    assert entity.fields[0].description == "the a column"
    connector.close()


def test_array_column_sets_is_array():
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={("public", "t"): [column("tags", sqltypes.ARRAY(sqltypes.String()))]},
    )
    connector = FakeRelationalConnector(inspector)
    field = discover_schema(connector).entities[0].fields[0]

    assert field.normalized_data_type is FieldDataType.ARRAY
    assert field.is_array is True
    connector.close()


# ============================================================
# Primary keys (Step 9)
# ============================================================

def test_single_primary_key():
    connector = FakeRelationalConnector(_simple_inspector())
    entity = discover_schema(connector).entities[0]

    assert entity.primary_key_fields == ("id",)
    assert entity.field_by_normalized_name("id").is_primary_key is True
    assert entity.field_by_normalized_name("name").is_primary_key is False
    connector.close()


def test_composite_primary_key_preserves_column_order():
    inspector = FakeInspector(
        tables={"public": ["invoice_line"]},
        columns={
            ("public", "invoice_line"): [
                column("tenant_id", sqltypes.Integer(), nullable=False),
                column("invoice_no", sqltypes.String(20), nullable=False),
                column("line_no", sqltypes.Integer(), nullable=False),
                column("amount", sqltypes.Numeric(12, 2)),
            ]
        },
        primary_keys={
            ("public", "invoice_line"): {
                "constrained_columns": ["tenant_id", "invoice_no", "line_no"],
                "name": "invoice_line_pkey",
            }
        },
    )
    connector = FakeRelationalConnector(inspector)
    entity = discover_schema(connector).entities[0]

    assert entity.primary_key_fields == ("tenant_id", "invoice_no", "line_no")
    assert entity.metadata["primary_key_constraint"] == "invoice_line_pkey"
    connector.close()


def test_table_without_primary_key_is_valid_and_not_fabricated():
    inspector = FakeInspector(
        tables={"public": ["no_key"]},
        columns={("public", "no_key"): [column("a", sqltypes.String(10))]},
        primary_keys={("public", "no_key"): {"constrained_columns": []}},
    )
    connector = FakeRelationalConnector(inspector)
    entity = discover_schema(connector).entities[0]

    assert entity.primary_key_fields == ()
    assert entity.has_primary_key is False
    assert all(f.is_primary_key is False for f in entity.fields)
    connector.close()


def test_primary_key_column_is_never_nullable():
    """Phase 1 rejects a nullable PK; reflection reporting nullable=True for a
    PK column must be corrected, not passed through into a validation error."""
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={("public", "t"): [column("id", sqltypes.Integer(), nullable=True)]},
        primary_keys={("public", "t"): {"constrained_columns": ["id"]}},
    )
    connector = FakeRelationalConnector(inspector)
    field = discover_schema(connector).entities[0].fields[0]

    assert field.is_primary_key is True
    assert field.nullable is False
    connector.close()


# ============================================================
# Foreign keys (Step 10)
# ============================================================

def _fk_inspector(**overrides):
    defaults = dict(
        tables={"public": ["customer", "fin_invoice"]},
        columns={
            ("public", "customer"): [column("id", sqltypes.Integer(), nullable=False)],
            ("public", "fin_invoice"): [
                column("invoice_no", sqltypes.String(20), nullable=False),
                column("customer_id", sqltypes.Integer()),
            ],
        },
        primary_keys={
            ("public", "customer"): {"constrained_columns": ["id"]},
            ("public", "fin_invoice"): {"constrained_columns": ["invoice_no"]},
        },
        foreign_keys={
            ("public", "fin_invoice"): [
                {
                    "name": "fk_invoice_customer",
                    "constrained_columns": ["customer_id"],
                    "referred_schema": "public",
                    "referred_table": "customer",
                    "referred_columns": ["id"],
                }
            ]
        },
    )
    defaults.update(overrides)
    return FakeInspector(**defaults)


def test_single_column_foreign_key():
    connector = FakeRelationalConnector(_fk_inspector())
    schema = discover_schema(connector)

    assert len(schema.relationships) == 1
    relationship = schema.relationships[0]
    assert relationship.relationship_type is RelationshipType.FOREIGN_KEY
    assert relationship.from_entity == "public.fin_invoice"
    assert relationship.to_entity == "public.customer"
    assert relationship.from_fields == ("customer_id",)
    assert relationship.to_fields == ("id",)
    assert relationship.confidence == 1.0
    assert relationship.metadata["source_constraint_name"] == "fk_invoice_customer"
    connector.close()


def test_composite_foreign_key():
    inspector = FakeInspector(
        tables={"public": ["parent", "child"]},
        columns={
            ("public", "parent"): [
                column("tenant_id", sqltypes.Integer(), nullable=False),
                column("code", sqltypes.String(10), nullable=False),
            ],
            ("public", "child"): [
                column("tenant_id", sqltypes.Integer(), nullable=False),
                column("code", sqltypes.String(10), nullable=False),
            ],
        },
        primary_keys={("public", "parent"): {"constrained_columns": ["tenant_id", "code"]}},
        foreign_keys={
            ("public", "child"): [
                {
                    "name": "fk_child_parent",
                    "constrained_columns": ["tenant_id", "code"],
                    "referred_schema": "public",
                    "referred_table": "parent",
                    "referred_columns": ["tenant_id", "code"],
                }
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    relationship = discover_schema(connector).relationships[0]

    assert relationship.from_fields == ("tenant_id", "code")
    assert relationship.to_fields == ("tenant_id", "code")
    assert relationship.metadata["is_composite"] is True
    connector.close()


def test_self_referencing_foreign_key():
    inspector = FakeInspector(
        tables={"public": ["employee"]},
        columns={
            ("public", "employee"): [
                column("id", sqltypes.Integer(), nullable=False),
                column("manager_id", sqltypes.Integer()),
            ]
        },
        primary_keys={("public", "employee"): {"constrained_columns": ["id"]}},
        foreign_keys={
            ("public", "employee"): [
                {
                    "name": "fk_employee_manager",
                    "constrained_columns": ["manager_id"],
                    "referred_schema": "public",
                    "referred_table": "employee",
                    "referred_columns": ["id"],
                }
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    relationship = discover_schema(connector).relationships[0]

    assert relationship.from_entity == relationship.to_entity == "public.employee"
    assert relationship.metadata["is_self_reference"] is True
    connector.close()


def test_cross_schema_foreign_key():
    inspector = FakeInspector(
        schema_names=["public", "sales"],
        tables={"public": ["customer"], "sales": ["invoice"]},
        columns={
            ("public", "customer"): [column("id", sqltypes.Integer(), nullable=False)],
            ("sales", "invoice"): [column("customer_id", sqltypes.Integer())],
        },
        primary_keys={("public", "customer"): {"constrained_columns": ["id"]}},
        foreign_keys={
            ("sales", "invoice"): [
                {
                    "name": "fk_invoice_customer",
                    "constrained_columns": ["customer_id"],
                    "referred_schema": "public",
                    "referred_table": "customer",
                    "referred_columns": ["id"],
                }
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    relationship = discover_schema(connector).relationships[0]

    assert relationship.from_entity == "sales.invoice"
    assert relationship.to_entity == "public.customer"
    assert relationship.metadata["referred_namespace"] == "public"
    connector.close()


def test_foreign_key_to_out_of_scope_table_is_omitted_with_warning():
    inspector = FakeInspector(
        tables={"public": ["invoice"]},
        columns={("public", "invoice"): [column("customer_id", sqltypes.Integer())]},
        foreign_keys={
            ("public", "invoice"): [
                {
                    "name": "fk_out_of_scope",
                    "constrained_columns": ["customer_id"],
                    "referred_schema": "excluded_schema",
                    "referred_table": "customer",
                    "referred_columns": ["id"],
                }
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    discovery = RelationalSchemaDiscovery(connector)
    schema = discovery.discover()

    assert schema.relationships == ()
    assert any("outside the discovered scope" in w for w in discovery.warnings)
    connector.close()


def test_relationships_are_only_declared_constraints_never_inferred():
    """Phase 4 records declared FKs only - no name-similarity inference."""
    inspector = FakeInspector(
        tables={"public": ["customer", "invoice"]},
        columns={
            ("public", "customer"): [column("id", sqltypes.Integer(), nullable=False)],
            # customer_id looks like an FK but no constraint declares it.
            ("public", "invoice"): [column("customer_id", sqltypes.Integer())],
        },
        primary_keys={("public", "customer"): {"constrained_columns": ["id"]}},
        foreign_keys={},
    )
    connector = FakeRelationalConnector(inspector)
    assert discover_schema(connector).relationships == ()
    connector.close()


# ============================================================
# Unique constraints and indexes (Steps 11, 12)
# ============================================================

def test_single_column_unique_marks_the_field_unique():
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={("public", "t"): [column("email", sqltypes.String(255), nullable=False)]},
        unique_constraints={("public", "t"): [{"name": "uq_email", "column_names": ["email"]}]},
    )
    connector = FakeRelationalConnector(inspector)
    entity = discover_schema(connector).entities[0]

    assert entity.fields[0].is_unique is True
    assert "composite_unique_constraints" not in entity.metadata
    connector.close()


def test_composite_unique_does_not_mark_member_fields_individually_unique():
    """The core Step 11 rule: marking each member unique would assert
    something false about the data."""
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={
            ("public", "t"): [
                column("customer_id", sqltypes.Integer(), nullable=False),
                column("issued_on", sqltypes.Date(), nullable=False),
            ]
        },
        unique_constraints={
            ("public", "t"): [
                {"name": "uq_customer_date", "column_names": ["customer_id", "issued_on"]}
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    entity = discover_schema(connector).entities[0]

    assert all(f.is_unique is False for f in entity.fields)

    preserved = entity.metadata["composite_unique_constraints"]
    assert len(preserved) == 1
    assert preserved[0]["columns"] == ["customer_id", "issued_on"]
    assert preserved[0]["name"] == "uq_customer_date"
    connector.close()


def test_composite_unique_backed_by_index_is_not_duplicated():
    """PostgreSQL/SQL Server back a UNIQUE constraint with an index; the same
    logical rule must be recorded once, not twice."""
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={
            ("public", "t"): [
                column("a", sqltypes.Integer(), nullable=False),
                column("b", sqltypes.Integer(), nullable=False),
            ]
        },
        unique_constraints={("public", "t"): [{"name": "uq_ab", "column_names": ["a", "b"]}]},
        indexes={("public", "t"): [{"name": "uq_ab", "column_names": ["a", "b"], "unique": True}]},
    )
    connector = FakeRelationalConnector(inspector)
    entity = discover_schema(connector).entities[0]

    assert len(entity.metadata["composite_unique_constraints"]) == 1
    connector.close()


def test_indexes_are_preserved_in_entity_metadata_not_as_fields():
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={
            ("public", "t"): [
                column("a", sqltypes.Integer()),
                column("b", sqltypes.Integer()),
            ]
        },
        indexes={
            ("public", "t"): [
                {"name": "ix_a", "column_names": ["a"], "unique": False},
                {"name": "ix_ab_unique", "column_names": ["a", "b"], "unique": True},
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    entity = discover_schema(connector).entities[0]

    assert len(entity.fields) == 2  # no extra field created for an index
    indexes = entity.metadata["indexes"]
    assert {i["name"] for i in indexes} == {"ix_a", "ix_ab_unique"}
    assert next(i for i in indexes if i["name"] == "ix_ab_unique")["unique"] is True
    assert next(i for i in indexes if i["name"] == "ix_a")["columns"] == ["a"]
    connector.close()


def test_table_without_indexes_has_no_index_metadata():
    connector = FakeRelationalConnector(_simple_inspector())
    assert "indexes" not in discover_schema(connector).entities[0].metadata
    connector.close()


def test_expression_index_records_a_count_not_the_expression_body():
    """A functional index expression can embed literal values; only the count
    of such expressions is recorded."""
    inspector = FakeInspector(
        tables={"public": ["t"]},
        columns={("public", "t"): [column("a", sqltypes.String(10))]},
        indexes={
            ("public", "t"): [
                {"name": "ix_expr", "column_names": [None, "a"], "unique": False}
            ]
        },
    )
    connector = FakeRelationalConnector(inspector)
    index = discover_schema(connector).entities[0].metadata["indexes"][0]

    assert index["columns"] == ["a"]
    assert index["expression_column_count"] == 1
    connector.close()


# ============================================================
# Deterministic identity and hashing (Steps 6, 15, 16)
# ============================================================

def test_entity_ids_are_deterministic_across_runs():
    first = discover_schema(FakeRelationalConnector(_simple_inspector()))
    second = discover_schema(FakeRelationalConnector(_simple_inspector()))

    assert [e.entity_id for e in first.entities] == [e.entity_id for e in second.entities]


def test_entity_id_does_not_depend_on_discovery_order():
    forward = FakeInspector(
        tables={"public": ["alpha", "beta"]},
        columns={
            ("public", "alpha"): [column("a", sqltypes.Integer())],
            ("public", "beta"): [column("b", sqltypes.Integer())],
        },
    )
    reversed_order = FakeInspector(
        tables={"public": ["beta", "alpha"]},
        columns={
            ("public", "alpha"): [column("a", sqltypes.Integer())],
            ("public", "beta"): [column("b", sqltypes.Integer())],
        },
    )

    ids_forward = {e.source_name: e.entity_id for e in discover_schema(FakeRelationalConnector(forward)).entities}
    ids_reverse = {e.source_name: e.entity_id for e in discover_schema(FakeRelationalConnector(reversed_order)).entities}

    assert ids_forward == ids_reverse


def test_schema_id_and_hash_are_deterministic():
    first = discover_schema(FakeRelationalConnector(_simple_inspector()))
    second = discover_schema(FakeRelationalConnector(_simple_inspector()))

    assert first.schema_id == second.schema_id
    assert first.compute_schema_hash() == second.compute_schema_hash()
    assert first.schema_hash == first.compute_schema_hash()


def test_schema_id_changes_when_structure_changes_but_scope_does_not():
    """Content-addressed snapshot id: required so Phase 2 can create a new
    version instead of raising SchemaIdentityConflictError."""
    original = discover_schema(FakeRelationalConnector(_simple_inspector()))

    changed_inspector = _simple_inspector(
        columns={
            ("public", "customer"): [
                column("id", sqltypes.Integer(), nullable=False),
                column("name", sqltypes.String(120)),
                column("email", sqltypes.String(255)),
            ]
        }
    )
    changed = discover_schema(FakeRelationalConnector(changed_inspector))

    assert changed.schema_id != original.schema_id
    assert changed.compute_schema_hash() != original.compute_schema_hash()
    # ...but the logical scope Phase 2 versions within is unchanged.
    assert changed.schema_name == original.schema_name


def test_schema_name_is_the_stable_logical_scope():
    schema = discover_schema(FakeRelationalConnector(_simple_inspector()))
    assert schema.schema_name == "public"


def test_schema_id_contains_no_timestamp_or_randomness():
    """Two discoveries seconds apart must produce byte-identical ids."""
    import time

    first = discover_schema(FakeRelationalConnector(_simple_inspector()))
    time.sleep(0.01)
    second = discover_schema(FakeRelationalConnector(_simple_inspector()))

    assert first.schema_id == second.schema_id


# ============================================================
# Graceful degradation
# ============================================================

def test_optional_metadata_failure_does_not_fail_discovery():
    inspector = _simple_inspector()
    inspector._failing = {"get_indexes", "get_table_comment", "get_unique_constraints"}

    connector = FakeRelationalConnector(inspector)
    discovery = RelationalSchemaDiscovery(connector)
    schema = discovery.discover()

    assert len(schema.entities) == 1  # structural discovery still succeeded
    assert discovery.warnings  # but the problem was reported
    connector.close()


def test_a_single_broken_table_does_not_abort_the_run():
    class _PartiallyBroken(FakeInspector):
        def get_columns(self, table_name, schema=None):
            if table_name == "broken":
                raise RuntimeError("table vanished mid-discovery")
            return super().get_columns(table_name, schema=schema)

    inspector = _PartiallyBroken(
        tables={"public": ["good", "broken"]},
        columns={("public", "good"): [column("a", sqltypes.Integer())]},
    )
    connector = FakeRelationalConnector(inspector)
    discovery = RelationalSchemaDiscovery(connector)
    schema = discovery.discover()

    assert {e.source_name for e in schema.entities} == {"good"}
    assert any("broken" in w for w in discovery.warnings)
    connector.close()
