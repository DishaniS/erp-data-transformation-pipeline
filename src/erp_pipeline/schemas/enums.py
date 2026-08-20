"""Closed classification vocabularies for the ERP pipeline contracts.

Closed enum vs open string
--------------------------
A value is an enum here only when the set is genuinely controlled by this
framework, so that adding a member is a deliberate code change. A value stays
an open normalized string when the real world - not this codebase - decides
what the valid values are.

Enums (closed):
    SourceType, RecordType, SensitivityLevel, FieldDataType, SchemaOrigin,
    EntityKind, RelationshipType, MappingStatus, RunStatus, QualitySeverity,
    TransformationOperation

Open normalized strings (deliberately NOT enums):
    entity_type       Business object being represented: invoice, customer,
                      purchase_order, goods_receipt, cost_center, ... No fixed
                      list can cover every ERP domain, and the framework's
                      value is that a new domain object needs no code change.
    semantic_type     Field meaning: email, currency_code, iban, tax_id, ...
                      Grows continuously and is often organization-specific.
    environment       Deployment label: research, dev, uat, production, ...
                      Organizations name these arbitrarily.
    ingestion_method  How a record was obtained: batch_extract, incremental_
                      sync, file_upload, api_pull, ... New connectors add new
                      values without touching this module.

Open strings are still validated: they must be normalized identifiers (see
``validation.validate_normalized_name``), so they remain safe inside composite
IDs, filenames, URLs and payload filters.

Every enum subclasses ``str`` so members compare equal to their wire value and
serialize predictably.
"""

from __future__ import annotations

from enum import Enum
from typing import TypeVar

_E = TypeVar("_E", bound="_ContractEnum")


class _ContractEnum(str, Enum):
    """Base for the contract vocabularies.

    Subclassing ``str`` means a member IS its wire value, so ``json.dumps``
    and equality against a plain string both behave as expected.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @classmethod
    def from_value(cls: type[_E], value: object) -> _E:
        """Convert a wire value to a member, with an actionable error.

        The default ``ValueError`` from ``Enum`` does not list the valid
        options, which makes a typo in stored data annoying to diagnose.
        """
        if isinstance(value, cls):
            return value

        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass

        allowed = ", ".join(member.value for member in cls)
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__}. Allowed values: {allowed}."
        )

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(member.value for member in cls)


class SourceType(_ContractEnum):
    """Technology a source system speaks.

    Closed on purpose. Supporting a new source technology always requires a
    connector, a discovery strategy and a type-normalization table, so it is a
    deliberate framework change and must not be expressible as a free string.
    An unrecognized value is rejected rather than passed through.
    """

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    SQL_SERVER = "sql_server"
    MONGODB = "mongodb"
    CSV = "csv"
    PDF = "pdf"
    IMAGE = "image"
    OPENAPI = "openapi"
    POSTMAN = "postman"


class RecordType(_ContractEnum):
    """Shape of a canonical record.

    This vocabulary belongs to ``erp_pipeline`` alone. It is intentionally
    NOT the same vocabulary as the Phase 0 BPI unified layer, which uses
    ``erp_case`` / ``erp_document``. The two namespaces are independent.
    """

    STRUCTURED_RECORD = "structured_record"
    DOCUMENT = "document"
    EVENT = "event"
    CASE = "case"
    API_RECORD = "api_record"


class SensitivityLevel(_ContractEnum):
    """Handling classification carried by every canonical record."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class FieldDataType(_ContractEnum):
    """Normalized cross-source type lattice.

    Deliberately coarse. Vendor precision (``VARCHAR(100)``, ``DECIMAL(12,2)``,
    ``NVARCHAR``, ``ObjectId``) is preserved verbatim in
    ``SourceField.source_data_type``; this enum only says how the value behaves
    once normalized, so that mappings can reason across MySQL, SQL Server,
    MongoDB and OpenAPI without a combinatorial type matrix.
    """

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    DATE = "date"
    BINARY = "binary"
    OBJECT = "object"
    ARRAY = "array"
    UNKNOWN = "unknown"


class SchemaOrigin(_ContractEnum):
    """How a source schema description came to exist."""

    DISCOVERED = "discovered"
    INFERRED = "inferred"
    UPLOADED = "uploaded"
    API_SPEC = "api_spec"
    MANUAL = "manual"


class EntityKind(_ContractEnum):
    """Structural shape of a source entity.

    ``OTHER`` is an explicit escape hatch so an unusual source shape can be
    represented without inventing an untyped string, while keeping
    serialization predictable. Details go in ``SourceEntity.metadata``.
    """

    TABLE = "table"
    VIEW = "view"
    COLLECTION = "collection"
    DATASET = "dataset"
    API_SCHEMA = "api_schema"
    DOCUMENT_SET = "document_set"
    OTHER = "other"


class RelationshipType(_ContractEnum):
    """How two source entities relate.

    Covers non-relational shapes deliberately: MongoDB embedding and generic
    parent/child nesting are first-class, not modelled as degenerate foreign
    keys.
    """

    FOREIGN_KEY = "foreign_key"
    REFERENCE = "reference"
    EMBEDDED = "embedded"
    PARENT_CHILD = "parent_child"
    INFERRED = "inferred"


class MappingStatus(_ContractEnum):
    """Review state of a mapping decision."""

    AUTO_ACCEPTED = "auto_accepted"
    SUGGESTED = "suggested"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    REJECTED = "rejected"


class RunStatus(_ContractEnum):
    """Lifecycle state of a transformation run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """True when the run has finished and cannot change state again."""
        return self in _TERMINAL_RUN_STATUSES


_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.PARTIAL, RunStatus.FAILED}
)


class QualitySeverity(_ContractEnum):
    """Severity of a data-quality finding."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class TransformationOperation(_ContractEnum):
    """Declarative operations a future mapping engine may implement.

    This enum exists so a transformation can be expressed as DATA - an
    operation name plus a JSON configuration - instead of executable code.
    Nothing here is implemented in Phase 1; the engine that interprets these
    operations belongs to a later phase, and it must dispatch on these members
    rather than evaluating anything supplied by a mapping author.
    """

    COPY = "copy"
    RENAME = "rename"
    CAST = "cast"
    DEFAULT = "default"
    ENUM_MAP = "enum_map"
    NESTED_PATH = "nested_path"
    DATE_PARSE = "date_parse"
    CONCAT = "concat"
    SPLIT = "split"
    TRIM = "trim"
    CONSTANT = "constant"
    REDACT = "redact"


__all__ = [
    "SourceType",
    "RecordType",
    "SensitivityLevel",
    "FieldDataType",
    "SchemaOrigin",
    "EntityKind",
    "RelationshipType",
    "MappingStatus",
    "RunStatus",
    "QualitySeverity",
    "TransformationOperation",
]
