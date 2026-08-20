"""Options and API-specific contract models.

Two kinds of thing, as in ``discovery.models`` and ``ingestion.models``: input
configuration (``ApiSpecOptions``) and the supplemental results parsing
produces.

Why API-specific models exist at all
------------------------------------
``SourceSchema`` describes STRUCTURE. It has no place to record that a
structure is the request body of ``POST /invoices`` rather than the 404
response of ``GET /invoices/{id}`` - and losing that would make the schema
useless for a later mapping phase, which must know which contract it is
mapping. So Phase 7 keeps HTTP semantics in supplemental models
(``ApiOperation``, ``ApiParameter``, ``ApiResponse``, ...) and still emits the
same ``SourceSchema`` every other source produces. The two are linked by
entity id, never merged.

THE PRIVACY SPLIT
-----------------
An API specification is documentation, so most of it is safe. Three things in
it are not, and they are treated as data rather than structure:

    NEVER RETAINED
        example / examples payloads      may be real customer records
        Postman header values            Authorization, X-API-Key, Cookie
        Postman variable values          {{token}} resolves to a secret
        Postman auth credentials         bearer tokens, basic passwords
        query parameter values           may carry ids, tokens, filters

    RETAINED (declared structure, not data)
        schema, field, parameter and header NAMES
        types, formats, required flags, nullability
        enum values - a declared constraint, bounded by max_enum_values
        security scheme names and types - descriptive only

The distinction is that a NAME is part of the contract every consumer must
know, while a VALUE is one caller's data that happened to be pasted into the
documentation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from erp_pipeline.schemas.enums import SourceType

#: Version of the parsing behaviour itself, recorded in provenance. Bumped when
#: a change here would make the same bytes parse differently.
PARSER_VERSION = "1.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# Format and version
# ============================================================

class ApiSpecFormat(str, Enum):
    """Which specification language a document is written in.

    An ingestion-layer discriminator, deliberately NOT added to the frozen
    Phase 1 ``SourceType`` - which already carries ``OPENAPI`` and ``POSTMAN``.
    ``to_source_type()`` is the single place the two vocabularies are mapped.
    """

    OPENAPI = "openapi"
    POSTMAN = "postman"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    def to_source_type(self) -> SourceType:
        return _FORMAT_TO_SOURCE_TYPE[self]


_FORMAT_TO_SOURCE_TYPE: Mapping[ApiSpecFormat, SourceType] = {
    ApiSpecFormat.OPENAPI: SourceType.OPENAPI,
    ApiSpecFormat.POSTMAN: SourceType.POSTMAN,
}


class HttpMethod(str, Enum):
    """HTTP methods an operation may declare.

    Declared as an ordered enum so operation ordering is deterministic: paths
    sort alphabetically, then methods sort in this fixed order rather than in
    whatever order the document happened to list them.
    """

    GET = "get"
    HEAD = "head"
    POST = "post"
    PUT = "put"
    PATCH = "patch"
    DELETE = "delete"
    OPTIONS = "options"
    TRACE = "trace"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def sort_key(self) -> int:
        return _METHOD_ORDER[self]


_METHOD_ORDER: Mapping[HttpMethod, int] = {
    method: index for index, method in enumerate(HttpMethod)
}

#: Lookup used by both parsers when reading a method name from a document.
HTTP_METHODS_BY_NAME: Mapping[str, HttpMethod] = {
    method.value: method for method in HttpMethod
}


class ParameterLocation(str, Enum):
    """Where a parameter travels. Mirrors OpenAPI's ``in`` values."""

    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"
    #: Swagger 2 ``formData``, which OpenAPI 3 replaced with a request body.
    FORM_DATA = "formData"
    #: Swagger 2 ``body``, likewise replaced by ``requestBody``.
    BODY = "body"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ContractDirection(str, Enum):
    """Whether a structure is sent to the API or returned by it.

    Kept explicit because a request and a response are DIFFERENT contracts even
    when they share a name: ``CreateCustomerRequest`` has no ``id``, while
    ``CustomerResponse`` does. Merging them would describe neither correctly.
    """

    REQUEST = "request"
    RESPONSE = "response"
    #: A reusable component/definition, not tied to one direction.
    COMPONENT = "component"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class StructureOrigin(str, Enum):
    """How one entity's structure became known.

    ``SourceSchema.origin`` is a single schema-level value, but a Postman
    collection legitimately mixes declared request shapes with structures
    inferred from saved examples. Recording the per-entity truth here keeps the
    provenance honest instead of flattening it into one convenient answer.
    """

    #: Read from a declared schema - OpenAPI ``components.schemas``, an
    #: explicit ``type``/``properties`` block.
    DECLARED = "declared"
    #: Inferred from example payloads, with no declared types available.
    INFERRED_FROM_EXAMPLES = "inferred_from_examples"
    #: Inferred from parameter definitions that carry names but no types.
    INFERRED_FROM_PARAMETERS = "inferred_from_parameters"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ============================================================
