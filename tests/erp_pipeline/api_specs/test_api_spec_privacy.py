"""Step 55: declared names survive; values never do.

An API specification is documentation, so most of it is safe to publish. Three
things in it are not, and they are exactly the things developers most often
paste into one:

    an OpenAPI ``example``      may be a real customer record
    a Postman header value      Authorization: Bearer <a real token>
    a Postman variable value    {{apiToken}} resolves to a real secret

These tests plant synthetic sentinels in every such position and assert they
are absent from schemas, summaries, warnings, exceptions and logs - while the
declared NAMES around them are still reported, because a consumer must know
the header is called ``Authorization`` even though its value is forbidden.
"""

from __future__ import annotations

import json
import logging

import pytest

from erp_pipeline.api_specs import ApiSpecOptions, parse_api_spec

from tests.erp_pipeline.api_specs.conftest import (
    SECRET_API_KEY,
    SECRET_BASIC_PASSWORD,
    SECRET_BEARER,
    SECRET_COOKIE,
    SECRET_CUSTOMER,
    SECRET_HTML_BODY,
    SECRET_IBAN,
    SECRET_OPENAPI_EXAMPLE,
    SECRET_QUERY_KEY,
    SECRETS,
)

SENSITIVE_FIXTURES = (
    "openapi_3_basic.json",
    "openapi_3_basic.yaml",
    "postman_auth_secrets.json",
    "postman_raw_body.json",
    "postman_response_examples.json",
    "postman_mixed_examples.json",
    "postman_variables.json",
)


def assert_clean(payload: str, context: str) -> None:
    for secret in SECRETS:
        assert secret not in payload, f"{context} leaked {secret!r}"


# ============================================================
# Nothing leaks, from any fixture, through any surface
# ============================================================

@pytest.mark.parametrize("filename", SENSITIVE_FIXTURES)
def test_no_secret_reaches_the_serialized_result(spec_fixtures, filename):
    result = parse_api_spec(spec_fixtures / filename)

    assert_clean(json.dumps(result.to_dict(), default=str), f"{filename} to_dict()")


@pytest.mark.parametrize("filename", SENSITIVE_FIXTURES)
def test_no_secret_reaches_the_source_schema(spec_fixtures, filename):
    result = parse_api_spec(spec_fixtures / filename)

    assert_clean(json.dumps(result.schema.to_json_dict(), default=str),
                 f"{filename} SourceSchema")


@pytest.mark.parametrize("filename", SENSITIVE_FIXTURES)
def test_no_secret_reaches_field_metadata(spec_fixtures, filename):
    result = parse_api_spec(spec_fixtures / filename)

    for entity in result.schema.entities:
        assert_clean(json.dumps(dict(entity.metadata), default=str),
                     f"{filename} entity metadata")
        for field in entity.fields:
            assert_clean(json.dumps(dict(field.metadata), default=str),
                         f"{filename} field metadata")


@pytest.mark.parametrize("filename", SENSITIVE_FIXTURES)
def test_no_secret_reaches_warnings(spec_fixtures, filename):
    result = parse_api_spec(spec_fixtures / filename)

    assert_clean(
        json.dumps([w.to_dict() for w in result.warnings], default=str),
        f"{filename} warnings",
    )


@pytest.mark.parametrize("filename", SENSITIVE_FIXTURES)
def test_nothing_is_logged_while_parsing(spec_fixtures, filename, caplog):
    with caplog.at_level(logging.DEBUG):
        parse_api_spec(spec_fixtures / filename)

    assert_clean(caplog.text, f"{filename} log output")


# ============================================================
# Postman auth and headers (Steps 27, 28, 36)
# ============================================================

@pytest.fixture()
def secured(spec_fixtures):
    return parse_api_spec(spec_fixtures / "postman_auth_secrets.json")


def test_a_sensitive_header_is_named_but_never_valued(secured):
    """A consumer must know the endpoint expects an Authorization header. The
    token in it is not theirs to receive."""
    from erp_pipeline.api_specs import ParameterLocation

    operation = secured.operations[0]
    headers = {
        h.name: h for h in operation.parameters_in(ParameterLocation.HEADER)
    }

    assert set(headers) == {"Authorization", "X-API-Key", "Cookie", "X-Tenant-ID"}
    assert headers["Authorization"].is_sensitive_name is True
    assert headers["X-API-Key"].is_sensitive_name is True
    assert headers["Cookie"].is_sensitive_name is True
    assert headers["X-Tenant-ID"].is_sensitive_name is False

    assert_clean(json.dumps([h.to_dict() for h in headers.values()]), "headers")


def test_collection_auth_records_only_its_type(secured):
    schemes = secured.specification.security_schemes

    assert [s.scheme_type for s in schemes] == ["bearer"]
    assert_clean(json.dumps([s.to_dict() for s in schemes]), "collection auth")


