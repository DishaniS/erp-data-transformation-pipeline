"""Supplemental mapping models: options, candidates, evidence, coverage.

None of these replaces a Phase 1 contract. The engine's PERSISTED output is
the existing ``MappingProfile`` containing existing ``FieldMapping`` objects;
everything here is the reviewable working-out that produced them.

Why supplemental models are needed at all
-----------------------------------------
``FieldMapping`` records a decision - this source field becomes that target
field, with this confidence and this status. It has no room for the candidates
that were considered and rejected, the evidence behind each, or the margin
between the best two. Discarding that would make the engine unexplainable,
which is the one thing Phase 8 must not be. So the decision goes into the
frozen contract and the reasoning travels beside it.

``FieldMapping.metadata`` does carry a compact evidence summary, so a profile
loaded back from the catalog is still self-explaining without the supplemental
objects.

PRIVACY
-------
No model here can hold a business value, because the engine never reads one.
Every field is a name, a type, a score or a count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from erp_pipeline.mapping.compatibility import TypeComparison
from erp_pipeline.schemas.enums import FieldDataType, MappingStatus

#: Version of the scoring and selection behaviour. Recorded on every generated
#: profile: a mapping produced under different rules must be traceable, and
#: silently changing mapping semantics under a stable version would make stored
#: profiles impossible to reason about (Step 29).
MAPPING_ENGINE_VERSION = "1.0"


class ConfidenceLevel(str, Enum):
    """Confidence band of a mapping score.

    A new enum, added after checking the existing vocabulary: ``MappingStatus``
    describes where a mapping is in a REVIEW WORKFLOW (suggested, approved,
    rejected), which is a different question from how strong the evidence is.
    The two are related but not interchangeable - a HIGH-confidence candidate
    can still be ``REVIEW_REQUIRED`` because it is ambiguous.

    The bands are a reporting convenience over the numeric score, not a
    probability. See ``MappingScore``.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class FieldOutcome(str, Enum):
    """What the engine decided about one source field."""

    #: Auto-selected: strong enough, unambiguous, type-compatible.
    AUTO_SELECTED = "auto_selected"
    #: A human chose this target explicitly.
    MANUAL_OVERRIDE = "manual_override"
    #: Candidates exist but none met the bar for automatic selection.
    REVIEW_REQUIRED = "review_required"
    #: Two or more candidates are too close to choose between.
    AMBIGUOUS = "ambiguous"
    #: No acceptable candidate at all.
    UNMAPPED = "unmapped"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def is_selected(self) -> bool:
        return self in (FieldOutcome.AUTO_SELECTED, FieldOutcome.MANUAL_OVERRIDE)


# ============================================================
# Scoring configuration (Steps 17, 18, 19)
# ============================================================

@dataclass(frozen=True)
class ScoringWeights:
    """Weights of the four evidence components. Must sum to 1.0.

    The split reflects what actually distinguishes a correct ERP mapping:

    ``name`` (0.50)
        The dominant signal. A field called ``customer_id`` almost always is
        the customer id, and an explicit alias is a human's own statement that
        two names mean the same thing.

    ``type`` (0.20)
        Corroborating, not deciding. Half the fields in any ERP are strings,
        so a type match is weak positive evidence - but a type CONFLICT is
        strong negative evidence, which is handled by the veto rather than by
        this weight.

    ``entity`` (0.20)
        What keeps ``supplier.email`` from winning against ``customer.email``.
        Weighted equally with type because in practice it disambiguates far
        more real cases.

    ``path`` (0.10)
        Refines within an entity - ``customer.contact.email`` over a flat
        ``email`` - and is worth less because three of the six source
        technologies produce flat fields with no path at all.
    """

    name: float = 0.50
    type: float = 0.20
    entity: float = 0.20
    path: float = 0.10

    def __post_init__(self) -> None:
        total = self.name + self.type + self.entity + self.path
        if abs(total - 1.0) > 1e-9:
            from erp_pipeline.mapping.errors import MappingConfigurationError

            raise MappingConfigurationError(
                f"ScoringWeights must sum to 1.0, got {total}. A score built "
                "from weights that do not sum to one is not comparable against "
                "the configured thresholds."
            )

    def to_dict(self) -> dict[str, float]:
        return {
            "name": self.name,
            "type": self.type,
            "entity": self.entity,
            "path": self.path,
        }

    def fingerprint(self) -> str:
        return f"w({self.name},{self.type},{self.entity},{self.path})"


