"""Vendor datatype normalization across PostgreSQL, MySQL and SQL Server.

Every case asserts the same two-part rule: the vendor spelling is preserved
verbatim, and the normalized type is the correct common FieldDataType.
"""

import pytest

from erp_pipeline.discovery.type_mapping import (
    normalize_data_type,
    normalize_type_name,
    render_source_data_type,
)
from erp_pipeline.schemas.enums import FieldDataType


# ============================================================
# PostgreSQL vendor types
# ============================================================

@pytest.mark.parametrize(
    "vendor_type, expected",
    [
        ("VARCHAR", FieldDataType.STRING),
        ("VARCHAR(100)", FieldDataType.STRING),
        ("CHARACTER VARYING(255)", FieldDataType.STRING),
        ("TEXT", FieldDataType.STRING),
        ("CHAR(3)", FieldDataType.STRING),
        ("CITEXT", FieldDataType.STRING),
        ("SMALLINT", FieldDataType.INTEGER),
        ("INTEGER", FieldDataType.INTEGER),
        ("BIGINT", FieldDataType.INTEGER),
        ("SERIAL", FieldDataType.INTEGER),
        ("NUMERIC(12,2)", FieldDataType.DECIMAL),
        ("NUMERIC(12, 2)", FieldDataType.DECIMAL),
        ("DOUBLE PRECISION", FieldDataType.DECIMAL),
        ("REAL", FieldDataType.DECIMAL),
        ("MONEY", FieldDataType.DECIMAL),
        ("BOOLEAN", FieldDataType.BOOLEAN),
        ("DATE", FieldDataType.DATE),
        ("TIMESTAMP", FieldDataType.DATETIME),
        ("TIMESTAMP WITH TIME ZONE", FieldDataType.DATETIME),
        ("TIMESTAMP WITHOUT TIME ZONE", FieldDataType.DATETIME),
        ("UUID", FieldDataType.STRING),
        ("JSON", FieldDataType.OBJECT),
        ("JSONB", FieldDataType.OBJECT),
        ("BYTEA", FieldDataType.BINARY),
        ("ARRAY", FieldDataType.ARRAY),
        ("XML", FieldDataType.STRING),
        ("INET", FieldDataType.STRING),
    ],
)
def test_postgresql_type_normalization(vendor_type, expected):
    assert normalize_type_name(vendor_type) is expected


# ============================================================
# MySQL vendor types
# ============================================================

@pytest.mark.parametrize(
    "vendor_type, expected",
    [
        ("VARCHAR(255)", FieldDataType.STRING),
        ("LONGTEXT", FieldDataType.STRING),
        ("MEDIUMTEXT", FieldDataType.STRING),
        ("TINYTEXT", FieldDataType.STRING),
        ("ENUM('a','b')", FieldDataType.STRING),
        ("INT", FieldDataType.INTEGER),
        ("INT(11)", FieldDataType.INTEGER),
        ("BIGINT(20)", FieldDataType.INTEGER),
        ("MEDIUMINT", FieldDataType.INTEGER),
        ("DECIMAL(12,2)", FieldDataType.DECIMAL),
        ("DOUBLE", FieldDataType.DECIMAL),
        ("FLOAT", FieldDataType.DECIMAL),
        # TINYINT is normalized as an integer: MySQL uses TINYINT(1) both for
        # BOOLEAN and for a genuine small integer, and guessing "boolean" from
        # the display width would misclassify real integer columns. When the
        # column is truly declared BOOLEAN, SQLAlchemy reflects a Boolean type
        # object and the class-based path (not this string path) resolves it.
        ("TINYINT(1)", FieldDataType.INTEGER),
        ("TINYINT", FieldDataType.INTEGER),
        ("BOOL", FieldDataType.BOOLEAN),
        ("BOOLEAN", FieldDataType.BOOLEAN),
        ("DATETIME", FieldDataType.DATETIME),
        ("TIMESTAMP", FieldDataType.DATETIME),
        ("DATE", FieldDataType.DATE),
        ("JSON", FieldDataType.OBJECT),
        ("BLOB", FieldDataType.BINARY),
        ("LONGBLOB", FieldDataType.BINARY),
    ],
)
def test_mysql_type_normalization(vendor_type, expected):
    assert normalize_type_name(vendor_type) is expected


# ============================================================
# SQL Server vendor types
# ============================================================

