"""OpenAPI 3.0 / 3.1 and Swagger 2.0 parsing, against real fixture files.

Covers detection, operations, parameters, request bodies, responses, component
schemas and type normalization. References and composition are covered in
``test_openapi_references.py``.
"""

from __future__ import annotations

import pytest

from erp_pipeline.api_specs import (
    ApiSpecFormat,
    ApiSpecOptions,
    ContractDirection,
    HttpMethod,
    MalformedSpecError,
    ParameterLocation,
    SpecFileError,
    StructureOrigin,
    UnsafeSpecContentError,
    UnsupportedSpecFormatError,
    UnsupportedSpecVersionError,
    describe_api_spec,
    parse_api_spec,
)
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    SchemaOrigin,
    SourceType,
)
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema


@pytest.fixture()
def basic(spec_fixtures):
    return parse_api_spec(spec_fixtures / "openapi_3_basic.json")


def fields_of(result, entity_name: str) -> dict:
    entity = result.schema.entity_by_normalized_name(entity_name)
    assert entity is not None, f"missing entity {entity_name!r}"
    return {field.normalized_name: field for field in entity.fields}


# ============================================================
# Detection (Step 5)
# ============================================================

@pytest.mark.parametrize(
    "filename,expected_format,expected_version",
    [
        ("openapi_3_basic.json", ApiSpecFormat.OPENAPI, "3.0.3"),
        ("openapi_3_basic.yaml", ApiSpecFormat.OPENAPI, "3.0.3"),
        ("openapi_31_basic.yaml", ApiSpecFormat.OPENAPI, "3.1.0"),
        ("swagger_2_basic.json", ApiSpecFormat.OPENAPI, "2.0"),
        ("postman_v21_basic.json", ApiSpecFormat.POSTMAN, "2.1"),
        ("postman_nested_folders.json", ApiSpecFormat.POSTMAN, "2.0"),
    ],
)
def test_format_and_version_are_detected_from_content(
    spec_fixtures, filename, expected_format, expected_version
):
    detection = describe_api_spec(spec_fixtures / filename)

    assert detection.spec_format is expected_format
    assert detection.spec_version == expected_version


def test_json_and_yaml_of_the_same_spec_agree(spec_fixtures):
    """The serialization format is not part of the contract."""
    from_json = parse_api_spec(spec_fixtures / "openapi_3_basic.json")
    from_yaml = parse_api_spec(spec_fixtures / "openapi_3_basic.yaml")

    assert from_json.schema.compute_schema_hash() == (
        from_yaml.schema.compute_schema_hash()
    )
    assert [e.normalized_name for e in from_json.schema.entities] == [
        e.normalized_name for e in from_yaml.schema.entities
    ]
    # The files differ, so their content identities must differ too.
    assert from_json.content_hash != from_yaml.content_hash


def test_detection_never_relies_on_the_filename(spec_fixtures, tmp_path):
    misleading = tmp_path / "definitely_a_postman_collection.json"
    misleading.write_bytes((spec_fixtures / "openapi_3_basic.json").read_bytes())

    assert describe_api_spec(misleading).spec_format is ApiSpecFormat.OPENAPI


def test_a_document_with_no_specification_marker_is_refused(spec_fixtures):
    with pytest.raises(UnsupportedSpecFormatError, match="no recognizable"):
        parse_api_spec(spec_fixtures / "not_a_spec.json")


def test_an_unsupported_openapi_version_is_refused(spec_fixtures):
    with pytest.raises(UnsupportedSpecVersionError) as excinfo:
        parse_api_spec(spec_fixtures / "unsupported_version.json")

    assert excinfo.value.declared_version == "4.1.0"


def test_an_unsupported_postman_version_is_refused(spec_fixtures):
    with pytest.raises(UnsupportedSpecVersionError):
        parse_api_spec(spec_fixtures / "postman_v1_unsupported.json")


def test_malformed_json_reports_a_position(spec_fixtures):
    with pytest.raises(MalformedSpecError) as excinfo:
        parse_api_spec(spec_fixtures / "malformed_openapi.json")

    assert excinfo.value.line is not None
    assert excinfo.value.column is not None


