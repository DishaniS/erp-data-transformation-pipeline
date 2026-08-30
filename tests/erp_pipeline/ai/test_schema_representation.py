"""Phase 7 - the ERP's structure as retrievable text.

These tests are about the BUILDER: what goes into a schema representation, what
is deliberately kept out, and what happens to a table too wide to fit the
embedding model's window. Retrieval itself is tested in
``tests/erp_pipeline/api/test_schema_search.py``.

The test that matters most is ``test_no_business_value_reaches_schema_text``.
A schema vector that carried sampled rows would turn a metadata search into an
unaudited data-export channel.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ai.schema_representation import (
    SCHEMA_ENTITY_TYPE,
    SCHEMA_MAX_CHARACTERS,
    SchemaRepresentationConfig,
    build_entity_texts,
    representation_ids_for_entity,
    schema_to_representations,
    source_entity_to_representations,
)
from erp_pipeline.schemas.enums import (
    ContentKind,
    EntityKind,
    FieldDataType,
    RelationshipType,
    SchemaOrigin,
)
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
)


def field(name, source_type, normalized, **kwargs):
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type=source_type,
        normalized_data_type=normalized,
        **kwargs,
    )


def entity(entity_id, name, fields, **kwargs):
    return SourceEntity(
        entity_id=entity_id,
        source_name=name,
        normalized_name=name,
        entity_kind=kwargs.pop("entity_kind", EntityKind.TABLE),
        fields=tuple(fields),
        **kwargs,
    )


def schema(entities, relationships=(), **kwargs):
    return SourceSchema(
        schema_id=kwargs.pop("schema_id", "sch_1"),
        source_system_id=kwargs.pop("source_system_id", "legacy_hr"),
        schema_name=kwargs.pop("schema_name", "public"),
        origin=SchemaOrigin.DISCOVERED,
        entities=tuple(entities),
        relationships=tuple(relationships),
        **kwargs,
    )


EMPLOYEES = entity(
    "legacy_hr.public.employees",
    "employees",
    [
        field(
            "employee_id", "VARCHAR(20)", FieldDataType.STRING,
            is_primary_key=True, nullable=False, required=True,
        ),
        field("name", "VARCHAR(200)", FieldDataType.STRING),
        field("department_id", "INTEGER", FieldDataType.INTEGER),
        field("salary", "DECIMAL(12,2)", FieldDataType.DECIMAL),
        field("birth_certificate", "BYTEA", FieldDataType.BINARY),
    ],
    primary_key_fields=("employee_id",),
)

DEPARTMENTS = entity(
    "legacy_hr.public.departments",
    "departments",
    [
        field(
            "department_id", "INTEGER", FieldDataType.INTEGER,
            is_primary_key=True, nullable=False, required=True,
        ),
        field("department_name", "VARCHAR(100)", FieldDataType.STRING),
    ],
    primary_key_fields=("department_id",),
)

EMPLOYMENT = SourceRelationship(
    relationship_id="fk_emp_dept",
    relationship_type=RelationshipType.FOREIGN_KEY,
    from_entity="employees",
    to_entity="departments",
    from_fields=("department_id",),
    to_fields=("department_id",),
)


@pytest.fixture
def hr_schema():
    return schema(
        [EMPLOYEES, DEPARTMENTS],
        [EMPLOYMENT],
        metadata={"database": "hrdb"},
        schema_hash="abc123",
    )


# ======================================================================
# What the text contains
# ======================================================================


def test_the_representation_names_its_source_schema_and_entity(hr_schema):
    text = source_entity_to_representations(hr_schema, EMPLOYEES)[0].text_for_ai

    assert "Source System: legacy_hr" in text
    assert "Database: hrdb" in text
    assert "Schema: public" in text
    assert "Entity: employees" in text
    assert "Entity Kind: table" in text


def test_every_field_name_appears(hr_schema):
    text = source_entity_to_representations(hr_schema, EMPLOYEES)[0].text_for_ai

    for name in (
        "employee_id", "name", "department_id", "salary", "birth_certificate"
    ):
        assert name in text


def test_both_type_systems_are_preserved(hr_schema):
    """The vendor's spelling AND the cross-dialect classification."""
    text = source_entity_to_representations(hr_schema, EMPLOYEES)[0].text_for_ai

    assert "Source Type: BYTEA" in text
    assert "Normalized Type: binary" in text
    assert "Source Type: DECIMAL(12,2)" in text
    assert "Normalized Type: decimal" in text


def test_keys_and_constraints_are_stated_as_discovered(hr_schema):
    text = source_entity_to_representations(hr_schema, EMPLOYEES)[0].text_for_ai

    assert "Primary Key: employee_id" in text
    assert "Primary Key: yes" in text
    assert "Nullable: yes" in text


