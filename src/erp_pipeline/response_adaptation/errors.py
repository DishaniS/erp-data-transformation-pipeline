"""Typed errors for response adaptation.

Every failure mode gets its own class, for the same reason every other phase
does it: a caller should branch on what went wrong without matching on message
text, and the API error map should be able to turn each one into the right
status code.

PARTIAL SUCCESS IS NOT A FAILURE
--------------------------------
Most of these are raised by a SUB-STEP, not by the whole adaptation. An image
URL refused by policy must not discard the JSON fields that adapted perfectly
well - the service catches the asset-level errors and records them as warnings
on an otherwise successful result. Only the errors marked below as fatal stop
the whole adaptation.
"""

from __future__ import annotations

from typing import Any


class ResponseAdaptationError(Exception):
    """Base class for every adaptation failure."""


class AdaptationConfigurationError(ResponseAdaptationError):
    """The adaptation options or policy are internally inconsistent.

    Fatal, and raised at configuration time rather than per request, so a
    misconfigured weight is reported once instead of once per response.
    """


class UnsupportedResponseTypeError(ResponseAdaptationError):
    """The response is of a kind this phase does not adapt.

    Not automatically fatal: an unsupported BINARY body still yields a valid
    ``unsupported_binary`` asset rather than an error, which is the honest
    outcome. This is raised only when a caller explicitly demands a type that
    cannot be produced.
    """

    def __init__(self, message: str, response_type: str | None = None) -> None:
        super().__init__(message)
        self.response_type = response_type


class MalformedResponseError(ResponseAdaptationError):
    """The declared structured payload cannot be interpreted as ERP data.

    Fatal for the structured path: there is nothing to map.
    """

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class MappingUnavailableError(ResponseAdaptationError):
    """No canonical entity could be matched, so no ERP mapping is possible.

    Deliberately distinct from ``MalformedResponseError``: the payload was
    perfectly well-formed, the framework simply has no canonical vocabulary
    for what it describes. The caller can still receive the passthrough
    result, and the difference tells them whether to fix their data or extend
    the canonical model.
    """

    def __init__(self, message: str, entity_hint: str | None = None) -> None:
        super().__init__(message)
        self.entity_hint = entity_hint


class AssetError(ResponseAdaptationError):
    """Base class for asset-level failures. Never fatal to the whole run."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context


class AssetFetchRefusedError(AssetError):
    """A URL was refused before any request was made.

    Carries the specific rule that refused it, so the caller learns which
    policy to change rather than merely that the fetch did not happen.
    """

    def __init__(self, message: str, url: str | None = None, rule: str | None = None) -> None:
        super().__init__(message, url=url, rule=rule)
        self.url = url
        self.rule = rule


class AssetTooLargeError(AssetError):
    """An asset exceeded the configured size limit."""

    def __init__(self, message: str, size_bytes: int | None = None,
                 limit_bytes: int | None = None) -> None:
        super().__init__(message, size_bytes=size_bytes, limit_bytes=limit_bytes)
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


class InvalidAssetContentError(AssetError):
    """An asset's declared type and its actual bytes disagree.

    The same principle file ingestion already applies: a declared content type
    is a claim, and magic bytes are evidence.
    """

    def __init__(self, message: str, declared: str | None = None,
                 detected: str | None = None) -> None:
        super().__init__(message, declared=declared, detected=detected)
        self.declared = declared
        self.detected = detected


class AssetFetchFailedError(AssetError):
    """A permitted fetch failed - timeout, connection error, bad status."""


class BudgetExceededError(ResponseAdaptationError):
    """The output could not be brought within the configured budget.

    Raised only when truncation itself is disabled. With truncation enabled the
    output is bounded and the truncation is reported explicitly instead, because
    a bounded answer with a visible marker beats no answer at all.
    """

    def __init__(self, message: str, produced: int | None = None,
                 limit: int | None = None) -> None:
        super().__init__(message)
        self.produced = produced
        self.limit = limit


__all__ = [
    "ResponseAdaptationError",
    "AdaptationConfigurationError",
    "UnsupportedResponseTypeError",
    "MalformedResponseError",
    "MappingUnavailableError",
    "AssetError",
    "AssetFetchRefusedError",
    "AssetTooLargeError",
    "InvalidAssetContentError",
    "AssetFetchFailedError",
    "BudgetExceededError",
]
