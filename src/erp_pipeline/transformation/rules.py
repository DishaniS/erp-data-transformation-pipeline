"""Execution of declared ``TransformationRule`` steps.

SAFETY (Step 18)
----------------
A transformation is an operation NAME plus a JSON configuration - never a code
string. This module dispatches on the frozen ``TransformationOperation`` enum
through a closed registry. There is no ``eval``, no ``exec``, no ``compile``,
no ``import``, no ``getattr`` on caller-supplied names and no callable anywhere
in a rule's configuration - ``TransformationRule.config`` is validated as a
JSON object by Phase 1, which structurally excludes callables before this
module ever sees it.

An operation the engine cannot execute raises ``UnsupportedOperationError``
rather than being skipped. Silently ignoring a step a mapping author declared
would produce records that look successful and are wrong.

ONLY THE FROZEN OPERATIONS
--------------------------
Exactly the twelve members of ``TransformationOperation`` are implemented.
Operations the phase brief mentions conceptually but the frozen enum does not
declare - ``lowercase``, ``uppercase``, ``coalesce`` - are deliberately NOT
added to it. Amending a frozen Phase 1 contract to suit this phase is exactly
what the brief forbids, so those capabilities are delivered as run
configuration instead: case folding through ``NormalizationPolicy``, coalesce
through ``ComputedField``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from erp_pipeline.schemas.enums import FieldDataType, TransformationOperation
from erp_pipeline.schemas.mapping_models import TransformationRule
from erp_pipeline.transformation.errors import UnsupportedOperationError
from erp_pipeline.transformation.models import (
    IssueCode,
    TransformationOptions,
)
from erp_pipeline.transformation import type_converter

#: Mask written by REDACT. Fixed rather than value-derived, so a redacted field
#: leaks neither content nor length.
REDACTION_MASK = "[REDACTED]"


@dataclass(frozen=True)
class RuleContext:
    """What a rule may read besides the value it is transforming.

    ``source_values`` is needed by CONCAT and NESTED_PATH, which by definition
    reach beyond a single field. Nothing here is mutable, so a rule cannot
    affect another field's transformation.
    """

    source_values: Mapping[str, Any]
    options: TransformationOptions
    target_type: FieldDataType | None = None


@dataclass(frozen=True)
class RuleResult:
    ok: bool
    value: Any = None
    reason: str | None = None
    code: IssueCode | None = None

    @classmethod
    def success(cls, value: Any) -> "RuleResult":
        return cls(ok=True, value=value)

    @classmethod
    def failure(
        cls, reason: str, code: IssueCode = IssueCode.RULE_EXECUTION_FAILED
    ) -> "RuleResult":
        return cls(ok=False, reason=reason, code=code)


def apply_rules(
    value: Any,
    rules: Sequence[TransformationRule],
    context: RuleContext,
) -> RuleResult:
    """Run every declared rule in order, stopping at the first failure.

    Order is the mapping author's declared order and is never rearranged: a
    ``trim`` before a ``cast`` and a ``cast`` before a ``trim`` are different
    instructions, and the engine is not entitled to decide which was meant.
    """
    current = value

    for rule in rules:
        result = apply_rule(current, rule, context)
        if not result.ok:
            return result
        current = result.value

    return RuleResult.success(current)


def apply_rule(
    value: Any, rule: TransformationRule, context: RuleContext
) -> RuleResult:
    """Execute one declared rule."""
    handler = _REGISTRY.get(rule.operation)

    if handler is None:
        raise UnsupportedOperationError(
            f"Transformation operation {rule.operation.value!r} is declared in "
            "a mapping profile but is not implemented by this engine. "
            "Refusing to skip it, because ignoring a declared transformation "
            "would produce a record that looks correct and is not.",
            operation=rule.operation.value,
        )

    return handler(value, dict(rule.config), context)


# ============================================================
# Operations
# ============================================================

def _op_copy(value: Any, config: dict, context: RuleContext) -> RuleResult:
    return RuleResult.success(value)


def _op_rename(value: Any, config: dict, context: RuleContext) -> RuleResult:
    """A no-op on the VALUE.

    Renaming is expressed by ``FieldMapping.target_field``; the rule exists so
    a profile can record that a rename was the intent. Changing the value here
    would be wrong.
    """
    return RuleResult.success(value)


def _op_cast(value: Any, config: dict, context: RuleContext) -> RuleResult:
    target = config.get("to")

    if target is None:
        return RuleResult.failure(
            "a cast rule declares no target type in config['to']"
        )

    try:
        data_type = FieldDataType.from_value(target)
    except ValueError:
        return RuleResult.failure(
            f"a cast rule names an unknown target type {str(target)!r}"
        )

    result = type_converter.convert(value, data_type, context.options)

    if not result.ok:
        return RuleResult.failure(
            result.reason or "the declared cast could not be applied",
            result.code or IssueCode.TYPE_CONVERSION_FAILED,
        )

    return RuleResult.success(result.value)


def _op_default(value: Any, config: dict, context: RuleContext) -> RuleResult:
    """Substitute a declared value when there is nothing to transform.

    Applies to ``None`` ONLY. A conversion failure never reaches a default
    (Step 15): ``amount = "hello"`` must not become ``0`` because a default
    exists, so the default is applied before conversion, not as a rescue after
    it.
    """
    if value is not None:
        return RuleResult.success(value)

    if "value" not in config:
        return RuleResult.failure(
            "a default rule declares no config['value']"
        )

    return RuleResult.success(config["value"])


def _op_enum_map(value: Any, config: dict, context: RuleContext) -> RuleResult:
    """Explicit, deterministic code translation (Step 16).

    Exact lookup only. No case-insensitive fallback, no trimming, no fuzzy
    match - an enum table is a declaration that these exact codes mean these
    exact things, and guessing beyond it defeats the point.
    """
    mapping = config.get("values")

    if not isinstance(mapping, Mapping):
        return RuleResult.failure(
            "an enum_map rule declares no config['values'] object"
        )

    if value is None:
        return RuleResult.success(None)

    key = value if isinstance(value, str) else str(value)

    if key in mapping:
        return RuleResult.success(mapping[key])

    on_unknown = config.get("on_unknown", "issue")

    if on_unknown == "fallback":
        if "fallback" not in config:
            return RuleResult.failure(
                "an enum_map rule requests a fallback but declares none"
            )
        return RuleResult.success(config["fallback"])

    if on_unknown == "passthrough":
        return RuleResult.success(value)

    return RuleResult.failure(
        "the source code is not declared in the enum mapping; "
        f"{len(mapping)} code(s) are declared",
        IssueCode.UNKNOWN_ENUM_VALUE,
    )


def _op_nested_path(value: Any, config: dict, context: RuleContext) -> RuleResult:
    """Re-read the value from a declared path in the source record."""
    path = config.get("path")

    if not isinstance(path, (list, tuple)) or not path:
        return RuleResult.failure(
            "a nested_path rule declares no config['path'] list"
        )

    current: Any = context.source_values

    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            return RuleResult.failure(
                f"the declared nested path is not present in the source record "
                f"(stopped at segment {str(segment)!r})",
                IssueCode.SOURCE_FIELD_MISSING,
            )
        current = current[segment]

    return RuleResult.success(current)


def _op_date_parse(value: Any, config: dict, context: RuleContext) -> RuleResult:
    """Parse a date/datetime using an EXPLICITLY declared format (Step 13).

    This is the sanctioned route for ambiguous forms: ``03/04/2026`` is
    unreadable without a format, and a rule declaring ``%d/%m/%Y`` supplies the
    missing evidence.
    """
    if value is None:
        return RuleResult.success(None)

    fmt = config.get("format")

    if not isinstance(fmt, str) or not fmt:
        return RuleResult.failure(
            "a date_parse rule declares no config['format'] string"
        )

    if not isinstance(value, str):
        return RuleResult.failure(
            f"a date_parse rule needs text to parse, got "
            f"{type(value).__name__}"
        )

    try:
        parsed = datetime.strptime(value.strip(), fmt)
    except ValueError:
        return RuleResult.failure(
            "the source text does not match the declared date format",
            IssueCode.TYPE_CONVERSION_FAILED,
        )

    wanted = config.get("to")

    if wanted is None:
        wanted = (
            "date"
            if context.target_type is FieldDataType.DATE
            else "datetime"
        )

    if wanted == "date":
        return RuleResult.success(parsed.date())

    if wanted == "datetime":
        if parsed.tzinfo is None:
            if not context.options.date_policy.assume_utc_when_naive:
                return RuleResult.failure(
                    "the parsed datetime carries no timezone and assuming UTC "
                    "is disabled by configuration"
                )
            parsed = parsed.replace(tzinfo=timezone.utc)
        return RuleResult.success(parsed.astimezone(timezone.utc))

    return RuleResult.failure(
        f"a date_parse rule names an unknown config['to'] value {str(wanted)!r}"
    )


def _op_concat(value: Any, config: dict, context: RuleContext) -> RuleResult:
    """Join several source fields into one string."""
    fields = config.get("fields")

    if not isinstance(fields, (list, tuple)) or not fields:
        return RuleResult.failure(
            "a concat rule declares no config['fields'] list"
        )

    separator = config.get("separator", "")

    if not isinstance(separator, str):
        return RuleResult.failure(
            "a concat rule's config['separator'] must be text"
        )

    parts: list[str] = []

    for name in fields:
        if name not in context.source_values:
            if config.get("skip_missing") is True:
                continue
            return RuleResult.failure(
                f"a concat rule needs source field {str(name)!r}, which the "
                "record does not contain",
                IssueCode.SOURCE_FIELD_MISSING,
            )
        item = context.source_values[name]
        if item is None:
            if config.get("skip_missing") is True:
                continue
            return RuleResult.failure(
                f"a concat rule needs source field {str(name)!r}, which is null",
                IssueCode.SOURCE_VALUE_NULL,
            )
        parts.append(item if isinstance(item, str) else str(item))

    return RuleResult.success(separator.join(parts))


def _op_split(value: Any, config: dict, context: RuleContext) -> RuleResult:
    """Take one component out of a delimited string."""
    if value is None:
        return RuleResult.success(None)

    if not isinstance(value, str):
        return RuleResult.failure(
            f"a split rule needs text, got {type(value).__name__}"
        )

    separator = config.get("separator")

    if not isinstance(separator, str) or not separator:
        return RuleResult.failure(
            "a split rule declares no config['separator'] string"
        )

    parts = value.split(separator)
    index = config.get("index", 0)

    if not isinstance(index, int) or isinstance(index, bool):
        return RuleResult.failure(
            "a split rule's config['index'] must be an integer"
        )

    if index >= len(parts) or index < -len(parts):
        return RuleResult.failure(
            f"a split rule asked for component {index} but the value has "
            f"{len(parts)} component(s)"
        )

    return RuleResult.success(parts[index])


def _op_trim(value: Any, config: dict, context: RuleContext) -> RuleResult:
    if value is None or not isinstance(value, str):
        return RuleResult.success(value)

    characters = config.get("characters")

    if characters is not None and not isinstance(characters, str):
        return RuleResult.failure(
            "a trim rule's config['characters'] must be text"
        )

    return RuleResult.success(
        value.strip() if characters is None else value.strip(characters)
    )


def _op_constant(value: Any, config: dict, context: RuleContext) -> RuleResult:
    """Always produce the declared value, whatever the source said."""
    if "value" not in config:
        return RuleResult.failure(
            "a constant rule declares no config['value']"
        )

    return RuleResult.success(config["value"])


def _op_redact(value: Any, config: dict, context: RuleContext) -> RuleResult:
    """Replace the value with a fixed mask.

    The mask is constant, so a redacted field reveals neither the content nor
    its length. ``None`` stays ``None``: masking an absent value would invent
    the appearance of data.
    """
    if value is None:
        return RuleResult.success(None)

    mask = config.get("mask", REDACTION_MASK)

    if not isinstance(mask, str):
        return RuleResult.failure("a redact rule's config['mask'] must be text")

    return RuleResult.success(mask)


#: The closed registry. Every member of the frozen enum is present, so
#: ``UnsupportedOperationError`` can only fire if the enum grows.
_REGISTRY = {
    TransformationOperation.COPY: _op_copy,
    TransformationOperation.RENAME: _op_rename,
    TransformationOperation.CAST: _op_cast,
    TransformationOperation.DEFAULT: _op_default,
    TransformationOperation.ENUM_MAP: _op_enum_map,
    TransformationOperation.NESTED_PATH: _op_nested_path,
    TransformationOperation.DATE_PARSE: _op_date_parse,
    TransformationOperation.CONCAT: _op_concat,
    TransformationOperation.SPLIT: _op_split,
    TransformationOperation.TRIM: _op_trim,
    TransformationOperation.CONSTANT: _op_constant,
    TransformationOperation.REDACT: _op_redact,
}


def supported_operations() -> tuple[str, ...]:
    """The operations this engine can execute, for documentation and tests."""
    return tuple(sorted(operation.value for operation in _REGISTRY))


__all__ = [
    "REDACTION_MASK",
    "RuleContext",
    "RuleResult",
    "apply_rule",
    "apply_rules",
    "supported_operations",
]
