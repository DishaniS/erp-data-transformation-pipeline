"""Typed orchestration failures.

Every error carries a stable ``code``. The API layer maps that code to an HTTP
status, so a caller can branch on the code rather than parse a message - and so
changing wording never breaks a client.

None of these carry credentials, source rows or vectors. An exception is the
single most likely place for a secret to escape, because it is the one object
everybody logs.
"""

from __future__ import annotations

from typing import Any


class OrchestrationError(Exception):
    """Base class. ``code`` is the contract; ``message`` is for humans."""

    code = "ORCHESTRATION_ERROR"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class JobNotFoundError(OrchestrationError):
    code = "JOB_NOT_FOUND"


class SourceNotFoundError(OrchestrationError):
    code = "SOURCE_NOT_FOUND"


class SchemaNotFoundError(OrchestrationError):
    code = "SCHEMA_NOT_FOUND"


class MappingNotFoundError(OrchestrationError):
    code = "MAPPING_NOT_FOUND"


class RecordNotFoundError(OrchestrationError):
    code = "RECORD_NOT_FOUND"


class RepresentationNotFoundError(OrchestrationError):
    """No stored representation for that id.

    Distinct from ``RECORD_NOT_FOUND`` so a caller can tell "this ERP record
    does not exist" apart from "this record exists but its AI text was never
    persisted" - which are different problems with different fixes.
    """

    code = "REPRESENTATION_NOT_FOUND"


class UploadNotFoundError(OrchestrationError):
    code = "UPLOAD_NOT_FOUND"


class UnsupportedCapabilityError(OrchestrationError):
    """The source type cannot do what was asked.

    Asking for record extraction from an OpenAPI document is the canonical
    case: the spec describes endpoints, and Phase 13 never calls them. That is
    a capability boundary, not a bug, so it gets its own code.
    """

    code = "UNSUPPORTED_CAPABILITY"


class InvalidPipelineRequestError(OrchestrationError):
    code = "INVALID_PIPELINE_REQUEST"


class SourceNativeNotPermittedError(OrchestrationError):
    """A source-native job named an entity the canonical model DOES cover.

    Refused so that source-native indexing cannot become a route around a
    mapping decision. The detail carries the ambiguous-field count and the
    mapping id, so the caller is told what to resolve rather than only that
    they were stopped.
    """

    code = "SOURCE_NATIVE_NOT_PERMITTED"


class MappingNotExecutableError(OrchestrationError):
    """The mapping still needs human review, so no data may flow through it.

    Continuing here would silently transform ERP records through a mapping
    nobody approved, which is the failure mode Phase 8 exists to prevent.
    """

    code = "MAPPING_REQUIRES_REVIEW"


class UploadTooLargeError(OrchestrationError):
    code = "UPLOAD_TOO_LARGE"


class UnsupportedUploadError(OrchestrationError):
    code = "UNSUPPORTED_UPLOAD"


class UnsafeUploadNameError(OrchestrationError):
    code = "UNSAFE_UPLOAD_NAME"


class DependencyUnavailableError(OrchestrationError):
    """A configured backing service (PostgreSQL, Qdrant, the model) is down."""

    code = "DEPENDENCY_UNAVAILABLE"


class JobConflictError(OrchestrationError):
    """An idempotency key was reused with a different payload."""

    code = "JOB_CONFLICT"


class SecretUnavailableError(OrchestrationError):
    code = "SECRET_UNAVAILABLE"


class RetryNotSupportedError(OrchestrationError):
    code = "RETRY_NOT_SUPPORTED"


__all__ = [
    "OrchestrationError",
    "JobNotFoundError",
    "SourceNotFoundError",
    "SchemaNotFoundError",
    "MappingNotFoundError",
    "RecordNotFoundError",
    "UploadNotFoundError",
    "UnsupportedCapabilityError",
    "InvalidPipelineRequestError",
    "MappingNotExecutableError",
    "UploadTooLargeError",
    "UnsupportedUploadError",
    "UnsafeUploadNameError",
    "DependencyUnavailableError",
    "JobConflictError",
    "SecretUnavailableError",
    "RetryNotSupportedError",
]
