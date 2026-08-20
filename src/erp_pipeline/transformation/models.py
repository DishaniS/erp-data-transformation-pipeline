"""Supplemental transformation models: source records, policy, results.

NOTHING HERE REPLACES A FROZEN CONTRACT
---------------------------------------
The authoritative outputs of Phase 9 are the Phase 1 contracts:
``CanonicalRecord``, ``DataQualityIssue`` and ``TransformationRun``. Everything
in this module is either CONFIGURATION (how to convert, what to validate, when
to stop) or the REPORTING structure around those outputs (which record was
rejected and why).

Why configuration models were needed at all
-------------------------------------------
Phase 1 defines no constraint vocabulary: there is no allowed-values, range,
pattern or nullable-override anywhere in ``schemas/``, and the Phase 8
``CanonicalField`` declares only ``required`` and ``data_type``. A validator
with no way to express "status must be one of these three" would be unable to
do the job Step 26 describes. So ``ValidationProfile`` is added here as
supplemental, versioned configuration - deliberately NOT as a competing
canonical contract, and it never overrides the canonical model's own
``required``/``data_type`` facts.

PRIVACY
-------
``RejectedRecord`` can hold the original record in memory for remediation, but
``to_dict()`` never serializes it unless a caller explicitly opts in. The
default serialization of every model here is safe to log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.schemas.enums import (
    FieldDataType,
    MappingStatus,
    QualitySeverity,
    SensitivityLevel,
)
from erp_pipeline.schemas.identity import hash_json_payload

#: Version of the transformation engine's behaviour. Recorded on every run so a
#: record transformed under different rules is traceable (Step 74).
TRANSFORMATION_ENGINE_VERSION = "1.0"


# ============================================================
# Issue codes (Steps 31, 32, 76)
# ============================================================

class IssueCode(str, Enum):
    """Stable, machine-readable data-quality codes.

    These are an operational and research INTERFACE: dashboards group by them,
    tests pin them, and a report from one run must be comparable with a report
    from another. They are therefore declared once, here, and never derived
    from an exception class name or a message string (Step 76).
    """

    # --- extraction ---
    SOURCE_FIELD_MISSING = "SOURCE_FIELD_MISSING"
    SOURCE_VALUE_NULL = "SOURCE_VALUE_NULL"
    TARGET_PATH_CONFLICT = "TARGET_PATH_CONFLICT"

    # --- conversion ---
    TYPE_CONVERSION_FAILED = "TYPE_CONVERSION_FAILED"
    UNKNOWN_ENUM_VALUE = "UNKNOWN_ENUM_VALUE"
    RULE_EXECUTION_FAILED = "RULE_EXECUTION_FAILED"
    UNSUPPORTED_DATA_TYPE = "UNSUPPORTED_DATA_TYPE"

    # --- computed fields ---
    COMPUTED_FIELD_DEPENDENCY_CYCLE = "COMPUTED_FIELD_DEPENDENCY_CYCLE"
    COMPUTED_FIELD_INPUT_MISSING = "COMPUTED_FIELD_INPUT_MISSING"

    # --- validation ---
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
    NULL_NOT_ALLOWED = "NULL_NOT_ALLOWED"
    DATATYPE_MISMATCH = "DATATYPE_MISMATCH"
    INVALID_ALLOWED_VALUE = "INVALID_ALLOWED_VALUE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    INVALID_IDENTIFIER = "INVALID_IDENTIFIER"

    # --- record level ---
    DUPLICATE_RECORD = "DUPLICATE_RECORD"
    REFERENCE_NOT_FOUND = "REFERENCE_NOT_FOUND"
    REFERENCE_NOT_CHECKED = "REFERENCE_NOT_CHECKED"
    RECORD_IDENTITY_MISSING = "RECORD_IDENTITY_MISSING"
    NO_FIELDS_MAPPED = "NO_FIELDS_MAPPED"

    # --- run level ---
    QUALITY_THRESHOLD_EXCEEDED = "QUALITY_THRESHOLD_EXCEEDED"
    INTERNAL_TRANSFORMATION_ERROR = "INTERNAL_TRANSFORMATION_ERROR"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ============================================================
# Source record abstraction (Step 4)
# ============================================================

class ExtractionOutcome(str, Enum):
    """Why a source lookup produced what it produced.

    ``MISSING`` and ``NULL`` are kept apart deliberately (Step 6). A column the
    source never sent and a column it explicitly sent as null are different
    facts: the first may be a schema drift or a mapping error, the second is
    the source stating that it has no value. Collapsing them would make both
    undiagnosable.
    """

    FOUND = "found"
    MISSING = "missing"
    NULL = "null"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class SourceRecord:
    """One source record, normalized to a key/value view at the boundary.

    This is the ONLY shape the transformation engine consumes. A PostgreSQL
    row, a MongoDB document, a Phase 6 ``SourceRow`` and an API payload all
    become this before any transformation logic runs, which is what keeps the
    engine free of ``if source_type is ...`` branches (Step 4).

    ``values`` may nest arbitrarily - a MongoDB document is a nested mapping,
    and extraction walks it by path.
    """

    values: Mapping[str, Any]
    #: Business key of this record in the source, when known. Used only for
    #: diagnostics and provenance - canonical identity comes from the mapped
    #: canonical identifier field, never from here.
    record_key: str | None = None
    #: Position in the batch, 1-based. A CSV row number, a cursor offset.
    ordinal: int | None = None
    source_entity: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
        record_key: str | None = None,
        ordinal: int | None = None,
        source_entity: str | None = None,
    ) -> "SourceRecord":
        """Wrap a plain dictionary - a DB row, a document, an API payload."""
        return cls(
            values=values,
            record_key=record_key,
            ordinal=ordinal,
            source_entity=source_entity,
        )

    @classmethod
    def from_source_row(cls, row: Any, source_entity: str | None = None) -> "SourceRecord":
        """Adapt a Phase 6 CSV ``SourceRow`` without importing Phase 6.

        Structural typing on purpose: this package must not depend on the
        ingestion package, or the "one engine, no source-specific paths" claim
        would be false at the import level. Anything exposing ``values`` and
        ``row_number`` works.
        """
        return cls(
            values=dict(getattr(row, "values", {}) or {}),
            record_key=None,
            ordinal=getattr(row, "row_number", None),
            source_entity=source_entity,
            metadata={"file_id": getattr(row, "file_id", None)}
            if getattr(row, "file_id", None)
            else {},
        )

    def reference(self) -> str:
        """A safe, non-sensitive way to name this record in a report.

        Never the record's content - a key or a position only. When neither is
        known the record is reported as unidentified rather than described by
        its data.
        """
        if self.record_key:
            return f"key={self.record_key}"
        if self.ordinal is not None:
            return f"ordinal={self.ordinal}"
        return "unidentified"


# ============================================================
# Conversion policy (Steps 8-14)
# ============================================================

class UnknownTypePolicy(str, Enum):
    """What to do when a target's declared type is ``UNKNOWN``."""

    #: Keep the value as the source gave it. Honest: an unknown target type is
    #: an absence of instruction, not an instruction to reject.
    PASS_THROUGH = "pass_through"
    #: Refuse to map into a target whose type was never established.
    REJECT = "reject"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class NullPolicy:
    """What counts as null on the way in (Step 14).

    ``null_markers`` is EMPTY by default. Treating ``"N/A"`` or ``"NULL"`` as
    null is a real and common need, but it is also a destructive guess: a
    status column may legitimately contain the string ``"NONE"``. So it is
    opt-in, per configuration, and the markers are compared case-sensitively
    unless ``case_insensitive_markers`` is set.
    """

    null_markers: tuple[str, ...] = ()
    case_insensitive_markers: bool = True
    #: Whether ``""`` means null. Off by default: an empty string is a value,
    #: and CSV in particular cannot distinguish "empty" from "absent" for the
    #: caller, so the caller decides.
    empty_string_is_null: bool = False

    def is_null_marker(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        if self.empty_string_is_null and value == "":
            return True
        if not self.null_markers:
            return False
        if self.case_insensitive_markers:
            lowered = value.lower()
            return any(marker.lower() == lowered for marker in self.null_markers)
        return value in self.null_markers

    def fingerprint(self) -> str:
        return (
            f"null(markers={sorted(self.null_markers)},"
            f"ci={int(self.case_insensitive_markers)},"
            f"empty={int(self.empty_string_is_null)})"
        )


#: Booleans a well-behaved source produces. Deliberately small (Step 12):
#: ``"approved"`` must never become ``True`` merely for being a non-empty
#: string, and ``1``/``"yes"`` are only accepted when a caller configures them.
DEFAULT_TRUE_VALUES: tuple[str, ...] = ("true",)
DEFAULT_FALSE_VALUES: tuple[str, ...] = ("false",)


@dataclass(frozen=True)
class BooleanPolicy:
    """Which literals convert to a boolean (Step 12)."""

    true_values: tuple[str, ...] = DEFAULT_TRUE_VALUES
    false_values: tuple[str, ...] = DEFAULT_FALSE_VALUES
    case_insensitive: bool = True
    #: Accept ``1``/``0`` integers. Off by default - an integer column is
    #: frequently a count, and reading it as a flag would be a silent misread.
    allow_integer_forms: bool = False

    def __post_init__(self) -> None:
        overlap = set(self._fold(self.true_values)) & set(self._fold(self.false_values))
        if overlap:
            from erp_pipeline.transformation.errors import (
                TransformationConfigurationError,
            )

            raise TransformationConfigurationError(
                f"BooleanPolicy declares {sorted(overlap)} as both true and "
                "false. A literal cannot mean both."
            )

    def _fold(self, values: Iterable[str]) -> tuple[str, ...]:
        return tuple(v.lower() if self.case_insensitive else v for v in values)

    def resolve(self, value: Any) -> bool | None:
        """Return the boolean this literal means, or ``None`` if it means none."""
        if isinstance(value, bool):
            return value

        if isinstance(value, int) and not isinstance(value, bool):
            if self.allow_integer_forms and value in (0, 1):
                return bool(value)
            return None

        if isinstance(value, str):
            candidate = value.lower() if self.case_insensitive else value
            if candidate in self._fold(self.true_values):
                return True
            if candidate in self._fold(self.false_values):
                return False

        return None

    def fingerprint(self) -> str:
        return (
            f"bool(t={sorted(self.true_values)},f={sorted(self.false_values)},"
            f"ci={int(self.case_insensitive)},int={int(self.allow_integer_forms)})"
        )


@dataclass(frozen=True)
class NumberPolicy:
    """Numeric conversion rules (Steps 10, 11)."""

    #: ``25.0`` -> ``25`` is allowed because it is mathematically exact.
    #: ``25.9`` -> ``25`` is never allowed: silent truncation of a financial
    #: quantity is precisely the corruption this phase exists to prevent.
    allow_integral_float_to_integer: bool = True
    #: NaN and Infinity are rejected for ordinary ERP financial data. They are
    #: also not representable in JSON, so accepting them would produce a record
    #: that cannot be serialized.
    allow_nan_and_infinity: bool = False
    #: Whether a numeric string may carry thousands separators. Off by default:
    #: ``"1,234"`` is ``1234`` in one locale and ``1.234`` in another, and
    #: guessing is how currency errors happen.
    allow_thousands_separator: bool = False

    def fingerprint(self) -> str:
        return (
            f"num(intfloat={int(self.allow_integral_float_to_integer)},"
            f"nan={int(self.allow_nan_and_infinity)},"
            f"sep={int(self.allow_thousands_separator)})"
        )


@dataclass(frozen=True)
class DatePolicy:
    """Temporal conversion rules (Step 13).

    ISO-8601 ONLY by default. ``03/04/2026`` is 3 April in one country and 4
    March in another, and there is no evidence in a schema that says which - so
    it is rejected unless a caller declares the format explicitly, either here
    or in a ``date_parse`` transformation rule.

    CANONICAL DATETIME IS UTC-AWARE. This follows the frozen contract rather
    than inventing a convention: ``serialization.to_rfc3339`` converts every
    aware datetime to UTC with a ``Z`` suffix and REJECTS naive datetimes, so a
    canonical datetime that was not aware could not be serialized at all. An
    offset-bearing input keeps its instant and is normalized to UTC.
    """

    #: Extra ``strptime`` formats for DATE targets, tried in order after ISO.
    date_formats: tuple[str, ...] = ()
    #: Extra ``strptime`` formats for DATETIME targets.
    datetime_formats: tuple[str, ...] = ()
    #: A naive datetime is interpreted as UTC. The alternative - rejecting it -
    #: would make most CSV and MySQL sources unmappable, so it is allowed but
    #: recorded here as an explicit, inspectable decision rather than a hidden
    #: assumption.
    assume_utc_when_naive: bool = True
    #: Accept a whole date where a datetime is wanted (midnight UTC).
    allow_date_as_datetime: bool = True

    def fingerprint(self) -> str:
        return (
            f"date(d={list(self.date_formats)},dt={list(self.datetime_formats)},"
            f"utc={int(self.assume_utc_when_naive)},"
            f"d2dt={int(self.allow_date_as_datetime)})"
        )


@dataclass(frozen=True)
class StringPolicy:
    """String conversion rules (Step 9).

    Leading-zero business identifiers survive because a STRING source value is
    never parsed as a number on the way to a STRING target - ``"007"`` stays
    ``"007"``. That is a consequence of the conversion table, not a special
    case.
    """

    #: ``25`` -> ``"25"``. Allowed: lossless and frequently needed when an ERP
    #: stores a business key as an integer.
    allow_number_to_string: bool = True
    allow_boolean_to_string: bool = False
    allow_temporal_to_string: bool = True
    #: Stringifying an object or an array produces Python ``repr``-ish text
    #: that no consumer can parse back. Off unless explicitly demanded.
    allow_structural_to_string: bool = False

    def fingerprint(self) -> str:
        return (
            f"str(num={int(self.allow_number_to_string)},"
            f"bool={int(self.allow_boolean_to_string)},"
            f"time={int(self.allow_temporal_to_string)},"
            f"struct={int(self.allow_structural_to_string)})"
        )


# ============================================================
# Normalization policy (Step 17)
# ============================================================

class CaseNormalization(str, Enum):
    NONE = "none"
    LOWER = "lower"
    UPPER = "upper"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class NormalizationPolicy:
    """Deterministic post-conversion string tidying.

    EVERY OPERATION IS OFF BY DEFAULT (Step 17). Business identifiers must not
    be mutated by accident: lower-casing ``AB-001`` silently changes a primary
    key, and a pipeline that does it globally will corrupt joins in a way that
    is very hard to trace back. So a caller opts in, and may scope the opt-in
    to named target fields.

    ``TransformationOperation.TRIM`` remains available as a per-field
    transformation rule for callers who want it declared in the mapping profile
    rather than in run configuration.
    """

    trim_strings: bool = False
    case: CaseNormalization = CaseNormalization.NONE
    #: Unicode normal form ("NFC", "NFKC", ...) or None. NFC is the safe choice
    #: when enabled; NFKC is lossy for some scripts and is not defaulted to.
    unicode_form: str | None = None
    collapse_internal_whitespace: bool = False
    #: When set, normalization applies ONLY to these canonical target fields.
    apply_to_fields: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.unicode_form is not None and self.unicode_form not in (
            "NFC", "NFD", "NFKC", "NFKD",
        ):
            from erp_pipeline.transformation.errors import (
                TransformationConfigurationError,
            )

            raise TransformationConfigurationError(
                f"NormalizationPolicy.unicode_form {self.unicode_form!r} is not "
                "a Unicode normal form. Use NFC, NFD, NFKC or NFKD."
            )

    @property
    def is_noop(self) -> bool:
        return (
            not self.trim_strings
            and self.case is CaseNormalization.NONE
            and self.unicode_form is None
            and not self.collapse_internal_whitespace
        )

    def applies_to(self, target_field: str) -> bool:
        if self.apply_to_fields is None:
            return True
        return target_field in self.apply_to_fields

    def fingerprint(self) -> str:
        return (
            f"norm(trim={int(self.trim_strings)},case={self.case.value},"
            f"uni={self.unicode_form},ws={int(self.collapse_internal_whitespace)},"
            f"fields={sorted(self.apply_to_fields) if self.apply_to_fields else None})"
        )


# ============================================================
# Computed fields (Steps 19, 20)
# ============================================================

class ComputedOperation(str, Enum):
    """The allow-listed computed-field operations.

    A CLOSED list, dispatched by name. There is no expression language, no
    ``eval``, no ``exec`` and no user-supplied code path anywhere in this
    package - a computed field is data describing which inputs to combine and
    how.

    These are deliberately NOT added to the frozen Phase 1
    ``TransformationOperation`` enum: ``COALESCE`` is not a member of it, and
    amending a frozen contract to suit this phase would be exactly the kind of
    change the phase brief forbids. Computed fields are run configuration, and
    they live here.
    """

    CONCAT = "concat"
    COALESCE = "coalesce"
    CONSTANT = "constant"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class ComputedField:
    """One canonical field derived from other fields rather than mapped.

    ``sources`` name SOURCE fields (or other computed targets), and they are
    recorded on the produced record's audit metadata so a reader can see what a
    computed value was built from - names only, never values (Step 75).
    """

    target_field: str
    operation: ComputedOperation
    sources: tuple[str, ...] = ()
    #: For CONCAT.
    separator: str = ""
    #: For CONSTANT.
    constant: Any = None
    #: For CONCAT: whether a missing input is an error (default) or is skipped.
    require_all_inputs: bool = True
    target_type: FieldDataType | None = None

    def __post_init__(self) -> None:
        from erp_pipeline.transformation.errors import (
            TransformationConfigurationError,
        )

        if self.operation is ComputedOperation.CONSTANT:
            if self.sources:
                raise TransformationConfigurationError(
                    f"Computed field {self.target_field!r} is a constant and "
                    "must declare no sources."
                )
        elif not self.sources:
            raise TransformationConfigurationError(
                f"Computed field {self.target_field!r} uses "
                f"{self.operation.value!r} and must declare at least one source."
            )

    def fingerprint(self) -> str:
        return (
            f"{self.target_field}:{self.operation.value}"
            f"({','.join(self.sources)};sep={self.separator!r};"
            f"all={int(self.require_all_inputs)})"
        )


# ============================================================
# Validation configuration (Steps 26-30, 73)
# ============================================================

@dataclass(frozen=True)
class FieldConstraint:
    """Declared, versionable constraints on one canonical target field.

    Supplemental configuration, not a canonical contract. The canonical model
    remains the authority on ``required`` and ``data_type``; this adds only the
    constraints Phase 1 has no vocabulary for.

    Every constraint is optional, and an absent constraint is NOT checked -
    the engine never invents a business rule (Step 27).
    """

    target_field: str
    allowed_values: tuple[Any, ...] | None = None
    min_value: Any | None = None
    max_value: Any | None = None
    min_length: int | None = None
    max_length: int | None = None
    #: Anchored regular expression the value's string form must match.
    pattern: str | None = None
    #: Overrides the canonical model's nullability for this field only.
    nullable: bool | None = None
    #: Name of a reference set this value must exist in (Step 30).
    reference_set: str | None = None

    def __post_init__(self) -> None:
        from erp_pipeline.transformation.errors import (
            TransformationConfigurationError,
        )

        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise TransformationConfigurationError(
                    f"FieldConstraint for {self.target_field!r} has an invalid "
                    f"pattern: {exc}."
                ) from exc

        if (
            self.min_value is not None
            and self.max_value is not None
            and _comparable(self.min_value, self.max_value)
            and self.min_value > self.max_value
        ):
            raise TransformationConfigurationError(
                f"FieldConstraint for {self.target_field!r} has min_value > "
                "max_value, which nothing can satisfy."
            )

        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise TransformationConfigurationError(
                f"FieldConstraint for {self.target_field!r} has min_length > "
                "max_length, which nothing can satisfy."
            )

    def fingerprint(self) -> str:
        return (
            f"{self.target_field}:av={self.allowed_values},"
            f"rng=({self.min_value},{self.max_value}),"
            f"len=({self.min_length},{self.max_length}),"
            f"pat={self.pattern},null={self.nullable},ref={self.reference_set}"
        )


def _comparable(left: Any, right: Any) -> bool:
    """Whether two constraint bounds can be ordered against each other."""
    numeric = (int, float, Decimal)
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, numeric) and isinstance(right, numeric):
        return True
    return type(left) is type(right) and isinstance(left, (str, date, datetime))