DEFAULT_WEIGHTS = ScoringWeights()


@dataclass(frozen=True)
class MappingOptions:
    """Engine configuration. Every threshold is explicit and configurable.

    No magic constants live in the scoring or selection code; they all arrive
    here, and the whole object contributes to ``config_fingerprint`` so a
    profile records the rules that produced it.
    """

    #: Score at or above which a candidate may be auto-selected.
    high_threshold: float = 0.75
    #: Score at or above which a candidate is worth showing to a reviewer.
    medium_threshold: float = 0.50
    #: The best candidate must beat the second by at least this much, or the
    #: field is reported AMBIGUOUS rather than being silently decided.
    ambiguity_margin: float = 0.05
    #: Candidates scoring below this are not returned at all.
    minimum_candidate_score: float = 0.20
    #: How many candidates to keep per source field.
    max_candidates_per_field: int = 5

    #: Auto-select only when the type is genuinely compatible. Turning this off
    #: is possible but is a deliberate loosening, not a default.
    require_type_compatibility_for_auto: bool = True
    #: Refuse to auto-select two source fields onto one target (Step 22).
    detect_target_collisions: bool = True
    #: Emit at most one target per source field automatically (Step 23).
    single_target_per_source_field: bool = True

    weights: ScoringWeights = field(default_factory=ScoringWeights)

    def __post_init__(self) -> None:
        from erp_pipeline.mapping.errors import MappingConfigurationError

        if not 0.0 <= self.medium_threshold <= self.high_threshold <= 1.0:
            raise MappingConfigurationError(
                "Thresholds must satisfy 0 <= medium <= high <= 1, got "
                f"medium={self.medium_threshold}, high={self.high_threshold}."
            )
        if not 0.0 <= self.ambiguity_margin <= 1.0:
            raise MappingConfigurationError(
                f"ambiguity_margin must be in [0, 1], got {self.ambiguity_margin}."
            )
        if self.max_candidates_per_field < 1:
            raise MappingConfigurationError(
                "max_candidates_per_field must be at least 1."
            )

    def confidence_level(self, score: float) -> ConfidenceLevel:
        if score >= self.high_threshold:
            return ConfidenceLevel.HIGH
        if score >= self.medium_threshold:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW

    def fingerprint(self) -> str:
        """Stable description of the selection rules, for profile identity."""
        return (
            f"h={self.high_threshold},m={self.medium_threshold},"
            f"a={self.ambiguity_margin},min={self.minimum_candidate_score},"
            f"typeveto={int(self.require_type_compatibility_for_auto)},"
            f"{self.weights.fingerprint()}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "high_threshold": self.high_threshold,
            "medium_threshold": self.medium_threshold,
            "ambiguity_margin": self.ambiguity_margin,
            "minimum_candidate_score": self.minimum_candidate_score,
            "max_candidates_per_field": self.max_candidates_per_field,
            "require_type_compatibility_for_auto": (
                self.require_type_compatibility_for_auto
            ),
            "detect_target_collisions": self.detect_target_collisions,
            "single_target_per_source_field": self.single_target_per_source_field,
            "weights": self.weights.to_dict(),
        }


DEFAULT_OPTIONS = MappingOptions()


# ============================================================
# Evidence and candidates (Steps 16, 17)
# ============================================================