def test_unsafe_yaml_is_refused_before_it_can_construct_anything(spec_fixtures):
    """A spec containing !!python/object/apply is a security event, and is
    reported as one rather than as a generic parse failure."""
    with pytest.raises(UnsafeSpecContentError, match="python/"):
        parse_api_spec(spec_fixtures / "malicious_yaml.yaml")


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(SpecFileError):
        parse_api_spec(tmp_path / "nope.yaml")


def test_a_directory_is_refused(spec_fixtures):
    with pytest.raises(SpecFileError, match="directory"):
        parse_api_spec(spec_fixtures)


def test_the_size_limit_is_enforced_before_reading(spec_fixtures):
    with pytest.raises(SpecFileError, match="exceeds"):
        parse_api_spec(
            spec_fixtures / "openapi_3_basic.json",
            ApiSpecOptions(max_spec_size_bytes=32),
        )


# ============================================================
# Document metadata (Steps 6, 22)
# ============================================================

def test_specification_metadata_is_extracted(basic):
    specification = basic.specification

    assert specification.title == "ERP Invoicing API"
    assert specification.api_version == "1.4.0"
    assert specification.spec_version == "3.0.3"


def test_a_server_url_keeps_its_path_but_drops_its_query(basic):
    """A documented server URL can carry an api key in its query string."""
    assert basic.specification.server_paths == (
        "https://erp.example.invalid/api/v1",
    )


# ============================================================
# Operations (Step 7)
# ============================================================

def test_every_path_and_method_is_discovered(basic):
    found = {(op.method, op.path) for op in basic.operations}

    assert found == {
        (HttpMethod.GET, "/invoices"),
        (HttpMethod.POST, "/invoices"),
        (HttpMethod.GET, "/invoices/{id}"),
        (HttpMethod.DELETE, "/invoices/{id}"),
    }


def test_operation_ordering_is_deterministic(basic, spec_fixtures):
    """Paths alphabetically, then methods in a fixed order - never document
    key order, which would make a reparse look different."""
    ordering = [(op.path, op.method.value) for op in basic.operations]

    assert ordering == [
        ("/invoices", "get"),
        ("/invoices", "post"),
        ("/invoices/{id}", "get"),
        ("/invoices/{id}", "delete"),
    ]

    again = parse_api_spec(spec_fixtures / "openapi_3_basic.json")
    assert [(op.path, op.method.value) for op in again.operations] == ordering


def test_operation_identity_and_tags_are_preserved(basic):
    operation = basic.operation_by_key("get.invoices")

    assert operation is not None
    assert operation.operation_id == "listInvoices"
    assert operation.tags == ("invoices",)


def test_the_deprecated_flag_is_preserved(basic):
    cancel = next(
        op for op in basic.operations if op.operation_id == "cancelInvoice"
    )

    assert cancel.deprecated is True
    assert not any(
        op.deprecated for op in basic.operations if op.operation_id == "getInvoice"
    )


def test_unknown_path_item_keys_are_ignored_safely(spec_fixtures):
    """`summary`, `servers` and vendor extensions sit beside methods."""
    result = parse_api_spec(spec_fixtures / "openapi_3_inline.yaml")

    assert {op.method for op in result.operations} == {
        HttpMethod.GET, HttpMethod.POST,
    }


# ============================================================
# Parameters (Step 8)
# ============================================================

def test_query_and_header_parameters_are_parsed(basic):
    operation = basic.operation_by_key("get.invoices")

    query = {p.name: p for p in operation.parameters_in(ParameterLocation.QUERY)}
    header = {p.name: p for p in operation.parameters_in(ParameterLocation.HEADER)}

    assert set(query) == {"status", "limit"}
    assert query["limit"].data_type == FieldDataType.INTEGER.value
    assert query["limit"].source_data_type == "integer(int32)"
    assert header["X-Tenant-ID"].required is True


def test_path_level_parameters_reach_every_operation(basic):
    """`id` is declared once on the path item and applies to both methods."""
    for key in ("get.invoices_id", "delete.invoices_id"):
        operation = basic.operation_by_key(key)
        path_params = operation.parameters_in(ParameterLocation.PATH)
        assert [p.name for p in path_params] == ["id"]
        assert path_params[0].required is True


