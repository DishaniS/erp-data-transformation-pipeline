"""Schema drift: detecting and classifying structural change at the source.

REUSE, NOT REIMPLEMENTATION (Step 42)
-------------------------------------
The comparison itself is Phase 2's ``compare_schemas`` / ``SchemaDiff``, used
unchanged. This module adds only what Phase 2 deliberately does not have: a
per-finding drift TYPE vocabulary, and a classification that is allowed to
consider the active mapping rather than only generic database convention
(Step 44).

That distinction matters. Phase 2 rightly says "a removed field is breaking"
as a statement about the schema. Phase 10 has to answer a different question:
*is this removal breaking for what we are actually doing with this source?* A
removed column nobody maps is noise; a removed column feeding a required
canonical field stops the pipeline. Only the mapping knows which.

DRIFT DOES NOT REDISCOVER (Step 41)
-----------------------------------
Nothing here discovers or infers a schema. Phases 4-7 already do that, and a
second discovery engine would be a second thing to keep correct. This module
receives two ``SourceSchema`` snapshots and reasons about the difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from erp_pipeline.catalog.versioning import (
    BreakingLevel,
    FieldChange,
    SchemaDiff,
    compare_schemas,
)
from erp_pipeline.schemas.source_models import SourceSchema


class DriftType(str, Enum):
    """What kind of structural change occurred (Step 43)."""

    ENTITY_ADDED = "entity_added"
    ENTITY_REMOVED = "entity_removed"
    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    FIELD_TYPE_CHANGED = "field_type_changed"
    FIELD_NULLABILITY_CHANGED = "field_nullability_changed"
    FIELD_REQUIREDNESS_CHANGED = "field_requiredness_changed"
    PRIMARY_KEY_CHANGED = "primary_key_changed"
    FIELD_ARRAYNESS_CHANGED = "field_arrayness_changed"
    FIELD_ATTRIBUTE_CHANGED = "field_attribute_changed"
    RELATIONSHIP_ADDED = "relationship_added"
    RELATIONSHIP_REMOVED = "relationship_removed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class DriftSeverity(str, Enum):
    """How much a drift finding threatens correctness.

    Mirrors Phase 2's ``BreakingLevel`` vocabulary deliberately, so the two
    layers cannot drift apart in their own terminology.
    """

    NON_BREAKING = "non_breaking"
    POTENTIALLY_BREAKING = "potentially_breaking"
    BREAKING = "breaking"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


_SEVERITY_ORDER = {
    DriftSeverity.NON_BREAKING: 0,
    DriftSeverity.POTENTIALLY_BREAKING: 1,
    DriftSeverity.BREAKING: 2,
}


def max_severity(left: DriftSeverity, right: DriftSeverity) -> DriftSeverity:
    return left if _SEVERITY_ORDER[left] >= _SEVERITY_ORDER[right] else right


_FROM_BREAKING_LEVEL = {
    BreakingLevel.NON_BREAKING: DriftSeverity.NON_BREAKING,
    BreakingLevel.POTENTIALLY_BREAKING: DriftSeverity.POTENTIALLY_BREAKING,
    BreakingLevel.BREAKING: DriftSeverity.BREAKING,
}


class DriftStatus(str, Enum):
    """The gate's verdict (Step 50).

    Explicit states, not free-form warnings, because the coordinator branches
    on this: ``BLOCKED`` stops data processing before a single record is
    transformed under a schema the mapping can no longer survive.
    """

    NO_DRIFT = "no_drift"
    NON_BREAKING_DRIFT = "non_breaking_drift"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def allows_data_sync(self) -> bool:
        return self is not DriftStatus.BLOCKED


@dataclass(frozen=True)
class DriftFinding:
    """One structural change, classified.

    Carries only structural facts - entity names, field names, type names.
    Never a value from the source, so a drift report is always safe to log.
    """

    drift_type: DriftType
    entity: str
    field_name: str | None = None
    attribute: str | None = None
    old_value: Any = None
    new_value: Any = None
    severity: DriftSeverity = DriftSeverity.NON_BREAKING
    detail: str | None = None

    def describe(self) -> str:
        location = (
            f"{self.entity}.{self.field_name}" if self.field_name else self.entity
        )
        base = f"{self.drift_type.value} at {location}"
        if self.attribute:
            base += f" ({self.attribute}: {self.old_value!r} -> {self.new_value!r})"
        return base

    def to_dict(self) -> dict[str, Any]:
        return {
            "drift_type": self.drift_type.value,
            "entity": self.entity,
            "field": self.field_name,
            "attribute": self.attribute,
            "old_value": (
                None if self.old_value is None else str(self.old_value)
            ),
            "new_value": (
                None if self.new_value is None else str(self.new_value)
            ),
            "severity": self.severity.value,
            "detail": self.detail,
            "description": self.describe(),
        }


@dataclass(frozen=True)
class DriftReport:
    """Everything one drift check found."""

    source_system_id: str
    old_schema_id: str | None
    new_schema_id: str
    status: DriftStatus
    findings: tuple[DriftFinding, ...] = ()
    diff: SchemaDiff | None = None
    #: The mapping impact analysis, when a profile was supplied.
    impact: Any = None
    severity: DriftSeverity = DriftSeverity.NON_BREAKING
    reasons: tuple[str, ...] = ()

    @property
    def has_drift(self) -> bool:
        return bool(self.findings)

    @property
    def is_blocked(self) -> bool:
        return self.status is DriftStatus.BLOCKED

    def findings_of(self, drift_type: DriftType) -> tuple[DriftFinding, ...]:
        return tuple(f for f in self.findings if f.drift_type is drift_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_system_id": self.source_system_id,
            "old_schema_id": self.old_schema_id,
            "new_schema_id": self.new_schema_id,
            "status": self.status.value,
            "severity": self.severity.value,
            "finding_count": len(self.findings),
            "findings": [item.to_dict() for item in self.findings],
            "reasons": list(self.reasons),
            "impact": self.impact.to_dict() if self.impact is not None else None,
        }


# ============================================================
# Detection
# ============================================================

#: Which changed attribute maps to which drift type. Anything not listed is
#: reported as a generic attribute change rather than being dropped - an
#: unclassified change is still a change, and silently ignoring it would be
#: worse than labelling it loosely.
_ATTRIBUTE_DRIFT_TYPES = {
    "normalized_data_type": DriftType.FIELD_TYPE_CHANGED,
    "source_data_type": DriftType.FIELD_TYPE_CHANGED,
    "nullable": DriftType.FIELD_NULLABILITY_CHANGED,
    "required": DriftType.FIELD_REQUIREDNESS_CHANGED,
    "is_primary_key": DriftType.PRIMARY_KEY_CHANGED,
    "is_array": DriftType.FIELD_ARRAYNESS_CHANGED,
}


def _classify_field_change(change: FieldChange) -> DriftFinding:
    drift_type = _ATTRIBUTE_DRIFT_TYPES.get(
        change.attribute, DriftType.FIELD_ATTRIBUTE_CHANGED
    )

    if drift_type is DriftType.FIELD_TYPE_CHANGED:
        # A normalized type change alters the data contract; the vendor type
        # alone (VARCHAR(50) -> VARCHAR(100)) does not.
        severity = (
            DriftSeverity.BREAKING
            if change.attribute == "normalized_data_type"
            else DriftSeverity.NON_BREAKING
        )
    elif drift_type is DriftType.PRIMARY_KEY_CHANGED:
        # Identity drift threatens incremental correctness itself (Step 49):
        # the watermark tie-breaker and the canonical record key both depend
        # on it.
        severity = DriftSeverity.BREAKING
    elif drift_type is DriftType.FIELD_NULLABILITY_CHANGED:
        # Becoming non-nullable can invalidate existing rows; becoming
        # nullable only loosens.
        severity = (
            DriftSeverity.POTENTIALLY_BREAKING
            if change.old_value and not change.new_value
            else DriftSeverity.NON_BREAKING
        )
    elif drift_type is DriftType.FIELD_ARRAYNESS_CHANGED:
        severity = DriftSeverity.BREAKING
    elif drift_type is DriftType.FIELD_REQUIREDNESS_CHANGED:
        severity = (
            DriftSeverity.POTENTIALLY_BREAKING
            if change.new_value
            else DriftSeverity.NON_BREAKING
        )
    else:
        severity = DriftSeverity.NON_BREAKING

    return DriftFinding(
        drift_type=drift_type,
        entity=change.entity,
        field_name=change.field,
        attribute=change.attribute,
        old_value=change.old_value,
        new_value=change.new_value,
        severity=severity,
    )


def findings_from_diff(diff: SchemaDiff, new_schema: SourceSchema | None = None) -> tuple[DriftFinding, ...]:
    """Turn a Phase 2 ``SchemaDiff`` into classified findings."""
    findings: list[DriftFinding] = []

    for entity in diff.added_entities:
        findings.append(
            DriftFinding(
                drift_type=DriftType.ENTITY_ADDED,
                entity=entity,
                severity=DriftSeverity.NON_BREAKING,
                detail="a new entity appeared; nothing existing depends on it yet",
            )
        )

    for entity in diff.removed_entities:
        findings.append(
            DriftFinding(
                drift_type=DriftType.ENTITY_REMOVED,
                entity=entity,
                severity=DriftSeverity.BREAKING,
                detail="an entity disappeared; anything mapped from it cannot run",
            )
        )

    added_lookup = {}
    if new_schema is not None:
        for entity in new_schema.entities:
            for source_field in entity.fields:
                added_lookup[(entity.normalized_name, source_field.normalized_name)] = (
                    source_field
                )

    for entity, field_name in diff.added_fields:
        source_field = added_lookup.get((entity, field_name))
        # A new OPTIONAL field is ordinary evolution. A new REQUIRED field is
        # not: existing mappings produce nothing for it.
        severity = (
            DriftSeverity.POTENTIALLY_BREAKING
            if source_field is not None
            and source_field.required
            and not source_field.nullable
            else DriftSeverity.NON_BREAKING
        )
        findings.append(
            DriftFinding(
                drift_type=DriftType.FIELD_ADDED,
                entity=entity,
                field_name=field_name,
                severity=severity,
                detail=(
                    "new required field with no mapping"
                    if severity is DriftSeverity.POTENTIALLY_BREAKING
                    else "new optional field"
                ),
            )
        )

    for entity, field_name in diff.removed_fields:
        findings.append(
            DriftFinding(
                drift_type=DriftType.FIELD_REMOVED,
                entity=entity,
                field_name=field_name,
                severity=DriftSeverity.BREAKING,
                detail="field no longer exists at the source",
            )
        )

    for change in diff.changed_fields:
        findings.append(_classify_field_change(change))

    for from_entity, to_entity in diff.added_relationships:
        findings.append(
            DriftFinding(
                drift_type=DriftType.RELATIONSHIP_ADDED,
                entity=from_entity,
                severity=DriftSeverity.NON_BREAKING,
                detail=f"new relationship to {to_entity}",
            )
        )

    for from_entity, to_entity in diff.removed_relationships:
        findings.append(
            DriftFinding(
                drift_type=DriftType.RELATIONSHIP_REMOVED,
                entity=from_entity,
                severity=DriftSeverity.POTENTIALLY_BREAKING,
                detail=f"relationship to {to_entity} removed",
            )
        )

    return tuple(findings)


def detect_drift(
    old_schema: SourceSchema | None,
    new_schema: SourceSchema,
    source_system_id: str | None = None,
) -> DriftReport:
    """Compare two snapshots and classify the difference.

    ``old_schema`` of ``None`` means this source has never been catalogued.
    That is a baseline, not drift: reporting a first discovery as "everything
    was added" would block every new source on its first run.
    """
    system = source_system_id or new_schema.source_system_id

    if old_schema is None:
        return DriftReport(
            source_system_id=system,
            old_schema_id=None,
            new_schema_id=new_schema.schema_id,
            status=DriftStatus.NO_DRIFT,
            findings=(),
            diff=None,
            severity=DriftSeverity.NON_BREAKING,
            reasons=("no previous schema in the catalog; this run is a baseline",),
        )

    diff = compare_schemas(old_schema, new_schema)
    findings = findings_from_diff(diff, new_schema)

    if not findings:
        return DriftReport(
            source_system_id=system,
            old_schema_id=old_schema.schema_id,
            new_schema_id=new_schema.schema_id,
            status=DriftStatus.NO_DRIFT,
            findings=(),
            diff=diff,
            severity=DriftSeverity.NON_BREAKING,
        )

    severity = DriftSeverity.NON_BREAKING
    for finding in findings:
        severity = max_severity(severity, finding.severity)

    # Structural status BEFORE mapping impact is considered. The gate in
    # impact.py may raise it to BLOCKED, and may also LOWER a structural
    # breaking change to non-breaking when nothing maps the affected field.
    status = {
        DriftSeverity.NON_BREAKING: DriftStatus.NON_BREAKING_DRIFT,
        DriftSeverity.POTENTIALLY_BREAKING: DriftStatus.REVIEW_REQUIRED,
        DriftSeverity.BREAKING: DriftStatus.REVIEW_REQUIRED,
    }[severity]

    return DriftReport(
        source_system_id=system,
        old_schema_id=old_schema.schema_id,
        new_schema_id=new_schema.schema_id,
        status=status,
        findings=findings,
        diff=diff,
        severity=severity,
        reasons=tuple(finding.describe() for finding in findings),
    )


__all__ = [
    "DriftType",
    "DriftSeverity",
    "DriftStatus",
    "DriftFinding",
    "DriftReport",
    "max_severity",
    "findings_from_diff",
    "detect_drift",
]