class NameMatchKind(str, Enum):
    """How a source name matched a canonical name. Ordered strongest first."""

    #: The raw names are identical.
    EXACT = "exact"
    #: Identical after normalization (``customerId`` vs ``customer_id``).
    NORMALIZED_EXACT = "normalized_exact"
    #: The canonical field explicitly declares this spelling as an alias.
    EXPLICIT_ALIAS = "explicit_alias"
    #: Tokens overlap without an exact or declared match.
    TOKEN_OVERLAP = "token_overlap"
    #: No name evidence at all.
    NONE = "none"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class NameEvidence:
    """Why (or whether) two names were considered the same concept."""

    kind: NameMatchKind
    score: float
    source_tokens: tuple[str, ...] = ()
    target_tokens: tuple[str, ...] = ()
    shared_tokens: tuple[str, ...] = ()
    matched_alias: str | None = None

    def explain(self) -> str:
        if self.kind is NameMatchKind.EXACT:
            return "source and canonical names are identical"
        if self.kind is NameMatchKind.NORMALIZED_EXACT:
            return (
                "names are identical after normalization "
                f"({'_'.join(self.source_tokens)})"
            )
        if self.kind is NameMatchKind.EXPLICIT_ALIAS:
            return (
                f"the canonical field declares {self.matched_alias!r} as an "
                "explicit alias"
            )
        if self.kind is NameMatchKind.TOKEN_OVERLAP:
            return (
                f"names share token(s) {list(self.shared_tokens)} "
                f"(similarity {self.score})"
            )
        return "names have nothing in common"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "score": self.score,
            "source_tokens": list(self.source_tokens),
            "target_tokens": list(self.target_tokens),
            "shared_tokens": list(self.shared_tokens),
            "matched_alias": self.matched_alias,
            "explanation": self.explain(),
        }


@dataclass(frozen=True)
class ContextEvidence:
    """How well the surrounding context agrees - entity, or nested path."""

    score: float
    source_context: str
    target_context: str
    shared_tokens: tuple[str, ...] = ()
    matched_alias: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "source_context": self.source_context,
            "target_context": self.target_context,
            "shared_tokens": list(self.shared_tokens),
            "matched_alias": self.matched_alias,
        }


@dataclass(frozen=True)
class MappingEvidence:
    """The complete, inspectable reasoning behind one candidate.

    Every component is populated for every candidate - there is no code path
    that produces a score without also producing the evidence for it, which is
    what the explainability tests in
    ``tests/erp_pipeline/mapping/test_mapping_boundary.py`` assert.
    """

    name: NameEvidence
    type_comparison: TypeComparison
    entity: ContextEvidence
    path: ContextEvidence

    @property
    def has_type_conflict(self) -> bool:
        return self.type_comparison.blocks_auto_selection

    def to_dict(self) -> dict[str, Any]:
        return {
            "name_match": self.name.to_dict(),
            "type_compatibility": self.type_comparison.to_dict(),
            "entity_context": self.entity.to_dict(),
            "nested_path_context": self.path.to_dict(),
        }

    def summary(self) -> dict[str, Any]:
        """Compact form stored in ``FieldMapping.metadata``.

        Small enough to live in a catalog row, complete enough that a profile
        reloaded from the database still explains itself.
        """
        return {
            "name_match": self.name.kind.value,
            "name_score": self.name.score,
            "matched_alias": self.name.matched_alias,
            "type_compatibility": self.type_comparison.compatibility.value,
            "entity_context_score": self.entity.score,
            "path_context_score": self.path.score,
        }


@dataclass(frozen=True)
class MappingScore:
    """The weighted score and its component contributions.

    Deliberately NOT called a probability. It is a bounded, deterministic
    ranking score produced by documented weights; nothing here is calibrated
    against observed correctness, and presenting it as "94% likely correct"
    would be a claim this phase has not earned.
    """

    total: float
    name_component: float
    type_component: float
    entity_component: float
    path_component: float
    weights: ScoringWeights

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "components": {
                "name": self.name_component,
                "type": self.type_component,
                "entity": self.entity_component,
                "path": self.path_component,
            },
            "weights": self.weights.to_dict(),
        }


