"""Phase 9 - the scheduler decides WHEN, and nothing else.

Every test drives an injected clock. Nothing sleeps, so the suite stays
deterministic and fast, and a test can advance an hour to assert exactly which
sources became eligible.

The two that matter most are ``test_a_running_sync_blocks_the_next_tick`` and
``test_only_one_instance_leads_a_tick``: both guard against two runs racing on
one watermark, which is how a source change gets silently skipped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from erp_pipeline.orchestration.models import JobRequest, JobStatus, JobType
from erp_pipeline.orchestration.scheduler import (
    DEFAULT_INTERVAL_SECONDS,
    ScheduledSource,
    SchedulerConfig,
    SingleProcessLease,
    SourceSchedule,
    SyncScheduler,
)

START = datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)


class Clock:
    """An injected clock. Tests advance it; nothing sleeps."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeJob:
    def __init__(self, source_id, status, job_type=JobType.INCREMENTAL_SYNC):
        self.source_id = source_id
        self.status = status
        self.job_type = job_type


class FakeOrchestration:
    """Records submissions and reports whatever job history a test wants."""

    def __init__(self, existing=None, fail_on=None):
        self.submitted: list[JobRequest] = []
        self.existing = list(existing or [])
        self.fail_on = fail_on or set()

    def submit(self, request, idempotency_key=None):
        if request.source_id in self.fail_on:
            raise RuntimeError("source unreachable")

        self.submitted.append(request)

        return FakeJob(request.source_id, JobStatus.PENDING, request.job_type)

    def list(self, job_type=None, source_id=None, limit=50, **kwargs):
        return [
            job for job in self.existing
            if (source_id is None or job.source_id == source_id)
            and (job_type is None or job.job_type == job_type)
        ]

    @property
    def sync_requests(self):
        return [
            request for request in self.submitted
            if request.job_type is JobType.INCREMENTAL_SYNC
        ]


def build(sources=("legacy_hr",), enabled=True, interval=60.0, drift=None,
          orchestration=None, clock=None, lease=None):
    config = SchedulerConfig(
        enabled=enabled,
        sources=tuple(
            ScheduledSource(
                source_id=name, interval_seconds=interval,
                drift_interval_seconds=drift,
            )
            for name in sources
        ),
    )

    return SyncScheduler(
        orchestration or FakeOrchestration(),
        config,
        lease=lease or SingleProcessLease(),
        clock=clock or Clock(),
    )


# ======================================================================
# TEST A - disabled by default
# ======================================================================


def test_a_scheduler_with_no_configuration_submits_nothing():
    """The safe reading of an absent configuration is: poll nothing."""
    orchestration = FakeOrchestration()
    scheduler = SyncScheduler(orchestration, SchedulerConfig(), clock=Clock())
    clock = Clock()

    for _ in range(10):
        scheduler.tick()

    assert orchestration.submitted == []


def test_an_explicitly_disabled_scheduler_never_submits():
    clock = Clock()
    orchestration = FakeOrchestration()
    scheduler = build(enabled=False, orchestration=orchestration, clock=clock)

    for _ in range(20):
        clock.advance(600)
        scheduler.tick()

    assert orchestration.submitted == []
    assert scheduler.last_result.reason == "scheduler disabled"


def test_an_enabled_scheduler_with_no_sources_submits_nothing():
    """Enabling scheduling is not consent to poll every registered source."""
    orchestration = FakeOrchestration()
    scheduler = SyncScheduler(
        orchestration, SchedulerConfig(enabled=True), clock=Clock()
    )
    scheduler.tick()

    assert orchestration.submitted == []
    assert scheduler.last_result.reason == "no sources configured"


def test_the_environment_default_is_disabled():
    assert SchedulerConfig.from_environment({}).enabled is False
    assert SchedulerConfig.from_environment({}).sources == ()


def test_the_environment_can_enable_named_sources():
    config = SchedulerConfig.from_environment({
        "ERP_SYNC_SCHEDULER_ENABLED": "true",
        "ERP_SYNC_SOURCES": "legacy_hr, finance_erp",
        "ERP_SYNC_INTERVAL_SECONDS": "30",
    })

    assert config.enabled is True
    assert [s.source_id for s in config.sources] == ["legacy_hr", "finance_erp"]
    assert config.sources[0].interval_seconds == 30.0


# ======================================================================
# TEST B - enabled, and it reuses the existing job
# ======================================================================


def test_an_eligible_source_gets_an_incremental_sync_job():
    orchestration = FakeOrchestration()
    scheduler = build(orchestration=orchestration)
    result = scheduler.tick()

    assert result.submitted == ("legacy_hr",)
    assert len(orchestration.submitted) == 1

    request = orchestration.submitted[0]

    assert request.job_type is JobType.INCREMENTAL_SYNC
    assert request.source_id == "legacy_hr"


