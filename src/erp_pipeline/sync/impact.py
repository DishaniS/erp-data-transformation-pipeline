"""Mapping impact analysis: what schema drift means for an active mapping.

THE QUESTION THIS ANSWERS (Step 45)
-----------------------------------
Drift detection says *the schema changed*. That is not enough to decide
anything. A dropped column nobody maps is noise; a dropped column feeding a
required canonical field stops the pipeline. The difference is the mapping, so
the gate has to read it.

    SchemaDiff + active MappingProfile  ->  MappingImpactReport
                                        ->  continue / review / block

TYPE COMPATIBILITY IS NOT REDECIDED HERE (Step 48)
--------------------------------------------------
Phase 8 already owns the question "can a source type feed this canonical
type?", with a documented matrix and a veto rule. This module calls
``compare_types`` rather than restating it, so the two can never disagree about
whether DECIMAL -> STRING is safe.

And critically: a type change is judged at the SCHEMA CONTRACT level, not
excused because Phase 9 might convert it at runtime. Phase 9 converting
``"2500.50"`` into a Decimal does not mean the source silently changing a
DECIMAL column to VARCHAR was fine - it means the breakage would show up later,
as rejected records, instead of now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from erp_pipeline.mapping.canonical_model import (
    CanonicalTargetModel,
    DEFAULT_CANONICAL_MODEL,
)
from erp_pipeline.mapping.compatibility import TypeCompatibility, compare_types
from erp_pipeline.schemas.enums import FieldDataType, MappingStatus
from erp_pipeline.schemas.mapping_models import FieldMapping, MappingProfile
from erp_pipeline.schemas.source_models import SourceSchema
from erp_pipeline.sync.drift import (
    DriftFinding,
    DriftReport,
    DriftSeverity,
    DriftStatus,
    DriftType,
    max_severity,
)


class ImpactAction(str, Enum):
    """What a human or the pipeline must do about one impacted mapping."""

    #: Nothing to do; the mapping still holds.
    NONE = "none"
    #: A human should look, but data can still flow.
    MAPPING_REVIEW_RECOMMENDED = "mapping_review_recommended"
    #: A human must look; running further risks wrong canonical data.
    MAPPING_REVIEW_REQUIRED = "mapping_review_required"
    #: The mapping cannot execute at all.
    MAPPING_INVALID = "mapping_invalid"
    #: A new source field nothing maps. Phase 8 owns deciding what to do with
    #: it - Phase 10 only reports it (Step 47).
    UNMAPPED_NEW_FIELD = "unmapped_new_field"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ImpactKind(str, Enum):
    """Why a mapping is impacted."""

    SOURCE_FIELD_REMOVED = "source_field_removed"
    SOURCE_ENTITY_REMOVED = "source_entity_removed"
    TYPE_COMPATIBILITY_CHANGED = "type_compatibility_changed"
    TYPE_CHANGED_STILL_COMPATIBLE = "type_changed_still_compatible"
    NULLABILITY_CHANGED = "nullability_changed"
    PRIMARY_KEY_CHANGED = "primary_key_changed"
    NEW_UNMAPPED_FIELD = "new_unmapped_field"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class MappingImpact:
    """One mapping affected by one drift finding."""

    source_field: str
    target_field: str | None
    kind: ImpactKind
    action: ImpactAction
    severity: DriftSeverity
    drift_type: DriftType
    old_value: Any = None
    new_value: Any = None
    detail: str | None = None

    @property
    def is_blocking(self) -> bool:
        return self.action in (
            ImpactAction.MAPPING_INVALID,
            ImpactAction.MAPPING_REVIEW_REQUIRED,
        )

    def describe(self) -> str:
        target = self.target_field or "<unmapped>"
        base = f"{self.source_field} -> {target}: {self.kind.value}"
        if self.old_value is not None or self.new_value is not None:
            base += f" ({self.old_value!r} -> {self.new_value!r})"
        return f"{base} [{self.action.value}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.source_field,
            "target_field": self.target_field,
            "kind": self.kind.value,
            "action": self.action.value,
            "severity": self.severity.value,
            "drift_type": self.drift_type.value,
            "old_value": None if self.old_value is None else str(self.old_value),
            "new_value": None if self.new_value is None else str(self.new_value),
            "detail": self.detail,
            "description": self.describe(),
        }


@dataclass(frozen=True)
class MappingImpactReport:
    """Every impacted mapping for one drift check."""

    mapping_id: str | None
    target_entity_type: str | None
    impacts: tuple[MappingImpact, ...] = ()
    severity: DriftSeverity = DriftSeverity.NON_BREAKING
    status: DriftStatus = DriftStatus.NO_DRIFT
    reasons: tuple[str, ...] = ()

    @property
    def blocking_impacts(self) -> tuple[MappingImpact, ...]:
        return tuple(item for item in self.impacts if item.is_blocking)

    @property
    def unmapped_new_fields(self) -> tuple[str, ...]:
        return tuple(
            item.source_field
            for item in self.impacts
            if item.action is ImpactAction.UNMAPPED_NEW_FIELD
        )

    def impacts_for(self, source_field: str) -> tuple[MappingImpact, ...]:
        return tuple(
            item for item in self.impacts if item.source_field == source_field
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_id": self.mapping_id,
            "target_entity_type": self.target_entity_type,
            "status": self.status.value,
            "severity": self.severity.value,
            "impact_count": len(self.impacts),
            "blocking_count": len(self.blocking_impacts),
            "unmapped_new_fields": list(self.unmapped_new_fields),
            "impacts": [item.to_dict() for item in self.impacts],
            "reasons": list(self.reasons),
        }


def _executable(mapping: FieldMapping) -> bool:
    """Only decided mappings matter, matching Phase 9's execution rule."""
    return mapping.status in (
        MappingStatus.AUTO_ACCEPTED,
        MappingStatus.APPROVED,
    )