# Resource limits (Step 44)
# ============================================================

#: 16 MiB. Generous for a specification document - real ERP OpenAPI files run
#: to a few MB - while still bounding a hostile input.
DEFAULT_MAX_SPEC_SIZE_BYTES = 16 * 1024 * 1024

DEFAULT_MAX_OPERATIONS = 2000
DEFAULT_MAX_SCHEMAS = 2000
DEFAULT_MAX_FIELDS_PER_SCHEMA = 500
DEFAULT_MAX_NESTING_DEPTH = 12
DEFAULT_MAX_REFERENCE_DEPTH = 8
DEFAULT_MAX_EXAMPLES_PER_OPERATION = 20
DEFAULT_MAX_EXAMPLE_BODY_BYTES = 1024 * 1024
DEFAULT_MAX_ENUM_VALUES = 100
DEFAULT_MAX_WARNINGS = 200
DEFAULT_MAX_DESCRIPTION_LENGTH = 500

#: Media types whose payloads carry a JSON structure this phase can describe.
JSON_MEDIA_TYPES: tuple[str, ...] = (
    "application/json",
    "application/problem+json",
    "application/hal+json",
    "application/vnd.api+json",
    "text/json",
)


@dataclass(frozen=True)
class ApiSpecOptions:
    """Parsing configuration. Conservative by default."""

    source_system_id: str = "api_specification"

    max_spec_size_bytes: int = DEFAULT_MAX_SPEC_SIZE_BYTES
    max_operations: int = DEFAULT_MAX_OPERATIONS
    max_schemas: int = DEFAULT_MAX_SCHEMAS
    max_fields_per_schema: int = DEFAULT_MAX_FIELDS_PER_SCHEMA
    max_nesting_depth: int = DEFAULT_MAX_NESTING_DEPTH
    max_reference_depth: int = DEFAULT_MAX_REFERENCE_DEPTH
    max_examples_per_operation: int = DEFAULT_MAX_EXAMPLES_PER_OPERATION
    max_example_body_bytes: int = DEFAULT_MAX_EXAMPLE_BODY_BYTES
    max_enum_values: int = DEFAULT_MAX_ENUM_VALUES
    max_warnings: int = DEFAULT_MAX_WARNINGS
    max_description_length: int = DEFAULT_MAX_DESCRIPTION_LENGTH

    #: Authored prose from the specification. Safe by default - a description
    #: is documentation written for consumers, and it is the single most useful
    #: signal a later semantic-mapping phase has. Truncated to
    #: ``max_description_length``.
    include_descriptions: bool = True
    #: Enum members are declared CONSTRAINTS, not sampled data, so they are
    #: retained by default (bounded). Turn off for a maximally minimal schema.
    include_enum_values: bool = True
    #: Operations marked ``deprecated`` are still part of the contract.
    include_deprecated: bool = True
    #: Emit EMBEDDED relationships for declared ``$ref`` links between schemas.
    include_reference_relationships: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_spec_size_bytes", "max_operations", "max_schemas",
            "max_fields_per_schema", "max_nesting_depth", "max_reference_depth",
            "max_examples_per_operation", "max_example_body_bytes",
            "max_enum_values", "max_warnings", "max_description_length",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"ApiSpecOptions.{name} must be a positive integer, "
                    f"got {value!r}."
                )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ============================================================
# Warnings
# ============================================================

@dataclass(frozen=True)
class ApiSpecWarning:
    """One non-fatal problem, described POSITIONALLY.

    ``pointer`` is a JSON-pointer-style location into the document
    (``#/components/schemas/Invoice/properties/total``). A location is
    actionable and is never sensitive; the value at that location is not
    recorded.
    """

    category: str
    message: str
    pointer: str | None = None
    operation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "message": self.message,
            "pointer": self.pointer,
            "operation": self.operation,
        }


# ============================================================
# API contract models
# ============================================================

