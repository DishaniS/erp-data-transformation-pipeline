"""Turn raw source rows into :class:`ProcessEvent` objects.

The only place in the process layer that touches a source's own vocabulary.
Everything downstream works on ``ProcessEvent``, which is why the case builder
and the process model contain no column names at all.

Timestamp handling is deliberately conservative: a value that cannot be parsed
becomes ``None`` and the event keeps its arrival order, rather than being
dropped or silently assigned "now". A dropped event changes a case's activity
sequence, which is exactly the kind of quiet corruption this layer must not
introduce.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator, Mapping

from erp_pipeline.process.errors import EventNormalizationError
from erp_pipeline.process.models import (
    DEFAULT_PROCESS_TYPE,
    EventLogConfig,
    ProcessEvent,
)

#: Formats tried, in order, when a timestamp arrives as text. ISO-8601 first
#: because that is what every well-behaved export produces; the rest cover the
#: common ERP exports seen in practice.
_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y",
)


def coerce_timestamp(value: Any) -> datetime | None:
    """Best-effort conversion of one source value into an aware UTC datetime.

    Returns ``None`` rather than raising: an unparseable timestamp is a
    data-quality fact about one event, not a reason to abandon the case.
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)

    text = str(value).strip()

    if not text:
        return None

    # ``fromisoformat`` handles the widest range and is fastest; the explicit
    # format list only runs when it fails.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    for fmt in _TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def _text_or_none(value: Any) -> str | None:
    """Render a source value as text, treating blanks and NaN as absent.

    ``str(float('nan')) == 'nan'`` and pandas hands out ``'None'`` strings, so
    both are filtered here rather than becoming activity names.
    """
    if value is None:
        return None

    text = str(value).strip()

    if not text or text.lower() in {"nan", "none", "null", "nat"}:
        return None

    return text


def resolve_process_type(row: Mapping[str, Any], config: EventLogConfig) -> str:
    """Per-row process type, falling back to the configured constant."""
    if config.process_type_field:
        value = _text_or_none(row.get(config.process_type_field))
        if value:
            return value

    if config.process_type:
        return config.process_type

    return DEFAULT_PROCESS_TYPE


def extract_attributes(
    row: Mapping[str, Any], config: EventLogConfig
) -> dict[str, Any]:
    """Which of the row's remaining columns are retained on the event.

    An explicit allow-list wins. Otherwise everything that is neither process
    structure nor explicitly excluded is kept, sorted so two rows with the same
    content always produce the same ordering and therefore the same hash.
    """
    if config.attribute_fields is not None:
        keys = [key for key in config.attribute_fields if key in row]
    else:
        skip = config.reserved_fields | config.excluded_fields
        keys = [key for key in row if key not in skip]

    return {key: row[key] for key in sorted(keys)}


def normalize_event(
    row: Mapping[str, Any],
    config: EventLogConfig,
    ordinal: int | None = None,
) -> ProcessEvent:
    """Turn one source row into a :class:`ProcessEvent`.

    Only a missing case id is fatal: an event that belongs to no case cannot be
    placed anywhere. A missing activity is retained as ``None`` so the event
    still contributes to the case's event count and timing, which is what an
    ERP log with occasional blank activity names actually needs.
    """
    if not isinstance(row, Mapping):
        raise EventNormalizationError(
            f"expected a mapping of column -> value, got {type(row).__name__}",
            ordinal=ordinal,
        )

    case_id = _text_or_none(row.get(config.case_id_field))

    if case_id is None:
        raise EventNormalizationError(
            f"row has no value in the configured case id column "
            f"{config.case_id_field!r}, so it belongs to no process instance",
            ordinal=ordinal,
        )

    event_key = (
        _text_or_none(row.get(config.event_key_field))
        if config.event_key_field
        else None
    )

    timestamp = (
        coerce_timestamp(row.get(config.timestamp_field))
        if config.timestamp_field
        else None
    )

    return ProcessEvent(
        case_id=case_id,
        activity=_text_or_none(row.get(config.activity_field)),
        process_type=resolve_process_type(row, config),
        timestamp=timestamp,
        event_key=event_key,
        ordinal=ordinal,
        attributes=extract_attributes(row, config),
    )


def normalize_events(
    rows: Iterable[Mapping[str, Any]],
    config: EventLogConfig,
    skip_invalid: bool = False,
) -> Iterator[ProcessEvent]:
    """Normalize a stream of rows lazily.

    Lazy so a multi-hundred-thousand-row event log is never materialized twice.
    With ``skip_invalid`` a row that cannot be normalized is passed over instead
    of aborting the run; the default refuses, because silently discarding
    events changes the process that gets discovered.
    """
    for ordinal, row in enumerate(rows):
        try:
            yield normalize_event(row, config, ordinal=ordinal)
        except EventNormalizationError:
            if skip_invalid:
                continue
            raise


__all__ = [
    "coerce_timestamp",
    "resolve_process_type",
    "extract_attributes",
    "normalize_event",
    "normalize_events",
]
