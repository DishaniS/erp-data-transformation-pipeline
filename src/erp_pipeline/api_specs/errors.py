"""Domain error hierarchy for API specification ingestion.

Follows the pattern every prior phase established: a parse failure surfaces as
one of these rather than a raw ``json``/``yaml`` exception, and ``__cause__``
always preserves the original.

PRIVACY RULE, enforced by test
------------------------------
No message constructed here may contain specification CONTENT that could carry
business or credential data - not an example payload, not a header value, not
a Postman variable value, not a token. An error may name only structural
context: a JSON pointer, a path, a method, a line/column from the parser, a
limit that was exceeded.

Declared *names* (a schema called ``Invoice``, a field called ``customerId``,
a header called ``Authorization``) are structure, not data, and may appear.
Their VALUES may not.
"""

from __future__ import annotations


class ApiSpecError(Exception):
    """Base class for every API-specification error."""


# ============================================================
# Loading and detection
# ============================================================

class SpecFileError(ApiSpecError):
    """Raised when the specification file is missing, unreadable, or too big."""


class UnsupportedSpecFormatError(ApiSpecError):
    """Raised when a document is neither an OpenAPI/Swagger spec nor a Postman
    collection.

    Phase 7 refuses to guess. A JSON document with no ``openapi``, ``swagger``
    or ``info.schema`` marker is rejected rather than speculatively parsed.
    """


class UnsupportedSpecVersionError(UnsupportedSpecFormatError):
    """Raised when the format is recognized but the version is not supported.

    Kept distinct from ``UnsupportedSpecFormatError`` because the remedy
    differs: an unsupported version means "this parser needs extending",
    whereas an unsupported format means "this is not an API specification".
    """

    def __init__(self, message: str, declared_version: str | None = None) -> None:
        super().__init__(message)
        self.declared_version = declared_version


class MalformedSpecError(ApiSpecError):
    """Raised when a document cannot be parsed as JSON or YAML at all.

    Carries the parser's own line/column when available - a position is
    actionable and is never sensitive.
    """

    def __init__(
        self,
        message: str,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(message)
        self.line = line
        self.column = column


class UnsafeSpecContentError(ApiSpecError):
    """Raised when a YAML document tries to construct arbitrary Python objects.

    ``yaml.safe_load`` refuses tags such as ``!!python/object/apply``, and this
    error reports that refusal explicitly rather than letting it surface as a
    generic parse failure - a spec attempting it is a security event, not a
    typo.
    """


# ============================================================
# Structure
# ============================================================

class SpecStructureError(ApiSpecError):
    """Raised when a document declares its format but violates that format's
    required shape - an OpenAPI document with no ``paths`` and no
    ``components``, a Postman collection with no ``item`` array."""


class ReferenceResolutionError(ApiSpecError):
    """Raised when a local ``$ref`` cannot be resolved at all.

    Cycles and depth limits do NOT raise: they are recorded structurally so a
    recursive model still produces a usable schema. This is for a pointer that
    names something the document simply does not contain.
    """

    def __init__(self, message: str, pointer: str | None = None) -> None:
        super().__init__(message)
        self.pointer = pointer


class SpecLimitExceededError(ApiSpecError):
    """Raised when a specification exceeds a configured structural budget.

    Only the budgets whose breach makes the result meaningless raise -
    operations, schemas and total size. Per-schema field caps, nesting depth
    and reference depth degrade to an explicitly partial result instead, since
    a truncated but honest schema is more useful than none.
    """

    def __init__(
        self,
        message: str,
        limit_name: str | None = None,
        limit: int | None = None,
        observed: int | None = None,
    ) -> None:
        super().__init__(message)
        self.limit_name = limit_name
        self.limit = limit
        self.observed = observed


__all__ = [
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
