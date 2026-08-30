"""Event normalization and the process contracts.

Migrated from the BPI prototype's case-building tests. The behaviour asserted
is the same; the code under test is now generic and driven by an
``EventLogConfig`` rather than by hard-coded column names.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from erp_pipeline.process import (
    DEFAULT_PROCESS_TYPE,
    EventLogConfig,
    EventNormalizationError,
    ProcessConfigurationError,
    coerce_timestamp,
    extract_attributes,
    make_case_record_id,
    normalize_event,
    normalize_events,
    resolve_process_type,
)
from erp_pipeline.schemas.identity import parse_canonical_id


# ============================================================
# Configuration
# ============================================================


def test_config_requires_a_case_id_column():
    with pytest.raises(ProcessConfigurationError) as error:
        EventLogConfig(case_id_field="", activity_field="act", process_type="p")

    assert "case_id_field" in str(error.value)


def test_config_requires_an_activity_column():
    with pytest.raises(ProcessConfigurationError):
        EventLogConfig(case_id_field="case", activity_field="", process_type="p")


def test_config_requires_a_resolvable_process_type():
    """Process type is part of a case's natural key, so it cannot be absent."""
    with pytest.raises(ProcessConfigurationError) as error:
        EventLogConfig(case_id_field="case", activity_field="act")

    assert "process_type" in str(error.value)


def test_config_accepts_either_a_column_or_a_constant():
    EventLogConfig(case_id_field="c", activity_field="a", process_type="fixed")
    EventLogConfig(case_id_field="c", activity_field="a", process_type_field="p")


def test_reserved_fields_cover_every_structural_column():
    config = EventLogConfig(
        case_id_field="case",
        activity_field="act",
        timestamp_field="ts",
        process_type_field="ptype",
        event_key_field="key",
    )

    assert config.reserved_fields == {"case", "act", "ts", "ptype", "key"}


def test_fingerprint_changes_when_configuration_changes():
    base = EventLogConfig(case_id_field="c", activity_field="a", process_type="p")
    other = EventLogConfig(case_id_field="c", activity_field="b", process_type="p")

    assert base.fingerprint() != other.fingerprint()


def test_fingerprint_is_stable_for_identical_configuration():
    first = EventLogConfig(case_id_field="c", activity_field="a", process_type="p")
    second = EventLogConfig(case_id_field="c", activity_field="a", process_type="p")

    assert first.fingerprint() == second.fingerprint()


# ============================================================
# Timestamp coercion
# ============================================================


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-15 09:30:00",
        "2026-01-15T09:30:00",
        "2026-01-15T09:30:00Z",
        "15/01/2026 09:30:00",
        "15-01-2026 09:30:00",
    ],
)
def test_common_timestamp_formats_are_parsed(value):
    parsed = coerce_timestamp(value)

    assert parsed is not None
    assert parsed.tzinfo is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 1, 15)


def test_naive_datetimes_are_treated_as_utc():
    parsed = coerce_timestamp(datetime(2026, 1, 15, 9, 30))

    assert parsed.tzinfo is timezone.utc


def test_aware_datetimes_keep_their_zone():
    original = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)

    assert coerce_timestamp(original) == original


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-date", "nan"])
def test_unparseable_timestamps_become_none_rather_than_raising(value):
    """An unusable timestamp is a fact about one event, not a fatal error.

    Dropping the event instead would change the case's activity sequence,
    which is exactly the quiet corruption this layer must not introduce.
    """
    assert coerce_timestamp(value) is None


# ============================================================
# Event normalization
# ============================================================


@pytest.fixture
def config():
    return EventLogConfig(
        case_id_field="case_id",
        activity_field="activity",
        timestamp_field="ts",
        process_type_field="process",
        event_key_field="row_key",
        excluded_fields=frozenset({"loaded_at"}),
    )


def test_normalize_event_extracts_every_structural_field(config):
    event = normalize_event(
        {
            "case_id": "Declaration 100000",
            "activity": "Declaration SUBMITTED",
            "ts": "2026-01-15 09:30:00",
            "process": "domestic_declarations",
            "row_key": "dd:41",
            "amount": 120.5,
            "loaded_at": "ignored",
        },
        config,
        ordinal=7,
    )

    assert event.case_id == "Declaration 100000"
    assert event.activity == "Declaration SUBMITTED"
    assert event.process_type == "domestic_declarations"
    assert event.event_key == "dd:41"
    assert event.ordinal == 7
    assert event.attributes == {"amount": 120.5}


def test_excluded_columns_never_reach_attributes(config):
    event = normalize_event(
        {
            "case_id": "c1",
            "activity": "a",
            "process": "p",
            "loaded_at": "2026-01-01",
        },
        config,
    )

    assert "loaded_at" not in event.attributes


def test_a_row_without_a_case_id_is_refused(config):
    with pytest.raises(EventNormalizationError) as error:
        normalize_event({"activity": "a", "process": "p"}, config, ordinal=3)

    assert error.value.ordinal == 3
    assert "case_id" in str(error.value)


