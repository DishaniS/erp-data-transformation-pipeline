"""Scheduled incremental synchronisation (Phase 9).

WHAT THIS IS
------------
A TRIGGER, and nothing else. It decides *when* a source should be synchronised
and then submits the existing ``INCREMENTAL_SYNC`` job. It does not query a
source, transform a record, embed anything or write a vector - every one of
those already has an owner, and a scheduler that reimplemented them would be a
second synchronisation engine drifting away from the first.

WHAT IT DOES NOT CLAIM
----------------------
This is not CDC and not replication. There is no change stream; the sync engine
polls with a watermark. Freshness is therefore bounded by

    interval + processing time

which is *near-real-time synchronisation*, and the report says exactly that.

WHY THE CLOCK IS INJECTED
-------------------------
A scheduler tested by sleeping is a scheduler tested slowly and flakily. Time
and the tick are both supplied, so a test can advance an hour instantly and
assert precisely which sources became eligible.

WHY NOTHING STARTS ON IMPORT
----------------------------
No thread, no loop, no timer runs because this module was imported. The
scheduler is constructed explicitly and driven by ``tick()``; a deployment that
wants it running attaches it to application startup. Importing this package can
never cause a job to be submitted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

LOGGER = logging.getLogger("erp_pipeline.orchestration.scheduler")

SCHEDULER_SCHEMA_NAME = "erp_runtime"
LEASE_TABLE = "scheduler_lease"

#: The one lease every scheduler instance competes for. A single row, so
#: "who is the leader" is a primary-key question rather than a protocol.
LEASE_NAME = "incremental_sync"

#: Sensible for a research deployment, and never used unless scheduling is
#: explicitly enabled.
DEFAULT_INTERVAL_SECONDS = 60.0
#: Schema discovery is far more expensive than a watermark query, so drift runs
#: on its own, much longer, interval.
DEFAULT_DRIFT_INTERVAL_SECONDS = 3600.0
#: A lease must outlive a tick comfortably, or a slow tick loses leadership to
#: an instance that then races it.
DEFAULT_LEASE_SECONDS = 120.0


def _validate_schema(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        raise ValueError(f"{schema!r} is not a valid PostgreSQL schema name")

    return schema


Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ScheduledSource:
    """A source a deployment has explicitly made eligible for scheduling.

    Eligibility is opt-in per source. Registering a source is how you make it
    usable; it is not consent to have it polled forever afterwards.
    """

    source_id: str
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    entity: str | None = None
    drift_interval_seconds: float | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SchedulerConfig:
    """When and whether anything is scheduled at all.

    ``enabled`` defaults to False. A deployment that configures nothing polls
    nothing, which is the safe reading of an absent configuration.
    """

    enabled: bool = False
    sources: tuple[ScheduledSource, ...] = ()
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    #: Identifies this process in the lease row, so an operator can see which
    #: instance is leading.
    instance_id: str = "local"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "SchedulerConfig":
        """Read configuration from the environment. Disabled unless asked."""
        import os
        import uuid

        source = environ if environ is not None else os.environ

        def flag(name: str) -> bool:
            return str(source.get(name, "")).strip().lower() in {
                "1", "true", "yes", "on"
            }

        def number(name: str, fallback: float) -> float:
            try:
                return float(source.get(name) or fallback)
            except (TypeError, ValueError):
                return fallback

        raw_sources = (source.get("ERP_SYNC_SOURCES") or "").strip()
        interval = number("ERP_SYNC_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
        drift = number(
            "ERP_SYNC_DRIFT_INTERVAL_SECONDS", DEFAULT_DRIFT_INTERVAL_SECONDS
        )

        return cls(
            enabled=flag("ERP_SYNC_SCHEDULER_ENABLED"),
            sources=tuple(
                ScheduledSource(
                    source_id=name.strip(),
                    interval_seconds=interval,
                    drift_interval_seconds=drift,
                )
                for name in raw_sources.split(",")
                if name.strip()
            ),
            lease_seconds=number("ERP_SYNC_LEASE_SECONDS", DEFAULT_LEASE_SECONDS),
            instance_id=source.get("ERP_INSTANCE_ID") or str(uuid.uuid4())[:12],
        )


@dataclass
class SourceSchedule:
    """Mutable per-source timing state."""

    last_sync_started: datetime | None = None
    last_drift_started: datetime | None = None
    last_outcome: str | None = None
    consecutive_failures: int = 0

    def backoff_seconds(self, base: float) -> float:
        """How long to wait after repeated failures.

        A legacy database that is down should not be hammered once per interval
        forever. The delay doubles up to a ceiling and resets on success, which
        is enough for a research deployment and needs no retry platform.
        """
        if self.consecutive_failures <= 0:
            return 0.0

        return min(base * (2 ** min(self.consecutive_failures, 5)), base * 32)


@dataclass(frozen=True)
class TickResult:
    """What one scheduler tick did, and what it deliberately did not do."""

    submitted: tuple[str, ...] = ()
    drift_submitted: tuple[str, ...] = ()
    skipped_running: tuple[str, ...] = ()
    skipped_interval: tuple[str, ...] = ()
    skipped_backoff: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    held_lease: bool = True
    reason: str | None = None

    @property
    def did_nothing(self) -> bool:
        return not (self.submitted or self.drift_submitted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "submitted": list(self.submitted),
            "drift_submitted": list(self.drift_submitted),
            "skipped_running": list(self.skipped_running),
            "skipped_interval": list(self.skipped_interval),
            "skipped_backoff": list(self.skipped_backoff),
            "failed": list(self.failed),
            "held_lease": self.held_lease,
            "reason": self.reason,
        }


# ----------------------------------------------------------------------
# Leases
# ----------------------------------------------------------------------


def create_lease_sql(schema: str = SCHEDULER_SCHEMA_NAME) -> str:
    """One row per lease name. The primary key IS the mutual exclusion."""
    return f"""
