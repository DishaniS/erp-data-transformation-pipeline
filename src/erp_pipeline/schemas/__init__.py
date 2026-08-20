"""Public contract surface for the generic ERP pipeline.

Everything a later phase needs is importable from here:

    from erp_pipeline.schemas import CanonicalRecord, SourceType

The modules behind this package are organized by concern:

    enums            closed classification vocabularies
    identity         deterministic id and content-hash rules
    serialization    the single JSON serialization approach
    validation       shared structural validation rules
    source_models    how a source system and its schemas are described
    canonical_models the common representation every source converges on
    mapping_models   declarative source-to-canonical mapping definitions
    run_models       transformation-run and data-quality records

None of these modules performs I/O.
"""

from __future__ import annotations

from erp_pipeline.schemas.canonical_models import (
    CanonicalDocument,
    CanonicalEnvelope,
    CanonicalRecord,
    RecordProvenance,
    SourceReference,
)
from erp_pipeline.schemas.deserialization import (
    DeserializationError,
    canonical_document_from_dict,
    canonical_record_from_dict,
    data_quality_issue_from_dict,
    field_mapping_from_dict,
    mapping_profile_from_dict,
    record_provenance_from_dict,
    source_entity_from_dict,
    source_field_from_dict,
    source_reference_from_dict,
    source_relationship_from_dict,
    source_schema_from_dict,
    source_system_from_dict,
    transformation_rule_from_dict,
    transformation_run_from_dict,
)
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    MappingStatus,
    QualitySeverity,
    RecordType,
    RelationshipType,
    RunStatus,
    SchemaOrigin,
    SensitivityLevel,
    SourceType,
    TransformationOperation,
)
from erp_pipeline.schemas.identity import (
    CANONICAL_ID_PREFIX,
    DOCUMENT_ENTITY_TYPE,
    IdentityError,
    compute_content_hash,
    hash_json_payload,
    is_normalized_identifier,
    make_canonical_document_id,
    make_canonical_record_id,
    make_deterministic_uuid,
    normalize_identifier,
    parse_canonical_id,
)
from erp_pipeline.schemas.mapping_models import (
    FieldMapping,
    MappingProfile,
    TransformationRule,
)
from erp_pipeline.schemas.run_models import DataQualityIssue, TransformationRun
from erp_pipeline.schemas.serialization import (
    JsonModel,
    SerializationError,
    to_json_value,
    to_rfc3339,
    utc_now,
)
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
    SourceSystem,
)
from erp_pipeline.schemas.validation import (
    MAX_VALUE_SUMMARY_LENGTH,
    SECRET_KEY_MARKERS,
    ValidationError,
    summarize_value,
)
from erp_pipeline.version import (
    CANONICAL_MODEL_VERSION,
    MAPPING_MODEL_VERSION,
    RUN_MODEL_VERSION,
    SOURCE_MODEL_VERSION,
)

__all__ = [
    # enums
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
    # source models
    "SourceSystem",
    "SourceSchema",
    "SourceEntity",
    "SourceField",
    "SourceRelationship",
    # canonical models
    "SourceReference",
    "RecordProvenance",
    "CanonicalEnvelope",
    "CanonicalRecord",
    "CanonicalDocument",
    # mapping models
    "TransformationRule",
    "FieldMapping",
    "MappingProfile",
    # run models
    "TransformationRun",
    "DataQualityIssue",
    # deserialization
    "DeserializationError",
    "source_system_from_dict",
    "source_field_from_dict",
    "source_entity_from_dict",
    "source_relationship_from_dict",
    "source_schema_from_dict",
    "source_reference_from_dict",
    "record_provenance_from_dict",
    "canonical_record_from_dict",
    "canonical_document_from_dict",
    "transformation_rule_from_dict",
    "field_mapping_from_dict",
    "mapping_profile_from_dict",
    "transformation_run_from_dict",
    "data_quality_issue_from_dict",
    # identity
    "CANONICAL_ID_PREFIX",
    "DOCUMENT_ENTITY_TYPE",
    "IdentityError",
    "normalize_identifier",
    "is_normalized_identifier",
    "make_canonical_record_id",
    "make_canonical_document_id",
    "parse_canonical_id",
    "make_deterministic_uuid",
    "compute_content_hash",
    "hash_json_payload",
    # serialization / validation
    "JsonModel",
    "SerializationError",
    "ValidationError",
    "to_json_value",
    "to_rfc3339",
    "utc_now",
    "summarize_value",
    "SECRET_KEY_MARKERS",
    "MAX_VALUE_SUMMARY_LENGTH",
    # versions
    "CANONICAL_MODEL_VERSION",
    "SOURCE_MODEL_VERSION",
    "MAPPING_MODEL_VERSION",
    "RUN_MODEL_VERSION",
]