def test_a_key_is_never_guessed_from_a_column_name():
    """``employee_id`` that discovery did not declare a key is not one."""
    unkeyed = entity(
        "s.public.notes",
        "notes",
        [field("employee_id", "VARCHAR(20)", FieldDataType.STRING)],
    )
    text = source_entity_to_representations(schema([unkeyed]), unkeyed)[0].text_for_ai

    assert "Primary Key" not in text


def test_a_composite_key_keeps_its_declared_order():
    stock = entity(
        "wms.public.warehouse_stock",
        "warehouse_stock",
        [
            field("warehouse_id", "VARCHAR(10)", FieldDataType.STRING,
                  is_primary_key=True, nullable=False, required=True),
            field("product_id", "VARCHAR(10)", FieldDataType.STRING,
                  is_primary_key=True, nullable=False, required=True),
        ],
        primary_key_fields=("warehouse_id", "product_id"),
    )
    text = source_entity_to_representations(schema([stock]), stock)[0].text_for_ai

    assert "Primary Key: warehouse_id, product_id" in text


# ======================================================================
# Relationships
# ======================================================================


def test_a_discovered_relationship_is_included(hr_schema):
    text = source_entity_to_representations(hr_schema, EMPLOYEES)[0].text_for_ai

    assert "Relationships:" in text
    assert "employees.department_id -> departments.department_id" in text
    assert "foreign_key" in text


def test_a_relationship_appears_on_both_of_its_entities(hr_schema):
    """The related entity is as good a starting point as the owning one."""
    text = source_entity_to_representations(hr_schema, DEPARTMENTS)[0].text_for_ai

    assert "departments.department_id" in text


def test_no_relationship_is_invented_from_similar_column_names():
    """``employees.department_id`` and ``departments.department_id`` do not
    imply a foreign key nobody declared."""
    without = schema([EMPLOYEES, DEPARTMENTS])
    text = source_entity_to_representations(without, EMPLOYEES)[0].text_for_ai

    assert "Relationships:" not in text


def test_an_uncertain_relationship_reports_its_confidence():
    guessed = SourceRelationship(
        relationship_id="maybe",
        relationship_type=RelationshipType.REFERENCE,
        from_entity="employees",
        to_entity="departments",
        from_fields=("department_id",),
        to_fields=("department_id",),
        confidence=0.62,
    )
    built = schema([EMPLOYEES, DEPARTMENTS], [guessed])
    text = source_entity_to_representations(built, EMPLOYEES)[0].text_for_ai

    assert "confidence=0.62" in text


def test_a_certain_relationship_does_not_print_confidence(hr_schema):
    """"confidence=1.00" on every declared foreign key is noise."""
    text = source_entity_to_representations(hr_schema, EMPLOYEES)[0].text_for_ai

    assert "confidence=" not in text


# ======================================================================
# TEST L - structure only, never data
# ======================================================================


def test_no_business_value_reaches_schema_text(hr_schema):
    """The column ``salary`` is structure. ``250000`` is not.

    A schema representation carrying sampled rows would let a caller read ERP
    data through a metadata search, bypassing every routing decision the record
    path makes.
    """
    text = source_entity_to_representations(hr_schema, EMPLOYEES)[0].text_for_ai

    for value in ("EMP002", "Nimal Silva", "Finance", "250000", "2025-06-01"):
        assert value not in text

    # The column names those values would live under ARE present.
    assert "salary" in text
    assert "name" in text


def test_the_builder_cannot_read_rows(hr_schema):
    """Structurally impossible, checked against the AST rather than the prose.

    A text search would match the word "sampled" in this module's own
    docstring explaining why it does not sample - which is why this inspects
    calls and imports instead.
    """
    import ast
    import inspect

    from erp_pipeline.ai import schema_representation

    tree = ast.parse(inspect.getsource(schema_representation))

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    for forbidden in (
        "execute", "fetchall", "fetchone", "iter_records", "read_rows",
        "sample", "extract_rows",
    ):
        assert forbidden not in called, forbidden

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    # Nothing that could reach a database or a file.
    for forbidden in ("sqlalchemy", "psycopg2", "pymongo", "csv", "pathlib"):
        assert forbidden not in imported, forbidden


# ======================================================================
# TEST Q - a table too wide for the model's window
# ======================================================================


def wide_entity(field_count: int):
    return entity(
        "erp.public.wide_table",
        "wide_table",
        [
            field(f"column_{index:03d}", "VARCHAR(50)", FieldDataType.STRING)
            for index in range(field_count)
        ],
    )


@pytest.mark.parametrize("field_count", [1, 5, 40, 200])
def test_no_field_is_ever_lost(field_count):
    """Silent truncation is worse than an obvious one - nothing reports it."""
    wide = wide_entity(field_count)
    built = source_entity_to_representations(schema([wide]), wide)
    covered = [
        name for item in built for name in item.content["field_names"]
    ]

    assert len(covered) == field_count
    assert len(set(covered)) == field_count
    assert set(covered) == {
        f"column_{index:03d}" for index in range(field_count)
    }