@dataclass(frozen=True)
class MappingCandidate:
    """One possible canonical target for one source field."""

    source_field: str            # full source path, e.g. "customer.contact.email"
    source_entity: str
    target_entity_type: str
    target_field: str            # bare canonical name, e.g. "email"
    score: MappingScore
    evidence: MappingEvidence
    confidence: ConfidenceLevel
    source_data_type: FieldDataType
    target_data_type: FieldDataType

    @property
    def qualified_target(self) -> str:
        return f"{self.target_entity_type}.{self.target_field}"

    @property
    def has_type_conflict(self) -> bool:
        return self.evidence.has_type_conflict

    def explain(self) -> str:
        """A single human-readable sentence for a reviewer."""
        return (
            f"{self.source_field} -> {self.qualified_target} "
            f"(score {self.score.total}, {self.confidence.value}): "
            f"{self.evidence.name.explain()}; "
            f"{self.evidence.type_comparison.explain()}; "
            f"entity context {self.evidence.entity.score}; "
            f"path context {self.evidence.path.score}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.source_field,
            "source_entity": self.source_entity,
            "target": self.qualified_target,
            "target_entity_type": self.target_entity_type,
            "target_field": self.target_field,
            "score": self.score.to_dict(),
            "confidence": self.confidence.value,
            "source_data_type": self.source_data_type.value,
            "target_data_type": self.target_data_type.value,
            "type_conflict": self.has_type_conflict,
            "evidence": self.evidence.to_dict(),
            "explanation": self.explain(),
        }


@dataclass(frozen=True)
class MappingAmbiguity:
    """Two or more candidates too close to choose between.

    Recorded rather than resolved. Picking one of ``customer.name`` (0.82) and
    ``supplier.name`` (0.81) automatically would be a coin toss dressed up as a
    decision.
    """

    source_field: str
    best_target: str
    runner_up_target: str
    best_score: float
    runner_up_score: float
    margin: float
    required_margin: float

    def explain(self) -> str:
        return (
            f"{self.source_field}: {self.best_target} ({self.best_score}) and "
            f"{self.runner_up_target} ({self.runner_up_score}) differ by "
            f"{self.margin}, below the required margin of "
            f"{self.required_margin}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.source_field,
            "best_target": self.best_target,
            "runner_up_target": self.runner_up_target,
            "best_score": self.best_score,
            "runner_up_score": self.runner_up_score,
            "margin": self.margin,
            "required_margin": self.required_margin,
            "explanation": self.explain(),
        }


