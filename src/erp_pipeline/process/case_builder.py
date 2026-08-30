"""Assemble normalized events into cases, and cases into a process model.

Nothing in this module refers to a column name or a dataset. It works on
:class:`ProcessEvent` objects, which is what makes the same builder usable for
an SAP approval log, a Dynamics order log, or a public research event log.

EVENT ORDERING
--------------
Events are ordered by ``(timestamp, ordinal)`` with unstamped events sorted
last but keeping their arrival order among themselves. Sorting on timestamp
alone would make two events sharing a timestamp order non-deterministically,
and a non-deterministic activity sequence produces a non-deterministic content
hash - which would re-embed the entire log on every rebuild.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.process.errors import CaseBuildError
from erp_pipeline.process.models import (
    CaseSummaryConfig,
    DEFAULT_SUMMARY_CONFIG,
    EventLogConfig,
    ProcessCase,
    ProcessEvent,
    ProcessModel,
    make_case_record_id,
)

#: Marker appended when a summary is bounded, so the truncation is visible in
#: the text itself rather than being silently absorbed by the model.
TRUNCATION_MARKER = "\n[content truncated]"


def sort_events(events: Iterable[ProcessEvent]) -> tuple[ProcessEvent, ...]:
    """Deterministic chronological order, stable for equal timestamps."""
    materialized = list(events)

    def key(event: ProcessEvent) -> tuple[int, float, int]:
        # (has_timestamp, when, arrival) - unstamped events sort last but keep
        # their relative arrival order.
        if event.timestamp is None:
            return (1, 0.0, event.ordinal if event.ordinal is not None else 0)

        return (
            0,
            event.timestamp.timestamp(),
            event.ordinal if event.ordinal is not None else 0,
        )

    return tuple(sorted(materialized, key=key))


def activity_sequence(events: Sequence[ProcessEvent]) -> tuple[str, ...]:
    """Ordered activity names, skipping events whose activity is absent."""
    return tuple(
        event.activity for event in events if event.activity is not None
    )


def unique_activities(sequence: Sequence[str]) -> tuple[str, ...]:
    """Distinct activities in first-occurrence order.

    First-occurrence rather than alphabetical, because the order in which a
    process first reaches each activity is itself information.
    """
    seen: set[str] = set()
    ordered: list[str] = []

    for activity in sequence:
        if activity not in seen:
            ordered.append(activity)
            seen.add(activity)

    return tuple(ordered)


def case_duration_seconds(
    start: datetime | None, end: datetime | None
) -> float | None:
    """Elapsed seconds between the first and last stamped event."""
    if start is None or end is None:
        return None

    return round((end - start).total_seconds(), 6)


def extract_entity_references(
    events: Sequence[ProcessEvent], config: EventLogConfig
) -> dict[str, str]:
    """First non-empty business-entity reference seen across the case.

    First rather than last: the entity a case was opened against is the one a
    downstream consumer wants to join on, and a later event that blanks the
    column should not erase it.
    """
    references: dict[str, str] = {}

    for entity_type, column in config.entity_reference_fields.items():
        for event in events:
            value = event.attributes.get(column)

            if value is None:
                continue

            text = str(value).strip()

            if text and text.lower() not in {"nan", "none", "null"}:
                references[entity_type] = text
                break

    return references


def build_case_summary(
    case: ProcessCase, config: CaseSummaryConfig | None = None
) -> str:
    """Render a case as the deterministic text an embedding model receives.

    Assembled from the case's own structure, in a fixed order, so two cases
    with identical content always produce identical text.
    """
    config = config or DEFAULT_SUMMARY_CONFIG
    parts: list[str] = []

    if config.include_process_header:
        parts.append(
            f"Process case {case.case_id} belongs to the "
            f"{case.process_type} process."
        )

    parts.append(f"The case contains {case.total_events} recorded event(s).")

    if case.start_activity and case.end_activity:
        parts.append(
            f"It starts with '{case.start_activity}' and its most recent "
            f"activity is '{case.end_activity}'."
        )

    if config.include_timing and case.is_complete:
        parts.append(
            f"The case ran from {case.start_timestamp.isoformat()} to "
            f"{case.end_timestamp.isoformat()}."
        )

        if case.duration_days is not None:
            parts.append(
                f"Its total duration is approximately "
                f"{case.duration_days:.4f} days."
            )

    if config.include_activity_sequence and case.unique_activities:
        listed = case.unique_activities[: config.max_activities_listed]
        parts.append("Activities observed: " + ", ".join(listed) + ".")

        if len(case.unique_activities) > len(listed):
            parts.append(
                f"({len(case.unique_activities) - len(listed)} further "
                "distinct activities not listed.)"
            )

    if case.entity_references:
        rendered = ", ".join(
            f"{entity}={key}"
            for entity, key in sorted(case.entity_references.items())
        )
        parts.append(f"Related business records: {rendered}.")

    text = " ".join(parts)

    if len(text) > config.max_characters:
        text = text[: config.max_characters - len(TRUNCATION_MARKER)] + (
            TRUNCATION_MARKER
        )

    return text


def build_case(
    events: Sequence[ProcessEvent],
    source_system_id: str,
    config: EventLogConfig,
) -> ProcessCase:
    """Assemble one case from the events belonging to it.

    Every event must share the case's id; a caller that mixes cases would
    otherwise produce a case whose activity sequence interleaves two process
    instances, which is silently wrong rather than loudly wrong.
    """
    if not events:
        raise CaseBuildError("cannot build a case from zero events")

    ordered = sort_events(events)
    case_id = ordered[0].case_id

    mismatched = {event.case_id for event in ordered} - {case_id}

    if mismatched:
        raise CaseBuildError(
            f"events for case {case_id!r} also contain case id(s) "
            f"{sorted(mismatched)!r}; a case must be built from its own events "
            "only",
            case_id=case_id,
        )

    process_type = ordered[0].process_type
    sequence = activity_sequence(ordered)
    stamped = [event.timestamp for event in ordered if event.timestamp]

    start = min(stamped) if stamped else None
    end = max(stamped) if stamped else None

    case = ProcessCase(
        case_record_id=make_case_record_id(
            source_system_id, process_type, case_id
        ),
        case_id=case_id,
        process_type=process_type,
        source_system_id=source_system_id,
        total_events=len(ordered),
        activity_sequence=sequence,
        unique_activities=unique_activities(sequence),
        events=ordered,
        start_timestamp=start,
        end_timestamp=end,
        duration_seconds=case_duration_seconds(start, end),
        # The last OBSERVED activity. Honest about what an event log can know:
        # this is where the case has got to, not a declared workflow state.
        current_state=sequence[-1] if sequence else None,
        entity_references=extract_entity_references(ordered, config),
        config_fingerprint=config.fingerprint(),
    )

    # Assigned after construction so the hash covers the finished case.
    from dataclasses import replace

    return replace(case, content_hash=case.compute_content_hash())


def group_events(
    events: Iterable[ProcessEvent],
) -> dict[tuple[str, str], list[ProcessEvent]]:
    """Group events by ``(process_type, case_id)`` - a case's natural key.

    Process type is part of the key because two different processes may reuse
    the same case numbering, and merging them would fabricate a case that never
    existed.
    """
    grouped: dict[tuple[str, str], list[ProcessEvent]] = defaultdict(list)

    for event in events:
        grouped[(event.process_type, event.case_id)].append(event)

    return dict(grouped)


def build_cases(
    events: Iterable[ProcessEvent],
    source_system_id: str,
    config: EventLogConfig,
) -> tuple[ProcessCase, ...]:
    """Build every case present in a stream of events.

    Returned sorted by ``case_record_id`` so two runs over the same log emit
    cases in the same order.
    """
    grouped = group_events(events)

    cases = [
        build_case(group, source_system_id, config)
        for group in grouped.values()
    ]

    return tuple(sorted(cases, key=lambda case: case.case_record_id))


def build_process_model(
    cases: Sequence[ProcessCase], process_type: str | None = None
) -> ProcessModel:
    """Derive a directly-follows model from a set of cases.

    Counts each observed transition once per occurrence, so a transition seen
    in 900 of 1,000 cases outranks one seen twice. That ordering is what makes
    ``allowed_next_states`` useful rather than merely exhaustive.
    """
    if process_type is None:
        types = {case.process_type for case in cases}

        if len(types) > 1:
            raise CaseBuildError(
                "cannot build one process model from cases of different "
                f"process types {sorted(types)!r}; build one model per type"
            )

        process_type = types.pop() if types else "unknown"

    follows: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    starts: dict[str, int] = defaultdict(int)
    ends: dict[str, int] = defaultdict(int)

    for case in cases:
        sequence = case.activity_sequence

        if not sequence:
            continue

        starts[sequence[0]] += 1
        ends[sequence[-1]] += 1

        for current, following in zip(sequence, sequence[1:]):
            follows[current][following] += 1

    def ranked(counter: Mapping[str, int]) -> tuple[str, ...]:
        return tuple(
            name
            for name, _ in sorted(
                counter.items(), key=lambda pair: (-pair[1], pair[0])
            )
        )

    return ProcessModel(
        process_type=process_type,
        directly_follows={
            activity: dict(successors)
            for activity, successors in follows.items()
        },
        start_activities=ranked(starts),
        end_activities=ranked(ends),
        case_count=len(cases),
    )


def apply_process_model(
    cases: Sequence[ProcessCase], model: ProcessModel
) -> tuple[ProcessCase, ...]:
    """Attach ``allowed_next_states`` to each case from an observed model.

    Kept separate from ``build_cases`` because it is a second pass by nature:
    what a case may do next depends on every OTHER case in the log, and a case
    built in isolation cannot know it. Keeping the passes separate is also what
    lets ``content_hash`` stay stable when an unrelated case is added.
    """
    return tuple(
        case.with_allowed_next_states(
            model.allowed_next_states(case.current_state)
        )
        for case in cases
    )


__all__ = [
    "TRUNCATION_MARKER",
    "sort_events",
    "activity_sequence",
    "unique_activities",
    "case_duration_seconds",
    "extract_entity_references",
    "build_case_summary",
    "build_case",
    "group_events",
    "build_cases",
    "build_process_model",
    "apply_process_model",
]
