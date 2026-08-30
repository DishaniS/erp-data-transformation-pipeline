"""Hybrid tiered vector storage for the ERP pipeline (Phase 12).

                    EmbeddingRecord (Phase 11)
                            │
                            ▼
                     StorageService
                            │
                            ▼
                  StoragePolicyRouter        hard constraints, THEN scores
                            │
                            ▼
                 authoritative tier state    durable, version-guarded
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
       HOT                 WARM                COLD
  full precision      int8 + on-disk      gzip + AES-256-GCM
  Qdrant, in RAM      Qdrant              encrypted archive files
        └───────────────────┼───────────────────┘
                            ▼
                    HybridVectorStore        one facade, HOT+WARM search
                            │
                      TierMonitor            evaluate → plan → execute

WHAT MAKES THIS RESEARCH RATHER THAN STRUCTURE
----------------------------------------------
Not three folders named hot, warm and cold. Three things:

1. HARD CONSTRAINTS ARE APPLIED BEFORE SCORING, not as a penalty. A prohibited
   tier is removed from the candidate set, so no cost advantage can ever reach
   it. That is what makes "restricted data stays on-premises" a guarantee.

2. THE TIERS DIFFER MEASURABLY. WARM is server-verified int8 scalar
   quantization with on-disk vectors; COLD is really compressed and really
   authenticated-encrypted. Every claim is read back from the server or the
   filesystem.

3. THE MEASUREMENTS ARE LABELLED. Cold bytes are MEASURED; Qdrant bytes are a
   PROXY; the cost multipliers are ESTIMATED assumptions. They are never
   presented as the same kind of number.

BOUNDARIES
----------
No REST API, no UI, no RAG, no LLM and no required cloud service. Phase 12
decides WHERE a vector lives and proves the trade-off; Phase 13 orchestrates.
"""

from __future__ import annotations

from erp_pipeline.storage.cold_tier import (
    ARCHIVE_SUFFIX,
    COLD_FORMAT_VERSION,
    COLD_KEY_ENV,
    COMPRESSION_ALGORITHM,
    ENCRYPTION_ALGORITHM,
    KEY_BYTES,
    NONCE_BYTES,
    ColdArchiveEnvelope,
    ColdArchiveTier,
    ColdEncryptionKeyProvider,
    EnvironmentKeyProvider,
    StaticKeyProvider,
    generate_key,
)
from erp_pipeline.storage.cost import (
    DEFAULT_COST_MODEL,
    DEFAULT_RESOURCE_MULTIPLIERS,
    MULTIPLIER_RATIONALE,
    CostModel,
    TierCost,
)
from erp_pipeline.storage.errors import (
    ColdArchiveIntegrityError,
    ColdArchiveNotFoundError,
    ConcurrencyConflictError,
    EncryptionKeyUnavailableError,
    MigrationError,
    PolicyViolationError,
    RetentionProtectedError,
    StorageConfigurationError,
    StorageError,
    TierUnavailableError,
    VectorIdentityMismatchError,
)
from erp_pipeline.storage.hot_tier import FLOAT32_BYTES, QdrantHotTier
from erp_pipeline.storage.hybrid_store import (
    HybridVectorStore,
    SearchHit,
    SearchResult,
)
from erp_pipeline.storage.metrics import (
    LatencySample,
    RecallResult,
    evaluate_recall,
    measure_latency,
    ranking_overlap,
    vector_payload_proxy,
)
from erp_pipeline.storage.migration import (
    VECTOR_TOLERANCE,
    MigrationEngine,
    TierSet,
)
from erp_pipeline.storage.models import (
    STORAGE_ENGINE_VERSION,
    BusinessCriticality,
    FactorContribution,
    LatencyRequirement,
    MeasurementKind,
    MigrationPlan,
    MigrationResult,
    PlannedMigration,
    RoutingDecision,
    StorageFootprint,
    StorageLocation,
    StorageRecordMetadata,
    StorageRoutingContext,
    StorageTier,
    TierHealth,
    TierScore,
    TierTransition,
    TransitionReason,
    make_transition_id,
)
from erp_pipeline.storage.service import (
    DEFAULT_PROFILE,
    StorageProfile,
    StorageService,
)
from erp_pipeline.storage.state import (
    ACCESS_TABLE,
    STATE_TABLE,
    STORAGE_SCHEMA_NAME,
    TRANSITIONS_TABLE,
    InMemoryTierStateStore,
    PostgresTierStateStore,
    TierStateStore,
    bootstrap_storage_schema,
)
from erp_pipeline.storage.storage_policy import (
    DEFAULT_POLICY,
    DEFAULT_TIER_LOCATIONS,
    StoragePolicy,
    TierWeights,
)
from erp_pipeline.storage.tier_monitor import EvaluationEntry, TierMonitor
from erp_pipeline.storage.filters import (
    FILTERABLE_FIELDS,
    NO_FILTERS,
    InvalidFilterValueError,
    SearchFilters,
    UnknownFilterFieldError,
)
from erp_pipeline.storage.vector_router import StoragePolicyRouter
from erp_pipeline.storage.warm_tier import INT8_BYTES, QdrantWarmTier

