"""Mapping coverage metrics (Steps 24, 25, 26).

Coverage answers "how much of this source structure found a canonical home,
and does the canonical model get everything it requires?" It says nothing
about data quality - Phase 8 never sees a value, so it is in no position to
judge one.

The distinction that matters most here is between the two directions:

SOURCE coverage
    Of the fields this source has, how many were mapped? A low number means
    the source carries structure the canonical model has no target for.

REQUIRED TARGET coverage
    Of the fields the canonical model REQUIRES, how many did the source
    supply? A gap here means the profile is not transformation-ready, however
    good the source coverage looks - and Phase 9 must not be told otherwise.

A profile can easily have 95% source coverage and still be unusable because
the one required identifier is missing.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from erp_pipeline.mapping.canonical_model import CanonicalTargetModel
from erp_pipeline.mapping.models import (
    EntityCoverage,
    FieldDecision,
    FieldOutcome,
    MappingCoverage,
)


def compute_coverage(
    decisions: Sequence[FieldDecision],
    entity_matches: Mapping[str, str | None],
    canonical_model: CanonicalTargetModel,
    source_entity_names: Mapping[str, str] | None = None,
) -> MappingCoverage:
    """Compute whole-schema and per-entity coverage.

    ``entity_matches`` maps a source entity's NORMALIZED name to the canonical
    entity type it was matched to (or ``None``). ``source_entity_names`` maps
    the same normalized names to their display names, because decisions record
    the display name while matches are keyed by the normalized one.
    """
    display_to_normalized = {
        display: normalized
        for normalized, display in (source_entity_names or {}).items()
    }

    by_entity: dict[str, list[FieldDecision]] = {}
    for decision in decisions:
        by_entity.setdefault(decision.source_entity, []).append(decision)

    entity_coverages: list[EntityCoverage] = []

    for source_entity_display in sorted(by_entity):
        entity_decisions = by_entity[source_entity_display]
        normalized = display_to_normalized.get(
            source_entity_display, source_entity_display
        )
        target_entity_type = entity_matches.get(normalized)

        entity_coverages.append(
            _entity_coverage(
                source_entity=source_entity_display,
                target_entity_type=target_entity_type,
                decisions=entity_decisions,
                canonical_model=canonical_model,
            )
        )

    return MappingCoverage(
        total_fields=len(decisions),
        mapped_fields=_count(decisions, lambda d: d.outcome.is_selected),
        ambiguous_fields=_count(
            decisions, lambda d: d.outcome is FieldOutcome.AMBIGUOUS
        ),
        unmapped_fields=_count(
            decisions, lambda d: d.outcome is FieldOutcome.UNMAPPED
        ),
        review_required_fields=_count(
            decisions, lambda d: d.outcome is FieldOutcome.REVIEW_REQUIRED
        ),
        entities=tuple(entity_coverages),
    )


def _entity_coverage(
    source_entity: str,
    target_entity_type: str | None,
    decisions: Sequence[FieldDecision],
    canonical_model: CanonicalTargetModel,
) -> EntityCoverage:
    """Coverage for one source entity, including its required-target gaps."""
    selected_targets = {
        decision.selected.target_field
        for decision in decisions
        if decision.selected is not None
        and decision.selected.target_entity_type == target_entity_type
    }

    missing_required: tuple[str, ...] = ()

    if target_entity_type is not None:
        canonical_entity = canonical_model.entity(target_entity_type)
        if canonical_entity is not None:
            missing_required = tuple(
                sorted(
                    canonical_field.name
                    for canonical_field in canonical_entity.required_fields
                    if canonical_field.name not in selected_targets
                )
            )

    return EntityCoverage(
        source_entity=source_entity,
        target_entity_type=target_entity_type,
        total_fields=len(decisions),
        mapped_fields=_count(decisions, lambda d: d.outcome.is_selected),
        ambiguous_fields=_count(
            decisions, lambda d: d.outcome is FieldOutcome.AMBIGUOUS
        ),
        unmapped_fields=_count(
            decisions, lambda d: d.outcome is FieldOutcome.UNMAPPED
        ),
        review_required_fields=_count(
            decisions, lambda d: d.outcome is FieldOutcome.REVIEW_REQUIRED
        ),
        missing_required_targets=missing_required,
    )


def _count(decisions: Sequence[FieldDecision], predicate) -> int:
    return sum(1 for decision in decisions if predicate(decision))


__all__ = ["compute_coverage"]
