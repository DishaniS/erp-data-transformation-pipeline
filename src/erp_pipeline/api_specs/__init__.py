"""API specification ingestion for the generic ERP pipeline.

Phase 7 answers "WHAT structured data does this documented API expose or
accept?" It does not answer "how do we call it?" - runtime ERP/API execution
belongs to the integration/MCP component, and this package contains no HTTP
client, acquires no credential, and contacts no endpoint.

Two specification languages, one output contract::

    OpenAPI / Swagger  --> declared API contract  --> SourceSchema
    Postman collection --> request + saved-example analysis --> SourceSchema

The difference between them is not cosmetic and is preserved honestly:

    OpenAPI DECLARES types, so its entities carry SchemaOrigin.API_SPEC and
    structure_origin="declared".

    Postman declares NO types at all - a body is a payload someone once sent -
    so its entities are inferred from examples, carry SchemaOrigin.INFERRED,
    and record how many examples each claim rests on.

Position in the architecture::

    OpenAPI / Swagger / Postman
               |
               v
    Phase 7 Specification Parsing     THIS PACKAGE
               |
               v
           SourceSchema               erp_pipeline.schemas (Phase 1)
               |
               v
      Phase 2 Schema Catalog          erp_pipeline.catalog

The structural output is the SAME ``SourceSchema`` -> ``SourceEntity`` ->
``SourceField`` that PostgreSQL, MySQL, SQL Server, MongoDB and CSV produce.
HTTP semantics that ``SourceSchema`` cannot hold - which structure is the
request of which endpoint - live in supplemental ``ApiOperation`` models
linked by entity id.

Privacy: declared NAMES are structure and are kept; VALUES are not. Example
payloads, Postman header values, variable values and auth credentials never
reach a schema, a warning, an exception, a log or the catalog.

This package never imports ``bpi2020``.
"""

from __future__ import annotations

from erp_pipeline.api_specs.detection import (
    SUPPORTED_OPENAPI_MAJORS,
    SUPPORTED_POSTMAN_MAJORS,
    SUPPORTED_SWAGGER_VERSIONS,
    SpecDetectionResult,
    detect_specification,
)
from erp_pipeline.api_specs.errors import (
    ApiSpecError,
    MalformedSpecError,
    ReferenceResolutionError,
    SpecFileError,
    SpecLimitExceededError,
    SpecStructureError,
    UnsafeSpecContentError,
    UnsupportedSpecFormatError,
    UnsupportedSpecVersionError,
)
from erp_pipeline.api_specs.inference import (
    InferredStructure,
    infer_fields_from_parameters,
    infer_structure_from_examples,
    json_type_name,
)
from erp_pipeline.api_specs.models import (
    PARSER_VERSION,
    ApiOperation,
    ApiParameter,
    ApiRequestBody,
    ApiResponse,
    ApiSecurityScheme,
    ApiSpecification,
    ApiSpecificationResult,
    ApiSpecFormat,
    ApiSpecOptions,
    ApiSpecWarning,
    ContractDirection,
    HttpMethod,
    ParameterLocation,
    SpecProvenance,
    StructureOrigin,
)
from erp_pipeline.api_specs.openapi_parser import (
    OpenApiParser,
    build_inline_schema_name,
    build_operation_key,
    is_sensitive_header,
)
from erp_pipeline.api_specs.postman_parser import PostmanParser
from erp_pipeline.api_specs.references import (
    ReferenceResolver,
    RefStatus,
    is_remote_reference,
    reference_target_name,
)
from erp_pipeline.api_specs.schema_conversion import (
    ConvertedSchema,
    convert_schema_to_fields,
    normalize_schema_type,
)
from erp_pipeline.api_specs.service import (
    ApiSpecificationService,
    describe_api_spec,
    parse_api_spec,
)

__all__ = [
    # service
    "ApiSpecificationService",
    "parse_api_spec",
    "describe_api_spec",
    # options
    "ApiSpecOptions",
    # detection
    "ApiSpecFormat",
    "SpecDetectionResult",
    "detect_specification",
    "SUPPORTED_OPENAPI_MAJORS",
    "SUPPORTED_SWAGGER_VERSIONS",
    "SUPPORTED_POSTMAN_MAJORS",
    # contract models
    "ApiSpecification",
    "ApiSpecificationResult",
    "ApiOperation",
    "ApiParameter",
    "ApiRequestBody",
    "ApiResponse",
    "ApiSecurityScheme",
    "ApiSpecWarning",
    "SpecProvenance",
    "HttpMethod",
    "ParameterLocation",
    "ContractDirection",
    "StructureOrigin",
    "PARSER_VERSION",
    # parsers
    "OpenApiParser",
    "PostmanParser",
    "build_operation_key",
    "build_inline_schema_name",
    "is_sensitive_header",
    # schema conversion and inference
    "convert_schema_to_fields",
    "normalize_schema_type",
    "ConvertedSchema",
    "infer_structure_from_examples",
    "infer_fields_from_parameters",
    "InferredStructure",
    "json_type_name",
    # references
    "ReferenceResolver",
    "RefStatus",
    "is_remote_reference",
    "reference_target_name",
    # errors
    "ApiSpecError",
    "SpecFileError",
    "UnsupportedSpecFormatError",
    "UnsupportedSpecVersionError",
    "MalformedSpecError",
    "UnsafeSpecContentError",
    "SpecStructureError",
    "ReferenceResolutionError",
    "SpecLimitExceededError",
]
