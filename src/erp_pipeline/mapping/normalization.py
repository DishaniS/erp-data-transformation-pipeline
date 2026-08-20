"""Deterministic field- and entity-name normalization.

Turns the many spellings ERP systems use for one concept into comparable token
forms::

    customer_id   customerId   CustomerID   customer-id   CUSTOMER ID
        -> tokens ("customer", "id")

This is used for SCORING ONLY. ``SourceField.source_name`` and
``normalized_name`` are never modified: Phase 1 identity is frozen, and a
mapping engine that rewrote the identity of the thing it was describing would
break every downstream reference to it.

Determinism
-----------
No randomness, no learned model, no external service. The same string always
produces the same tokens, and the similarity metric is plain Jaccard over token
sets - bounded to [0, 1], trivially explainable, and testable by hand.

Abbreviations
-------------
``cust`` -> ``customer`` is a real and useful expansion, but it is applied only
from an EXPLICIT, configurable table. An open-ended stemmer would make matches
unpredictable and unexplainable, which is the opposite of what this phase is
for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

#: Splits camelCase and PascalCase while keeping acronym runs intact:
#: ``customerID`` -> ``customer`` + ``ID``; ``XMLHttpRequest`` -> ``XML`` +
#: ``Http`` + ``Request``.
_CAMEL_BOUNDARY = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)

#: Every separator ERP exports use between words.
_SEPARATORS = re.compile(r"[\s_\-./\\:]+")

#: Anything that is not alphanumeric survives only as a separator.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: Digits glued to a word (``line1`` -> ``line`` + ``1``).
_DIGIT_BOUNDARY = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")

#: Tokens that carry no distinguishing meaning in an ERP field or table name.
#: Dropped only when a name has other tokens left, so a field literally called
#: ``tbl`` still normalizes to something.
DEFAULT_NOISE_TOKENS: frozenset[str] = frozenset(
    {"tbl", "table", "the", "a", "an", "of", "erp", "col", "column", "fld",
     "field", "master", "mst", "dim", "fact", "stg", "staging", "tmp", "temp"}
)

#: Explicit, reviewable abbreviation expansions. Deliberately small: each entry
#: is a claim that two spellings mean the same thing, and a wrong entry
#: silently corrupts every mapping that touches it.
DEFAULT_ABBREVIATIONS: Mapping[str, str] = {
    "cust": "customer",
    "custs": "customer",
    "clnt": "client",
    "inv": "invoice",
    "invc": "invoice",
    "no": "number",
    "num": "number",
    "nbr": "number",
    "amt": "amount",
    "qty": "quantity",
    "desc": "description",
    "addr": "address",
    "tel": "telephone",
    "ph": "phone",
    "dt": "date",
    "ts": "timestamp",
    "ref": "reference",
    "id": "id",
    "ident": "id",
    "po": "purchase_order",
    "sup": "supplier",
    "supp": "supplier",
    "vend": "vendor",
    "ccy": "currency",
    "curr": "currency",
    "stat": "status",
    "tot": "total",
}

#: Token pairs treated as interchangeable when comparing. Unlike an
#: abbreviation, neither side is "the expansion" - they are synonyms, and the
#: table is symmetric.
DEFAULT_SYNONYMS: Mapping[str, str] = {
    "mail": "email",
    "e": "email",           # from "e_mail" -> ("e", "mail")
    "telephone": "phone",
    "mobile": "phone",
    "client": "customer",
    "buyer": "customer",
    "vendor": "supplier",
    "sum": "total",
    "value": "amount",
    "state": "status",
    "code": "id",
    "key": "id",
    "identifier": "id",
}


@dataclass(frozen=True)
class NormalizationConfig:
    """Explicit, versioned normalization rules.

    ``version`` participates in the mapping engine's configuration identity, so
    a mapping generated before an abbreviation was added is distinguishable
    from one generated after (Step 29).
    """

    version: str = "1.0"
    expand_abbreviations: bool = True
    apply_synonyms: bool = True
    drop_noise_tokens: bool = True
    abbreviations: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_ABBREVIATIONS))
    synonyms: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_SYNONYMS))
    noise_tokens: frozenset[str] = DEFAULT_NOISE_TOKENS

    def fingerprint(self) -> str:
        """Stable description of these rules, for the engine's config identity."""
        return (
            f"norm@{self.version}"
            f"/abbr={int(self.expand_abbreviations)}"
            f"/syn={int(self.apply_synonyms)}"
            f"/noise={int(self.drop_noise_tokens)}"
        )