def test_a_row_with_a_blank_activity_is_kept(config):
    """A blank activity still counts as an event: it has a timestamp and it
    contributes to the case's event count. Dropping it would silently shorten
    the case."""
    event = normalize_event(
        {"case_id": "c1", "activity": "  ", "process": "p"}, config
    )

    assert event.activity is None
    assert event.case_id == "c1"


@pytest.mark.parametrize("blank", ["nan", "None", "null", "NaT", ""])
def test_pandas_style_blanks_are_treated_as_absent(config, blank):
    event = normalize_event(
        {"case_id": "c1", "activity": blank, "process": "p"}, config
    )

    assert event.activity is None


def test_constant_process_type_is_used_when_the_column_is_absent():
    config = EventLogConfig(
        case_id_field="c", activity_field="a", process_type="fallback"
    )

    assert resolve_process_type({"c": "1", "a": "x"}, config) == "fallback"


def test_a_row_process_type_wins_over_the_constant():
    config = EventLogConfig(
        case_id_field="c",
        activity_field="a",
        process_type_field="p",
        process_type="fallback",
    )

    assert resolve_process_type({"p": "from_row"}, config) == "from_row"


def test_the_constant_is_used_when_the_row_column_is_blank():
    config = EventLogConfig(
        case_id_field="c",
        activity_field="a",
        process_type_field="p",
        process_type="fallback",
    )

    assert resolve_process_type({"p": ""}, config) == "fallback"


def test_default_process_type_is_the_last_resort():
    config = EventLogConfig(
        case_id_field="c", activity_field="a", process_type_field="p"
    )

    assert resolve_process_type({}, config) == DEFAULT_PROCESS_TYPE


def test_an_explicit_attribute_allow_list_wins():
    config = EventLogConfig(
        case_id_field="c",
        activity_field="a",
        process_type="p",
        attribute_fields=("amount",),
    )

    attributes = extract_attributes(
        {"c": "1", "a": "x", "amount": 5, "noise": "dropped"}, config
    )

    assert attributes == {"amount": 5}


def test_attributes_are_sorted_so_two_identical_rows_hash_alike():
    config = EventLogConfig(case_id_field="c", activity_field="a", process_type="p")

    first = extract_attributes({"c": "1", "a": "x", "z": 1, "b": 2}, config)
    second = extract_attributes({"c": "1", "a": "x", "b": 2, "z": 1}, config)

    assert list(first) == list(second) == ["b", "z"]


def test_normalize_events_can_skip_invalid_rows(config):
    rows = [
        {"case_id": "c1", "activity": "a", "process": "p"},
        {"activity": "orphan", "process": "p"},
        {"case_id": "c2", "activity": "b", "process": "p"},
    ]

    kept = list(normalize_events(rows, config, skip_invalid=True))

    assert [event.case_id for event in kept] == ["c1", "c2"]


def test_normalize_events_refuses_invalid_rows_by_default(config):
    rows = [
        {"case_id": "c1", "activity": "a", "process": "p"},
        {"activity": "orphan", "process": "p"},
    ]

    with pytest.raises(EventNormalizationError):
        list(normalize_events(rows, config))


def test_ordinals_follow_source_order(config):
    rows = [
        {"case_id": "c1", "activity": "a", "process": "p"},
        {"case_id": "c1", "activity": "b", "process": "p"},
    ]

    events = list(normalize_events(rows, config))

    assert [event.ordinal for event in events] == [0, 1]


# ============================================================
# Case identity
# ============================================================


def test_case_record_id_uses_the_one_canonical_grammar():
    record_id = make_case_record_id("erp_a", "travel_permit", "Permit 76455")

    assert record_id == "erp:erp_a:travel_permit:permit_76455"
    assert parse_canonical_id(record_id) == (
        "erp_a",
        "travel_permit",
        "permit_76455",
    )


def test_case_record_id_is_deterministic():
    first = make_case_record_id("erp_a", "declarations", "declaration 100000")
    second = make_case_record_id("erp_a", "declarations", "declaration 100000")

    assert first == second


def test_case_record_id_ignores_incidental_formatting():
    """Migrated from the prototype: whitespace and case must not fork identity."""
    assert make_case_record_id("erp_a", "Declarations", "  Declaration 100000 ") == (
        make_case_record_id("erp_a", "declarations", "declaration 100000")
    )


def test_two_source_systems_never_collide_on_the_same_case_number():
    """The component the prototype's identity scheme lacked."""
    left = make_case_record_id("erp_a", "orders", "1001")
    right = make_case_record_id("erp_b", "orders", "1001")

    assert left != right


def test_two_processes_never_collide_on_the_same_case_number():
    left = make_case_record_id("erp_a", "declarations", "100000")
    right = make_case_record_id("erp_a", "permits", "100000")

    assert left != right
