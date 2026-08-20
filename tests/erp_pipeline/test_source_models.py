"""Source-side contract tests.

Proves one vocabulary describes PostgreSQL, MySQL, SQL Server, MongoDB, CSV,
OpenAPI and Postman sources without any SQL-specific assumption.
"""

from datetime import datetime, timedelta, timezone

import pytest

from erp_pipeline.schemas import (
    EntityKind,
    FieldDataType,
    RelationshipType,
    SchemaOrigin,
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
    SourceSystem,
    SourceType,
    ValidationError,
)
from erp_pipeline.version import SOURCE_MODEL_VERSION


# ============================================================
# SourceSystem
# ============================================================

def test_source_system_valid_construction():
    system = SourceSystem(
        source_system_id="finance_erp_pg",
        name="Finance Legacy ERP",
        source_type=SourceType.POSTGRESQL,
        environment="research",
        description="Simulated legacy finance ERP used by the research project.",
    )

    payload = system.to_json_dict()

    assert payload["source_system_id"] == "finance_erp_pg"
    assert payload["name"] == "Finance Legacy ERP"
    assert payload["source_type"] == "postgresql"
    assert payload["environment"] == "research"
    assert payload["schema_version"] == SOURCE_MODEL_VERSION


def test_source_system_accepts_plain_string_source_type():
    system = SourceSystem(
        source_system_id="ops_erp_mysql",
        name="Operations ERP",
        source_type="mysql",
    )

    assert system.source_type is SourceType.MYSQL


def test_source_system_rejects_unknown_source_type():
    """SourceType is closed on purpose: a new technology needs a connector."""
    with pytest.raises(ValueError) as exc:
        SourceSystem(
            source_system_id="weird_erp",
            name="Unsupported",
            source_type="cobol_flat_file",
        )

    assert "not a valid SourceType" in str(exc.value)
    assert "postgresql" in str(exc.value)


def test_source_system_rejects_blank_id():
    with pytest.raises(ValidationError, match="must not be blank"):
        SourceSystem(source_system_id="   ", name="X", source_type=SourceType.CSV)


def test_source_system_rejects_unnormalized_id():
    with pytest.raises(ValidationError) as exc:
        SourceSystem(
            source_system_id="Finance ERP",
            name="X",
            source_type=SourceType.POSTGRESQL,
        )

    assert "normalized identifier" in str(exc.value)
    assert "finance_erp" in str(exc.value)  # suggestion is offered


@pytest.mark.parametrize(
    "secret_key",
    [
        "password",
        "db_password",
        "api_secret",
        "auth_token",
        "api_key",
        "connection_string",
        "private_key",
    ],
)
def test_source_system_never_carries_credentials(secret_key):
    """STRICT RULE: credentials must be impossible to put in the model."""
    with pytest.raises(ValidationError, match="must not contain credentials"):
        SourceSystem(
            source_system_id="finance_erp_pg",
            name="Finance",
            source_type=SourceType.POSTGRESQL,
            metadata={secret_key: "super-secret-value"},
        )


def test_source_system_rejects_nested_credentials():
    with pytest.raises(ValidationError, match="must not contain credentials"):
        SourceSystem(
            source_system_id="finance_erp_pg",
            name="Finance",
            source_type=SourceType.POSTGRESQL,
            metadata={"connection": {"host": "localhost", "password": "x"}},
        )


def test_source_system_has_no_connection_fields():
    """The model must not even offer a place to put connection details."""
    field_names = set(
        SourceSystem(
            source_system_id="s",
            name="n",
            source_type=SourceType.CSV,
        ).to_json_dict()
    )

    forbidden = {"host", "port", "user", "username", "password", "dsn", "url", "uri"}
    assert not (field_names & forbidden)


def test_source_system_metadata_must_be_json_safe():
    with pytest.raises(ValidationError, match="not JSON-serializable"):
        SourceSystem(
            source_system_id="s",
            name="n",
            source_type=SourceType.CSV,
            metadata={"handler": lambda value: value},
        )


def test_source_system_rejects_naive_datetime():
    with pytest.raises(ValidationError, match="timezone-aware"):
        SourceSystem(
            source_system_id="s",
            name="n",
            source_type=SourceType.CSV,
            created_at=datetime(2026, 1, 1, 12, 0, 0),
        )


# ============================================================
# SourceField: source vs normalized type separation
# ============================================================