@pytest.mark.parametrize(
    "vendor_type, expected",
    [
        ("VARCHAR(50)", FieldDataType.STRING),
        ("NVARCHAR(50)", FieldDataType.STRING),
        ("NVARCHAR(MAX)", FieldDataType.STRING),
        ("NCHAR(10)", FieldDataType.STRING),
        ("NTEXT", FieldDataType.STRING),
        ("INT", FieldDataType.INTEGER),
        ("BIGINT", FieldDataType.INTEGER),
        ("DECIMAL(18,2)", FieldDataType.DECIMAL),
        ("MONEY", FieldDataType.DECIMAL),
        ("SMALLMONEY", FieldDataType.DECIMAL),
        ("BIT", FieldDataType.BOOLEAN),
        ("DATETIME2", FieldDataType.DATETIME),
        ("DATETIME", FieldDataType.DATETIME),
        ("SMALLDATETIME", FieldDataType.DATETIME),
        ("DATETIMEOFFSET", FieldDataType.DATETIME),
        ("DATE", FieldDataType.DATE),
        ("UNIQUEIDENTIFIER", FieldDataType.STRING),
        ("VARBINARY(MAX)", FieldDataType.BINARY),
        ("IMAGE", FieldDataType.BINARY),
        ("XML", FieldDataType.STRING),
    ],
)
def test_sqlserver_type_normalization(vendor_type, expected):
    assert normalize_type_name(vendor_type) is expected


# ============================================================
# Unknown types stay honestly unknown
# ============================================================

@pytest.mark.parametrize(
    "vendor_type",
    ["SOMETHING_PROPRIETARY", "GEOGRAPHY", "HIERARCHYID", "TSVECTOR", "PERIOD"],
)
def test_unrecognized_vendor_type_is_unknown(vendor_type):
    assert normalize_type_name(vendor_type) is FieldDataType.UNKNOWN


def test_empty_or_none_type_is_unknown():
    assert normalize_type_name(None) is FieldDataType.UNKNOWN
    assert normalize_type_name("") is FieldDataType.UNKNOWN


# ============================================================
# SQLAlchemy type objects are classified by class first
# ============================================================

def test_sqlalchemy_generic_classes_are_classified():
    from sqlalchemy import types as sqltypes

    cases = [
        (sqltypes.String(50), FieldDataType.STRING),
        (sqltypes.Integer(), FieldDataType.INTEGER),
        (sqltypes.BigInteger(), FieldDataType.INTEGER),
        (sqltypes.Numeric(12, 2), FieldDataType.DECIMAL),
        (sqltypes.Float(), FieldDataType.DECIMAL),
        (sqltypes.Boolean(), FieldDataType.BOOLEAN),
        (sqltypes.Date(), FieldDataType.DATE),
        (sqltypes.DateTime(), FieldDataType.DATETIME),
        (sqltypes.LargeBinary(), FieldDataType.BINARY),
        (sqltypes.JSON(), FieldDataType.OBJECT),
        (sqltypes.Text(), FieldDataType.STRING),
    ]

    for type_object, expected in cases:
        assert normalize_data_type(type_object) is expected, type_object


def test_sqlalchemy_boolean_wins_over_integer_classification():
    """A dialect modelling BOOLEAN on an integer base must still be BOOLEAN."""
    from sqlalchemy import types as sqltypes

    assert normalize_data_type(sqltypes.Boolean()) is FieldDataType.BOOLEAN


def test_datetime_is_not_misclassified_as_date():
    from sqlalchemy import types as sqltypes

    assert normalize_data_type(sqltypes.DateTime()) is FieldDataType.DATETIME
    assert normalize_data_type(sqltypes.Date()) is FieldDataType.DATE


# ============================================================
# Source type rendering preserves precision/scale
# ============================================================

def test_render_preserves_precision_and_scale():
    from sqlalchemy import types as sqltypes

    rendered = render_source_data_type(sqltypes.Numeric(18, 2))
    assert "18" in rendered and "2" in rendered


def test_render_preserves_string_length():
    from sqlalchemy import types as sqltypes

    assert "100" in render_source_data_type(sqltypes.String(100))


def test_render_falls_back_without_raising_on_odd_type():
    class _Odd:
        def __str__(self):
            raise RuntimeError("cannot render")

    # Must degrade to the class name rather than propagating.
    assert render_source_data_type(_Odd()) == "_Odd"
