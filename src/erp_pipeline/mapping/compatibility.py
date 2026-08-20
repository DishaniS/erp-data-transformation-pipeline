"""Datatype compatibility between a source field and a canonical target.

A deterministic matrix over the existing ``FieldDataType`` - no new type
vocabulary is introduced.

What "compatible" means here
----------------------------
Not "are these the same type?" but "could a value of the source type be
represented as the target type WITHOUT inventing or destroying information?"
That framing is what makes the asymmetry correct:

    INTEGER -> DECIMAL    lossless widening          compatible
    DECIMAL -> INTEGER    silently truncates         lossy, scored down
    DATE    -> DATETIME   midnight is implied        lossless-ish
    DATETIME-> DATE       discards the time          lossy
    STRING  -> INTEGER    may or may not parse       needs a declared rule
    OBJECT  -> DECIMAL    no meaning at all          incompatible

Compatibility is one weighted component of the mapping score, but
``INCOMPATIBLE`` additionally acts as a VETO: no candidate with an
incompatible type is ever auto-selected, however well its name matches
(Step 12). A strong name match with an impossible type is exactly the case
where an automated engine should defer to a human.

UNKNOWN
-------
A source type of ``UNKNOWN`` is common and honest: MongoDB fields with mixed
types, CSV columns of only empty values, Postman query parameters. It is
neither compatible nor incompatible - it is unproven, so it scores low and
never auto-selects, rather than being optimistically accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from erp_pipeline.schemas.enums import FieldDataType


class TypeCompatibility(str, Enum):
    """How well a source type fits a canonical target type."""

    #: Same type, or an exact semantic equivalent.
    EXACT = "exact"
    #: Lossless conversion in the source -> target direction.
    WIDENING = "widening"
    #: Convertible, but a declared transformation rule would be needed and
    #: information may be lost or a parse may fail.
    LOSSY = "lossy"
    #: The source type carries no usable type evidence.
    UNKNOWN = "unknown"
    #: No meaningful conversion exists. Vetoes auto-selection.
    INCOMPATIBLE = "incompatible"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def score(self) -> float:
        """Contribution to the weighted mapping score, in [0, 1]."""
        return _COMPATIBILITY_SCORES[self]

    @property
    def blocks_auto_selection(self) -> bool:
        """Whether this compatibility forbids automatic selection.

        Only INCOMPATIBLE blocks outright. LOSSY and UNKNOWN are permitted to
        be auto-selected if the rest of the evidence is strong enough, because
        refusing them entirely would leave most CSV and Postman fields
        permanently unmapped - but they carry a much lower score, so in
        practice they rarely clear the high threshold on their own.
        """
        return self is TypeCompatibility.INCOMPATIBLE


_COMPATIBILITY_SCORES: Mapping[TypeCompatibility, float] = {
    TypeCompatibility.EXACT: 1.0,
    TypeCompatibility.WIDENING: 0.85,
    TypeCompatibility.LOSSY: 0.45,
    TypeCompatibility.UNKNOWN: 0.30,
    TypeCompatibility.INCOMPATIBLE: 0.0,
}

_S = FieldDataType

#: Explicit (source, target) pairs that are not simple equality. Anything not
#: listed and not equal falls through to the rules in ``compare_types``.
_MATRIX: Mapping[tuple[FieldDataType, FieldDataType], TypeCompatibility] = {
    # --- numeric ---
    (_S.INTEGER, _S.DECIMAL): TypeCompatibility.WIDENING,
    (_S.DECIMAL, _S.INTEGER): TypeCompatibility.LOSSY,
    (_S.INTEGER, _S.STRING): TypeCompatibility.LOSSY,
    (_S.DECIMAL, _S.STRING): TypeCompatibility.LOSSY,
    (_S.BOOLEAN, _S.STRING): TypeCompatibility.LOSSY,
    (_S.BOOLEAN, _S.INTEGER): TypeCompatibility.LOSSY,
    # A string MIGHT parse to a number; a declared cast rule would be needed,
    # and it can fail at run time. Never silently treated as fine.
    (_S.STRING, _S.INTEGER): TypeCompatibility.LOSSY,
    (_S.STRING, _S.DECIMAL): TypeCompatibility.LOSSY,
    (_S.STRING, _S.BOOLEAN): TypeCompatibility.LOSSY,
    # --- temporal ---
    (_S.DATE, _S.DATETIME): TypeCompatibility.WIDENING,
    (_S.DATETIME, _S.DATE): TypeCompatibility.LOSSY,
    (_S.STRING, _S.DATE): TypeCompatibility.LOSSY,
    (_S.STRING, _S.DATETIME): TypeCompatibility.LOSSY,
    (_S.DATE, _S.STRING): TypeCompatibility.LOSSY,
    (_S.DATETIME, _S.STRING): TypeCompatibility.LOSSY,
    # A number as a date is a real pattern (epoch seconds) but always needs a
    # declared rule, and getting it wrong is silent.
    (_S.INTEGER, _S.DATETIME): TypeCompatibility.LOSSY,
    (_S.INTEGER, _S.DATE): TypeCompatibility.LOSSY,
    # --- binary ---
    (_S.BINARY, _S.STRING): TypeCompatibility.LOSSY,
    (_S.STRING, _S.BINARY): TypeCompatibility.LOSSY,
}

#: Structural types. A composite value has no scalar meaning and a scalar has
#: no composite meaning; mapping between them is a modelling error, not a
#: conversion. Handled as a rule rather than by enumerating every pair.
_STRUCTURAL = frozenset({FieldDataType.OBJECT, FieldDataType.ARRAY})


@dataclass(frozen=True)
class TypeComparison:
    """The result of comparing one source type with one target type."""

    source_type: FieldDataType
    target_type: FieldDataType
    compatibility: TypeCompatibility
    #: True when the source is an array and the target is not, or vice versa.
    cardinality_conflict: bool = False
    #: True when one side is a composite (object/array) and the other a scalar.
    structural_conflict: bool = False

    @property
    def score(self) -> float:
        return self.compatibility.score

    @property
    def blocks_auto_selection(self) -> bool:
        return self.compatibility.blocks_auto_selection

    def explain(self) -> str:
        """One human-readable sentence, safe to log - types only, no values."""
        arrow = f"{self.source_type.value} -> {self.target_type.value}"

        if self.compatibility is TypeCompatibility.EXACT:
            return f"type {arrow} is an exact match"
        if self.compatibility is TypeCompatibility.WIDENING:
            return f"type {arrow} widens without loss"
        if self.compatibility is TypeCompatibility.LOSSY:
            return (
                f"type {arrow} is convertible but needs a declared "
                "transformation and may lose information"
            )
        if self.compatibility is TypeCompatibility.UNKNOWN:
            return (
                f"type {arrow} cannot be judged: the source type is unknown"
            )

        if self.cardinality_conflict:
            return f"type {arrow} conflicts: one side is a collection"
        if self.structural_conflict:
            return f"type {arrow} conflicts: composite versus scalar"

        return f"type {arrow} has no meaningful conversion"

    def to_dict(self) -> dict[str, object]:
        return {
            "source_type": self.source_type.value,
            "target_type": self.target_type.value,
            "compatibility": self.compatibility.value,
            "score": self.score,
            "cardinality_conflict": self.cardinality_conflict,
            "structural_conflict": self.structural_conflict,
            "explanation": self.explain(),
        }


def compare_types(
    source_type: FieldDataType,
    target_type: FieldDataType,
    source_is_array: bool = False,
) -> TypeComparison:
    """Compare a source field's type with a canonical target's type.

    ``source_is_array`` is passed separately because ``SourceField`` records
    array-ness both in ``normalized_data_type == ARRAY`` and in the
    ``is_array`` flag, and a field can legitimately be flagged as an array
    while carrying the element's type.
    """
    effective_source = FieldDataType.ARRAY if source_is_array else source_type

    # A collection can only feed a collection.
    if (effective_source is FieldDataType.ARRAY) != (
        target_type is FieldDataType.ARRAY
    ):
        return TypeComparison(
            source_type=effective_source,
            target_type=target_type,
            compatibility=TypeCompatibility.INCOMPATIBLE,
            cardinality_conflict=True,
            structural_conflict=effective_source in _STRUCTURAL
            or target_type in _STRUCTURAL,
        )

    if effective_source is target_type:
        # UNKNOWN == UNKNOWN is still unproven, not an exact match.
        if effective_source is FieldDataType.UNKNOWN:
            return TypeComparison(
                source_type=effective_source,
                target_type=target_type,
                compatibility=TypeCompatibility.UNKNOWN,
            )
        return TypeComparison(
            source_type=effective_source,
            target_type=target_type,
            compatibility=TypeCompatibility.EXACT,
        )

    if effective_source is FieldDataType.UNKNOWN:
        return TypeComparison(
            source_type=effective_source,
            target_type=target_type,
            compatibility=TypeCompatibility.UNKNOWN,
        )

    # An unknown TARGET would mean the canonical model failed to declare a
    # type; treated as unproven rather than as an error, so one sloppy target
    # cannot break a whole mapping run.
    if target_type is FieldDataType.UNKNOWN:
        return TypeComparison(
            source_type=effective_source,
            target_type=target_type,
            compatibility=TypeCompatibility.UNKNOWN,
        )

    # Composite versus scalar, in either direction.
    if (effective_source in _STRUCTURAL) != (target_type in _STRUCTURAL):
        return TypeComparison(
            source_type=effective_source,
            target_type=target_type,
            compatibility=TypeCompatibility.INCOMPATIBLE,
            structural_conflict=True,
            cardinality_conflict=FieldDataType.ARRAY
            in (effective_source, target_type),
        )

    explicit = _MATRIX.get((effective_source, target_type))
    if explicit is not None:
        return TypeComparison(
            source_type=effective_source,
            target_type=target_type,
            compatibility=explicit,
        )

    return TypeComparison(
        source_type=effective_source,
        target_type=target_type,
        compatibility=TypeCompatibility.INCOMPATIBLE,
    )


def compatibility_matrix() -> dict[str, dict[str, str]]:
    """The full matrix, rendered for documentation and tests.

    Generated from ``compare_types`` rather than duplicated, so the published
    matrix can never disagree with the implementation.
    """
    types = [item for item in FieldDataType]

    return {
        source.value: {
            target.value: compare_types(source, target).compatibility.value
            for target in types
        }
        for source in types
    }


__all__ = [
    "TypeCompatibility",
    "TypeComparison",
    "compare_types",
    "compatibility_matrix",
]
