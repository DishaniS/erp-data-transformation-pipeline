"""The schema endpoint must report real ERP field types.

THE DEFECT THIS PINS
--------------------
The handler read ``getattr(field, "data_type", "")``. ``SourceField`` has no
``data_type`` attribute - it has ``source_data_type`` and
``normalized_data_type`` - so the default fired for every field of every
schema, and the endpoint reported an empty string as each field's type while
returning 200 OK.

That is the worst shape of bug for an integration contract: a consumer
generating typed ERP tooling from this response got a syntactically valid
document describing nothing.

These tests assert BOTH type views survive serialization, for the vendor types
a real ERP actually produces.
"""

from __future__ import annotations

import pytest

from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.api.serialization import schema_response
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.schemas.enums import (
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


def field(
    name: str,
    source_type: str | None,
    normalized: FieldDataType,
    **overrides,
) -> SourceField:
    payload = {
        "source_name": name,
        "normalized_name": name.lower(),
        "source_data_type": source_type,
        "normalized_data_type": normalized,
    }
    payload.update(overrides)

    return SourceField(**payload)


#: One field per vendor type a real ERP export produces.
FIELDS = (
    field("invoice_no", "VARCHAR(20)", FieldDataType.STRING,
          is_primary_key=True, nullable=False, required=True, ordinal=1),
    field("line_count", "INTEGER", FieldDataType.INTEGER, ordinal=2),
    field("total_amount", "NUMERIC(12,2)", FieldDataType.DECIMAL, ordinal=3),
    field("issued_at", "TIMESTAMP WITH TIME ZONE", FieldDataType.DATETIME,
          ordinal=4),
    field("is_settled", "BOOLEAN", FieldDataType.BOOLEAN, ordinal=5),
    field("customer_ref", "VARCHAR(20)", FieldDataType.STRING,
          is_unique=True, ordinal=6),
    field("line_items", "jsonb[]", FieldDataType.ARRAY, is_array=True,
          ordinal=7),
    field("total", "Decimal128", FieldDataType.DECIMAL,
          nested_path=("financial", "total"), ordinal=8),
    field("legacy_blob", "SOME_VENDOR_SPECIFIC_T", FieldDataType.UNKNOWN,
          description="A vendor type the normalizer does not recognize.",
          ordinal=9),
    field("no_declared_type", None, FieldDataType.UNKNOWN, ordinal=10),
)


@pytest.fixture
def source_schema() -> SourceSchema:
    invoice = SourceEntity(
        entity_id="fin_invoice",
        source_name="fin_invoice",
        normalized_name="fin_invoice",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("invoice_no",),
        fields=FIELDS,
    )
    customer = SourceEntity(
        entity_id="fin_customer",
        source_name="fin_customer",
        normalized_name="fin_customer",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("customer_no",),
        fields=(
            field("customer_no", "VARCHAR(20)", FieldDataType.STRING,
                  is_primary_key=True, nullable=False),
        ),
    )

    return SourceSchema(
        schema_id="finance_erp_public_v1",
        source_system_id="finance_erp",
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(invoice, customer),
        relationships=(
            SourceRelationship(
                relationship_id="fk_invoice_customer",
                relationship_type=RelationshipType.FOREIGN_KEY,
                from_entity="fin_invoice",
                to_entity="fin_customer",
                from_fields=("customer_ref",),
                to_fields=("customer_no",),
            ),
        ),
    )


@pytest.fixture
def client(source_schema, tmp_path):
    from fastapi.testclient import TestClient

    class SchemaOnlyServices(PipelineServices):
        def get_schema(self, schema_id):
            return source_schema

    orchestration = OrchestrationService(
        services=SchemaOnlyServices(),
        job_store=InMemoryJobStore(),
        executor=InlineJobExecutor(),
    )
    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=orchestration,
    )

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def invoice_fields(client):
    body = client.get("/v1/schemas/finance_erp_public_v1").json()
    entity = next(e for e in body["entities"] if e["source_name"] == "fin_invoice")

    return {item["source_name"]: item for item in entity["fields"]}


# ============================================================
# The defect itself
# ============================================================


def test_no_field_reports_an_empty_type(invoice_fields):
    """The exact symptom: every type came back as ''."""
    for name, item in invoice_fields.items():
        assert item["normalized_data_type"], name
        assert item["normalized_data_type"] != "", name


def test_the_response_has_no_data_type_key(invoice_fields):
    """The attribute that never existed must not reappear."""
    for item in invoice_fields.values():
        assert "data_type" not in item


# ============================================================
# Both type views survive
# ============================================================


@pytest.mark.parametrize(
    "name,source_type,normalized",
    [
        ("invoice_no", "VARCHAR(20)", "string"),
        ("line_count", "INTEGER", "integer"),
        ("total_amount", "NUMERIC(12,2)", "decimal"),
        ("issued_at", "TIMESTAMP WITH TIME ZONE", "datetime"),
        ("is_settled", "BOOLEAN", "boolean"),
        ("line_items", "jsonb[]", "array"),
        ("legacy_blob", "SOME_VENDOR_SPECIFIC_T", "unknown"),
    ],
)
def test_both_type_views_are_reported(invoice_fields, name, source_type, normalized):
    item = invoice_fields[name]

    assert item["source_data_type"] == source_type
    assert item["normalized_data_type"] == normalized


