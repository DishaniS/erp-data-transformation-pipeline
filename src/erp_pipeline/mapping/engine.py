"""The mapping engine: candidates, selection, ambiguity, collisions.

SOURCE-INDEPENDENT BY CONSTRUCTION
----------------------------------
There is not one branch in this module on where a schema came from. No
``if source_type is MYSQL``, no MongoDB special case, no OpenAPI carve-out.
The engine consumes ``SourceSchema`` / ``SourceEntity`` / ``SourceField`` and
nothing else, which is only possible because Phases 4-7 did the work of making
seven very different technologies produce that one contract. A test asserts
the absence of source-specific branching, because it is the central research
claim of this phase.

CONSERVATIVE BY DESIGN
----------------------
The engine would rather leave a field unmapped than map it wrongly. Three
independent gates must all pass before anything is selected automatically:

    score >= high threshold
    margin over the runner-up >= ambiguity margin
    the datatype is not incompatible

Failing any one of them produces REVIEW_REQUIRED or AMBIGUOUS - a result a
human can act on - rather than a confident-looking guess.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from erp_pipeline.mapping.aliases import AliasIndex, build_alias_index
from erp_pipeline.mapping.canonical_model import (
    CanonicalEntity,
    CanonicalTargetModel,
    DEFAULT_CANONICAL_MODEL,
)
from erp_pipeline.mapping.errors import (
    CanonicalTargetNotFoundError,
    InvalidMappingOverrideError,
    SourceFieldNotFoundError,
)
from erp_pipeline.mapping.models import (
    DEFAULT_OPTIONS,
    NameMatchKind,
    FieldDecision,
    FieldOutcome,
    MappingAmbiguity,
    MappingCandidate,
    MappingOptions,
    MappingOverride,
    RejectedCandidate,
    TargetCollision,
)
from erp_pipeline.mapping.normalization import (
    DEFAULT_NORMALIZATION,
    NormalizationConfig,
    canonical_tokens,
    normalized_key,
    token_similarity,
)
from erp_pipeline.mapping.scoring import (
    render_source_field_path,
    score_entity_context,
    score_pair,
)
from erp_pipeline.schemas.source_models import SourceEntity, SourceField, SourceSchema

#: Below this, a source entity is considered to match no canonical entity at
#: all, and its fields are scored against every entity instead of a chosen one.
_ENTITY_MATCH_FLOOR = 0.60


class MappingEngine:
    """Generates explainable mapping candidates for a ``SourceSchema``."""

    def __init__(
        self,
        canonical_model: CanonicalTargetModel | None = None,
        options: MappingOptions | None = None,
        normalization: NormalizationConfig | None = None,
    ) -> None:
        self._model = canonical_model or DEFAULT_CANONICAL_MODEL
        self._options = options or DEFAULT_OPTIONS
        self._normalization = normalization or DEFAULT_NORMALIZATION
        self._alias_index = build_alias_index(self._model, self._normalization)

    # ------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------

    @property
    def canonical_model(self) -> CanonicalTargetModel:
        return self._model

    @property
    def options(self) -> MappingOptions:
        return self._options

    @property
    def alias_index(self) -> AliasIndex:
        return self._alias_index

    @property
    def config_fingerprint(self) -> str:
        """Everything that could change a mapping outcome, in one string.

        Feeds the profile's deterministic identity, so a mapping generated
        under different thresholds, weights or normalization rules is
        distinguishable from one generated under these (Step 29).
        """
        return (
            f"{self._options.fingerprint()}"
            f"/{self._normalization.fingerprint()}"
            f"/alias@{self._alias_index.version}"
            f"/model={self._model.identity}"
        )

    # ------------------------------------------------------------
    # Entity matching (Step 7)
    # ------------------------------------------------------------

    def match_entity(self, source_entity: SourceEntity) -> tuple[str | None, float]:
        """Choose the canonical entity a source entity most likely represents.

        Returns ``(entity_type, score)``, or ``(None, score)`` when nothing
        clears the floor - in which case the caller scores the entity's fields
        against EVERY canonical entity rather than guessing. That is the
        honest behaviour for a CSV called ``export_2026_q1``, whose name says
        nothing about the business object.
        """
        best_type: str | None = None
        best_score = 0.0

        for canonical_entity in self._model.entities:
            evidence = score_entity_context(
                source_entity, canonical_entity, self._alias_index,
                self._normalization,
            )
            if evidence.score > best_score:
                best_score = evidence.score
                best_type = canonical_entity.entity_type

        if best_score < _ENTITY_MATCH_FLOOR:
            return None, best_score

        return best_type, best_score

    # ------------------------------------------------------------
    # Candidate generation (Steps 9-13)
    # ------------------------------------------------------------

    def generate_candidates(
        self,
        source_field: SourceField,
        source_entity: SourceEntity,
        restrict_to_entity: str | None = None,
        rejected: Sequence[RejectedCandidate] = (),
    ) -> tuple[MappingCandidate, ...]:
        """Score one source field against the canonical model.

        ``restrict_to_entity`` narrows scoring to one canonical entity when the
        source entity was confidently matched. Candidates are still generated
        across all entities when it was not, so a field can find its home even
        if its table name was uninformative.

        Ordering is deterministic: by descending score, then by qualified
        target name. Ties therefore resolve alphabetically rather than by
        dictionary order, so two runs never disagree.
        """
        source_path = render_source_field_path(source_field)
        suppressed = {
            item.target for item in rejected if item.source_field == source_path
        }

        entities = self._entities_in_scope(source_field, restrict_to_entity)
        candidates = self._score_against(
            source_field, source_entity, entities, source_path, suppressed
        )

        if not candidates and len(entities) < len(self._model.entities):
            # The matched entity had nothing to offer this field. An invoice
            # response carrying `emailAddress`, or an invoice table carrying a
            # customer's phone number, is ordinary - the field belongs to a
            # different canonical entity than its container does. Widening only
            # when the narrow scope came up EMPTY keeps the disambiguation
            # benefit (a matched entity still wins whenever it has a candidate)
            # without stranding fields that plainly have a home elsewhere.
            candidates = self._score_against(
                source_field, source_entity, self._model.entities, source_path,
                suppressed,
            )

        candidates.sort(key=lambda item: (-item.score.total, item.qualified_target))

        return tuple(candidates[: self._options.max_candidates_per_field])

    def _score_against(
        self,
        source_field: SourceField,
        source_entity: SourceEntity,
        entities: Iterable[CanonicalEntity],
        source_path: str,
        suppressed: set[str],
    ) -> list[MappingCandidate]:
        """Score one field against a given set of canonical entities."""
        candidates: list[MappingCandidate] = []

        for canonical_entity in entities:
            for canonical_field in canonical_entity.fields:
                qualified = canonical_field.qualified_name

                if qualified in suppressed:
                    # A reviewer already declined this pairing (Step 33).
                    continue

                score, evidence = score_pair(
                    source_field=source_field,
                    source_entity=source_entity,
                    canonical_field=canonical_field,
                    canonical_entity=canonical_entity,
                    alias_index=self._alias_index,
                    weights=self._options.weights,
                    normalization=self._normalization,
                )

                if evidence.name.kind is NameMatchKind.NONE:
                    # No name evidence at all. Entity and path context are
                    # CORROBORATING signals - they say which of several
                    # plausible targets is meant, not that an unrelated field
                    # is one. Without them this gate, every field in a table
                    # matched to `invoice` would produce weak candidates purely
                    # for being in that table, and `legacy_internal_flag_74`
                    # would be offered as a status. It must stay unmapped.
                    continue

                if score.total < self._options.minimum_candidate_score:
                    continue

                candidates.append(
                    MappingCandidate(
                        source_field=source_path,
                        source_entity=source_entity.source_name,
                        target_entity_type=canonical_entity.entity_type,
                        target_field=canonical_field.name,
                        score=score,
                        evidence=evidence,
                        confidence=self._options.confidence_level(score.total),
                        source_data_type=source_field.normalized_data_type,
                        target_data_type=canonical_field.data_type,
                    )
                )

        return candidates

    def _entities_in_scope(
        self, source_field: SourceField, restrict_to_entity: str | None
    ) -> tuple[CanonicalEntity, ...]:
        """Which canonical entities this field should be scored against.

        Normally the entity its table was matched to. But a NESTED PATH can
        name a different business object than the enclosing entity does, and
        that is common rather than exotic: a MongoDB ``invoices`` collection
        holding ``customer.contact.email``, or an OpenAPI invoice schema
        embedding a customer block. Restricting such a field to ``invoice``
        would leave it permanently unmapped even though its path says exactly
        where it belongs.

        So any canonical entity whose name appears in the field's PATH is
        brought into scope alongside the matched one. This widens on evidence -
        the path - never on a guess, and the ordinary flat case is unaffected.
        """
        matched = (
            self._model.entity(restrict_to_entity) if restrict_to_entity else None
        )

        if matched is None:
            return self._model.entities

        in_scope = [matched]

        nested_tokens: set[str] = set()
        for segment in source_field.nested_path or ():
            if segment != "[]":
                nested_tokens.update(canonical_tokens(segment, self._normalization))

        if nested_tokens:
            for entity in self._model.entities:
                if entity.entity_type == matched.entity_type:
                    continue
                entity_tokens = set(
                    canonical_tokens(entity.entity_type, self._normalization)
                )
                if entity_tokens and entity_tokens <= nested_tokens:
                    in_scope.append(entity)

        return tuple(in_scope)

    # ------------------------------------------------------------
    # Selection (Steps 19, 20, 21)
    # ------------------------------------------------------------

    def decide_field(
        self,
        source_field: SourceField,
        source_entity: SourceEntity,
        restrict_to_entity: str | None = None,
        overrides: Mapping[str, MappingOverride] | None = None,
        rejected: Sequence[RejectedCandidate] = (),
    ) -> FieldDecision:
        """Produce the engine's decision about one source field."""
        source_path = render_source_field_path(source_field)
        candidates = self.generate_candidates(
            source_field, source_entity, restrict_to_entity, rejected
        )

        override = (overrides or {}).get(source_path)
        if override is not None:
            return self._apply_override(
                override, source_field, source_entity, candidates
            )

        if not candidates:
            return FieldDecision(
                source_field=source_path,
                source_entity=source_entity.source_name,
                source_data_type=source_field.normalized_data_type,
                outcome=FieldOutcome.UNMAPPED,
                candidates=(),
                reason=(
                    "no canonical target scored above the minimum candidate "
                    f"threshold of {self._options.minimum_candidate_score}"
                ),
            )

        best = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None

        # Gate 1: the type must not be impossible.
        if (
            self._options.require_type_compatibility_for_auto
            and best.has_type_conflict
        ):
            return FieldDecision(
                source_field=source_path,
                source_entity=source_entity.source_name,
                source_data_type=source_field.normalized_data_type,
                outcome=FieldOutcome.REVIEW_REQUIRED,
                candidates=candidates,
                reason=(
                    "the best candidate has an incompatible datatype: "
                    f"{best.evidence.type_comparison.explain()}"
                ),
            )

        # Gate 2: the evidence must be strong enough.
        if best.score.total < self._options.high_threshold:
            return FieldDecision(
                source_field=source_path,
                source_entity=source_entity.source_name,
                source_data_type=source_field.normalized_data_type,
                outcome=FieldOutcome.REVIEW_REQUIRED,
                candidates=candidates,
                reason=(
                    f"best score {best.score.total} is below the high-confidence "
                    f"threshold of {self._options.high_threshold}"
                ),
            )

        # Gate 3: the choice must be clear-cut.
        if runner_up is not None:
            margin = round(best.score.total - runner_up.score.total, 6)
            if margin < self._options.ambiguity_margin:
                ambiguity = MappingAmbiguity(
                    source_field=source_path,
                    best_target=best.qualified_target,
                    runner_up_target=runner_up.qualified_target,
                    best_score=best.score.total,
                    runner_up_score=runner_up.score.total,
                    margin=margin,
                    required_margin=self._options.ambiguity_margin,
                )
                return FieldDecision(
                    source_field=source_path,
                    source_entity=source_entity.source_name,
                    source_data_type=source_field.normalized_data_type,
                    outcome=FieldOutcome.AMBIGUOUS,
                    candidates=candidates,
                    ambiguity=ambiguity,
                    reason=ambiguity.explain(),
                )

        return FieldDecision(
            source_field=source_path,
            source_entity=source_entity.source_name,
            source_data_type=source_field.normalized_data_type,
            outcome=FieldOutcome.AUTO_SELECTED,
            candidates=candidates,
            selected=best,
            reason=best.explain(),
        )

    # ------------------------------------------------------------
    # Manual override (Step 32)
    # ------------------------------------------------------------

    def _apply_override(
        self,
        override: MappingOverride,
        source_field: SourceField,
        source_entity: SourceEntity,
        candidates: tuple[MappingCandidate, ...],
    ) -> FieldDecision:
        """Honour a human decision - after validating it.

        An override is trusted over the engine's own suggestion, but it is not
        trusted blindly: a target that does not exist, or a type that cannot
        possibly convert, would produce a profile that fails in Phase 9. It is
        far cheaper to reject it here.
        """
        source_path = render_source_field_path(source_field)

        if override.target is None:
            # A reviewer deliberately marking a field as not mappable.
            return FieldDecision(
                source_field=source_path,
                source_entity=source_entity.source_name,
                source_data_type=source_field.normalized_data_type,
                outcome=FieldOutcome.UNMAPPED,
                candidates=candidates,
                reason=(
                    "manually left unmapped"
                    + (f": {override.reason}" if override.reason else "")
                ),
            )

        canonical_field = self._model.field_by_qualified_name(override.target)

        if canonical_field is None:
            raise CanonicalTargetNotFoundError(
                f"Override for {source_path!r} names canonical target "
                f"{override.target!r}, which the canonical model "
                f"{self._model.identity} does not declare.",
                target=override.target,
            )

        canonical_entity = self._model.entity(canonical_field.entity_type)
        assert canonical_entity is not None  # guaranteed by the lookup above

        score, evidence = score_pair(
            source_field=source_field,
            source_entity=source_entity,
            canonical_field=canonical_field,
            canonical_entity=canonical_entity,
            alias_index=self._alias_index,
            weights=self._options.weights,
            normalization=self._normalization,
        )

        if evidence.has_type_conflict:
            raise InvalidMappingOverrideError(
                f"Override maps {source_path!r} to {override.target!r}, but "
                f"{evidence.type_comparison.explain()}. A mapping that cannot "
                "convert would fail during transformation; fix the override or "
                "declare a transformation rule.",
                source_field=source_path,
                target_field=override.target,
            )

        selected = MappingCandidate(
            source_field=source_path,
            source_entity=source_entity.source_name,
            target_entity_type=canonical_field.entity_type,
            target_field=canonical_field.name,
            score=score,
            evidence=evidence,
            confidence=self._options.confidence_level(score.total),
            source_data_type=source_field.normalized_data_type,
            target_data_type=canonical_field.data_type,
        )

        return FieldDecision(
            source_field=source_path,
            source_entity=source_entity.source_name,
            source_data_type=source_field.normalized_data_type,
            outcome=FieldOutcome.MANUAL_OVERRIDE,
            # The override is surfaced first, with the engine's own suggestions
            # kept beside it so the disagreement stays visible.
            candidates=(selected,) + tuple(
                item for item in candidates
                if item.qualified_target != selected.qualified_target
            ),
            selected=selected,
            reason=(
                "manually selected"
                + (f": {override.reason}" if override.reason else "")
            ),
        )

    # ------------------------------------------------------------
    # Whole-schema decisions
    # ------------------------------------------------------------

    def decide_schema(
        self,
        schema: SourceSchema,
        overrides: Sequence[MappingOverride] = (),
        rejected: Sequence[RejectedCandidate] = (),
    ) -> tuple[tuple[FieldDecision, ...], dict[str, str | None], tuple[TargetCollision, ...]]:
        """Decide every field of every entity in a schema.

        Returns the decisions, the source-entity -> canonical-entity match map,
        and any target collisions found after selection.
        """
        override_map = {item.source_field: item for item in overrides}
        decisions: list[FieldDecision] = []
        entity_matches: dict[str, str | None] = {}

        for source_entity in schema.entities:
            matched_entity, _ = self.match_entity(source_entity)
            entity_matches[source_entity.normalized_name] = matched_entity

            for source_field in source_entity.fields:
                decisions.append(
                    self.decide_field(
                        source_field=source_field,
                        source_entity=source_entity,
                        restrict_to_entity=matched_entity,
                        overrides=override_map,
                        rejected=rejected,
                    )
                )

        resolved, collisions = self._resolve_collisions(tuple(decisions))

        return resolved, entity_matches, collisions

    # ------------------------------------------------------------
    # Target collisions (Step 22)
    # ------------------------------------------------------------

    def _resolve_collisions(
        self, decisions: tuple[FieldDecision, ...]
    ) -> tuple[tuple[FieldDecision, ...], tuple[TargetCollision, ...]]:
        """Detect several source fields auto-selected onto one target.

        ``cust_no`` and ``customer_number`` both landing on
        ``customer.customer_id`` is not something to silently accept: at most
        one of them is the real identifier, and choosing wrongly corrupts every
        record. The strongest is kept and the rest are demoted to review, with
        the collision recorded.

        A MANUAL override is never demoted - a human has already decided, and
        overriding their decision on a heuristic would defeat the point of
        having overrides.
        """
        if not self._options.detect_target_collisions:
            return decisions, ()

        by_target: dict[str, list[FieldDecision]] = {}

        for decision in decisions:
            if decision.outcome is not FieldOutcome.AUTO_SELECTED:
                continue
            assert decision.selected is not None
            by_target.setdefault(decision.selected.qualified_target, []).append(
                decision
            )

        collisions: list[TargetCollision] = []
        demoted: dict[str, FieldDecision] = {}

        for target, competing in sorted(by_target.items()):
            if len(competing) < 2:
                continue

            # Deterministic: highest score wins, ties broken by source path.
            ordered = sorted(
                competing,
                key=lambda item: (
                    -item.selected.score.total,  # type: ignore[union-attr]
                    item.source_field,
                ),
            )
            keeper = ordered[0]

            for loser in ordered[1:]:
                demoted[loser.source_field] = FieldDecision(
                    source_field=loser.source_field,
                    source_entity=loser.source_entity,
                    source_data_type=loser.source_data_type,
                    outcome=FieldOutcome.REVIEW_REQUIRED,
                    candidates=loser.candidates,
                    reason=(
                        f"target {target} was also selected by "
                        f"{keeper.source_field!r}, which scored higher; a "
                        "single-source target cannot take both"
                    ),
                )

            collisions.append(
                TargetCollision(
                    target=target,
                    source_fields=tuple(item.source_field for item in ordered),
                    kept_source_field=keeper.source_field,
                )
            )

        if not demoted:
            return decisions, tuple(collisions)

        resolved = tuple(
            demoted.get(decision.source_field, decision) for decision in decisions
        )

        return resolved, tuple(collisions)


def find_source_field(
    schema: SourceSchema, source_field_path: str
) -> tuple[SourceEntity, SourceField]:
    """Locate a field by its rendered path, or raise.

    Used when validating overrides: an override naming a field the schema does
    not contain is a mistake worth failing on, not something to ignore.
    """
    for entity in schema.entities:
        for field in entity.fields:
            if render_source_field_path(field) == source_field_path:
                return entity, field

    raise SourceFieldNotFoundError(
        f"Source field {source_field_path!r} does not exist in schema "
        f"{schema.schema_id!r}.",
        source_field=source_field_path,
    )


__all__ = [
    "MappingEngine",
    "find_source_field",
]
