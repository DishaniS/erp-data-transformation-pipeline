"""Postman collection parsing and example-driven structure inference.

Postman declares no types, so every structural claim here is an observation
over saved examples - and these tests check that the parser says so rather
than presenting an inference as a declaration.
"""

from __future__ import annotations

import pytest

from erp_pipeline.api_specs import (
    ApiSpecFormat,
    ApiSpecOptions,
    HttpMethod,
    ParameterLocation,
    SpecStructureError,
    StructureOrigin,
    parse_api_spec,
)
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SchemaOrigin
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema


def fields_of(result, entity_name: str) -> dict:
    entity = result.schema.entity_by_normalized_name(entity_name)
    assert entity is not None, f"missing entity {entity_name!r}"
    return {field.normalized_name: field for field in entity.fields}


# ============================================================
# Collection structure (Steps 23, 24)
# ============================================================

def test_a_v21_collection_is_parsed(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_v21_basic.json")

    assert result.spec_format is ApiSpecFormat.POSTMAN
    assert result.specification.spec_version == "2.1"
    assert result.specification.title == "ERP API Collection"
    assert len(result.operations) == 2


def test_a_v20_collection_is_parsed(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_nested_folders.json")

    assert result.specification.spec_version == "2.0"
    assert len(result.operations) == 4


def test_nested_folders_are_preserved(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_nested_folders.json")

    folders = {op.operation_id: op.folder_path for op in result.operations}

    assert folders["Get Invoice"] in (("Invoices",), ("Customers",))
    assert folders["Monthly Totals"] == ("Invoices", "Reporting")


def test_the_same_request_name_in_two_folders_stays_distinct(spec_fixtures):
    """`Invoices/Get Invoice` and `Customers/Get Invoice` are different
    operations, and the folder is what tells them apart."""
    result = parse_api_spec(spec_fixtures / "postman_nested_folders.json")

    keys = [op.operation_key for op in result.operations]

    assert len(keys) == len(set(keys))
    assert "get.invoices.get_invoice" in keys
    assert "get.customers.get_invoice" in keys


def test_request_methods_are_parsed(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_nested_folders.json")

    methods = {op.operation_id: op.method for op in result.operations}
    assert methods["Create Invoice"] is HttpMethod.POST
    assert methods["Monthly Totals"] is HttpMethod.GET


def test_a_collection_with_no_items_is_refused(spec_fixtures):
    with pytest.raises(SpecStructureError, match="item"):
        parse_api_spec(spec_fixtures / "postman_no_items.json")


def test_item_order_is_stable_across_runs(spec_fixtures):
    first = parse_api_spec(spec_fixtures / "postman_nested_folders.json")
    second = parse_api_spec(spec_fixtures / "postman_nested_folders.json")

    assert [op.operation_key for op in first.operations] == [
        op.operation_key for op in second.operations
    ]


# ============================================================
# URLs, parameters and variables (Steps 25, 26, 29)
# ============================================================

def test_a_structured_url_becomes_a_path_template(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_v21_basic.json")
    operation = next(op for op in result.operations if op.operation_id == "Get Invoice")

    assert operation.path == "/invoices/:id"


def test_a_string_url_becomes_a_path_template(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_v21_basic.json")
    operation = next(
        op for op in result.operations if op.operation_id == "List Invoices"
    )

    assert operation.path == "/invoices"


def test_query_parameter_names_are_kept_and_values_are_not(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_v21_basic.json")
    operation = next(op for op in result.operations if op.operation_id == "Get Invoice")

    query = operation.parameters_in(ParameterLocation.QUERY)
    assert [p.name for p in query] == ["expand"]
    assert not hasattr(query[0], "value")


def test_query_names_are_recovered_from_a_raw_url_string(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_v21_basic.json")
    operation = next(
        op for op in result.operations if op.operation_id == "List Invoices"
    )

    assert {p.name for p in operation.parameters_in(ParameterLocation.QUERY)} == {
        "status", "limit",
    }
    assert "PAID" not in str(result.to_dict())


def test_path_variables_are_parsed(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_v21_basic.json")
    operation = next(op for op in result.operations if op.operation_id == "Get Invoice")

    path_params = operation.parameters_in(ParameterLocation.PATH)
    assert [p.name for p in path_params] == ["id"]
    assert path_params[0].required is True


def test_variable_names_are_collected_without_their_values(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_variables.json")

    names = set(result.specification.variable_names)
    assert {"baseUrl", "invoiceId", "tenantCode"} <= names
    # The declared value of invoiceId is never persisted.
    assert "INV-4242" not in str(result.to_dict())


def test_a_templated_path_keeps_its_variable_placeholder(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_variables.json")
    operation = result.operations[0]

    assert operation.path == "/invoices/{{invoiceId}}"


def test_headers_are_named_and_classified(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_v21_basic.json")
    operation = next(op for op in result.operations if op.operation_id == "Get Invoice")

    headers = {h.name: h for h in operation.parameters_in(ParameterLocation.HEADER)}
    assert set(headers) == {"Accept", "X-Tenant-ID"}
    assert headers["Accept"].enabled is True
    assert headers["X-Tenant-ID"].is_sensitive_name is False


# ============================================================
# Request bodies (Step 30)
# ============================================================

def test_a_raw_json_body_is_structurally_inferred(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_raw_body.json")
    operation = next(
        op for op in result.operations if op.operation_id == "Create Invoice"
    )

    body = operation.request_bodies[0]
    assert body.media_type == "application/json"
    assert body.structure_origin is StructureOrigin.INFERRED_FROM_EXAMPLES

    fields = fields_of(result, body.entity_id)
    assert fields["customerid"].normalized_data_type is FieldDataType.STRING
    assert fields["totalamount"].normalized_data_type is FieldDataType.DECIMAL
    assert fields["urgent"].normalized_data_type is FieldDataType.BOOLEAN
    assert fields["lines"].is_array is True
    assert fields["lines_.quantity"].normalized_data_type is FieldDataType.INTEGER


def test_an_invalid_json_body_is_reported_not_guessed(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_raw_body.json")
    operation = next(
        op for op in result.operations if op.operation_id == "Invalid JSON Body"
    )

    assert operation.request_bodies[0].entity_id is None
    assert "invalid_json_body" in {w.category for w in result.warnings}


def test_formdata_fields_are_described_as_text(spec_fixtures):
    """A form encoding transmits text; claiming otherwise would invent type
    information the collection does not contain."""
    result = parse_api_spec(spec_fixtures / "postman_formdata.json")
    operation = next(
        op for op in result.operations if op.operation_id == "Upload Invoices"
    )

    body = operation.request_bodies[0]
    assert body.media_type == "multipart/form-data"
    assert body.structure_origin is StructureOrigin.INFERRED_FROM_PARAMETERS

    fields = fields_of(result, body.entity_id)
    assert set(fields) == {"period", "dryrun", "file", "legacyflag"}
    assert all(
        field.normalized_data_type is FieldDataType.STRING
        for field in fields.values()
    )
    # A disabled field is present but not required.
    assert fields["legacyflag"].required is False


def test_a_file_form_field_is_recorded_without_reading_the_file(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_formdata.json")
    operation = next(
        op for op in result.operations if op.operation_id == "Upload Invoices"
    )
    entity = result.schema.entity_by_normalized_name(
        operation.request_bodies[0].entity_id
    )

    assert entity.metadata["file_fields"] == ["file"]
    # The local path in the fixture's "src" is never touched or stored.
    assert "Desktop" not in str(result.to_dict())


def test_urlencoded_fields_are_described(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_formdata.json")
    operation = next(
        op for op in result.operations if op.operation_id == "Submit Form"
    )

    body = operation.request_bodies[0]
    assert body.media_type == "application/x-www-form-urlencoded"
    assert set(fields_of(result, body.entity_id)) == {"customernumber", "totalamt"}


# ============================================================
# Response example inference (Steps 31, 32, 33, 34)
# ============================================================

@pytest.fixture()
def examples(spec_fixtures):
    return parse_api_spec(spec_fixtures / "postman_response_examples.json")


def test_a_saved_response_becomes_a_described_contract(examples):
    operation = next(
        op for op in examples.operations if op.operation_id == "Get Invoice"
    )

    success = next(r for r in operation.responses if r.status_code == "200")
    assert success.entity_id == "get_invoice_response_200"
    assert success.examples_observed == 1
    assert success.structure_origin is StructureOrigin.INFERRED_FROM_EXAMPLES


def test_example_fields_and_types_are_inferred(examples):
    fields = fields_of(examples, "get_invoice_response_200")

    assert fields["invoiceid"].normalized_data_type is FieldDataType.STRING
    assert fields["customerid"].normalized_data_type is FieldDataType.STRING
    assert fields["totalamount"].normalized_data_type is FieldDataType.INTEGER
    assert fields["status"].normalized_data_type is FieldDataType.STRING
    assert fields["settled"].normalized_data_type is FieldDataType.BOOLEAN


def test_example_values_are_never_stored(examples):
    """Step 31: infer invoiceId is a STRING; never store "INV-1"."""
    payload = str(examples.to_dict())

    for value in ("INV-1", "CUS-5", "PAID", "SKU-A", "urgent"):
        assert value not in payload


def test_nested_example_objects_are_inferred(examples):
    fields = fields_of(examples, "get_invoice_response_200")

    assert fields["customer"].normalized_data_type is FieldDataType.OBJECT
    assert fields["customer.contact.email"].nested_path == ("customer", "contact")


def test_example_arrays_of_objects_are_inferred(examples):
    fields = fields_of(examples, "get_invoice_response_200")

    assert fields["lines"].is_array is True
    assert fields["lines"].source_data_type == "array<object>"
    assert fields["lines_.sku"].normalized_data_type is FieldDataType.STRING
    assert fields["lines_.quantity"].normalized_data_type is FieldDataType.INTEGER


def test_an_array_of_primitives_is_inferred(examples):
    tags = fields_of(examples, "get_invoice_response_200")["tags"]

    assert tags.is_array is True
    assert tags.source_data_type == "array<string>"


def test_a_null_example_value_yields_no_type_claim(examples):
    note = fields_of(examples, "get_invoice_response_200")["note"]

    assert note.normalized_data_type is FieldDataType.UNKNOWN
    assert note.nullable is True


def test_a_root_array_response_describes_its_elements(examples):
    """Step 33: the response root is array<object>; the fields live on the
    element."""
    fields = fields_of(examples, "list_invoices_response_200")
    entity = examples.schema.entity_by_normalized_name("list_invoices_response_200")

    assert entity.metadata["root_type"] == "array"
    assert fields["invoiceid"].normalized_data_type is FieldDataType.STRING
    assert fields["amount"].normalized_data_type is FieldDataType.INTEGER


def test_responses_are_grouped_by_status_code(examples):
    """A 200 body and a 404 body are different contracts and must not merge."""
    operation = next(
        op for op in examples.operations if op.operation_id == "Get Invoice"
    )
    codes = {r.status_code: r.entity_id for r in operation.responses}

    assert set(codes) == {"200", "404"}
    assert codes["200"] != codes["404"]
    assert set(fields_of(examples, codes["404"])) == {"type", "title", "status"}


# ============================================================
# Multiple, disagreeing examples (Step 32)
# ============================================================

@pytest.fixture()
def mixed(spec_fixtures):
    return parse_api_spec(spec_fixtures / "postman_mixed_examples.json")


def test_multiple_examples_are_combined_not_discarded(mixed):
    operation = next(
        op for op in mixed.operations if op.operation_id == "Get Payment"
    )
    response = next(r for r in operation.responses if r.status_code == "200")

    assert response.examples_observed == 2


def test_a_field_present_in_only_one_example_records_its_presence(mixed):
    message = fields_of(mixed, "get_payment_response_200")["message"]

    assert message.metadata["observed"]["presence_ratio"] == 0.5
    assert message.required is False
    assert message.nullable is True


def test_a_field_with_disagreeing_types_keeps_both(mixed):
    """Structural disagreement between examples is information, not noise."""
    identifier = fields_of(mixed, "get_payment_response_200")["id"]

    assert identifier.metadata["mixed_types"] is True
    assert identifier.metadata["json_type_distribution"] == {
        "integer": 1, "string": 1,
    }
    assert identifier.source_data_type == "mixed<integer|string>"
    # No single type is true of both, so none is claimed.
    assert identifier.normalized_data_type is FieldDataType.UNKNOWN


def test_a_field_present_in_every_example_is_observed_required(mixed):
    status = fields_of(mixed, "get_payment_response_200")["status"]

    assert status.required is True
    assert status.metadata["observed"]["presence_ratio"] == 1.0
    # The claim travels with the sample size it rests on.
    assert status.metadata["observed"]["examples_sampled"] == 2


def test_a_non_json_response_produces_no_invented_fields(mixed):
    """Step 34: HTML is not JSON, and pretending otherwise would fabricate."""
    operation = next(
        op for op in mixed.operations if op.operation_id == "Get Receipt"
    )
    response = operation.responses[0]

    assert response.media_type == "text/html"
    assert response.entity_id is None
    assert "non_json_response_example" in {w.category for w in mixed.warnings}


def test_the_example_budget_is_enforced(spec_fixtures):
    result = parse_api_spec(
        spec_fixtures / "postman_mixed_examples.json",
        ApiSpecOptions(max_examples_per_operation=1),
    )
    response = next(
        r for op in result.operations for r in op.responses
        if r.status_code == "200" and r.entity_id
    )

    assert response.examples_observed == 1


# ============================================================
# Scripts (Step 35)
# ============================================================

def test_a_script_is_recorded_but_never_executed(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_auth_secrets.json")
    operation = result.operations[0]

    assert operation.script_present is True
    # The script body sets a token; nothing from it is retained.
    payload = str(result.to_dict())
    assert "pm.environment.set" not in payload
    assert "console.log" not in payload


# ============================================================
# SourceSchema output (Steps 17, 39)
# ============================================================

def test_postman_produces_the_phase_1_contract(examples):
    assert isinstance(examples.schema, SourceSchema)
    assert all(isinstance(e, SourceEntity) for e in examples.schema.entities)
    assert all(
        isinstance(f, SourceField)
        for e in examples.schema.entities
        for f in e.fields
    )
    assert all(
        e.entity_kind is EntityKind.API_SCHEMA for e in examples.schema.entities
    )


def test_postman_structures_are_marked_inferred_not_declared(examples):
    """A Postman collection declares no types, so claiming API_SPEC origin
    would misrepresent where the structure came from."""
    assert examples.schema.origin is SchemaOrigin.INFERRED
    assert examples.schema.metadata["schema_claim"] == "observed_from_examples"
    assert all(
        entity.metadata["structure_origin"]
        in (
            StructureOrigin.INFERRED_FROM_EXAMPLES.value,
            StructureOrigin.INFERRED_FROM_PARAMETERS.value,
        )
        for entity in examples.schema.entities
    )


def test_entity_metadata_records_how_many_examples_it_rests_on(examples):
    entity = examples.schema.entity_by_normalized_name("get_invoice_response_200")

    assert entity.metadata["examples_observed"] == 1
    assert entity.metadata["media_type"] == "application/json"
    assert entity.metadata["status_code"] == "200"


def test_no_keys_relationships_or_semantics_are_invented(examples):
    assert examples.schema.relationships == ()

    for entity in examples.schema.entities:
        assert entity.primary_key_fields == ()
        assert not any(f.is_primary_key for f in entity.fields)
        assert all(f.semantic_type is None for f in entity.fields)


def test_request_and_response_entities_stay_distinct(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "postman_raw_body.json")

    directions = {
        entity.normalized_name: entity.metadata["contract_direction"]
        for entity in result.schema.entities
    }
    assert "request" in directions.values()


def test_repeated_parsing_is_deterministic(spec_fixtures):
    first = parse_api_spec(spec_fixtures / "postman_response_examples.json")
    second = parse_api_spec(spec_fixtures / "postman_response_examples.json")

    assert first.schema.schema_id == second.schema.schema_id
    assert first.schema.compute_schema_hash() == second.schema.compute_schema_hash()
    assert first.content_hash == second.content_hash
