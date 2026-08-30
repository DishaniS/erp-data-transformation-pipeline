"""Typed errors for the process/case layer.

Every failure mode gets its own class for the same reason the other phases do
it: a caller should be able to branch on what went wrong without matching on
message text.
"""

from __future__ import annotations


class ProcessError(Exception):
    """Base class for every process-layer failure."""


class ProcessConfigurationError(ProcessError):
    """The event-log configuration cannot describe the given source.

    Raised at configuration time, not per row, so a misconfigured field name is
    reported once rather than once per event.
    """


class EventNormalizationError(ProcessError):
    """One source row could not be turned into a process event.

    Carries the offending row's position so a caller can locate it without
    the row's business values being copied into the message.
    """

    def __init__(self, message: str, ordinal: int | None = None) -> None:
        super().__init__(message)
        self.ordinal = ordinal


class CaseBuildError(ProcessError):
    """A set of events could not be assembled into a case."""

    def __init__(self, message: str, case_id: str | None = None) -> None:
        super().__init__(message)
        self.case_id = case_id


__all__ = [
    "ProcessError",
    "ProcessConfigurationError",
    "EventNormalizationError",
    "CaseBuildError",
]
