"""Shared model-level validation for the contract models.

Every rule lives here rather than inside individual models, so a rule is
defined once and applied identically everywhere.

Scope boundary
--------------
These are *structural* rules: identity is present and well formed, numbers are
in range, timestamps are coherent, payloads are JSON-safe, secrets are absent.

Business-domain rules ("an invoice amount must be positive", "a status must be
one of the customer's workflow states") are deliberately NOT here. Those depend
on the source schema and the mapping, and belong to the future schema, mapping
and data-quality layers. Encoding them into the foundational contract would
make the model unusable for the next ERP domain it meets.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from erp_pipeline.schemas.identity import (
    IdentityError,
    is_normalized_identifier,
    normalize_identifier,
)
from erp_pipeline.schemas.serialization import SerializationError, to_json_value

# Substrings that mark a metadata key as credential-bearing. The contract
# models refuse to carry these at all, which is what makes "no secret ever
# reaches a serialized SourceSystem or provenance record" a checkable property
# rather than a convention someone has to remember.
#
# Matching is substring-based and deliberately blunt: a key such as
# "password_rotated_at" is rejected too. Storing that alongside credentials is
# a category error anyway, and a false rejection is cheap to fix while a leaked
# secret is not.
SECRET_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "connection_string",
    "conn_str",
    "dsn",
    "access_key",
)

# Upper bound on a redacted diagnostic excerpt. Long enough to identify a
# malformed value, short enough that a quality report cannot become a covert
# copy of the source data.
MAX_VALUE_SUMMARY_LENGTH = 200


class ValidationError(ValueError):
    """Raised when a contract model is constructed with invalid content."""


def require_text(value: Any, field_name: str) -> str:
    """Require a non-blank string."""
    if not isinstance(value, str):
        raise ValidationError(
            f"{field_name} must be a string, got {type(value).__name__}."
        )

    if not value.strip():
        raise ValidationError(f"{field_name} must not be blank.")

    return value


def optional_text(value: Any, field_name: str) -> str | None:
    """Allow ``None``; otherwise require a non-blank string."""
    if value is None:
        return None

    return require_text(value, field_name)


def require_identifier(value: Any, field_name: str) -> str:
    """Require a non-blank, fully normalized identifier.

    Rejecting an un-normalized value rather than silently normalizing it keeps
    the caller's stored value and the value used inside composite ids the same
    string. A silent rewrite would make ``record.source.source_system_id`` and
    the ``source_system_id`` embedded in ``record.record_id`` disagree.
    """
    text = require_text(value, field_name)

    if not is_normalized_identifier(text):
        try:
            suggestion = normalize_identifier(text)
        except IdentityError:
            suggestion = None

        hint = f" Did you mean {suggestion!r}?" if suggestion else ""
        raise ValidationError(
            f"{field_name} must be a normalized identifier "
            f"(lower-case, characters from [a-z0-9_.-], no leading or trailing "
            f"underscore), got {text!r}.{hint}"
        )

    return text


def optional_identifier(value: Any, field_name: str) -> str | None:
    """Allow ``None``; otherwise require a normalized identifier."""
    if value is None:
        return None

    return require_identifier(value, field_name)


def require_confidence(value: Any, field_name: str = "confidence") -> float:
    """Require a confidence score in the inclusive range 0.0 - 1.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(
            f"{field_name} must be a number between 0.0 and 1.0, got "
            f"{type(value).__name__}."
        )

    numeric = float(value)

    if numeric != numeric:  # NaN
        raise ValidationError(f"{field_name} must be a number, got NaN.")

    if not 0.0 <= numeric <= 1.0:
        raise ValidationError(
            f"{field_name} must be between 0.0 and 1.0 inclusive, got {numeric}."
        )

    return numeric


