"""The response envelope and the domain-error to HTTP-status mapping.

WHY A FIXED ENVELOPE
--------------------
A client should be able to branch on ``error.code`` without parsing prose. The
codes come from the orchestration layer's typed errors, so the wording can
change without breaking anybody.

WHY NOT EVERYTHING IS A 500
---------------------------
A 500 tells a caller nothing except "we broke". A missing source is a 404, a
reused idempotency key is a 409, an oversized upload is a 413, a downed Qdrant
is a 503. Each of those is actionable; a blanket 500 is not.
"""

from __future__ import annotations

from typing import Any, Mapping

from erp_pipeline.orchestration.errors import (
    DependencyUnavailableError,
    InvalidPipelineRequestError,
    JobConflictError,
    JobNotFoundError,
    MappingNotExecutableError,
    MappingNotFoundError,
    OrchestrationError,
    RecordNotFoundError,
    RetryNotSupportedError,
    SchemaNotFoundError,
    SecretUnavailableError,
    SourceNotFoundError,
    UnsafeUploadNameError,
    UnsupportedCapabilityError,
    UnsupportedUploadError,
    UploadNotFoundError,
    UploadTooLargeError,
)

#: Domain error -> HTTP status. Anything absent is a 500, which is correct:
#: an unmapped error is genuinely unexpected.
ERROR_STATUS: Mapping[type, int] = {
    InvalidPipelineRequestError: 422,
    UnsupportedUploadError: 415,
    UnsafeUploadNameError: 400,
    UploadTooLargeError: 413,
    SourceNotFoundError: 404,
    SchemaNotFoundError: 404,
    MappingNotFoundError: 404,
    RecordNotFoundError: 404,
    JobNotFoundError: 404,
    UploadNotFoundError: 404,
    JobConflictError: 409,
    MappingNotExecutableError: 409,
    RetryNotSupportedError: 409,
    UnsupportedCapabilityError: 422,
    SecretUnavailableError: 503,
    DependencyUnavailableError: 503,
}


def status_for(error: Exception) -> int:
    for error_type, status in ERROR_STATUS.items():
        if isinstance(error, error_type):
            return status

    return 500


def success(data: Any, request_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"success": True, "data": data}

    if request_id:
        payload["request_id"] = request_id

    return payload


def failure(
    code: str,
    message: str,
    request_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an error body.

    ``detail`` carries only structured, non-sensitive context that the domain
    error chose to attach - never an exception string, a traceback, a DSN or a
    row of business data.
    """
    error: dict[str, Any] = {"code": code, "message": message}

    if request_id:
        error["request_id"] = request_id

    if detail:
        error["detail"] = dict(detail)

    return {"success": False, "error": error}


def error_body(
    exc: Exception, request_id: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Convert an exception into (status, body), disclosing nothing extra."""
    status = status_for(exc)

    if isinstance(exc, OrchestrationError):
        return status, failure(exc.code, exc.message, request_id, exc.detail)

    # An unexpected exception's text is not ours to trust: it could contain a
    # connection string, a row value or a file path. Only the type is exposed.
    return status, failure(
        "INTERNAL_ERROR",
        "The request could not be completed due to an internal error.",
        request_id,
        {"exception_type": type(exc).__name__},
    )


__all__ = ["ERROR_STATUS", "status_for", "success", "failure", "error_body"]
