"""Case assembly, process-model derivation, and the projections.

The behavioural contract migrated from ``bpi2020.transformation.
build_ai_ready_cases``: ordered events, activity sequences, unique activities,
durations, and a content hash that is stable across a rebuild but moves when
the case's content genuinely changes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from erp_pipeline.process import (
    CaseBuildError,
    CaseSummaryConfig,
    EventLogConfig,
    ProcessCaseService,
    ProcessEvent,
    apply_process_model,
    build_case,
    build_case_summary,
    build_cases,
    build_process_model,
    group_events,
    sort_events,
    unique_activities,
)
from erp_pipeline.schemas.enums import RecordType


SYSTEM = "erp_demo"


def event(case_id, activity, day=None, ordinal=0, process="declarations", **attrs):
    return ProcessEvent(
        case_id=case_id,
        activity=activity,
        process_type=process,
        timestamp=(
            datetime(2026, 1, day, tzinfo=timezone.utc) if day else None
        ),
        ordinal=ordinal,
        attributes=attrs,
    )


@pytest.fixture
def config():
    return EventLogConfig(
        case_id_field="case_id",
        activity_field="activity",
        timestamp_field="ts",
        process_type_field="process",
        entity_reference_fields={"employee": "employee_id"},
    )


# ============================================================
# Ordering
# ============================================================


def test_events_are_ordered_chronologically():
    events = [
        event("c", "third", day=3, ordinal=0),
        event("c", "first", day=1, ordinal=1),
        event("c", "second", day=2, ordinal=2),
    ]

    assert [item.activity for item in sort_events(events)] == [
        "first",
        "second",
        "third",
    ]


def test_events_sharing_a_timestamp_keep_arrival_order():
    """Sorting on timestamp alone would order these non-deterministically, and
    a non-deterministic sequence produces a non-deterministic content hash."""
    events = [
        event("c", "b", day=1, ordinal=1),
        event("c", "a", day=1, ordinal=0),
    ]

    assert [item.activity for item in sort_events(events)] == ["a", "b"]


def test_unstamped_events_sort_last_but_keep_their_own_order():
    events = [
        event("c", "no-ts-2", day=None, ordinal=5),
        event("c", "stamped", day=1, ordinal=9),
        event("c", "no-ts-1", day=None, ordinal=2),
    ]

    assert [item.activity for item in sort_events(events)] == [
        "stamped",
        "no-ts-1",
        "no-ts-2",
    ]


# ============================================================
# Case assembly
# ============================================================


def test_build_case_produces_the_full_process_view(config):
    case = build_case(
        [
            event("Declaration 1", "SUBMITTED", day=1, ordinal=0),
            event("Declaration 1", "APPROVED", day=3, ordinal=1),
            event("Declaration 1", "Payment Handled", day=13, ordinal=2),
        ],
        SYSTEM,
        config,
    )

    assert case.case_id == "Declaration 1"
    assert case.process_type == "declarations"
    assert case.total_events == 3
    assert case.activity_sequence == ("SUBMITTED", "APPROVED", "Payment Handled")
    assert case.unique_activities == ("SUBMITTED", "APPROVED", "Payment Handled")
    assert case.start_activity == "SUBMITTED"
    assert case.end_activity == "Payment Handled"
    assert case.current_state == "Payment Handled"
    assert case.duration_days == pytest.approx(12.0)
    assert case.is_complete is True
    assert case.content_hash


def test_unique_activities_preserve_first_occurrence_order():
    assert unique_activities(["b", "a", "b", "c", "a"]) == ("b", "a", "c")


def test_events_without_an_activity_do_not_enter_the_sequence(config):
    case = build_case(
        [
            event("c", "start", day=1, ordinal=0),
            event("c", None, day=2, ordinal=1),
            event("c", "end", day=3, ordinal=2),
        ],
        SYSTEM,
        config,
    )

    assert case.activity_sequence == ("start", "end")
    # ...but they still count as events, which is the point.
    assert case.total_events == 3


def test_a_case_with_no_timestamps_is_still_a_valid_case(config):
    case = build_case(
        [
            event("c", "a", day=None, ordinal=0),
            event("c", "b", day=None, ordinal=1),
        ],
        SYSTEM,
        config,
    )

    assert case.activity_sequence == ("a", "b")
    assert case.duration_days is None
    assert case.is_complete is False


def test_building_a_case_from_zero_events_is_refused(config):
    with pytest.raises(CaseBuildError):
        build_case([], SYSTEM, config)


def test_mixing_two_cases_is_refused(config):
    """Interleaving two process instances is silently wrong, so it is loud."""
    with pytest.raises(CaseBuildError) as error:
        build_case(
            [event("c1", "a", day=1), event("c2", "b", day=2)], SYSTEM, config
        )

    assert "c2" in str(error.value)


def test_entity_references_take_the_first_non_empty_value(config):
    case = build_case(
        [
            event("c", "a", day=1, ordinal=0, employee_id="E1"),
            event("c", "b", day=2, ordinal=1, employee_id=""),
        ],
        SYSTEM,
        config,
    )

    assert case.entity_references == {"employee": "E1"}


# ============================================================
# Grouping
# ============================================================


def test_events_group_by_process_type_and_case_id():
    events = [
        event("1", "a", day=1, process="declarations"),
        event("1", "b", day=1, process="permits"),
    ]

    grouped = group_events(events)

    assert len(grouped) == 2, "same case number in two processes must not merge"


def test_build_cases_returns_a_deterministic_order(config):
    events = [
        event("z", "a", day=1),
        event("a", "a", day=1),
        event("m", "a", day=1),
    ]

    cases = build_cases(events, SYSTEM, config)

    assert [case.case_id for case in cases] == ["a", "m", "z"]


# ============================================================
# Content hashing
# ============================================================


def test_the_same_events_always_hash_the_same(config):
    events = [event("c", "a", day=1, ordinal=0), event("c", "b", day=2, ordinal=1)]

    first = build_case(events, SYSTEM, config)
    second = build_case(list(reversed(events)), SYSTEM, config)

    assert first.content_hash == second.content_hash


def test_a_changed_activity_sequence_changes_the_hash(config):
    original = build_case([event("c", "a", day=1)], SYSTEM, config)
    changed = build_case([event("c", "b", day=1)], SYSTEM, config)

    assert original.content_hash != changed.content_hash


def test_the_hash_ignores_event_ordinals(config):
    """Ordinals move whenever the source is reloaded. If they entered the hash,
    every rebuild would look like a content change and re-embed the whole log.
    """
    left = build_case([event("c", "a", day=1, ordinal=0)], SYSTEM, config)
    right = build_case([event("c", "a", day=1, ordinal=999)], SYSTEM, config)

    assert left.content_hash == right.content_hash


def test_the_hash_ignores_allowed_next_states(config):
    """A new case elsewhere in the log must not invalidate this case's hash."""
    case = build_case([event("c", "a", day=1)], SYSTEM, config)
    enriched = case.with_allowed_next_states(("b", "c"))

    assert enriched.compute_content_hash() == case.compute_content_hash()