def test_the_scheduler_only_triggers_and_never_synchronises_itself():
    """It submits a job. It does not touch a source, a vector or a watermark."""
    import ast
    import inspect

    from erp_pipeline.orchestration import scheduler as module

    tree = ast.parse(inspect.getsource(module))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    for forbidden in (
        "extract", "transform", "embed", "upsert", "store_vector",
        "save_watermark", "advanced_to",
    ):
        assert forbidden not in called, forbidden


# ======================================================================
# TEST C / DR32 - never two runs on one watermark
# ======================================================================


def test_a_running_sync_blocks_the_next_tick():
    """Two syncs racing on one watermark is how a change gets skipped."""
    orchestration = FakeOrchestration(
        existing=[FakeJob("legacy_hr", JobStatus.RUNNING)]
    )
    scheduler = build(orchestration=orchestration)
    result = scheduler.tick()

    assert result.submitted == ()
    assert result.skipped_running == ("legacy_hr",)
    assert orchestration.submitted == []


def test_a_pending_sync_also_blocks():
    orchestration = FakeOrchestration(
        existing=[FakeJob("legacy_hr", JobStatus.PENDING)]
    )
    scheduler = build(orchestration=orchestration)

    assert scheduler.tick().skipped_running == ("legacy_hr",)


def test_a_finished_sync_does_not_block():
    orchestration = FakeOrchestration(
        existing=[FakeJob("legacy_hr", JobStatus.SUCCEEDED)]
    )
    scheduler = build(orchestration=orchestration)

    assert scheduler.tick().submitted == ("legacy_hr",)


def test_an_unreadable_job_store_blocks_rather_than_risks_a_duplicate():
    """Not knowing whether a sync is running is a reason to wait, not to start."""
    class Broken(FakeOrchestration):
        def list(self, **kwargs):
            raise RuntimeError("job store unavailable")

    orchestration = Broken()
    scheduler = build(orchestration=orchestration)
    result = scheduler.tick()

    assert result.submitted == ()
    assert orchestration.submitted == []


def test_a_slow_sync_is_not_duplicated_across_many_ticks():
    """Interval 30s, sync takes 50s: the intervening tick must not double up."""
    clock = Clock()
    orchestration = FakeOrchestration()
    scheduler = build(orchestration=orchestration, interval=30.0, clock=clock)

    scheduler.tick()
    orchestration.existing.append(FakeJob("legacy_hr", JobStatus.RUNNING))

    for _ in range(3):
        clock.advance(30)
        scheduler.tick()

    assert len(orchestration.sync_requests) == 1


# ======================================================================
# Interval behaviour
# ======================================================================


def test_a_source_is_not_resynced_before_its_interval_elapses():
    clock = Clock()
    orchestration = FakeOrchestration()
    scheduler = build(orchestration=orchestration, interval=60.0, clock=clock)

    scheduler.tick()
    clock.advance(30)
    result = scheduler.tick()

    assert result.submitted == ()
    assert result.skipped_interval == ("legacy_hr",)
    assert len(orchestration.sync_requests) == 1


def test_a_source_is_resynced_once_its_interval_elapses():
    clock = Clock()
    orchestration = FakeOrchestration()
    scheduler = build(orchestration=orchestration, interval=60.0, clock=clock)

    scheduler.tick()
    clock.advance(61)
    result = scheduler.tick()

    assert result.submitted == ("legacy_hr",)
    assert len(orchestration.sync_requests) == 2


def test_several_sources_are_scheduled_independently():
    clock = Clock()
    orchestration = FakeOrchestration()
    scheduler = build(
        sources=("legacy_hr", "finance_erp"), orchestration=orchestration,
        clock=clock,
    )
    result = scheduler.tick()

    assert set(result.submitted) == {"legacy_hr", "finance_erp"}


# ======================================================================
# DR8 - drift runs on its own, longer interval
# ======================================================================


def test_drift_is_not_run_on_every_data_sync():
    """Schema discovery is expensive; it must not run every few seconds."""
    clock = Clock()
    orchestration = FakeOrchestration()
    scheduler = build(
        orchestration=orchestration, interval=60.0, drift=3600.0, clock=clock
    )

    first = scheduler.tick()

    assert first.drift_submitted == ("legacy_hr",)

    for _ in range(10):
        clock.advance(61)
        result = scheduler.tick()

        assert result.drift_submitted == ()

    drift_jobs = [
        r for r in orchestration.submitted if r.job_type is JobType.DRIFT_CHECK
    ]

    assert len(drift_jobs) == 1


def test_drift_runs_again_after_its_own_interval():
    clock = Clock()
    orchestration = FakeOrchestration()
    scheduler = build(
        orchestration=orchestration, interval=60.0, drift=600.0, clock=clock
    )

    scheduler.tick()
    clock.advance(700)
    result = scheduler.tick()

    assert result.drift_submitted == ("legacy_hr",)


