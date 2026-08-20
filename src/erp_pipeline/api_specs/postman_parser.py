"""Postman collection (v2.0 / v2.1) parsing.

Postman is a fundamentally different problem from OpenAPI, and pretending
otherwise would produce a dishonest schema. An OpenAPI document DECLARES
types; a Postman collection declares none at all. What it contains is:

    a request body   a literal JSON payload someone once sent
    a saved response a literal payload the server once returned
    parameters       names, with example values, and no types

So every structural claim this parser makes is an OBSERVATION over examples,
and it says so: entities carry ``structure_origin =
inferred_from_examples``, and the schema-level origin is ``INFERRED`` rather
than ``API_SPEC``.

WHAT IS NEVER RETAINED
----------------------
A Postman collection is the single most credential-dense artifact this
framework ingests. Developers save real bearer tokens, real API keys and real
customer records in them. So:

    header values          never - only names, and a sensitive-name flag
    variable values        never - only names
    auth credentials       never - only the auth TYPE
    query/path values      never - only names
    body and response      parsed for STRUCTURE; values counted and discarded

Names are structure - a consumer must know the header is called
``X-Tenant-ID``. Values are one developer's data that happened to be saved.

SCRIPTS ARE NEVER EXECUTED
--------------------------
``prerequest`` and ``test`` scripts are JavaScript. This parser records only
that a script exists. It does not read its logic, evaluate it, or infer
behaviour from it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from erp_pipeline.api_specs.errors import SpecLimitExceededError, SpecStructureError
from erp_pipeline.api_specs.inference import (
    infer_fields_from_parameters,
    infer_structure_from_examples,
)
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
from erp_pipeline.api_specs.openapi_parser import is_sensitive_header
from erp_pipeline.api_specs.safety import WarningBudget, truncate_description
from erp_pipeline.schemas.enums import EntityKind, FieldDataType
from erp_pipeline.schemas.identity import IdentityError, hash_json_payload, normalize_identifier
from erp_pipeline.schemas.source_models import SourceEntity

PARSER_NAME = "postman"

#: ``{{baseUrl}}`` - Postman's variable syntax. Matched to recover NAMES; the
#: values those names resolve to are never read, and no environment file is
#: ever loaded.
_VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")

#: Postman body modes this parser understands.
_BODY_MODES = ("raw", "urlencoded", "formdata", "file", "graphql")


class PostmanParser:
    """Parses one Postman collection into operations and entities."""

    def __init__(
        self,
        document: Mapping[str, Any],
        spec_version: str,
        options: ApiSpecOptions | None = None,
    ) -> None:
        self._document = document
        self._version = spec_version
        self._options = options or ApiSpecOptions()
        self._warnings = WarningBudget(self._options.max_warnings)
        self._entities: list[SourceEntity] = []
        self._entity_names: dict[str, int] = {}
        self._variable_names: set[str] = set()

    @property
    def warnings(self) -> tuple[ApiSpecWarning, ...]:
        return self._warnings.items()

    # ------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------

    def parse(self) -> tuple[ApiSpecification, tuple[ApiOperation, ...],
                             tuple[SourceEntity, ...]]:
        items = self._document.get("item")

        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise SpecStructureError(
                "The collection declares no 'item' array, so it contains no "
                "requests to describe."
            )

        self._collect_collection_variables()

        operations = tuple(self._walk_items(items, folder_path=()))

        return self._specification_metadata(), operations, tuple(self._entities)

    # ------------------------------------------------------------
    # Collection metadata (Steps 26, 28)
    # ------------------------------------------------------------

    def _specification_metadata(self) -> ApiSpecification:
        info = self._document.get("info")
        info = info if isinstance(info, Mapping) else {}

        return ApiSpecification(
            spec_format=ApiSpecFormat.POSTMAN,
            spec_version=self._version,
            title=truncate_description(
                info.get("name"), self._options.max_description_length
            ),
            api_version=None,
            description=(
                truncate_description(
                    _description_text(info.get("description")),
                    self._options.max_description_length,
                )
                if self._options.include_descriptions else None
            ),
            server_paths=(),
            security_schemes=self._parse_auth(self._document.get("auth")),
            # NAMES only. A collection variable called "apiToken" is structure;
            # whatever it resolves to is a secret.
            variable_names=tuple(sorted(self._variable_names)),
        )

    def _collect_collection_variables(self) -> None:
        variables = self._document.get("variable")

        if not isinstance(variables, Sequence) or isinstance(variables, (str, bytes)):
            return

        for entry in variables:
            if isinstance(entry, Mapping) and isinstance(entry.get("key"), str):
                self._variable_names.add(str(entry["key"]))

    def _parse_auth(self, auth: Any) -> tuple[ApiSecurityScheme, ...]:
        """Record the auth TYPE and nothing else (Step 28).

        A Postman auth block contains live credentials - a bearer token, a
        basic password, an OAuth client secret. The type is contract
        information a consumer needs; the credential is not, and this parser
        has no field capable of carrying it.
        """
        if not isinstance(auth, Mapping):
            return ()

        auth_type = auth.get("type")

        if not isinstance(auth_type, str) or not auth_type:
            return ()

        return (
            ApiSecurityScheme(
                name=f"collection_auth_{auth_type}",
                scheme_type=str(auth_type),
            ),
        )

    # ------------------------------------------------------------
    # Folder traversal (Step 24)
    # ------------------------------------------------------------

    def _walk_items(
        self, items: Sequence[Any], folder_path: tuple[str, ...]
    ) -> list[ApiOperation]:
        """Walk the folder tree, preserving nesting.

        Items keep their declared order rather than being sorted: a Postman
        collection's order is authored - it is the sequence the API's own
        documentation walks through - and re-sorting it would discard that.
        Determinism is preserved because the file's order is itself fixed.
        """
        operations: list[ApiOperation] = []

        for entry in items:
            if not isinstance(entry, Mapping):
                continue

            name = str(entry.get("name") or "")
            nested = entry.get("item")

            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                operations.extend(
                    self._walk_items(nested, folder_path + (name,) if name else folder_path)
                )
                continue

            request = entry.get("request")
            if request is None:
                continue

            if len(operations) >= self._options.max_operations:
                raise SpecLimitExceededError(
                    f"The collection declares more than "
                    f"{self._options.max_operations} requests.",
                    limit_name="max_operations",
                    limit=self._options.max_operations,
                )

            operations.append(self._parse_request_item(entry, name, folder_path))

        return operations

    # ------------------------------------------------------------
    # One request (Steps 25, 27, 29, 30, 31)
    # ------------------------------------------------------------

    def _parse_request_item(
        self, item: Mapping[str, Any], name: str, folder_path: tuple[str, ...]
    ) -> ApiOperation:
        request = item.get("request")

        # Postman allows a bare URL string as the whole request.
        if isinstance(request, str):
            request = {"method": "GET", "url": request}

        request = request if isinstance(request, Mapping) else {}

        method = self._parse_method(request.get("method"), name)
        url_template, query_parameters, path_variables = self._parse_url(
            request.get("url")
        )

        headers = self._parse_headers(request.get("header"))
        operation_key = self._operation_key(method, url_template, folder_path, name)

        request_bodies = self._parse_body(
            request.get("body"), operation_key, name, folder_path
        )
        responses = self._parse_saved_responses(
            item.get("response"), operation_key, name, folder_path
        )

        return ApiOperation(
            operation_key=operation_key,
            method=method,
            path=url_template,
            operation_id=name or None,
            summary=truncate_description(name, self._options.max_description_length),
            description=(
                truncate_description(
                    _description_text(request.get("description")),
                    self._options.max_description_length,
                )
                if self._options.include_descriptions else None
            ),
            tags=(),
            deprecated=False,
            folder_path=folder_path,
            parameters=path_variables + query_parameters + headers,
            request_bodies=request_bodies,
            responses=responses,
            security_schemes=tuple(
                scheme.name for scheme in self._parse_auth(request.get("auth"))
            ),
            # Recorded as a fact. Never read, never evaluated.
            script_present=self._has_script(item),
        )

    def _parse_method(self, raw: Any, name: str) -> HttpMethod:
        if isinstance(raw, str) and raw.lower() in HTTP_METHODS_BY_NAME:
            return HTTP_METHODS_BY_NAME[raw.lower()]

        self._warn(
            "unknown_http_method",
            f"Request {name!r} declares an unrecognized method "
            f"{str(raw)!r}; recorded as GET.",
        )
        return HttpMethod.GET

    # ------------------------------------------------------------
    # URL (Steps 25, 26, 29)
    # ------------------------------------------------------------

    def _parse_url(
        self, url: Any
    ) -> tuple[str, tuple[ApiParameter, ...], tuple[ApiParameter, ...]]:
        """Normalize a Postman URL into a path template plus parameter names.

        Postman writes a URL either as a plain string or as a structured
        object. Both reduce to a path TEMPLATE - ``/invoices/:id`` - with the
        host and protocol dropped and every query VALUE discarded. Nothing here
        resolves a variable or contacts the address.
        """
        if isinstance(url, str):
            self._record_variables(url)
            return self._template_from_string(url), self._query_from_string(url), ()

        if not isinstance(url, Mapping):
            return "/", (), ()

        raw = url.get("raw")
        if isinstance(raw, str):
            self._record_variables(raw)

        segments = url.get("path")
        if isinstance(segments, Sequence) and not isinstance(segments, (str, bytes)):
            parts = [str(segment) for segment in segments]
            template = "/" + "/".join(part.strip("/") for part in parts if str(part))
        elif isinstance(raw, str):
            template = self._template_from_string(raw)
        else:
            template = "/"

        for part in re.findall(r"[:{]{1,2}([A-Za-z0-9_.\-]+)", template):
            self._variable_names.add(part)

        return (
            template or "/",
            self._parse_query_parameters(url.get("query")),
            self._parse_path_variables(url.get("variable")),
        )

    def _template_from_string(self, url: str) -> str:
        """Strip protocol, host and query, keeping the path template."""
        without_query = url.split("?", 1)[0]
        without_protocol = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", without_query)

        # Drop the host, but only when one is actually present: "{{baseUrl}}/x"
        # has a variable in the host position, and "/invoices/1" has no host.
        if not without_protocol.startswith("/"):
            parts = without_protocol.split("/", 1)
            without_protocol = "/" + parts[1] if len(parts) > 1 else "/"

        return without_protocol or "/"

    def _query_from_string(self, url: str) -> tuple[ApiParameter, ...]:
        """Recover query parameter NAMES from a raw URL string.

        Values are deliberately dropped: a documented query string routinely
        carries an api key or a real customer id.
        """
        if "?" not in url:
            return ()

        query = url.split("?", 1)[1]
        parameters: list[ApiParameter] = []

        for pair in query.split("&"):
            if not pair:
                continue
            name = pair.split("=", 1)[0].strip()
            if not name:
                continue
            self._record_variables(pair)
            parameters.append(
                ApiParameter(
                    name=name,
                    location=ParameterLocation.QUERY,
                    required=False,
                    data_type=FieldDataType.UNKNOWN.value,
                    source_data_type=None,
                )
            )

        return tuple(parameters)

    def _parse_query_parameters(self, raw: Any) -> tuple[ApiParameter, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()

        parameters: list[ApiParameter] = []

        for entry in raw:
            if not isinstance(entry, Mapping):
                continue

            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue

            # The VALUE is read only to harvest {{variable}} names from it, and
            # is never stored.
            value = entry.get("value")
            if isinstance(value, str):
                self._record_variables(value)

            parameters.append(
                ApiParameter(
                    name=key,
                    location=ParameterLocation.QUERY,
                    # Postman marks a parameter off with "disabled": true.
                    required=False,
                    enabled=not bool(entry.get("disabled", False)),
                    # Postman declares no types at all.
                    data_type=FieldDataType.UNKNOWN.value,
                    source_data_type=None,
                    description=(
                        truncate_description(
                            _description_text(entry.get("description")),
                            self._options.max_description_length,
                        )
                        if self._options.include_descriptions else None
                    ),
                )
            )

        return tuple(parameters)

    def _parse_path_variables(self, raw: Any) -> tuple[ApiParameter, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()

        parameters: list[ApiParameter] = []

        for entry in raw:
            if not isinstance(entry, Mapping):
                continue

            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue

            if isinstance(entry.get("value"), str):
                self._record_variables(entry["value"])

            parameters.append(
                ApiParameter(
                    name=key,
                    location=ParameterLocation.PATH,
                    required=True,
                    data_type=FieldDataType.UNKNOWN.value,
                    source_data_type=None,
                )
            )

        return tuple(parameters)

    def _record_variables(self, text: str) -> None:
        """Harvest ``{{name}}`` variable NAMES from a string."""
        for match in _VARIABLE_PATTERN.finditer(text):
            self._variable_names.add(match.group(1))

    # ------------------------------------------------------------
    # Headers (Step 27)
    # ------------------------------------------------------------

    def _parse_headers(self, raw: Any) -> tuple[ApiParameter, ...]:
        """Header NAMES and enabled state. Never a header value.

        ``Authorization: Bearer eyJ...`` is the most common way a real
        credential ends up in a committed collection, so no code path here
        reads ``entry["value"]`` into anything that is returned - it is
        consulted only to harvest ``{{variable}}`` names.
        """
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()

        headers: list[ApiParameter] = []

        for entry in raw:
            if not isinstance(entry, Mapping):
                continue

            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue

            value = entry.get("value")
            if isinstance(value, str):
                self._record_variables(value)

            headers.append(
                ApiParameter(
                    name=key,
                    location=ParameterLocation.HEADER,
                    required=False,
                    enabled=not bool(entry.get("disabled", False)),
                    data_type=FieldDataType.STRING.value,
                    source_data_type="string",
                    is_sensitive_name=is_sensitive_header(key),
                )
            )

        return tuple(headers)

    # ------------------------------------------------------------
    # Request bodies (Step 30)
    # ------------------------------------------------------------

    def _parse_body(
        self,
        body: Any,
        operation_key: str,
        name: str,
        folder_path: tuple[str, ...],
    ) -> tuple[ApiRequestBody, ...]:
        if not isinstance(body, Mapping):
            return ()

        mode = body.get("mode")

        if not isinstance(mode, str) or mode not in _BODY_MODES:
            return ()

        display_name = _contract_name(folder_path, name, ContractDirection.REQUEST)

        if mode == "raw":
            return self._parse_raw_body(body, display_name, name)

        if mode in ("urlencoded", "formdata"):
            return self._parse_form_body(body, mode, display_name, name)

        if mode == "file":
            # A file upload. Only the FACT is recorded - the referenced local
            # path is never opened.
            self._warn(
                "file_body_not_read",
                f"Request {name!r} uploads a file; only the presence of a "
                "binary body is recorded, and no local file was read.",
            )
            return (
                ApiRequestBody(
                    media_type="application/octet-stream",
                    required=True,
                    entity_id=None,
                    schema_name=None,
                    structure_origin=StructureOrigin.INFERRED_FROM_PARAMETERS,
                ),
            )

        # graphql - a query string, not a JSON data contract.
        return (
            ApiRequestBody(
                media_type="application/graphql",
                required=True,
                structure_origin=StructureOrigin.INFERRED_FROM_EXAMPLES,
            ),
        )

    def _parse_raw_body(
        self, body: Mapping[str, Any], display_name: str, name: str
    ) -> tuple[ApiRequestBody, ...]:
        raw = body.get("raw")

        if not isinstance(raw, str) or not raw.strip():
            return ()

        media_type = _raw_media_type(body)

        if len(raw.encode("utf-8", errors="ignore")) > self._options.max_example_body_bytes:
            self._warn(
                "body_too_large",
                f"The request body of {name!r} exceeds max_example_body_bytes "
                f"({self._options.max_example_body_bytes}) and was not parsed.",
            )
            return (ApiRequestBody(media_type=media_type, required=True),)

        payload = _try_parse_json(raw)

        if payload is None:
            if media_type in JSON_MEDIA_TYPES:
                self._warn(
                    "invalid_json_body",
                    f"The request body of {name!r} is declared as JSON but does "
                    "not parse; no structure was inferred from it.",
                )
            return (ApiRequestBody(media_type=media_type, required=True),)

        structure = infer_structure_from_examples([payload], self._options, display_name)

        entity_name = self._build_entity(
            display_name=display_name,
            structure=structure,
            direction=ContractDirection.REQUEST,
            media_type=media_type,
            status_code=None,
        )

        return (
            ApiRequestBody(
                media_type=media_type,
                required=True,
                entity_id=entity_name,
                schema_name=display_name,
                structure_origin=StructureOrigin.INFERRED_FROM_EXAMPLES,
            ),
        )

    def _parse_form_body(
        self, body: Mapping[str, Any], mode: str, display_name: str, name: str
    ) -> tuple[ApiRequestBody, ...]:
        entries = body.get(mode)

        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            return ()

        names: list[tuple[str, bool]] = []
        file_fields: list[str] = []

        for entry in entries:
            if not isinstance(entry, Mapping):
                continue

            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue

            if isinstance(entry.get("value"), str):
                self._record_variables(entry["value"])

            if entry.get("type") == "file":
                # Recorded as a binary input. The referenced local file - the
                # "src" - is never opened.
                file_fields.append(key)

            names.append((key, not bool(entry.get("disabled", False))))

        if not names:
            return ()

        fields = infer_fields_from_parameters(names)
        media_type = (
            "application/x-www-form-urlencoded" if mode == "urlencoded"
            else "multipart/form-data"
        )

        entity_name = self._build_entity_from_fields(
            display_name=display_name,
            fields=fields,
            direction=ContractDirection.REQUEST,
            media_type=media_type,
            status_code=None,
            structure_origin=StructureOrigin.INFERRED_FROM_PARAMETERS,
            extra_metadata={"file_fields": sorted(file_fields)} if file_fields else None,
        )

        return (
            ApiRequestBody(
                media_type=media_type,
                required=True,
                entity_id=entity_name,
                schema_name=display_name,
                structure_origin=StructureOrigin.INFERRED_FROM_PARAMETERS,
            ),
        )

    # ------------------------------------------------------------
    # Saved responses (Steps 31, 32, 33, 34)
    # ------------------------------------------------------------

    def _parse_saved_responses(
        self,
        raw: Any,
        operation_key: str,
        name: str,
        folder_path: tuple[str, ...],
    ) -> tuple[ApiResponse, ...]:
        """Infer response contracts from saved examples.

        Examples are grouped by STATUS CODE and the group is combined into one
        observed structure. Grouping matters: a 200 body and a 404 body are
        different contracts, and merging them would describe a response shape
        that never occurs. Within a group, combining is exactly right - two
        saved 200s that disagree tell you the field is optional or
        polymorphic, which is information a consumer needs.
        """
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()

        grouped: dict[str, list[Any]] = {}
        media_types: dict[str, str] = {}
        non_json: dict[str, str] = {}
        considered = 0

        for entry in raw:
            if not isinstance(entry, Mapping):
                continue

            if considered >= self._options.max_examples_per_operation:
                self._warn(
                    "example_limit",
                    f"Request {name!r} has more saved responses than "
                    f"max_examples_per_operation "
                    f"({self._options.max_examples_per_operation}); the rest "
                    "were not examined.",
                )
                break
            considered += 1

            status_code = str(entry.get("code") or entry.get("status") or "default")
            body = entry.get("body")
            media_type = _response_media_type(entry)

            if not isinstance(body, str) or not body.strip():
                media_types.setdefault(status_code, media_type)
                grouped.setdefault(status_code, [])
                continue

            if len(body.encode("utf-8", errors="ignore")) > self._options.max_example_body_bytes:
                self._warn(
                    "example_too_large",
                    f"A saved response of {name!r} exceeds "
                    f"max_example_body_bytes; it was not parsed.",
                )
                continue

            payload = _try_parse_json(body)

            if payload is None:
                # HTML, XML, plain text, binary. Recorded honestly with its
                # media type and NO invented fields - pretending it is JSON
                # would fabricate a structure.
                non_json[status_code] = media_type
                media_types.setdefault(status_code, media_type)
                grouped.setdefault(status_code, [])
                continue

            media_types.setdefault(status_code, media_type)
            grouped.setdefault(status_code, []).append(payload)

        responses: list[ApiResponse] = []

        for status_code in sorted(grouped, key=str):
            payloads = grouped[status_code]
            media_type = media_types.get(status_code, "application/json")

            if not payloads:
                if status_code in non_json:
                    self._warn(
                        "non_json_response_example",
                        f"A saved {status_code} response of {name!r} is "
                        f"{media_type}, which carries no JSON structure to "
                        "describe.",
                    )
                responses.append(
                    ApiResponse(
                        status_code=status_code,
                        media_type=media_type,
                        structure_origin=StructureOrigin.INFERRED_FROM_EXAMPLES,
                        examples_observed=0,
                    )
                )
                continue

            display_name = _contract_name(
                folder_path, name, ContractDirection.RESPONSE, status_code
            )
            structure = infer_structure_from_examples(
                payloads, self._options, display_name
            )

            entity_name = self._build_entity(
                display_name=display_name,
                structure=structure,
                direction=ContractDirection.RESPONSE,
                media_type=media_type,
                status_code=status_code,
            )

            responses.append(
                ApiResponse(
                    status_code=status_code,
                    media_type=media_type,
                    entity_id=entity_name,
                    schema_name=display_name,
                    structure_origin=StructureOrigin.INFERRED_FROM_EXAMPLES,
                    examples_observed=structure.examples_observed,
                )
            )

        return tuple(responses)

    # ------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------

    def _build_entity(
        self,
        display_name: str,
        structure: Any,
        direction: ContractDirection,
        media_type: str,
        status_code: str | None,
    ) -> str | None:
        if structure.is_empty and structure.root_type is FieldDataType.UNKNOWN:
            return None

        return self._build_entity_from_fields(
            display_name=display_name,
            fields=structure.fields,
            direction=direction,
            media_type=media_type,
            status_code=status_code,
            structure_origin=StructureOrigin.INFERRED_FROM_EXAMPLES,
            extra_metadata={
                "examples_observed": structure.examples_observed,
                "root_type": structure.root_type.value,
                "root_source_type": structure.root_source_type,
                "partial": structure.partial,
            },
        )

    def _build_entity_from_fields(
        self,
        display_name: str,
        fields: Sequence[Any],
        direction: ContractDirection,
        media_type: str,
        status_code: str | None,
        structure_origin: StructureOrigin,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> str:
        normalized_name = self._unique_entity_name(display_name)

        metadata: dict[str, Any] = {
            "spec_format": ApiSpecFormat.POSTMAN.value,
            "declared_name": display_name,
            "contract_direction": direction.value,
            # Honest provenance: a Postman collection declares no types, so
            # every structural claim here is an observation.
            "structure_origin": structure_origin.value,
            "media_type": media_type,
            "field_count": len(fields),
        }

        if status_code is not None:
            metadata["status_code"] = status_code

        if extra_metadata:
            metadata.update(dict(extra_metadata))

        self._entities.append(
            SourceEntity(
                entity_id=self._entity_id(normalized_name),
                source_name=display_name,
                normalized_name=normalized_name,
                entity_kind=EntityKind.API_SCHEMA,
                namespace=None,
                fields=tuple(fields),
                primary_key_fields=(),
                description=None,
                metadata=metadata,
            )
        )

        return normalized_name

    def _unique_entity_name(self, display_name: str) -> str:
        try:
            base = normalize_identifier(display_name)
        except IdentityError:
            base = f"contract.{hash_json_payload(display_name)[:12]}"

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

    def _operation_key(
        self,
        method: HttpMethod,
        path: str,
        folder_path: tuple[str, ...],
        name: str,
    ) -> str:
        """Deterministic key including the folder path.

        The folder participates because two folders legitimately contain a
        request with the same name against the same path - ``Admin/Get
        Invoice`` and ``Public/Get Invoice`` are different operations.
        """
        parts = list(folder_path) + [name or path]
        return normalize_identifier(f"{method.value}.{'.'.join(parts)}")

    @staticmethod
    def _has_script(item: Mapping[str, Any]) -> bool:
        """Whether a script exists. Its contents are never read or executed."""
        events = item.get("event")

        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            return False

        return any(
            isinstance(event, Mapping) and event.get("listen") in ("prerequest", "test")
            for event in events
        )

    def _warn(self, category: str, message: str, pointer: str | None = None) -> None:
        self._warnings.add(
            ApiSpecWarning(category=category, message=message, pointer=pointer)
        )


# ============================================================
# Helpers
# ============================================================

def _try_parse_json(text: str) -> Any:
    """Parse JSON, or return ``None``.

    Returning ``None`` rather than raising is deliberate: a saved response that
    is HTML is not an error in the collection, it is simply a response with no
    JSON structure to describe.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _raw_media_type(body: Mapping[str, Any]) -> str:
    options = body.get("options")

    if isinstance(options, Mapping):
        raw_options = options.get("raw")
        if isinstance(raw_options, Mapping):
            language = raw_options.get("language")
            if isinstance(language, str):
                return {
                    "json": "application/json",
                    "xml": "application/xml",
                    "html": "text/html",
                    "text": "text/plain",
                    "javascript": "application/javascript",
                }.get(language.lower(), "text/plain")

    return "application/json"


