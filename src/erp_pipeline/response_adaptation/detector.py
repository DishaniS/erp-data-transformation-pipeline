"""Classify what an ERP endpoint actually returned.

EVIDENCE ORDER, AND WHY
-----------------------
    1. magic bytes          what the payload IS
    2. payload structure    what the decoded body IS
    3. declared content type what the server SAYS it is
    4. fallback             an explicit "unknown", never a guess

Bytes outrank the declaration for the same reason file ingestion already
refuses to trust a filename: a legacy ERP that labels a PDF
``application/json`` is not a hypothetical, and believing the label would send
binary into a JSON parser. When the two disagree the bytes win AND the
disagreement is reported, so a caller learns their ERP is mislabelling rather
than silently receiving a different type than they asked for.

Filename extensions play no part here at all. A response has no filename.

REUSE
-----
The signature table comes from ``ingestion.detection``. Duplicating it would
mean two places to update when a format is added, and they would drift.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from erp_pipeline.ingestion.detection import _SIGNATURES, looks_like_text
from erp_pipeline.ingestion.models import FileType
from erp_pipeline.response_adaptation.models import (
    DetectionEvidence,
    DetectionResult,
    ResponseType,
)

#: How many leading bytes are examined. The longest signature is 8 bytes; the
#: rest of the window is for the text probe.
SNIFF_BYTES = 512

#: Declared content types that mean "structured data".
_STRUCTURED_TYPES = (
    "application/json",
    "text/json",
    "application/problem+json",
    "application/hal+json",
    "application/vnd.api+json",
)

#: FileType -> the adaptation path that handles it.
_FILE_TYPE_TO_RESPONSE: Mapping[FileType, ResponseType] = {
    FileType.PDF: ResponseType.DOCUMENT,
    FileType.IMAGE: ResponseType.IMAGE,
    FileType.CSV: ResponseType.STRUCTURED,
}


def normalize_content_type(content_type: str | None) -> str | None:
    """Strip parameters and case from a declared content type.

    ``application/json; charset=utf-8`` and ``APPLICATION/JSON`` are the same
    claim and must classify identically.
    """
    if not content_type:
        return None

    return content_type.split(";", 1)[0].strip().lower() or None


def _from_magic_bytes(raw: bytes) -> tuple[ResponseType, str] | None:
    """Positive identification from a byte signature, or ``None``."""
    prefix = raw[:SNIFF_BYTES]

    for signature, file_type, media_type in _SIGNATURES:
        if prefix.startswith(signature):
            return _FILE_TYPE_TO_RESPONSE[file_type], media_type

    return None


def _looks_like_json(raw: bytes) -> bool:
    """Whether a byte payload decodes as JSON.

    Parsed rather than pattern-matched: a body that merely starts with ``{``
    is not necessarily JSON, and this classification decides which parser runs
    next.
    """
    if not looks_like_text(raw[:SNIFF_BYTES]):
        return False

    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False

    return True


def _declared_type(content_type: str | None) -> ResponseType | None:
    """What the server's own label would imply."""
    normalized = normalize_content_type(content_type)

    if normalized is None:
        return None

    if normalized in _STRUCTURED_TYPES or normalized.endswith("+json"):
        return ResponseType.STRUCTURED

    if normalized == "application/pdf":
        return ResponseType.DOCUMENT

    if normalized.startswith("image/"):
        return ResponseType.IMAGE

    if normalized.startswith("application/") or normalized.startswith("binary/"):
        return ResponseType.BINARY

    return None


def detect_response_type(
    content_type: str | None = None,
    body: Any = None,
    raw: bytes | None = None,
) -> DetectionResult:
    """Classify one ERP response.

    A decoded ``body`` and ``raw`` bytes may both be supplied; bytes are
    examined first because they are the stronger evidence.
    """
    declared = normalize_content_type(content_type)
    implied = _declared_type(content_type)

    # -- 1. magic bytes --------------------------------------------------
    if raw:
        signature = _from_magic_bytes(raw)

        if signature is not None:
            response_type, media_type = signature

            return DetectionResult(
                response_type=response_type,
                evidence=DetectionEvidence.MAGIC_BYTES,
                media_type=media_type,
                declared_content_type=declared,
                content_type_mismatch=(
                    implied is not None and implied is not response_type
                ),
                detail=(
                    f"declared {declared!r} but the bytes are {media_type!r}"
                    if implied is not None and implied is not response_type
                    else None
                ),
            )

        if _looks_like_json(raw):
            return DetectionResult(
                response_type=ResponseType.STRUCTURED,
                evidence=DetectionEvidence.MAGIC_BYTES,
                media_type="application/json",
                declared_content_type=declared,
                content_type_mismatch=(
                    implied is not None and implied is not ResponseType.STRUCTURED
                ),
                detail="the byte payload parses as JSON",
            )

    # -- 2. decoded payload structure ------------------------------------
    if isinstance(body, (Mapping, list, tuple)):
        return DetectionResult(
            response_type=ResponseType.STRUCTURED,
            evidence=DetectionEvidence.PAYLOAD_STRUCTURE,
            media_type=declared or "application/json",
            declared_content_type=declared,
            content_type_mismatch=(
                implied is not None and implied is not ResponseType.STRUCTURED
            ),
            detail=(
                f"declared {declared!r} but the decoded body is a "
                f"{type(body).__name__}"
                if implied is not None and implied is not ResponseType.STRUCTURED
                else None
            ),
        )

    # -- 3. the server's own label ---------------------------------------
    if implied is not None:
        # An IMAGE or DOCUMENT claim with no bytes to back it cannot be
        # honoured: there is nothing to extract. Reported as UNKNOWN rather
        # than as a type whose handler would immediately fail.
        if implied in (ResponseType.IMAGE, ResponseType.DOCUMENT) and not raw:
            return DetectionResult(
                response_type=ResponseType.UNKNOWN,
                evidence=DetectionEvidence.CONTENT_TYPE,
                media_type=declared,
                declared_content_type=declared,
                detail=(
                    f"declared {declared!r} but the response carried no bytes "
                    "to extract"
                ),
            )

        return DetectionResult(
            response_type=implied,
            evidence=DetectionEvidence.CONTENT_TYPE,
            media_type=declared,
            declared_content_type=declared,
        )

    # -- 4. bytes we cannot name -----------------------------------------
    if raw:
        return DetectionResult(
            response_type=ResponseType.BINARY,
            evidence=DetectionEvidence.FALLBACK,
            media_type=declared or "application/octet-stream",
            declared_content_type=declared,
            detail="no signature matched and the payload is not text",
        )

    return DetectionResult(
        response_type=ResponseType.UNKNOWN,
        evidence=DetectionEvidence.FALLBACK,
        media_type=declared,
        declared_content_type=declared,
        detail="the response carried neither a structured body nor bytes",
    )


__all__ = [
    "SNIFF_BYTES",
    "normalize_content_type",
    "detect_response_type",
]