def test_vendor_precision_survives_serialization(invoice_fields):
    """``NUMERIC(12,2)`` is unrecoverable once discarded, and a consumer
    generating typed tooling needs it."""
    assert invoice_fields["total_amount"]["source_data_type"] == "NUMERIC(12,2)"


def test_an_unrecognized_vendor_type_keeps_its_own_spelling(invoice_fields):
    """The normalizer says ``unknown``; the vendor's word is still reported."""
    item = invoice_fields["legacy_blob"]

    assert item["normalized_data_type"] == "unknown"
    assert item["source_data_type"] == "SOME_VENDOR_SPECIFIC_T"


def test_a_field_with_no_declared_vendor_type_reports_null(invoice_fields):
    item = invoice_fields["no_declared_type"]

    assert item["source_data_type"] is None
    assert item["normalized_data_type"] == "unknown"


def test_the_normalized_type_is_the_enum_wire_value(invoice_fields):
    """Not ``FieldDataType.DECIMAL``, not ``'FieldDataType.DECIMAL'``."""
    assert invoice_fields["total_amount"]["normalized_data_type"] == "decimal"


# ============================================================
# The other structural facts
# ============================================================


def test_primary_key_and_nullability_are_reported(invoice_fields):
    item = invoice_fields["invoice_no"]

    assert item["is_primary_key"] is True
    assert item["nullable"] is False
    assert item["required"] is True


def test_uniqueness_is_reported(invoice_fields):
    assert invoice_fields["customer_ref"]["is_unique"] is True


def test_array_fields_are_flagged(invoice_fields):
    assert invoice_fields["line_items"]["is_array"] is True


def test_nested_paths_are_reported(invoice_fields):
    assert invoice_fields["total"]["nested_path"] == ["financial", "total"]


def test_a_flat_field_reports_no_nested_path(invoice_fields):
    assert invoice_fields["invoice_no"]["nested_path"] is None


def test_descriptions_and_ordinals_survive(invoice_fields):
    item = invoice_fields["legacy_blob"]

    assert item["description"]
    assert item["ordinal"] == 9


def test_semantic_type_is_present_but_unpopulated(invoice_fields):
    """The contract slot exists; no producer fills it yet. Reported honestly
    as null rather than omitted, so a consumer can see the gap."""
    assert invoice_fields["invoice_no"]["semantic_type"] is None


def test_normalized_names_are_reported(invoice_fields):
    assert invoice_fields["invoice_no"]["normalized_name"] == "invoice_no"


# ============================================================
# Entities and relationships
# ============================================================


def test_entities_report_their_kind_and_keys(client):
    body = client.get("/v1/schemas/finance_erp_public_v1").json()
    entity = next(e for e in body["entities"] if e["source_name"] == "fin_invoice")

    assert entity["entity_kind"] == "table"
    assert entity["primary_key_fields"] == ["invoice_no"]
    assert entity["field_count"] == len(FIELDS)


def test_the_relationship_graph_is_exposed_not_merely_counted(client):
    """A consumer previously saw that relationships existed but not what they
    were, which made ERP entity relationships unreconstructable."""
    body = client.get("/v1/schemas/finance_erp_public_v1").json()

    assert body["relationship_count"] == 1
    assert len(body["relationships"]) == 1

    relationship = body["relationships"][0]
    assert relationship["from_entity"] == "fin_invoice"
    assert relationship["from_fields"] == ["customer_ref"]
    assert relationship["to_entity"] == "fin_customer"
    assert relationship["to_fields"] == ["customer_no"]
    assert relationship["relationship_type"] == "foreign_key"


def test_relationship_field_names_mirror_the_contract(client):
    """Renaming in transit would make the contract and the response disagree."""
    body = client.get("/v1/schemas/finance_erp_public_v1").json()
    relationship = body["relationships"][0]

    for name in ("from_entity", "to_entity", "from_fields", "to_fields"):
        assert name in relationship


# ============================================================
# The serializer reads the contract explicitly
# ============================================================


def test_the_serializer_fails_loudly_on_a_contract_change():
    """The regression guard. A defensive ``getattr`` default is what turned a
    contract mismatch into an empty string; explicit access raises instead."""

    class NotASourceField:
        source_name = "x"
        normalized_name = "x"

    from erp_pipeline.api.serialization import field_response

    with pytest.raises(AttributeError):
        field_response(NotASourceField())


def test_the_serializer_and_the_endpoint_agree(source_schema, client):
    direct = schema_response(source_schema).model_dump()
    over_http = client.get("/v1/schemas/finance_erp_public_v1").json()

    assert direct["entities"] == over_http["entities"]