def test_an_operation_parameter_overrides_a_path_parameter():
    """OpenAPI requires the operation-level definition to win; getting this
    backwards would apply the wrong requiredness to an endpoint."""
    from erp_pipeline.api_specs import ApiParameter
    from erp_pipeline.api_specs.openapi_parser import merge_parameters

    shared = (
        ApiParameter(name="id", location=ParameterLocation.PATH, required=False),
        ApiParameter(name="tenant", location=ParameterLocation.QUERY),
    )
    own = (
        ApiParameter(name="id", location=ParameterLocation.PATH, required=True),
    )

    merged = {p.name: p for p in merge_parameters(shared, own)}

    assert len(merged) == 2
    assert merged["id"].required is True


def test_a_path_parameter_is_not_folded_into_the_response_schema(basic):
    """`GET /invoices/{id}` has a path parameter `id`; Invoice has no `id`
    property, and inventing one would misdescribe the payload."""
    operation = basic.operation_by_key("get.invoices_id")
    assert [p.name for p in operation.parameters_in(ParameterLocation.PATH)] == ["id"]

    invoice_fields = fields_of(basic, "invoice")
    assert "id" not in invoice_fields
    assert "invoiceid" in invoice_fields


# ============================================================
# Request bodies and responses (Steps 9, 10, 48, 49)
# ============================================================

def test_a_request_body_links_to_its_declared_schema(basic):
    operation = basic.operation_by_key("post.invoices")

    assert len(operation.request_bodies) == 1
    body = operation.request_bodies[0]
    assert body.media_type == "application/json"
    assert body.required is True
    assert body.entity_id == "createinvoicerequest"


def test_every_declared_status_code_is_parsed_not_just_200(basic):
    codes = {
        response.status_code
        for operation in basic.operations
        for response in operation.responses
    }

    assert {"200", "201", "204", "400", "404", "default"} <= codes


def test_multiple_content_types_are_not_collapsed(basic):
    operation = basic.operation_by_key("get.invoices")
    media_types = {r.media_type for r in operation.responses}

    assert media_types == {"application/json", "application/problem+json"}


def test_a_non_json_response_is_recorded_without_inventing_fields(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "openapi_3_inline.yaml")
    operation = result.operation_by_key("get.orders")

    text_response = next(
        r for r in operation.responses if r.media_type == "text/plain"
    )
    assert text_response.entity_id is None


def test_an_array_response_links_to_one_entity_and_flags_the_collection(basic):
    """"A list of Invoice" links to the Invoice contract rather than minting a
    near-duplicate entity that could drift."""
    listing = basic.operation_by_key("get.invoices")
    single = basic.operation_by_key("get.invoices_id")

    array_response = next(r for r in listing.responses if r.status_code == "200")
    single_response = next(r for r in single.responses if r.status_code == "200")

    assert array_response.entity_id == "invoice"
    assert array_response.is_collection is True
    assert single_response.entity_id == "invoice"
    assert single_response.is_collection is False


def test_request_and_response_stay_distinct_contracts(basic):
    """CreateInvoiceRequest has no invoiceId; Invoice does."""
    request_fields = fields_of(basic, "createinvoicerequest")
    response_fields = fields_of(basic, "invoice")

    assert "invoiceid" not in request_fields
    assert "invoiceid" in response_fields
    assert "customerid" in request_fields


def test_operation_to_entity_linkage_survives_conversion(basic):
    """Step 20: the SourceSchema must not lose which structure belongs to
    which endpoint, in which direction."""
    operation = basic.operation_by_key("post.invoices")

    assert operation.request_entity_ids == ("createinvoicerequest",)
    assert operation.response_entity_ids == ("invoice",)

    index = {
        entry["operation_key"]: entry
        for entry in basic.schema.metadata["operations"]
    }
    assert index["post.invoices"]["method"] == "post"
    assert index["post.invoices"]["path"] == "/invoices"
    assert index["post.invoices"]["request_entity_ids"] == ["createinvoicerequest"]


# ============================================================
# Component schemas and types (Steps 11, 12)
# ============================================================

def test_component_schemas_become_entities(basic):
    names = {entity.normalized_name for entity in basic.schema.entities}

    assert {"invoice", "customer", "invoiceline", "problem",
            "createinvoicerequest"} <= names