def _field_lookup(schema: SourceSchema | None) -> dict[str, Any]:
    """Source fields by their rendered dotted path, matching Phase 8."""
    if schema is None:
        return {}

    lookup: dict[str, Any] = {}

    for entity in schema.entities:
        for source_field in entity.fields:
            segments = list(source_field.nested_path or ()) + [
                source_field.source_name
            ]
            path = ".".join(segment for segment in segments if segment != "[]")
            lookup[path] = source_field
            lookup[source_field.normalized_name] = source_field

    return lookup


def analyze_mapping_impact(
    drift: DriftReport,
    profile: MappingProfile | None,
    new_schema: SourceSchema | None = None,
    canonical_model: CanonicalTargetModel | None = None,
) -> MappingImpactReport:
    """Work out what a set of drift findings means for one mapping profile.

    Every impacted mapping is reported with its source field, canonical target,
    the drift that caused it and the action required, which is exactly the
    shape Step 45 asks for.
    """
    if profile is None:
        # No active mapping: structural drift is informational. Nothing can be
        # broken by it because nothing is consuming the schema yet.
        return MappingImpactReport(
            mapping_id=None,
            target_entity_type=None,
            impacts=(),
            severity=DriftSeverity.NON_BREAKING,
            status=(
                DriftStatus.NON_BREAKING_DRIFT
                if drift.has_drift
                else DriftStatus.NO_DRIFT
            ),
            reasons=("no active mapping profile was supplied for this entity",),
        )

    model = canonical_model or DEFAULT_CANONICAL_MODEL
    entity = model.entity(profile.target_entity_type)
    mappings = [m for m in profile.field_mappings if _executable(m)]
    by_source = {m.source_field: m for m in mappings}
    mapped_paths = set(by_source)

    fields = _field_lookup(new_schema)
    impacts: list[MappingImpact] = []
    severity = DriftSeverity.NON_BREAKING

    for finding in drift.findings:
        impacts.extend(
            _impacts_for_finding(finding, by_source, mapped_paths, fields, entity)
        )

    for impact in impacts:
        severity = max_severity(severity, impact.severity)

    status = _status_from(impacts, drift)
    reasons = tuple(impact.describe() for impact in impacts)

    return MappingImpactReport(
        mapping_id=profile.mapping_id,
        target_entity_type=profile.target_entity_type,
        impacts=tuple(impacts),
        severity=severity,
        status=status,
        reasons=reasons,
    )