@dataclass(frozen=True)
class ValidationProfile:
    """A versioned set of declared constraints (Step 73).

    Business rules live in one reviewable object rather than scattered through
    ``validator.py`` as unexplained constants. ``version`` travels into the run
    metadata so a result is reproducible against the rules that produced it.
    """

    version: str = "1.0"
    constraints: tuple[FieldConstraint, ...] = ()
    #: Canonical fields whose combined value identifies a record for duplicate
    #: detection. Empty means duplicate detection is OFF - it is never inferred
    #: (Step 29).
    duplicate_key_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for constraint in self.constraints:
            if constraint.target_field in seen:
                from erp_pipeline.transformation.errors import (
                    TransformationConfigurationError,
                )

                raise TransformationConfigurationError(
                    f"ValidationProfile declares two constraints for "
                    f"{constraint.target_field!r}."
                )
            seen.add(constraint.target_field)

    def constraint_for(self, target_field: str) -> FieldConstraint | None:
        for constraint in self.constraints:
            if constraint.target_field == target_field:
                return constraint
        return None

    def fingerprint(self) -> str:
        return (
            f"validation@{self.version}/"
            + ";".join(sorted(c.fingerprint() for c in self.constraints))
            + f"/dup={list(self.duplicate_key_fields)}"
        )