@pytest.mark.parametrize(
    "source_data_type, normalized",
    [
        ("VARCHAR(100)", FieldDataType.STRING),
        ("DECIMAL(12,2)", FieldDataType.DECIMAL),
        ("NVARCHAR(MAX)", FieldDataType.STRING),
        ("ObjectId", FieldDataType.STRING),
        ("array<object>", FieldDataType.ARRAY),
        ("timestamp with time zone", FieldDataType.DATETIME),
        ("string($date-time)", FieldDataType.DATETIME),
    ],
)
def test_source_field_preserves_vendor_type_verbatim(source_data_type, normalized):
    """Vendor precision must survive; normalization must not overwrite it."""
    field = SourceField(
        source_name="some_column",
        normalized_name="some_column",
        source_data_type=source_data_type,
        normalized_data_type=normalized,
    )

    assert field.source_data_type == source_data_type
    assert field.normalized_data_type is normalized

    payload = field.to_json_dict()
    assert payload["source_data_type"] == source_data_type
    assert payload["normalized_data_type"] == normalized.value


def test_source_field_defaults_to_unknown_type():
    field = SourceField(source_name="mystery", normalized_name="mystery")
    assert field.normalized_data_type is FieldDataType.UNKNOWN


def test_source_field_nested_path_from_dotted_string():
    field = SourceField(
        source_name="total",
        normalized_name="total",
        nested_path="financial.summary",
        normalized_data_type=FieldDataType.DECIMAL,
    )

    assert field.nested_path == ("financial", "summary")
    assert field.access_path == ("financial", "summary", "total")
    assert field.to_json_dict()["nested_path"] == ["financial", "summary"]


def test_source_field_nested_path_from_sequence():
    field = SourceField(
        source_name="total",
        normalized_name="total",
        nested_path=["financial", "summary"],
    )

    assert field.nested_path == ("financial", "summary")


def test_source_field_rejects_unnormalized_normalized_name():
    with pytest.raises(ValidationError, match="normalized identifier"):
        SourceField(source_name="InvoiceNumber", normalized_name="InvoiceNumber")


def test_source_field_rejects_nullable_primary_key():
    with pytest.raises(ValidationError, match="cannot be nullable"):
        SourceField(
            source_name="id",
            normalized_name="id",
            is_primary_key=True,
            nullable=True,
        )


# ============================================================
# SourceEntity
# ============================================================

def test_source_entity_rejects_duplicate_field_names():
    with pytest.raises(ValidationError, match="more than once"):
        SourceEntity(
            entity_id="fin_invoice",
            source_name="fin_invoice",
            normalized_name="fin_invoice",
            fields=(
                SourceField(source_name="a", normalized_name="invoice_no"),
                SourceField(source_name="b", normalized_name="invoice_no"),
            ),
        )


def test_source_entity_rejects_primary_key_not_in_fields():
    with pytest.raises(ValidationError, match="not declared in fields"):
        SourceEntity(
            entity_id="fin_invoice",
            source_name="fin_invoice",
            normalized_name="fin_invoice",
            fields=(SourceField(source_name="a", normalized_name="invoice_no"),),
            primary_key_fields=("missing_column",),
        )


def test_source_entity_without_primary_key_is_valid():
    """MongoDB collections, CSV files and API payloads often have no PK."""
    entity = SourceEntity(
        entity_id="invoice_upload",
        source_name="invoices_2026.csv",
        normalized_name="invoices_2026",
        entity_kind=EntityKind.DATASET,
    )

    assert entity.has_primary_key is False
    assert entity.primary_key_fields == ()


# ============================================================
# SourceRelationship
# ============================================================

def test_source_relationship_foreign_key_is_valid():
    relationship = SourceRelationship(
        relationship_id="fk_invoice_customer",
        relationship_type=RelationshipType.FOREIGN_KEY,
        from_entity="fin_invoice",
        from_fields=("customer_ref",),
        to_entity="fin_customer",
        to_fields=("customer_id",),
        confidence=1.0,
    )

    assert relationship.confidence == 1.0
    assert relationship.to_json_dict()["relationship_type"] == "foreign_key"


def test_source_relationship_embedded_needs_no_target_fields():
    """MongoDB embedding is a first-class relationship, not a degenerate FK."""
    relationship = SourceRelationship(
        relationship_id="invoice_embeds_financial",
        relationship_type=RelationshipType.EMBEDDED,
        from_entity="invoices",
        from_fields=("financial",),
        to_entity="financial_block",
    )

    assert relationship.to_fields == ()


