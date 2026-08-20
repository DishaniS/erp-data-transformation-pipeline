"""Controlled errors raised by the transformation engine.

PRIVACY
-------
No exception in this module carries a business value. Messages name fields,
paths, types, codes and rules - never the data that failed. A stack trace from
a production run must be safe to paste into a ticket, and an engine that leaks
a customer's email into an error string has failed at exactly the job Phase 9
exists to do carefully.

RAISED VERSUS RECORDED
----------------------
Most per-record problems are NOT exceptions. A value that will not convert, a
missing required field, a duplicate - those are ordinary, expected outcomes of
transforming real ERP data, and they become ``DataQualityIssue`` objects
attached to a rejected record so the batch continues.

Exceptions here are for problems with the CONFIGURATION or the ENGINE, which
no amount of retrying different records will fix.
"""

from __future__ import annotations


class TransformationError(Exception):
    """Base class for every error this package raises."""


class TransformationConfigurationError(TransformationError):
    """The engine was configured in a way that cannot work.

    Contradictory thresholds, a normalization policy naming an unknown
    operation, a validation profile with an impossible range. Raised at
    construction time, before any record is touched, because failing on record
    50,000 for a problem visible at record 0 wastes the whole run.
    """


class UnsupportedOperationError(TransformationError):
    """A ``TransformationRule`` names an operation the engine cannot execute.

    Deliberately fatal rather than skipped. Silently ignoring a transformation
    step a mapping author declared would produce records that look successful
    while being wrong - the exact failure mode this phase must not have.
    """

    def __init__(self, message: str, operation: str | None = None) -> None:
        super().__init__(message)
        self.operation = operation


class ComputedFieldCycleError(TransformationConfigurationError):
    """Computed fields depend on each other in a cycle (Step 20).

    ``full_name`` needs ``display_name`` which needs ``full_name``. There is no
    evaluation order, so the configuration is rejected rather than evaluated in
    an arbitrary one.
    """

    #: The stable issue code a caller should record if it converts this
    #: configuration failure into a data-quality finding.
    code = "COMPUTED_FIELD_DEPENDENCY_CYCLE"

    def __init__(self, message: str, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fields = fields


class QualityThresholdExceeded(TransformationError):
    """A configured run-level quality threshold was breached under FAIL_FAST.

    Carries the run summary so a caller that stops early still gets the
    counters describing what was actually processed.
    """

    def __init__(self, message: str, summary: object | None = None) -> None:
        super().__init__(message)
        self.summary = summary


__all__ = [
    "TransformationError",
    "TransformationConfigurationError",
    "UnsupportedOperationError",
    "ComputedFieldCycleError",
    "QualityThresholdExceeded",
]