def require_non_negative_int(value: Any, field_name: str) -> int:
    """Require a non-negative integer count."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"{field_name} must be an integer, got {type(value).__name__}."
        )

    if value < 0:
        raise ValidationError(f"{field_name} must not be negative, got {value}.")

    return value


def require_aware_datetime(value: Any, field_name: str) -> datetime:
    """Require a timezone-aware datetime.

    A naive datetime is ambiguous: nothing downstream can tell whether it was
    local time or UTC, and guessing produces silently wrong ordering across
    systems in different regions.
    """
    if not isinstance(value, datetime):
        raise ValidationError(
            f"{field_name} must be a datetime, got {type(value).__name__}."
        )

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationError(
            f"{field_name} must be timezone-aware. Use "
            "erp_pipeline.schemas.serialization.utc_now() or attach a tzinfo."
        )

    return value


def optional_aware_datetime(value: Any, field_name: str) -> datetime | None:
    """Allow ``None``; otherwise require a timezone-aware datetime."""
    if value is None:
        return None

    return require_aware_datetime(value, field_name)


def require_ordered_times(
    started_at: datetime | None,
    completed_at: datetime | None,
    start_field: str = "started_at",
    end_field: str = "completed_at",
) -> None:
    """Reject a completion timestamp that precedes its start."""
    if started_at is None or completed_at is None:
        return

    if completed_at < started_at:
        raise ValidationError(
            f"{end_field} ({completed_at.isoformat()}) must not be earlier than "
            f"{start_field} ({started_at.isoformat()})."
        )


def require_json_object(value: Any, field_name: str) -> dict[str, Any]:
    """Require a JSON-safe mapping and return it as a plain dict.

    Used for both ``metadata`` and ``normalized_data``. Returning a copy stops
    a caller mutating a model's payload after construction has validated it.
    """
    if value is None:
        return {}

    if not isinstance(value, Mapping):
        raise ValidationError(
            f"{field_name} must be a mapping/object, got {type(value).__name__}."
        )

    for key in value:
        if not isinstance(key, str):
            raise ValidationError(
                f"{field_name} keys must be strings, got "
                f"{type(key).__name__} ({key!r})."
            )

    try:
        # Converting proves the payload is representable. The result is
        # discarded: models keep the original Python values (Decimal, datetime)
        # and convert at serialization time.
        to_json_value(dict(value), field_name)
    except SerializationError as exc:
        raise ValidationError(str(exc)) from exc

    return dict(value)


def require_metadata(value: Any, field_name: str = "metadata") -> dict[str, Any]:
    """Require JSON-safe metadata that carries no credential-like key."""
    payload = require_json_object(value, field_name)
    reject_secret_keys(payload, field_name)
    return payload


def reject_secret_keys(payload: Mapping[str, Any], field_name: str) -> None:
    """Refuse a payload containing a credential-bearing key, at any depth.

    Connection configuration and credentials belong to a future connector /
    secrets layer. The descriptive models in this package must stay safe to
    serialize, log, diff and publish.
    """
    for key, value in payload.items():
        lowered = str(key).lower()

        for marker in SECRET_KEY_MARKERS:
            if marker in lowered:
                raise ValidationError(
                    f"{field_name} must not contain credentials: key {key!r} "
                    f"looks like a secret ({marker!r}). Connection details and "
                    "credentials belong to a future connector/secrets layer, "
                    "never to a descriptive contract model."
                )

        if isinstance(value, Mapping):
            reject_secret_keys(value, f"{field_name}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    reject_secret_keys(item, f"{field_name}.{key}[{index}]")


def require_value_summary(value: Any, field_name: str) -> str | None:
    """Require a short, bounded diagnostic excerpt.

    Data-quality findings must not become a second copy of the source data, so
    the excerpt is length-bounded. Use ``summarize_value`` to produce a
    compliant excerpt from an arbitrary value.
    """
    if value is None:
        return None

    text = require_text(value, field_name)

    if len(text) > MAX_VALUE_SUMMARY_LENGTH:
        raise ValidationError(
            f"{field_name} must be at most {MAX_VALUE_SUMMARY_LENGTH} characters "
            f"(got {len(text)}). It is a bounded diagnostic excerpt, not a copy "
            "of the original value. Use summarize_value() to truncate safely."
        )

    return text


def summarize_value(
    value: Any,
    max_length: int = MAX_VALUE_SUMMARY_LENGTH,
    redact: bool = False,
) -> str:
    """Produce a bounded, optionally redacted excerpt of an offending value.

    With ``redact=True`` only the shape is reported - type and length - and no
    content at all, which is what a finding on a confidential or restricted
    field should carry.
    """
    if redact:
        length = len(str(value)) if value is not None else 0
        return f"<redacted {type(value).__name__} length={length}>"

    text = "None" if value is None else str(value)

    if len(text) <= max_length:
        return text

    marker = "...[truncated]"
    return text[: max_length - len(marker)] + marker


__all__ = [
    "ValidationError",
    "SECRET_KEY_MARKERS",
    "MAX_VALUE_SUMMARY_LENGTH",
    "require_text",
    "optional_text",
    "require_identifier",
    "optional_identifier",
    "require_confidence",
    "require_non_negative_int",
    "require_aware_datetime",
    "optional_aware_datetime",
    "require_ordered_times",
    "require_json_object",
    "require_metadata",
    "reject_secret_keys",
    "require_value_summary",
    "summarize_value",
]
