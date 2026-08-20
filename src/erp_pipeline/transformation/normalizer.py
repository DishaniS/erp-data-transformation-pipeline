"""Deterministic post-conversion string normalization.

EVERYTHING IS OPT-IN
--------------------
Normalization runs only where a caller declared it (Step 17). The reason is
narrow and important: business identifiers must not be mutated by accident.
Lower-casing ``AB-001`` changes a primary key; stripping a space inside a name
changes a person's name. Both are the kind of corruption that surfaces months
later as a broken join, and neither is recoverable from the canonical record.

So ``NormalizationPolicy`` defaults to doing nothing at all, and a caller may
additionally scope it to named canonical fields.

Applies to strings only. Normalizing a Decimal or a datetime is meaningless -
their canonical form was already fixed by conversion.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from erp_pipeline.transformation.models import (
    CaseNormalization,
    NormalizationPolicy,
)

_INTERNAL_WHITESPACE = re.compile(r"\s+")


def normalize_value(
    value: Any, target_field: str, policy: NormalizationPolicy
) -> Any:
    """Apply the declared normalization to one converted value.

    Order is fixed and documented, because a different order gives different
    output: Unicode form first (so later comparisons see composed characters),
    then internal whitespace, then trim, then case.
    """
    if policy.is_noop or not isinstance(value, str):
        return value

    if not policy.applies_to(target_field):
        return value

    result = value

    if policy.unicode_form is not None:
        result = unicodedata.normalize(policy.unicode_form, result)

    if policy.collapse_internal_whitespace:
        result = _INTERNAL_WHITESPACE.sub(" ", result)

    if policy.trim_strings:
        result = result.strip()

    if policy.case is CaseNormalization.LOWER:
        result = result.lower()
    elif policy.case is CaseNormalization.UPPER:
        result = result.upper()

    return result


__all__ = [
    "normalize_value",
]
