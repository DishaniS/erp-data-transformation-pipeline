"""Validation of generated or supplied mapping profiles (Step 30).

Checks a profile against the source schema and the canonical model, and
returns every problem at once rather than failing on the first - a reviewer
fixing mappings wants the whole list, not a game of whack-a-mole.

Validation is non-fatal by default. ``MappingValidationError`` is raised only
when a caller explicitly asks for strict enforcement, because a profile with
warnings is still a useful thing to review, whereas a profile that refuses to
exist is not.

TRANSFORMATION RULES ARE INSPECTED, NEVER EXECUTED
--------------------------------------------------
A ``TransformationRule`` is checked STRUCTURALLY - that its operation is a
member of the frozen ``TransformationOperation`` enum. Its ``config`` is not
interpreted, no value is converted, and nothing is dispatched. Running a rule
is Phase 9's job, and this module contains no code that could.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from erp_pipeline.mapping.canonical_model import CanonicalTargetModel
from erp_pipeline.mapping.compatibility import compare_types
from erp_pipeline.mapping.models import (
    FindingSeverity,
    ValidationFinding,
    ValidationReport,
)
from erp_pipeline.mapping.scoring import render_source_field_path
from erp_pipeline.schemas.enums import TransformationOperation
from erp_pipeline.schemas.mapping_models import MappingProfile
from erp_pipeline.schemas.source_models import SourceField, SourceSchema


def validate_profile(
    profile: MappingProfile,
    schema: SourceSchema,
    canonical_model: CanonicalTargetModel,
) -> ValidationReport:
    """Validate one mapping profile. Returns every finding."""
    findings: list[ValidationFinding] = []

    source_fields = _index_source_fields(schema)
    canonical_entity = canonical_model.entity(profile.target_entity_type)

    if canonical_entity is None:
        findings.append(
            ValidationFinding(
                severity=FindingSeverity.ERROR,
                code="unknown_target_entity",
                message=(
                    f"Profile targets entity type "
                    f"{profile.target_entity_type!r}, which canonical model "
                    f"{canonical_model.identity} does not declare."
                ),
            )
        )
        return ValidationReport(findings=tuple(findings))

    seen_source_fields: dict[str, int] = {}
    seen_targets: dict[str, list[str]] = {}

    for field_mapping in profile.field_mappings:
        source_path = field_mapping.source_field
        target_name = field_mapping.target_field

        seen_source_fields[source_path] = seen_source_fields.get(source_path, 0) + 1
        seen_targets.setdefault(target_name, []).append(source_path)

        # --- the source field must exist ---
        source_field = source_fields.get(source_path)
        if source_field is None:
            findings.append(
                ValidationFinding(
                    severity=FindingSeverity.ERROR,
                    code="unknown_source_field",
                    message=(
                        f"Mapping references source field {source_path!r}, "
                        f"which schema {schema.schema_id!r} does not contain."
                    ),
                    source_field=source_path,
                    target_field=target_name,
                )
            )
            continue

        # --- the target field must exist ---
        canonical_field = canonical_entity.field_by_name(target_name)
        if canonical_field is None:
            findings.append(
                ValidationFinding(
                    severity=FindingSeverity.ERROR,
                    code="unknown_target_field",
                    message=(
                        f"Mapping targets {profile.target_entity_type}."
                        f"{target_name}, which the canonical model does not "
                        "declare."
                    ),
                    source_field=source_path,
                    target_field=target_name,
                )
            )
            continue

        # --- the types must be convertible ---
        comparison = compare_types(
            source_field.normalized_data_type,
            canonical_field.data_type,
            source_is_array=source_field.is_array,
        )
        if comparison.blocks_auto_selection:
            findings.append(
                ValidationFinding(
                    severity=FindingSeverity.ERROR,
                    code="incompatible_type",
                    message=(
                        f"Mapping {source_path} -> {target_name} is not "
                        f"convertible: {comparison.explain()}."
                    ),
                    source_field=source_path,
                    target_field=target_name,
                )
            )
        elif comparison.compatibility.value == "lossy":
            findings.append(
                ValidationFinding(
                    severity=FindingSeverity.WARNING,
                    code="lossy_type_conversion",
                    message=(
                        f"Mapping {source_path} -> {target_name} needs a "
                        f"declared transformation: {comparison.explain()}."
                    ),
                    source_field=source_path,
                    target_field=target_name,
                )
            )

        # --- declared transformations must be structurally valid ---
        findings.extend(
            _validate_transformations(field_mapping, source_path, target_name)
        )

    findings.extend(_duplicate_source_findings(seen_source_fields))
    findings.extend(_collision_findings(seen_targets))
    findings.extend(
        _required_coverage_findings(profile, canonical_entity, seen_targets)
    )

    return ValidationReport(findings=tuple(findings))


def _validate_transformations(
    field_mapping, source_path: str, target_name: str
) -> list[ValidationFinding]:
    """Check declared rules STRUCTURALLY. Nothing is executed.

    The only question asked is whether the operation is a member of the frozen
    ``TransformationOperation`` vocabulary - a rule naming an operation no
    engine implements would fail at transformation time, and catching it here
    is free.
    """
    findings: list[ValidationFinding] = []

    for rule in field_mapping.transformations:
        if not isinstance(rule.operation, TransformationOperation):
            findings.append(
                ValidationFinding(
                    severity=FindingSeverity.ERROR,
                    code="unknown_transformation_operation",
                    message=(
                        f"Mapping {source_path} -> {target_name} declares "
                        f"transformation {rule.operation!r}, which is not a "
                        "member of the TransformationOperation vocabulary."
                    ),
                    source_field=source_path,
                    target_field=target_name,
                )
            )

    return findings


def _duplicate_source_findings(
    seen_source_fields: Mapping[str, int]
) -> list[ValidationFinding]:
    """One source field mapped to several targets (Step 23).

    Reported as a WARNING rather than an error: the Phase 1 contract
    deliberately allows it (a future concat/coalesce needs it), but automatic
    generation never produces it, so its presence means a human did it on
    purpose and should confirm.
    """
    return [
        ValidationFinding(
            severity=FindingSeverity.WARNING,
            code="source_field_mapped_multiple_times",
            message=(
                f"Source field {source_path!r} is mapped to {count} targets. "
                "This is legal but is never produced automatically; confirm it "
                "is intended."
            ),
            source_field=source_path,
        )
        for source_path, count in sorted(seen_source_fields.items())
        if count > 1
    ]


def _collision_findings(
    seen_targets: Mapping[str, Sequence[str]]
) -> list[ValidationFinding]:
    """Several source fields feeding one canonical target (Step 22)."""
    return [
        ValidationFinding(
            severity=FindingSeverity.WARNING,
            code="target_collision",
            message=(
                f"Canonical target {target!r} receives {len(sources)} source "
                f"fields {sorted(sources)}. At most one can be the real "
                "source unless a combining transformation is declared."
            ),
            target_field=target,
        )
        for target, sources in sorted(seen_targets.items())
        if len(sources) > 1
    ]


def _required_coverage_findings(
    profile: MappingProfile,
    canonical_entity,
    seen_targets: Mapping[str, Sequence[str]],
) -> list[ValidationFinding]:
    """Canonical required fields nothing maps onto (Step 24).

    An ERROR, not a warning: a profile missing a required target cannot
    produce a valid canonical record, so calling it transformation-ready would
    be false.
    """
    return [
        ValidationFinding(
            severity=FindingSeverity.ERROR,
            code="missing_required_target",
            message=(
                f"Canonical {canonical_entity.entity_type}."
                f"{canonical_field.name} is required but no source field maps "
                "to it. This profile is not transformation-ready."
            ),
            target_field=canonical_field.name,
        )
        for canonical_field in canonical_entity.required_fields
        if canonical_field.name not in seen_targets
    ]


def _index_source_fields(schema: SourceSchema) -> dict[str, SourceField]:
    return {
        render_source_field_path(field): field
        for entity in schema.entities
        for field in entity.fields
    }


__all__ = ["validate_profile"]