def _impacts_for_finding(
    finding: DriftFinding,
    by_source: Mapping[str, FieldMapping],
    mapped_paths: set[str],
    fields: Mapping[str, Any],
    entity: Any,
) -> list[MappingImpact]:
    """Every mapping impacted by one drift finding."""
    name = finding.field_name

    # --- a mapped source field disappeared (Steps 46, 55) ---
    if finding.drift_type is DriftType.FIELD_REMOVED and name in mapped_paths:
        mapping = by_source[name]
        canonical_field = (
            entity.field_by_name(mapping.target_field) if entity else None
        )
        required = bool(canonical_field and canonical_field.required)

        return [
            MappingImpact(
                source_field=name,
                target_field=mapping.target_field,
                kind=ImpactKind.SOURCE_FIELD_REMOVED,
                # A removed field feeding a REQUIRED canonical target makes the
                # mapping unrunnable: every record would fail required-field
                # validation. Feeding an optional target it is still a decision
                # a human has to make.
                action=(
                    ImpactAction.MAPPING_INVALID
                    if required
                    else ImpactAction.MAPPING_REVIEW_REQUIRED
                ),
                severity=DriftSeverity.BREAKING,
                drift_type=finding.drift_type,
                detail=(
                    "the source field this mapping reads no longer exists"
                    + (" and its canonical target is required" if required else "")
                ),
            )
        ]

    if finding.drift_type is DriftType.ENTITY_REMOVED:
        return [
            MappingImpact(
                source_field=finding.entity,
                target_field=None,
                kind=ImpactKind.SOURCE_ENTITY_REMOVED,
                action=ImpactAction.MAPPING_INVALID,
                severity=DriftSeverity.BREAKING,
                drift_type=finding.drift_type,
                detail="the entity this profile reads no longer exists",
            )
        ]

    # --- a new source field nothing maps (Step 47) ---
    if finding.drift_type is DriftType.FIELD_ADDED and name not in mapped_paths:
        return [
            MappingImpact(
                source_field=name or finding.entity,
                target_field=None,
                kind=ImpactKind.NEW_UNMAPPED_FIELD,
                # Reported, never auto-mapped: Phase 8 owns mapping generation
                # and it is deliberately not invoked here.
                action=ImpactAction.UNMAPPED_NEW_FIELD,
                severity=DriftSeverity.NON_BREAKING,
                drift_type=finding.drift_type,
                detail=(
                    "a new source field appeared with no mapping; run the "
                    "Phase 8 mapping engine to propose one"
                ),
            )
        ]

    # --- a mapped field changed type (Steps 48, 54) ---
    if (
        finding.drift_type is DriftType.FIELD_TYPE_CHANGED
        and finding.attribute == "normalized_data_type"
        and name in mapped_paths
    ):
        return [_type_change_impact(finding, by_source[name], entity)]

    # --- nullability of a mapped field ---
    if (
        finding.drift_type is DriftType.FIELD_NULLABILITY_CHANGED
        and name in mapped_paths
    ):
        mapping = by_source[name]
        became_nullable = bool(finding.new_value) and not bool(finding.old_value)
        canonical_field = (
            entity.field_by_name(mapping.target_field) if entity else None
        )
        required = bool(canonical_field and canonical_field.required)

        if became_nullable and required:
            return [
                MappingImpact(
                    source_field=name,
                    target_field=mapping.target_field,
                    kind=ImpactKind.NULLABILITY_CHANGED,
                    action=ImpactAction.MAPPING_REVIEW_REQUIRED,
                    severity=DriftSeverity.POTENTIALLY_BREAKING,
                    drift_type=finding.drift_type,
                    old_value=finding.old_value,
                    new_value=finding.new_value,
                    detail=(
                        "the source may now be null where the canonical target "
                        "is required"
                    ),
                )
            ]

        return [
            MappingImpact(
                source_field=name,
                target_field=mapping.target_field,
                kind=ImpactKind.NULLABILITY_CHANGED,
                action=ImpactAction.MAPPING_REVIEW_RECOMMENDED,
                severity=DriftSeverity.NON_BREAKING,
                drift_type=finding.drift_type,
                old_value=finding.old_value,
                new_value=finding.new_value,
            )
        ]

    # --- identity drift (Step 49) ---
    if finding.drift_type is DriftType.PRIMARY_KEY_CHANGED:
        mapping = by_source.get(name or "")
        return [
            MappingImpact(
                source_field=name or finding.entity,
                target_field=mapping.target_field if mapping else None,
                kind=ImpactKind.PRIMARY_KEY_CHANGED,
                # Always blocking, mapped or not: the primary key is what the
                # watermark tie-breaker and the canonical record key are built
                # from, so incremental correctness itself is in question.
                action=ImpactAction.MAPPING_REVIEW_REQUIRED,
                severity=DriftSeverity.BREAKING,
                drift_type=finding.drift_type,
                old_value=finding.old_value,
                new_value=finding.new_value,
                detail=(
                    "primary-key structure changed; incremental identity and "
                    "watermark tie-breaking depend on it"
                ),
            )
        ]

    return []


