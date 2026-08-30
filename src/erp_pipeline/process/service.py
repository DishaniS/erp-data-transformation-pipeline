"""The process layer's public entry point.

Composes normalization, case building and process-model derivation, and
projects the result into the contracts the rest of the framework already
speaks - ``CanonicalRecord``, ``AIRepresentation`` - rather than inventing a
third representation for cases.

That projection is the whole point of the layer. Once a case is an
``AIRepresentation`` it flows through the existing embedding service, the
existing hybrid storage router and the existing search endpoint with no
case-specific code anywhere downstream.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.ai.models import make_representation_id
from erp_pipeline.process.case_builder import (
    apply_process_model,
    build_case_summary,
    build_cases,
    build_process_model,
)
from erp_pipeline.process.event_normalizer import normalize_events
from erp_pipeline.process.models import (
    PROCESS_ENGINE_VERSION,
    CaseSummaryConfig,
    DEFAULT_SUMMARY_CONFIG,
    EventLogConfig,
    ProcessCase,
    ProcessEvent,
    ProcessModel,
)
from erp_pipeline.schemas.canonical_models import (
    CanonicalRecord,
    RecordProvenance,
    SourceReference,
)
from erp_pipeline.schemas.enums import RecordType, SensitivityLevel, SourceType
from erp_pipeline.sync.propagation import AIRepresentation


class ProcessCaseService:
    """Builds ERP process cases from an event log and projects them.

    One instance is bound to one source system and one event-log shape, which
    is the unit a caller actually works with: a log has one schema, and cases
    from two different ERP systems must never be merged.
    """

    def __init__(
        self,
        source_system_id: str,
        config: EventLogConfig,
        source_type: SourceType = SourceType.POSTGRESQL,
        summary_config: CaseSummaryConfig | None = None,
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
    ) -> None:
        self._source_system_id = source_system_id
        self._config = config
        self._source_type = source_type
        self._summary_config = summary_config or DEFAULT_SUMMARY_CONFIG
        self._sensitivity = sensitivity

    # -- accessors -----------------------------------------------------

    @property
    def source_system_id(self) -> str:
        return self._source_system_id

    @property
    def config(self) -> EventLogConfig:
        return self._config

    @property
    def summary_config(self) -> CaseSummaryConfig:
        return self._summary_config

    # -- building ------------------------------------------------------

    def normalize(
        self, rows: Iterable[Mapping[str, Any]], skip_invalid: bool = False
    ) -> tuple[ProcessEvent, ...]:
        """Turn raw source rows into events."""
        return tuple(normalize_events(rows, self._config, skip_invalid=skip_invalid))

    def build_cases(
        self,
        rows: Iterable[Mapping[str, Any]],
        skip_invalid: bool = False,
        derive_process_model: bool = True,
    ) -> tuple[ProcessCase, ...]:
        """Build every case in an event log.

        ``derive_process_model`` runs the second pass that fills in
        ``allowed_next_states``. It is on by default because that field is the
        reason a downstream governance or workflow consumer wants cases at all,
        and off is available for a caller processing one case in isolation.
        """
        events = self.normalize(rows, skip_invalid=skip_invalid)
        cases = build_cases(events, self._source_system_id, self._config)

        if not derive_process_model or not cases:
            return cases

        return self.apply_models(cases)

    def apply_models(
        self, cases: Sequence[ProcessCase]
    ) -> tuple[ProcessCase, ...]:
        """Derive one model per process type and attach allowed next states."""
        by_type: dict[str, list[ProcessCase]] = {}

        for case in cases:
            by_type.setdefault(case.process_type, []).append(case)

        enriched: list[ProcessCase] = []

        for process_type, group in by_type.items():
            model = build_process_model(group, process_type=process_type)
            enriched.extend(apply_process_model(group, model))

        return tuple(sorted(enriched, key=lambda case: case.case_record_id))

    def build_models(
        self, cases: Sequence[ProcessCase]
    ) -> dict[str, ProcessModel]:
        """Derive one :class:`ProcessModel` per process type present."""
        by_type: dict[str, list[ProcessCase]] = {}

        for case in cases:
            by_type.setdefault(case.process_type, []).append(case)

        return {
            process_type: build_process_model(group, process_type=process_type)
            for process_type, group in by_type.items()
        }

    # -- projection ----------------------------------------------------

    def summarize(self, case: ProcessCase) -> str:
        """The deterministic text an embedding model receives for this case."""
        return build_case_summary(case, self._summary_config)

    def to_canonical_record(
        self,
        case: ProcessCase,
        provenance: RecordProvenance | None = None,
    ) -> CanonicalRecord:
        """Project a case into the frozen canonical contract.

        ``record_type`` is ``CASE`` - the vocabulary already reserved that
        member for exactly this - and the id derived here is byte-identical to
        ``case.case_record_id``, because both go through
        ``make_canonical_record_id`` with the same components.
        """
        source = SourceReference(
            source_system_id=self._source_system_id,
            source_type=self._source_type,
            source_entity=case.process_type,
            source_record_key=case.case_id,
        )

        return CanonicalRecord.from_source(
            source=source,
            entity_type=case.process_type,
            stable_source_key=case.case_id,
            normalized_data=case.to_dict(include_events=False),
            text_for_ai=self.summarize(case),
            sensitivity=self._sensitivity,
            provenance=provenance,
            record_type=RecordType.CASE,
            metadata={
                # Structural audit only - which engine and which configuration
                # produced this case. No business values.
                "process_engine_version": PROCESS_ENGINE_VERSION,
                "event_log_config": self._config.fingerprint(),
                "summary_config": self._summary_config.fingerprint(),
            },
        )

    def to_representation(self, case: ProcessCase) -> AIRepresentation:
        """Project a case into the AI representation the framework embeds.

        Carries the process facts a retrieval consumer needs in metadata -
        process type, current state, event count - so a hit can be filtered and
        interpreted without a second lookup, while the bulk of the case (its
        events) stays out of the vector payload.
        """
        text = self.summarize(case)
        content = case.to_dict(include_events=False)

        representation_id = make_representation_id(
            case.process_type, case.case_record_id
        )

        return AIRepresentation(
            representation_id=representation_id,
            entity_type=case.process_type,
            text_for_ai=text,
            content=content,
            source_record_ids=(case.case_record_id,),
            metadata={
                "canonical_record_id": case.case_record_id,
                "source_system_id": self._source_system_id,
                "source_type": self._source_type.value,
                "source_entity": case.process_type,
                "sensitivity": self._sensitivity.value,
                "record_type": RecordType.CASE.value,
                "process_type": case.process_type,
                "case_id": case.case_id,
                "current_state": case.current_state,
                "total_events": case.total_events,
                "process_engine_version": PROCESS_ENGINE_VERSION,
            },
        )

    def to_representations(
        self, cases: Iterable[ProcessCase]
    ) -> tuple[AIRepresentation, ...]:
        return tuple(self.to_representation(case) for case in cases)


__all__ = ["ProcessCaseService"]
