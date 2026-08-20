"""``$ref`` resolution, recursion safety, composition, enums and security.

The parts of OpenAPI where a naive parser either loops forever, fetches
something it should not, or quietly invents structure that the specification
never declared.
"""

from __future__ import annotations

import pytest

from erp_pipeline.api_specs import (
    ApiSpecOptions,
    RefStatus,
    ReferenceResolver,
    is_remote_reference,
    parse_api_spec,
    reference_target_name,
)
from erp_pipeline.schemas.enums import FieldDataType, RelationshipType


def fields_of(result, entity_name: str) -> dict:
    entity = result.schema.entity_by_normalized_name(entity_name)
    assert entity is not None, f"missing entity {entity_name!r}"
    return {field.normalized_name: field for field in entity.fields}


# ============================================================
# Local references (Step 13)
# ============================================================

def test_a_local_ref_links_entities_instead_of_duplicating_them(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "openapi_3_refs.yaml")

    names = {entity.normalized_name for entity in result.schema.entities}
    assert {"customer", "address"} <= names

    # Customer.address is described once, on the Address entity.
    customer = fields_of(result, "customer")
    assert customer["address"].normalized_data_type is FieldDataType.OBJECT
    assert customer["address"].metadata["ref_target"] == "Address"


def test_referenced_properties_are_still_expanded_in_place(spec_fixtures):
    """A consumer reading Customer needs to know the shape it will receive."""
    customer = fields_of(parse_api_spec(spec_fixtures / "openapi_3_refs.yaml"),
                         "customer")

    assert customer["address.city"].normalized_data_type is FieldDataType.STRING
    assert customer["address.country"].nested_path == ("address",)


def test_a_declared_ref_becomes_a_relationship(spec_fixtures):
    """Step 21: this is a DECLARED structural reference, not a name guess."""
    result = parse_api_spec(spec_fixtures / "openapi_3_refs.yaml")

    links = {
        (r.from_entity, r.from_fields, r.to_entity)
        for r in result.schema.relationships
    }
    assert ("customer", ("address",), "address") in links
    assert all(
        r.relationship_type is RelationshipType.EMBEDDED
        for r in result.schema.relationships
    )
    assert all(r.confidence == 1.0 for r in result.schema.relationships)


def test_no_relationship_is_invented_from_a_field_name(spec_fixtures):
    """`customerId` is a name, not a foreign key."""
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json")

    invoice_links = {
        r.from_fields for r in result.schema.relationships
        if r.from_entity == "invoice"
    }
    # customer and lines are declared $refs; customerId in the request is not.
    assert ("customerid",) not in invoice_links

    request_links = {
        r.from_fields for r in result.schema.relationships
        if r.from_entity == "createinvoicerequest"
    }
    assert ("customerid",) not in request_links
    assert ("lines",) in request_links


def test_relationships_can_be_switched_off(spec_fixtures):
    result = parse_api_spec(
        spec_fixtures / "openapi_3_refs.yaml",
        ApiSpecOptions(include_reference_relationships=False),
    )

    assert result.schema.relationships == ()


# ============================================================
# Remote and missing references (Steps 13, 43)
# ============================================================

def test_a_remote_ref_is_never_fetched(spec_fixtures):
    """The single most important safety property in this phase."""
    result = parse_api_spec(spec_fixtures / "openapi_3_refs.yaml")

    categories = [warning.category for warning in result.warnings]
    assert "remote_reference_not_fetched" in categories

    warning = next(
        w for w in result.warnings if w.category == "remote_reference_not_fetched"
    )
    assert "no network access" in warning.message

    external = result.operation_by_key("get.external")
    assert external.responses[0].entity_id is None


def test_a_dangling_local_ref_is_reported_not_fabricated(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "openapi_3_refs.yaml")

    assert "unresolved_contract_reference" in [
        warning.category for warning in result.warnings
    ]
    missing = result.operation_by_key("get.missing")
    assert missing.responses[0].entity_id is None


@pytest.mark.parametrize(
    "pointer,expected",
    [
        ("#/components/schemas/Invoice", False),
        ("#/definitions/Customer", False),
        ("https://example.invalid/common.yaml#/Customer", True),
        ("http://example.invalid/common.yaml", True),
        ("//example.invalid/common.yaml", True),
        ("common.yaml#/Customer", True),
        ("./sibling.yaml#/Thing", True),
    ],
)
def test_remote_reference_detection(pointer, expected):
    assert is_remote_reference(pointer) is expected


@pytest.mark.parametrize(
    "pointer,expected",
    [
        ("#/components/schemas/Invoice", "Invoice"),
        ("#/definitions/Legacy~1Customer", "Legacy/Customer"),
        ("#/components/schemas/With~0Tilde", "With~Tilde"),
        ("#/components/schemas/Percent%20Name", "Percent Name"),
        ("no-fragment", None),
    ],
)
def test_reference_target_names_are_decoded(pointer, expected):
    assert reference_target_name(pointer) == expected


# ============================================================
# Recursion (Step 14)
# ============================================================

