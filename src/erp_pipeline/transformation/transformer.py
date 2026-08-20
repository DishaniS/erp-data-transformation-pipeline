"""The per-record pipeline: source record + mapping profile -> canonical record.

PIPELINE ORDER (Step 44)
------------------------
Fixed, single-path, and identical for every source technology::

    1. extract        read the source field by path (missing != null)
    2. null/default   apply declared null markers, then declared defaults
    3. rules          execute the profile's declared TransformationRules
    4. convert        one deterministic conversion to the canonical type
    5. normalize      apply declared string normalization
    6. assign         write into the candidate record, nested paths included
    7. computed       evaluate declared computed fields in dependency order
    8. validate       required / type / constraints / references
    9. decide         emit or reject

Defaults come BEFORE conversion on purpose. A default is a substitute for an
absent value, not a rescue for a broken one - so ``amount = "hello"`` can never
become ``0`` because a default exists (Step 15).

TRANSACTIONAL (Step 45)
-----------------------
The candidate record is assembled in full, then validated as a whole, and only
then emitted. A record with any blocking issue is rejected and its partial data
discarded. There is no half-transformed record in ``successful_records``.

SOURCE-INDEPENDENT
------------------
Not one branch anywhere on where the record came from. A PostgreSQL row, a
MongoDB document, a CSV row and an API payload are all a ``SourceRecord``
before this module sees them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from erp_pipeline.mapping.canonical_model import (
    CanonicalEntity,
    CanonicalTargetModel,
    DEFAULT_CANONICAL_MODEL,
)
from erp_pipeline.schemas.canonical_models import (
    CanonicalRecord,
    RecordProvenance,
    SourceReference,
)
from erp_pipeline.schemas.enums import FieldDataType, SourceType
from erp_pipeline.schemas.identity import IdentityError
from erp_pipeline.schemas.mapping_models import FieldMapping, MappingProfile
from erp_pipeline.schemas.run_models import DataQualityIssue
from erp_pipeline.transformation import rules as rule_engine
from erp_pipeline.transformation import type_converter
from erp_pipeline.transformation.errors import (
    ComputedFieldCycleError,
    TransformationConfigurationError,
    UnsupportedOperationError,
)
from erp_pipeline.transformation.models import (
    DEFAULT_OPTIONS,
    TRANSFORMATION_ENGINE_VERSION,
    ComputedField,
    ComputedOperation,
    ExtractionOutcome,
    IssueCode,
    RecordOutcome,
    RecordTransformationResult,
    RejectedRecord,
    SourceRecord,
    TransformationOptions,
)
from erp_pipeline.transformation.normalizer import normalize_value
from erp_pipeline.transformation.quality import make_issue
from erp_pipeline.transformation.validator import (
    ReferenceResolver,
    MISSING,
    resolve_path,
    validate_record,
)


@dataclass(frozen=True)
class TransformationContext:
    """Facts about the source that the mapping profile does not carry.

    ``source_type`` is REQUIRED and is never guessed. ``SourceReference``
    demands it, and inventing a technology label would put a false statement
    into a record's permanent provenance - which is worse than making the
    caller say what it is.
    """

    source_type: SourceType
    schema_id: str | None = None
    schema_version: str | None = None
    ingestion_method: str | None = None
    source_file_path: str | None = None


class RecordTransformer:
    """Executes one mapping profile against one source record at a time."""

    def __init__(
        self,
        canonical_model: CanonicalTargetModel | None = None,
        options: TransformationOptions | None = None,
        resolver: ReferenceResolver | None = None,
    ) -> None:
        self._model = canonical_model or DEFAULT_CANONICAL_MODEL
        self._options = options or DEFAULT_OPTIONS
        self._resolver = resolver
        # Fail fast: a dependency cycle is a configuration defect, and finding
        # it on record 40,000 rather than before record 1 helps nobody.
        self._computed_order = _order_computed_fields(self._options.computed_fields)

    @property
    def options(self) -> TransformationOptions:
        return self._options

    @property
    def canonical_model(self) -> CanonicalTargetModel:
        return self._model

    # ------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------

    def transform(
        self,
        source_record: SourceRecord,
        mapping_profile: MappingProfile,
        context: TransformationContext,
        run_id: str | None = None,
    ) -> RecordTransformationResult:
        """Transform one record, returning either a record or a rejection."""
        reference = source_record.reference()
        entity = self._model.entity(mapping_profile.target_entity_type)
        issues: list[DataQualityIssue] = []
        candidate: dict[str, Any] = {}
        applied: list[str] = []

        executable = self._executable_mappings(mapping_profile)

        if not executable:
            issues.append(
                make_issue(
                    IssueCode.NO_FIELDS_MAPPED,
                    "the mapping profile contains no field mapping in an "
                    "executable state; nothing was transformed",
                    record_reference=reference,
                    source_entity=mapping_profile.source_entity,
                    run_id=run_id,
                    options=self._options,
                )
            )
            return self._reject(source_record, mapping_profile, tuple(issues))

        for mapping in executable:
            self._apply_mapping(
                mapping=mapping,
                source_record=source_record,
                mapping_profile=mapping_profile,
                entity=entity,
                candidate=candidate,
                issues=issues,
                applied=applied,
                reference=reference,
                run_id=run_id,
            )

        self._apply_computed_fields(
            source_record=source_record,
            mapping_profile=mapping_profile,
            entity=entity,
            candidate=candidate,
            issues=issues,
            applied=applied,
            reference=reference,
            run_id=run_id,
        )

        issues.extend(
            validate_record(
                candidate,
                entity,
                self._options,
                record_reference=reference,
                source_entity=mapping_profile.source_entity,
                run_id=run_id,
                resolver=self._resolver,
            )
        )

        if any(issue.is_blocking for issue in issues):
            return self._reject(source_record, mapping_profile, tuple(issues))

        record = self._build_record(
            candidate=candidate,
            source_record=source_record,
            mapping_profile=mapping_profile,
            entity=entity,
            context=context,
            applied=applied,
            issues=issues,
            reference=reference,
            run_id=run_id,
        )

        if record is None:
            return self._reject(source_record, mapping_profile, tuple(issues))

        return RecordTransformationResult(
            outcome=RecordOutcome.TRANSFORMED,
            record=record,
            issues=tuple(issues),
        )

    # ------------------------------------------------------------
    # Step 5 - which mappings may execute
    # ------------------------------------------------------------

    def _executable_mappings(
        self, profile: MappingProfile
    ) -> tuple[FieldMapping, ...]:
        """Only decided mappings run.

        ``AUTO_ACCEPTED`` (the engine chose it on strong evidence) and
        ``APPROVED`` (a human chose it) are instructions. ``SUGGESTED``,
        ``REVIEW_REQUIRED`` and ``REJECTED`` are not: executing an undecided
        proposal would quietly promote a guess into production data, which is
        exactly what Phase 8's conservatism was protecting against.
        """
        return tuple(
            mapping
            for mapping in profile.field_mappings
            if mapping.status in self._options.executable_statuses
        )

    # ------------------------------------------------------------
    # Steps 1-6 - one field
    # ------------------------------------------------------------

    def _apply_mapping(
        self,
        mapping: FieldMapping,
        source_record: SourceRecord,
        mapping_profile: MappingProfile,
        entity: CanonicalEntity | None,
        candidate: dict[str, Any],
        issues: list[DataQualityIssue],
        applied: list[str],
        reference: str,
        run_id: str | None,
    ) -> None:
        target = mapping.target_field
        canonical_field = (
            entity.field_by_name(target) if entity is not None else None
        )

        # --- 1. extract ---
        outcome, value = extract_value(
            source_record.values, mapping.source_field, self._options
        )

        if outcome is ExtractionOutcome.MISSING:
            issues.append(
                make_issue(
                    IssueCode.SOURCE_FIELD_MISSING,
                    f"source field {mapping.source_field!r} is not present in "
                    "the record",
                    record_reference=reference,
                    source_entity=mapping_profile.source_entity,
                    field_name=mapping.source_field,
                    expected=f"a value for canonical target {target!r}",
                    run_id=run_id,
                    options=self._options,
                )
            )
        elif outcome is ExtractionOutcome.NULL:
            issues.append(
                make_issue(
                    IssueCode.SOURCE_VALUE_NULL,
                    f"source field {mapping.source_field!r} is null",
                    record_reference=reference,
                    source_entity=mapping_profile.source_entity,
                    field_name=mapping.source_field,
                    run_id=run_id,
                    options=self._options,
                )
            )

        # --- 2. default (absence only, never a conversion rescue) ---
        defaulted = False
        if value is None and target in self._options.defaults:
            value = self._options.defaults[target]
            defaulted = True

        # --- 3. declared transformation rules ---
        target_type = self._target_type(mapping, canonical_field)

        if mapping.transformations:
            applied.extend(
                f"{target}:{rule.operation.value}"
                for rule in mapping.transformations
            )
            rule_result = rule_engine.apply_rules(
                value,
                mapping.transformations,
                rule_engine.RuleContext(
                    source_values=source_record.values,
                    options=self._options,
                    target_type=target_type,
                ),
            )
            if not rule_result.ok:
                issues.append(
                    make_issue(
                        rule_result.code or IssueCode.RULE_EXECUTION_FAILED,
                        f"a declared transformation for canonical target "
                        f"{target!r} could not be applied: {rule_result.reason}",
                        record_reference=reference,
                        source_entity=mapping_profile.source_entity,
                        field_name=mapping.source_field,
                        expected=f"a value convertible to {target!r}",
                        value=value,
                        run_id=run_id,
                        options=self._options,
                    )
                )
                return
            value = rule_result.value

        if (
            outcome is ExtractionOutcome.MISSING
            and value is None
            and not defaulted
        ):
            # The source never sent this field and nothing supplied a
            # substitute, so the canonical target stays ABSENT rather than
            # being written as null. The difference is what lets validation
            # report REQUIRED_FIELD_MISSING (nothing produced a value) instead
            # of NULL_NOT_ALLOWED (the source said null), which are different
            # problems with different fixes (Step 6).
            return

        # --- 4. conversion ---
        conversion = type_converter.convert(value, target_type, self._options)

        if not conversion.ok:
            issues.append(
                make_issue(
                    conversion.code or IssueCode.TYPE_CONVERSION_FAILED,
                    f"source field {mapping.source_field!r} could not be "
                    f"converted to canonical target {target!r} "
                    f"({target_type.value if target_type else 'untyped'}): "
                    f"{conversion.reason}",
                    record_reference=reference,
                    source_entity=mapping_profile.source_entity,
                    field_name=mapping.source_field,
                    expected=(
                        target_type.value if target_type is not None else "a value"
                    ),
                    value=value,
                    run_id=run_id,
                    options=self._options,
                )
            )
            return

        value = conversion.value

        # --- 5. normalization ---
        value = normalize_value(value, target, self._options.normalization)

        # --- 6. assignment ---
        conflict = assign_value(candidate, target, value)

        if conflict is not None:
            issues.append(
                make_issue(
                    IssueCode.TARGET_PATH_CONFLICT,
                    f"canonical target {target!r} cannot be written: {conflict}",
                    record_reference=reference,
                    source_entity=mapping_profile.source_entity,
                    field_name=target,
                    expected="a free canonical target path",
                    run_id=run_id,
                    options=self._options,
                )
            )

    def _target_type(
        self, mapping: FieldMapping, canonical_field: Any
    ) -> FieldDataType | None:
        """The canonical model wins over the mapping's own declaration.

        A ``FieldMapping.target_type`` is what Phase 8 believed when it wrote
        the profile; the canonical model is what the target actually is. If
        they disagree the model is right, and converting to the mapping's
        belief would produce a record that fails its own type validation.
        """
        if canonical_field is not None:
            return canonical_field.data_type
        return mapping.target_type

    # ------------------------------------------------------------
    # Step 7 - computed fields
    # ------------------------------------------------------------

    def _apply_computed_fields(
        self,
        source_record: SourceRecord,
        mapping_profile: MappingProfile,
        entity: CanonicalEntity | None,
        candidate: dict[str, Any],
        issues: list[DataQualityIssue],
        applied: list[str],
        reference: str,
        run_id: str | None,
    ) -> None:
        for computed in self._computed_order:
            applied.append(f"{computed.target_field}:{computed.operation.value}")

            value, failure = self._evaluate_computed(
                computed, source_record, candidate
            )

            if failure is not None:
                issues.append(
                    make_issue(
                        IssueCode.COMPUTED_FIELD_INPUT_MISSING,
                        f"computed field {computed.target_field!r} could not be "
                        f"evaluated: {failure}",
                        record_reference=reference,
                        source_entity=mapping_profile.source_entity,
                        field_name=computed.target_field,
                        expected="all declared inputs to be present",
                        run_id=run_id,
                        options=self._options,
                    )
                )
                continue

            canonical_field = (
                entity.field_by_name(computed.target_field)
                if entity is not None
                else None
            )
            target_type = (
                canonical_field.data_type
                if canonical_field is not None
                else computed.target_type
            )

            conversion = type_converter.convert(value, target_type, self._options)

            if not conversion.ok:
                issues.append(
                    make_issue(
                        conversion.code or IssueCode.TYPE_CONVERSION_FAILED,
                        f"computed field {computed.target_field!r} produced a "
                        f"value that could not be converted: {conversion.reason}",
                        record_reference=reference,
                        source_entity=mapping_profile.source_entity,
                        field_name=computed.target_field,
                        value=value,
                        run_id=run_id,
                        options=self._options,
                    )
                )
                continue

            normalized = normalize_value(
                conversion.value, computed.target_field, self._options.normalization
            )
            conflict = assign_value(candidate, computed.target_field, normalized)

            if conflict is not None:
                issues.append(
                    make_issue(
                        IssueCode.TARGET_PATH_CONFLICT,
                        f"computed field {computed.target_field!r} cannot be "
                        f"written: {conflict}",
                        record_reference=reference,
                        source_entity=mapping_profile.source_entity,
                        field_name=computed.target_field,
                        run_id=run_id,
                        options=self._options,
                    )
                )

    def _evaluate_computed(
        self,
        computed: ComputedField,
        source_record: SourceRecord,
        candidate: Mapping[str, Any],
    ) -> tuple[Any, str | None]:
        """Evaluate one computed field. Returns ``(value, failure_reason)``.

        Inputs are looked up in the source record first, then in the candidate
        canonical record, so a computed field may build on a mapped canonical
        value or on another computed field.
        """
        if computed.operation is ComputedOperation.CONSTANT:
            return computed.constant, None

        resolved: list[Any] = []

        for name in computed.sources:
            outcome, value = extract_value(
                source_record.values, name, self._options
            )
            if outcome is ExtractionOutcome.MISSING:
                found = resolve_path(candidate, name)
                value = None if found is MISSING else found
                if found is MISSING:
                    outcome = ExtractionOutcome.MISSING
                else:
                    outcome = ExtractionOutcome.FOUND
            resolved.append(None if outcome is ExtractionOutcome.MISSING else value)

        if computed.operation is ComputedOperation.COALESCE:
            for item in resolved:
                if item is not None:
                    return item, None
            if computed.require_all_inputs:
                return None, (
                    f"none of the {len(computed.sources)} declared inputs had a "
                    "value"
                )
            return None, None

        # CONCAT
        parts: list[str] = []
        for name, item in zip(computed.sources, resolved):
            if item is None:
                if computed.require_all_inputs:
                    return None, f"input {name!r} is missing or null"
                continue
            parts.append(item if isinstance(item, str) else str(item))

        return computed.separator.join(parts), None

    # ------------------------------------------------------------
    # Steps 21, 22, 46 - the canonical record
    # ------------------------------------------------------------

    def _build_record(
        self,
        candidate: dict[str, Any],
        source_record: SourceRecord,
        mapping_profile: MappingProfile,
        entity: CanonicalEntity | None,
        context: TransformationContext,
        applied: list[str],
        issues: list[DataQualityIssue],
        reference: str,
        run_id: str | None,
    ) -> CanonicalRecord | None:
        """Assemble the frozen Phase 1 ``CanonicalRecord``.

        Identity is DETERMINISTIC and derived from the record's own business
        key - the canonical identifier field where the entity declares one,
        otherwise the source record's key. Never a UUID4, never a timestamp,
        never anything that changes between two runs over the same data
        (Step 22).
        """
        stable_key = self._stable_key(candidate, entity, source_record)

        if stable_key is None:
            issues.append(
                make_issue(
                    IssueCode.RECORD_IDENTITY_MISSING,
                    "no stable business key is available for this record: the "
                    "canonical identifier field produced no value and the "
                    "source record carries no key, so a deterministic canonical "
                    "id cannot be derived",
                    record_reference=reference,
                    source_entity=mapping_profile.source_entity,
                    expected="a canonical identifier value or a source key",
                    run_id=run_id,
                    options=self._options,
                )
            )
            return None

        source = SourceReference(
            source_system_id=mapping_profile.source_system_id,
            source_type=context.source_type,
            source_entity=mapping_profile.source_entity,
            source_record_key=str(stable_key),
        )

        provenance = RecordProvenance(
            schema_id=context.schema_id or mapping_profile.source_schema_id,
            schema_version=context.schema_version,
            ingestion_method=context.ingestion_method,
            original_record_id=(
                str(source_record.ordinal)
                if source_record.ordinal is not None
                else None
            ),
            source_file_path=context.source_file_path,
            metadata={"mapping_id": mapping_profile.mapping_id},
        )

        try:
            return CanonicalRecord.from_source(
                source=source,
                entity_type=mapping_profile.target_entity_type,
                stable_source_key=stable_key,
                normalized_data=candidate,
                sensitivity=self._options.sensitivity,
                provenance=provenance,
                metadata={
                    # Structural audit only: which profile, which engine, which
                    # rules by NAME. No before/after values (Step 75).
                    "mapping_id": mapping_profile.mapping_id,
                    "transformation_engine_version": TRANSFORMATION_ENGINE_VERSION,
                    "transformation_config": self._options.fingerprint(),
                    "canonical_model_identity": self._model.identity,
                    "validation_profile_version": self._options.validation.version,
                    "rules_applied": sorted(set(applied)),
                },
            )
        except IdentityError:
            issues.append(
                make_issue(
                    IssueCode.RECORD_IDENTITY_MISSING,
                    "the record's business key does not normalize to a usable "
                    "canonical identifier component",
                    record_reference=reference,
                    source_entity=mapping_profile.source_entity,
                    expected="a non-empty business key",
                    run_id=run_id,
                    options=self._options,
                )
            )
            return None

    def _stable_key(
        self,
        candidate: Mapping[str, Any],
        entity: CanonicalEntity | None,
        source_record: SourceRecord,
    ) -> Any | None:
        if entity is not None:
            identifier = entity.identifier_field
            if identifier is not None:
                value = resolve_path(candidate, identifier.name)
                if value is not MISSING and value is not None:
                    return value

        if source_record.record_key:
            return source_record.record_key

        return None

    # ------------------------------------------------------------
    # Rejection
    # ------------------------------------------------------------

    def _reject(
        self,
        source_record: SourceRecord,
        mapping_profile: MappingProfile,
        issues: tuple[DataQualityIssue, ...],
    ) -> RecordTransformationResult:
        """Build a rejection whose reasons are guaranteed non-empty (Step 42)."""
        reasons = tuple(
            dict.fromkeys(issue.code for issue in issues if issue.is_blocking)
        )

        if not reasons:
            # Only reachable if a caller rejects on non-blocking findings.
            reasons = tuple(dict.fromkeys(issue.code for issue in issues)) or (
                IssueCode.INTERNAL_TRANSFORMATION_ERROR.value,
            )

        rejected = RejectedRecord(
            record_reference=source_record.reference(),
            reasons=reasons,
            issues=issues,
            source_entity=mapping_profile.source_entity,
            ordinal=source_record.ordinal,
            mapping_id=mapping_profile.mapping_id,
            source_record=(
                source_record if self._options.retain_source_on_rejection else None
            ),
        )

        return RecordTransformationResult(
            outcome=RecordOutcome.REJECTED,
            issues=issues,
            rejected=rejected,
        )


# ============================================================
# Extraction and assignment (Steps 6, 7)
# ============================================================

def extract_value(
    values: Mapping[str, Any],
    path: str,
    options: TransformationOptions,
) -> tuple[ExtractionOutcome, Any]:
    """Read a source field by dotted path, distinguishing missing from null.

    The distinction matters (Step 6): a column the source never sent may be
    schema drift or a stale mapping, while a column sent as null is the source
    stating it has no value. Both arrive here as ``None`` in the end, but the
    ``ExtractionOutcome`` records which happened and the issue codes differ.

    A traversal that runs into a non-mapping reports MISSING rather than
    raising - a ``KeyError`` escaping into a batch would take down records that
    are perfectly fine.
    """
    if "[]" in path:
        # Array element access is not supported: nothing in the mapping
        # contract expresses which element is meant, and picking one would be
        # a guess.
        return ExtractionOutcome.MISSING, None

    current: Any = values

    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return ExtractionOutcome.MISSING, None
        current = current[segment]

    if current is None:
        return ExtractionOutcome.NULL, None

    if options.null_policy.is_null_marker(current):
        return ExtractionOutcome.NULL, None

    return ExtractionOutcome.FOUND, current


def assign_value(data: dict[str, Any], path: str, value: Any) -> str | None:
    """Write a value into a possibly-nested canonical path.

    Returns ``None`` on success, or a safe description of the conflict.

    A scalar is never silently replaced by an object (Step 7): if ``customer``
    already holds ``"ABC"`` and something tries to write ``customer.email``,
    that is a modelling contradiction and it is reported, not resolved.
    """
    segments = path.split(".")
    current = data

    for index, segment in enumerate(segments[:-1]):
        existing = current.get(segment, MISSING)

        if existing is MISSING:
            new_branch: dict[str, Any] = {}
            current[segment] = new_branch
            current = new_branch
            continue

        if not isinstance(existing, dict):
            return (
                f"path segment {'.'.join(segments[: index + 1])!r} already holds "
                f"a {type(existing).__name__} value, so it cannot also be an "
                "object"
            )

        current = existing

    leaf = segments[-1]

    if leaf in current:
        return (
            f"canonical target {path!r} has already been assigned by another "
            "mapping in this profile"
        )

    current[leaf] = value
    return None


# ============================================================
# Computed-field ordering (Step 20)
# ============================================================

def _order_computed_fields(
    computed_fields: Sequence[ComputedField],
) -> tuple[ComputedField, ...]:
    """Topologically order computed fields, refusing cycles.

    Edges exist only between computed fields: a computed field depending on a
    plain source field imposes no ordering, because the source record is fully
    available from the start.

    Ties are broken by declaration order, so the evaluation order is stable
    across runs (Step 66).
    """
    if not computed_fields:
        return ()

    by_target = {item.target_field: item for item in computed_fields}
    ordered: list[ComputedField] = []
    state: dict[str, int] = {}   # 0 = visiting, 1 = done
    stack: list[str] = []

    def visit(target: str) -> None:
        marker = state.get(target)

        if marker == 1:
            return

        if marker == 0:
            cycle = tuple(stack[stack.index(target):] + [target])
            raise ComputedFieldCycleError(
                "Computed fields form a dependency cycle: "
                + " -> ".join(cycle)
                + ". There is no evaluation order that satisfies them, so the "
                "configuration is refused rather than evaluated in an "
                "arbitrary one.",
                fields=cycle,
            )

        state[target] = 0
        stack.append(target)

        for dependency in by_target[target].sources:
            if dependency in by_target and dependency != target:
                visit(dependency)

        stack.pop()
        state[target] = 1
        ordered.append(by_target[target])

    for item in computed_fields:
        visit(item.target_field)

    return tuple(ordered)


__all__ = [
    "TransformationContext",
    "RecordTransformer",
    "extract_value",
    "assign_value",
]