@dataclass(frozen=True)
class ApiParameter:
    """One declared parameter, with its location preserved.

    No ``value`` field exists, deliberately: a parameter's example or default
    may be a real customer id or an API key pasted into the documentation.
    """

    name: str
    location: ParameterLocation
    required: bool = False
    data_type: str | None = None          # normalized FieldDataType value
    source_data_type: str | None = None   # the declared type/format verbatim
    description: str | None = None
    enabled: bool = True                  # Postman can disable a parameter
    is_sensitive_name: bool = False       # e.g. Authorization, X-API-Key
    style: str | None = None
    explode: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"location": self.location.value}


@dataclass(frozen=True)
class ApiRequestBody:
    """A request body contract for one media type."""

    media_type: str
    required: bool = False
    entity_id: str | None = None          # the SourceEntity describing it
    schema_name: str | None = None
    structure_origin: StructureOrigin = StructureOrigin.DECLARED
    description: str | None = None
    #: True when the payload is an ARRAY of ``entity_id`` rather than one of
    #: them. Kept as a flag rather than a separate entity so that "a list of
    #: Invoice" links to the one Invoice contract instead of duplicating its
    #: fields into a near-identical copy that could later drift.
    is_collection: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"structure_origin": self.structure_origin.value}


@dataclass(frozen=True)
class ApiResponse:
    """A response contract for one status code and media type."""

    status_code: str                      # "200", "404", "default"
    media_type: str | None = None
    entity_id: str | None = None
    schema_name: str | None = None
    structure_origin: StructureOrigin = StructureOrigin.DECLARED
    description: str | None = None
    examples_observed: int = 0
    #: True when the body is an ARRAY of ``entity_id``. See ApiRequestBody.
    is_collection: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"structure_origin": self.structure_origin.value}


@dataclass(frozen=True)
class ApiOperation:
    """One documented operation, with its full request/response linkage.

    This is what stops the conversion to ``SourceSchema`` losing the thing that
    makes an API specification useful: which structure belongs to which
    endpoint, in which direction.
    """

    operation_key: str                    # deterministic, e.g. "get_invoices_id"
    method: HttpMethod
    path: str
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    deprecated: bool = False
    folder_path: tuple[str, ...] = ()     # Postman folders
    parameters: tuple[ApiParameter, ...] = ()
    request_bodies: tuple[ApiRequestBody, ...] = ()
    responses: tuple[ApiResponse, ...] = ()
    security_schemes: tuple[str, ...] = ()
    script_present: bool = False          # Postman: recorded, never executed

    @property
    def request_entity_ids(self) -> tuple[str, ...]:
        return tuple(
            body.entity_id for body in self.request_bodies if body.entity_id
        )

    @property
    def response_entity_ids(self) -> tuple[str, ...]:
        return tuple(
            response.entity_id for response in self.responses if response.entity_id
        )

    def parameters_in(self, location: ParameterLocation) -> tuple[ApiParameter, ...]:
        return tuple(p for p in self.parameters if p.location is location)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_key": self.operation_key,
            "method": self.method.value,
            "path": self.path,
            "operation_id": self.operation_id,
            "summary": self.summary,
            "description": self.description,
            "tags": list(self.tags),
            "deprecated": self.deprecated,
            "folder_path": list(self.folder_path),
            "parameters": [p.to_dict() for p in self.parameters],
            "request_bodies": [b.to_dict() for b in self.request_bodies],
            "responses": [r.to_dict() for r in self.responses],
            "security_schemes": list(self.security_schemes),
            "script_present": self.script_present,
            "request_entity_ids": list(self.request_entity_ids),
            "response_entity_ids": list(self.response_entity_ids),
        }


@dataclass(frozen=True)
class ApiSecurityScheme:
    """A declared security scheme - DESCRIPTIVE ONLY.

    Records that an endpoint expects, say, a bearer token. Records nothing that
    could be used to obtain one. There is no field here capable of holding a
    credential, and acquiring or sending one is the teammate's integration
    component, not this phase.
    """

    name: str
    scheme_type: str                      # apiKey | http | oauth2 | openIdConnect
    location: str | None = None           # header | query | cookie
    parameter_name: str | None = None     # e.g. "X-API-Key" - a NAME, not a key
    http_scheme: str | None = None        # bearer | basic
    #: OAuth2 flow names only (``authorizationCode``), never URLs or scopes'
    #: secrets. Flow endpoints are deliberately not stored: they are addresses
    #: this phase must never contact.
    oauth_flows: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApiSpecification:
    """Document-level metadata about the specification itself."""

    spec_format: ApiSpecFormat
    spec_version: str                     # "3.0.3", "2.0", "v2.1.0"
    title: str | None = None
    api_version: str | None = None
    description: str | None = None
    #: Server/base-path TEMPLATES only. Never contacted, and query strings are
    #: stripped: a documented URL can carry an api key in its query.
    server_paths: tuple[str, ...] = ()
    security_schemes: tuple[ApiSecurityScheme, ...] = ()
    variable_names: tuple[str, ...] = ()  # Postman: NAMES only, never values

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_format": self.spec_format.value,
            "spec_version": self.spec_version,
            "title": self.title,
            "api_version": self.api_version,
            "description": self.description,
            "server_paths": list(self.server_paths),
            "security_schemes": [s.to_dict() for s in self.security_schemes],
            "variable_names": list(self.variable_names),
        }


