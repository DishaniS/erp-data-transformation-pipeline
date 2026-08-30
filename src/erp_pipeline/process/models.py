"""Contracts for the generic process/case layer.

WHY THIS LAYER EXISTS
---------------------
The canonical model in ``erp_pipeline.schemas`` describes a *record* and a
*document*. Neither shape can express what an ERP event log actually contains:
a business process instance made of ordered activities over time - a purchase
requisition moving through approval, an invoice moving through settlement, a
travel permit moving through authorisation.

Downstream consumers need exactly that shape. A governance model reasons about
what a case is allowed to do next; a workflow engine resolves the current state
of a process instance. Neither can be answered from a flat record.

CONFIGURATION, NOT DATASET KNOWLEDGE
------------------------------------
Nothing here knows what a travel permit is, which columns a particular ERP
names, or which dataset motivated the work. An ``EventLogConfig`` says where
the case id, the activity and the timestamp live in a given source; everything
else follows from that. A new ERP event log is a new configuration, never a
code change.

IDENTITY
--------
Case identity reuses the one canonical grammar rather than inventing a second:

    erp:{source_system_id}:{process_type}:{case_id}

The ``process_type`` occupies the entity-type slot because a case IS an
instance of its process, and because ``(process_type, case_id)`` is the natural
key of a case in every event log this has been tried against. The id is
therefore parseable by ``parse_canonical_id`` like any other canonical id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from erp_pipeline.process.errors import ProcessConfigurationError
from erp_pipeline.schemas.identity import (
    hash_json_payload,
    make_canonical_record_id,
    normalize_identifier,
)

#: Version of the process contracts. Bumped when the shape changes, never when
#: a configuration changes.
PROCESS_ENGINE_VERSION = "1.0"

#: Entity type used when a case is projected into an AI representation and a
#: caller supplied no process type. Kept as a constant so the fallback is
#: greppable rather than a bare string in three places.
DEFAULT_PROCESS_TYPE = "process_case"


# ============================================================
# Configuration
# ============================================================


@dataclass(frozen=True)
class EventLogConfig:
    """Where the process concepts live in one particular source.

    Field names are the source's own, before any canonical mapping. That is
    deliberate: process discovery is useful on a raw event log, long before a
    mapping profile exists for it.
    """

    #: Column/key holding the case (process instance) identifier. Required -
    #: without it there are events but no cases.
    case_id_field: str
    #: Column/key holding the activity name.
    activity_field: str
    #: Column/key holding the event timestamp. Optional: an event log with no
    #: usable timestamp still yields an ordered case, ordered by arrival.
    timestamp_field: str | None = None
    #: Column/key holding the process type, when the log mixes several.
    process_type_field: str | None = None
    #: Constant process type, used when ``process_type_field`` is absent or
    #: yields nothing. One of the two must resolve, or the case is refused.
    process_type: str | None = None
    #: Column/key holding a stable per-event business key. Optional; used for
    #: event identity and for change propagation.
    event_key_field: str | None = None
    #: Explicit allow-list of columns retained as event attributes. ``None``
    #: means "everything not already consumed above and not excluded".
    attribute_fields: tuple[str, ...] | None = None
    #: Columns never retained as attributes. Use for operational bookkeeping
    #: (surrogate ids, load timestamps) that would pollute the content hash.
    excluded_fields: frozenset[str] = frozenset()
    #: Optional map of ``canonical entity type -> source column`` recording
    #: which business entities a case refers to (``customer`` -> ``cust_no``).
    #: Consumed by downstream components that need to join a case to records.
    entity_reference_fields: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("case_id_field", "activity_field"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ProcessConfigurationError(
                    f"EventLogConfig.{name} is required and must be a non-empty "
                    f"column name, got {value!r}."
                )

        if not self.process_type_field and not self.process_type:
            raise ProcessConfigurationError(
                "EventLogConfig needs either process_type_field (read per row) "
                "or process_type (a constant). Without one of them a case "
                "cannot be identified, because process type is part of a "
                "case's natural key."
            )

        if self.attribute_fields is not None and not isinstance(
            self.attribute_fields, tuple
        ):
            raise ProcessConfigurationError(
                "EventLogConfig.attribute_fields must be a tuple or None."
            )

    @property
    def reserved_fields(self) -> frozenset[str]:
        """Columns consumed as process structure rather than as attributes."""
        names = {self.case_id_field, self.activity_field}

        for optional in (
            self.timestamp_field,
            self.process_type_field,
            self.event_key_field,
        ):
            if optional:
                names.add(optional)

        return frozenset(names)

    def fingerprint(self) -> str:
        """Everything that could change a built case, in one string.

        Folded into case content hashes so a case built under a different
        configuration is distinguishable from one built under this.
        """
        return "/".join(
            (
                f"process@{PROCESS_ENGINE_VERSION}",
                f"case={self.case_id_field}",
                f"activity={self.activity_field}",
                f"ts={self.timestamp_field}",
                f"ptf={self.process_type_field}",
                f"pt={self.process_type}",
                f"key={self.event_key_field}",
                f"attrs={sorted(self.attribute_fields) if self.attribute_fields else None}",
                f"excl={sorted(self.excluded_fields)}",
                f"refs={sorted(self.entity_reference_fields.items())}",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id_field": self.case_id_field,
            "activity_field": self.activity_field,
            "timestamp_field": self.timestamp_field,
            "process_type_field": self.process_type_field,
            "process_type": self.process_type,
            "event_key_field": self.event_key_field,
            "attribute_fields": (
                list(self.attribute_fields) if self.attribute_fields else None
            ),
            "excluded_fields": sorted(self.excluded_fields),
            "entity_reference_fields": dict(self.entity_reference_fields),
            "fingerprint": self.fingerprint(),
        }


@dataclass(frozen=True)
class CaseSummaryConfig:
    """How a case becomes the text an embedding model sees.

    Separate from ``EventLogConfig`` because two deployments can read the same
    log and want different summaries, and because changing the summary must be
    visible in the content hash without looking like the log changed.
    """

    include_process_header: bool = True
    include_timing: bool = True
    include_activity_sequence: bool = True
    #: Cap on activities named in the summary. A 400-event case would otherwise
    #: produce text far past the model's useful window.
    max_activities_listed: int = 10
    #: Hard cap on generated text, bounded explicitly rather than left to the
    #: model's silent truncation.
    max_characters: int = 4000
    version: str = "1.0"

    def __post_init__(self) -> None:
        if self.max_activities_listed < 1:
            raise ProcessConfigurationError(
                "CaseSummaryConfig.max_activities_listed must be at least 1."
            )
        if self.max_characters < 1:
            raise ProcessConfigurationError(
                "CaseSummaryConfig.max_characters must be at least 1."
            )

    def fingerprint(self) -> str:
        return (
            f"summary@{self.version}/hdr={int(self.include_process_header)}"
            f"/time={int(self.include_timing)}"
            f"/seq={int(self.include_activity_sequence)}"
            f"/max_act={self.max_activities_listed}"
            f"/max_chars={self.max_characters}"
        )


DEFAULT_SUMMARY_CONFIG = CaseSummaryConfig()


# ============================================================
# Events
# ============================================================


@dataclass(frozen=True)
class ProcessEvent:
    """One activity occurrence inside one case."""

    case_id: str
    activity: str | None
    process_type: str
    timestamp: datetime | None = None
    #: The source's own stable key for this event, when it has one.
    event_key: str | None = None
    #: Position in the source stream. Traceability only - never identity, and
    #: never part of a content hash, because it moves when the source reloads.
    ordinal: int | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self, include_attributes: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": self.case_id,
            "activity": self.activity,
            "process_type": self.process_type,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "event_key": self.event_key,
            "ordinal": self.ordinal,
        }

        if include_attributes:
            payload["attributes"] = dict(self.attributes)

        return payload


# ============================================================
# Cases
# ============================================================


def make_case_record_id(
    source_system_id: str, process_type: Any, case_id: Any
) -> str:
    """Build the canonical id for one process instance.

    Reuses ``make_canonical_record_id`` rather than inventing a second grammar,
    with ``process_type`` in the entity-type slot. The result parses like any
    other canonical id, and ``source_system_id`` keeps two ERP systems that
    happen to number their cases identically apart.
    """
    return make_canonical_record_id(
        source_system_id=source_system_id,
        entity_type=process_type,
        stable_source_key=case_id,
    )


@dataclass(frozen=True)
class ProcessCase:
    """One completed view of a process instance.

    ``current_state`` and ``allowed_next_states`` are what make this useful to
    a governance or workflow consumer: the first says where the case is, the
    second says where an observed process permits it to go. ``allowed_next_states``
    is populated from a :class:`ProcessModel` and is empty until one is applied,
    because a single case cannot know what the process as a whole allows.
    """

    case_record_id: str
    case_id: str
    process_type: str
    source_system_id: str
    total_events: int
    activity_sequence: tuple[str, ...]
    unique_activities: tuple[str, ...]
    events: tuple[ProcessEvent, ...]
    start_timestamp: datetime | None = None
    end_timestamp: datetime | None = None
    duration_seconds: float | None = None
    #: Last observed activity. The closest thing to a process state that an
    #: event log can honestly report: it is observed, not declared.
    current_state: str | None = None
    #: Successors observed elsewhere in the same process. Empty until a
    #: ProcessModel is applied - see ``with_allowed_next_states``.
    allowed_next_states: tuple[str, ...] = ()
    #: ``canonical entity type -> business key`` references carried by the case.
    entity_references: Mapping[str, str] = field(default_factory=dict)
    content_hash: str | None = None
    config_fingerprint: str | None = None

    @property
    def start_activity(self) -> str | None:
        return self.activity_sequence[0] if self.activity_sequence else None

    @property
    def end_activity(self) -> str | None:
        return self.activity_sequence[-1] if self.activity_sequence else None

    @property
    def duration_days(self) -> float | None:
        """Duration in days, the unit ERP process reporting normally uses."""
        if self.duration_seconds is None:
            return None

        return round(self.duration_seconds / 86400, 6)

    @property
    def is_complete(self) -> bool:
        """Whether both endpoints of the case are known.

        A case with no usable timestamps is still a valid case; it simply
        cannot support duration analysis, and saying so explicitly is better
        than reporting a duration of zero.
        """
        return self.start_timestamp is not None and self.end_timestamp is not None

    def with_allowed_next_states(
        self, allowed: Sequence[str]
    ) -> "ProcessCase":
        """Return a copy carrying the successors a process model permits."""
        from dataclasses import replace

        return replace(self, allowed_next_states=tuple(allowed))

    def hashable_content(self) -> dict[str, Any]:
        """The fields a content hash covers.

        Deliberately excludes ``events[].ordinal`` and every operational field:
        those move when the source is reloaded, and including them would make
        every rebuild look like a content change and re-embed the whole log.

        Also excludes ``allowed_next_states``, which is a property of the
        PROCESS rather than of this case - one new case elsewhere in the log
        must not invalidate every existing case's hash.
        """
        return {
            "case_record_id": self.case_record_id,
            "case_id": self.case_id,
            "process_type": self.process_type,
            "source_system_id": self.source_system_id,
            "total_events": self.total_events,
            "activity_sequence": list(self.activity_sequence),
            "start_timestamp": (
                self.start_timestamp.isoformat() if self.start_timestamp else None
            ),
            "end_timestamp": (
                self.end_timestamp.isoformat() if self.end_timestamp else None
            ),
            "duration_seconds": self.duration_seconds,
            "current_state": self.current_state,
            "entity_references": dict(self.entity_references),
        }

    def compute_content_hash(self) -> str:
        """Deterministic hash over this case's AI-relevant content."""
        return hash_json_payload(self.hashable_content())

    def to_dict(self, include_events: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            **self.hashable_content(),
            "unique_activities": list(self.unique_activities),
            "start_activity": self.start_activity,
            "end_activity": self.end_activity,
            "duration_days": self.duration_days,
            "allowed_next_states": list(self.allowed_next_states),
            "is_complete": self.is_complete,
            "content_hash": self.content_hash,
            "config_fingerprint": self.config_fingerprint,
            "process_engine_version": PROCESS_ENGINE_VERSION,
        }

        if include_events:
            payload["events"] = [event.to_dict() for event in self.events]

        return payload

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"ProcessCase(id={self.case_record_id!r}, "
            f"process={self.process_type!r}, events={self.total_events}, "
            f"state={self.current_state!r})"
        )