def test_source_relationship_requires_both_entities():
    with pytest.raises(ValidationError, match="must not be blank"):
        SourceRelationship(
            relationship_id="broken",
            relationship_type=RelationshipType.REFERENCE,
            from_entity="fin_invoice",
            from_fields=("customer_ref",),
            to_entity="",
            to_fields=("customer_id",),
        )


def test_source_relationship_requires_source_fields():
    with pytest.raises(ValidationError, match="at least one field"):
        SourceRelationship(
            relationship_id="broken",
            relationship_type=RelationshipType.FOREIGN_KEY,
            from_entity="a",
            from_fields=(),
            to_entity="b",
            to_fields=(),
        )


def test_source_relationship_key_based_must_pair_fields():
    with pytest.raises(ValidationError, match="one to one"):
        SourceRelationship(
            relationship_id="composite_mismatch",
            relationship_type=RelationshipType.FOREIGN_KEY,
            from_entity="a",
            from_fields=("x", "y"),
            to_entity="b",
            to_fields=("z",),
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0, -1.0])
def test_source_relationship_rejects_confidence_out_of_range(confidence):
    with pytest.raises(ValidationError, match="between 0.0 and 1.0"):
        SourceRelationship(
            relationship_id="inferred_link",
            relationship_type=RelationshipType.INFERRED,
            from_entity="a",
            from_fields=("x",),
            to_entity="b",
            to_fields=("y",),
            confidence=confidence,
        )


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_source_relationship_accepts_confidence_boundaries(confidence):
    relationship = SourceRelationship(
        relationship_id="inferred_link",
        relationship_type=RelationshipType.INFERRED,
        from_entity="a",
        from_fields=("x",),
        to_entity="b",
        to_fields=("y",),
        confidence=confidence,
    )

    assert relationship.confidence == confidence


# ============================================================
# SourceSchema across every supported source technology
# ============================================================

def test_source_schema_represents_postgresql():
    schema = SourceSchema(
        schema_id="finance_erp_pg_public_v1",
        source_system_id="finance_erp_pg",
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        discovered_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        entities=(
            SourceEntity(
                entity_id="fin_invoice",
                source_name="fin_invoice",
                normalized_name="fin_invoice",
                entity_kind=EntityKind.TABLE,
                namespace="public",
                primary_key_fields=("invoice_no",),
                fields=(
                    SourceField(
                        source_name="invoice_no",
                        normalized_name="invoice_no",
                        source_data_type="VARCHAR(20)",
                        normalized_data_type=FieldDataType.STRING,
                        is_primary_key=True,
                        nullable=False,
                        required=True,
                        is_unique=True,
                    ),
                    SourceField(
                        source_name="total_amount",
                        normalized_name="total_amount",
                        source_data_type="NUMERIC(12,2)",
                        normalized_data_type=FieldDataType.DECIMAL,
                    ),
                ),
            ),
            SourceEntity(
                entity_id="fin_customer",
                source_name="fin_customer",
                normalized_name="fin_customer",
                entity_kind=EntityKind.TABLE,
                primary_key_fields=("customer_id",),
                fields=(
                    SourceField(
                        source_name="customer_id",
                        normalized_name="customer_id",
                        source_data_type="VARCHAR(20)",
                        normalized_data_type=FieldDataType.STRING,
                        is_primary_key=True,
                        nullable=False,
                    ),
                ),
            ),
        ),
    )

    payload = schema.to_json_dict()
    assert payload["origin"] == "discovered"
    assert len(payload["entities"]) == 2
    assert payload["entities"][0]["entity_kind"] == "table"
    assert schema.entity_by_normalized_name("fin_invoice") is not None


