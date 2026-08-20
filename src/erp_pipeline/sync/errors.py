"""Controlled errors raised by the incremental sync framework.

PRIVACY
-------
No exception here carries a business value. Messages name systems, entities,
record keys, stages and watermarks - never the contents of a changed row. A
sync run touches live ERP data, so an error string that leaked an invoice
total would defeat the point of the privacy work in Phase 9.

RAISED VERSUS RECORDED
----------------------
A record that fails to transform is not an exception - it is an expected
outcome, recorded as a quarantined change so the run continues under the
configured policy.

Exceptions are for problems no amount of retrying different records will fix:
a misconfigured strategy, a lost checkpoint race, a schema that has drifted
beyond what the active mapping can survive.
"""

from __future__ import annotations


class SyncError(Exception):
    """Base class for every error this package raises."""


class SyncConfigurationError(SyncError):
    """The sync was configured in a way that cannot work.

    A timestamp strategy with no watermark field, a batch size of zero, a
    composite strategy with no tie-breaker. Raised before any record is read,
    because discovering it mid-run risks leaving a checkpoint in a state nobody
    intended.
    """


class UnsupportedStrategyError(SyncConfigurationError):
    """The source cannot support the requested change-detection strategy.

    Deliberately loud rather than silently degrading to "full reload". A
    MongoDB collection with no update marker genuinely cannot report updates,
    and pretending otherwise would produce a sync that looks incremental and
    quietly misses changes.
    """


class CheckpointConflictError(SyncError):
    """Another run advanced the checkpoint for this entity concurrently.

    Detected by an optimistic version check on the persisted sync state. The
    losing run aborts rather than overwriting: two runs advancing the same
    watermark independently is exactly how changes get skipped.
    """

    def __init__(
        self,
        message: str,
        expected_version: int | None = None,
        actual_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.expected_version = expected_version
        self.actual_version = actual_version


class SyncBlockedError(SyncError):
    """Schema drift makes it unsafe to process data changes.

    Raised by the drift gate when an active mapping can no longer be trusted -
    a mapped source field removed, a primary key changed. Transforming under a
    known-incompatible schema would produce canonical records that are wrong in
    a way no downstream stage can detect.
    """

    def __init__(self, message: str, report: object | None = None) -> None:
        super().__init__(message)
        self.report = report


class PropagationError(SyncError):
    """A downstream propagation stage failed for one change.

    Carries the stage so the run can record where the pipeline stopped and a
    retry can resume from a known point rather than from the beginning.
    """

    def __init__(self, message: str, stage: object | None = None) -> None:
        super().__init__(message)
        self.stage = stage


__all__ = [
    "SyncError",
    "SyncConfigurationError",
    "UnsupportedStrategyError",
    "CheckpointConflictError",
    "SyncBlockedError",
    "PropagationError",
]
