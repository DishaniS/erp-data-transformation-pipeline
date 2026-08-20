"""Domain error hierarchy for the schema catalog.

Every catalog operation that can fail raises one of these instead of letting a
raw SQLAlchemy or driver exception cross the public API. A caller that catches
``CatalogError`` has caught every catalog failure; a caller that wants detail
can inspect ``__cause__``, which is always preserved via ``raise ... from exc``
at the point the underlying error was translated.
"""

from __future__ import annotations


class CatalogError(Exception):
    """Base class for every catalog-layer error."""


class CatalogConfigurationError(CatalogError):
    """Raised when the catalog's PostgreSQL configuration is missing or invalid."""


class CatalogConnectionError(CatalogError):
    """Raised when the catalog database cannot be reached."""


class SourceSystemNotFoundError(CatalogError):
    """Raised when a referenced ``source_system_id`` has no catalog row."""


class SourceSystemIdentityConflictError(CatalogError):
    """Raised when saving a SourceSystem would silently change its source_type.

    ``source_system_id`` is meant to identify one system for its lifetime.
    Changing what technology it names part-way through would retroactively
    change the meaning of every schema snapshot and mapping profile already
    filed under that id.
    """


class SchemaSnapshotNotFoundError(CatalogError):
    """Raised when a referenced ``schema_id`` (or scope) has no catalog row."""


class SchemaIdentityConflictError(CatalogError):
    """Raised when a ``schema_id`` is reused for structurally different content.

    A ``schema_id`` is an immutable identity: once a snapshot is filed under it,
    that snapshot's content must never change. A caller that wants to persist
    changed content must mint a new ``schema_id`` (typically by producing a new
    ``SourceSchema`` with a fresh id) rather than reuse the old one - the
    catalog version, not the schema id, is what tracks change over time.
    """


class MappingProfileNotFoundError(CatalogError):
    """Raised when a referenced ``mapping_id`` has no catalog row."""


class CatalogIntegrityError(CatalogError):
    """Raised when the catalog's own invariants are violated.

    Covers cases the database schema's constraints do not fully express, such
    as a snapshot save that would leave a schema in a state where its counts
    or version sequence are inconsistent.
    """


__all__ = [
    "CatalogError",
    "CatalogConfigurationError",
    "CatalogConnectionError",
    "SourceSystemNotFoundError",
    "SourceSystemIdentityConflictError",
    "SchemaSnapshotNotFoundError",
    "SchemaIdentityConflictError",
    "MappingProfileNotFoundError",
    "CatalogIntegrityError",
]