def test_source_schema_represents_mongodb_with_nested_and_embedded_shapes():
    schema = SourceSchema(
        schema_id="billing_mongo_v1",
        source_system_id="billing_erp_mongo",
        schema_name="billing",
        # A Mongo shape is sampled, not declared, so the origin is inferred.
        origin=SchemaOrigin.INFERRED,
        entities=(
            SourceEntity(
                entity_id="invoices",
                source_name="invoices",
                normalized_name="invoices",
                entity_kind=EntityKind.COLLECTION,
                primary_key_fields=("id",),
                fields=(
                    SourceField(
                        source_name="_id",
                        normalized_name="id",
                        source_data_type="ObjectId",
                        normalized_data_type=FieldDataType.STRING,
                        is_primary_key=True,
                        nullable=False,
                    ),
                    SourceField(
                        source_name="total",
                        normalized_name="financial_total",
                        source_data_type="Decimal128",
                        normalized_data_type=FieldDataType.DECIMAL,
                        nested_path=("financial",),
                    ),
                    SourceField(
                        source_name="line_items",
                        normalized_name="line_items",
                        source_data_type="array<object>",
                        normalized_data_type=FieldDataType.ARRAY,
                        is_array=True,
                    ),
                ),
            ),
        ),
        relationships=(
            SourceRelationship(
                relationship_id="invoices_embeds_line_items",
                relationship_type=RelationshipType.EMBEDDED,
                from_entity="invoices",
                from_fields=("line_items",),
                to_entity="invoices",
            ),
        ),
    )

    nested = schema.entities[0].field_by_normalized_name("financial_total")
    assert nested is not None
    assert nested.access_path == ("financial", "total")
    assert schema.to_json_dict()["entities"][0]["entity_kind"] == "collection"


def test_source_schema_represents_csv():
    schema = SourceSchema(
        schema_id="invoice_upload_csv_v1",
        source_system_id="invoice_upload_csv",
        schema_name="invoice_upload",
        origin=SchemaOrigin.UPLOADED,
        entities=(
            SourceEntity(
                entity_id="invoices_2026",
                source_name="invoices_2026.csv",
                normalized_name="invoices_2026",
                entity_kind=EntityKind.DATASET,
                fields=(
                    SourceField(
                        source_name="Invoice No",
                        normalized_name="invoice_no",
                        source_data_type="text",
                        normalized_data_type=FieldDataType.STRING,
                        ordinal=0,
                    ),
                    SourceField(
                        source_name="Total Amount",
                        normalized_name="total_amount",
                        source_data_type="text",
                        normalized_data_type=FieldDataType.DECIMAL,
                        ordinal=1,
                    ),
                ),
            ),
        ),
    )

    entity = schema.entities[0]
    assert entity.has_primary_key is False
    # A CSV header is not a normalized name; both forms are preserved.
    assert entity.fields[0].source_name == "Invoice No"
    assert entity.fields[0].normalized_name == "invoice_no"


def test_source_schema_represents_openapi():
    schema = SourceSchema(
        schema_id="vendor_api_openapi_v1",
        source_system_id="vendor_erp_api",
        schema_name="components.schemas",
        origin=SchemaOrigin.API_SPEC,
        entities=(
            SourceEntity(
                entity_id="invoice_schema",
                source_name="Invoice",
                normalized_name="invoice",
                entity_kind=EntityKind.API_SCHEMA,
                namespace="#/components/schemas",
                fields=(
                    SourceField(
                        source_name="invoiceNumber",
                        normalized_name="invoice_number",
                        source_data_type="string",
                        normalized_data_type=FieldDataType.STRING,
                        required=True,
                        nullable=False,
                    ),
                    SourceField(
                        source_name="issuedAt",
                        normalized_name="issued_at",
                        source_data_type="string($date-time)",
                        normalized_data_type=FieldDataType.DATETIME,
                    ),
                    SourceField(
                        source_name="amount",
                        normalized_name="amount",
                        source_data_type="number($double)",
                        normalized_data_type=FieldDataType.DECIMAL,
                        nested_path=("totals",),
                    ),
                ),
                metadata={"openapi_ref": "#/components/schemas/Invoice"},
            ),
        ),
        relationships=(
            SourceRelationship(
                relationship_id="invoice_ref_customer",
                relationship_type=RelationshipType.REFERENCE,
                from_entity="invoice",
                from_fields=("customer_ref",),
                to_entity="invoice",
                to_fields=("invoice_number",),
                confidence=0.6,
                description="Proposed by a future inference step, not confirmed.",
            ),
        ),
        metadata={"openapi_version": "3.0.3"},
    )

    payload = schema.to_json_dict()
    assert payload["origin"] == "api_spec"
    assert payload["entities"][0]["entity_kind"] == "api_schema"
    assert payload["entities"][0]["namespace"] == "#/components/schemas"


