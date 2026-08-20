"""Validation of a candidate canonical record before it is published.

WHAT VALIDATES AGAINST WHAT
---------------------------
Two independent authorities, deliberately kept apart:

``CanonicalEntity`` (Phase 8)
    Declares which fields are REQUIRED and what DATA TYPE each holds. These are
    facts about the canonical model, and this module never overrides them.

``ValidationProfile`` (Phase 9 configuration)
    Declares allowed values, ranges, lengths, patterns and reference sets -
    constraints Phase 1 has no vocabulary for at all. Supplemental, versioned,
    and always additive.

NO INVENTED BUSINESS RULES (Step 27)
------------------------------------
An absent constraint is not checked. The engine never decides on its own that
an amount should be non-negative, that a customer id looks like ``C\\d+``, or
that a date should be in the past. Every such rule is a claim about a specific
ERP, and it must be declared by someone who knows that ERP.

REFERENCES (Step 30)
--------------------
No database call is made from this module, and no connection object reaches it.
Reference checking goes through a resolver interface; when no resolver is
supplied the result is ``REFERENCE_NOT_CHECKED``, which is honest, rather than
being silently reported as valid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from erp_pipeline.mapping.canonical_model import CanonicalEntity, CanonicalField
from erp_pipeline.schemas.run_models import DataQualityIssue
from erp_pipeline.transformation.models import (
    FieldConstraint,
    IssueCode,
    TransformationOptions,
)
from erp_pipeline.transformation.quality import make_issue
from erp_pipeline.transformation.type_converter import matches_type


# ============================================================
# Reference resolution (Step 30)
# ============================================================

@runtime_checkable
class ReferenceResolver(Protocol):
    """Answers whether a value exists in a named reference set.

    An interface, not an implementation, so ``validator.py`` carries no
    database coupling. A production resolver may query PostgreSQL; a test
    resolver holds a Python set; both satisfy this protocol.

    Returning ``None`` means "I do not know this set" and produces
    ``REFERENCE_NOT_CHECKED``. That third answer is the whole point: a resolver
    that cannot check something must be able to say so rather than guess.
    """

    def exists(self, reference_set: str, value: Any) -> bool | None:
        ...  # pragma: no cover - protocol declaration


@dataclass(frozen=True)
class KnownReferenceSet:
    """One named set of valid values, held in memory."""

    name: str
    values: frozenset[Any]

    @classmethod
    def of(cls, name: str, values: Iterable[Any]) -> "KnownReferenceSet":
        return cls(name=name, values=frozenset(values))


@dataclass(frozen=True)
class InMemoryReferenceResolver:
    """A resolver backed by in-memory sets.

    Sufficient for tests and for small controlled vocabularies. Deliberately
    the only implementation this phase ships: cross-database reference
    orchestration is not Phase 9's job.
    """

    sets: Mapping[str, KnownReferenceSet] = field(default_factory=dict)

    @classmethod
    def of(cls, **named: Iterable[Any]) -> "InMemoryReferenceResolver":
        return cls(
            sets={
                name: KnownReferenceSet.of(name, values)
                for name, values in named.items()
            }
        )

    def exists(self, reference_set: str, value: Any) -> bool | None:
        known = self.sets.get(reference_set)
        if known is None:
            return None
        return value in known.values


# ============================================================
# Value lookup inside a candidate record
# ============================================================

class _Missing:
    """Sentinel type for "no value at this path".

    A named class rather than a bare ``object()`` so there is exactly ONE
    sentinel in the package. Two modules each holding their own ``object()``
    would compare unequal, and every ``is MISSING`` check across the boundary
    would silently be False - which is precisely the kind of defect this engine
    exists to prevent, so it is designed out rather than tested for.
    """

    _instance: "_Missing | None" = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False


#: The single shared "absent" marker. Import this; never create another.
MISSING = _Missing()

_MISSING = MISSING


def resolve_path(data: Mapping[str, Any], path: str) -> Any:
    """Read a possibly-nested canonical field, or ``MISSING``.

    ``contact.email`` reads ``data["contact"]["email"]``. A path that runs into
    a non-mapping returns ``_MISSING`` rather than raising, because a missing
    field is an ordinary finding, not an engine error.
    """
    current: Any = data

    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return _MISSING
        current = current[segment]

    return current


# ============================================================
# Record validation
# ============================================================

def validate_record(
    normalized_data: Mapping[str, Any],
    canonical_entity: CanonicalEntity | None,
    options: TransformationOptions,
    *,
    record_reference: str,
    source_entity: str | None = None,
    run_id: str | None = None,
    resolver: ReferenceResolver | None = None,
    mapped_targets: frozenset[str] = frozenset(),
) -> tuple[DataQualityIssue, ...]:
    """Check a candidate record and return every finding at once.

    Every check runs even after one fails, so a reviewer sees the full picture
    for a record rather than fixing one problem and rediscovering the next on
    the following run.
    """
    issues: list[DataQualityIssue] = []

    if canonical_entity is not None:
        issues.extend(
            _validate_required(
                normalized_data, canonical_entity, options,
                record_reference, source_entity, run_id,
            )
        )
        issues.extend(
            _validate_types(
                normalized_data, canonical_entity, options,
                record_reference, source_entity, run_id,
            )
        )

    issues.extend(
        _validate_constraints(
            normalized_data, canonical_entity, options,
            record_reference, source_entity, run_id, resolver,
        )
    )

    return tuple(issues)


def _validate_required(
    data: Mapping[str, Any],
    entity: CanonicalEntity,
    options: TransformationOptions,
    record_reference: str,
    source_entity: str | None,
    run_id: str | None,
) -> list[DataQualityIssue]:
    """Required canonical fields must be present and non-null (Steps 23, 25)."""
    issues: list[DataQualityIssue] = []

    for canonical_field in entity.required_fields:
        value = resolve_path(data, canonical_field.name)

        if value is _MISSING:
            issues.append(
                make_issue(
                    IssueCode.REQUIRED_FIELD_MISSING,
                    f"canonical field {canonical_field.qualified_name!r} is "
                    "required but no mapping produced a value for it",
                    record_reference=record_reference,
                    source_entity=source_entity,
                    field_name=canonical_field.name,
                    expected="a non-null value",
                    run_id=run_id,
                    options=options,
                )
            )
            continue

        if value is None:
            issues.append(
                make_issue(
                    IssueCode.NULL_NOT_ALLOWED,
                    f"canonical field {canonical_field.qualified_name!r} is "
                    "required but the mapped value is null",
                    record_reference=record_reference,
                    source_entity=source_entity,
                    field_name=canonical_field.name,
                    expected="a non-null value",
                    run_id=run_id,
                    options=options,
                )
            )

    return issues


def _validate_types(
    data: Mapping[str, Any],
    entity: CanonicalEntity,
    options: TransformationOptions,
    record_reference: str,
    source_entity: str | None,
    run_id: str | None,
) -> list[DataQualityIssue]:
    """Confirm each value really is the canonical type (Step 24).

    A value that reached here as the wrong type means conversion did not run or
    did not do its job - for instance a mapping whose ``target_type`` disagrees
    with the canonical model's. Reporting it as a mismatch is what stops
    ``"2500.00"`` sitting in a DECIMAL field as text.
    """
    issues: list[DataQualityIssue] = []

    for canonical_field in entity.fields:
        value = resolve_path(data, canonical_field.name)

        if value is _MISSING or value is None:
            continue

        if not matches_type(value, canonical_field.data_type):
            issues.append(
                make_issue(
                    IssueCode.DATATYPE_MISMATCH,
                    f"canonical field {canonical_field.qualified_name!r} expects "
                    f"{canonical_field.data_type.value} but holds a "
                    f"{type(value).__name__} after transformation",
                    record_reference=record_reference,
                    source_entity=source_entity,
                    field_name=canonical_field.name,
                    expected=canonical_field.data_type.value,
                    value=value,
                    run_id=run_id,
                    options=options,
                )
            )

    return issues


def _validate_constraints(
    data: Mapping[str, Any],
    entity: CanonicalEntity | None,
    options: TransformationOptions,
    record_reference: str,
    source_entity: str | None,
    run_id: str | None,
    resolver: ReferenceResolver | None,
) -> list[DataQualityIssue]:
    """Apply every declared ``FieldConstraint`` (Steps 25-30)."""
    issues: list[DataQualityIssue] = []

    for constraint in options.validation.constraints:
        value = resolve_path(data, constraint.target_field)

        if value is _MISSING:
            # A constraint on a field this profile does not produce is not a
            # finding: profiles are per-entity, and a validation profile may
            # legitimately span several of them.
            continue

        if value is None:
            if constraint.nullable is False:
                issues.append(
                    make_issue(
                        IssueCode.NULL_NOT_ALLOWED,
                        f"canonical field {constraint.target_field!r} is "
                        "declared non-nullable but the mapped value is null",
                        record_reference=record_reference,
                        source_entity=source_entity,
                        field_name=constraint.target_field,
                        expected="a non-null value",
                        run_id=run_id,
                        options=options,
                    )
                )
            continue

        issues.extend(
            _check_allowed_values(
                value, constraint, record_reference, source_entity, run_id,
                options,
            )
        )
        issues.extend(
            _check_range(
                value, constraint, record_reference, source_entity, run_id,
                options,
            )
        )
        issues.extend(
            _check_identifier(
                value, constraint, record_reference, source_entity, run_id,
                options,
            )
        )
        issues.extend(
            _check_reference(
                value, constraint, record_reference, source_entity, run_id,
                options, resolver,
            )
        )

    return issues


def _check_allowed_values(
    value: Any,
    constraint: FieldConstraint,
    record_reference: str,
    source_entity: str | None,
    run_id: str | None,
    options: TransformationOptions,
) -> list[DataQualityIssue]:
    """Step 26. An out-of-vocabulary value is reported, never replaced."""
    if constraint.allowed_values is None:
        return []

    if value in constraint.allowed_values:
        return []

    return [
        make_issue(
            IssueCode.INVALID_ALLOWED_VALUE,
            f"canonical field {constraint.target_field!r} holds a value outside "
            f"its declared vocabulary of {len(constraint.allowed_values)} "
            "allowed value(s)",
            record_reference=record_reference,
            source_entity=source_entity,
            field_name=constraint.target_field,
            expected="one of the declared allowed values",
            value=value,
            run_id=run_id,
            options=options,
        )
    ]


def _check_range(
    value: Any,
    constraint: FieldConstraint,
    record_reference: str,
    source_entity: str | None,
    run_id: str | None,
    options: TransformationOptions,
) -> list[DataQualityIssue]:
    """Step 27. Numeric and temporal bounds, only where declared."""
    issues: list[DataQualityIssue] = []

    for bound, comparison, label in (
        (constraint.min_value, "min", "below the declared minimum"),
        (constraint.max_value, "max", "above the declared maximum"),
    ):
        if bound is None:
            continue

        try:
            violated = value < bound if comparison == "min" else value > bound
        except TypeError:
            # Comparing a Decimal with a date says nothing useful. Report it as
            # a mismatch rather than crashing the batch on one bad constraint.
            issues.append(
                make_issue(
                    IssueCode.OUT_OF_RANGE,
                    f"canonical field {constraint.target_field!r} cannot be "
                    f"compared against its declared {comparison}imum: the value "
                    f"is a {type(value).__name__} and the bound is a "
                    f"{type(bound).__name__}",
                    record_reference=record_reference,
                    source_entity=source_entity,
                    field_name=constraint.target_field,
                    expected=f"a value comparable with the declared {comparison}",
                    run_id=run_id,
                    options=options,
                )
            )
            continue

        if violated:
            issues.append(
                make_issue(
                    IssueCode.OUT_OF_RANGE,
                    f"canonical field {constraint.target_field!r} is {label}",
                    record_reference=record_reference,
                    source_entity=source_entity,
                    field_name=constraint.target_field,
                    expected=f"{comparison} {bound}",
                    value=value,
                    run_id=run_id,
                    options=options,
                )
            )

    return issues


def _check_identifier(
    value: Any,
    constraint: FieldConstraint,
    record_reference: str,
    source_entity: str | None,
    run_id: str | None,
    options: TransformationOptions,
) -> list[DataQualityIssue]:
    """Step 28. Structural checks on a declared business identifier.

    Length and pattern only, and only when declared. No country, tax or
    industry format is assumed anywhere - those belong to the ERP being
    integrated, not to this framework.
    """
    if (
        constraint.min_length is None
        and constraint.max_length is None
        and constraint.pattern is None
    ):
        return []

    text = value if isinstance(value, str) else str(value)
    issues: list[DataQualityIssue] = []

    if constraint.min_length is not None and len(text) < constraint.min_length:
        issues.append(
            make_issue(
                IssueCode.INVALID_IDENTIFIER,
                f"canonical field {constraint.target_field!r} is shorter than "
                f"its declared minimum length of {constraint.min_length}",
                record_reference=record_reference,
                source_entity=source_entity,
                field_name=constraint.target_field,
                expected=f"at least {constraint.min_length} characters",
                run_id=run_id,
                options=options,
            )
        )

    if constraint.max_length is not None and len(text) > constraint.max_length:
        issues.append(
            make_issue(
                IssueCode.INVALID_IDENTIFIER,
                f"canonical field {constraint.target_field!r} is longer than "
                f"its declared maximum length of {constraint.max_length}",
                record_reference=record_reference,
                source_entity=source_entity,
                field_name=constraint.target_field,
                expected=f"at most {constraint.max_length} characters",
                run_id=run_id,
                options=options,
            )
        )

    if constraint.pattern is not None and not re.fullmatch(
        constraint.pattern, text
    ):
        issues.append(
            make_issue(
                IssueCode.INVALID_IDENTIFIER,
                f"canonical field {constraint.target_field!r} does not match its "
                "declared pattern",
                record_reference=record_reference,
                source_entity=source_entity,
                field_name=constraint.target_field,
                expected="a value matching the declared pattern",
                run_id=run_id,
                options=options,
            )
        )

    return issues


def _check_reference(
    value: Any,
    constraint: FieldConstraint,
    record_reference: str,
    source_entity: str | None,
    run_id: str | None,
    options: TransformationOptions,
    resolver: ReferenceResolver | None,
) -> list[DataQualityIssue]:
    """Step 30. Three outcomes: found, not found, and not checked."""
    if constraint.reference_set is None:
        return []

    if resolver is None:
        return [
            make_issue(
                IssueCode.REFERENCE_NOT_CHECKED,
                f"canonical field {constraint.target_field!r} declares a "
                f"reference to {constraint.reference_set!r} but no resolver was "
                "supplied, so the reference was NOT verified",
                record_reference=record_reference,
                source_entity=source_entity,
                field_name=constraint.target_field,
                expected=f"a value present in {constraint.reference_set!r}",
                run_id=run_id,
                options=options,
            )
        ]

    outcome = resolver.exists(constraint.reference_set, value)

    if outcome is None:
        return [
            make_issue(
                IssueCode.REFERENCE_NOT_CHECKED,
                f"the resolver does not know reference set "
                f"{constraint.reference_set!r}, so the reference for "
                f"{constraint.target_field!r} was NOT verified",
                record_reference=record_reference,
                source_entity=source_entity,
                field_name=constraint.target_field,
                expected=f"a known reference set {constraint.reference_set!r}",
                run_id=run_id,
                options=options,
            )
        ]

    if outcome:
        return []

    return [
        make_issue(
            IssueCode.REFERENCE_NOT_FOUND,
            f"canonical field {constraint.target_field!r} references a value "
            f"that does not exist in {constraint.reference_set!r}",
            record_reference=record_reference,
            source_entity=source_entity,
            field_name=constraint.target_field,
            expected=f"a value present in {constraint.reference_set!r}",
            value=value,
            run_id=run_id,
            options=options,
        )
    ]


__all__ = [
    "ReferenceResolver",
    "KnownReferenceSet",
    "InMemoryReferenceResolver",
    "resolve_path",
    "validate_record",
]
