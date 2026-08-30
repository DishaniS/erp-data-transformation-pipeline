"""Decide which fields of an ERP response the caller's question needs.

THIS IS THE NEW MECHANISM OF THE PHASE
--------------------------------------
Everything else in ``response_adaptation`` re-uses machinery that already
exists. This module does not: nothing in the pipeline previously scored a
field against a natural-language question.

WHY IT IS NOT AN LLM
--------------------
Asking a model "which of these fields matter?" would be easy and would make
the result unmeasurable: non-deterministic between runs, unattributable to any
rule, and impossible to defend in an evaluation. Every score here is a
weighted sum of four bounded signals, and every field carries the signals that
produced its score, so a reviewer can read WHY ``row_version`` was dropped
instead of being told that it was.

WHY IT IS NOT PURE STRING MATCHING EITHER
-----------------------------------------
The ``alias`` signal is what makes the mechanism ERP-aware rather than
lexical. A question asking about "the customer" matches a source field named
``cust_ref`` - not because the strings resemble each other, but because the
canonical model states that ``cust_ref`` is one way ERP systems spell
``customer_id``. That vocabulary is the component's own contribution, and it
is weighted the heaviest for exactly that reason.

THE FOUR SIGNALS
----------------
    alias     the query names the CANONICAL concept this field maps to
    name      the query names the SOURCE field literally
    entity    this field belongs to the entity the response is about
    identity  this field identifies the record

DIRECTION OF THE MATCH MATTERS
------------------------------
Each lexical signal measures *what fraction of the field's name the query
mentions*, not Jaccard similarity between the two. A question is a sentence
and a field name is one or two words; symmetric similarity would punish every
field for the length of the question that asked about it. "How much is the
invoice for?" covers all of ``amount``, which is the fact worth scoring.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from erp_pipeline.mapping.canonical_model import (
    DEFAULT_CANONICAL_MODEL,
    CanonicalField,
    CanonicalTargetModel,
)
from erp_pipeline.mapping.normalization import canonical_tokens, split_tokens
from erp_pipeline.response_adaptation.models import FieldRelevance, RelevanceWeights

#: Words carrying no field-selection information. Kept deliberately short and
#: explicit: an aggressive list would silently delete real ERP vocabulary
#: ("order", "status", "date" are all legitimate field names AND common English
#: words), so only true function words are removed.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "be", "by", "can", "did",
        "do", "does", "for", "from", "get", "give", "has", "have", "how", "i",
        "in", "is", "it", "its", "many", "me", "much", "my", "of", "on", "or",
        "please", "show", "that", "the", "their", "them", "there", "these",
        "this", "to", "was", "were", "what", "when", "where", "which", "who",
        "why", "will", "with", "you", "your",
    }
)

#: Question phrasings mapped onto the ERP concepts they ask about.
#:
#: WHY THIS EXISTS SEPARATELY FROM THE MAPPING VOCABULARY
#: ``mapping.normalization`` already knows that ``amt`` means ``amount`` -
#: that table is about how ERP systems SPELL a field. This table is about how a
#: person ASKS for one. "How much is this invoice for" contains no form of the
#: word "amount", and no amount of field-name normalisation will connect them,
#: because the gap is between a question and a schema rather than between two
#: schemas.
#:
#: It is deliberately kept here rather than merged into ``DEFAULT_SYNONYMS``:
#: declaring "much" a synonym of "amount" globally would corrupt every schema
#: mapping in the pipeline, where those words are not interchangeable at all.
#:
#: HONESTY NOTE: this is a hand-authored lexicon of ERP question vocabulary,
#: not a learned resource, and its size is reported in the evaluation. It was
#: written from ERP domain terms (accounts payable / receivable phrasing), not
#: from the evaluation queries, but a reader should weigh it as an authored
#: component of the method rather than as an emergent result.
#:
#: Keys are token sequences matched CONTIGUOUSLY against the query, before
#: stopwords are removed - "how much" is two stopwords and would otherwise be
#: gone before it could be recognised.
QUERY_INTENT_TERMS: Mapping[tuple[str, ...], tuple[str, ...]] = {
    ("how", "much"): ("amount", "total", "price"),
    ("how", "many"): ("quantity", "count"),
    ("total",): ("amount",),
    ("cost",): ("amount",),
    ("costs",): ("amount",),
    ("price",): ("amount",),
    ("worth",): ("amount",),
    ("owe",): ("amount", "status"),
    ("owed",): ("amount", "status"),
    ("outstanding",): ("amount", "status"),
    ("balance",): ("amount", "status"),
    ("who",): ("customer", "supplier", "name"),
    ("named",): ("name",),
    ("called",): ("name",),
    ("when",): ("date",),
    ("where",): ("address", "location"),
    ("overdue",): ("due", "date", "status"),
    ("late",): ("due", "date", "status"),
    ("paid",): ("status", "payment"),
    ("unpaid",): ("status", "payment"),
    ("settled",): ("status", "payment"),
    ("approved",): ("status",),
    ("approval",): ("status",),
    ("rejected",): ("status",),
    ("pending",): ("status",),
    ("contact",): ("email", "phone"),
    ("reach",): ("email", "phone"),
    ("tax",): ("tax", "amount"),
    ("vat",): ("tax", "amount"),
    ("shipped",): ("status", "date", "address"),
    ("delivered",): ("status", "date", "address"),
}

#: Suffixes that make a field name STRUCTURAL rather than descriptive.
#:
#: ``merchant_name`` is about a merchant; ``_name`` says only that the value is
#: a label. Left in, these tokens halve the score of every two-word field for a
#: question that named the meaningful half - "which merchant" covers one of
#: ``{merchant, name}`` and scores 0.5 for a field it named exactly. They are
#: removed from the TARGET side only; a question that says "name" still matches
#: through the remaining tokens.
GENERIC_FIELD_TOKENS = frozenset(
    {"id", "no", "num", "nbr", "number", "code", "key", "ref", "reference",
     "name", "value", "flag", "ind", "indicator"}
)

#: Raw (pre-expansion) suffixes that mark a field as a record key.
#:
#: Deliberately checked BEFORE abbreviation expansion, because the expansion
#: maps ``code`` and ``key`` onto ``id`` - which is right for matching a field
#: to a canonical target and wrong here, where ``currency_code`` is a currency
#: and not an identifier.
IDENTITY_SUFFIXES = frozenset({"id", "no", "num", "nbr", "number", "ident"})

#: Question words that ask for a WHOLE record rather than an attribute.
#:
#: "Give me the full customer record" names no field, and scoring it field by
#: field answers a question nobody asked. When one of these appears, selection
#: steps aside and everything the budget allows is kept - which is what the
#: caller literally requested.
BROAD_QUERY_TERMS = frozenset(
    {"everything", "all", "full", "complete", "entire", "whole", "overview",
     "summary", "details", "anything"}
)

#: Reasons recorded on a decision, as constants so tests and the evaluation
#: harness match on a value rather than on prose.
REASON_MANDATORY = "mandatory_identity_field"
REASON_SELECTED = "score_above_threshold"
REASON_BELOW_THRESHOLD = "score_below_threshold"
REASON_FIELD_BUDGET = "field_budget_exhausted"
REASON_NO_QUERY = "no_query_supplied"
REASON_NO_SIGNAL = "no_relevance_signal"
REASON_BROAD_QUERY = "broad_query_requests_whole_record"
REASON_INFERRED_IDENTITY = "inferred_identity_field"
REASON_SELECTION_DISABLED = "relevance_selection_disabled"
REASON_BLOCKED_FIELD = "blocked_by_policy"


def intent_expansions(raw_tokens: Sequence[str]) -> tuple[str, ...]:
    """The ERP concepts a question's phrasing implies, from the lexicon above.

    Returned separately from the literal tokens so an explanation can state
    that ``amount`` was matched because the caller said "how much", rather than
    appearing to have found a word that was never in the question.
    """
    found: list[str] = []

    for phrase, added in QUERY_INTENT_TERMS.items():
        width = len(phrase)

        for start in range(len(raw_tokens) - width + 1):
            if tuple(raw_tokens[start : start + width]) == phrase:
                found.extend(added)
                break

    # Deduplicated but order-preserving, so the same query always produces the
    # same expansion list.
    return tuple(dict.fromkeys(found))


def query_tokens(query: str | None) -> tuple[str, ...]:
    """The content words of a question, plus what its phrasing implies.

    Reuses ``canonical_tokens`` rather than a private tokeniser so that a
    question and a field name are always split by identical rules - otherwise
    ``purchaseOrder`` in a query and ``purchase_order`` in a schema would fail
    to match for no reason a user could see.
    """
    if not query:
        return ()

    raw = canonical_tokens(query)
    literal = [token for token in raw if token not in STOPWORDS]

    return tuple(dict.fromkeys([*literal, *intent_expansions(raw)]))


def entity_tokens(entity_type: str | None) -> frozenset[str]:
    """The tokens of an entity's own name, which the lexical signals discount.

    Separate from ``STOPWORDS`` because it is per-response, not global:
    "invoice" is uninformative inside an invoice response and highly
    informative inside a purchase-order response that references one.
    """
    if not entity_type:
        return frozenset()

    return frozenset(canonical_tokens(entity_type))


def _distinctive(tokens: Sequence[str]) -> tuple[str, ...]:
    """Drop structural suffixes, unless that would leave nothing behind.

    ``merchant_name`` -> ``merchant``. ``name`` on its own stays ``name``,
    because a field called nothing but "name" has no other tokens to carry its
    meaning.
    """
    kept = tuple(token for token in tokens if token not in GENERIC_FIELD_TOKENS)

    return kept or tuple(tokens)


def _coverage(
    target: Sequence[str], query: Sequence[str], ignore: frozenset[str] = frozenset()
) -> float:
    """What fraction of ``target``'s tokens the query mentions, in [0, 1].

    Asymmetric on purpose - see the module docstring.

    ``ignore`` removes tokens from BOTH sides before comparing. It carries the
    entity's own name, and dropping it is what stops one word in the question
    from lifting every field at once: an invoice response has aliases like
    ``invoice_amount``, ``invoice_date`` and ``invoice_status``, so the word
    "invoice" in "how much is this invoice for" would half-match all three and
    the lexical signal would stop distinguishing between them. Entity
    membership is already measured, once, by the ``entity`` signal; counting it
    again here would be the same evidence paid for twice.
    """
    target_set = set(_distinctive(target)) - ignore
    query_set = set(query) - ignore

    if not target_set or not query_set:
        return 0.0

    # The overlap coefficient: shared tokens over the SMALLER of the two sets.
    # Dividing by the target alone would penalise a field for having a longer
    # name than the question used - asking "when is it due" would score
    # ``due_date`` at 0.5 purely because the schema also spelled out "date",
    # and it would lose to a single-token field that matched less specifically.
    smaller = min(len(target_set), len(query_set))

    return round(len(target_set & query_set) / smaller, 6)


def _alias_coverage(
    field: CanonicalField | None,
    query: Sequence[str],
    ignore: frozenset[str] = frozenset(),
) -> float:
    """Best coverage across a canonical field's name and all its aliases.

    The maximum, not the mean: an ERP vocabulary lists many spellings of one
    concept and a question only ever uses one of them. Averaging would punish a
    field for being well documented.
    """
    if field is None:
        return 0.0

    best = _coverage(canonical_tokens(field.name), query, ignore)

    for alias in field.aliases:
        best = max(best, _coverage(canonical_tokens(alias), query, ignore))

    return best


def is_broad_query(tokens: Sequence[str]) -> bool:
    """Whether the question asks for the whole record rather than a field."""
    return any(token in BROAD_QUERY_TERMS for token in tokens)


def infer_identity_field(fields: Sequence[tuple[str, str | None]]) -> str | None:
    """Guess which field identifies the record, for entities the model lacks.

    Mandatory-field preservation is driven by the canonical model's
    ``is_identifier`` flag, which works only for the three entities the model
    covers. A process case, a policy document and a receipt all have a perfectly
    obvious key field and no canonical vocabulary to declare it - and dropping
    it leaves the caller an answer they cannot trace to a record.

    The rule is one line of evidence: a name ending in ``_id`` / ``_no`` /
    ``_number`` BEFORE abbreviation expansion. Checking the raw form matters -
    the expansion maps ``code`` and ``key`` onto ``id``, which would make
    ``currency_code`` and ``storage_key`` look like record keys.

    Returns ``None`` rather than guessing when no name says so. An identity
    invented from field ORDER would be wrong the moment a response is
    serialized alphabetically, which is exactly how these arrive.
    """
    for name, _ in fields:
        tokens = split_tokens(name)

        if len(tokens) > 1 and tokens[-1] in IDENTITY_SUFFIXES:
            return name

    return None


def _accept(item: FieldRelevance, reason: str) -> FieldRelevance:
    return FieldRelevance(
        source_field=item.source_field,
        canonical_target=item.canonical_target,
        score=item.score,
        signals=item.signals,
        selected=True,
        reason=reason,
        mandatory=item.mandatory,
    )


def _reject(item: FieldRelevance, reason: str) -> FieldRelevance:
    return FieldRelevance(
        source_field=item.source_field,
        canonical_target=item.canonical_target,
        score=item.score,
        signals=item.signals,
        selected=False,
        reason=reason,
        mandatory=item.mandatory,
    )


class RelevanceScorer:
    """Scores response fields against a caller's question.

    Stateless apart from its weights and vocabulary, so one instance is safe to
    share and two instances with the same configuration always agree.
    """

    def __init__(
        self,
        weights: RelevanceWeights | None = None,
        model: CanonicalTargetModel | None = None,
    ) -> None:
        self.weights = weights or RelevanceWeights()
        self.model = model or DEFAULT_CANONICAL_MODEL

    # -- signals ---------------------------------------------------------

    def canonical_field(self, target: str | None) -> CanonicalField | None:
        """Resolve a mapping target to its canonical definition.

        Accepts both the qualified form (``invoice.amount``) and the bare form
        (``amount``), because ``FieldMapping.target_field`` persists the bare
        name while ``MappingCandidate`` reports the qualified one.
        """
        if not target:
            return None

        if "." in target:
            return self.model.field_by_qualified_name(target)

        for field in self.model.iter_fields():
            if field.name == target:
                return field

        return None

    def _entity_signal(
        self, field: CanonicalField | None, entity_type: str | None
    ) -> float:
        """Whether this field belongs to the entity the response is about.

        Corroborating rather than discriminating: for a well-mapped
        single-entity response this term is identical across fields and
        therefore cannot change their ORDER - it raises the response's absolute
        score, which is what decides how much of it clears the threshold. It
        earns its place when a response mixes entities, where a stray
        ``customer.*`` field inside an invoice response should not outrank the
        invoice's own fields.
        """
        if field is None:
            return 0.0

        if entity_type is None:
            return 0.5

        return 1.0 if field.entity_type == entity_type else 0.25

    def score_field(
        self,
        source_field: str,
        canonical_target: str | None,
        tokens: Sequence[str],
        entity_type: str | None,
    ) -> tuple[float, dict[str, float]]:
        """One field's score and the signals behind it."""
        canonical = self.canonical_field(canonical_target)
        ignore = entity_tokens(entity_type)

        signals = {
            "alias": _alias_coverage(canonical, tokens, ignore),
            "name": _coverage(canonical_tokens(source_field), tokens, ignore),
            "entity": self._entity_signal(canonical, entity_type),
            "identity": (
                1.0 if canonical is not None and canonical.is_identifier else 0.0
            ),
        }

        weights = self.weights
        total = weights.alias + weights.name + weights.entity + weights.identity

        if total <= 0:
            return 0.0, signals

        weighted = (
            signals["alias"] * weights.alias
            + signals["name"] * weights.name
            + signals["entity"] * weights.entity
            + signals["identity"] * weights.identity
        )

        return round(weighted / total, 6), signals

    # -- selection -------------------------------------------------------

    def rank(
        self,
        query: str | None,
        fields: Sequence[tuple[str, str | None]],
        entity_type: str | None = None,
        minimum_score: float = 0.15,
        max_fields: int = 24,
        enabled: bool = True,
        blocked_fields: Iterable[str] = (),
    ) -> tuple[FieldRelevance, ...]:
        """Score, order and select fields.

        ``fields`` is ``(source_field, canonical_target)`` pairs; the target is
        ``None`` for a field the mapping engine could not place, which still
        gets scored on its literal name so an unmapped-but-clearly-asked-for
        field is not lost.

        Returns EVERY field, selected or not. The rejected ones carry their
        score and their reason, because "which fields did you throw away, and
        why" is the question this phase has to be able to answer.
        """
        blocked = {name.lower() for name in blocked_fields}
        tokens = query_tokens(query)
        broad = is_broad_query(tokens)

        if broad:
            # The caller asked for the record, not for a field in it. Scoring
            # proceeds so the report still explains each field, but nothing is
            # dropped for irrelevance.
            tokens = ()

        scored: list[FieldRelevance] = []

        for source_field, target in fields:
            canonical = self.canonical_field(target)
            score, signals = self.score_field(
                source_field, target, tokens, entity_type
            )

            if source_field.lower() in blocked:
                # A policy block outranks everything, mandatory included: the
                # caller has said this field must not leave the system.
                scored.append(
                    FieldRelevance(
                        source_field=source_field,
                        canonical_target=target,
                        score=score,
                        signals=signals,
                        selected=False,
                        reason=REASON_BLOCKED_FIELD,
                        mandatory=False,
                    )
                )
                continue

            scored.append(
                FieldRelevance(
                    source_field=source_field,
                    canonical_target=target,
                    score=score,
                    signals=signals,
                    selected=False,
                    reason=REASON_BELOW_THRESHOLD,
                    mandatory=(
                        canonical is not None and canonical.is_identifier
                    ),
                )
            )

        if not any(item.mandatory for item in scored):
            inferred = infer_identity_field(
                [(item.source_field, item.canonical_target) for item in scored]
            )

            if inferred is not None:
                scored = [
                    FieldRelevance(
                        source_field=item.source_field,
                        canonical_target=item.canonical_target,
                        score=item.score,
                        signals=item.signals,
                        selected=item.selected,
                        reason=item.reason,
                        mandatory=True,
                    )
                    if item.source_field == inferred
                    and item.reason != REASON_BLOCKED_FIELD
                    else item
                    for item in scored
                ]

        # Deterministic order: mandatory first, then score, then name. The name
        # tie-break is what stops two equally-scored fields swapping places
        # between runs - an evaluation cannot be reproducible without it.
        ordered = sorted(
            scored,
            key=lambda item: (not item.mandatory, -item.score, item.source_field),
        )

        decided: list[FieldRelevance] = []
        budget = max(0, max_fields)
        kept = 0

        for item in ordered:
            if item.reason == REASON_BLOCKED_FIELD:
                decided.append(item)
                continue

            if item.mandatory:
                # Checked BEFORE the budget. An identity field is what lets a
                # caller trace an answer back to the record it came from, and a
                # traceless answer is worse than a long one. Mandatory fields
                # still count against the budget, so they shrink what is left
                # for business fields rather than being free.
                decided.append(_accept(item, REASON_MANDATORY))
                kept += 1
                continue

            if kept >= budget:
                decided.append(_reject(item, REASON_FIELD_BUDGET))
                continue

            if not enabled:
                decided.append(_accept(item, REASON_SELECTION_DISABLED))
                kept += 1
                continue

            if not tokens:
                # No question - or a question asking for everything - means no
                # basis for preferring one field over another. Dropping fields
                # here would be arbitrary, so the engine keeps them and says
                # which of the two situations applied.
                decided.append(
                    _accept(
                        item, REASON_BROAD_QUERY if broad else REASON_NO_QUERY
                    )
                )
                kept += 1
                continue

            if item.score >= minimum_score:
                decided.append(_accept(item, REASON_SELECTED))
                kept += 1
            else:
                decided.append(_reject(item, REASON_BELOW_THRESHOLD))

        if enabled and tokens and not _has_business_field(decided):
            # NO-SIGNAL FALLBACK
            # The question mentioned nothing this response contains - "give me
            # everything about this invoice", or vocabulary the lexicon does
            # not cover. Returning the identity field alone would be a
            # confidently wrong answer: the caller learns which record it is
            # and nothing else, and has no way to tell an empty result from a
            # missed match.
            #
            # Falling back to the unfiltered record is the conservative
            # failure: it costs context, which is measurable and bounded by the
            # budgets, instead of costing recall, which is not recoverable
            # downstream. The reason is recorded on every field so the
            # evaluation can count how often the mechanism abstained rather
            # than crediting the fallback as a successful selection.
            return _fallback(decided, budget)

        return tuple(decided)


