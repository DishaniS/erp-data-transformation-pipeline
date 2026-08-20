"""Watermarks, tie-breaking, sync state, checkpoints and concurrency.

Steps 3-7, 32-36, 67-69. The most important test in this file is
``test_no_record_is_lost_at_an_equal_watermark``: a timestamp-only watermark
loses rows silently, and silence is what makes it dangerous.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from erp_pipeline.sync import (
    EMPTY_WATERMARK,
    CheckpointConflictError,
    ExtractionConfig,
    InMemoryChangeSource,
    InMemorySyncStateStore,
    SyncConfigurationError,
    SyncOptions,
    SyncState,
    SyncStatus,
    Watermark,
    WatermarkStrategy,
    build_extraction_sql,
    build_watermark_predicate,
    ensure_state,
    validate_identifier,
)

from tests.erp_pipeline.sync.conftest import (
    BASE_TIME,
    Harness,
    extraction_config,
    invoice_row,
)


# ============================================================
# Watermark semantics
# ============================================================

def test_an_empty_watermark_precedes_everything():
    real = Watermark(timestamp=BASE_TIME, tie_breaker="INV-001")

    assert EMPTY_WATERMARK.is_empty
    assert real.is_after(EMPTY_WATERMARK)
    assert not EMPTY_WATERMARK.is_after(real)


def test_watermarks_order_by_timestamp_then_tie_breaker():
    earlier = Watermark(timestamp=BASE_TIME, tie_breaker="INV-001")
    same_time = Watermark(timestamp=BASE_TIME, tie_breaker="INV-002")
    later = Watermark(timestamp=BASE_TIME + timedelta(seconds=1), tie_breaker="A")

    assert same_time.is_after(earlier)
    assert later.is_after(same_time)


def test_a_naive_watermark_timestamp_is_refused():
    """It cannot be ordered against aware source timestamps."""
    with pytest.raises(ValueError):
        Watermark(timestamp=datetime(2026, 8, 14, 10, 0, 0))


def test_a_watermark_round_trips_through_its_dictionary_form():
    original = Watermark(timestamp=BASE_TIME, tie_breaker="INV-007")

    restored = Watermark.from_dict(original.to_dict())

    assert restored.timestamp == original.timestamp
    assert restored.tie_breaker == original.tie_breaker


def test_a_watermark_describes_itself_without_business_values():
    described = Watermark(timestamp=BASE_TIME, tie_breaker="INV-007").describe()

    assert "INV-007" in described
    assert "2026" in described


def test_mixed_tie_breaker_types_do_not_crash_ordering():
    """One entity keys on integers, another on strings."""
    numeric = Watermark(timestamp=BASE_TIME, tie_breaker=42)
    textual = Watermark(timestamp=BASE_TIME, tie_breaker="INV-001")

    assert isinstance(numeric.is_after(textual), bool)


# ============================================================
# Predicate construction (Steps 4, 5)
# ============================================================

def test_a_fresh_sync_has_no_predicate():
    config = extraction_config()

    predicate, params = build_watermark_predicate(config, EMPTY_WATERMARK)

    assert predicate == ""
    assert params == {}


def test_the_timestamp_predicate_is_a_strict_comparison():
    config = extraction_config(WatermarkStrategy.TIMESTAMP)

    predicate, params = build_watermark_predicate(
        config, Watermark(timestamp=BASE_TIME)
    )

    assert predicate == "updated_at > :wm_ts"
    assert params == {"wm_ts": BASE_TIME}


def test_the_monotonic_id_predicate_uses_the_tie_break_column():
    config = extraction_config(WatermarkStrategy.MONOTONIC_ID)

    predicate, params = build_watermark_predicate(
        config, Watermark(tie_breaker=100)
    )

    assert predicate == "id > :wm_tie"
    assert params == {"wm_tie": 100}


def test_the_composite_predicate_breaks_ties_at_an_equal_timestamp():
    """This clause is the whole answer to Step 5."""
    config = extraction_config(WatermarkStrategy.COMPOSITE)

    predicate, params = build_watermark_predicate(
        config, Watermark(timestamp=BASE_TIME, tie_breaker="INV-101")
    )

    assert predicate == (
        "(updated_at > :wm_ts OR "
        "(updated_at = :wm_ts AND id > :wm_tie))"
    )
    assert params == {"wm_ts": BASE_TIME, "wm_tie": "INV-101"}


def test_the_extraction_sql_is_ordered_and_bounded():
    """Without ORDER BY, LIMIT selects an arbitrary subset."""
    sql, params = build_extraction_sql(
        extraction_config(), Watermark(timestamp=BASE_TIME, tie_breaker="X"), 250
    )

    assert "ORDER BY updated_at, id" in sql
    assert "LIMIT :batch_size" in sql
    assert params["batch_size"] == 250


def test_no_value_is_ever_formatted_into_sql():
    sql, params = build_extraction_sql(
        extraction_config(), Watermark(timestamp=BASE_TIME, tie_breaker="X'; DROP"), 10
    )

    assert "DROP" not in sql
    assert params["wm_tie"] == "X'; DROP"


def test_a_hostile_identifier_is_refused_not_escaped():
    with pytest.raises(SyncConfigurationError):
        validate_identifier("invoices; DROP TABLE users", "source_entity")


def test_a_composite_strategy_without_a_tie_breaker_is_refused():
    with pytest.raises(SyncConfigurationError):
        ExtractionConfig(
            source_entity="t",
            strategy=WatermarkStrategy.COMPOSITE,
            key_field="id",
            watermark_field="updated_at",
        )


def test_a_timestamp_strategy_without_a_watermark_field_is_refused():
    with pytest.raises(SyncConfigurationError):
        ExtractionConfig(
            source_entity="t",
            strategy=WatermarkStrategy.TIMESTAMP,
            key_field="id",
        )


# ============================================================
# THE tie-break proof (Step 5)
# ============================================================

def test_no_record_is_lost_at_an_equal_watermark():
    """Three rows share a timestamp; a batch ends in the middle of them.

    A timestamp-only watermark would ask for ``> 10:00:00`` next time and
    never see id=102 again.
    """
    shared = BASE_TIME
    rows = [
        {"id": 100, "customer_id": "C1", "amount": "1.00", "updated_at": shared},
        {"id": 101, "customer_id": "C1", "amount": "1.00", "updated_at": shared},
        {"id": 102, "customer_id": "C1", "amount": "1.00", "updated_at": shared},
    ]

    harness = Harness(rows=rows)

    first = harness.run(SyncOptions(batch_size=2))
    assert first.changes_read == 2

    second = harness.run(SyncOptions(batch_size=2))

    assert second.changes_read == 1
    assert second.results[0].change.record_key == "102"


def test_every_equal_timestamp_record_arrives_exactly_once():
    shared = BASE_TIME
    rows = [
        {"id": i, "customer_id": "C1", "amount": "1.00", "updated_at": shared}
        for i in range(100, 110)
    ]
    harness = Harness(rows=rows)

    seen: list[str] = []
    for _ in range(10):
        summary = harness.run(SyncOptions(batch_size=3))
        seen.extend(r.change.record_key for r in summary.results)
        if summary.changes_read == 0:
            break

    assert sorted(seen) == sorted(str(i) for i in range(100, 110))
    assert len(seen) == len(set(seen))


# ============================================================
# Sync state (Steps 3, 32, 34)
# ============================================================

def test_a_fresh_entity_starts_at_the_beginning():
    store = InMemorySyncStateStore()

    state = ensure_state(
        store, "erp_pg", "invoices", WatermarkStrategy.COMPOSITE
    )

    assert state.is_fresh
    assert state.status is SyncStatus.NEW
    assert state.version == 0


def test_watermarks_are_tracked_per_entity():
    """Step 34: not one global timestamp for a whole ERP."""
    store = InMemorySyncStateStore()

    customers = ensure_state(
        store, "erp_pg", "customers", WatermarkStrategy.COMPOSITE
    ).advanced_to(Watermark(timestamp=BASE_TIME, tie_breaker="C9"))
    invoices = ensure_state(
        store, "erp_pg", "invoices", WatermarkStrategy.COMPOSITE
    ).advanced_to(
        Watermark(timestamp=BASE_TIME + timedelta(hours=5), tie_breaker="I9")
    )

    store.save(customers)
    store.save(invoices)

    assert store.load("erp_pg", "customers").watermark.tie_breaker == "C9"
    assert store.load("erp_pg", "invoices").watermark.tie_breaker == "I9"


def test_state_records_the_schema_and_mapping_it_ran_against(harness):
    """Step 53: reproducibility."""
    state = harness.state

    assert state.schema_id == "erp_pg.phase10.v1"
    assert state.schema_hash == "0" * 64
    assert state.mapping_id == "p10.inv"
    assert state.transformation_engine_version


def test_state_serializes_without_business_values(harness):
    payload = harness.state.to_dict()

    assert "watermark" in payload
    assert "amount" not in str(payload)


def test_a_watermark_never_moves_backwards():
    state = SyncState(
        source_system_id="s",
        source_entity="e",
        strategy=WatermarkStrategy.COMPOSITE,
        watermark=Watermark(timestamp=BASE_TIME + timedelta(hours=1)),
    )

    regressed = state.advanced_to(Watermark(timestamp=BASE_TIME))

    assert regressed.watermark.timestamp == BASE_TIME + timedelta(hours=1)


# ============================================================
# Optimistic concurrency (Steps 67, 68)
# ============================================================

def test_a_concurrent_checkpoint_advance_is_refused():
    store = InMemorySyncStateStore()
    state = ensure_state(store, "s", "e", WatermarkStrategy.COMPOSITE)
    store.save(state.advanced_to(Watermark(timestamp=BASE_TIME)), expected_version=0)

    # A second runner still holding the version-0 view.
    with pytest.raises(CheckpointConflictError):
        store.save(
            state.advanced_to(Watermark(timestamp=BASE_TIME + timedelta(hours=1))),
            expected_version=0,
        )


def test_a_conflict_reports_both_versions():
    store = InMemorySyncStateStore()
    state = ensure_state(store, "s", "e", WatermarkStrategy.COMPOSITE)
    store.save(state.advanced_to(Watermark(timestamp=BASE_TIME)), expected_version=0)

    with pytest.raises(CheckpointConflictError) as excinfo:
        store.save(state.advanced_to(Watermark(timestamp=BASE_TIME)), expected_version=0)

    assert excinfo.value.expected_version == 0
    assert excinfo.value.actual_version == 1


def test_the_version_increments_on_every_advance():
    store = InMemorySyncStateStore()
    state = ensure_state(store, "s", "e", WatermarkStrategy.COMPOSITE)

    first = state.advanced_to(Watermark(timestamp=BASE_TIME))
    second = first.advanced_to(Watermark(timestamp=BASE_TIME + timedelta(hours=1)))

    assert (state.version, first.version, second.version) == (0, 1, 2)


# ============================================================
# Bounded batches and catch-up (Steps 35, 36)
# ============================================================

def test_a_batch_is_bounded():
    harness = Harness(rows=[invoice_row(i) for i in range(1, 51)])

    summary = harness.run(SyncOptions(batch_size=10))

    assert summary.changes_read == 10


def test_repeated_batches_advance_until_caught_up():
    harness = Harness(rows=[invoice_row(i) for i in range(1, 26)])

    summaries = harness.catch_up(SyncOptions(batch_size=10))

    assert sum(s.changes_read for s in summaries) == 25
    assert summaries[-1].changes_read == 0


def test_a_caught_up_source_reads_nothing(harness):
    summary = harness.run()

    assert summary.changes_read == 0
    assert summary.changes_processed == 0


def test_an_interrupted_run_resumes_from_the_durable_checkpoint():
    """Step 36: no full reload after an interruption."""
    harness = Harness(rows=[invoice_row(i) for i in range(1, 21)])

    harness.run(SyncOptions(batch_size=5))
    watermark_after_first = harness.state.watermark

    # A brand-new service instance, as if the process had restarted.
    resumed = harness.service.run_incremental(
        harness.target,
        harness.source,
        SyncOptions(batch_size=5),
        strategy=harness.strategy,
        watermark_field="updated_at",
        tie_break_field="id",
    )

    assert resumed.watermark_before.tie_breaker == watermark_after_first.tie_breaker
    assert resumed.changes_read == 5


def test_a_batch_size_below_one_is_refused():
    with pytest.raises(SyncConfigurationError):
        SyncOptions(batch_size=0)


# ============================================================
# Idempotent replay (Steps 7, 69)
# ============================================================

def test_replaying_the_same_change_creates_no_duplicate(harness):
    """At-least-once delivery plus idempotent upsert."""
    before = len(harness.canonical)

    harness.source.add(invoice_row(101, offset_seconds=500))
    harness.run()
    after_first = len(harness.canonical)

    # Force the checkpoint back and replay the same change.
    state = harness.state
    rewound = SyncState(
        source_system_id=state.source_system_id,
        source_entity=state.source_entity,
        strategy=state.strategy,
        watermark=Watermark(
            timestamp=BASE_TIME + timedelta(seconds=100), tie_breaker="INV-100"
        ),
        version=state.version,
    )
    harness.state_store.save(rewound, expected_version=state.version)

    harness.run()

    assert after_first == before + 1
    assert len(harness.canonical) == after_first


def test_an_idempotency_key_is_deterministic_and_not_random():
    from erp_pipeline.sync import ChangeOperation, SourceChange

    def build() -> SourceChange:
        return SourceChange(
            source_system_id="s",
            source_entity="e",
            record_key="INV-1",
            operation=ChangeOperation.UPDATE,
            watermark=Watermark(timestamp=BASE_TIME, tie_breaker="INV-1"),
        )

    assert build().idempotency_key == build().idempotency_key


def test_an_idempotency_key_changes_with_the_position():
    from erp_pipeline.sync import ChangeOperation, SourceChange

    first = SourceChange(
        source_system_id="s",
        source_entity="e",
        record_key="INV-1",
        operation=ChangeOperation.UPDATE,
        watermark=Watermark(timestamp=BASE_TIME, tie_breaker="INV-1"),
    )
    second = SourceChange(
        source_system_id="s",
        source_entity="e",
        record_key="INV-1",
        operation=ChangeOperation.UPDATE,
        watermark=Watermark(
            timestamp=BASE_TIME + timedelta(hours=1), tie_breaker="INV-1"
        ),
    )

    assert first.idempotency_key != second.idempotency_key
