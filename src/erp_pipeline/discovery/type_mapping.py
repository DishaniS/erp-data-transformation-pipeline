"""Centralized relational datatype normalization.

One mapping table for all three relational engines, in one file, so a new
vendor type is added in exactly one place instead of three.

Two values are produced for every column and must never be conflated - this
is the Phase 1 rule ``SourceField`` was designed around:

    source_data_type      the vendor's own spelling, preserved verbatim
                          including precision/scale: "NUMERIC(12, 2)",
                          "NVARCHAR(50)", "DECIMAL(18, 2)", "TINYINT(1)"

    normalized_data_type  the coarse cross-source FieldDataType a mapping
                          layer reasons about

Classification strategy, in order:

1. Trust SQLAlchemy's own generic type classification (``isinstance`` against
   ``sqlalchemy.types``). SQLAlchemy's dialects already do the hard work of
   deciding that PostgreSQL ``TIMESTAMPTZ`` and SQL Server ``DATETIME2`` are
   both DateTime, so re-deriving that from strings would be redundant and
   less reliable.
2. Fall back to the rendered type string for dialect-specific types
   SQLAlchemy exposes as vendor classes (``JSONB``, ``BYTEA``,
   ``UNIQUEIDENTIFIER``, ``MONEY``, ...).
3. Anything still unrecognized becomes ``FieldDataType.UNKNOWN`` - honestly
   unknown, never guessed.
"""

from __future__ import annotations

from typing import Any

from erp_pipeline.schemas.enums import FieldDataType

# Vendor type-name prefixes checked when SQLAlchemy's generic classification
# does not resolve the type. Order matters: the first matching prefix wins,
# so longer/more specific names are listed before shorter ones that would
# also prefix-match them.
_STRING_PREFIX_MAP: tuple[tuple[str, FieldDataType], ...] = (
    # --- binary (before anything that could prefix-collide) ---
    ("VARBINARY", FieldDataType.BINARY),
    ("BYTEA", FieldDataType.BINARY),
    ("LONGBLOB", FieldDataType.BINARY),
    ("MEDIUMBLOB", FieldDataType.BINARY),
    ("TINYBLOB", FieldDataType.BINARY),
    ("BLOB", FieldDataType.BINARY),
    ("BINARY", FieldDataType.BINARY),
    ("IMAGE", FieldDataType.BINARY),
    # --- structured ---
    ("JSONB", FieldDataType.OBJECT),
    ("JSON", FieldDataType.OBJECT),
    ("HSTORE", FieldDataType.OBJECT),
    ("ARRAY", FieldDataType.ARRAY),
    # --- identifiers / text-ish ---
    ("UNIQUEIDENTIFIER", FieldDataType.STRING),
    ("UUID", FieldDataType.STRING),
    ("XML", FieldDataType.STRING),
    ("NVARCHAR", FieldDataType.STRING),
    ("NCHAR", FieldDataType.STRING),
    ("NTEXT", FieldDataType.STRING),
    ("LONGTEXT", FieldDataType.STRING),
    ("MEDIUMTEXT", FieldDataType.STRING),
    ("TINYTEXT", FieldDataType.STRING),
    ("VARCHAR", FieldDataType.STRING),
    ("CHARACTER VARYING", FieldDataType.STRING),
    ("CITEXT", FieldDataType.STRING),
    ("TEXT", FieldDataType.STRING),
    ("CHAR", FieldDataType.STRING),
    ("ENUM", FieldDataType.STRING),
    ("SET", FieldDataType.STRING),
    ("INET", FieldDataType.STRING),
    ("CIDR", FieldDataType.STRING),
    ("MACADDR", FieldDataType.STRING),
    # --- numeric ---
    ("SMALLMONEY", FieldDataType.DECIMAL),
    ("MONEY", FieldDataType.DECIMAL),
    ("NUMERIC", FieldDataType.DECIMAL),
    ("DECIMAL", FieldDataType.DECIMAL),
    ("DOUBLE", FieldDataType.DECIMAL),
    ("FLOAT", FieldDataType.DECIMAL),
    ("REAL", FieldDataType.DECIMAL),
    ("BIGINT", FieldDataType.INTEGER),
    ("SMALLINT", FieldDataType.INTEGER),
    ("TINYINT", FieldDataType.INTEGER),
    ("MEDIUMINT", FieldDataType.INTEGER),
    ("INTEGER", FieldDataType.INTEGER),
    ("INT", FieldDataType.INTEGER),
    ("SERIAL", FieldDataType.INTEGER),
    # --- boolean ---
    ("BOOLEAN", FieldDataType.BOOLEAN),
    ("BOOL", FieldDataType.BOOLEAN),
    ("BIT", FieldDataType.BOOLEAN),
    # --- temporal (DATETIME family before DATE) ---
    ("SMALLDATETIME", FieldDataType.DATETIME),
    ("DATETIMEOFFSET", FieldDataType.DATETIME),
    ("DATETIME2", FieldDataType.DATETIME),
    ("DATETIME", FieldDataType.DATETIME),
    ("TIMESTAMP", FieldDataType.DATETIME),
    ("DATE", FieldDataType.DATE),
)


