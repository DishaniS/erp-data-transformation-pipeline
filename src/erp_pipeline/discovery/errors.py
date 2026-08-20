"""Domain error hierarchy for schema discovery and inference.

One hierarchy covers both paradigms - relational metadata discovery (Phase 4)
and MongoDB observed-schema inference (Phase 5) - so a caller handling
``DiscoveryError`` catches either without knowing which source produced it.

Mirrors the pattern Phase 2 (`catalog.exceptions`) and Phase 3
(`connectors.errors`) established: every discovery failure surfaces as one of
these instead of a raw SQLAlchemy/driver exception, and ``__cause__`` always
preserves the original for debugging.

No message constructed here may contain a password or a connection URL with
embedded credentials. Where driver text is included it passes through
``erp_pipeline.connectors.errors.redact_text`` first.
"""

from __future__ import annotations


class DiscoveryError(Exception):
    """Base class for every discovery-layer error."""


class UnsupportedDiscoverySourceError(DiscoveryError):
    """Raised when discovery is attempted against a source it cannot handle.

    Raised in both directions rather than silently producing an empty or wrong
    schema: a MongoDB connector handed to relational discovery, and a
    relational connector handed to MongoDB inference, are equally rejected.
    The two entry points read genuinely different things - declared catalog
    metadata versus sampled documents - and neither can stand in for the other.
    """


class MongoInferenceError(DiscoveryError):
    """Raised when MongoDB documents cannot be sampled for inference.

    Deliberately the ONLY error Phase 5 adds. Wrong connector type reuses
    ``UnsupportedDiscoverySourceError`` and failed collection metadata reuses
    ``MetadataInspectionError``, because those failures mean the same thing
    whichever paradigm raised them.

    Safety limits do NOT raise. Exceeding ``max_fields_per_collection``,
    ``max_depth`` or ``max_total_documents`` marks the result explicitly
    partial and records a warning - an inference run that hits a budget has
    still learned something true, and discarding it would be worse than
    reporting it honestly.
    """


class MetadataInspectionError(DiscoveryError):
    """Raised when the database rejects or fails a metadata introspection call.

    Covers permission failures on catalog views, dropped objects observed
    mid-discovery, and dialect quirks that make an Inspector call fail.
    """


class ProfilingError(DiscoveryError):
    """Raised when optional profiling fails in a way the caller asked to see.

    Profiling is best-effort by default: individual failures are recorded on
    the profile result rather than raised, so a profiling problem can never
    fail structural schema discovery.
    """


class ProfilingBudgetExceeded(DiscoveryError):
    """Raised only when a caller explicitly requests strict budget enforcement.

    Default behaviour when a budget is hit is to stop profiling, mark the
    result partial, and continue - never to fail discovery.
    """


__all__ = [
    "DiscoveryError",
    "UnsupportedDiscoverySourceError",
    "MongoInferenceError",
    "MetadataInspectionError",
    "ProfilingError",
    "ProfilingBudgetExceeded",
]
