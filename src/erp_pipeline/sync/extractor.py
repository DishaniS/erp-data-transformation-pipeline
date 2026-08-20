"""Incremental change extraction: reading only what changed.

ONE CONTRACT, NOT ONE ENGINE PER TECHNOLOGY (Step 9)
-----------------------------------------------------
There is no ``PostgresSyncEngine`` and no ``MySQLSyncEngine``. There is one
``IncrementalExtractor`` protocol, and connector-specific detail lives behind
strategy adapters that all produce the same ``SourceChange`` stream. The
coordinator never learns which technology it is talking to.

THE TIE-BREAK PROBLEM (Step 5)
------------------------------
A timestamp-only watermark loses data, and it does so silently::

    10:00:00  id=100
    10:00:00  id=101   <- batch ends here, watermark = 10:00:00
    10:00:00  id=102   <- next run asks for "> 10:00:00" and never sees it

The fix is a composite position and a matching predicate::

    WHERE updated_at > :ts OR (updated_at = :ts AND id > :tie)
    ORDER BY updated_at, id
    LIMIT :batch

The ORDER BY is not decoration. Without it the LIMIT selects an arbitrary
subset, and the watermark computed from it means nothing.

SQL SAFETY
----------
Values are ALWAYS bound parameters - no value is ever formatted into SQL.
Identifiers cannot be bound, so table and column names are validated against a
strict pattern and rejected otherwise. The engine offers no public
arbitrary-query API, and reads go through the connector's existing read-only
connection seam.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from erp_pipeline.sync.errors import SyncConfigurationError, UnsupportedStrategyError
from erp_pipeline.sync.models import (
    ChangeOperation,
    SourceChange,
    SyncOptions,
    SyncState,
    Watermark,
    WatermarkStrategy,
)

#: Identifiers safe to interpolate. Anything else is refused rather than
#: quoted-and-hoped-for: an unquotable identifier is a configuration error, and
#: string-escaping user input into SQL is how injection happens.
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(value: str, role: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.match(value):
        raise SyncConfigurationError(
            f"{role} {value!r} is not a plain SQL identifier. Only "
            "letters, digits and underscores are accepted; identifiers cannot "
            "be bound as parameters, so anything else is refused rather than "
            "escaped."
        )
    return value


@dataclass(frozen=True)
class ExtractionConfig:
    """How to read changes out of one source entity."""

    source_entity: str
    strategy: WatermarkStrategy
    #: Business key column, used as ``SourceChange.record_key``.
    key_field: str
    #: Ordering column for TIMESTAMP/COMPOSITE.
    watermark_field: str | None = None
    #: Tie-breaking column for COMPOSITE, and the ordering column for
    #: MONOTONIC_ID.
    tie_break_field: str | None = None
    #: Database schema / namespace, when the source uses one.
    namespace: str | None = None
    #: Column carrying a soft-delete flag, when the source has one (Step 17).
    deleted_flag_field: str | None = None
    #: Columns to read. Empty means all.
    columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_identifier(self.source_entity, "source_entity")
        validate_identifier(self.key_field, "key_field")

        for value, role in (
            (self.watermark_field, "watermark_field"),
            (self.tie_break_field, "tie_break_field"),
            (self.namespace, "namespace"),
            (self.deleted_flag_field, "deleted_flag_field"),
        ):
            if value is not None:
                validate_identifier(value, role)

        for column in self.columns:
            validate_identifier(column, "column")

        if self.strategy in (
            WatermarkStrategy.TIMESTAMP,
            WatermarkStrategy.COMPOSITE,
        ) and not self.watermark_field:
            raise SyncConfigurationError(
                f"Strategy {self.strategy.value!r} needs a watermark_field."
            )

        if self.strategy is WatermarkStrategy.COMPOSITE and not self.tie_break_field:
            raise SyncConfigurationError(
                "COMPOSITE strategy needs a tie_break_field. Without one it is "
                "a plain timestamp watermark, which loses rows that share a "
                "timestamp across a batch boundary."
            )

        if (
            self.strategy is WatermarkStrategy.MONOTONIC_ID
            and not self.tie_break_field
        ):
            raise SyncConfigurationError(
                "MONOTONIC_ID strategy needs a tie_break_field naming the "
                "monotonic column."
            )

    @property
    def qualified_name(self) -> str:
        if self.namespace:
            return f"{self.namespace}.{self.source_entity}"
        return self.source_entity

    @property
    def order_columns(self) -> tuple[str, ...]:
        if self.strategy is WatermarkStrategy.COMPOSITE:
            return (self.watermark_field, self.tie_break_field)  # type: ignore[return-value]
        if self.strategy is WatermarkStrategy.TIMESTAMP:
            return (self.watermark_field,)  # type: ignore[return-value]
        if self.strategy is WatermarkStrategy.MONOTONIC_ID:
            return (self.tie_break_field,)  # type: ignore[return-value]
        raise UnsupportedStrategyError(
            f"Strategy {self.strategy.value!r} has no SQL ordering; it is not "
            "a relational extraction strategy."
        )


@runtime_checkable
class IncrementalExtractor(Protocol):
    """Produces the changes that happened after a sync state's watermark."""

    def fetch_changes(
        self, state: SyncState, options: SyncOptions
    ) -> Iterable[SourceChange]:
        ...  # pragma: no cover - protocol declaration