def test_source_schema_represents_postman():
    """A Postman collection yields payload shapes inferred from examples."""
    schema = SourceSchema(
        schema_id="vendor_postman_v1",
        source_system_id="vendor_erp_postman",
        schema_name="Vendor ERP collection",
        origin=SchemaOrigin.INFERRED,
        entities=(
            SourceEntity(
                entity_id="create_invoice_request",
                source_name="POST /invoices :: request body",
                normalized_name="create_invoice_request",
                entity_kind=EntityKind.API_SCHEMA,
                fields=(
                    SourceField(
                        source_name="invoice",
                        normalized_name="invoice",
                        source_data_type="string",
                        normalized_data_type=FieldDataType.STRING,
                    ),
                    SourceField(
                        source_name="total",
                        normalized_name="total",
                        source_data_type="number",
                        normalized_data_type=FieldDataType.DECIMAL,
                        nested_path=("financial",),
                    ),
                ),
                metadata={
                    "postman_request": "Create Invoice",
                    "http_method": "POST",
                    "inferred_from_examples": 3,
                },
            ),
        ),
    )

    payload = schema.to_json_dict()
    assert payload["origin"] == "inferred"
    assert payload["entities"][0]["metadata"]["http_method"] == "POST"
    # The source name is a human API description, not an SQL identifier.
    assert schema.entities[0].source_name == "POST /invoices :: request body"


def test_source_schema_rejects_relationship_to_undeclared_entity():
    with pytest.raises(ValidationError, match="not declared in schema"):
        SourceSchema(
            schema_id="s1",
            source_system_id="sys",
            schema_name="public",
            origin=SchemaOrigin.DISCOVERED,
            entities=(
                SourceEntity(
                    entity_id="a", source_name="a", normalized_name="a"
                ),
            ),
            relationships=(
                SourceRelationship(
                    relationship_id="r1",
                    relationship_type=RelationshipType.FOREIGN_KEY,
                    from_entity="a",
                    from_fields=("x",),
                    to_entity="nonexistent",
                    to_fields=("y",),
                ),
            ),
        )


def test_source_schema_rejects_duplicate_entity_names():
    with pytest.raises(ValidationError, match="more than once"):
        SourceSchema(
            schema_id="s1",
            source_system_id="sys",
            schema_name="public",
            origin=SchemaOrigin.DISCOVERED,
            entities=(
                SourceEntity(entity_id="a1", source_name="a", normalized_name="a"),
                SourceEntity(entity_id="a2", source_name="A", normalized_name="a"),
            ),
        )


def test_schema_hash_is_deterministic_and_structure_sensitive():
    def build(total_type: str) -> SourceSchema:
        return SourceSchema(
            schema_id="s1",
            source_system_id="sys",
            schema_name="public",
            origin=SchemaOrigin.DISCOVERED,
            # Timestamps differ between the two builds on purpose.
            created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            entities=(
                SourceEntity(
                    entity_id="inv",
                    source_name="inv",
                    normalized_name="inv",
                    fields=(
                        SourceField(
                            source_name="total",
                            normalized_name="total",
                            source_data_type=total_type,
                            normalized_data_type=FieldDataType.DECIMAL,
                        ),
                    ),
                ),
            ),
        )

    first = build("DECIMAL(12,2)")
    second = build("DECIMAL(12,2)")
    changed = build("DECIMAL(18,4)")

    # Same structure hashes identically even though created_at differs.
    assert first.compute_schema_hash() == second.compute_schema_hash()
    # A vendor precision change is a structural change.
    assert first.compute_schema_hash() != changed.compute_schema_hash()


def test_schema_hash_is_sensitive_to_semantic_type():
    """Regression test (Phase 0-2 audit fix).

    ``compute_schema_hash()`` previously omitted ``semantic_type`` from its
    per-field projection while ``erp_pipeline.catalog.versioning.
    compare_schemas()`` already treated it as structural. That let a
    semantic_type-only change hash identically to the unchanged schema, which
    meant ``CatalogRepository.save_schema_snapshot()`` silently deduplicated
    it instead of creating a new catalog version. The hash must change
    whenever ``semantic_type`` changes, matching what ``compare_schemas()``
    already reports.
    """

    def build(semantic_type: str | None) -> SourceSchema:
        return SourceSchema(
            schema_id="s1",
            source_system_id="sys",
            schema_name="public",
            origin=SchemaOrigin.DISCOVERED,
            entities=(
                SourceEntity(
                    entity_id="customer",
                    source_name="customer",
                    normalized_name="customer",
                    fields=(
                        SourceField(
                            source_name="email",
                            normalized_name="email",
                            normalized_data_type=FieldDataType.STRING,
                            semantic_type=semantic_type,
                        ),
                    ),
                ),
            ),
        )

    without_semantic_type = build(None)
    with_semantic_type = build("email_address")

    assert (
        without_semantic_type.compute_schema_hash()
        != with_semantic_type.compute_schema_hash()
    )
