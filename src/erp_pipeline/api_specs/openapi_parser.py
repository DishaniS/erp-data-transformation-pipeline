"""OpenAPI 3.0 / 3.1 and Swagger 2.0 parsing.

Reads a declared API contract and produces both halves of the Phase 7 result:
the HTTP semantics (``ApiOperation`` and friends) and the structural
``SourceSchema`` every other source in this framework also produces.

Swagger 2 versus OpenAPI 3
--------------------------
The two differ in three places that matter, and each is handled explicitly
rather than by pretending one is the other:

    reusable schemas   ``definitions``      vs ``components.schemas``
    request bodies     a ``body`` parameter vs ``requestBody.content``
    responses          ``response.schema``  vs ``response.content[media]``

Everything downstream of those three points is shared, which is why there is
one parser rather than two.

NO NETWORK
----------
Nothing here contacts a documented server, and a remote ``$ref`` is recorded
as unresolved rather than fetched. See ``references``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.api_specs.errors import SpecLimitExceededError, SpecStructureError
from erp_pipeline.api_specs.models import (
    HTTP_METHODS_BY_NAME,
    JSON_MEDIA_TYPES,
    ApiOperation,
    ApiParameter,
    ApiRequestBody,
    ApiResponse,
    ApiSecurityScheme,
    ApiSpecification,
    ApiSpecFormat,
    ApiSpecOptions,
    ApiSpecWarning,
    ContractDirection,
    HttpMethod,
    ParameterLocation,
    StructureOrigin,
)
from erp_pipeline.api_specs.references import (
    ReferenceResolver,
    RefStatus,
    reference_target_name,
)
from erp_pipeline.api_specs.safety import WarningBudget, truncate_description
from erp_pipeline.api_specs.schema_conversion import (
    ConvertedSchema,
    convert_schema_to_fields,
    normalize_schema_type,
)
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, RelationshipType
from erp_pipeline.schemas.identity import IdentityError, hash_json_payload, normalize_identifier
from erp_pipeline.schemas.source_models import SourceEntity, SourceRelationship

PARSER_NAME = "openapi"

#: Header names whose VALUES must never be retained. Matched case-insensitively
#: against a parameter name. Only ever used to set a flag - a declared header
#: parameter has no value in an OpenAPI document anyway, but the same list is
#: shared with the Postman parser, where values do appear.
SENSITIVE_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization", "proxy-authorization", "cookie", "set-cookie",
        "x-api-key", "api-key", "apikey", "x-auth-token", "auth-token",
        "x-access-token", "access-token", "x-csrf-token", "x-session-token",
        "private-token", "x-amz-security-token",
    }
)


def is_sensitive_header(name: str) -> bool:
    return name.strip().lower() in SENSITIVE_HEADER_NAMES


class OpenApiParser:
    """Parses one OpenAPI/Swagger document into operations and entities."""

    def __init__(
        self,
        document: Mapping[str, Any],
        spec_version: str,
        options: ApiSpecOptions | None = None,
    ) -> None:
        self._document = document
        self._version = spec_version
        self._options = options or ApiSpecOptions()
        self._is_swagger_2 = spec_version.startswith("2")
        self._resolver = ReferenceResolver(
            document, self._options.max_reference_depth
        )
        self._warnings = WarningBudget(self._options.max_warnings)
        self._entities: list[SourceEntity] = []
        self._entity_names: dict[str, int] = {}
        self._relationships: list[SourceRelationship] = []
        #: Declared schema name -> entity normalized name, for $ref linking.
        self._schema_entities: dict[str, str] = {}
        #: (entity, field path, target schema name) links, resolved once every
        #: entity exists - an inline response body can reference a component
        #: schema that is itself parsed first, and vice versa.
        self._link_queue: list[tuple[str, str, str]] = []

    # ------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------

    @property
    def warnings(self) -> tuple[ApiSpecWarning, ...]:
        return self._warnings.items()

    def parse(self) -> tuple[ApiSpecification, tuple[ApiOperation, ...],
                             tuple[SourceEntity, ...], tuple[SourceRelationship, ...]]:
        """Parse the document. Returns spec metadata, operations and entities."""
        self._require_structure()

        # Component schemas first, so an operation referring to one links to an
        # entity that already exists rather than duplicating it inline.
        self._parse_component_schemas()

        operations = self._parse_operations()

        # Only now is every entity known, so a $ref from an inline response
        # body to a component schema can be linked in either parse order.
        self._link_referenced_schemas()

        specification = self._parse_specification_metadata()

        return specification, operations, tuple(self._entities), tuple(self._relationships)

    # ------------------------------------------------------------
    # Document metadata (Steps 6, 22)
    # ------------------------------------------------------------

    def _require_structure(self) -> None:
        has_paths = isinstance(self._document.get("paths"), Mapping)
        has_schemas = bool(self._component_schema_container())

        if not has_paths and not has_schemas:
            raise SpecStructureError(
                "The document declares neither 'paths' nor reusable schemas, "
                "so it describes no API contract."
            )

    def _parse_specification_metadata(self) -> ApiSpecification:
        info = self._document.get("info")
        info = info if isinstance(info, Mapping) else {}

        return ApiSpecification(
            spec_format=ApiSpecFormat.OPENAPI,
            spec_version=self._version,
            title=truncate_description(
                info.get("title"), self._options.max_description_length
            ),
            api_version=(
                str(info["version"]) if isinstance(info.get("version"), (str, int, float))
                else None
            ),
            description=(
                truncate_description(
                    info.get("description"), self._options.max_description_length
                )
                if self._options.include_descriptions
                else None
            ),
            server_paths=self._parse_server_paths(),
            security_schemes=self._parse_security_schemes(),
        )

    def _parse_server_paths(self) -> tuple[str, ...]:
        """Base-path templates only.

        A documented server URL can carry an api key in its query string, so
        the query is stripped. These are recorded as structure, never used: no
        code in this package contacts a server.
        """
        paths: list[str] = []

        if self._is_swagger_2:
            base = self._document.get("basePath")
            if isinstance(base, str) and base:
                paths.append(base.split("?", 1)[0])
            return tuple(paths)

        servers = self._document.get("servers")
        if isinstance(servers, Sequence) and not isinstance(servers, (str, bytes)):
            for entry in servers:
                if isinstance(entry, Mapping) and isinstance(entry.get("url"), str):
                    paths.append(entry["url"].split("?", 1)[0])

        return tuple(paths)

    def _parse_security_schemes(self) -> tuple[ApiSecurityScheme, ...]:
        """Descriptive security metadata (Step 22).

        Records that an endpoint expects a credential and where it goes.
        Records nothing usable to obtain one, and contacts no OAuth endpoint -
        flow URLs are deliberately not stored, because they are addresses this
        phase must never visit. Acquiring and sending credentials is the
        teammate's integration component.
        """
        if self._is_swagger_2:
            container = self._document.get("securityDefinitions")
        else:
            components = self._document.get("components")
            container = (
                components.get("securitySchemes")
                if isinstance(components, Mapping) else None
            )

        if not isinstance(container, Mapping):
            return ()

        schemes: list[ApiSecurityScheme] = []

        for name in sorted(container, key=str):
            definition = container[name]
            if not isinstance(definition, Mapping):
                continue

            flows = definition.get("flows")
            flow_names: tuple[str, ...] = ()
            if isinstance(flows, Mapping):
                flow_names = tuple(sorted(str(key) for key in flows))
            elif isinstance(definition.get("flow"), str):  # Swagger 2
                flow_names = (str(definition["flow"]),)

            schemes.append(
                ApiSecurityScheme(
                    name=str(name),
                    scheme_type=str(definition.get("type") or "unknown"),
                    location=(
                        str(definition["in"]) if isinstance(definition.get("in"), str)
                        else None
                    ),
                    parameter_name=(
                        str(definition["name"]) if isinstance(definition.get("name"), str)
                        else None
                    ),
                    http_scheme=(
                        str(definition["scheme"])
                        if isinstance(definition.get("scheme"), str) else None
                    ),
                    oauth_flows=flow_names,
                )
            )

        return tuple(schemes)

    # ------------------------------------------------------------
    # Component schemas (Step 11)
    # ------------------------------------------------------------

    def _component_schema_container(self) -> Mapping[str, Any]:
        if self._is_swagger_2:
            container = self._document.get("definitions")
        else:
            components = self._document.get("components")
            container = (
                components.get("schemas") if isinstance(components, Mapping) else None
            )

        return container if isinstance(container, Mapping) else {}

    def _parse_component_schemas(self) -> None:
        container = self._component_schema_container()

        if len(container) > self._options.max_schemas:
            raise SpecLimitExceededError(
                f"The document declares {len(container)} reusable schemas, "
                f"which exceeds max_schemas ({self._options.max_schemas}).",
                limit_name="max_schemas",
                limit=self._options.max_schemas,
                observed=len(container),
            )

        # Sorted so entity order never depends on document key order.
        for name in sorted(container, key=str):
            schema = container[name]
            if not isinstance(schema, Mapping):
                self._warn(
                    "invalid_schema",
                    f"Reusable schema {str(name)!r} is not an object and was "
                    "skipped.",
                )
                continue

            entity_name = self._build_entity(
                display_name=str(name),
                schema=schema,
                direction=ContractDirection.COMPONENT,
                pointer=self._component_pointer(str(name)),
            )
            self._schema_entities[str(name)] = entity_name

    def _component_pointer(self, name: str) -> str:
        prefix = "#/definitions" if self._is_swagger_2 else "#/components/schemas"
        return f"{prefix}/{name}"

    # ------------------------------------------------------------
    # Operations (Steps 7-10)
    # ------------------------------------------------------------

    def _parse_operations(self) -> tuple[ApiOperation, ...]:
        paths = self._document.get("paths")

        if not isinstance(paths, Mapping):
            return ()

        collected: list[ApiOperation] = []

        # Paths alphabetically, methods in the fixed HttpMethod order.
        for path in sorted(paths, key=str):
            path_item = paths[path]
            if not isinstance(path_item, Mapping):
                continue

            shared = self._parse_parameters(path_item.get("parameters"), str(path))

            methods = [
                (HTTP_METHODS_BY_NAME[key.lower()], path_item[key])
                for key in path_item
                if isinstance(key, str) and key.lower() in HTTP_METHODS_BY_NAME
            ]

            for method, operation in sorted(
                methods, key=lambda item: item[0].sort_key
            ):
                if not isinstance(operation, Mapping):
                    continue

                if len(collected) >= self._options.max_operations:
                    raise SpecLimitExceededError(
                        f"The document declares more than "
                        f"{self._options.max_operations} operations.",
                        limit_name="max_operations",
                        limit=self._options.max_operations,
                    )

                collected.append(
                    self._parse_operation(str(path), method, operation, shared)
                )

        return tuple(collected)

    def _parse_operation(
        self,
        path: str,
        method: HttpMethod,
        operation: Mapping[str, Any],
        shared_parameters: tuple[ApiParameter, ...],
    ) -> ApiOperation:
        pointer = f"#/paths/{path}/{method.value}"
        operation_key = build_operation_key(method, path)

        own = self._parse_parameters(operation.get("parameters"), path)
        parameters = merge_parameters(shared_parameters, own)

        request_bodies = self._parse_request_bodies(
            operation, method, path, parameters, pointer
        )
        responses = self._parse_responses(operation, method, path, pointer)

        return ApiOperation(
            operation_key=operation_key,
            method=method,
            path=path,
            operation_id=(
                str(operation["operationId"])
                if isinstance(operation.get("operationId"), str) else None
            ),
            summary=truncate_description(
                operation.get("summary"), self._options.max_description_length
            ),
            description=(
                truncate_description(
                    operation.get("description"), self._options.max_description_length
                )
                if self._options.include_descriptions else None
            ),
            tags=tuple(
                str(tag) for tag in operation.get("tags", [])
                if isinstance(tag, (str, int))
            ),
            deprecated=bool(operation.get("deprecated", False)),
            parameters=tuple(
                p for p in parameters
                if p.location not in (ParameterLocation.BODY,)
            ),
            request_bodies=request_bodies,
            responses=responses,
            security_schemes=self._operation_security(operation),
        )

    def _operation_security(self, operation: Mapping[str, Any]) -> tuple[str, ...]:
        """Names of the security schemes an operation requires - names only."""
        security = operation.get("security")
        if not isinstance(security, Sequence) or isinstance(security, (str, bytes)):
            security = self._document.get("security")

        if not isinstance(security, Sequence) or isinstance(security, (str, bytes)):
            return ()

        names: list[str] = []
        for requirement in security:
            if isinstance(requirement, Mapping):
                names.extend(str(key) for key in requirement)

        return tuple(sorted(set(names)))

    # ------------------------------------------------------------
    # Parameters (Step 8)
    # ------------------------------------------------------------

    def _parse_parameters(
        self, raw: Any, path: str
    ) -> tuple[ApiParameter, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()

        parameters: list[ApiParameter] = []

        for index, entry in enumerate(raw):
            if not isinstance(entry, Mapping):
                continue

            definition = entry
            if isinstance(entry.get("$ref"), str):
                resolved = self._resolver.resolve(entry["$ref"])
                if resolved.is_usable and isinstance(resolved.target, Mapping):
                    definition = resolved.target
                else:
                    self._warn(
                        "unresolved_parameter_reference",
                        f"Parameter reference {entry['$ref']!r} could not be "
                        f"resolved ({resolved.status.value}).",
                        f"#/paths/{path}/parameters/{index}",
                    )
                    continue

            name = definition.get("name")
            location = definition.get("in")

            if not isinstance(name, str) or not isinstance(location, str):
                continue

            parameters.append(
                self._build_parameter(str(name), str(location), definition)
            )

        return tuple(parameters)

    def _build_parameter(
        self, name: str, location: str, definition: Mapping[str, Any]
    ) -> ApiParameter:
        try:
            parsed_location = ParameterLocation(location)
        except ValueError:
            parsed_location = ParameterLocation.QUERY
            self._warn(
                "unknown_parameter_location",
                f"Parameter {name!r} declares an unrecognized location "
                f"{location!r}; treated as a query parameter.",
            )

        # Swagger 2 puts type/format on the parameter; OpenAPI 3 nests a schema.
        schema = definition.get("schema")
        type_source: Mapping[str, Any] = (
            schema if isinstance(schema, Mapping) else definition
        )

        resolved = type_source
        if isinstance(type_source.get("$ref"), str):
            reference = self._resolver.resolve(type_source["$ref"])
            if reference.is_usable and isinstance(reference.target, Mapping):
                resolved = reference.target

        data_type, _ = normalize_schema_type(resolved)

        from erp_pipeline.api_specs.schema_conversion import render_source_data_type

        return ApiParameter(
            name=name,
            location=parsed_location,
            required=bool(definition.get("required", parsed_location is ParameterLocation.PATH)),
            data_type=data_type.value,
            source_data_type=render_source_data_type(resolved),
            description=(
                truncate_description(
                    definition.get("description"), self._options.max_description_length
                )
                if self._options.include_descriptions else None
            ),
            is_sensitive_name=(
                parsed_location is ParameterLocation.HEADER and is_sensitive_header(name)
            ),
            style=(
                str(definition["style"]) if isinstance(definition.get("style"), str)
                else None
            ),
            explode=(
                bool(definition["explode"]) if isinstance(definition.get("explode"), bool)
                else None
            ),
        )

    # ------------------------------------------------------------
    # Request bodies (Step 9)
    # ------------------------------------------------------------

    def _parse_request_bodies(
        self,
        operation: Mapping[str, Any],
        method: HttpMethod,
        path: str,
        parameters: Sequence[ApiParameter],
        pointer: str,
    ) -> tuple[ApiRequestBody, ...]:
        if self._is_swagger_2:
            return self._parse_swagger_2_body(operation, method, path, pointer)

        request_body = operation.get("requestBody")

        if isinstance(request_body, Mapping) and isinstance(
            request_body.get("$ref"), str
        ):
            resolved = self._resolver.resolve(request_body["$ref"])
            if resolved.is_usable and isinstance(resolved.target, Mapping):
                request_body = resolved.target

        if not isinstance(request_body, Mapping):
            return ()

        content = request_body.get("content")
        if not isinstance(content, Mapping):
            return ()

        required = bool(request_body.get("required", False))
        bodies: list[ApiRequestBody] = []

        for media_type in sorted(content, key=str):
            media = content[media_type]
            if not isinstance(media, Mapping):
                continue

            schema = media.get("schema")
            entity_id = None
            schema_name = None

            is_collection = False
            if isinstance(schema, Mapping):
                entity_id, schema_name, is_collection = self._entity_for_contract(
                    schema=schema,
                    method=method,
                    path=path,
                    direction=ContractDirection.REQUEST,
                    media_type=str(media_type),
                    multiple_media=len(content) > 1,
                    status_code=None,
                    pointer=f"{pointer}/requestBody/content/{media_type}/schema",
                )
            elif str(media_type) in JSON_MEDIA_TYPES:
                self._warn(
                    "request_body_without_schema",
                    f"Request body for {media_type} declares no schema.",
                    pointer,
                )

            bodies.append(
                ApiRequestBody(
                    media_type=str(media_type),
                    required=required,
                    entity_id=entity_id,
                    schema_name=schema_name,
                    is_collection=is_collection,
                    structure_origin=StructureOrigin.DECLARED,
                    description=(
                        truncate_description(
                            request_body.get("description"),
                            self._options.max_description_length,
                        )
                        if self._options.include_descriptions else None
                    ),
                )
            )

        return tuple(bodies)

    def _parse_swagger_2_body(
        self,
        operation: Mapping[str, Any],
        method: HttpMethod,
        path: str,
        pointer: str,
    ) -> tuple[ApiRequestBody, ...]:
        """Swagger 2 has no ``requestBody``: a body is a parameter with
        ``in: body``, and form fields are ``in: formData``."""
        raw = operation.get("parameters")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()

        consumes = operation.get("consumes") or self._document.get("consumes")
        media_type = "application/json"
        if isinstance(consumes, Sequence) and not isinstance(consumes, (str, bytes)):
            for candidate in consumes:
                if isinstance(candidate, str):
                    media_type = candidate
                    break

        bodies: list[ApiRequestBody] = []
        form_fields: list[Mapping[str, Any]] = []

        for entry in raw:
            if not isinstance(entry, Mapping):
                continue

            location = entry.get("in")

            if location == "body":
                schema = entry.get("schema")
                if not isinstance(schema, Mapping):
                    continue

                entity_id, schema_name, is_collection = self._entity_for_contract(
                    schema=schema,
                    method=method,
                    path=path,
                    direction=ContractDirection.REQUEST,
                    media_type=media_type,
                    multiple_media=False,
                    status_code=None,
                    pointer=f"{pointer}/parameters/body/schema",
                )
                bodies.append(
                    ApiRequestBody(
                        media_type=media_type,
                        required=bool(entry.get("required", False)),
                        entity_id=entity_id,
                        schema_name=schema_name,
                        is_collection=is_collection,
                        structure_origin=StructureOrigin.DECLARED,
                    )
                )
            elif location == "formData":
                form_fields.append(entry)

        if form_fields:
            # A synthetic object schema, so form fields reach SourceSchema the
            # same way any other declared body does.
            synthetic = {
                "type": "object",
                "properties": {
                    str(entry.get("name")): {
                        key: value
                        for key, value in entry.items()
                        if key in ("type", "format", "description", "enum")
                    }
                    for entry in form_fields
                    if isinstance(entry.get("name"), str)
                },
                "required": [
                    str(entry["name"])
                    for entry in form_fields
                    if entry.get("required") and isinstance(entry.get("name"), str)
                ],
            }
            entity_id, schema_name, _ = self._entity_for_contract(
                schema=synthetic,
                method=method,
                path=path,
                direction=ContractDirection.REQUEST,
                media_type="multipart/form-data",
                multiple_media=bool(bodies),
                status_code=None,
                pointer=f"{pointer}/parameters/formData",
            )
            bodies.append(
                ApiRequestBody(
                    media_type="multipart/form-data",
                    required=any(entry.get("required") for entry in form_fields),
                    entity_id=entity_id,
                    schema_name=schema_name,
                    structure_origin=StructureOrigin.DECLARED,
                )
            )

        return tuple(bodies)

    # ------------------------------------------------------------
    # Responses (Step 10)
    # ------------------------------------------------------------

    def _parse_responses(
        self,
        operation: Mapping[str, Any],
        method: HttpMethod,
        path: str,
        pointer: str,
    ) -> tuple[ApiResponse, ...]:
        responses = operation.get("responses")

        if not isinstance(responses, Mapping):
            return ()

        collected: list[ApiResponse] = []

        # Every declared status code, not just 200: a 4xx problem body is a
        # contract a consumer must handle.
        for status_code in sorted(responses, key=str):
            response = responses[status_code]

            if isinstance(response, Mapping) and isinstance(response.get("$ref"), str):
                resolved = self._resolver.resolve(response["$ref"])
                if resolved.is_usable and isinstance(resolved.target, Mapping):
                    response = resolved.target

            if not isinstance(response, Mapping):
                continue

            description = (
                truncate_description(
                    response.get("description"), self._options.max_description_length
                )
                if self._options.include_descriptions else None
            )

            if self._is_swagger_2:
                schema = response.get("schema")
                if not isinstance(schema, Mapping):
                    collected.append(
                        ApiResponse(status_code=str(status_code), description=description)
                    )
                    continue

                entity_id, schema_name, is_collection = self._entity_for_contract(
                    schema=schema,
                    method=method,
                    path=path,
                    direction=ContractDirection.RESPONSE,
                    media_type="application/json",
                    multiple_media=False,
                    status_code=str(status_code),
                    pointer=f"{pointer}/responses/{status_code}/schema",
                )
                collected.append(
                    ApiResponse(
                        status_code=str(status_code),
                        media_type="application/json",
                        entity_id=entity_id,
                        schema_name=schema_name,
                        is_collection=is_collection,
                        description=description,
                    )
                )
                continue

            content = response.get("content")
            if not isinstance(content, Mapping) or not content:
                collected.append(
                    ApiResponse(status_code=str(status_code), description=description)
                )
                continue

            for media_type in sorted(content, key=str):
                media = content[media_type]
                schema = media.get("schema") if isinstance(media, Mapping) else None

                if not isinstance(schema, Mapping):
                    # A non-JSON body - text/plain, an image. Recorded as a
                    # contract with a media type but no structure, rather than
                    # pretending it has fields.
                    collected.append(
                        ApiResponse(
                            status_code=str(status_code),
                            media_type=str(media_type),
                            description=description,
                        )
                    )
                    continue

                entity_id, schema_name, is_collection = self._entity_for_contract(
                    schema=schema,
                    method=method,
                    path=path,
                    direction=ContractDirection.RESPONSE,
                    media_type=str(media_type),
                    multiple_media=len(content) > 1,
                    status_code=str(status_code),
                    pointer=(
                        f"{pointer}/responses/{status_code}/content/{media_type}/schema"
                    ),
                )
                collected.append(
                    ApiResponse(
                        status_code=str(status_code),
                        media_type=str(media_type),
                        entity_id=entity_id,
                        schema_name=schema_name,
                        is_collection=is_collection,
                        description=description,
                    )
                )

        return tuple(collected)

    # ------------------------------------------------------------
    # Entity construction (Steps 19, 46, 47)
    # ------------------------------------------------------------

    def _entity_for_contract(
        self,
        schema: Mapping[str, Any],
        method: HttpMethod,
        path: str,
        direction: ContractDirection,
        media_type: str,
        multiple_media: bool,
        status_code: str | None,
        pointer: str,
    ) -> tuple[str | None, str | None, bool]:
        """Return ``(entity_name, schema_name, is_collection)`` for a contract.

        A ``$ref`` to a component schema LINKS to the entity that already
        describes it rather than duplicating it - which is what keeps
        ``GET /invoices`` and ``GET /invoices/{id}`` both pointing at one
        ``Invoice`` entity instead of two identical copies.
        """
        reference = schema.get("$ref")

        if isinstance(reference, str):
            target = reference_target_name(reference)
            if target and target in self._schema_entities:
                return self._schema_entities[target], target, False

            resolved = self._resolver.resolve(reference)
            if not resolved.is_usable:
                # The category distinguishes "we refused to fetch this" from
                # "this document does not contain it" - different problems with
                # different fixes, and only the first is a safety decision.
                category = (
                    "remote_reference_not_fetched"
                    if resolved.status is RefStatus.REMOTE_NOT_FETCHED
                    else "unresolved_contract_reference"
                )
                detail = (
                    "it points outside this document and Phase 7 performs no "
                    "network access"
                    if resolved.status is RefStatus.REMOTE_NOT_FETCHED
                    else f"the pointer resolved to {resolved.status.value}"
                )
                self._warn(
                    category,
                    f"Reference {reference!r} was not resolved: {detail}. This "
                    "contract has no structural description.",
                    pointer,
                )
                return None, target, False

        # "an array of Invoice" links to the ONE Invoice entity and records the
        # array-ness on the contract, rather than minting a second entity whose
        # fields are a copy of Invoice's and could later drift out of step with
        # them.
        collection_target = self._collection_element_reference(schema)
        if collection_target is not None:
            return self._schema_entities[collection_target], collection_target, True

        if self._is_scalar_contract(schema):
            # ``text/plain`` with ``type: string`` is a real contract, but it
            # has no fields. Minting an empty entity for it would add a
            # meaningless row to the catalog; the media type on the response
            # already says everything there is to say.
            return None, None, False

        display_name = build_inline_schema_name(
            method=method,
            path=path,
            direction=direction,
            status_code=status_code,
            media_type=media_type if multiple_media else None,
        )

        entity_name = self._build_entity(
            display_name=display_name,
            schema=schema,
            direction=direction,
            pointer=pointer,
            method=method,
            path=path,
            status_code=status_code,
            media_type=media_type,
        )

        return entity_name, display_name, False

    def _is_scalar_contract(self, schema: Mapping[str, Any]) -> bool:
        """Whether an inline schema describes a bare scalar rather than a shape.

        Only applied to inline operation contracts. A NAMED component schema
        that happens to be a scalar type alias still becomes an entity: its
        name is part of the API's declared vocabulary, whereas an inline
        ``type: string`` is just "this endpoint returns text".
        """
        if any(key in schema for key in ("properties", "items", "allOf",
                                         "oneOf", "anyOf", "$ref")):
            return False

        data_type, _ = normalize_schema_type(schema)

        return data_type not in (FieldDataType.OBJECT, FieldDataType.ARRAY)

    def _collection_element_reference(
        self, schema: Mapping[str, Any]
    ) -> str | None:
        """The component schema name a one-level array wraps, if any."""
        if schema.get("type") != "array":
            return None

        items = schema.get("items")
        if not isinstance(items, Mapping):
            return None

        reference = items.get("$ref")
        if not isinstance(reference, str):
            return None

        target = reference_target_name(reference)

        return target if target and target in self._schema_entities else None

    def _build_entity(
        self,
        display_name: str,
        schema: Mapping[str, Any],
        direction: ContractDirection,
        pointer: str,
        method: HttpMethod | None = None,
        path: str | None = None,
        status_code: str | None = None,
        media_type: str | None = None,
    ) -> str:
        converted = convert_schema_to_fields(schema, self._resolver, self._options)

        for warning in converted.warnings:
            self._warnings.add(warning)

        normalized_name = self._unique_entity_name(display_name)

        metadata: dict[str, Any] = {
            "spec_format": ApiSpecFormat.OPENAPI.value,
            "declared_name": display_name,
            "contract_direction": direction.value,
            "structure_origin": StructureOrigin.DECLARED.value,
            "schema_pointer": pointer,
            "root_type": converted.root_type.value,
            "root_source_type": converted.root_source_type,
            "field_count": len(converted.fields),
            "partial": converted.partial,
        }

        if method is not None:
            metadata["http_method"] = method.value
        if path is not None:
            metadata["http_path"] = path
        if status_code is not None:
            metadata["status_code"] = status_code
        if media_type is not None:
            metadata["media_type"] = media_type

        description = (
            truncate_description(
                schema.get("description"), self._options.max_description_length
            )
            if self._options.include_descriptions else None
        )

        self._entities.append(
            SourceEntity(
                entity_id=self._entity_id(normalized_name),
                source_name=display_name,
                normalized_name=normalized_name,
                # An API contract is a schema, not a table or a collection.
                entity_kind=EntityKind.API_SCHEMA,
                namespace=None,
                fields=converted.fields,
                # An API contract declares no database keys.
                primary_key_fields=(),
                description=description,
                metadata=metadata,
            )
        )

        if converted.referenced_schemas:
            self._pending_links(normalized_name, converted.referenced_schemas)

        return normalized_name

    def _pending_links(
        self, entity_name: str, references: Sequence[tuple[str, str]]
    ) -> None:
        """Record ``$ref`` links to resolve once every entity exists."""
        for field_path, target in references:
            self._link_queue.append((entity_name, field_path, target))

    def _link_referenced_schemas(self) -> None:
        """Turn declared ``$ref`` links into relationships (Step 21).

        These are DECLARED structural references, not guesses: the
        specification literally says this property is that schema. Field-name
        heuristics are never used - a property called ``customerId`` produces
        no relationship at all.

        ``EMBEDDED`` rather than ``REFERENCE`` because Phase 1 requires a
        key-based relationship to pair source and target fields one to one, and
        a ``$ref`` names no target field: in the serialized payload the
        referenced object appears inline.
        """
        if not self._options.include_reference_relationships:
            return

        seen: set[str] = set()

        for entity_name, field_path, target in self._link_queue:
            target_entity = self._schema_entities.get(target)

            if target_entity is None or target_entity == entity_name:
                continue

            try:
                from_field = normalize_identifier(field_path)
            except IdentityError:
                continue

            relationship_id = normalize_identifier(
                f"ref.{entity_name}.{from_field}.{target_entity}"
            )
            if relationship_id in seen:
                continue
            seen.add(relationship_id)

            self._relationships.append(
                SourceRelationship(
                    relationship_id=relationship_id,
                    relationship_type=RelationshipType.EMBEDDED,
                    from_entity=entity_name,
                    to_entity=target_entity,
                    from_fields=(from_field,),
                    to_fields=(),
                    # A declared $ref is fact, not inference.
                    confidence=1.0,
                    description=None,
                    metadata={
                        "declared_by": "$ref",
                        "source_field_path": field_path,
                        "target_schema": target,
                    },
                )
            )

    def _unique_entity_name(self, display_name: str) -> str:
        try:
            base = normalize_identifier(display_name)
        except IdentityError:
            base = f"schema.{hash_json_payload(display_name)[:12]}"

        count = self._entity_names.get(base, 0)
        self._entity_names[base] = count + 1

        if count == 0:
            return base

        candidate = f"{base}.{count + 1}"
        while candidate in self._entity_names:
            count += 1
            self._entity_names[base] = count + 1
            candidate = f"{base}.{count + 1}"

        self._entity_names[candidate] = 1
        return candidate

    def _entity_id(self, normalized_name: str) -> str:
        return normalize_identifier(
            f"{self._options.source_system_id}.{normalized_name}"
        )

    def _warn(self, category: str, message: str, pointer: str | None = None) -> None:
        self._warnings.add(
            ApiSpecWarning(category=category, message=message, pointer=pointer)
        )


# ============================================================
# Deterministic naming (Steps 46, 47)
# ============================================================

def build_operation_key(method: HttpMethod, path: str) -> str:
    """A stable key for one operation - never random, never order-dependent."""
    return normalize_identifier(f"{method.value}.{_path_slug(path)}")


def build_inline_schema_name(
    method: HttpMethod,
    path: str,
    direction: ContractDirection,
    status_code: str | None = None,
    media_type: str | None = None,
) -> str:
    """Name an inline (unnamed) schema deterministically.

    ``POST /invoices`` request  -> ``POST_invoices_request``
    ``GET /invoices/{id}`` 200  -> ``GET_invoices_id_response_200``

    Derived entirely from method, path, direction, status and media type, so
    two runs over the same document always produce the same names - a random
    or counter-based suffix would make every reparse look like a schema change.
    """
    parts = [method.value.upper(), _path_slug(path), direction.value]

    if status_code:
        parts.append(status_code)

    if media_type:
        parts.append(_media_slug(media_type))

    return "_".join(part for part in parts if part)


def _path_slug(path: str) -> str:
    """``/invoices/{id}/lines`` -> ``invoices_id_lines``."""
    cleaned = (
        path.replace("{", "").replace("}", "").replace(":", "").strip("/")
    )
    slug = cleaned.replace("/", "_") or "root"
    return slug


def _media_slug(media_type: str) -> str:
    """``application/problem+json`` -> ``problem_json``."""
    tail = media_type.split("/")[-1]
    return tail.replace("+", "_").replace(".", "_").replace("-", "_")


def merge_parameters(
    shared: Sequence[ApiParameter], own: Sequence[ApiParameter]
) -> tuple[ApiParameter, ...]:
    """Combine path-level and operation-level parameters.

    An operation-level parameter OVERRIDES a path-level one with the same
    ``(name, location)`` pair - that is what the OpenAPI specification
    requires, and getting it backwards would silently apply the wrong
    requiredness or type to an endpoint.
    """
    merged: dict[tuple[str, str], ApiParameter] = {
        (parameter.name, parameter.location.value): parameter for parameter in shared
    }

    for parameter in own:
        merged[(parameter.name, parameter.location.value)] = parameter

    return tuple(
        merged[key] for key in sorted(merged, key=lambda item: (item[1], item[0]))
    )


__all__ = [
    "PARSER_NAME",
    "SENSITIVE_HEADER_NAMES",
    "OpenApiParser",
    "build_operation_key",
    "build_inline_schema_name",
    "merge_parameters",
    "is_sensitive_header",
]