# ============================================================
# Predicate construction (Steps 4, 5)
# ============================================================

def build_watermark_predicate(
    config: ExtractionConfig, watermark: Watermark
) -> tuple[str, dict[str, Any]]:
    """Build the WHERE fragment and its bound parameters.

    A pure function so the tie-break logic can be unit-tested by hand without a
    database, which is the only way to be confident about a clause whose bug
    would be silent data loss.

    Returns ``("", {})`` for a fresh sync, which reads from the beginning.
    """
    if watermark.is_empty:
        return "", {}

    if config.strategy is WatermarkStrategy.MONOTONIC_ID:
        return (
            f"{config.tie_break_field} > :wm_tie",
            {"wm_tie": watermark.tie_breaker},
        )

    if config.strategy is WatermarkStrategy.TIMESTAMP:
        return (
            f"{config.watermark_field} > :wm_ts",
            {"wm_ts": watermark.timestamp},
        )

    if config.strategy is WatermarkStrategy.COMPOSITE:
        if watermark.tie_breaker is None:
            # Position known only to timestamp precision: fall back to a strict
            # timestamp comparison rather than inventing a tie-breaker.
            return (
                f"{config.watermark_field} > :wm_ts",
                {"wm_ts": watermark.timestamp},
            )
        return (
            f"({config.watermark_field} > :wm_ts OR "
            f"({config.watermark_field} = :wm_ts AND "
            f"{config.tie_break_field} > :wm_tie))",
            {"wm_ts": watermark.timestamp, "wm_tie": watermark.tie_breaker},
        )

    raise UnsupportedStrategyError(
        f"Strategy {config.strategy.value!r} does not support SQL watermark "
        "extraction."
    )


def build_extraction_sql(
    config: ExtractionConfig, watermark: Watermark, batch_size: int
) -> tuple[str, dict[str, Any]]:
    """The full bounded, ordered extraction statement."""
    predicate, params = build_watermark_predicate(config, watermark)

    projection = ", ".join(config.columns) if config.columns else "*"
    where_clause = f"WHERE {predicate}" if predicate else ""
    order_clause = ", ".join(config.order_columns)

    sql = (
        f"SELECT {projection} FROM {config.qualified_name} "
        f"{where_clause} ORDER BY {order_clause} LIMIT :batch_size"
    ).replace("  ", " ").strip()

    return sql, {**params, "batch_size": batch_size}


def watermark_from_row(
    config: ExtractionConfig, row: Mapping[str, Any]
) -> Watermark:
    """The position of one extracted row."""
    timestamp = None

    if config.watermark_field:
        raw = row.get(config.watermark_field)
        if isinstance(raw, datetime):
            timestamp = (
                raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            )

    tie_breaker = (
        row.get(config.tie_break_field) if config.tie_break_field else None
    )

    return Watermark(timestamp=timestamp, tie_breaker=tie_breaker)