# ============================================================
# Run policy (Steps 29, 36, 38)
# ============================================================

class FailurePolicy(str, Enum):
    """What a batch does when records fail."""

    #: Keep going, reject bad records, collect every issue. The right default
    #: for ERP migration: stopping the whole load because record 12 has a bad
    #: date hides the other 40 problems a reviewer needs to see at once.
    CONTINUE = "continue"
    #: Stop as soon as a configured threshold or a critical issue is hit.
    FAIL_FAST = "fail_fast"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class DuplicatePolicy(str, Enum):
    """What to do with a repeated canonical key inside one run (Step 29)."""

    REJECT = "reject"
    SKIP = "skip"
    WARN = "warn"
    ALLOW = "allow"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class QualityThresholds:
    """Run-level quality limits (Steps 36, 37).

    EVERY NUMERIC LIMIT DEFAULTS TO ``None`` - not enforced. That is a
    deliberate refusal to invent a number: there is no defensible universal
    "5% failures is acceptable" for ERP migration, and shipping one as a
    default would dress an arbitrary choice up as a standard.

    The single enabled default is ``stop_on_critical_issue``, because a
    CRITICAL finding means the engine itself is in trouble, not the data.

    These are OPERATIONAL defaults. A research run should set them explicitly.
    """

    max_failed_records: int | None = None
    max_failure_ratio: float | None = None
    max_error_issues: int | None = None
    max_warning_issues: int | None = None
    max_duplicate_ratio: float | None = None
    minimum_success_ratio: float | None = None
    stop_on_critical_issue: bool = True

    def __post_init__(self) -> None:
        from erp_pipeline.transformation.errors import (
            TransformationConfigurationError,
        )

        for name in ("max_failure_ratio", "max_duplicate_ratio",
                     "minimum_success_ratio"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= value <= 1.0:
                raise TransformationConfigurationError(
                    f"QualityThresholds.{name} must be a ratio in [0, 1], got "
                    f"{value}."
                )

        for name in ("max_failed_records", "max_error_issues",
                     "max_warning_issues"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise TransformationConfigurationError(
                    f"QualityThresholds.{name} must not be negative, got {value}."
                )

    @property
    def is_enabled(self) -> bool:
        return any(
            value is not None
            for value in (
                self.max_failed_records,
                self.max_failure_ratio,
                self.max_error_issues,
                self.max_warning_issues,
                self.max_duplicate_ratio,
                self.minimum_success_ratio,
            )
        )

    def fingerprint(self) -> str:
        return (
            f"thr(f={self.max_failed_records},fr={self.max_failure_ratio},"
            f"e={self.max_error_issues},w={self.max_warning_issues},"
            f"dr={self.max_duplicate_ratio},sr={self.minimum_success_ratio},"
            f"crit={int(self.stop_on_critical_issue)})"
        )


#: Mapping states whose instructions the engine will execute (Step 5).
#: AUTO_ACCEPTED is the engine's own confident selection; APPROVED is a human
#: decision. SUGGESTED, REVIEW_REQUIRED and REJECTED are explicitly excluded:
#: nobody has decided them, and executing an undecided mapping would silently
#: turn a proposal into production data.
DEFAULT_EXECUTABLE_STATUSES: frozenset[MappingStatus] = frozenset(
    {MappingStatus.AUTO_ACCEPTED, MappingStatus.APPROVED}
)


@dataclass(frozen=True)
class TransformationOptions:
    """Everything that can change what a transformation produces.

    The whole object contributes to ``fingerprint()``, which is recorded on the
    run, so a canonical record can always be traced to the exact configuration
    that produced it (Step 74).
    """

    null_policy: NullPolicy = field(default_factory=NullPolicy)
    boolean_policy: BooleanPolicy = field(default_factory=BooleanPolicy)
    number_policy: NumberPolicy = field(default_factory=NumberPolicy)
    date_policy: DatePolicy = field(default_factory=DatePolicy)
    string_policy: StringPolicy = field(default_factory=StringPolicy)
    normalization: NormalizationPolicy = field(default_factory=NormalizationPolicy)
    validation: ValidationProfile = field(default_factory=ValidationProfile)
    computed_fields: tuple[ComputedField, ...] = ()
    thresholds: QualityThresholds = field(default_factory=QualityThresholds)
    failure_policy: FailurePolicy = FailurePolicy.CONTINUE
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.REJECT
    unknown_type_policy: UnknownTypePolicy = UnknownTypePolicy.PASS_THROUGH
    executable_statuses: frozenset[MappingStatus] = DEFAULT_EXECUTABLE_STATUSES
    #: Declared defaults per canonical target field, applied ONLY when the
    #: source field is missing or null - never after a conversion failure
    #: (Step 15).
    defaults: Mapping[str, Any] = field(default_factory=dict)
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    #: Off by default. When on, diagnostics may quote a bounded, redacted
    #: excerpt of an offending value. Even then the excerpt goes through
    #: ``summarize_value``, so it is a shape description, not a copy.
    include_value_diagnostics: bool = False
    #: Whether a rejected record keeps a reference to its source record in
    #: memory for remediation. Never serialized regardless (Step 34).
    retain_source_on_rejection: bool = True

    def __post_init__(self) -> None:
        from erp_pipeline.transformation.errors import (
            TransformationConfigurationError,
        )

        seen: set[str] = set()
        for computed in self.computed_fields:
            if computed.target_field in seen:
                raise TransformationConfigurationError(
                    f"Two computed fields target {computed.target_field!r}."
                )
            seen.add(computed.target_field)

        if not self.executable_statuses:
            raise TransformationConfigurationError(
                "executable_statuses is empty, so no mapping could ever run."
            )

    def fingerprint(self) -> str:
        """Stable description of every setting that affects output."""
        return "/".join(
            (
                f"engine@{TRANSFORMATION_ENGINE_VERSION}",
                self.null_policy.fingerprint(),
                self.boolean_policy.fingerprint(),
                self.number_policy.fingerprint(),
                self.date_policy.fingerprint(),
                self.string_policy.fingerprint(),
                self.normalization.fingerprint(),
                self.validation.fingerprint(),
                "computed(" + ";".join(
                    c.fingerprint() for c in self.computed_fields
                ) + ")",
                self.thresholds.fingerprint(),
                f"fail={self.failure_policy.value}",
                f"dup={self.duplicate_policy.value}",
                f"unknown={self.unknown_type_policy.value}",
                f"exec={sorted(s.value for s in self.executable_statuses)}",
                f"defaults={sorted(self.defaults)}",
            )
        )


DEFAULT_OPTIONS = TransformationOptions()


# ============================================================
# Per-record results (Steps 34, 35, 41, 45)
# ============================================================

class RecordOutcome(str, Enum):
    """Exactly one of these is true of every source record (Step 41)."""

    TRANSFORMED = "transformed"
    REJECTED = "rejected"
    SKIPPED = "skipped"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class SkipReason(str, Enum):
    """Why a record was skipped rather than failed (Step 35)."""

    DUPLICATE = "duplicate"
    FILTERED = "filtered"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class RejectedRecord:
    """A record that could not become a valid canonical record (Step 34).

    ``reasons`` is guaranteed non-empty by ``__post_init__``: "failed for
    unknown reason" is not an acceptable outcome, and making it structurally
    impossible is better than testing for its absence (Step 42).

    ``source_record`` may be retained IN MEMORY for remediation, but
    ``to_dict()`` never includes it. Serialization is privacy-safe by default.
    """

    record_reference: str
    reasons: tuple[str, ...]
    issues: tuple[Any, ...] = ()          # DataQualityIssue
    source_entity: str | None = None
    ordinal: int | None = None
    mapping_id: str | None = None
    #: Excluded from every serialization. Present only when the caller asked
    #: for it via ``retain_source_on_rejection``.
    source_record: SourceRecord | None = None

    def __post_init__(self) -> None:
        if not self.reasons:
            from erp_pipeline.transformation.errors import TransformationError

            raise TransformationError(
                "A rejected record must state at least one reason. An "
                "unexplained rejection is not actionable."
            )

    def to_dict(self, include_source_values: bool = False) -> dict[str, Any]:
        """Privacy-safe by default (Step 34).

        ``include_source_values`` is an explicit, auditable opt-in for a
        remediation tool. It is never used by the run summary.
        """
        payload: dict[str, Any] = {
            "record_reference": self.record_reference,
            "reasons": list(self.reasons),
            "source_entity": self.source_entity,
            "ordinal": self.ordinal,
            "mapping_id": self.mapping_id,
            "issue_codes": [issue.code for issue in self.issues],
            "issue_count": len(self.issues),
        }

        if include_source_values and self.source_record is not None:
            payload["source_values"] = dict(self.source_record.values)

        return payload


@dataclass(frozen=True)
class SkippedRecord:
    """A record deliberately not transformed - not a failure (Step 35)."""

    record_reference: str
    reason: SkipReason
    detail: str | None = None
    source_entity: str | None = None
    ordinal: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_reference": self.record_reference,
            "reason": self.reason.value,
            "detail": self.detail,
            "source_entity": self.source_entity,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True)
class RecordTransformationResult:
    """The outcome of transforming exactly one source record.

    A record is emitted as ``TRANSFORMED`` only when the whole candidate record
    validated (Step 45). There is no partially-successful state: a record with
    a blocking issue is rejected, and its partial normalized data is discarded
    rather than published looking healthy.
    """

    outcome: RecordOutcome
    record: Any | None = None             # CanonicalRecord
    issues: tuple[Any, ...] = ()          # DataQualityIssue
    rejected: RejectedRecord | None = None
    skipped: SkippedRecord | None = None

    @property
    def is_transformed(self) -> bool:
        return self.outcome is RecordOutcome.TRANSFORMED

    @property
    def blocking_issues(self) -> tuple[Any, ...]:
        return tuple(issue for issue in self.issues if issue.is_blocking)

    def issue_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "record_id": self.record.record_id if self.record is not None else None,
            "issue_codes": list(self.issue_codes()),
            "rejected": self.rejected.to_dict() if self.rejected else None,
            "skipped": self.skipped.to_dict() if self.skipped else None,
        }