# ============================================================
# Process model
# ============================================================


@dataclass(frozen=True)
class ProcessModel:
    """A directly-follows model observed across many cases of one process.

    The smallest process model that answers the question a workflow or
    governance component actually asks - *given this case is at activity X,
    what has this process ever done next?* - without claiming to be a full
    process-mining discovery algorithm. It is descriptive, not normative: it
    reports what the log contains, never what a policy permits.
    """

    process_type: str
    #: ``activity -> {successor: observed count}``
    directly_follows: Mapping[str, Mapping[str, int]]
    start_activities: tuple[str, ...]
    end_activities: tuple[str, ...]
    case_count: int

    def allowed_next_states(self, activity: str | None) -> tuple[str, ...]:
        """Successors observed after ``activity``, most frequent first."""
        if activity is None:
            return tuple(self.start_activities)

        successors = self.directly_follows.get(activity)

        if not successors:
            return ()

        return tuple(
            name
            for name, _ in sorted(
                successors.items(), key=lambda pair: (-pair[1], pair[0])
            )
        )

    @property
    def activities(self) -> tuple[str, ...]:
        """Every activity the model has seen, in a stable order."""
        names = set(self.directly_follows)

        for successors in self.directly_follows.values():
            names.update(successors)

        names.update(self.start_activities)
        names.update(self.end_activities)

        return tuple(sorted(names))

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_type": self.process_type,
            "case_count": self.case_count,
            "activities": list(self.activities),
            "start_activities": list(self.start_activities),
            "end_activities": list(self.end_activities),
            "directly_follows": {
                activity: dict(successors)
                for activity, successors in sorted(self.directly_follows.items())
            },
        }


def normalize_process_type(value: Any) -> str:
    """Normalize a process type into an identifier component."""
    return normalize_identifier(value)


__all__ = [
    "PROCESS_ENGINE_VERSION",
    "DEFAULT_PROCESS_TYPE",
    "EventLogConfig",
    "CaseSummaryConfig",
    "DEFAULT_SUMMARY_CONFIG",
    "ProcessEvent",
    "ProcessCase",
    "ProcessModel",
    "make_case_record_id",
    "normalize_process_type",
]
