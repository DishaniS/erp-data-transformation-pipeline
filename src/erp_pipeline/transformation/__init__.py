"""Generic transformation and validation for the ERP pipeline (Phase 9).

Phase 8 decided WHAT MAPS TO WHAT. Phase 9 executes HOW A SOURCE RECORD BECOMES
A VALID CANONICAL RECORD::

    Source Record  +  MappingProfile
              |
              v
       field extraction          missing != null
              v
      null / default handling    defaults substitute absence, never failure
              v
     TransformationRule execution   declared, data-only, no eval
              v
        type conversion          Decimal for money, never float
              v
         normalization           opt-in only; identifiers are not mutated
              v
      canonical assignment       nested targets, conflicts reported
              v
          validation             required / type / constraints / references
              |
      +-------+-------+
      |               |
    VALID          INVALID
      |               |
      v               v
 CanonicalRecord   DataQualityIssue
                       |
                       v
              reject / skip / threshold action

THE GOVERNING PRINCIPLE
-----------------------
The pipeline never optimizes for "make every record succeed". A failed
transformation is an acceptable, well-reported outcome. A silently corrupted
ERP record is not - so ``"hello"`` never becomes ``0``, ``"25.9"`` never
becomes ``25``, and ``"approved"`` never becomes ``True``.

BOUNDARIES
----------
This package writes nothing anywhere: no database, no file, no vector store, no
network, no LLM, no embeddings. It transforms in memory and hands the results
back. It never imports ``bpi2020``. Static tests assert all of it.
"""

from __future__ import annotations

from erp_pipeline.transformation.errors import (
    ComputedFieldCycleError,
    QualityThresholdExceeded,
    TransformationConfigurationError,
    TransformationError,
    UnsupportedOperationError,
)
from erp_pipeline.transformation.models import (
    DEFAULT_EXECUTABLE_STATUSES,
    DEFAULT_OPTIONS,
    TRANSFORMATION_ENGINE_VERSION,
    BooleanPolicy,
    CaseNormalization,
    ComputedField,
    ComputedOperation,
    DatePolicy,
    DuplicatePolicy,
    ExtractionOutcome,
    FailurePolicy,
    FieldConstraint,
    IssueCode,
    NormalizationPolicy,
    NullPolicy,
    NumberPolicy,
    QualityThresholds,
    RecordOutcome,
    RecordTransformationResult,
    RejectedRecord,
    SkipReason,
    SkippedRecord,
    SourceRecord,
    StringPolicy,
    TransformationOptions,
    TransformationRunSummary,
    UnknownTypePolicy,
    ValidationProfile,
)
from erp_pipeline.transformation.normalizer import normalize_value
from erp_pipeline.transformation.quality import (
    DEFAULT_SEVERITIES,
    count_by_severity,
    default_severity,
    has_blocking,
    make_issue,
)
from erp_pipeline.transformation.rules import (
    REDACTION_MASK,
    RuleContext,
    RuleResult,
    apply_rule,
    apply_rules,
    supported_operations,
)
from erp_pipeline.transformation.service import (
    TransformationService,
    transform_record,
    transform_records,
)
from erp_pipeline.transformation.transformer import (
    RecordTransformer,
    TransformationContext,
    assign_value,
    extract_value,
)
from erp_pipeline.transformation.type_converter import (
    ConversionResult,
    convert,
    matches_type,
)
from erp_pipeline.transformation.validator import (
    InMemoryReferenceResolver,
    KnownReferenceSet,
    ReferenceResolver,
    resolve_path,
    validate_record,
)

__all__ = [
    # errors
    "TransformationError",
    "TransformationConfigurationError",
    "UnsupportedOperationError",
    "ComputedFieldCycleError",
    "QualityThresholdExceeded",
    # configuration
    "TRANSFORMATION_ENGINE_VERSION",
    "SourceRecord",
    "ExtractionOutcome",
    "NullPolicy",
    "BooleanPolicy",
    "NumberPolicy",
    "DatePolicy",
    "StringPolicy",
    "UnknownTypePolicy",
    "CaseNormalization",
    "NormalizationPolicy",
    "ComputedOperation",
    "ComputedField",
    "FieldConstraint",
    "ValidationProfile",
    "FailurePolicy",
    "DuplicatePolicy",
    "QualityThresholds",
    "DEFAULT_EXECUTABLE_STATUSES",
    "TransformationOptions",
    "DEFAULT_OPTIONS",
    # results
    "IssueCode",
    "RecordOutcome",
    "SkipReason",
    "RejectedRecord",
    "SkippedRecord",
    "RecordTransformationResult",
    "TransformationRunSummary",
    # engine
    "TransformationContext",
    "RecordTransformer",
    "TransformationService",
    "transform_record",
    "transform_records",
    "extract_value",
    "assign_value",
    # conversion / normalization / rules
    "ConversionResult",
    "convert",
    "matches_type",
    "normalize_value",
    "RuleContext",
    "RuleResult",
    "apply_rule",
    "apply_rules",
    "supported_operations",
    "REDACTION_MASK",
    # validation
    "ReferenceResolver",
    "KnownReferenceSet",
    "InMemoryReferenceResolver",
    "resolve_path",
    "validate_record",
    # quality
    "make_issue",
    "default_severity",
    "DEFAULT_SEVERITIES",
    "count_by_severity",
    "has_blocking",
]