# ============================================================
# Run summary (Steps 39, 68)
# ============================================================

@dataclass(frozen=True)
class TransformationRunSummary:
    """Everything one batch produced, around the frozen ``TransformationRun``.

    ``run`` IS the Phase 1 contract, populated and returned unmodified. This
    wrapper adds the ratios and the collections the contract has no field for,
    rather than changing the contract to hold them.

    DENOMINATORS (Step 68). Every ratio divides by ``records_read`` - the
    number of records the engine actually pulled from the iterable, which under
    FAIL_FAST is fewer than the source contains. An empty batch yields 0.0 for
    every ratio rather than raising (Step 70).
    """

    run: Any                              # TransformationRun
    successful_records: tuple[Any, ...] = ()      # CanonicalRecord
    rejected_records: tuple[RejectedRecord, ...] = ()
    skipped_records: tuple[SkippedRecord, ...] = ()
    issues: tuple[Any, ...] = ()          # DataQualityIssue
    threshold_exceeded: bool = False
    threshold_reasons: tuple[str, ...] = ()
    stopped_early: bool = False
    duration_seconds: float = 0.0

    # -- counters, read straight off the frozen contract --

    @property
    def records_read(self) -> int:
        return self.run.records_read

    @property
    def records_transformed(self) -> int:
        return self.run.records_transformed

    @property
    def records_failed(self) -> int:
        return self.run.records_failed

    @property
    def records_skipped(self) -> int:
        return self.run.records_skipped

    @property
    def warning_count(self) -> int:
        return self.run.warning_count

    @property
    def error_count(self) -> int:
        return self.run.error_count

    @property
    def critical_count(self) -> int:
        return sum(
            1 for issue in self.issues
            if issue.severity is QualitySeverity.CRITICAL
        )

    @property
    def quality_issue_count(self) -> int:
        return len(self.issues)

    # -- ratios --

    def _ratio(self, numerator: int) -> float:
        if self.records_read <= 0:
            return 0.0
        return round(numerator / self.records_read, 6)

    @property
    def success_ratio(self) -> float:
        return self._ratio(self.records_transformed)

    @property
    def failure_ratio(self) -> float:
        return self._ratio(self.records_failed)

    @property
    def skip_ratio(self) -> float:
        return self._ratio(self.records_skipped)

    @property
    def counters_balance(self) -> bool:
        """The Step 41 invariant: read == transformed + failed + skipped."""
        return self.records_read == (
            self.records_transformed + self.records_failed + self.records_skipped
        )

    def issue_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        """Privacy-safe summary: counts, codes and references only."""
        return {
            "run_id": self.run.run_id,
            "status": self.run.status.value,
            "mapping_id": self.run.mapping_id,
            "records_read": self.records_read,
            "records_transformed": self.records_transformed,
            "records_failed": self.records_failed,
            "records_skipped": self.records_skipped,
            "success_ratio": self.success_ratio,
            "failure_ratio": self.failure_ratio,
            "skip_ratio": self.skip_ratio,
            "warning_count": self.warning_count,
            "error_count": self.error_count,
            "critical_count": self.critical_count,
            "quality_issue_count": self.quality_issue_count,
            "duration_seconds": self.duration_seconds,
            "threshold_exceeded": self.threshold_exceeded,
            "threshold_reasons": list(self.threshold_reasons),
            "stopped_early": self.stopped_early,
            "counters_balance": self.counters_balance,
            "rejected_records": [item.to_dict() for item in self.rejected_records],
            "skipped_records": [item.to_dict() for item in self.skipped_records],
            "issue_codes": list(self.issue_codes()),
        }


def deterministic_suffix(payload: Any) -> str:
    """A short, stable hash suffix for generated identifiers."""
    return hash_json_payload(payload)[:12]


__all__ = [
    "TRANSFORMATION_ENGINE_VERSION",
    "IssueCode",
    "ExtractionOutcome",
    "SourceRecord",
    "UnknownTypePolicy",
    "NullPolicy",
    "BooleanPolicy",
    "NumberPolicy",
    "DatePolicy",
    "StringPolicy",
    "CaseNormalization",
    "NormalizationPolicy",
    "ComputedOperation",
    "ComputedField",
    "FieldConstraint",
    "ValidationProfile",
    "FailurePolicy",
    "DuplicatePolicy",
    "QualityThresholds",
    "DEFAULT_EXECUTABLE_STATUSES",
    "TransformationOptions",
    "DEFAULT_OPTIONS",
    "RecordOutcome",
    "SkipReason",
    "RejectedRecord",
    "SkippedRecord",
    "RecordTransformationResult",
    "TransformationRunSummary",
    "deterministic_suffix",
]
