"""Deterministic, explainable scoring of one (source field, canonical field)
pair.

Every score this module produces is accompanied by the evidence that produced
it. There is no code path that returns a number without its reasoning, which
is what makes the engine auditable rather than merely automated.

The formula
-----------
::

    score = 0.50 * name
          + 0.20 * type
          + 0.20 * entity
          + 0.10 * path

Each component is in [0, 1], the weights sum to 1.0, so the total is in [0, 1]
and directly comparable against the configured thresholds. Weights are
configurable (``ScoringWeights``) and their rationale is documented there.

This is a RANKING SCORE, not a probability. It has not been calibrated against
observed correctness, and this phase does not claim it has.
"""

from __future__ import annotations

from typing import Sequence

from erp_pipeline.mapping.aliases import AliasIndex
from erp_pipeline.mapping.canonical_model import CanonicalEntity, CanonicalField
from erp_pipeline.mapping.compatibility import compare_types
from erp_pipeline.mapping.models import (
    ContextEvidence,
    MappingEvidence,
    MappingScore,
    NameEvidence,
    NameMatchKind,
    ScoringWeights,
)
from erp_pipeline.mapping.normalization import (
    NormalizationConfig,
    canonical_tokens,
    normalized_key,
    path_tokens,
    shared_tokens,
    token_similarity,
)
from erp_pipeline.schemas.source_models import SourceEntity, SourceField

#: Name-evidence scores per match kind. Ordered so that a declared alias beats
#: token overlap but never beats an outright name match: a human declaring
#: "cust_no means customer_id" is strong evidence, and two names simply BEING
#: the same is stronger still.
_EXACT_SCORE = 1.0
_NORMALIZED_EXACT_SCORE = 0.98
_ALIAS_SCORE = 0.94

#: Below this token overlap, the names are treated as unrelated. Two long
#: names sharing only "id" would otherwise produce a stream of weak
#: candidates that bury the real ones.
_MIN_TOKEN_SIMILARITY = 0.25

#: Entity-context scores.
_ENTITY_EXACT = 1.0
_ENTITY_ALIAS = 0.95
#: The source entity's name contains every token of the canonical entity's -
#: ``fin_customer`` for ``customer``. Just below an explicitly declared alias.
_ENTITY_CONTAINS_TARGET = 0.85
#: The reverse, which is weaker: a source entity called ``customer`` against a
#: canonical ``customer_account`` says less, because the extra tokens are the
#: canonical model's own qualification.
_ENTITY_CONTAINED_BY_TARGET = 0.75
#: Neutral, not zero: three of the six source technologies produce entities
#: whose names say nothing about the business object (a CSV called
#: ``export_2026_q1``), and punishing them would make every one of their
#: fields unmappable.
_ENTITY_NEUTRAL = 0.5


def score_pair(
    source_field: SourceField,
    source_entity: SourceEntity,
    canonical_field: CanonicalField,
    canonical_entity: CanonicalEntity,
    alias_index: AliasIndex,
    weights: ScoringWeights,
    normalization: NormalizationConfig | None = None,
    source_field_path: str | None = None,
) -> tuple[MappingScore, MappingEvidence]:
    """Score one candidate pairing and return it with its full evidence."""
    name_evidence = score_name(
        source_field, canonical_field, alias_index, normalization
    )
    type_comparison = compare_types(
        source_field.normalized_data_type,
        canonical_field.data_type,
        source_is_array=source_field.is_array,
    )
    entity_evidence = score_entity_context(
        source_entity, canonical_entity, alias_index, normalization
    )
    path_evidence = score_path_context(
        source_field, canonical_field, canonical_entity, normalization
    )

    name_component = round(weights.name * name_evidence.score, 6)
    type_component = round(weights.type * type_comparison.score, 6)
    entity_component = round(weights.entity * entity_evidence.score, 6)
    path_component = round(weights.path * path_evidence.score, 6)

    total = round(
        name_component + type_component + entity_component + path_component, 6
    )

    score = MappingScore(
        total=total,
        name_component=name_component,
        type_component=type_component,
        entity_component=entity_component,
        path_component=path_component,
        weights=weights,
    )
    evidence = MappingEvidence(
        name=name_evidence,
        type_comparison=type_comparison,
        entity=entity_evidence,
        path=path_evidence,
    )

    return score, evidence


# ============================================================
# Name evidence (Steps 9, 10)
# ============================================================

