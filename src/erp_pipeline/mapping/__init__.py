"""Explainable source-to-canonical mapping for the generic ERP pipeline.

Phase 8 answers, for every field of every source:

    "Which canonical ERP field is the best compatible target,
     how confident are we, and WHY?"

It does not answer "transform this record" - that is Phase 9, and nothing in
this package can execute a transformation.

Position in the architecture::

    PostgreSQL  MySQL  SQL Server  MongoDB  CSV  OpenAPI  Postman
                              |
                              v
                        SourceSchema              (Phases 4-7)
                              |
                              v
                      Phase 8 Mapping Engine      THIS PACKAGE
                              |
              +---------------+---------------+
              |               |               |
           names           types           context
          aliases      compatibility    entity + path
              |               |               |
              +---------------+---------------+
                              v
                   MappingCandidate + Evidence
                              v
                        MappingProfile            (Phase 1 contract)
                              v
                    Phase 2 Schema Catalog

The engine is SOURCE-INDEPENDENT: it contains no branch on where a schema came
from. Seven technologies converge on one contract in Phases 4-7, and this phase
is what that convergence was for.

It is CONSERVATIVE: three independent gates - score, margin over the runner-up,
and type compatibility - must all pass before anything is selected
automatically. A field with no honest answer stays unmapped, because a wrong
mapping is worse than an absent one.

It is EXPLAINABLE: no candidate exists without the evidence that produced it.
Every score decomposes into name, type, entity-context and path-context
contributions, and every alias match quotes the declaration it came from.

It is OFFLINE and DETERMINISTIC: no network, no LLM, no embeddings, no
randomness. The same schema and configuration always produce the same
candidates, the same order and the same profile identity.

It reads SCHEMAS, never DATA: no business value, document, row or example
payload is required or consulted.

This package never imports ``bpi2020``.
"""

from __future__ import annotations

from erp_pipeline.mapping.aliases import AliasHit, AliasIndex, build_alias_index
from erp_pipeline.mapping.canonical_model import (
    DEFAULT_CANONICAL_MODEL,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_VERSION,
    REPOSITORY_INVOICE_FIELDS,
    CanonicalEntity,
    CanonicalField,
    CanonicalTargetModel,
    FieldProvenance,
)
from erp_pipeline.mapping.compatibility import (
    TypeComparison,
    TypeCompatibility,
    compare_types,
    compatibility_matrix,
)
from erp_pipeline.mapping.coverage import compute_coverage
from erp_pipeline.mapping.engine import MappingEngine, find_source_field
from erp_pipeline.mapping.errors import (
    CanonicalTargetNotFoundError,
    InvalidMappingOverrideError,
    MappingConfigurationError,
    MappingEngineError,
    MappingValidationError,
    SourceFieldNotFoundError,
)
from erp_pipeline.mapping.models import (
    DEFAULT_OPTIONS,
    DEFAULT_WEIGHTS,
    MAPPING_ENGINE_VERSION,
    ConfidenceLevel,
    ContextEvidence,
    EntityCoverage,
    FieldDecision,
    FieldOutcome,
    FindingSeverity,
    MappingAmbiguity,
    MappingCandidate,
    MappingCoverage,
    MappingEvidence,
    MappingOptions,
    MappingOverride,
    MappingResult,
    MappingScore,
    NameEvidence,
    NameMatchKind,
    RejectedCandidate,
    ScoringWeights,
    TargetCollision,
    ValidationFinding,
    ValidationReport,
)
from erp_pipeline.mapping.normalization import (
    DEFAULT_ABBREVIATIONS,
    DEFAULT_NORMALIZATION,
    DEFAULT_SYNONYMS,
    NormalizationConfig,
    canonical_tokens,
    normalized_key,
    path_tokens,
    shared_tokens,
    split_tokens,
    token_similarity,
)
from erp_pipeline.mapping.scoring import (
    render_source_field_path,
    score_entity_context,
    score_name,
    score_pair,
    score_path_context,
)
from erp_pipeline.mapping.service import MappingService, generate_mapping
from erp_pipeline.mapping.validation import validate_profile

__all__ = [
    # service
    "MappingService",
    "generate_mapping",
    "MappingEngine",
    "find_source_field",
    # canonical target model
    "CanonicalTargetModel",
    "CanonicalEntity",
    "CanonicalField",
    "FieldProvenance",
    "DEFAULT_CANONICAL_MODEL",
    "DEFAULT_MODEL_ID",
    "DEFAULT_MODEL_VERSION",
    "REPOSITORY_INVOICE_FIELDS",
    # configuration
    "MappingOptions",
    "DEFAULT_OPTIONS",
    "ScoringWeights",
    "DEFAULT_WEIGHTS",
    "NormalizationConfig",
    "DEFAULT_NORMALIZATION",
    "DEFAULT_ABBREVIATIONS",
    "DEFAULT_SYNONYMS",
    "MAPPING_ENGINE_VERSION",
    # results
    "MappingResult",
    "FieldDecision",
    "FieldOutcome",
    "MappingCandidate",
    "MappingEvidence",
    "MappingScore",
    "NameEvidence",
    "NameMatchKind",
    "ContextEvidence",
    "ConfidenceLevel",
    "MappingAmbiguity",
    "TargetCollision",
    "MappingCoverage",
    "EntityCoverage",
    "MappingOverride",
    "RejectedCandidate",
    # type compatibility
    "TypeCompatibility",
    "TypeComparison",
    "compare_types",
    "compatibility_matrix",
    # normalization
    "split_tokens",
    "canonical_tokens",
    "normalized_key",
    "token_similarity",
    "shared_tokens",
    "path_tokens",
    # scoring internals, exposed for testing and reuse
    "score_pair",
    "score_name",
    "score_entity_context",
    "score_path_context",
    "render_source_field_path",
    # aliases
    "AliasIndex",
    "AliasHit",
    "build_alias_index",
    # validation and coverage
    "validate_profile",
    "compute_coverage",
    "ValidationReport",
    "ValidationFinding",
    "FindingSeverity",
    # errors
    "MappingEngineError",
    "CanonicalTargetNotFoundError",
    "SourceFieldNotFoundError",
    "InvalidMappingOverrideError",
    "MappingValidationError",
    "MappingConfigurationError",
]