def test_a_recursive_schema_terminates(spec_fixtures):
    """Employee.manager -> Employee is a legitimate, common model."""
    result = parse_api_spec(spec_fixtures / "openapi_3_recursive.yaml")
    fields = fields_of(result, "employee")

    assert "employeeid" in fields
    assert "manager.employeeid" in fields
    # The cycle is recorded rather than expanded forever.
    assert "circular_reference" in {w.category for w in result.warnings}


def test_recursion_still_describes_the_useful_first_levels(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "openapi_3_recursive.yaml")
    fields = fields_of(result, "employee")

    assert fields["manager"].normalized_data_type is FieldDataType.OBJECT
    assert fields["reports"].is_array is True
    assert "reports_.employeeid" in fields


def test_a_self_cycle_is_governed_by_cycle_detection_not_the_depth_budget(
    spec_fixtures,
):
    """Employee -> Employee revisits immediately, so expansion stops at the
    cycle regardless of how generous max_reference_depth is. The two guards
    are complementary, not interchangeable."""
    shallow = parse_api_spec(
        spec_fixtures / "openapi_3_recursive.yaml",
        ApiSpecOptions(max_reference_depth=1),
    )
    deep = parse_api_spec(
        spec_fixtures / "openapi_3_recursive.yaml",
        ApiSpecOptions(max_reference_depth=8),
    )

    assert len(fields_of(shallow, "employee")) == len(fields_of(deep, "employee"))
    assert "circular_reference" in {w.category for w in deep.warnings}


def test_the_reference_depth_budget_bounds_a_chain_of_distinct_refs(tmp_path):
    """A -> B -> C -> D never revisits a node, so only the depth budget can
    stop it."""
    spec = tmp_path / "chain.json"
    spec.write_text(
        """
        {"openapi": "3.0.3", "info": {"title": "Chain"},
         "components": {"schemas": {
            "A": {"type": "object", "properties": {
                "b": {"$ref": "#/components/schemas/B"}}},
            "B": {"type": "object", "properties": {
                "c": {"$ref": "#/components/schemas/C"}}},
            "C": {"type": "object", "properties": {
                "d": {"$ref": "#/components/schemas/D"}}},
            "D": {"type": "object", "properties": {"leaf": {"type": "string"}}}
         }}}
        """,
        encoding="utf-8",
    )

    deep = parse_api_spec(spec, ApiSpecOptions(max_reference_depth=8))
    shallow = parse_api_spec(spec, ApiSpecOptions(max_reference_depth=1))

    assert "b.c.d.leaf" in fields_of(deep, "a")
    assert "b.c.d.leaf" not in fields_of(shallow, "a")
    assert "reference_depth_exceeded" in {w.category for w in shallow.warnings}


def test_the_resolver_reports_each_outcome_distinctly():
    document = {"components": {"schemas": {"A": {"type": "object"}}}}
    resolver = ReferenceResolver(document, max_depth=2)

    assert resolver.resolve("#/components/schemas/A").status is RefStatus.RESOLVED
    assert resolver.resolve("#/components/schemas/Nope").status is RefStatus.NOT_FOUND
    assert resolver.resolve("https://x.invalid#/A").status is (
        RefStatus.REMOTE_NOT_FETCHED
    )

    resolver.enter("#/components/schemas/A")
    assert resolver.resolve("#/components/schemas/A").status is RefStatus.CIRCULAR
    resolver.leave()
    assert resolver.resolve("#/components/schemas/A").status is RefStatus.RESOLVED


def test_two_independent_fields_may_reference_the_same_schema(spec_fixtures):
    """Cycle detection must not mistake reuse for recursion."""
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json")
    invoice = fields_of(result, "invoice")
    request = fields_of(result, "createinvoicerequest")

    assert "lines_.sku" in invoice
    assert "lines_.sku" in request


# ============================================================
# Nesting depth (Step 44)
# ============================================================

def test_nesting_depth_is_bounded(spec_fixtures):
    deep = parse_api_spec(spec_fixtures / "openapi_3_deep.yaml")
    shallow = parse_api_spec(
        spec_fixtures / "openapi_3_deep.yaml", ApiSpecOptions(max_nesting_depth=2)
    )

    deep_entity = deep.schema.entities[0]
    shallow_entity = shallow.schema.entities[0]

    assert len(shallow_entity.fields) < len(deep_entity.fields)
    assert any("l1.l2.l3.l4.l5" == f.normalized_name for f in deep_entity.fields)


def test_the_field_budget_marks_a_schema_partial(spec_fixtures):
    result = parse_api_spec(
        spec_fixtures / "openapi_3_basic.json",
        ApiSpecOptions(max_fields_per_schema=3),
    )

    invoice = result.schema.entity_by_normalized_name("invoice")
    assert len(invoice.fields) == 3
    assert invoice.metadata["partial"] is True


# ============================================================
# Composition (Steps 15, 16)
# ============================================================

@pytest.fixture()
def composition(spec_fixtures):
    return parse_api_spec(spec_fixtures / "openapi_3_composition.yaml")