def test_no_drift_interval_means_no_drift_jobs():
    orchestration = FakeOrchestration()
    scheduler = build(orchestration=orchestration, drift=None)
    scheduler.tick()

    assert all(
        r.job_type is not JobType.DRIFT_CHECK for r in orchestration.submitted
    )


# ======================================================================
# TEST E - leadership
# ======================================================================


class CountingLease:
    """Grants to whoever asks first, then only to that holder."""

    def __init__(self):
        self.holder_id = None

    def for_instance(self, name):
        lease = CountingLease()
        lease.__dict__ = self.__dict__

        class Bound:
            def __init__(self, shared, instance):
                self.shared = shared
                self.instance = instance

            def acquire(self, now, lease_seconds):
                if self.shared.holder_id in (None, self.instance):
                    self.shared.holder_id = self.instance
                    return True

                return False

            def release(self):
                if self.shared.holder_id == self.instance:
                    self.shared.holder_id = None

        return Bound(self, name)


def test_only_one_instance_leads_a_tick():
    """Two schedulers, one lease. Exactly one submits."""
    shared = CountingLease()
    clock = Clock()
    orchestration = FakeOrchestration()

    first = build(
        orchestration=orchestration, clock=clock,
        lease=shared.for_instance("instance-a"),
    )
    second = build(
        orchestration=orchestration, clock=clock,
        lease=shared.for_instance("instance-b"),
    )

    result_a = first.tick()
    result_b = second.tick()

    assert result_a.held_lease != result_b.held_lease
    assert len(orchestration.sync_requests) == 1


def test_the_follower_reports_why_it_did_nothing():
    shared = CountingLease()
    orchestration = FakeOrchestration()
    leader = build(orchestration=orchestration, lease=shared.for_instance("a"))
    follower = build(orchestration=orchestration, lease=shared.for_instance("b"))

    leader.tick()
    result = follower.tick()

    assert result.held_lease is False
    assert "lease" in result.reason


def test_the_single_process_lease_states_its_assumption():
    """A deployment running one scheduler is correct - and says so."""
    lease = SingleProcessLease("local")

    assert lease.acquire(START, 120.0) is True
    assert lease.acquire(START, 120.0) is True


# ======================================================================
# DR33 - failures back off rather than hammer
# ======================================================================


def test_a_failing_source_is_reported_and_not_retried_immediately():
    clock = Clock()
    orchestration = FakeOrchestration(fail_on={"legacy_hr"})
    scheduler = build(orchestration=orchestration, interval=60.0, clock=clock)

    first = scheduler.tick()

    assert first.failed == ("legacy_hr",)

    clock.advance(61)
    second = scheduler.tick()

    assert second.skipped_backoff == ("legacy_hr",)


def test_backoff_grows_with_consecutive_failures():
    schedule = SourceSchedule()

    schedule.consecutive_failures = 1
    first = schedule.backoff_seconds(60.0)
    schedule.consecutive_failures = 3
    third = schedule.backoff_seconds(60.0)

    assert third > first


def test_backoff_is_bounded():
    schedule = SourceSchedule()
    schedule.consecutive_failures = 500

    assert schedule.backoff_seconds(60.0) <= 60.0 * 32


def test_a_successful_submission_clears_the_backoff():
    clock = Clock()
    orchestration = FakeOrchestration(fail_on={"legacy_hr"})
    scheduler = build(orchestration=orchestration, interval=10.0, clock=clock)

    scheduler.tick()
    orchestration.fail_on = set()

    clock.advance(10_000)
    scheduler.tick()

    assert scheduler.schedule_for("legacy_hr").consecutive_failures == 0


# ======================================================================
# Nothing starts on import
# ======================================================================


def test_importing_the_module_starts_no_thread_or_loop():
    import ast
    import inspect

    from erp_pipeline.orchestration import scheduler as module

    tree = ast.parse(inspect.getsource(module))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    for forbidden in ("threading", "asyncio", "sched", "celery", "kafka", "pika"):
        assert forbidden not in imported, forbidden


def test_constructing_services_does_not_start_a_scheduler():
    """A test must be able to build the app without polling anything."""
    from erp_pipeline.api.main import build_services

    services = build_services(with_embedding=False, with_storage=False)

    assert getattr(services, "scheduler", None) is None


# ======================================================================
# Operational status
# ======================================================================


def test_status_reports_enough_to_operate():
    clock = Clock()
    scheduler = build(clock=clock)
    scheduler.tick()
    status = scheduler.status()

    assert status["enabled"] is True
    assert status["configured_sources"] == ["legacy_hr"]
    assert status["last_tick_at"]
    assert status["holds_lease"] is True
    assert status["sources"]["legacy_hr"]["last_outcome"] == "submitted"


def test_status_on_a_disabled_scheduler_says_so():
    scheduler = build(enabled=False)
    scheduler.tick()

    assert scheduler.status()["enabled"] is False
