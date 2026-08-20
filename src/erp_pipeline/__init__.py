"""Generic, source-independent ERP transformation framework.

This package is being built incrementally alongside ``bpi2020``, which remains
the stabilized source-specific research prototype. ``erp_pipeline`` never
imports or executes ``bpi2020``; the two are siblings, and ``bpi2020`` will
later be re-expressed as one source integration for this framework.

Phase 1 scope
-------------
This package currently contains ONLY data contracts - the models that describe
a source ERP system, its schemas, the canonical representation every source
converges on, the mapping definitions a future engine will consume, and the
run/quality records a future orchestrator will emit.

The contracts perform no I/O. There are no database connections, no SQL, no
file parsing, no network calls and no vector-store operations anywhere in this
package, and Phase 1 adds no third-party dependency.
"""

from __future__ import annotations

from erp_pipeline.version import (
    CANONICAL_MODEL_VERSION,
    MAPPING_MODEL_VERSION,
    RUN_MODEL_VERSION,
    SOURCE_MODEL_VERSION,
)

__all__ = [
    "CANONICAL_MODEL_VERSION",
    "SOURCE_MODEL_VERSION",
    "MAPPING_MODEL_VERSION",
    "RUN_MODEL_VERSION",
]