__all__ = [
    # errors
    "StorageError",
    "StorageConfigurationError",
    "PolicyViolationError",
    "TierUnavailableError",
    "MigrationError",
    "ConcurrencyConflictError",
    "EncryptionKeyUnavailableError",
    "ColdArchiveIntegrityError",
    "ColdArchiveNotFoundError",
    "RetentionProtectedError",
    "VectorIdentityMismatchError",
    # models
    "STORAGE_ENGINE_VERSION",
    "StorageTier",
    "StorageLocation",
    "BusinessCriticality",
    "LatencyRequirement",
    "TransitionReason",
    "StorageRoutingContext",
    "FactorContribution",
    "TierScore",
    "RoutingDecision",
    "StorageRecordMetadata",
    "TierTransition",
    "make_transition_id",
    "PlannedMigration",
    "MigrationPlan",
    "MigrationResult",
    "TierHealth",
    "MeasurementKind",
    "StorageFootprint",
    # policy and routing
    "TierWeights",
    "StoragePolicy",
    "DEFAULT_POLICY",
    "DEFAULT_TIER_LOCATIONS",
    "StoragePolicyRouter",
    # retrieval filters
    "SearchFilters",
    "FILTERABLE_FIELDS",
    "NO_FILTERS",
    "UnknownFilterFieldError",
    "InvalidFilterValueError",
    # tiers
    "QdrantHotTier",
    "QdrantWarmTier",
    "ColdArchiveTier",
    "FLOAT32_BYTES",
    "INT8_BYTES",
    "COLD_FORMAT_VERSION",
    "COLD_KEY_ENV",
    "ENCRYPTION_ALGORITHM",
    "COMPRESSION_ALGORITHM",
    "NONCE_BYTES",
    "KEY_BYTES",
    "ARCHIVE_SUFFIX",
    "ColdArchiveEnvelope",
    "ColdEncryptionKeyProvider",
    "EnvironmentKeyProvider",
    "StaticKeyProvider",
    "generate_key",
    # state
    "STORAGE_SCHEMA_NAME",
    "STATE_TABLE",
    "TRANSITIONS_TABLE",
    "ACCESS_TABLE",
    "TierStateStore",
    "InMemoryTierStateStore",
    "PostgresTierStateStore",
    "bootstrap_storage_schema",
    # engine
    "TierSet",
    "MigrationEngine",
    "VECTOR_TOLERANCE",
    "HybridVectorStore",
    "SearchHit",
    "SearchResult",
    "TierMonitor",
    "EvaluationEntry",
    "StorageService",
    "StorageProfile",
    "DEFAULT_PROFILE",
    # metrics and cost
    "LatencySample",
    "measure_latency",
    "vector_payload_proxy",
    "RecallResult",
    "evaluate_recall",
    "ranking_overlap",
    "CostModel",
    "TierCost",
    "DEFAULT_COST_MODEL",
    "DEFAULT_RESOURCE_MULTIPLIERS",
    "MULTIPLIER_RATIONALE",
]
