"""Deterministic conversion of a source value into a canonical field's type.

THE ONE PLACE CONVERSION HAPPENS
--------------------------------
The transformer converts; the validator checks the result. Neither duplicates
the other's logic (Step 8). If conversion succeeded, the validator's type check
is a cheap confirmation - and when it ever fails, that is a genuine engine bug
worth surfacing rather than a second opinion.

THE GOVERNING RULE
------------------
A conversion either produces a value that means exactly what the source meant,
or it FAILS. There is no third option, and in particular there is no
"best effort" path that returns a plausible-looking number. ``"25.9"`` does not
become ``25``; ``"hello"`` does not become ``0``; ``"approved"`` does not become
``True``.

MONEY
-----
Decimal, never float. ``float("2500.50")`` is not 2500.50, and a pipeline that
stores it as one has silently changed a financial figure. Floats that arrive
from a source are converted via ``str`` so the decimal reading of what the
source printed is preserved rather than its binary approximation.

BINARY
------
A BINARY target receives a base64 ASCII string, not raw bytes. This is forced
by the frozen contract, not chosen: ``CanonicalRecord.normalized_data`` is
validated with ``require_json_object``, and the Phase 1 serializer rejects
``bytes`` outright. Base64 is lossless and reversible, so no information is
lost - only its representation is pinned to something the canonical model can
actually hold.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
from typing import Any

from erp_pipeline.schemas.enums import FieldDataType
from erp_pipeline.transformation.models import (
    IssueCode,
    TransformationOptions,
    UnknownTypePolicy,
)


@dataclass(frozen=True)
class ConversionResult:
    """Outcome of one value conversion.

    ``reason`` names types and rules only - it never contains the value, so it
    is safe to put straight into a ``DataQualityIssue`` message.
    """

    ok: bool
    value: Any = None
    reason: str | None = None
    code: IssueCode | None = None

    @classmethod
    def success(cls, value: Any) -> "ConversionResult":
        return cls(ok=True, value=value)

    @classmethod
    def failure(
        cls, reason: str, code: IssueCode = IssueCode.TYPE_CONVERSION_FAILED
    ) -> "ConversionResult":
        return cls(ok=False, reason=reason, code=code)


def convert(
    value: Any,
    target_type: FieldDataType | None,
    options: TransformationOptions,
) -> ConversionResult:
    """Convert ``value`` to ``target_type`` under the configured policies.

    ``None`` passes through untouched: nullability is a validation question,
    decided against the canonical model, not a conversion question.
    """
    if value is None:
        return ConversionResult.success(None)

    if target_type is None:
        # The mapping declared no target type. Nothing to convert to, so the
        # value is carried as-is rather than guessed at.
        return ConversionResult.success(value)

    if target_type is FieldDataType.UNKNOWN:
        if options.unknown_type_policy is UnknownTypePolicy.REJECT:
            return ConversionResult.failure(
                "the canonical target declares no data type and the configured "
                "policy refuses to map into an untyped target",
                IssueCode.UNSUPPORTED_DATA_TYPE,
            )
        return ConversionResult.success(value)

    handler = _HANDLERS.get(target_type)
    if handler is None:  # pragma: no cover - every member is handled
        return ConversionResult.failure(
            f"no conversion is implemented for target type {target_type.value!r}",
            IssueCode.UNSUPPORTED_DATA_TYPE,
        )

    try:
        return handler(value, options)
    except _NaiveDatetimeRefused:
        return ConversionResult.failure(
            "the source datetime carries no timezone and assuming UTC is "
            "disabled by configuration"
        )


# ============================================================
# STRING (Step 9)
# ============================================================

def _to_string(value: Any, options: TransformationOptions) -> ConversionResult:
    policy = options.string_policy

    if isinstance(value, str):
        # Identity. This is why "007" survives as "007": a string target never
        # routes a string value through a numeric parse.
        return ConversionResult.success(value)

    if isinstance(value, bool):
        if not policy.allow_boolean_to_string:
            return ConversionResult.failure(
                "converting boolean to string is disabled; 'true'/'True'/'1' "
                "are all defensible renderings and the choice must be declared"
            )
        return ConversionResult.success("true" if value else "false")

    if isinstance(value, (int, Decimal)):
        if not policy.allow_number_to_string:
            return ConversionResult.failure(
                "converting a number to string is disabled by configuration"
            )
        return ConversionResult.success(str(value))

    if isinstance(value, float):
        if not policy.allow_number_to_string:
            return ConversionResult.failure(
                "converting a number to string is disabled by configuration"
            )
        if not _float_is_finite(value, options):
            return ConversionResult.failure(
                "the source value is NaN or Infinity, which has no meaningful "
                "string form for ERP data"
            )
        # Via Decimal so the printed form is the decimal reading, not the
        # binary artifact.
        return ConversionResult.success(str(Decimal(str(value))))

    if isinstance(value, (datetime, date)):
        if not policy.allow_temporal_to_string:
            return ConversionResult.failure(
                "converting a date/datetime to string is disabled by "
                "configuration"
            )
        return ConversionResult.success(_temporal_iso(value))

    if isinstance(value, (bytes, bytearray)):
        return ConversionResult.failure(
            "converting binary to string is not supported; map it to a BINARY "
            "target instead"
        )

    if isinstance(value, (dict, list, tuple)):
        if not policy.allow_structural_to_string:
            return ConversionResult.failure(
                "refusing to stringify an object/array into a string target: "
                "the result would not be parseable back into the original "
                "structure"
            )
        return ConversionResult.success(str(value))

    return ConversionResult.failure(
        f"no defined string conversion for a value of type "
        f"{type(value).__name__}"
    )


def _temporal_iso(value: datetime | date) -> str:
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(
            tzinfo=timezone.utc
        )
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value.isoformat()


# ============================================================
# INTEGER (Step 10)
# ============================================================

def _to_integer(value: Any, options: TransformationOptions) -> ConversionResult:
    policy = options.number_policy

    if isinstance(value, bool):
        # bool is an int subclass in Python. Accepting it silently would turn
        # a flag into 0/1 without anyone declaring that.
        return ConversionResult.failure(
            "refusing to convert a boolean into an integer target; declare an "
            "explicit mapping if 0/1 is genuinely intended"
        )

    if isinstance(value, int):
        return ConversionResult.success(value)

    if isinstance(value, float):
        if not _float_is_finite(value, options):
            return ConversionResult.failure(
                "the source value is NaN or Infinity and has no integer form"
            )
        return _integral_or_fail(Decimal(str(value)), policy)

    if isinstance(value, Decimal):
        if not value.is_finite():
            return ConversionResult.failure(
                "the source value is NaN or Infinity and has no integer form"
            )
        return _integral_or_fail(value, policy)

    if isinstance(value, str):
        text = _clean_numeric_text(value, options)
        if text is None:
            return ConversionResult.failure(
                "the source text contains a thousands separator and separator "
                "handling is not enabled, so its numeric reading is ambiguous"
            )
        if not text:
            return ConversionResult.failure(
                "the source text is empty and has no integer value"
            )
        try:
            parsed = Decimal(text)
        except (DecimalException, ValueError):
            return ConversionResult.failure(
                "the source text is not a number and cannot be read as an "
                "integer"
            )
        if not parsed.is_finite():
            return ConversionResult.failure(
                "the source text denotes NaN or Infinity, which has no integer "
                "form"
            )
        return _integral_or_fail(parsed, policy)

    return ConversionResult.failure(
        f"no defined integer conversion for a value of type "
        f"{type(value).__name__}"
    )


def _integral_or_fail(value: Decimal, policy: Any) -> ConversionResult:
    """Accept only an exactly-integral decimal. Never truncate."""
    if value == value.to_integral_value():
        if not policy.allow_integral_float_to_integer and value != Decimal(int(value)):
            return ConversionResult.failure(
                "converting a fractional type to integer is disabled"
            )
        return ConversionResult.success(int(value))

    return ConversionResult.failure(
        "the source value has a fractional part; truncating it would silently "
        "change the number, so an integer target is refused"
    )


# ============================================================
# DECIMAL (Step 11)
# ============================================================

def _to_decimal(value: Any, options: TransformationOptions) -> ConversionResult:
    if isinstance(value, bool):
        return ConversionResult.failure(
            "refusing to convert a boolean into a decimal target"
        )

    if isinstance(value, Decimal):
        return _finite_decimal(value, options)

    if isinstance(value, int):
        return ConversionResult.success(Decimal(value))

    if isinstance(value, float):
        if not _float_is_finite(value, options):
            return ConversionResult.failure(
                "the source value is NaN or Infinity; ordinary ERP financial "
                "data has no such value"
            )
        # str() first: Decimal(2500.50) would capture the binary approximation.
        return _finite_decimal(Decimal(str(value)), options)

    if isinstance(value, str):
        text = _clean_numeric_text(value, options)
        if text is None:
            return ConversionResult.failure(
                "the source text contains a thousands separator and separator "
                "handling is not enabled, so its numeric reading is ambiguous"
            )
        if not text:
            return ConversionResult.failure(
                "the source text is empty and has no decimal value"
            )
        try:
            parsed = Decimal(text)
        except (DecimalException, ValueError):
            return ConversionResult.failure(
                "the source text is not a number and cannot be read as a "
                "decimal"
            )
        return _finite_decimal(parsed, options)

    return ConversionResult.failure(
        f"no defined decimal conversion for a value of type "
        f"{type(value).__name__}"
    )


def _finite_decimal(
    value: Decimal, options: TransformationOptions
) -> ConversionResult:
    if value.is_finite():
        return ConversionResult.success(value)

    if options.number_policy.allow_nan_and_infinity:
        # Still refused: the canonical record could not be serialized, because
        # the frozen serializer rejects NaN and Infinity outright. Allowing it
        # here would only move the failure somewhere less informative.
        return ConversionResult.failure(
            "NaN/Infinity is permitted by policy but is not representable in "
            "the canonical model, which rejects it at serialization"
        )

    return ConversionResult.failure(
        "the source value is NaN or Infinity; ordinary ERP financial data has "
        "no such value"
    )


def _float_is_finite(value: float, options: TransformationOptions) -> bool:
    if math.isnan(value) or math.isinf(value):
        return False
    return True


def _clean_numeric_text(value: str, options: TransformationOptions) -> str | None:
    """Trim a numeric string; return ``None`` when it is ambiguous.

    Whitespace around a number is universally meaningless, so it is stripped.
    A thousands separator is NOT: ``"1,234"`` reads as 1234 in one locale and
    1.234 in another, so it is refused unless a caller declared the intent.
    """
    text = value.strip()

    if "," in text:
        if not options.number_policy.allow_thousands_separator:
            return None
        text = text.replace(",", "")

    return text


# ============================================================
# BOOLEAN (Step 12)
# ============================================================

def _to_boolean(value: Any, options: TransformationOptions) -> ConversionResult:
    resolved = options.boolean_policy.resolve(value)

    if resolved is None:
        return ConversionResult.failure(
            "the source value is not one of the literals declared as true or "
            "false; a non-empty string is not evidence of truth"
        )

    return ConversionResult.success(resolved)


# ============================================================
# DATE / DATETIME (Step 13)
# ============================================================

def _to_date(value: Any, options: TransformationOptions) -> ConversionResult:
    policy = options.date_policy

    if isinstance(value, datetime):
        return ConversionResult.failure(
            "refusing to convert a datetime into a date target: discarding the "
            "time component is lossy and must be declared explicitly"
        )

    if isinstance(value, date):
        return ConversionResult.success(value)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ConversionResult.failure(
                "the source text is empty and denotes no date"
            )

        # Guard against date.fromisoformat silently accepting a full timestamp
        # and dropping its time.
        if "T" in text or " " in text:
            return ConversionResult.failure(
                "the source text carries a time component; converting it to a "
                "date target would silently discard the time"
            )

        try:
            return ConversionResult.success(date.fromisoformat(text))
        except ValueError:
            pass

        for fmt in policy.date_formats:
            try:
                return ConversionResult.success(
                    datetime.strptime(text, fmt).date()
                )
            except ValueError:
                continue

        return ConversionResult.failure(
            "the source text is not an ISO-8601 date and matches no declared "
            "date format; ambiguous forms such as DD/MM/YYYY are refused "
            "unless a format is configured"
        )

    return ConversionResult.failure(
        f"no defined date conversion for a value of type {type(value).__name__}"
    )


def _to_datetime(value: Any, options: TransformationOptions) -> ConversionResult:
    policy = options.date_policy

    if isinstance(value, datetime):
        return ConversionResult.success(_as_utc(value, policy))

    if isinstance(value, date):
        if not policy.allow_date_as_datetime:
            return ConversionResult.failure(
                "the source is a whole date and promoting it to a datetime is "
                "disabled by configuration"
            )
        return ConversionResult.success(
            datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        )

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ConversionResult.failure(
                "the source text is empty and denotes no datetime"
            )

        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text

        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None

        if parsed is None:
            for fmt in policy.datetime_formats:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue

        if parsed is None:
            return ConversionResult.failure(
                "the source text is not an ISO-8601 datetime and matches no "
                "declared datetime format; ambiguous forms are refused unless "
                "a format is configured"
            )

        if (
            "T" not in text
            and " " not in text
            and not policy.allow_date_as_datetime
        ):
            return ConversionResult.failure(
                "the source text carries a date only and promoting it to a "
                "datetime is disabled by configuration"
            )

        return ConversionResult.success(_as_utc(parsed, policy))

    return ConversionResult.failure(
        f"no defined datetime conversion for a value of type "
        f"{type(value).__name__}"
    )


def _as_utc(value: datetime, policy: Any) -> datetime:
    """Normalize to a UTC-aware datetime.

    The canonical model requires awareness: the frozen serializer refuses a
    naive datetime rather than guessing its zone, so a naive value must either
    be given one here or be rejected.
    """
    if value.tzinfo is None:
        if not policy.assume_utc_when_naive:
            raise _NaiveDatetimeRefused()
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


class _NaiveDatetimeRefused(Exception):
    """Internal control-flow marker; never escapes ``convert``."""


# ============================================================
# BINARY / OBJECT / ARRAY
# ============================================================

def _to_binary(value: Any, options: TransformationOptions) -> ConversionResult:
    if isinstance(value, (bytes, bytearray)):
        return ConversionResult.success(
            base64.b64encode(bytes(value)).decode("ascii")
        )

    if isinstance(value, str):
        # Already-encoded content is accepted only if it really is base64;
        # otherwise a caller is silently storing text in a binary field.
        try:
            base64.b64decode(value, validate=True)
        except Exception:
            return ConversionResult.failure(
                "the source text is not valid base64; a BINARY canonical target "
                "holds base64 because the canonical model cannot carry raw bytes"
            )
        return ConversionResult.success(value)

    return ConversionResult.failure(
        f"no defined binary conversion for a value of type "
        f"{type(value).__name__}"
    )


def _to_object(value: Any, options: TransformationOptions) -> ConversionResult:
    if isinstance(value, dict):
        return ConversionResult.success(value)

    return ConversionResult.failure(
        f"an OBJECT target requires a mapping, got {type(value).__name__}"
    )


def _to_array(value: Any, options: TransformationOptions) -> ConversionResult:
    if isinstance(value, (list, tuple)):
        return ConversionResult.success(list(value))

    if isinstance(value, (str, bytes, bytearray, dict)):
        # A string is iterable, which makes an accidental character-by-character
        # array very easy to produce and very hard to notice.
        return ConversionResult.failure(
            f"an ARRAY target requires a list, got {type(value).__name__}"
        )

    return ConversionResult.failure(
        f"an ARRAY target requires a list, got {type(value).__name__}"
    )


_HANDLERS = {
    FieldDataType.STRING: _to_string,
    FieldDataType.INTEGER: _to_integer,
    FieldDataType.DECIMAL: _to_decimal,
    FieldDataType.BOOLEAN: _to_boolean,
    FieldDataType.DATE: _to_date,
    FieldDataType.DATETIME: _to_datetime,
    FieldDataType.BINARY: _to_binary,
    FieldDataType.OBJECT: _to_object,
    FieldDataType.ARRAY: _to_array,
}


def matches_type(value: Any, target_type: FieldDataType | None) -> bool:
    """Whether a converted value really is of the canonical type (Step 24).

    Used by the validator to confirm the transformer's work. ``None`` is not a
    type question - nullability is checked separately.
    """
    if value is None or target_type is None or target_type is FieldDataType.UNKNOWN:
        return True

    if target_type is FieldDataType.STRING:
        return isinstance(value, str)
    if target_type is FieldDataType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if target_type is FieldDataType.DECIMAL:
        return isinstance(value, Decimal)
    if target_type is FieldDataType.BOOLEAN:
        return isinstance(value, bool)
    if target_type is FieldDataType.DATE:
        return isinstance(value, date) and not isinstance(value, datetime)
    if target_type is FieldDataType.DATETIME:
        return isinstance(value, datetime) and value.tzinfo is not None
    if target_type is FieldDataType.BINARY:
        return isinstance(value, str)
    if target_type is FieldDataType.OBJECT:
        return isinstance(value, dict)
    if target_type is FieldDataType.ARRAY:
        return isinstance(value, list)

    return True  # pragma: no cover - unreachable for a closed enum


__all__ = [
    "ConversionResult",
    "convert",
    "matches_type",
]