def score_name(
    source_field: SourceField,
    canonical_field: CanonicalField,
    alias_index: AliasIndex,
    normalization: NormalizationConfig | None = None,
) -> NameEvidence:
    """Compare a source field's name with a canonical field's name.

    TWO SPELLINGS ARE CONSIDERED, and the stronger wins: the field's own leaf
    name, and its full dotted path. That matters because a nested source often
    puts half the name in the path - MongoDB's ``customer.id`` and OpenAPI's
    ``contact.email`` are ``customer_id`` and ``contact_email`` written
    structurally. Comparing only the leaf would score ``customer.id`` against
    ``customer_id`` as a weak token overlap, when it is in fact the same name.

    A flat field has one spelling, so the ordinary case is unchanged.
    """
    leaf = _evaluate_name(
        source_field.source_name, canonical_field, alias_index, normalization
    )

    if not source_field.nested_path:
        return leaf

    path_spelling = render_source_field_path(source_field).replace("[]", "")
    qualified = _evaluate_name(
        path_spelling, canonical_field, alias_index, normalization
    )

    # The leaf wins ties, so the evidence quotes the simpler explanation when
    # both readings are equally strong.
    return qualified if qualified.score > leaf.score else leaf


def _evaluate_name(
    source_name: str,
    canonical_field: CanonicalField,
    alias_index: AliasIndex,
    normalization: NormalizationConfig | None = None,
) -> NameEvidence:
    """Compare ONE source spelling against a canonical field's name.

    Checked strongest-first, and the first hit wins - so an exact match is
    never reported as a weaker alias or token match just because those also
    happen to hold.
    """
    target_name = canonical_field.name

    source_tokens = canonical_tokens(source_name, normalization)
    target_tokens = canonical_tokens(target_name, normalization)

    # 1. Literal identity.
    if source_name == target_name:
        return NameEvidence(
            kind=NameMatchKind.EXACT,
            score=_EXACT_SCORE,
            source_tokens=source_tokens,
            target_tokens=target_tokens,
            shared_tokens=shared_tokens(source_tokens, target_tokens),
        )

    # 2. Identical once spelling conventions are normalized away.
    if normalized_key(source_name, normalization) == normalized_key(
        target_name, normalization
    ):
        return NameEvidence(
            kind=NameMatchKind.NORMALIZED_EXACT,
            score=_NORMALIZED_EXACT_SCORE,
            source_tokens=source_tokens,
            target_tokens=target_tokens,
            shared_tokens=shared_tokens(source_tokens, target_tokens),
        )

    # 3. The canonical model declares this spelling as an alias. Checked
    #    against the field's own entity so an alias shared by two entities
    #    does not silently credit the wrong one.
    hit = alias_index.lookup_field_in_entity(source_name, canonical_field.entity_type)
    if hit is not None and hit.field_name == canonical_field.name:
        return NameEvidence(
            kind=NameMatchKind.EXPLICIT_ALIAS,
            score=_ALIAS_SCORE,
            source_tokens=source_tokens,
            target_tokens=target_tokens,
            shared_tokens=shared_tokens(source_tokens, target_tokens),
            # The spelling the SOURCE used, when the model declares it - so a
            # reviewer sees an alias their document actually contains.
            matched_alias=hit.best_spelling_for(source_name),
        )

    # 4. Token overlap - the weakest usable evidence.
    similarity = token_similarity(source_tokens, target_tokens)

    if similarity >= _MIN_TOKEN_SIMILARITY:
        return NameEvidence(
            kind=NameMatchKind.TOKEN_OVERLAP,
            score=similarity,
            source_tokens=source_tokens,
            target_tokens=target_tokens,
            shared_tokens=shared_tokens(source_tokens, target_tokens),
        )

    return NameEvidence(
        kind=NameMatchKind.NONE,
        score=0.0,
        source_tokens=source_tokens,
        target_tokens=target_tokens,
    )


# ============================================================
# Entity context (Steps 7, 13)
# ============================================================