def _has_business_field(decisions: Sequence[FieldRelevance]) -> bool:
    """Whether anything beyond the record's own identity was selected."""
    return any(item.selected and not item.mandatory for item in decisions)


def _fallback(
    decisions: Sequence[FieldRelevance], budget: int
) -> tuple[FieldRelevance, ...]:
    """Keep everything the budget allows, marked as an abstention."""
    result: list[FieldRelevance] = []
    kept = 0

    for item in decisions:
        if item.reason == REASON_BLOCKED_FIELD:
            result.append(item)
            continue

        if item.selected:
            result.append(item)
            kept += 1
            continue

        if kept >= budget:
            result.append(_reject(item, REASON_FIELD_BUDGET))
            continue

        result.append(_accept(item, REASON_NO_SIGNAL))
        kept += 1

    return tuple(result)


def removal_summary(decisions: Sequence[FieldRelevance]) -> dict[str, int]:
    """How many fields each rejection reason accounted for.

    Reported on every adaptation so a caller sees the shape of what was removed
    without having to read every individual decision.
    """
    summary: dict[str, int] = {}

    for decision in decisions:
        if decision.selected:
            continue

        summary[decision.reason] = summary.get(decision.reason, 0) + 1

    return dict(sorted(summary.items()))


__all__ = [
    "STOPWORDS",
    "REASON_MANDATORY",
    "REASON_SELECTED",
    "REASON_BELOW_THRESHOLD",
    "REASON_FIELD_BUDGET",
    "REASON_NO_QUERY",
    "REASON_NO_SIGNAL",
    "REASON_BROAD_QUERY",
    "REASON_INFERRED_IDENTITY",
    "REASON_SELECTION_DISABLED",
    "REASON_BLOCKED_FIELD",
    "QUERY_INTENT_TERMS",
    "GENERIC_FIELD_TOKENS",
    "IDENTITY_SUFFIXES",
    "BROAD_QUERY_TERMS",
    "entity_tokens",
    "is_broad_query",
    "infer_identity_field",
    "intent_expansions",
    "query_tokens",
    "RelevanceScorer",
    "removal_summary",
]
