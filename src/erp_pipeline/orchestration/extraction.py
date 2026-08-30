"""Bounded snapshot extraction. The smallest thing that reads records safely.

WHY THIS EXISTS AT ALL
----------------------
Phase 3 connectors deliberately expose no ``execute`` method - that is the
guarantee that no caller can push arbitrary SQL through this system, and it is
not weakened here. But an end-to-end pipeline has to read rows from somewhere,
so Phase 13 adds the narrowest possible reader.

THE RULES IT KEEPS
------------------
- The entity must already exist in a discovered ``SourceSchema``. A table name
  that was never discovered cannot be read.
- Identifiers are re-validated against a strict pattern and quoted before they
  reach SQL. Nothing that arrived from a request body is interpolated.
- Reads are ``SELECT`` only, bounded by an explicit limit, with deterministic
  ordering by the primary key where one exists - so paging cannot silently skip
  or duplicate rows.
- No caller-supplied SQL, no caller-supplied WHERE clause, no raw command
  execution on MongoDB.

Anything richer than this belongs to a query layer that is not part of this
component.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from erp_pipeline.orchestration.errors import (
    InvalidPipelineRequestError,
    UnsupportedCapabilityError,
)
from erp_pipeline.schemas.enums import SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceSchema
from erp_pipeline.transformation import SourceRecord

#: SQL identifiers this adapter is willing to emit. Anything else is refused
#: rather than escaped, because refusing is easier to prove correct.
SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 5000


def validate_identifier(name: str, kind: str = "identifier") -> str:
    if not name or not SAFE_IDENTIFIER.fullmatch(name):
        raise InvalidPipelineRequestError(
            f"{kind} {name!r} is not a plain identifier and will not be used "
            "to build a query"
        )

    return name


def resolve_entity(schema: SourceSchema, entity: str | None) -> SourceEntity:
    """Find the requested entity inside the discovered schema.

    Matching against the schema is what makes extraction safe: the caller
    chooses from what was discovered, rather than naming an arbitrary table.
    """
    if not schema.entities:
        raise InvalidPipelineRequestError("the schema contains no entities")

    if entity is None:
        return schema.entities[0]

    wanted = entity.strip().lower()

    for candidate in schema.entities:
        if wanted in {
            candidate.source_name.lower(),
            candidate.normalized_name.lower(),
            candidate.entity_id.lower(),
        }:
            return candidate

    raise InvalidPipelineRequestError(
        f"entity {entity!r} is not present in schema {schema.schema_id!r}"
    )


@dataclass(frozen=True)
class ExtractionRequest:
    schema: SourceSchema
    entity: SourceEntity
    limit: int = DEFAULT_BATCH_SIZE
    #: Explicit business-key fields supplied by the job. This is especially
    #: important for MongoDB, where ``_id`` is provenance rather than the ERP
    #: employee identity.
    key_fields: tuple[str, ...] = ()

    @property
    def bounded_limit(self) -> int:
        return max(1, min(self.limit, MAX_BATCH_SIZE))


class SnapshotExtractor:
    """Reads a bounded page of records for one discovered entity."""

    def extract(
        self, request: ExtractionRequest, connection_factory: Any
    ) -> tuple[SourceRecord, ...]:
        raise NotImplementedError


class RelationalSnapshotExtractor(SnapshotExtractor):
    """A single ordered, bounded ``SELECT`` over a discovered table."""

    def build_statement(self, request: ExtractionRequest) -> str:
        entity = request.entity
        table = validate_identifier(entity.source_name, "table")
        namespace = (
            validate_identifier(entity.namespace, "schema")
            if entity.namespace
            else None
        )
        columns = [
            validate_identifier(field.source_name, "column")
            for field in entity.fields
        ]

        if not columns:
            raise InvalidPipelineRequestError(
                f"entity {entity.source_name!r} has no discovered columns"
            )

        selected = ", ".join(f'"{column}"' for column in columns)
        target = f'"{namespace}"."{table}"' if namespace else f'"{table}"'

        # Deterministic ordering. Without it, two pages of an unordered result
        # can overlap or skip rows and the extraction is quietly wrong.
        order_columns = [
            validate_identifier(name, "key column")
            for name in entity.primary_key_fields
        ] or columns[:1]
        order = ", ".join(f'"{column}"' for column in order_columns)

        return (
            f"SELECT {selected} FROM {target} "
            f"ORDER BY {order} LIMIT {request.bounded_limit}"
        )

    def extract(
        self, request: ExtractionRequest, connection_factory: Any
    ) -> tuple[SourceRecord, ...]:
        from sqlalchemy import text

        statement = self.build_statement(request)
        records: list[SourceRecord] = []

        with connection_factory() as connection:
            rows = connection.execute(text(statement)).mappings().all()

        key_fields = request.key_fields or request.entity.primary_key_fields

        for ordinal, row in enumerate(rows):
            values = dict(row)
            key = (
                "|".join(str(values.get(name, "")) for name in key_fields)
                if key_fields
                else str(ordinal)
            )
            records.append(
                SourceRecord(
                    values=values,
                    record_key=key,
                    ordinal=ordinal,
                    source_entity=request.entity.source_name,
                )
            )

        return tuple(records)


class MongoSnapshotExtractor(SnapshotExtractor):
    """A bounded ``find()`` over one discovered collection. No commands."""

    def extract(
        self, request: ExtractionRequest, connection_factory: Any
    ) -> tuple[SourceRecord, ...]:
        collection_name = validate_identifier(
            request.entity.source_name, "collection"
        )
        database = connection_factory()
        cursor = (
            database[collection_name]
            .find({}, limit=request.bounded_limit)
            .sort("_id", 1)
        )

        records = []

        for ordinal, document in enumerate(cursor):
            values = {
                key: value for key, value in document.items() if key != "_id"
            }
            # Never use MongoDB's ObjectId as the business identity. A caller
            # that wants EMP002 must declare the field that carries EMP002.
            key = (
                "|".join(str(values.get(name, "")) for name in request.key_fields)
                if request.key_fields
                else None
            )
            records.append(
                SourceRecord(
                    values=values,
                    record_key=key,
                    ordinal=ordinal,
                    source_entity=collection_name,
                    metadata=(
                        {"source_object_id": str(document["_id"])}
                        if document.get("_id") is not None
                        else {}
                    ),
                )
            )

        return tuple(records)


class CsvSnapshotExtractor(SnapshotExtractor):
    """Reuses Phase 6's row reader rather than parsing CSV a second time."""

    def extract_rows(
        self, rows: Any, entity: SourceEntity, limit: int = DEFAULT_BATCH_SIZE
    ) -> tuple[SourceRecord, ...]:
        key_fields = entity.primary_key_fields
        records = []

        for ordinal, row in enumerate(rows):
            if ordinal >= limit:
                break

            values = dict(row.values)
            key = (
                "|".join(str(values.get(name, "")) for name in key_fields)
                if key_fields
                else str(getattr(row, "row_number", ordinal))
            )
            records.append(
                SourceRecord(
                    values=values,
                    record_key=key,
                    ordinal=ordinal,
                    source_entity=entity.source_name,
                )
            )

        return tuple(records)


def extractor_for(source_type: SourceType) -> SnapshotExtractor:
    if source_type in {
        SourceType.POSTGRESQL,
        SourceType.MYSQL,
        SourceType.SQL_SERVER,
    }:
        return RelationalSnapshotExtractor()

    if source_type is SourceType.MONGODB:
        return MongoSnapshotExtractor()

    if source_type is SourceType.CSV:
        return CsvSnapshotExtractor()

    raise UnsupportedCapabilityError(
        f"{source_type.value} has no snapshot extractor; Phase 13 does not "
        "invoke documented API endpoints to obtain records",
        source_type=source_type.value,
    )


__all__ = [
    "SAFE_IDENTIFIER",
    "DEFAULT_BATCH_SIZE",
    "MAX_BATCH_SIZE",
    "validate_identifier",
    "resolve_entity",
    "ExtractionRequest",
    "SnapshotExtractor",
    "RelationalSnapshotExtractor",
    "MongoSnapshotExtractor",
    "CsvSnapshotExtractor",
    "extractor_for",
]