DEFAULT_NORMALIZATION = NormalizationConfig()


def split_tokens(name: str) -> tuple[str, ...]:
    """Split a raw name into lower-case word tokens.

    Handles every convention the six supported source technologies produce::

        customer_id      -> ("customer", "id")
        customerId       -> ("customer", "id")
        CustomerID       -> ("customer", "id")
        customer-id      -> ("customer", "id")
        "CUSTOMER ID"    -> ("customer", "id")
        customer.contact -> ("customer", "contact")
        totalAmount      -> ("total", "amount")
        line1            -> ("line", "1")
    """
    if not name:
        return ()

    spaced = _SEPARATORS.sub(" ", name)
    spaced = _CAMEL_BOUNDARY.sub(" ", spaced)
    lowered = spaced.lower()
    lowered = _NON_ALNUM.sub(" ", lowered)
    lowered = _DIGIT_BOUNDARY.sub(" ", lowered)

    return tuple(token for token in lowered.split() if token)


def canonical_tokens(
    name: str, config: NormalizationConfig | None = None
) -> tuple[str, ...]:
    """Split, then apply the configured expansions.

    Order matters: abbreviations expand first (``cust`` -> ``customer``), then
    synonyms fold (``client`` -> ``customer``), so ``clnt`` reaches
    ``customer`` in two documented hops rather than needing an entry for every
    combination.
    """
    config = config or DEFAULT_NORMALIZATION
    tokens = list(split_tokens(name))

    if config.expand_abbreviations:
        expanded: list[str] = []
        for token in tokens:
            replacement = config.abbreviations.get(token, token)
            # An expansion may itself be multi-token ("po" -> "purchase_order").
            expanded.extend(split_tokens(replacement) if "_" in replacement
                            else [replacement])
        tokens = expanded

    if config.apply_synonyms:
        tokens = [config.synonyms.get(token, token) for token in tokens]

    if config.drop_noise_tokens:
        filtered = [token for token in tokens if token not in config.noise_tokens]
        # Never normalize a name away entirely.
        tokens = filtered or tokens

    return tuple(tokens)


def normalized_key(name: str, config: NormalizationConfig | None = None) -> str:
    """A comparable single-string form: ``customerId`` -> ``customer_id``.

    Used for exact-after-normalization matching, which is the strongest
    evidence short of a literal string match.
    """
    return "_".join(canonical_tokens(name, config))


def token_similarity(
    left: Sequence[str], right: Sequence[str]
) -> float:
    """Jaccard similarity over token sets, in [0, 1].

    Chosen deliberately over an edit-distance or a learned metric: it is
    order-insensitive (so ``total_amount`` and ``amount_total`` match), it is
    trivially explainable to a reviewer ("2 of 3 tokens shared"), and it is
    bounded and deterministic. Its known weakness - it ignores token order and
    treats all tokens as equally important - is acceptable because it is only
    one weighted component of the score, never the whole of it.
    """
    left_set = set(left)
    right_set = set(right)

    if not left_set or not right_set:
        return 0.0

    intersection = left_set & right_set
    union = left_set | right_set

    return round(len(intersection) / len(union), 6)


def shared_tokens(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    """The tokens two names have in common, sorted for deterministic output.

    Reported as evidence so a reviewer sees WHICH words matched, not just a
    number.
    """
    return tuple(sorted(set(left) & set(right)))


def path_tokens(
    nested_path: Sequence[str] | None,
    source_name: str,
    config: NormalizationConfig | None = None,
) -> tuple[str, ...]:
    """Tokens of a field's full access path, array markers dropped.

    ``("customer", "contact")`` + ``email`` becomes
    ``("customer", "contact", "email")``. The ``[]`` element marker that
    Phases 5-7 use is structural punctuation and carries no naming signal, so
    it is excluded.
    """
    segments = [
        segment for segment in (nested_path or ()) if segment and segment != "[]"
    ]
    segments.append(source_name)

    tokens: list[str] = []
    for segment in segments:
        tokens.extend(canonical_tokens(segment, config))

    return tuple(tokens)


def dedupe_preserving_order(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []

    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)

    return tuple(result)


__all__ = [
    "DEFAULT_NOISE_TOKENS",
    "DEFAULT_ABBREVIATIONS",
    "DEFAULT_SYNONYMS",
    "NormalizationConfig",
    "DEFAULT_NORMALIZATION",
    "split_tokens",
    "canonical_tokens",
    "normalized_key",
    "token_similarity",
    "shared_tokens",
    "path_tokens",
    "dedupe_preserving_order",
]
