"""Specification format and version detection, from CONTENT.

A filename tells you nothing useful here: ERP teams export
``swagger.json``, ``api.yaml``, ``collection.json`` and ``postman.json``
interchangeably, and a Postman collection is JSON exactly like an OpenAPI
document is. So detection reads the markers each format is required to
declare:

    OpenAPI 3.x   a top-level ``openapi: "3.x.y"`` string
    Swagger 2.0   a top-level ``swagger: "2.0"`` string
    Postman       ``info.schema`` naming a getpostman.com collection schema,
                  or an ``info`` + ``item`` pair

Anything else is refused rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from erp_pipeline.api_specs.errors import (
    UnsupportedSpecFormatError,
    UnsupportedSpecVersionError,
)
from erp_pipeline.api_specs.models import ApiSpecFormat

#: OpenAPI major versions this parser understands.
SUPPORTED_OPENAPI_MAJORS: tuple[str, ...] = ("3.0", "3.1")

#: The one Swagger version worth supporting - 2.0 is the only released one.
SUPPORTED_SWAGGER_VERSIONS: tuple[str, ...] = ("2.0",)

#: Postman collection schema versions. v1 is long obsolete and has a
#: fundamentally different shape, so it is refused rather than half-supported.
SUPPORTED_POSTMAN_MAJORS: tuple[str, ...] = ("2.0", "2.1")

_POSTMAN_SCHEMA_VERSION = re.compile(r"/collection/v?(\d+\.\d+)")


@dataclass(frozen=True)
class SpecDetectionResult:
    """What detection concluded, and on what evidence."""

    spec_format: ApiSpecFormat
    spec_version: str
    media_type: str
    detected_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_format": self.spec_format.value,
            "spec_version": self.spec_version,
            "media_type": self.media_type,
            "detected_by": self.detected_by,
        }


def detect_specification(document: Any, filename: str = "<spec>") -> SpecDetectionResult:
    """Identify a parsed document's format and version."""
    if not isinstance(document, Mapping):
        raise UnsupportedSpecFormatError(
            f"{filename!r} is not an API specification: its top level is a "
            f"{type(document).__name__}, but every supported format declares a "
            "JSON/YAML object at the root."
        )

    if "openapi" in document:
        return _detect_openapi_3(document, filename)

    if "swagger" in document:
        return _detect_swagger_2(document, filename)

    postman = _detect_postman(document, filename)
    if postman is not None:
        return postman

    raise UnsupportedSpecFormatError(
        f"{filename!r} declares no recognizable specification marker. Expected "
        "a top-level 'openapi' or 'swagger' version string, or a Postman "
        "'info.schema'. Phase 7 does not guess at unmarked documents."
    )


def _detect_openapi_3(
    document: Mapping[str, Any], filename: str
) -> SpecDetectionResult:
    declared = document.get("openapi")

    if not isinstance(declared, str) or not declared.strip():
        raise UnsupportedSpecVersionError(
            f"{filename!r} has an 'openapi' key that is not a version string.",
            declared_version=str(declared) if declared is not None else None,
        )

    version = declared.strip()
    major_minor = ".".join(version.split(".")[:2])

    if major_minor not in SUPPORTED_OPENAPI_MAJORS:
        raise UnsupportedSpecVersionError(
            f"{filename!r} declares OpenAPI {version}, which this parser does "
            f"not support. Supported: "
            f"{', '.join(SUPPORTED_OPENAPI_MAJORS)} and Swagger "
            f"{', '.join(SUPPORTED_SWAGGER_VERSIONS)}.",
            declared_version=version,
        )

    return SpecDetectionResult(
        spec_format=ApiSpecFormat.OPENAPI,
        spec_version=version,
        media_type="application/vnd.oai.openapi",
        detected_by="openapi_version_field",
    )


def _detect_swagger_2(
    document: Mapping[str, Any], filename: str
) -> SpecDetectionResult:
    declared = document.get("swagger")
    version = str(declared).strip() if declared is not None else ""

    if version not in SUPPORTED_SWAGGER_VERSIONS:
        raise UnsupportedSpecVersionError(
            f"{filename!r} declares Swagger {version!r}, which this parser does "
            f"not support. Supported: {', '.join(SUPPORTED_SWAGGER_VERSIONS)}.",
            declared_version=version or None,
        )

    return SpecDetectionResult(
        spec_format=ApiSpecFormat.OPENAPI,
        spec_version=version,
        media_type="application/vnd.oai.openapi",
        detected_by="swagger_version_field",
    )


def _detect_postman(
    document: Mapping[str, Any], filename: str
) -> SpecDetectionResult | None:
    """Recognize a Postman collection, or return ``None``.

    Returns ``None`` rather than raising for an unrecognized document, so the
    caller can produce one clear "not a specification" error instead of a
    format-specific one for a file that was never Postman in the first place.
    """
    info = document.get("info")

    if not isinstance(info, Mapping):
        return None

    schema = info.get("schema")

    if isinstance(schema, str) and "getpostman.com" in schema:
        match = _POSTMAN_SCHEMA_VERSION.search(schema)
        version = match.group(1) if match else ""

        if version not in SUPPORTED_POSTMAN_MAJORS:
            raise UnsupportedSpecVersionError(
                f"{filename!r} declares Postman collection schema "
                f"{version or 'of an unrecognized version'}, which this parser "
                f"does not support. Supported: "
                f"{', '.join(SUPPORTED_POSTMAN_MAJORS)}.",
                declared_version=version or None,
            )

        return SpecDetectionResult(
            spec_format=ApiSpecFormat.POSTMAN,
            spec_version=version,
            media_type="application/vnd.postman.collection+json",
            detected_by="postman_info_schema",
        )

    # A collection exported without its schema URL. The info+item pair is the
    # defining shape of a v2 collection, so it is accepted with the version
    # recorded as unknown rather than being silently assumed.
    if "item" in document and isinstance(document.get("item"), list):
        return SpecDetectionResult(
            spec_format=ApiSpecFormat.POSTMAN,
            spec_version="2.x",
            media_type="application/vnd.postman.collection+json",
            detected_by="postman_info_item_shape",
        )

    return None


__all__ = [
    "SUPPORTED_OPENAPI_MAJORS",
    "SUPPORTED_SWAGGER_VERSIONS",
    "SUPPORTED_POSTMAN_MAJORS",
    "SpecDetectionResult",
    "detect_specification",
]
