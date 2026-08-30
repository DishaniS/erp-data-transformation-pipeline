"""How sensitive a thing is, and which declaration wins (Phase 10).

WHY THIS EXISTS SEPARATELY FROM THE ENUM
----------------------------------------
``SensitivityLevel`` names four handling classes. It does not say which is
*more* restrictive, and until Phase 10 nothing needed to: a record carried one
value from one place.

Phase 10 lets several trusted declarations apply to one artifact - a source
default, a job option, a per-field override - so something must decide. Two
wrong answers were available:

    alphabetical    confidential < internal < public < restricted
    declaration     relies on nobody ever reordering an enum

The first is nonsense; the second is a silent trap. So the order is declared
explicitly, here, with a test that pins it.

THE RULE: MOST RESTRICTIVE WINS
-------------------------------
When two trusted declarations disagree, the stricter one is used. Not the most
specific, not the most recent - the strictest.

The asymmetry is deliberate. Treating restricted data as internal is a
disclosure; treating internal data as restricted is an inconvenience. Those are
not comparable mistakes, so the tie-break goes to the one that cannot leak.

WHAT THIS IS NOT
----------------
Not authorization. Nothing here asks who is calling, what role they hold or
whether they may see a record. Sensitivity is DATA-HANDLING METADATA that
travels with the content so a governance layer can make that decision with
accurate information. Member 4 supplies the label; Member 1 decides what it
permits.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from erp_pipeline.schemas.enums import SensitivityLevel

#: Severity order, least to most restrictive. Declared, not inferred.
#:
#: Written as an explicit tuple rather than relying on the enum's declaration
#: order, so reordering the enum for readability can never silently reorder
#: security decisions.
SENSITIVITY_ORDER: tuple[SensitivityLevel, ...] = (
    SensitivityLevel.PUBLIC,
    SensitivityLevel.INTERNAL,
    SensitivityLevel.CONFIDENTIAL,
    SensitivityLevel.RESTRICTED,
)

_RANK: Mapping[SensitivityLevel, int] = {
    level: index for index, level in enumerate(SENSITIVITY_ORDER)
}

#: The classification an artifact carries when nothing declares one. Unchanged
#: from every earlier phase: changing a default retroactively reclassifies a
#: corpus nobody re-examined.
DEFAULT_SENSITIVITY = SensitivityLevel.INTERNAL


def rank(level: Any) -> int:
    """How restrictive a level is. Higher is stricter."""
    resolved = coerce(level)

    return _RANK[resolved] if resolved is not None else -1


def coerce(value: Any) -> SensitivityLevel | None:
    """A declared value as a level, or ``None`` when nothing was declared.

    Returns ``None`` for absent input so a caller can tell "not declared" from
    "declared public" - which are different facts, and conflating them would
    turn a missing configuration into the least restrictive answer.
    """
    if value is None:
        return None

    if isinstance(value, SensitivityLevel):
        return value

    text = str(value).strip().lower()

    if not text:
        return None

    return SensitivityLevel(text)


def most_restrictive(*values: Any) -> SensitivityLevel | None:
    """The strictest of several declarations. ``None`` when none were made."""
    declared = [level for level in (coerce(value) for value in values) if level]

    if not declared:
        return None

    return max(declared, key=rank)


def resolve(
    *,
    artifact: Any = None,
    job: Any = None,
    source: Any = None,
    inherited: Any = None,
    default: SensitivityLevel = DEFAULT_SENSITIVITY,
) -> SensitivityLevel:
    """The classification to apply, given every declaration that could apply.

    All declarations are considered together and the strictest wins - which is
    NOT the same as a precedence chain where a narrower scope overrides a wider
    one. A per-field ``internal`` must not downgrade a source declared
    ``restricted``: the person who classified the source knew something the
    person who classified the field may not.

    ``inherited`` is the classification a parent artifact already carries - an
    attachment's ERP record, say. It participates on the same terms.
    """
    resolved = most_restrictive(artifact, job, source, inherited)

    return resolved if resolved is not None else default


def field_sensitivity(
    options: Mapping[str, Any] | None, field_name: str
) -> SensitivityLevel | None:
    """A per-field declaration from a job's options, if one was made.

    One ERP row genuinely mixes classes - a name is internal, a birth
    certificate is not - and a single job-level value cannot express that.
    """
    declared = (options or {}).get("field_sensitivity")

    if not isinstance(declared, Mapping):
        return None

    return coerce(declared.get(field_name))


def job_sensitivity(options: Mapping[str, Any] | None) -> SensitivityLevel | None:
    """The job-wide declaration, if one was made."""
    return coerce((options or {}).get("sensitivity"))


def describe(level: Any) -> str:
    """The wire value, for metadata and reports."""
    resolved = coerce(level)

    return resolved.value if resolved else DEFAULT_SENSITIVITY.value


__all__ = [
    "DEFAULT_SENSITIVITY",
    "SENSITIVITY_ORDER",
    "coerce",
    "describe",
    "field_sensitivity",
    "job_sensitivity",
    "most_restrictive",
    "rank",
    "resolve",
]