def _type_change_impact(
    finding: DriftFinding, mapping: FieldMapping, entity: Any
) -> MappingImpact:
    """Judge a type change using Phase 8's compatibility matrix (Step 48)."""
    canonical_field = (
        entity.field_by_name(mapping.target_field) if entity else None
    )
    target_type = (
        canonical_field.data_type if canonical_field else mapping.target_type
    )

    try:
        new_type = FieldDataType.from_value(finding.new_value)
    except ValueError:  # pragma: no cover - diff values come from the enum
        new_type = FieldDataType.UNKNOWN

    if target_type is None:
        return MappingImpact(
            source_field=mapping.source_field,
            target_field=mapping.target_field,
            kind=ImpactKind.TYPE_COMPATIBILITY_CHANGED,
            action=ImpactAction.MAPPING_REVIEW_REQUIRED,
            severity=DriftSeverity.POTENTIALLY_BREAKING,
            drift_type=finding.drift_type,
            old_value=finding.old_value,
            new_value=finding.new_value,
            detail="the canonical target declares no type to compare against",
        )

    comparison = compare_types(new_type, target_type)

    if comparison.compatibility is TypeCompatibility.EXACT:
        # The source type changed but still lands exactly on the target. Worth
        # telling a human; not worth stopping for.
        return MappingImpact(
            source_field=mapping.source_field,
            target_field=mapping.target_field,
            kind=ImpactKind.TYPE_CHANGED_STILL_COMPATIBLE,
            action=ImpactAction.MAPPING_REVIEW_RECOMMENDED,
            severity=DriftSeverity.NON_BREAKING,
            drift_type=finding.drift_type,
            old_value=finding.old_value,
            new_value=finding.new_value,
            detail=f"type {new_type.value} still matches the target exactly",
        )

    if comparison.compatibility is TypeCompatibility.WIDENING:
        return MappingImpact(
            source_field=mapping.source_field,
            target_field=mapping.target_field,
            kind=ImpactKind.TYPE_CHANGED_STILL_COMPATIBLE,
            action=ImpactAction.MAPPING_REVIEW_RECOMMENDED,
            severity=DriftSeverity.NON_BREAKING,
            drift_type=finding.drift_type,
            old_value=finding.old_value,
            new_value=finding.new_value,
            detail=f"type {new_type.value} widens losslessly into the target",
        )

    # LOSSY, UNKNOWN or INCOMPATIBLE. Note that DECIMAL -> STRING lands here as
    # LOSSY, and it is deliberately NOT waved through on the grounds that
    # Phase 9 can parse the text: the source's declared contract changed, and
    # that has to be visible (Step 48).
    return MappingImpact(
        source_field=mapping.source_field,
        target_field=mapping.target_field,
        kind=ImpactKind.TYPE_COMPATIBILITY_CHANGED,
        action=ImpactAction.MAPPING_REVIEW_REQUIRED,
        severity=(
            DriftSeverity.BREAKING
            if comparison.compatibility is TypeCompatibility.INCOMPATIBLE
            else DriftSeverity.POTENTIALLY_BREAKING
        ),
        drift_type=finding.drift_type,
        old_value=finding.old_value,
        new_value=finding.new_value,
        detail=(
            f"source type {new_type.value} against canonical "
            f"{target_type.value} is now {comparison.compatibility.value}"
        ),
    )


def _status_from(
    impacts: Sequence[MappingImpact], drift: DriftReport
) -> DriftStatus:
    """The gate's verdict once the mapping has been consulted.

    Note this can LOWER the structural verdict: a removed column nothing maps
    is structurally breaking but operationally irrelevant, and blocking a sync
    over it would be a false alarm that teaches people to ignore the gate.
    """
    if any(item.action is ImpactAction.MAPPING_INVALID for item in impacts):
        return DriftStatus.BLOCKED

    if any(
        item.action is ImpactAction.MAPPING_REVIEW_REQUIRED for item in impacts
    ):
        return DriftStatus.REVIEW_REQUIRED

    if impacts or drift.has_drift:
        return DriftStatus.NON_BREAKING_DRIFT

    return DriftStatus.NO_DRIFT


__all__ = [
    "ImpactAction",
    "ImpactKind",
    "MappingImpact",
    "MappingImpactReport",
    "analyze_mapping_impact",
]
