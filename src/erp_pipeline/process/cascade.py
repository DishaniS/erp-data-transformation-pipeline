"""Carry one changed event through to the vector of the case it belongs to.

THE GAP THIS CLOSES
-------------------
Incremental sync knows how to detect that one source row changed. Case building
knows how to assemble a case from its events. Between them sits a question
neither answers: *which case did that row change, and how do I rebuild only
that one?*

Without an answer the only correct response to a single changed event is to
rebuild every case in the log - which, on a log of a few hundred thousand
events, means re-embedding the entire corpus because one timestamp moved.

WHAT THIS PROVIDES
------------------
Implementations of the Phase 10 propagation protocols, in terms of process
concepts rather than any particular schema:

    ProcessCaseResolver              changed event  -> affected case id(s)
    ProcessCaseRepresentationBuilder affected case  -> rebuilt AIRepresentation
    InMemoryCaseEventSource          a testable event source

The coordinator then does the rest: it compares the rebuilt representation's
content hash against the ledger, skips the unchanged, embeds the changed, and
drops the vector of a case whose events have all disappeared.

WHY THE KEY INDEX EXISTS
------------------------
Case identity is NORMALIZED - ``Declaration 100000`` becomes
``erp:sys:declarations:declaration_100000`` - so the source's own key cannot be
recovered by parsing the identifier back. Re-deriving it by splitting would
query for a case that does not exist, which looks exactly like "this case was
deleted" and would silently drop a live vector. The index remembers the
mapping instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence

from erp_pipeline.process.case_builder import build_case
from erp_pipeline.process.event_normalizer import normalize_event
from erp_pipeline.process.models import EventLogConfig, make_case_record_id
from erp_pipeline.process.service import ProcessCaseService
from erp_pipeline.sync.propagation import AIRepresentation


class CaseEventSource(Protocol):
    """The narrow data surface the cascade needs.

    An interface rather than a database handle, so the cascade logic is
    provable without a live store and so nothing here grows into a second
    data-access layer.
    """

    def case_id_for_event(self, event_key: str) -> str | None:
        """The case a given source event belongs to, or ``None`` if unknown."""
        ...  # pragma: no cover - protocol declaration

    def events_for_case(self, case_id: str) -> Sequence[Mapping[str, Any]]:
        """Every current source row belonging to one case."""
        ...  # pragma: no cover - protocol declaration


@dataclass
class CaseKeyIndex:
    """Remembers the source's own case key for each canonical case id.

    See the module docstring: normalization is lossy, so the reverse mapping
    has to be recorded rather than recomputed.
    """

    mapping: dict[str, str] = field(default_factory=dict)

    def remember(self, case_record_id: str, case_id: str) -> None:
        self.mapping[case_record_id] = case_id

    def resolve(self, case_record_id: str) -> str | None:
        return self.mapping.get(case_record_id)

    def forget(self, case_record_id: str) -> None:
        self.mapping.pop(case_record_id, None)

    def __len__(self) -> int:
        return len(self.mapping)


@dataclass
class ProcessCaseResolver:
    """Which case one changed event affects.

    Exactly one, in the ordinary case - which is the entire point. Rebuilding
    every case because one event moved is the behaviour this replaces.

    The case id is taken from the change's own payload when it carries one,
    because that avoids a database round trip; otherwise the event source is
    asked. A change that resolves to no case yields no work rather than an
    error: an event that was never part of a case has nothing to propagate.
    """

    access: CaseEventSource
    source_system_id: str
    config: EventLogConfig
    process_type: str | None = None
    index: CaseKeyIndex = field(default_factory=CaseKeyIndex)
    calls: int = 0

    def _process_type_for(self, payload: Mapping[str, Any]) -> str:
        if self.config.process_type_field:
            value = payload.get(self.config.process_type_field)

            if value:
                return str(value)

        return self.process_type or self.config.process_type or "process_case"

    def resolve_affected(self, change: Any, record: Any = None) -> tuple[str, ...]:
        self.calls += 1

        payload = getattr(change, "payload", None) or {}
        case_id = payload.get(self.config.case_id_field)

        if case_id is None:
            case_id = self.access.case_id_for_event(
                str(getattr(change, "record_key", "") or "")
            )

        if not case_id:
            return ()

        case_id = str(case_id)
        case_record_id = make_case_record_id(
            self.source_system_id, self._process_type_for(payload), case_id
        )
        self.index.remember(case_record_id, case_id)

        return (case_record_id,)


@dataclass
class ProcessCaseRepresentationBuilder:
    """Rebuild ONE case from its current events.

    Reads only the events belonging to that case, so the cost is proportional
    to one case rather than to the whole log.

    Returning ``None`` is meaningful and load-bearing: it tells the coordinator
    that every event of the case has gone, so the case no longer exists and its
    vector must go with it.
    """

    access: CaseEventSource
    service: ProcessCaseService
    index: CaseKeyIndex = field(default_factory=CaseKeyIndex)
    rebuild_calls: int = 0
    rebuilt_keys: list[str] = field(default_factory=list)

    def rebuild(self, key: str) -> AIRepresentation | None:
        self.rebuild_calls += 1
        self.rebuilt_keys.append(key)

        case_id = self.index.resolve(key)

        if case_id is None:
            # Falling back to parsing the normalized identifier would query the
            # wrong case, so the fallback is explicit and deliberately last.
            case_id = key.rsplit(":", 1)[-1] if ":" in key else key

        rows = list(self.access.events_for_case(case_id))

        if not rows:
            return None

        config = self.service.config
        events = [
            normalize_event(row, config, ordinal=ordinal)
            for ordinal, row in enumerate(rows)
        ]

        case = build_case(events, self.service.source_system_id, config)

        return self.service.to_representation(case)


@dataclass
class InMemoryCaseEventSource:
    """A ``CaseEventSource`` backed by a list of rows.

    Documents exactly what a SQL implementation has to provide, and lets the
    cascade be proved without a database.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    case_id_field: str = "case_id"
    event_key_field: str = "event_key"

    def add(self, row: Mapping[str, Any]) -> None:
        self.rows.append(dict(row))

    def remove_case(self, case_id: str) -> int:
        before = len(self.rows)
        self.rows = [
            row
            for row in self.rows
            if str(row.get(self.case_id_field)) != str(case_id)
        ]

        return before - len(self.rows)

    def case_id_for_event(self, event_key: str) -> str | None:
        for row in self.rows:
            if str(row.get(self.event_key_field)) == str(event_key):
                value = row.get(self.case_id_field)

                return str(value) if value is not None else None

        return None

    def events_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows
            if str(row.get(self.case_id_field)) == str(case_id)
        ]


def build_case_cascade(
    access: CaseEventSource,
    service: ProcessCaseService,
    process_type: str | None = None,
) -> tuple[ProcessCaseResolver, ProcessCaseRepresentationBuilder]:
    """Wire a resolver and a builder that share one key index.

    Sharing the index matters: the resolver is what learns the source's own
    case key, and the builder is what needs it. Two separate indexes would send
    the builder to the fallback path on every change.
    """
    index = CaseKeyIndex()

    resolver = ProcessCaseResolver(
        access=access,
        source_system_id=service.source_system_id,
        config=service.config,
        process_type=process_type,
        index=index,
    )
    builder = ProcessCaseRepresentationBuilder(
        access=access, service=service, index=index
    )

    return resolver, builder


__all__ = [
    "CaseEventSource",
    "CaseKeyIndex",
    "ProcessCaseResolver",
    "ProcessCaseRepresentationBuilder",
    "InMemoryCaseEventSource",
    "build_case_cascade",
]