# ============================================================
# Process model
# ============================================================


@pytest.fixture
def three_cases(config):
    return build_cases(
        [
            event("1", "SUBMITTED", day=1, ordinal=0),
            event("1", "APPROVED", day=2, ordinal=1),
            event("1", "PAID", day=3, ordinal=2),
            event("2", "SUBMITTED", day=1, ordinal=3),
            event("2", "APPROVED", day=2, ordinal=4),
            event("2", "PAID", day=3, ordinal=5),
            event("3", "SUBMITTED", day=1, ordinal=6),
            event("3", "REJECTED", day=2, ordinal=7),
        ],
        SYSTEM,
        config,
    )


def test_process_model_reports_starts_and_ends(three_cases):
    model = build_process_model(three_cases)

    assert model.start_activities == ("SUBMITTED",)
    assert set(model.end_activities) == {"PAID", "REJECTED"}
    assert model.case_count == 3


def test_allowed_next_states_are_ranked_by_observed_frequency(three_cases):
    model = build_process_model(three_cases)

    # APPROVED was observed twice after SUBMITTED, REJECTED once.
    assert model.allowed_next_states("SUBMITTED") == ("APPROVED", "REJECTED")


def test_a_terminal_activity_permits_nothing(three_cases):
    model = build_process_model(three_cases)

    assert model.allowed_next_states("PAID") == ()


def test_allowed_next_states_for_no_activity_are_the_start_activities(three_cases):
    model = build_process_model(three_cases)

    assert model.allowed_next_states(None) == model.start_activities


def test_applying_a_model_fills_in_each_case_current_position(three_cases):
    model = build_process_model(three_cases)
    enriched = apply_process_model(three_cases, model)

    by_id = {case.case_id: case for case in enriched}

    assert by_id["3"].current_state == "REJECTED"
    assert by_id["3"].allowed_next_states == ()
    assert by_id["1"].current_state == "PAID"


def test_one_model_cannot_span_two_process_types(config):
    cases = build_cases(
        [
            event("1", "a", day=1, process="declarations"),
            event("2", "a", day=1, process="permits"),
        ],
        SYSTEM,
        config,
    )

    with pytest.raises(CaseBuildError):
        build_process_model(cases)