def test_request_auth_records_only_its_type(secured):
    operation = secured.operations[0]

    assert operation.security_schemes == ("collection_auth_basic",)
    assert SECRET_BASIC_PASSWORD not in json.dumps(operation.to_dict())


def test_variable_names_are_kept_and_their_values_are_not(secured):
    names = set(secured.specification.variable_names)

    assert {"baseUrl", "apiToken", "basicPassword"} <= names
    assert_clean(json.dumps(secured.specification.to_dict()), "variables")


def test_a_variable_reference_in_a_query_keeps_only_the_name(secured):
    """`?token={{apiToken}}` records the parameter name and the variable name,
    never what the variable resolves to."""
    from erp_pipeline.api_specs import ParameterLocation

    query = secured.operations[0].parameters_in(ParameterLocation.QUERY)

    assert [p.name for p in query] == ["token"]
    assert "apiToken" in secured.specification.variable_names
    assert SECRET_BEARER not in json.dumps(secured.to_dict())


def test_business_values_in_a_saved_response_are_not_stored(secured):
    """The response example contains a customer name and an IBAN; only the
    field names and types survive."""
    entity = next(
        e for e in secured.schema.entities if "response" in e.normalized_name
    )
    names = {field.normalized_name for field in entity.fields}

    assert {"invoiceid", "customername", "iban"} <= names
    assert_clean(json.dumps(entity.to_json_dict(), default=str), "response entity")


# ============================================================
# OpenAPI examples and URLs (Steps 6, 18)
# ============================================================

def test_an_openapi_example_value_is_never_persisted(spec_fixtures):
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json")
    field = next(
        f for e in result.schema.entities for f in e.fields
        if f.normalized_name == "example_holder"
    )

    assert field.metadata["example_present"] is True
    assert SECRET_OPENAPI_EXAMPLE not in json.dumps(dict(field.metadata))


def test_a_server_url_query_string_is_stripped(spec_fixtures):
    """A documented base URL can carry an api key in its query."""
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json")

    assert SECRET_QUERY_KEY not in json.dumps(result.to_dict())
    assert result.specification.server_paths == (
        "https://erp.example.invalid/api/v1",
    )


def test_a_non_json_response_body_is_not_captured(spec_fixtures):
    """Its content is not JSON, and it is not stored as text either."""
    result = parse_api_spec(spec_fixtures / "postman_mixed_examples.json")

    assert SECRET_HTML_BODY not in json.dumps(result.to_dict(), default=str)


# ============================================================
# Exceptions
# ============================================================

def test_an_error_message_never_carries_specification_content(tmp_path):
    from erp_pipeline.api_specs import MalformedSpecError

    broken = tmp_path / "broken.json"
    broken.write_text(
        '{"openapi": "3.0.3", "info": {"title": "' + SECRET_CUSTOMER + '"},,}',
        encoding="utf-8",
    )

    with pytest.raises(MalformedSpecError) as excinfo:
        parse_api_spec(broken)

    assert_clean(str(excinfo.value), "MalformedSpecError")
    assert_clean(repr(excinfo.value), "MalformedSpecError repr")


def test_a_limit_error_names_the_budget_not_the_content(spec_fixtures):
    from erp_pipeline.api_specs import SpecLimitExceededError

    with pytest.raises(SpecLimitExceededError) as excinfo:
        parse_api_spec(
            spec_fixtures / "openapi_3_basic.json", ApiSpecOptions(max_schemas=1)
        )

    assert excinfo.value.limit_name == "max_schemas"
    assert_clean(str(excinfo.value), "SpecLimitExceededError")


# ============================================================
# Structural guarantees
# ============================================================

def test_no_contract_model_can_hold_a_parameter_value():
    """A structural guarantee rather than an assertion about one fixture:
    there is nowhere to put a value."""
    import dataclasses

    from erp_pipeline.api_specs import (
        ApiParameter,
        ApiRequestBody,
        ApiResponse,
        ApiSecurityScheme,
    )

    forbidden = {"value", "values", "example", "examples", "default", "token",
                 "secret", "password", "body", "content", "raw"}

    for model in (ApiParameter, ApiRequestBody, ApiResponse, ApiSecurityScheme):
        names = {f.name for f in dataclasses.fields(model)}
        assert not (names & forbidden), f"{model.__name__} can hold a value"


def test_declared_names_are_still_fully_reported(spec_fixtures):
    """The privacy rule must not have hollowed out the contract: an API
    description with no field names would be useless."""
    result = parse_api_spec(spec_fixtures / "openapi_3_basic.json")
    invoice = result.schema.entity_by_normalized_name("invoice")

    names = {field.source_name for field in invoice.fields}
    assert {"invoiceId", "totalAmount", "status", "customer"} <= names
