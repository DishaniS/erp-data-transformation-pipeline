"""One standard serialization approach for every contract model.

Every model inherits ``JsonModel`` and is serialized through ``to_json_dict``,
so there is exactly one place that decides how an enum, a datetime or a nested
model reaches the wire.

Rules
-----
enum        serialized as its ``.value`` string
datetime    converted to UTC and rendered RFC3339 with a ``Z`` suffix
date        rendered ISO-8601 (``YYYY-MM-DD``)
Decimal     rendered as a string, never a float, so money keeps its precision
tuple/list  rendered as a JSON array
Mapping     rendered as a JSON object with string keys
model       recursed into
None        preserved as ``null``

Naive datetimes are rejected at validation time rather than guessed at here,
so a timestamp can never be silently mislabelled as UTC.

This module provides serialization only (``model -> JSON``). The reverse
direction (``JSON -> model``) is implemented separately in
``erp_pipeline.schemas.deserialization``, added in Phase 2 when persisted
contracts needed to be read back from the schema catalog. The two directions
are deliberately kept in separate modules rather than merged into one
reflective loader, but both are pure and I/O-free.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

# Values that JSON represents natively and that need no conversion.
_JSON_PRIMITIVES = (str, int, float, bool)


class SerializationError(TypeError):
    """Raised when a value cannot be represented in JSON."""


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime.

    Used as the default for every ``created_at`` / ``updated_at`` so normal
    construction never produces a naive datetime.
    """
    return datetime.now(timezone.utc)


def to_rfc3339(value: datetime) -> str:
    """Render an aware datetime as RFC3339 in UTC with a ``Z`` suffix.

    A non-UTC aware datetime is converted, preserving the instant, so two
    records describing the same moment always serialize to the same string.
    """
    if value.tzinfo is None:
        raise SerializationError(
            "Refusing to serialize a naive datetime. Attach a timezone "
            "(erp_pipeline.schemas.serialization.utc_now() returns an aware "
            "UTC datetime)."
        )

    return (
        value.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def to_json_value(value: Any, path: str = "value") -> Any:
    """Convert any supported value into a JSON-compatible structure.

    ``path`` is threaded through purely to make the error message point at the
    offending location inside a nested structure.
    """
    if value is None or isinstance(value, _JSON_PRIMITIVES):
        # bool must be checked before int elsewhere, but here both are fine.
        if isinstance(value, float) and value != value:  # NaN
            raise SerializationError(
                f"{path}: NaN is not representable in JSON."
            )
        if isinstance(value, float) and value in (float("inf"), float("-inf")):
            raise SerializationError(
                f"{path}: Infinity is not representable in JSON."
            )
        return value

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, datetime):
        return to_rfc3339(value)

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, Decimal):
        # A string keeps exact precision. Rendering money as a float would
        # silently change 25000.10 into 25000.099999999999.
        return str(value)

    if isinstance(value, JsonModel):
        return value.to_json_dict()

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_json_value(
                getattr(value, field.name), f"{path}.{field.name}"
            )
            for field in dataclasses.fields(value)
        }

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SerializationError(
                    f"{path}: JSON object keys must be strings, got "
                    f"{type(key).__name__} ({key!r})."
                )
            result[key] = to_json_value(item, f"{path}.{key}")
        return result

    if isinstance(value, (list, tuple)):
        return [to_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]

    raise SerializationError(
        f"{path}: {type(value).__name__} is not JSON-serializable. Supported "
        "types are str, int, float, bool, None, Decimal, date, datetime, Enum, "
        "mappings, sequences and contract models."
    )


class JsonModel:
    """Mixin giving every contract model one serialization entry point.

    Deliberately not a dataclass itself, so subclasses stay free to declare
    their own fields and inheritance order without field-ordering surprises.
    """

    def to_json_dict(self, exclude_none: bool = False) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary.

        By default optional fields are emitted as ``null`` rather than
        omitted, so the serialized shape of a model is stable regardless of
        which optional values happen to be set. Pass ``exclude_none=True`` for
        a compact form.
        """
        if not dataclasses.is_dataclass(self):  # pragma: no cover - misuse guard
            raise SerializationError(
                f"{type(self).__name__} must be a dataclass to be serialized."
            )

        result: dict[str, Any] = {}

        for field in dataclasses.fields(self):
            value = getattr(self, field.name)

            if value is None and exclude_none:
                continue

            converted = to_json_value(value, f"{type(self).__name__}.{field.name}")

            if exclude_none and isinstance(converted, dict):
                converted = _drop_none(converted)

            result[field.name] = converted

        return result

    def to_json(self, indent: int | None = None, exclude_none: bool = False) -> str:
        """Serialize to a JSON string using the same rules as ``to_json_dict``."""
        return json.dumps(
            self.to_json_dict(exclude_none=exclude_none),
            indent=indent,
            ensure_ascii=False,
            sort_keys=False,
        )


def _drop_none(value: dict[str, Any]) -> dict[str, Any]:
    """Recursively remove ``None`` entries from an already-converted dict."""
    return {
        key: _drop_none(item) if isinstance(item, dict) else item
        for key, item in value.items()
        if item is not None
    }


__all__ = [
    "SerializationError",
    "JsonModel",
    "to_json_value",
    "to_rfc3339",
    "utc_now",
]