def test_every_chunk_stays_inside_the_models_window():
    """Text past 256 tokens contributes nothing, so a chunk must not exceed it."""
    wide = wide_entity(200)

    for item in source_entity_to_representations(schema([wide]), wide):
        assert len(item.text_for_ai) <= SCHEMA_MAX_CHARACTERS


def test_a_small_entity_is_a_single_representation(hr_schema):
    """Chunking is invisible until a table actually needs it."""
    built = source_entity_to_representations(hr_schema, EMPLOYEES)

    assert len(built) == 1
    assert built[0].content["schema_chunk_count"] == 1


def test_every_chunk_says_which_table_it_describes():
    """A hit on the fifth field group must still identify the entity."""
    wide = wide_entity(200)

    for item in source_entity_to_representations(schema([wide]), wide):
        assert "Entity: wide_table" in item.text_for_ai
        assert "Source System: legacy_hr" in item.text_for_ai


def test_a_field_definition_is_never_split_across_chunks():
    wide = wide_entity(200)

    for item in source_entity_to_representations(schema([wide]), wide):
        lines = item.text_for_ai.splitlines()
        # Every indented attribute line must follow a field line.
        for index, line in enumerate(lines):
            if line.startswith("  ") and line.strip():
                assert index > 0
                assert any(
                    lines[back].startswith("- ")
                    for back in range(index - 1, -1, -1)
                    if not lines[back].startswith("  ")
                )


def test_chunks_report_the_field_range_they_cover():
    wide = wide_entity(60)
    built = source_entity_to_representations(schema([wide]), wide)
    ranges = [
        (item.content["field_start"], item.content["field_end"])
        for item in built
    ]

    assert ranges[0][0] == 0
    assert ranges[-1][1] == 60
    # Contiguous, no gaps and no overlaps.
    for earlier, later in zip(ranges, ranges[1:]):
        assert earlier[1] == later[0]


# ======================================================================
# Identity
# ======================================================================


def test_identity_derives_from_the_stable_entity_id(hr_schema):
    """``schema_id`` changes with every schema change; ``entity_id`` does not."""
    first = source_entity_to_representations(hr_schema, EMPLOYEES)[0]

    changed = schema(
        [EMPLOYEES, DEPARTMENTS],
        [EMPLOYMENT],
        schema_id="a_completely_different_snapshot_id",
        metadata={"database": "hrdb"},
    )
    second = source_entity_to_representations(changed, EMPLOYEES)[0]

    assert first.representation_id == second.representation_id


def test_a_changed_entity_keeps_its_identity_and_updates_its_text(hr_schema):
    """Rediscovery updates the searchable structure; it does not add a rival."""
    before = source_entity_to_representations(hr_schema, EMPLOYEES)[0]

    grown = entity(
        "legacy_hr.public.employees",
        "employees",
        list(EMPLOYEES.fields) + [
            field("passport_scan", "BYTEA", FieldDataType.BINARY)
        ],
        primary_key_fields=("employee_id",),
    )
    after = source_entity_to_representations(
        schema([grown, DEPARTMENTS], [EMPLOYMENT]), grown
    )[0]

    assert before.representation_id == after.representation_id
    assert before.content_hash != after.content_hash
    assert "passport_scan" in after.text_for_ai
    assert "passport_scan" not in before.text_for_ai


def test_two_entities_never_share_an_identity(hr_schema):
    built = schema_to_representations(hr_schema)
    ids = [item.representation_id for item in built]

    assert len(ids) == len(set(ids))


def test_the_same_schema_built_twice_is_identical(hr_schema):
    """Deterministic: no timestamp, no ordering luck, no randomness."""
    first = schema_to_representations(hr_schema)
    second = schema_to_representations(hr_schema)

    assert [item.representation_id for item in first] == [
        item.representation_id for item in second
    ]
    assert [item.content_hash for item in first] == [
        item.content_hash for item in second
    ]
    assert [item.text_for_ai for item in first] == [
        item.text_for_ai for item in second
    ]


def test_the_prune_helper_names_the_ids_an_entity_occupies():
    ids = representation_ids_for_entity("legacy_hr.public.employees", 3)

    assert len(ids) == 3
    assert len(set(ids)) == 3


# ======================================================================
# Metadata / provenance
# ======================================================================


