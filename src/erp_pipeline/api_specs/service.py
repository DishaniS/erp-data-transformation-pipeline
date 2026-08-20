"""The public entry point for API specification ingestion.

    result = parse_api_spec("erp-openapi.yaml")

    result.specification      # title, version, security schemes
    result.operations         # GET /invoices/{id}, its params, its responses
    result.schema             # Phase 1 SourceSchema - the common contract
    result.warnings

One service handles both formats. Detection reads the document's own markers,
so a caller never has to say which it is holding - and the two parsers return
the same ``ApiSpecificationResult`` rather than two unrelated dictionaries.

WHAT THIS SERVICE DOES NOT DO
-----------------------------
It does not call the API it just read. No endpoint is contacted, no token is
acquired, no OAuth flow is run, no remote ``$ref`` is fetched. Runtime ERP/API
execution belongs to the teammate's integration/MCP component, and the
boundary is enforced by static tests over this package rather than by
convention.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from erp_pipeline.api_specs.detection import SpecDetectionResult, detect_specification
from erp_pipeline.api_specs.errors import ApiSpecError, UnsupportedSpecFormatError
from erp_pipeline.api_specs.models import (
    ApiSpecFormat,
    ApiSpecificationResult,
    ApiSpecOptions,
    ApiSpecWarning,
    SpecProvenance,
)
from erp_pipeline.api_specs.openapi_parser import OpenApiParser
from erp_pipeline.api_specs.postman_parser import PostmanParser
from erp_pipeline.api_specs.safety import (
    load_document,
    read_spec_text,
    validate_spec_path,
    validate_spec_size,
)
from erp_pipeline.schemas.enums import SchemaOrigin
from erp_pipeline.schemas.identity import normalize_identifier
from erp_pipeline.schemas.source_models import SourceSchema, SourceSystem

#: Reused from Phase 6 rather than reimplemented, so the SAME bytes get the
#: SAME identity whichever phase reads them. A specification file ingested as a
#: generic file and parsed as a spec must not disagree about what it is, and
#: two independent SHA-256 implementations would eventually drift.
from erp_pipeline.ingestion.hashing import hash_file, make_file_id  # noqa: E402

_PROVISIONAL_SCHEMA_ID = "provisional.schema.id"


class ApiSpecificationService:
    """Parses local OpenAPI/Swagger documents and Postman collections."""

    def __init__(self, options: ApiSpecOptions | None = None) -> None:
        self._options = options or ApiSpecOptions()

    @property
    def options(self) -> ApiSpecOptions:
        return self._options

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def parse(self, path: str | os.PathLike[str]) -> ApiSpecificationResult:
        """Parse one specification file.

        Order of operations is fixed:

        1. validate the path (real, readable, regular file);
        2. enforce the size limit from filesystem metadata, BEFORE reading;
        3. read and parse the document - JSON, or YAML through ``safe_load``;
        4. detect format and version from the document's own markers;
        5. dispatch to the matching parser;
        6. assemble the ``SourceSchema`` with a content-addressed identity.
        """
        resolved = validate_spec_path(path)
        size_bytes = validate_spec_size(resolved, self._options.max_spec_size_bytes)

        text = read_spec_text(resolved)
        document = load_document(text, resolved.name)
        detection = detect_specification(document, resolved.name)

        content_hash = hash_file(resolved)

        if detection.spec_format is ApiSpecFormat.OPENAPI:
            return self._parse_openapi(
                document, detection, resolved, content_hash, size_bytes
            )

        return self._parse_postman(
            document, detection, resolved, content_hash, size_bytes
        )

    def describe(self, path: str | os.PathLike[str]) -> SpecDetectionResult:
        """Identify a specification's format and version without parsing it."""
        resolved = validate_spec_path(path)
        validate_spec_size(resolved, self._options.max_spec_size_bytes)

        text = read_spec_text(resolved)
        document = load_document(text, resolved.name)

        return detect_specification(document, resolved.name)

    def source_system(
        self, spec_format: ApiSpecFormat, name: str | None = None
    ) -> SourceSystem:
        """Build the Phase 1 ``SourceSystem`` these specifications belong to.

        Offered because publishing a schema requires a registered source
        system, and constructing one correctly - normalized id, right
        ``SourceType``, no credentials - should not be every caller's problem.
        """
        return SourceSystem(
            source_system_id=self._options.source_system_id,
            name=name or f"API specification ({spec_format.value})",
            source_type=spec_format.to_source_type(),
            description=(
                "API contracts read from a specification document. Phase 7 "
                "describes what the API accepts and returns; it never calls it."
            ),
        )

    def parse_and_publish(
        self,
        path: str | os.PathLike[str],
        catalog_service: Any,
        register_source_system: SourceSystem | None = None,
    ) -> tuple[ApiSpecificationResult, Any]:
        """Parse a specification and publish its schema through Phase 2.

        Returns ``(result, SchemaSnapshotResult)``. Whether a new
        ``catalog_version`` was created is decided entirely by the catalog;
        this method only forwards the schema.
        """
        result = self.parse(path)

        if register_source_system is not None:
            catalog_service.register_source_system(register_source_system)

        snapshot_result = catalog_service.publish_schema(result.schema)

        return result, snapshot_result

    # ------------------------------------------------------------
    # Format dispatch
    # ------------------------------------------------------------

    def _parse_openapi(
        self,
        document: Any,
        detection: SpecDetectionResult,
        path: Path,
        content_hash: str,
        size_bytes: int,
    ) -> ApiSpecificationResult:
        parser = OpenApiParser(document, detection.spec_version, self._options)
        specification, operations, entities, relationships = parser.parse()

        schema = self._build_schema(
            entities=entities,
            relationships=relationships,
            content_hash=content_hash,
            filename=path.name,
            detection=detection,
            # An OpenAPI document is a DECLARED contract, so its structure was
            # neither discovered from a live system nor inferred from samples.
            # Phase 1 provides an origin for exactly this case.
            origin=SchemaOrigin.API_SPEC,
            operations=operations,
        )

        return ApiSpecificationResult(
            specification=specification,
            provenance=self._provenance(
                content_hash, path, detection, size_bytes,
                parser_name="openapi",
                operation_count=len(operations),
                schema_count=len(entities),
            ),
            schema=schema,
            operations=operations,
            warnings=parser.warnings,
        )

    def _parse_postman(
        self,
        document: Any,
        detection: SpecDetectionResult,
        path: Path,
        content_hash: str,
        size_bytes: int,
    ) -> ApiSpecificationResult:
        parser = PostmanParser(document, detection.spec_version, self._options)
        specification, operations, entities = parser.parse()

        schema = self._build_schema(
            entities=entities,
            relationships=(),
            content_hash=content_hash,
            filename=path.name,
            detection=detection,
            # A Postman collection declares no types. Everything structural in
            # it was observed from example payloads, so INFERRED is the honest
            # origin - and per-entity metadata records which examples.
            origin=SchemaOrigin.INFERRED,
            operations=operations,
        )

        return ApiSpecificationResult(
            specification=specification,
            provenance=self._provenance(
                content_hash, path, detection, size_bytes,
                parser_name="postman",
                operation_count=len(operations),
                schema_count=len(entities),
            ),
            schema=schema,
            operations=operations,
            warnings=parser.warnings,
        )

    # ------------------------------------------------------------
    # Schema assembly (Steps 19, 20, 37, 38)
    # ------------------------------------------------------------

    def _build_schema(
        self,
        entities: Any,
        relationships: Any,
        content_hash: str,
        filename: str,
        detection: SpecDetectionResult,
        origin: SchemaOrigin,
        operations: Any,
    ) -> SourceSchema:
        schema_name = self._schema_name(filename)

        # Two-pass build, exactly as in Phases 4, 5 and 6:
        # compute_schema_hash() excludes schema_id, so a provisional id
        # computes the hash and the final content-addressed id derives from it.
        provisional = self._assemble(
            _PROVISIONAL_SCHEMA_ID, schema_name, entities, relationships,
            detection, origin, operations, None,
        )
        structural_hash = provisional.compute_schema_hash()

        return self._assemble(
            self._schema_id(schema_name, structural_hash),
            schema_name, entities, relationships, detection, origin, operations,
            structural_hash,
        )

    def _assemble(
        self,
        schema_id: str,
        schema_name: str,
        entities: Any,
        relationships: Any,
        detection: SpecDetectionResult,
        origin: SchemaOrigin,
        operations: Any,
        schema_hash: str | None,
    ) -> SourceSchema:
        return SourceSchema(
            schema_id=schema_id,
            source_system_id=self._options.source_system_id,
            schema_name=schema_name,
            origin=origin,
            entities=tuple(entities),
            relationships=tuple(relationships),
            schema_hash=schema_hash,
            metadata={
                "spec_format": detection.spec_format.value,
                "spec_version": detection.spec_version,
                "media_type": detection.media_type,
                "operation_count": len(operations),
                "entity_count": len(entities),
                # Stated as data so a stored snapshot says what kind of claim
                # it is making, without a reader having to know the phase.
                "schema_claim": (
                    "declared_api_contract" if origin is SchemaOrigin.API_SPEC
                    else "observed_from_examples"
                ),
                # The operation index. This is what stops the conversion to
                # SourceSchema losing which structure belongs to which
                # endpoint, in which direction.
                "operations": [
                    {
                        "operation_key": operation.operation_key,
                        "method": operation.method.value,
                        "path": operation.path,
                        "operation_id": operation.operation_id,
                        "folder_path": list(operation.folder_path),
                        "request_entity_ids": list(operation.request_entity_ids),
                        "response_entity_ids": list(operation.response_entity_ids),
                    }
                    for operation in operations
                ],
                # No endpoint was contacted to produce any of this.
                "network_access": "none",
            },
        )

    def _schema_name(self, filename: str) -> str:
        """The STABLE logical scope Phase 2 versions snapshots within.

        Derived from the filename stem, deliberately NOT from the content hash:
        the scope must not move when the specification changes, or an edited
        spec would start a fresh version-1 history instead of incrementing the
        existing one. Identity and scope answer different questions - identity
        is ``content_hash``.
        """
        stem = Path(filename).stem

        try:
            return normalize_identifier(stem)
        except Exception:
            return "api_specification"

    def _schema_id(self, schema_name: str, structural_hash: str) -> str:
        """Content-addressed snapshot identity, as in every prior phase.

            unchanged structure -> identical id -> still catalog version 1
            changed structure   -> new id       -> catalog version N+1
        """
        return normalize_identifier(
            f"{self._options.source_system_id}.{schema_name}."
            f"{structural_hash[:12]}"
        )

    def _provenance(
        self,
        content_hash: str,
        path: Path,
        detection: SpecDetectionResult,
        size_bytes: int,
        parser_name: str,
        operation_count: int,
        schema_count: int,
    ) -> SpecProvenance:
        return SpecProvenance(
            spec_id=make_file_id(content_hash),
            content_hash=content_hash,
            original_filename=path.name,
            spec_format=detection.spec_format,
            spec_version=detection.spec_version,
            media_type=detection.media_type,
            size_bytes=size_bytes,
            parser=parser_name,
            operation_count=operation_count,
            schema_count=schema_count,
        )


def parse_api_spec(
    path: str | os.PathLike[str], options: ApiSpecOptions | None = None
) -> ApiSpecificationResult:
    """Module-level convenience: parse one specification file."""
    return ApiSpecificationService(options).parse(path)


def describe_api_spec(
    path: str | os.PathLike[str], options: ApiSpecOptions | None = None
) -> SpecDetectionResult:
    """Module-level convenience: detect format and version without parsing."""
    return ApiSpecificationService(options).describe(path)


__all__ = [
    "ApiSpecificationService",
    "parse_api_spec",
    "describe_api_spec",
    "ApiSpecError",
    "UnsupportedSpecFormatError",
]