@dataclass(frozen=True)
class SpecProvenance:
    """Where this contract came from and how it was parsed.

    A pointer plus parsing facts, never a second copy of the document. As in
    Phase 6, the local path is excluded from the portable payload: a developer
    workstation path is not identity.
    """

    spec_id: str
    content_hash: str
    original_filename: str
    spec_format: ApiSpecFormat
    spec_version: str
    media_type: str
    size_bytes: int
    parser: str
    parser_version: str = PARSER_VERSION
    operation_count: int = 0
    schema_count: int = 0
    parsed_at: datetime = field(default_factory=utc_now)

    def to_dict(self, include_timestamp: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "spec_id": self.spec_id,
            "content_hash": self.content_hash,
            "original_filename": self.original_filename,
            "spec_format": self.spec_format.value,
            "spec_version": self.spec_version,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "operation_count": self.operation_count,
            "schema_count": self.schema_count,
        }
        if include_timestamp:
            # Operational only. Never an input to identity or any hash.
            payload["parsed_at"] = self.parsed_at.isoformat()
        return payload


@dataclass(frozen=True)
class ApiSpecificationResult:
    """What one specification parse produced.

    ``schema`` is the authoritative Phase 1 ``SourceSchema`` - the same
    contract PostgreSQL, MySQL, SQL Server, MongoDB and CSV produce.
    ``specification`` and ``operations`` are supplemental HTTP semantics, kept
    OUTSIDE the schema so that changing a summary or an example never perturbs
    the structural hash.
    """

    specification: ApiSpecification
    provenance: SpecProvenance
    schema: Any = None                    # SourceSchema, typed loosely
    operations: tuple[ApiOperation, ...] = ()
    warnings: tuple[ApiSpecWarning, ...] = ()

    @property
    def spec_format(self) -> ApiSpecFormat:
        return self.specification.spec_format

    @property
    def content_hash(self) -> str:
        return self.provenance.content_hash

    @property
    def schema_hash(self) -> str:
        return self.schema.compute_schema_hash()

    @property
    def entity_count(self) -> int:
        return len(self.schema.entities) if self.schema is not None else 0

    def operation_by_key(self, operation_key: str) -> ApiOperation | None:
        for operation in self.operations:
            if operation.operation_key == operation_key:
                return operation
        return None

    def operations_for_path(self, path: str) -> tuple[ApiOperation, ...]:
        return tuple(op for op in self.operations if op.path == path)

    def to_dict(self) -> dict[str, Any]:
        """Operational summary. Contains no example payloads or secret values."""
        return {
            "specification": self.specification.to_dict(),
            "provenance": self.provenance.to_dict(),
            "schema": self.schema.to_json_dict() if self.schema is not None else None,
            "operations": [operation.to_dict() for operation in self.operations],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


__all__ = [
    "PARSER_VERSION",
    "ApiSpecFormat",
    "HttpMethod",
    "HTTP_METHODS_BY_NAME",
    "ParameterLocation",
    "ContractDirection",
    "StructureOrigin",
    "JSON_MEDIA_TYPES",
    "DEFAULT_MAX_SPEC_SIZE_BYTES",
    "DEFAULT_MAX_OPERATIONS",
    "DEFAULT_MAX_SCHEMAS",
    "DEFAULT_MAX_FIELDS_PER_SCHEMA",
    "DEFAULT_MAX_NESTING_DEPTH",
    "DEFAULT_MAX_REFERENCE_DEPTH",
    "DEFAULT_MAX_ENUM_VALUES",
    "ApiSpecOptions",
    "ApiSpecWarning",
    "ApiParameter",
    "ApiRequestBody",
    "ApiResponse",
    "ApiOperation",
    "ApiSecurityScheme",
    "ApiSpecification",
    "SpecProvenance",
    "ApiSpecificationResult",
    "utc_now",
]