def classify_operation(
    config: ExtractionConfig, row: Mapping[str, Any], is_new: bool = False
) -> ChangeOperation:
    """INSERT, UPDATE or DELETE for one extracted row.

    A watermark-based source cannot distinguish an insert from an update
    without extra evidence, and that is fine: both become an idempotent upsert
    downstream. DELETE is only ever reported when the source carries an
    explicit soft-delete flag - a hard-deleted row is simply not returned by
    any query, which is a genuine capability limit (Step 17), not something to
    guess at.
    """
    if config.deleted_flag_field:
        flag = row.get(config.deleted_flag_field)
        if flag is True or flag == 1 or flag == "true" or flag == "t":
            return ChangeOperation.DELETE

    return ChangeOperation.INSERT if is_new else ChangeOperation.UPDATE


# ============================================================
# In-memory extractor (the reference semantics, and what tests use)
# ============================================================

class InMemoryChangeSource:
    """A list-backed source implementing the full watermark contract.

    Not a toy: it applies the same predicate, the same ordering and the same
    bounded batching as the relational extractor, which is what makes it a
    trustworthy place to prove the tie-break behaviour that Step 5 demands.
    """

    def __init__(
        self,
        config: ExtractionConfig,
        rows: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self._config = config
        self._rows: list[dict[str, Any]] = [dict(row) for row in rows]
        self.fetch_calls = 0
        self.rows_scanned = 0

    # -- source mutation, for tests --

    def add(self, row: Mapping[str, Any]) -> None:
        self._rows.append(dict(row))

    def update(self, key: Any, **changes: Any) -> None:
        for row in self._rows:
            if row.get(self._config.key_field) == key:
                row.update(changes)

    def remove(self, key: Any) -> None:
        self._rows = [
            row for row in self._rows
            if row.get(self._config.key_field) != key
        ]

    @property
    def total_rows(self) -> int:
        return len(self._rows)

    # -- extraction --

    def _sort_key(self, row: Mapping[str, Any]) -> tuple:
        watermark = watermark_from_row(self._config, row)
        return watermark.sort_key()

    def fetch_changes(
        self, state: SyncState, options: SyncOptions
    ) -> list[SourceChange]:
        self.fetch_calls += 1
        config = self._config

        ordered = sorted(self._rows, key=self._sort_key)
        selected: list[SourceChange] = []

        for row in ordered:
            watermark = watermark_from_row(config, row)

            if not state.watermark.is_empty and not watermark.is_after(
                state.watermark
            ):
                continue

            self.rows_scanned += 1

            operation = classify_operation(
                config, row, is_new=state.watermark.is_empty
            )

            if operation is ChangeOperation.DELETE and not options.process_deletes:
                continue

            selected.append(
                SourceChange(
                    source_system_id=state.source_system_id,
                    source_entity=config.source_entity,
                    record_key=str(row.get(config.key_field)),
                    operation=operation,
                    watermark=watermark,
                    payload=dict(row),
                    ordinal=len(selected) + 1,
                )
            )

            if len(selected) >= options.batch_size:
                break

        return selected


# ============================================================
# Relational extractor
# ============================================================

class RelationalIncrementalExtractor:
    """Watermark extraction over any SQLAlchemy-reachable relational source.

    One implementation covers PostgreSQL, MySQL and SQL Server (Step 10):
    the watermark predicate, the ordering and the LIMIT are standard SQL, and
    nothing vendor-specific is required. No CDC, no triggers, no replication
    slots - a source that has an updated-at column and a key is enough.
    """

    def __init__(self, engine: Any, config: ExtractionConfig) -> None:
        self._engine = engine
        self._config = config
        self.fetch_calls = 0
        self.rows_returned = 0

    @property
    def config(self) -> ExtractionConfig:
        return self._config

    def fetch_changes(
        self, state: SyncState, options: SyncOptions
    ) -> list[SourceChange]:
        from sqlalchemy import text

        self.fetch_calls += 1
        config = self._config

        sql, params = build_extraction_sql(
            config, state.watermark, options.batch_size
        )

        with self._engine.connect() as connection:
            rows = connection.execute(text(sql), params).mappings().all()

        self.rows_returned += len(rows)
        changes: list[SourceChange] = []

        for index, row in enumerate(rows, start=1):
            payload = dict(row)
            operation = classify_operation(
                config, payload, is_new=state.watermark.is_empty
            )

            if operation is ChangeOperation.DELETE and not options.process_deletes:
                continue

            changes.append(
                SourceChange(
                    source_system_id=state.source_system_id,
                    source_entity=config.source_entity,
                    record_key=str(payload.get(config.key_field)),
                    operation=operation,
                    watermark=watermark_from_row(config, payload),
                    payload=payload,
                    ordinal=index,
                )
            )

        return changes


class ConnectorIncrementalExtractor(RelationalIncrementalExtractor):
    """Relational extraction through a Phase 3 connector's read-only seam.

    Uses the connector's existing ``_open_readonly_connection`` rather than
    adding a public arbitrary-query API to a frozen connector, which Phase 3
    deliberately does not expose.
    """

    def __init__(self, connector: Any, config: ExtractionConfig) -> None:
        super().__init__(engine=None, config=config)
        self._connector = connector

    def fetch_changes(
        self, state: SyncState, options: SyncOptions
    ) -> list[SourceChange]:
        from sqlalchemy import text

        self.fetch_calls += 1
        config = self._config

        sql, params = build_extraction_sql(
            config, state.watermark, options.batch_size
        )

        with self._connector._open_readonly_connection() as connection:
            rows = connection.execute(text(sql), params).mappings().all()

        self.rows_returned += len(rows)

        return [
            SourceChange(
                source_system_id=state.source_system_id,
                source_entity=config.source_entity,
                record_key=str(dict(row).get(config.key_field)),
                operation=classify_operation(
                    config, dict(row), is_new=state.watermark.is_empty
                ),
                watermark=watermark_from_row(config, dict(row)),
                payload=dict(row),
                ordinal=index,
            )
            for index, row in enumerate(rows, start=1)
        ]


# ============================================================
# File and specification sources (Steps 12, 13)
# ============================================================

class ContentHashChangeSource:
    """Whole-artifact change detection for files and API specifications.

    A CSV upload or an OpenAPI document has no row-level change signal, and
    inventing one would be dishonest. What it does have is a content hash, so
    the unit of change is the ARTIFACT: same hash, nothing happened; different
    hash, the artifact changed and its records are reprocessed.
    """

    def __init__(
        self,
        source_entity: str,
        artifact_key: str,
        content_hash: str,
        records: Sequence[Mapping[str, Any]] = (),
        key_field: str = "id",
    ) -> None:
        self._source_entity = source_entity
        self._artifact_key = artifact_key
        self._content_hash = content_hash
        self._records = [dict(record) for record in records]
        self._key_field = key_field
        self.fetch_calls = 0

    def set_content(
        self, content_hash: str, records: Sequence[Mapping[str, Any]]
    ) -> None:
        self._content_hash = content_hash
        self._records = [dict(record) for record in records]

    def fetch_changes(
        self, state: SyncState, options: SyncOptions
    ) -> list[SourceChange]:
        self.fetch_calls += 1

        if state.watermark.content_hash == self._content_hash:
            # Byte-identical artifact. Nothing to do, and importantly nothing
            # is reprocessed just because a sync ran.
            return []

        watermark = Watermark(content_hash=self._content_hash)

        return [
            SourceChange(
                source_system_id=state.source_system_id,
                source_entity=self._source_entity,
                record_key=str(record.get(self._key_field, index)),
                operation=(
                    ChangeOperation.INSERT
                    if state.watermark.is_empty
                    else ChangeOperation.UPDATE
                ),
                watermark=watermark,
                payload=record,
                ordinal=index,
            )
            for index, record in enumerate(self._records[: options.batch_size], 1)
        ]


__all__ = [
    "ExtractionConfig",
    "IncrementalExtractor",
    "validate_identifier",
    "build_watermark_predicate",
    "build_extraction_sql",
    "watermark_from_row",
    "classify_operation",
    "InMemoryChangeSource",
    "RelationalIncrementalExtractor",
    "ConnectorIncrementalExtractor",
    "ContentHashChangeSource",
]