CREATE TABLE IF NOT EXISTS {_validate_schema(schema)}.{LEASE_TABLE} (
    lease_name  TEXT        PRIMARY KEY,
    holder      TEXT        NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
)
"""


def bootstrap_scheduler_schema(
    engine: Any, schema: str = SCHEDULER_SCHEMA_NAME
) -> None:
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {_validate_schema(schema)}")
        )
        connection.execute(text(create_lease_sql(schema)))


class SingleProcessLease:
    """The lease for a deployment that runs exactly one scheduler.

    Always grants. Correct when the deployment genuinely runs one instance, and
    honest about being nothing more than that: it makes the single-instance
    assumption VISIBLE instead of leaving it implied by the absence of any
    coordination at all.
    """

    def __init__(self, holder: str = "local") -> None:
        self.holder = holder

    def acquire(self, now: datetime, lease_seconds: float) -> bool:
        return True

    def release(self) -> None:
        return None


class PostgresLease:
    """A lease that lets several instances run and only one of them lead.

    Acquisition is a single conditional UPDATE plus an insert-if-absent: an
    instance wins only if the lease is unheld, already its own, or expired.
    Two instances ticking simultaneously therefore produce one leader, because
    the row's primary key serialises them.
    """

    def __init__(
        self,
        engine: Any,
        holder: str,
        schema: str = SCHEDULER_SCHEMA_NAME,
        lease_name: str = LEASE_NAME,
    ) -> None:
        self._engine = engine
        self.holder = holder
        self._schema = _validate_schema(schema)
        self._lease_name = lease_name

    def acquire(self, now: datetime, lease_seconds: float) -> bool:
        from sqlalchemy import text

        expires = now + timedelta(seconds=lease_seconds)
        table = f"{self._schema}.{LEASE_TABLE}"

        with self._engine.begin() as connection:
            updated = connection.execute(
                text(
                    f"""
                    UPDATE {table}
                       SET holder = :holder,
                           acquired_at = :now,
                           expires_at = :expires
                     WHERE lease_name = :name
                       AND (holder = :holder OR expires_at < :now)
                    """
                ),
                {
                    "holder": self.holder, "now": now, "expires": expires,
                    "name": self._lease_name,
                },
            )

            if updated.rowcount:
                return True

            # No row yet. INSERT ... DO NOTHING makes the first writer the
            # holder and every simultaneous rival a non-holder, without either
            # having to check first and race.
            inserted = connection.execute(
                text(
                    f"""
                    INSERT INTO {table} (
                        lease_name, holder, acquired_at, expires_at
                    ) VALUES (:name, :holder, :now, :expires)
                    ON CONFLICT (lease_name) DO NOTHING
                    """
                ),
                {
                    "name": self._lease_name, "holder": self.holder,
                    "now": now, "expires": expires,
                },
            )

            return bool(inserted.rowcount)

    def release(self) -> None:
        from sqlalchemy import text

        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"DELETE FROM {self._schema}.{LEASE_TABLE} "
                    "WHERE lease_name = :name AND holder = :holder"
                ),
                {"name": self._lease_name, "holder": self.holder},
            )


# ----------------------------------------------------------------------
# The scheduler
# ----------------------------------------------------------------------


class SyncScheduler:
    """Decides which configured sources are due, and submits their sync jobs."""

    def __init__(
        self,
        orchestration: Any,
        config: SchedulerConfig | None = None,
        lease: Any = None,
        clock: Clock = system_clock,
    ) -> None:
        self._orchestration = orchestration
        self._config = config or SchedulerConfig()
        self._lease = lease or SingleProcessLease(self._config.instance_id)
        self._clock = clock
        self._schedules: dict[str, SourceSchedule] = {}
        self.last_tick_at: datetime | None = None
        self.last_result: TickResult | None = None

    @property
    def config(self) -> SchedulerConfig:
        return self._config

    def schedule_for(self, source_id: str) -> SourceSchedule:
        return self._schedules.setdefault(source_id, SourceSchedule())

    def tick(self) -> TickResult:
        """One scheduling decision. Submits nothing it should not.

        Never raises: a scheduler that dies on a bad source stops synchronising
        every other source too.
        """
        now = self._clock()
        self.last_tick_at = now

        if not self._config.enabled:
            self.last_result = TickResult(reason="scheduler disabled")
            return self.last_result

        if not self._config.sources:
            self.last_result = TickResult(reason="no sources configured")
            return self.last_result

        if not self._lease.acquire(now, self._config.lease_seconds):
            # Another instance is leading. Doing nothing is the correct
            # behaviour, not a failure.
            self.last_result = TickResult(
                held_lease=False, reason="another instance holds the lease"
            )
            return self.last_result

        submitted: list[str] = []
        drift: list[str] = []
        running: list[str] = []
        waiting: list[str] = []
        backoff: list[str] = []
        failed: list[str] = []

        for source in self._config.sources:
            schedule = self.schedule_for(source.source_id)

            if self._has_active_sync(source.source_id):
                # A tick that fires while the previous sync is still running
                # must not start a second one: two runs racing on one watermark
                # is how a change gets skipped.
                running.append(source.source_id)
                continue

            if not self._is_due(schedule, source, now):
                waiting.append(source.source_id)
                continue

            if self._in_backoff(schedule, source, now):
                backoff.append(source.source_id)
                continue

            if self._submit_sync(source, schedule, now):
                submitted.append(source.source_id)
            else:
                failed.append(source.source_id)

            if self._drift_due(schedule, source, now):
                if self._submit_drift(source, schedule, now):
                    drift.append(source.source_id)

        self.last_result = TickResult(
            submitted=tuple(submitted),
            drift_submitted=tuple(drift),
            skipped_running=tuple(running),
            skipped_interval=tuple(waiting),
            skipped_backoff=tuple(backoff),
            failed=tuple(failed),
        )

        return self.last_result

    # ------------------------------------------------------------

    def _has_active_sync(self, source_id: str) -> bool:
        """Whether a sync for this source is already pending or running."""
        from erp_pipeline.orchestration.models import JobStatus, JobType

        try:
            jobs = self._orchestration.list(
                job_type=JobType.INCREMENTAL_SYNC, source_id=source_id, limit=25
            )
        except Exception:  # noqa: BLE001 - an unreadable job store is not a
            # reason to start a job that may already be running.
            return True

        return any(
            getattr(job, "status", None)
            in {JobStatus.PENDING, JobStatus.RUNNING}
            for job in jobs
        )

    def _is_due(
        self, schedule: SourceSchedule, source: ScheduledSource, now: datetime
    ) -> bool:
        if schedule.last_sync_started is None:
            return True

        elapsed = (now - schedule.last_sync_started).total_seconds()

        return elapsed >= source.interval_seconds

    def _in_backoff(
        self, schedule: SourceSchedule, source: ScheduledSource, now: datetime
    ) -> bool:
        delay = schedule.backoff_seconds(source.interval_seconds)

        if delay <= 0 or schedule.last_sync_started is None:
            return False

        return (now - schedule.last_sync_started).total_seconds() < delay

    def _drift_due(
        self, schedule: SourceSchedule, source: ScheduledSource, now: datetime
    ) -> bool:
        interval = source.drift_interval_seconds

        if not interval or interval <= 0:
            return False

        if schedule.last_drift_started is None:
            return True

        return (now - schedule.last_drift_started).total_seconds() >= interval

    def _submit_sync(
        self, source: ScheduledSource, schedule: SourceSchedule, now: datetime
    ) -> bool:
        from erp_pipeline.orchestration.models import JobRequest, JobType

        schedule.last_sync_started = now

        try:
            self._orchestration.submit(
                JobRequest(
                    job_type=JobType.INCREMENTAL_SYNC,
                    source_id=source.source_id,
                    entity=source.entity,
                    options=dict(source.options),
                )
            )
        except Exception as error:  # noqa: BLE001 - reported, never raised
            schedule.consecutive_failures += 1
            schedule.last_outcome = type(error).__name__
            LOGGER.warning(
                "scheduled sync for %s could not be submitted (%s)",
                source.source_id, type(error).__name__,
            )
            return False

        schedule.consecutive_failures = 0
        schedule.last_outcome = "submitted"

        return True

    def _submit_drift(
        self, source: ScheduledSource, schedule: SourceSchedule, now: datetime
    ) -> bool:
        from erp_pipeline.orchestration.models import JobRequest, JobType

        schedule.last_drift_started = now

        try:
            self._orchestration.submit(
                JobRequest(
                    job_type=JobType.DRIFT_CHECK, source_id=source.source_id
                )
            )
        except Exception:  # noqa: BLE001 - drift is secondary to data sync
            return False

        return True

    # ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Operational state, for health reporting. No new endpoint."""
        return {
            "enabled": self._config.enabled,
            "instance_id": self._config.instance_id,
            "configured_sources": [s.source_id for s in self._config.sources],
            "last_tick_at": (
                self.last_tick_at.isoformat() if self.last_tick_at else None
            ),
            "holds_lease": (
                self.last_result.held_lease if self.last_result else None
            ),
            "last_tick": self.last_result.to_dict() if self.last_result else None,
            "sources": {
                source_id: {
                    "last_sync_started": (
                        schedule.last_sync_started.isoformat()
                        if schedule.last_sync_started else None
                    ),
                    "last_outcome": schedule.last_outcome,
                    "consecutive_failures": schedule.consecutive_failures,
                }
                for source_id, schedule in self._schedules.items()
            },
        }


__all__ = [
    "DEFAULT_DRIFT_INTERVAL_SECONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "LEASE_NAME",
    "PostgresLease",
    "ScheduledSource",
    "SchedulerConfig",
    "SingleProcessLease",
    "SourceSchedule",
    "SyncScheduler",
    "TickResult",
    "bootstrap_scheduler_schema",
    "create_lease_sql",
    "system_clock",
]