# ============================================================
# Summary text
# ============================================================


def test_summary_names_the_process_and_the_activities(config):
    case = build_case(
        [event("Declaration 1", "SUBMITTED", day=1), event("Declaration 1", "PAID", day=3)],
        SYSTEM,
        config,
    )

    summary = build_case_summary(case)

    assert "Declaration 1" in summary
    assert "declarations" in summary
    assert "SUBMITTED" in summary
    assert "PAID" in summary


def test_summary_is_deterministic(config):
    case = build_case([event("c", "a", day=1)], SYSTEM, config)

    assert build_case_summary(case) == build_case_summary(case)


def test_summary_is_bounded_and_says_so(config):
    case = build_case(
        [event("c", f"activity {index}", day=1, ordinal=index) for index in range(60)],
        SYSTEM,
        config,
    )

    summary = build_case_summary(case, CaseSummaryConfig(max_characters=120))

    assert len(summary) <= 120
    assert "truncated" in summary


def test_summary_caps_how_many_activities_it_lists(config):
    case = build_case(
        [event("c", f"a{index}", day=1, ordinal=index) for index in range(20)],
        SYSTEM,
        config,
    )

    summary = build_case_summary(case, CaseSummaryConfig(max_activities_listed=3))

    assert "further" in summary


# ============================================================
# Service and projections
# ============================================================


@pytest.fixture
def rows():
    return [
        {
            "case_id": "Declaration 100000",
            "activity": "Declaration SUBMITTED",
            "ts": "2026-01-01 09:00:00",
            "process": "domestic_declarations",
            "employee_id": "E1",
        },
        {
            "case_id": "Declaration 100000",
            "activity": "Declaration APPROVED",
            "ts": "2026-01-03 09:00:00",
            "process": "domestic_declarations",
            "employee_id": "E1",
        },
        {
            "case_id": "Declaration 100001",
            "activity": "Declaration SUBMITTED",
            "ts": "2026-01-02 09:00:00",
            "process": "domestic_declarations",
            "employee_id": "E2",
        },
    ]


def test_service_builds_cases_from_raw_rows(config, rows):
    service = ProcessCaseService(SYSTEM, config)
    cases = service.build_cases(rows)

    assert len(cases) == 2
    assert cases[0].case_id == "Declaration 100000"


def test_service_fills_allowed_next_states_by_default(config, rows):
    service = ProcessCaseService(SYSTEM, config)
    cases = service.build_cases(rows)

    submitted = next(
        case for case in cases if case.current_state == "Declaration SUBMITTED"
    )

    assert "Declaration APPROVED" in submitted.allowed_next_states


def test_service_can_skip_the_process_model_pass(config, rows):
    service = ProcessCaseService(SYSTEM, config)
    cases = service.build_cases(rows, derive_process_model=False)

    assert all(case.allowed_next_states == () for case in cases)


def test_canonical_projection_reuses_the_case_identity(config, rows):
    service = ProcessCaseService(SYSTEM, config)
    case = service.build_cases(rows)[0]

    record = service.to_canonical_record(case)

    assert record.record_id == case.case_record_id
    assert record.record_type is RecordType.CASE
    assert record.entity_type == case.process_type
    assert record.content_hash


def test_representation_carries_the_process_facts_a_consumer_filters_on(
    config, rows
):
    service = ProcessCaseService(SYSTEM, config)
    case = service.build_cases(rows)[0]

    representation = service.to_representation(case)

    assert representation.metadata["canonical_record_id"] == case.case_record_id
    assert representation.metadata["process_type"] == case.process_type
    assert representation.metadata["current_state"] == case.current_state
    assert representation.metadata["record_type"] == RecordType.CASE.value
    assert representation.source_record_ids == (case.case_record_id,)


def test_representation_text_is_the_case_summary(config, rows):
    service = ProcessCaseService(SYSTEM, config)
    case = service.build_cases(rows)[0]

    assert service.to_representation(case).text_for_ai == service.summarize(case)


def test_representation_vector_id_is_stable_across_rebuilds(config, rows):
    service = ProcessCaseService(SYSTEM, config)

    first = service.to_representation(service.build_cases(rows)[0])
    second = service.to_representation(service.build_cases(rows)[0])

    assert first.vector_id == second.vector_id


def test_the_process_package_never_imports_a_dataset_module():
    """The boundary that made the old prototype impossible to reuse."""
    import pathlib

    package = pathlib.Path(
        __file__
    ).resolve().parents[3] / "src" / "erp_pipeline" / "process"

    for module in package.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert "bpi2020" not in source, module.name
        assert "erp_integrations" not in source, module.name