def render_source_data_type(sqlalchemy_type: Any, dialect: Any | None = None) -> str:
    """Render the vendor-visible type string, preserving precision and scale.

    Compiling against the source's own dialect yields the most faithful
    spelling (``NVARCHAR(50)`` rather than a generic rendering). Falls back to
    ``str()`` when a dialect cannot compile the type - some dialect-specific
    types raise when compiled by a different dialect.
    """
    if dialect is not None:
        try:
            return str(sqlalchemy_type.compile(dialect=dialect))
        except Exception:
            pass

    try:
        return str(sqlalchemy_type)
    except Exception:
        return type(sqlalchemy_type).__name__


def normalize_data_type(sqlalchemy_type: Any, rendered: str | None = None) -> FieldDataType:
    """Map a SQLAlchemy reflected column type to the common FieldDataType."""
    generic = _classify_by_sqlalchemy_class(sqlalchemy_type)
    if generic is not None:
        return generic

    text = (rendered if rendered is not None else render_source_data_type(sqlalchemy_type))
    return normalize_type_name(text)


def normalize_type_name(type_name: str | None) -> FieldDataType:
    """Map a raw vendor type *string* to the common FieldDataType.

    Exposed separately so the mapping can be unit-tested against literal
    vendor spellings without constructing SQLAlchemy type objects.
    """
    if not type_name:
        return FieldDataType.UNKNOWN

    upper = type_name.strip().upper()

    # "character varying(100)" / "NUMERIC(12, 2)" -> compare on the bare name,
    # but keep multi-word names such as "CHARACTER VARYING" and
    # "TIMESTAMP WITH TIME ZONE" intact for prefix matching.
    for prefix, data_type in _STRING_PREFIX_MAP:
        if upper.startswith(prefix):
            return data_type

    return FieldDataType.UNKNOWN


def _classify_by_sqlalchemy_class(sqlalchemy_type: Any) -> FieldDataType | None:
    """Classify using SQLAlchemy's own generic type hierarchy.

    Returns ``None`` when the type is a dialect-specific class that does not
    inherit from a generic SQLAlchemy type, leaving it to string matching.
    """
    try:
        from sqlalchemy import types as sqltypes
    except Exception:  # pragma: no cover - SQLAlchemy is a hard dependency
        return None

    # Order matters: Boolean before Integer (some dialects model BOOLEAN on an
    # integer base), DateTime before Date, Enum before String.
    checks: tuple[tuple[type, FieldDataType], ...] = (
        (sqltypes.Boolean, FieldDataType.BOOLEAN),
        (sqltypes.DateTime, FieldDataType.DATETIME),
        (sqltypes.Date, FieldDataType.DATE),
        (sqltypes.LargeBinary, FieldDataType.BINARY),
        (sqltypes.JSON, FieldDataType.OBJECT),
        (sqltypes.ARRAY, FieldDataType.ARRAY),
        (sqltypes.Numeric, FieldDataType.DECIMAL),
        (sqltypes.Integer, FieldDataType.INTEGER),
        (sqltypes.Enum, FieldDataType.STRING),
        (sqltypes.String, FieldDataType.STRING),
    )

    for sqlalchemy_class, data_type in checks:
        try:
            if isinstance(sqlalchemy_type, sqlalchemy_class):
                return data_type
        except TypeError:  # pragma: no cover - defensive
            continue

    # sqlalchemy.Uuid exists in SQLAlchemy 2.x; guard for older versions.
    uuid_type = getattr(sqltypes, "Uuid", None)
    if uuid_type is not None and isinstance(sqlalchemy_type, uuid_type):
        return FieldDataType.STRING

    return None


__all__ = [
    "render_source_data_type",
    "normalize_data_type",
    "normalize_type_name",
]