def test_entities_are_api_schemas_not_tables(basic):
    assert all(
        entity.entity_kind is EntityKind.API_SCHEMA
        for entity in basic.schema.entities
    )


@pytest.mark.parametrize(
    "field_name,expected_type,expected_source",
    [
        ("invoiceid", FieldDataType.STRING, "string"),
        ("issuedon", FieldDataType.DATE, "string(date)"),
        ("createdat", FieldDataType.DATETIME, "string(date-time)"),
        ("totalamount", FieldDataType.DECIMAL, "number(double)"),
        ("linecount", FieldDataType.INTEGER, "integer(int64)"),
        ("settled", FieldDataType.BOOLEAN, "boolean"),
        ("attachment", FieldDataType.BINARY, "string(binary)"),
        ("customer", FieldDataType.OBJECT, "object"),
        ("lines", FieldDataType.ARRAY, "array"),
    ],
)
def test_declared_types_normalize_onto_the_existing_enum(
    basic, field_name, expected_type, expected_source
):
    field = fields_of(basic, "invoice")[field_name]

    assert field.normalized_data_type is expected_type
    assert field.source_data_type == expected_source


def test_required_and_nullable_follow_the_declaration(basic):
    fields = fields_of(basic, "invoice")

    assert fields["invoiceid"].required is True
    assert fields["invoiceid"].nullable is False
    assert fields["note"].required is False
    assert fields["note"].nullable is True


def test_nested_objects_preserve_their_paths(basic):
    fields = fields_of(basic, "invoice")

    email = fields["customer.contact.email"]
    assert email.source_name == "email"
    assert email.nested_path == ("customer", "contact")
    assert email.access_path == ("customer", "contact", "email")


def test_arrays_of_objects_expose_element_fields(basic):
    fields = fields_of(basic, "invoice")

    assert fields["lines"].is_array is True
    assert fields["lines_.sku"].nested_path == ("lines", "[]")
    assert fields["lines_.quantity"].normalized_data_type is FieldDataType.INTEGER


def test_deeply_nested_inline_objects_are_expanded(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "openapi_3_nested.yaml")
    fields = fields_of(result, "purchaseorder")

    assert fields["supplier.address.geo.lat"].normalized_data_type is (
        FieldDataType.DECIMAL
    )
    assert fields["lines_.tags"].is_array is True


def test_openapi_31_type_arrays_are_understood(spec_fixtures):
    """3.1 expresses nullability as ["string", "null"] rather than
    nullable: true."""
    result = parse_api_spec(spec_fixtures / "openapi_31_basic.yaml")
    fields = fields_of(result, "payment")

    assert fields["note"].normalized_data_type is FieldDataType.STRING
    assert fields["note"].nullable is True
    # A genuine multi-type union has no honest common type.
    assert fields["mixedfield"].normalized_data_type is FieldDataType.UNKNOWN


# ============================================================
# Inline schemas (Step 47)
# ============================================================

def test_inline_schemas_get_deterministic_names(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "openapi_3_inline.yaml")
    names = {entity.normalized_name for entity in result.schema.entities}

    assert "get_orders_response_200_json" in names
    assert "post_orders_request" in names
    assert "post_orders_response_201" in names


def test_inline_schema_names_do_not_change_between_runs(spec_fixtures):
    first = parse_api_spec(spec_fixtures / "openapi_3_inline.yaml")
    second = parse_api_spec(spec_fixtures / "openapi_3_inline.yaml")

    assert [e.normalized_name for e in first.schema.entities] == [
        e.normalized_name for e in second.schema.entities
    ]


def test_inline_schema_fields_are_still_fully_described(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "openapi_3_inline.yaml")
    fields = fields_of(result, "post_orders_request")

    assert fields["amount"].normalized_data_type is FieldDataType.DECIMAL
    assert fields["amount"].required is True
    assert fields["reference"].required is False


# ============================================================
# Swagger 2 (Step 5)
# ============================================================

@pytest.fixture()
def swagger(spec_fixtures):
    return parse_api_spec(spec_fixtures / "swagger_2_basic.json")


def test_swagger_2_definitions_become_entities(swagger):
    fields = fields_of(swagger, "legacycustomer")

    assert fields["customernumber"].required is True
    assert fields["creditlimit"].normalized_data_type is FieldDataType.DECIMAL
    assert fields["registeredon"].normalized_data_type is FieldDataType.DATE