def score_entity_context(
    source_entity: SourceEntity,
    canonical_entity: CanonicalEntity,
    alias_index: AliasIndex,
    normalization: NormalizationConfig | None = None,
) -> ContextEvidence:
    """How well a source entity corresponds to a canonical entity.

    This is what keeps ``supplier.email`` from beating ``customer.email`` when
    the source entity is plainly a customer table. It INFLUENCES the score
    rather than gating it (Step 7): a source entity called ``tbl_cust_mast``
    should nudge the customer entity ahead, not veto every other target.
    """
    source_name = source_entity.source_name
    source_tokens = canonical_tokens(source_name, normalization)
    target_tokens = canonical_tokens(canonical_entity.entity_type, normalization)

    if normalized_key(source_name, normalization) == normalized_key(
        canonical_entity.entity_type, normalization
    ):
        return ContextEvidence(
            score=_ENTITY_EXACT,
            source_context=source_name,
            target_context=canonical_entity.entity_type,
            shared_tokens=shared_tokens(source_tokens, target_tokens),
        )

    matches = alias_index.lookup_entity(source_name)
    if canonical_entity.entity_type in matches:
        return ContextEvidence(
            score=_ENTITY_ALIAS,
            source_context=source_name,
            target_context=canonical_entity.entity_type,
            shared_tokens=shared_tokens(source_tokens, target_tokens),
            matched_alias=source_name,
        )

    # Containment, checked before plain overlap. ERP table names are almost
    # always the business object plus decoration - `fin_customer`,
    # `tbl_customer`, `customer_master`, `dim_customer` - so a source entity
    # CONTAINING every token of a canonical entity is strong evidence, even
    # though symmetric overlap would score it only 0.5 and miss it entirely.
    source_set, target_set = set(source_tokens), set(target_tokens)

    if target_set and target_set <= source_set:
        return ContextEvidence(
            score=_ENTITY_CONTAINS_TARGET,
            source_context=source_name,
            target_context=canonical_entity.entity_type,
            shared_tokens=shared_tokens(source_tokens, target_tokens),
        )

    if source_set and source_set <= target_set:
        return ContextEvidence(
            score=_ENTITY_CONTAINED_BY_TARGET,
            source_context=source_name,
            target_context=canonical_entity.entity_type,
            shared_tokens=shared_tokens(source_tokens, target_tokens),
        )

    similarity = token_similarity(source_tokens, target_tokens)

    if similarity > 0.0:
        return ContextEvidence(
            score=round(similarity, 6),
            source_context=source_name,
            target_context=canonical_entity.entity_type,
            shared_tokens=shared_tokens(source_tokens, target_tokens),
        )

    # No signal either way. Neutral rather than zero - see _ENTITY_NEUTRAL.
    return ContextEvidence(
        score=_ENTITY_NEUTRAL,
        source_context=source_name,
        target_context=canonical_entity.entity_type,
    )


# ============================================================
# Nested path context (Step 13)
# ============================================================

def score_path_context(
    source_field: SourceField,
    canonical_field: CanonicalField,
    canonical_entity: CanonicalEntity,
    normalization: NormalizationConfig | None = None,
) -> ContextEvidence:
    """How well a source field's PATH agrees with the canonical target.

    ``customer.contact.email`` carries its own entity hint in the path, which
    is often the only hint a MongoDB or OpenAPI source gives: the enclosing
    entity may be ``invoices`` while the field itself plainly belongs to the
    customer. Comparing the full access path against
    ``<entity_type>.<field_name>`` captures that.

    A flat field with no nested path gets a neutral score rather than a
    penalty, because most relational and CSV fields are flat and have nothing
    to say here.
    """
    source_path = path_tokens(
        source_field.nested_path, source_field.source_name, normalization
    )
    target_path = canonical_tokens(
        canonical_entity.entity_type, normalization
    ) + canonical_tokens(canonical_field.name, normalization)

    source_context = ".".join(
        [segment for segment in (source_field.nested_path or ()) if segment != "[]"]
        + [source_field.source_name]
    )

    if not source_field.nested_path:
        # Nothing to compare. Neutral, matching the entity-context rationale.
        return ContextEvidence(
            score=_ENTITY_NEUTRAL,
            source_context=source_context,
            target_context=canonical_field.qualified_name,
        )

    similarity = token_similarity(source_path, target_path)

    return ContextEvidence(
        score=round(similarity, 6),
        source_context=source_context,
        target_context=canonical_field.qualified_name,
        shared_tokens=shared_tokens(source_path, target_path),
    )


def render_source_field_path(source_field: SourceField) -> str:
    """The full source path used as ``FieldMapping.source_field``.

    ``("customer", "contact")`` + ``email`` -> ``customer.contact.email``.
    Array markers are kept, so a field inside a collection is still
    addressable exactly as Phases 5-7 described it.
    """
    segments = list(source_field.nested_path or ()) + [source_field.source_name]
    rendered = ""

    for segment in segments:
        if segment == "[]":
            rendered += "[]"
        elif rendered:
            rendered += f".{segment}"
        else:
            rendered = segment

    return rendered


__all__ = [
    "score_pair",
    "score_name",
    "score_entity_context",
    "score_path_context",
    "render_source_field_path",
]