@dataclass(frozen=True)
class TargetCollision:
    """Several source fields competing for one canonical target (Step 22)."""

    target: str
    source_fields: tuple[str, ...]
    kept_source_field: str | None = None

    def explain(self) -> str:
        return (
            f"{len(self.source_fields)} source fields "
            f"{list(self.source_fields)} were all selected for {self.target}; "
            + (
                f"kept {self.kept_source_field!r} and flagged the rest for review"
                if self.kept_source_field
                else "all were flagged for review"
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "source_fields": list(self.source_fields),
            "kept_source_field": self.kept_source_field,
            "explanation": self.explain(),
        }


@dataclass(frozen=True)
class FieldDecision:
    """What the engine concluded about one source field, with its workings."""

    source_field: str
    source_entity: str
    source_data_type: FieldDataType
    outcome: FieldOutcome
    candidates: tuple[MappingCandidate, ...] = ()
    selected: MappingCandidate | None = None
    ambiguity: MappingAmbiguity | None = None
    #: Why the field was not auto-selected, when it was not.
    reason: str | None = None

    @property
    def confidence(self) -> ConfidenceLevel | None:
        return self.selected.confidence if self.selected else None

    @property
    def best_candidate(self) -> MappingCandidate | None:
        return self.candidates[0] if self.candidates else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.source_field,
            "source_entity": self.source_entity,
            "source_data_type": self.source_data_type.value,
            "outcome": self.outcome.value,
            "selected_target": (
                self.selected.qualified_target if self.selected else None
            ),
            "reason": self.reason,
            "ambiguity": self.ambiguity.to_dict() if self.ambiguity else None,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


# ============================================================
# Coverage (Steps 24, 25, 26)
# ============================================================

@dataclass(frozen=True)
class EntityCoverage:
    """Mapping coverage for one source entity."""

    source_entity: str
    target_entity_type: str | None
    total_fields: int
    mapped_fields: int
    ambiguous_fields: int
    unmapped_fields: int
    review_required_fields: int
    #: Canonical required fields of the target that nothing maps onto.
    missing_required_targets: tuple[str, ...] = ()

    @property
    def coverage_ratio(self) -> float:
        if self.total_fields <= 0:
            return 0.0
        return round(self.mapped_fields / self.total_fields, 6)

    @property
    def required_target_coverage_complete(self) -> bool:
        return not self.missing_required_targets

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_entity": self.source_entity,
            "target_entity_type": self.target_entity_type,
            "total_fields": self.total_fields,
            "mapped_fields": self.mapped_fields,
            "ambiguous_fields": self.ambiguous_fields,
            "unmapped_fields": self.unmapped_fields,
            "review_required_fields": self.review_required_fields,
            "coverage_ratio": self.coverage_ratio,
            "missing_required_targets": list(self.missing_required_targets),
            "required_target_coverage_complete": (
                self.required_target_coverage_complete
            ),
        }


@dataclass(frozen=True)
class MappingCoverage:
    """Coverage across a whole schema.

    This is MAPPING coverage - how much of the source structure found a
    canonical home - and explicitly not data quality, which nothing in Phase 8
    is in a position to judge.
    """

    total_fields: int
    mapped_fields: int
    ambiguous_fields: int
    unmapped_fields: int
    review_required_fields: int
    entities: tuple[EntityCoverage, ...] = ()

    @property
    def coverage_ratio(self) -> float:
        if self.total_fields <= 0:
            return 0.0
        return round(self.mapped_fields / self.total_fields, 6)

    @property
    def ambiguity_rate(self) -> float:
        if self.total_fields <= 0:
            return 0.0
        return round(self.ambiguous_fields / self.total_fields, 6)

    @property
    def unmapped_rate(self) -> float:
        if self.total_fields <= 0:
            return 0.0
        return round(self.unmapped_fields / self.total_fields, 6)

    @property
    def all_required_targets_covered(self) -> bool:
        """Whether every mapped entity satisfies its canonical requirements.

        A profile failing this is NOT transformation-ready, and Phase 9 must
        not be told otherwise (Step 24).
        """
        return all(
            entity.required_target_coverage_complete for entity in self.entities
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_fields": self.total_fields,
            "mapped_fields": self.mapped_fields,
            "ambiguous_fields": self.ambiguous_fields,
            "unmapped_fields": self.unmapped_fields,
            "review_required_fields": self.review_required_fields,
            "coverage_ratio": self.coverage_ratio,
            "ambiguity_rate": self.ambiguity_rate,
            "unmapped_rate": self.unmapped_rate,
            "all_required_targets_covered": self.all_required_targets_covered,
            "entities": [entity.to_dict() for entity in self.entities],
        }


# ============================================================
# Validation findings (Step 30)
# ============================================================

class FindingSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class ValidationFinding:
    """One problem found in a generated or supplied mapping profile."""

    severity: FindingSeverity
    code: str
    message: str
    source_field: str | None = None
    target_field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "source_field": self.source_field,
            "target_field": self.target_field,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Everything wrong with a profile, gathered in one pass.

    Non-fatal by default so a reviewer sees the full picture rather than
    fixing one problem at a time.
    """

    findings: tuple[ValidationFinding, ...] = ()

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.ERROR)

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.WARNING)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [finding.to_dict() for finding in self.findings],
        }


# ============================================================
# Manual override (Step 32)
# ============================================================

@dataclass(frozen=True)
class MappingOverride:
    """A human decision that supersedes the engine's suggestion.

    ``target`` is a qualified canonical name (``customer.email``) or ``None``
    to force a field to stay unmapped - which is itself a legitimate and
    common review decision.
    """

    source_field: str
    target: str | None
    reason: str | None = None
    decided_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.source_field,
            "target": self.target,
            "reason": self.reason,
            "decided_by": self.decided_by,
        }


@dataclass(frozen=True)
class RejectedCandidate:
    """A candidate a reviewer has explicitly ruled out (Step 33).

    Suppressed for the rest of the reviewed context, so re-running the engine
    does not keep re-proposing something a human already declined.
    """

    source_field: str
    target: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.source_field,
            "target": self.target,
            "reason": self.reason,
        }


# ============================================================
# Result (Step 27)
# ============================================================

@dataclass(frozen=True)
class MappingResult:
    """Everything one mapping run produced.

    ``profiles`` are the authoritative Phase 1 output - one per
    (source entity -> canonical entity) pair, since that is exactly the scope
    ``MappingProfile`` defines. ``decisions``, ``coverage``, ``ambiguities``
    and ``collisions`` are the supplemental reasoning kept beside them.
    """

    source_system_id: str
    source_schema_id: str
    canonical_model_identity: str
    engine_version: str
    config_fingerprint: str
    profiles: tuple[Any, ...] = ()          # MappingProfile
    decisions: tuple[FieldDecision, ...] = ()
    coverage: MappingCoverage | None = None
    ambiguities: tuple[MappingAmbiguity, ...] = ()
    collisions: tuple[TargetCollision, ...] = ()
    validation: ValidationReport | None = None
    #: Source entities no canonical entity could be matched to.
    unmatched_entities: tuple[str, ...] = ()

    def profile_for(self, source_entity: str) -> Any | None:
        for profile in self.profiles:
            if profile.source_entity == source_entity:
                return profile
        return None

    def decision_for(self, source_field: str) -> FieldDecision | None:
        for decision in self.decisions:
            if decision.source_field == source_field:
                return decision
        return None

    def decisions_with_outcome(
        self, outcome: FieldOutcome
    ) -> tuple[FieldDecision, ...]:
        return tuple(d for d in self.decisions if d.outcome is outcome)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_system_id": self.source_system_id,
            "source_schema_id": self.source_schema_id,
            "canonical_model": self.canonical_model_identity,
            "engine_version": self.engine_version,
            "config_fingerprint": self.config_fingerprint,
            "profiles": [profile.to_json_dict() for profile in self.profiles],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "ambiguities": [item.to_dict() for item in self.ambiguities],
            "collisions": [item.to_dict() for item in self.collisions],
            "validation": self.validation.to_dict() if self.validation else None,
            "unmatched_entities": list(self.unmatched_entities),
        }


__all__ = [
    "MAPPING_ENGINE_VERSION",
    "ConfidenceLevel",
    "FieldOutcome",
    "ScoringWeights",
    "DEFAULT_WEIGHTS",
    "MappingOptions",
    "DEFAULT_OPTIONS",
    "NameMatchKind",
    "NameEvidence",
    "ContextEvidence",
    "MappingEvidence",
    "MappingScore",
    "MappingCandidate",
    "MappingAmbiguity",
    "TargetCollision",
    "FieldDecision",
    "EntityCoverage",
    "MappingCoverage",
    "FindingSeverity",
    "ValidationFinding",
    "ValidationReport",
    "MappingOverride",
    "RejectedCandidate",
    "MappingResult",
]