def test_metadata_carries_full_schema_provenance(hr_schema):
    metadata = source_entity_to_representations(hr_schema, EMPLOYEES)[0].metadata

    assert metadata["content_kind"] == ContentKind.SCHEMA.value
    assert metadata["source_system_id"] == "legacy_hr"
    assert metadata["source_entity"] == "employees"
    assert metadata["schema_id"] == "sch_1"
    assert metadata["schema_name"] == "public"
    assert metadata["entity_id"] == "legacy_hr.public.employees"
    assert metadata["entity_kind"] == "table"
    assert metadata["schema_hash"] == "abc123"
    assert metadata["database_name"] == "hrdb"
    assert metadata["schema_chunk_index"] == 0


def test_the_entity_type_is_schema(hr_schema):
    built = source_entity_to_representations(hr_schema, EMPLOYEES)[0]

    assert built.entity_type == SCHEMA_ENTITY_TYPE


def test_a_schema_representation_claims_no_canonical_record(hr_schema):
    """A schema describes structure; it derives from no ERP row."""
    built = source_entity_to_representations(hr_schema, EMPLOYEES)[0]

    assert built.source_record_ids == ()
    assert "canonical_record_id" not in built.metadata


def test_a_missing_database_name_is_absent_not_invented():
    """``schema_name`` is not a database name."""
    built = source_entity_to_representations(
        schema([EMPLOYEES, DEPARTMENTS]), EMPLOYEES
    )[0]

    assert "database_name" not in built.metadata
    assert "Database:" not in built.text_for_ai


# ======================================================================
# TEST F - one builder, every dialect
# ======================================================================


@pytest.mark.parametrize(
    "vendor_type, kind",
    [
        ("BYTEA", EntityKind.TABLE),
        ("LONGBLOB", EntityKind.TABLE),
        ("VARBINARY(MAX)", EntityKind.TABLE),
        ("binData", EntityKind.COLLECTION),
    ],
)
def test_every_dialect_keeps_its_vendor_type_and_normalizes_to_binary(
    vendor_type, kind
):
    built_entity = entity(
        "sys.ns.docs",
        "docs",
        [field("attachment", vendor_type, FieldDataType.BINARY)],
        entity_kind=kind,
    )
    text = source_entity_to_representations(
        schema([built_entity]), built_entity
    )[0].text_for_ai

    assert f"Source Type: {vendor_type}" in text
    assert "Normalized Type: binary" in text
    assert f"Entity Kind: {kind.value}" in text


def test_a_nested_mongo_field_reports_its_path():
    collection = entity(
        "mongo.hr.people",
        "people",
        [
            field(
                "total", "double", FieldDataType.DECIMAL,
                nested_path=("financial", "total"),
            )
        ],
        entity_kind=EntityKind.COLLECTION,
    )
    text = source_entity_to_representations(
        schema([collection]), collection
    )[0].text_for_ai

    assert "Nested Path: financial.total" in text


def test_an_array_field_is_marked():
    collection = entity(
        "mongo.hr.people",
        "people",
        [field("tags", "array<string>", FieldDataType.ARRAY, is_array=True)],
        entity_kind=EntityKind.COLLECTION,
    )
    text = source_entity_to_representations(
        schema([collection]), collection
    )[0].text_for_ai

    assert "Array: yes" in text


# ======================================================================
# Edge cases
# ======================================================================


def test_an_entity_with_no_fields_still_produces_a_representation():
    empty = entity("s.public.empty", "empty_table", [])
    built = source_entity_to_representations(schema([empty]), empty)

    assert len(built) == 1
    assert "Entity: empty_table" in built[0].text_for_ai


def test_field_order_follows_the_source_not_the_alphabet():
    """Sorting would make the representation disagree with the real table."""
    ordered = entity(
        "s.public.t",
        "t",
        [
            field("zebra", "INT", FieldDataType.INTEGER, ordinal=1),
            field("alpha", "INT", FieldDataType.INTEGER, ordinal=2),
        ],
    )
    text = source_entity_to_representations(schema([ordered]), ordered)[0].text_for_ai

    assert text.index("- zebra") < text.index("- alpha")


def test_relationships_ride_with_the_first_chunk_of_a_wide_entity():
    wide = entity(
        "legacy_hr.public.employees",
        "employees",
        [
            field(f"column_{index:03d}", "VARCHAR(50)", FieldDataType.STRING)
            for index in range(80)
        ],
    )
    built = source_entity_to_representations(
        schema([wide, DEPARTMENTS], [EMPLOYMENT]), wide
    )

    assert len(built) > 1
    assert "Relationships:" in built[0].text_for_ai


def test_the_config_fingerprint_is_recorded(hr_schema):
    built = source_entity_to_representations(
        hr_schema, EMPLOYEES, SchemaRepresentationConfig(max_characters=500)
    )[0]

    assert "max=500" in built.metadata["representation_config"]


def test_build_entity_texts_returns_field_names_per_chunk():
    wide = wide_entity(30)
    chunks = build_entity_texts(schema([wide]), wide)

    assert sum(len(chunk["field_names"]) for chunk in chunks) == 30