def test_allof_branches_are_merged(composition):
    """The payload satisfies ALL branches at once, so their properties really
    do coexist."""
    fields = fields_of(composition, "invoice")

    assert "documentid" in fields      # from BaseDocument
    assert "total" in fields           # from the inline branch
    assert fields["createdat"].normalized_data_type is FieldDataType.DATETIME


def test_allof_merges_required_from_every_branch(composition):
    fields = fields_of(composition, "invoice")

    assert fields["documentid"].required is True
    assert fields["total"].required is True
    assert fields["currency"].required is False


def test_an_allof_conflict_is_reported_and_resolved_conservatively(composition):
    """Two branches declaring the same property differently: the first wins and
    the disagreement is recorded, rather than one silently overwriting."""
    assert "composition_conflict" in {w.category for w in composition.warnings}

    fields = fields_of(composition, "paymenttarget")
    assert fields["conflicting.shared"].normalized_data_type is FieldDataType.STRING


def test_oneof_names_the_alternatives_without_choosing_one(composition):
    fields = fields_of(composition, "paymenttarget")
    method = fields["method"]

    assert method.source_data_type == "oneOf<BankAccount|CreditCard>"
    assert method.metadata["variant_of"] == ["BankAccount", "CreditCard"]
    # Both branches are objects, so the union genuinely is an object.
    assert method.normalized_data_type is FieldDataType.OBJECT


def test_oneof_branches_are_not_merged_into_coexisting_fields(composition):
    """Flattening them would describe a payload shape that never occurs."""
    fields = fields_of(composition, "paymenttarget")

    assert "method.iban" not in fields
    assert "method.maskednumber" not in fields


def test_anyof_with_incompatible_branches_is_honestly_unknown(composition):
    fields = fields_of(composition, "paymenttarget")
    fallback = fields["fallback"]

    assert fallback.source_data_type == "anyOf<string|integer>"
    assert fallback.normalized_data_type is FieldDataType.UNKNOWN


# ============================================================
# Enums (Step 17)
# ============================================================

def test_enum_values_are_preserved_as_declared_constraints(spec_fixtures):
    """An enum is part of the contract every consumer must satisfy - unlike an
    example, which is one caller's data."""
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json")
    status = fields_of(result, "invoice")["status"]

    assert status.metadata["enum"] == ["PENDING", "PAID", "CANCELLED"]


def test_enum_values_are_bounded(spec_fixtures):
    result = parse_api_spec(
        spec_fixtures / "openapi_3_enum_overflow.yaml",
        ApiSpecOptions(max_enum_values=10),
    )
    entity = result.schema.entities[0]
    code = next(f for f in entity.fields if f.normalized_name == "code")

    assert len(code.metadata["enum"]) == 10
    assert code.metadata["enum_truncated"] is True
    assert code.metadata["enum_total_count"] == 60


def test_enum_capture_can_be_switched_off(spec_fixtures):
    result = parse_api_spec(
        spec_fixtures / "openapi_3_basic.json",
        ApiSpecOptions(include_enum_values=False),
    )
    status = fields_of(result, "invoice")["status"]

    assert "enum" not in status.metadata


# ============================================================
# Examples (Step 18)
# ============================================================

def test_a_declared_example_is_recorded_as_a_fact_not_a_value(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json")
    field = fields_of(result, "invoice")["example_holder"]

    assert field.metadata["example_present"] is True
    assert "example" not in field.metadata
    assert field.normalized_data_type is FieldDataType.STRING


# ============================================================
# Security metadata (Step 22)
# ============================================================

@pytest.fixture()
def secured(spec_fixtures):
    return parse_api_spec(spec_fixtures / "openapi_3_security.yaml")


def test_security_schemes_are_described(secured):
    schemes = {s.name: s for s in secured.specification.security_schemes}

    assert schemes["bearerAuth"].scheme_type == "http"
    assert schemes["bearerAuth"].http_scheme == "bearer"
    assert schemes["tenantApiKey"].scheme_type == "apiKey"
    assert schemes["tenantApiKey"].location == "header"
    # The header NAME is contract information; a key's value never is.
    assert schemes["tenantApiKey"].parameter_name == "X-API-Key"


def test_oauth_flow_names_are_recorded_but_not_their_endpoints(secured):
    """A token URL is an address this phase must never visit, so it is not
    stored at all."""
    oauth = next(
        s for s in secured.specification.security_schemes if s.name == "erpOauth"
    )

    assert oauth.oauth_flows == ("authorizationCode",)
    payload = str(secured.to_dict())
    assert "auth.example.invalid" not in payload
    assert "tokenUrl" not in payload


def test_operations_record_which_schemes_they_require(secured):
    operation = secured.operation_by_key("get.secure_invoices")

    assert set(operation.security_schemes) == {"bearerAuth", "tenantApiKey"}


def test_no_security_model_can_hold_a_credential():
    """Structural guarantee: there is no field to put a token in."""
    from erp_pipeline.api_specs import ApiSecurityScheme

    import dataclasses

    field_names = {f.name for f in dataclasses.fields(ApiSecurityScheme)}

    assert not (field_names & {
        "token", "secret", "password", "client_secret", "credential",
        "value", "api_key",
    })