def test_swagger_2_body_parameter_becomes_a_request_body(swagger):
    operation = swagger.operation_by_key("post.customers")

    assert len(operation.request_bodies) == 1
    assert operation.request_bodies[0].entity_id == "legacycustomer"
    assert operation.request_bodies[0].required is True
    # The body parameter must not also appear as an ordinary parameter.
    assert not operation.parameters_in(ParameterLocation.BODY)


def test_swagger_2_formdata_becomes_a_structured_request(swagger):
    operation = swagger.operation_by_key("post.customers_upload")
    body = operation.request_bodies[0]

    assert body.media_type == "multipart/form-data"
    fields = fields_of(swagger, body.entity_id)
    assert fields["filename"].required is True
    assert fields["rowcount"].normalized_data_type is FieldDataType.INTEGER


def test_swagger_2_base_path_is_recorded(swagger):
    assert swagger.specification.server_paths == ("/legacy/v1",)


def test_swagger_2_query_parameter_types_come_off_the_parameter(swagger):
    operation = swagger.operation_by_key("get.customers")
    active = operation.parameters_in(ParameterLocation.QUERY)[0]

    assert active.name == "active"
    assert active.data_type == FieldDataType.BOOLEAN.value


# ============================================================
# SourceSchema output (Steps 19, 39)
# ============================================================

def test_the_output_is_the_phase_1_contract(basic):
    assert isinstance(basic.schema, SourceSchema)
    assert all(isinstance(e, SourceEntity) for e in basic.schema.entities)
    assert all(
        isinstance(f, SourceField)
        for e in basic.schema.entities
        for f in e.fields
    )


def test_a_declared_openapi_contract_uses_the_api_spec_origin(basic):
    """Phase 1 provides an origin for exactly this case: neither discovered
    from a live system nor inferred from samples, but declared."""
    assert basic.schema.origin is SchemaOrigin.API_SPEC
    assert basic.schema.metadata["schema_claim"] == "declared_api_contract"
    assert all(
        entity.metadata["structure_origin"] == StructureOrigin.DECLARED.value
        for entity in basic.schema.entities
    )


def test_the_source_type_is_the_frozen_phase_1_value(basic):
    from erp_pipeline.api_specs import ApiSpecificationService

    system = ApiSpecificationService().source_system(ApiSpecFormat.OPENAPI)

    assert system.source_type is SourceType.OPENAPI


def test_no_primary_keys_or_uniqueness_are_invented(basic):
    for entity in basic.schema.entities:
        assert entity.primary_key_fields == ()
        assert not any(field.is_primary_key for field in entity.fields)
        assert not any(field.is_unique for field in entity.fields)


def test_no_semantic_types_are_inferred(basic):
    """Step 42/57: customerId gets a TYPE, never a canonical meaning."""
    assert all(
        field.semantic_type is None
        for entity in basic.schema.entities
        for field in entity.fields
    )


def test_field_ordering_is_deterministic(spec_fixtures):
    first = parse_api_spec(spec_fixtures / "openapi_3_basic.json")
    second = parse_api_spec(spec_fixtures / "openapi_3_basic.json")

    for left, right in zip(first.schema.entities, second.schema.entities):
        assert [f.normalized_name for f in left.fields] == [
            f.normalized_name for f in right.fields
        ]


def test_a_spec_with_no_paths_but_schemas_still_parses(spec_fixtures, tmp_path):
    spec = tmp_path / "models_only.json"
    spec.write_text(
        '{"openapi": "3.0.3", "info": {"title": "Models"}, '
        '"components": {"schemas": {"Thing": {"type": "object", '
        '"properties": {"id": {"type": "string"}}}}}}',
        encoding="utf-8",
    )

    result = parse_api_spec(spec)

    assert result.operations == ()
    assert [e.normalized_name for e in result.schema.entities] == ["thing"]


def test_a_document_with_neither_paths_nor_schemas_is_refused(spec_fixtures):
    from erp_pipeline.api_specs import SpecStructureError

    with pytest.raises(SpecStructureError):
        parse_api_spec(spec_fixtures / "empty_openapi.json")
