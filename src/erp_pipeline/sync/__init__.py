"""Incremental synchronization and schema drift for the ERP pipeline (Phase 10).

Two independent questions, one package::

    DATA CHANGE                          STRUCTURAL CHANGE
    -----------                          -----------------
    Source changed                       Current SourceSchema
         |                                    + previous
         v                                         |
    Connector detects changes                      v
         |                                    SchemaDiff  (Phase 2, reused)
         v                                         |
    Extract changed records ONLY                   v
         |                                    Mapping impact
         v                                         |
    Phase 9 TransformationService                  v
         |                             continue / review / BLOCK
         v
    CanonicalRecord
         |
         v
    Canonical upsert (idempotent)
         |
         v
    Affected AI representations ONLY
         |
         v
    Rebuild -> deterministic content_hash
         |
    hash changed?
      NO  -> do not re-embed
      YES -> embed -> update the SAME vector identity
         |
         v
    Advance checkpoint - only past changes that finished every stage

WHAT THIS FIXES
---------------
The prototype's incremental path stopped at an intermediate cleaned table.
Downstream case aggregates, AI-ready text, embeddings and vectors were
refreshed only by full rebuild scripts, so a "synced" source change left the
vector store answering from stale content. This package carries one change all
the way down and rebuilds nothing it does not have to.

GUARANTEE
---------
AT-LEAST-ONCE delivery with idempotent downstream upserts. Not exactly-once:
canonical storage, the embedding model and the vector store are independent
systems with no shared transaction, and claiming atomicity across them would be
false. A replay is safe because every downstream write is keyed by a
deterministic identity.

BOUNDARIES
----------
No dataset-specific import, no REST/SOAP endpoint execution, no external LLM,
and no vector-database-specific code - the vector layer is reached through an
interface, so Phase 11/12 can generalize it without touching this engine.
"""

from __future__ import annotations

from erp_pipeline.sync.coordinator import (
    IncrementalCoordinator,
    PropagationPipeline,
    SyncTarget,
)
from erp_pipeline.sync.drift import (
    DriftFinding,
    DriftReport,
    DriftSeverity,
    DriftStatus,
    DriftType,
    detect_drift,
    findings_from_diff,
    max_severity,
)
from erp_pipeline.sync.errors import (
    CheckpointConflictError,
    PropagationError,
    SyncBlockedError,
    SyncConfigurationError,
    SyncError,
    UnsupportedStrategyError,
)
from erp_pipeline.sync.extractor import (
    ConnectorIncrementalExtractor,
    ContentHashChangeSource,
    ExtractionConfig,
    InMemoryChangeSource,
    IncrementalExtractor,
    RelationalIncrementalExtractor,
    build_extraction_sql,
    build_watermark_predicate,
    classify_operation,
    validate_identifier,
    watermark_from_row,
)
from erp_pipeline.sync.hashing import (
    VOLATILE_KEYS,
    representation_content_hash,
    strip_volatile,
    vector_id_for,
)
from erp_pipeline.sync.impact import (
    ImpactAction,
    ImpactKind,
    MappingImpact,
    MappingImpactReport,
    analyze_mapping_impact,
)
from erp_pipeline.sync.models import (
    DEFAULT_SYNC_OPTIONS,
    EMPTY_WATERMARK,
    SYNC_ENGINE_VERSION,
    ChangeOperation,
    ChangeResult,
    FailurePolicy,
    QuarantinedChange,
    SourceChange,
    SyncOptions,
    SyncRunStatus,
    SyncRunSummary,
    SyncStage,
    SyncState,
    SyncStatus,
    Watermark,
    WatermarkStrategy,
)
from erp_pipeline.sync.propagation import (
    AffectedRepresentationResolver,
    AIRepresentation,
    AIRepresentationBuilder,
    CanonicalRecordStore,
    CountingEmbeddingUpdater,
    DictRepresentationBuilder,
    EmbeddingResult,
    EmbeddingUpdater,
    FailingStage,
    InMemoryCanonicalStore,
    InMemoryHashLedger,
    InMemoryVectorStore,
    RepresentationHashLedger,
    StaticAffectedResolver,
    VectorRecordStore,
)
from erp_pipeline.sync.service import SyncResult, SyncService
from erp_pipeline.sync.state import (
    InMemorySyncStateStore,
    PostgresSyncStateStore,
    SYNC_SCHEMA_NAME,
    SYNC_STATE_TABLE,
    SyncStateStore,
    bootstrap_sync_schema,
    ensure_state,
)

__all__ = [
    # errors
    "SyncError",
    "SyncConfigurationError",
    "UnsupportedStrategyError",
    "CheckpointConflictError",
    "SyncBlockedError",
    "PropagationError",
    # models
    "SYNC_ENGINE_VERSION",
    "WatermarkStrategy",
    "Watermark",
    "EMPTY_WATERMARK",
    "SyncStatus",
    "SyncState",
    "ChangeOperation",
    "SourceChange",
    "SyncStage",
    "FailurePolicy",
    "QuarantinedChange",
    "ChangeResult",
    "SyncRunStatus",
    "SyncOptions",
    "DEFAULT_SYNC_OPTIONS",
    "SyncRunSummary",
    # state
    "SYNC_SCHEMA_NAME",
    "SYNC_STATE_TABLE",
    "SyncStateStore",
    "InMemorySyncStateStore",
    "PostgresSyncStateStore",
    "bootstrap_sync_schema",
    "ensure_state",
    # extraction
    "ExtractionConfig",
    "IncrementalExtractor",
    "InMemoryChangeSource",
    "RelationalIncrementalExtractor",
    "ConnectorIncrementalExtractor",
    "ContentHashChangeSource",
    "validate_identifier",
    "build_watermark_predicate",
    "build_extraction_sql",
    "watermark_from_row",
    "classify_operation",
    # hashing
    "VOLATILE_KEYS",
    "strip_volatile",
    "representation_content_hash",
    "vector_id_for",
    # propagation
    "AIRepresentation",
    "EmbeddingResult",
    "CanonicalRecordStore",
    "AffectedRepresentationResolver",
    "AIRepresentationBuilder",
    "RepresentationHashLedger",
    "EmbeddingUpdater",
    "VectorRecordStore",
    "InMemoryCanonicalStore",
    "InMemoryHashLedger",
    "InMemoryVectorStore",
    "StaticAffectedResolver",
    "DictRepresentationBuilder",
    "CountingEmbeddingUpdater",
    "FailingStage",
    # drift
    "DriftType",
    "DriftSeverity",
    "DriftStatus",
    "DriftFinding",
    "DriftReport",
    "detect_drift",
    "findings_from_diff",
    "max_severity",
    # impact
    "ImpactAction",
    "ImpactKind",
    "MappingImpact",
    "MappingImpactReport",
    "analyze_mapping_impact",
    # engine
    "SyncTarget",
    "PropagationPipeline",
    "IncrementalCoordinator",
    "SyncService",
    "SyncResult",
]