def _response_media_type(entry: Mapping[str, Any]) -> str:
    """The media type of a saved response, from its Content-Type header."""
    headers = entry.get("header")

    if isinstance(headers, Sequence) and not isinstance(headers, (str, bytes)):
        for header in headers:
            if not isinstance(header, Mapping):
                continue
            key = header.get("key")
            if isinstance(key, str) and key.lower() == "content-type":
                value = header.get("value")
                if isinstance(value, str) and value:
                    # The media type only - parameters such as charset are
                    # dropped, and no other header value is ever read.
                    return value.split(";", 1)[0].strip()

    postman_language = entry.get("_postman_previewlanguage")
    if isinstance(postman_language, str):
        return {
            "json": "application/json",
            "html": "text/html",
            "xml": "application/xml",
            "text": "text/plain",
        }.get(postman_language.lower(), "application/json")

    return "application/json"


def _description_text(value: Any) -> str | None:
    """Postman writes a description either as a string or as an object."""
    if isinstance(value, str):
        return value

    if isinstance(value, Mapping):
        content = value.get("content")
        if isinstance(content, str):
            return content

    return None


def _contract_name(
    folder_path: Sequence[str],
    request_name: str,
    direction: ContractDirection,
    status_code: str | None = None,
) -> str:
    """Deterministic contract name from folder path, request and direction.

    ``Invoices/Get Invoice`` 200 -> ``Invoices_Get Invoice_response_200``.
    Derived only from the collection's own structure, so a reparse produces
    identical names - a counter or a UUID would make every reparse look like a
    schema change.
    """
    parts = [part for part in folder_path if part]
    parts.append(request_name or "request")
    parts.append(direction.value)

    if status_code:
        parts.append(status_code)

    return "_".join(parts)


__all__ = [
    "PARSER_NAME",
    "PostmanParser",
]
