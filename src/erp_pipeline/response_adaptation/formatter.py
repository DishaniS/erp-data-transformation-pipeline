"""Assemble the selected fields into a bounded, LLM-ready payload.

WHAT A BUDGET IS FOR
--------------------
Relevance decides what is WORTH sending. A budget decides what FITS. They are
separate steps because they fail differently: a field dropped for irrelevance
is a judgement that can be wrong, while a field dropped for space is an
arithmetic fact. Keeping them apart means the evaluation can attribute a missed
field to the right cause.

TRUNCATION IS ALWAYS ANNOUNCED
------------------------------
A silently shortened payload is worse than a long one, because a model given
half a record has no way to know it. Every cut here sets a flag and names the
fields it removed. Nothing is trimmed quietly.

CHARACTERS, NOT TOKENS
----------------------
There is no tokenizer in this project, and adding one would mean shipping a
model's vocabulary to make a budget decision. Characters are the honest
available proxy: they are exact, they need no dependency, and they are
monotonic in tokens, so a smaller character budget is always a smaller token
budget. The report states the unit so nobody mistakes it for a token count.

WHY SIZE IS MEASURED ON THE ENCODED FORM
----------------------------------------
The cost of a payload is what gets serialized into the prompt, not the size of
the Python objects behind it. Every measurement here goes through the same
``serialized_size`` encoding, so an input and an output are always comparable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from erp_pipeline.response_adaptation.models import (
    AdaptationOptions,
    FieldRelevance,
    serialized_size,
)
from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.schemas.serialization import SerializationError, to_json_value

#: Marker appended to a value that was shortened. Visible on purpose: a model
#: reading a value must be able to tell that it is not the whole value.
TRUNCATION_MARKER = "...[truncated]"

#: Reasons a field left the payload after relevance had already kept it.
REASON_CHARACTER_BUDGET = "character_budget_exhausted"
REASON_SENSITIVITY = "blocked_by_sensitivity"


@dataclass(frozen=True)
class FormattedPayload:
    """The business payload, plus an account of what it cost to fit."""

    payload: dict[str, Any]
    truncated: bool = False
    #: Fields relevance had selected but the character budget removed.
    dropped_fields: tuple[str, ...] = ()
    #: Fields kept, but with a shortened value.
    clipped_fields: tuple[str, ...] = ()
    #: Fields withheld because the response's sensitivity is blocked.
    withheld_fields: tuple[str, ...] = ()

    @property
    def field_count(self) -> int:
        return len(self.payload)


def _bare_target(target: str | None) -> str | None:
    """``invoice.amount`` -> ``amount``.

    The canonical record stores bare names; the qualified form only exists for
    explanation.
    """
    if not target:
        return None

    return target.split(".", 1)[1] if "." in target else target


def _clip_value(value: Any, limit: int) -> tuple[Any, bool]:
    """Shorten one oversized value, leaving non-strings alone.

    Only strings are clipped. Truncating a number would change its meaning
    rather than its length - ``45000.00`` cut to ``450`` is not a shorter
    amount, it is a wrong one.
    """
    if not isinstance(value, str) or len(value) <= limit:
        return value, False

    keep = max(0, limit - len(TRUNCATION_MARKER))

    return value[:keep] + TRUNCATION_MARKER, True


def build_payload(
    decisions: Sequence[FieldRelevance],
    canonical_data: Mapping[str, Any] | None,
    source_record: Mapping[str, Any],
    options: AdaptationOptions,
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
) -> FormattedPayload:
    """Turn selected field decisions into the payload an LLM receives.

    Keys are CANONICAL where a mapping exists and the source name otherwise.
    That is what makes the output comparable across ERP systems - two vendors
    spelling the same concept ``cust_ref`` and ``KUNNR`` both emit
    ``customer_id`` - while a field the canonical model has no word for is
    still passed through under its own name rather than being lost.

    SENSITIVITY IS CONSUMED, NEVER INFERRED. This function reads the
    classification the response already carries and applies the caller's
    policy to it. It does not examine values to decide whether they look
    sensitive; guessing would produce a classification nothing else in the
    pipeline agrees with.
    """
    if sensitivity in options.policy.blocked_sensitivities:
        # The whole response is classified above what this caller may receive.
        # The field names are still reported - a caller learns that data exists
        # and was withheld, which is a different fact from it not existing.
        withheld = tuple(
            decision.source_field for decision in decisions if decision.selected
        )

        return FormattedPayload(
            payload={},
            truncated=bool(withheld),
            withheld_fields=withheld,
        )

    payload: dict[str, Any] = {}
    clipped: list[str] = []
    order: list[str] = []

    for decision in decisions:
        if not decision.selected:
            continue

        bare = _bare_target(decision.canonical_target)

        if bare and canonical_data and bare in canonical_data:
            key, value = bare, canonical_data[bare]
        else:
            key, value = decision.source_field, source_record.get(
                decision.source_field
            )

        if value is None and key not in source_record:
            # The mapping named a target the transformation dropped (a value
            # that failed validation, say). Emitting a null would assert the
            # field is empty, which is a different claim from absent.
            continue

        # JSON-safe BEFORE clipping and measuring. The transformation layer
        # hands back Decimals and datetimes, and a Decimal that reaches the API
        # serializer unconverted is an error at the edge rather than here.
        # ``to_json_value`` is the project's existing converter, so money keeps
        # the exact string form it already uses everywhere else.
        try:
            value = to_json_value(value, key)
        except SerializationError:
            # A value no encoding can represent (NaN, a raw object). Naming it
            # is more useful than dropping it silently or emitting a null that
            # would read as "this field is empty".
            value = f"<unrepresentable {type(value).__name__}>"

        value, was_clipped = _clip_value(value, options.max_value_characters)

        if was_clipped:
            clipped.append(key)

        payload[key] = value
        order.append(key)

    truncated = bool(clipped)
    dropped: list[str] = []

    # Character budget. Fields are removed from the END of the relevance
    # ordering, so the least relevant thing goes first and identity fields -
    # which sort first - are the last to be affected.
    while order and serialized_size(payload) > options.max_output_characters:
        victim = order.pop()
        payload.pop(victim, None)
        dropped.append(victim)
        truncated = True

    return FormattedPayload(
        payload=payload,
        truncated=truncated,
        dropped_fields=tuple(dropped),
        clipped_fields=tuple(clipped),
    )


def apply_budget_to_decisions(
    decisions: Sequence[FieldRelevance], formatted: FormattedPayload
) -> tuple[FieldRelevance, ...]:
    """Rewrite decisions so the report matches what was actually emitted.

    Without this, a field the budget removed would still be reported as
    ``selected``, and the field count in the metrics would disagree with the
    payload. The report has to describe the output that exists, not the one
    relevance intended.
    """
    if not formatted.dropped_fields and not formatted.withheld_fields:
        return tuple(decisions)

    dropped = set(formatted.dropped_fields)
    withheld = set(formatted.withheld_fields)
    rewritten: list[FieldRelevance] = []

    for decision in decisions:
        target = _bare_target(decision.canonical_target)
        was_dropped = decision.source_field in dropped or (
            target is not None and target in dropped
        )

        if decision.selected and decision.source_field in withheld:
            reason = REASON_SENSITIVITY
        elif decision.selected and was_dropped:
            reason = REASON_CHARACTER_BUDGET
        else:
            rewritten.append(decision)
            continue

        rewritten.append(
            FieldRelevance(
                source_field=decision.source_field,
                canonical_target=decision.canonical_target,
                score=decision.score,
                signals=decision.signals,
                selected=False,
                reason=reason,
                mandatory=decision.mandatory,
            )
        )

    return tuple(rewritten)


def limit_decisions(
    decisions: Sequence[FieldRelevance], limit: int
) -> tuple[tuple[FieldRelevance, ...], bool]:
    """Cap how many per-field decisions the report carries.

    An explanation must not become a second copy of the payload. Selected
    fields are kept first, because "why was this included" is answerable from
    the payload itself while "why was this dropped" is not - so when the report
    must be shortened, the rejections are the more valuable half to keep.
    """
    if len(decisions) <= limit:
        return tuple(decisions), False

    rejected = [item for item in decisions if not item.selected]
    selected = [item for item in decisions if item.selected]

    kept = (rejected + selected)[:limit]

    # Restore the original ordering so the report is not reshuffled.
    index = {id(item): position for position, item in enumerate(decisions)}

    return tuple(sorted(kept, key=lambda item: index[id(item)])), True


__all__ = [
    "TRUNCATION_MARKER",
    "REASON_CHARACTER_BUDGET",
    "REASON_SENSITIVITY",
    "FormattedPayload",
    "build_payload",
    "apply_budget_to_decisions",
    "limit_decisions",
]
