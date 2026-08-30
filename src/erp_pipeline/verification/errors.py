"""Typed errors for the verification layer."""

from __future__ import annotations


class VerificationError(Exception):
    """Base class for every verification-layer failure."""


class VerificationConfigurationError(VerificationError):
    """A scan was requested that the configured stores cannot support.

    Distinct from a failed check: a missing store means the question could not
    be asked, which must never be reported as a passing answer.
    """


class StoreUnavailableError(VerificationError):
    """A store could not be reached while a scan was running."""

    def __init__(self, message: str, store: str | None = None) -> None:
        super().__init__(message)
        self.store = store


__all__ = [
    "VerificationError",
    "VerificationConfigurationError",
    "StoreUnavailableError",
]
