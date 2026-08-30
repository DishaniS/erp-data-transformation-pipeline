"""Schema discovery and inference for the generic ERP pipeline.

Two paradigms, one output contract.

Phase 4 - RELATIONAL DISCOVERY (PostgreSQL, MySQL, SQL Server) answers "WHAT
relational structure is DECLARED inside this database?" by reading catalog
metadata.

Phase 5 - MONGODB OBSERVED-SCHEMA INFERENCE answers "WHAT structure was
OBSERVED in a bounded sample of these documents?" An ordinary MongoDB
collection declares no schema, so its result is explicitly sample-derived and
carries ``SchemaOrigin.INFERRED`` rather than ``DISCOVERED``.

Neither answers "what does this field MEAN?" - semantic interpretation and
canonical mapping belong to later phases.

Position in the architecture::

    Relational Database            MongoDB
           |                          |
           v                          v
    Phase 3 Connector          Phase 3 Connector    erp_pipeline.connectors
           |                          |
           v                          v
    Phase 4 Discovery          Phase 5 Inference    THIS PACKAGE
           |                          |
           +------------+-------------+
                        v
                   SourceSchema        erp_pipeline.schemas (Phase 1)
                        |
                        v
             Phase 2 Schema Catalog    erp_pipeline.catalog

All four source technologies produce the SAME generic ``SourceSchema``; there
is no ``PostgresTable`` / ``MySQLTable`` / ``SQLServerTable`` /
``MongoCollectionSchema`` competing public model.

Read-only: relational discovery uses SQLAlchemy's ``Inspector`` (catalog
metadata only), optional profiling issues aggregate SELECTs exclusively, and
MongoDB inference calls only ``list_collections``, ``find`` and
``estimated_document_count``. Nothing in this package emits DDL, DML or a
document write.

This package never imports a dataset-specific module.
"""

from __future__ import annotations

from erp_pipeline.discovery.errors import (
    DiscoveryError,
    MetadataInspectionError,
    MongoInferenceError,
    ProfilingBudgetExceeded,
    ProfilingError,
    UnsupportedDiscoverySourceError,
)
from erp_pipeline.discovery.models import (
    SYSTEM_COLLECTION_PREFIXES,
    SYSTEM_NAMESPACES,
    CollectionInferenceSummary,
    ColumnProfile,
    DiscoveryOptions,
    DiscoveryResult,
    FieldObservation,
    MongoDiscoveryResult,
    MongoInferenceOptions,
    MongoInferenceSummary,
    ProfilingSummary,
    TableProfile,
    is_system_collection,
)
from erp_pipeline.discovery.mongodb import (
    MongoDBSchemaInference,
    infer_mongodb_schema,
)
from erp_pipeline.discovery.mongodb_inference import (
    ARRAY_ELEMENT_SEGMENT,
    BSON_ALIAS_TO_FIELD_TYPE,
    DocumentStructureInference,
    bson_type_alias,
    normalize_bson_alias,
    render_path,
    resolve_normalized_type,
)
from erp_pipeline.discovery.profiling import profile_schema
from erp_pipeline.discovery.relational import (
    SUPPORTED_SOURCE_TYPES,
    RelationalSchemaDiscovery,
    discover_schema,
)
from erp_pipeline.discovery.service import (
    MongoDBInferenceService,
    RelationalDiscoveryService,
    discover_relational_schema,
)
from erp_pipeline.discovery.type_mapping import (
    normalize_data_type,
    normalize_type_name,
    render_source_data_type,
)

__all__ = [
    # errors
    "DiscoveryError",
    "UnsupportedDiscoverySourceError",
    "MetadataInspectionError",
    "MongoInferenceError",
    "ProfilingError",
    "ProfilingBudgetExceeded",
    # options and results
    "DiscoveryOptions",
    "DiscoveryResult",
    "ColumnProfile",
    "TableProfile",
    "ProfilingSummary",
    "SYSTEM_NAMESPACES",
    # relational discovery (Phase 4)
    "RelationalSchemaDiscovery",
    "RelationalDiscoveryService",
    "discover_schema",
    "discover_relational_schema",
    "profile_schema",
    "SUPPORTED_SOURCE_TYPES",
    "normalize_data_type",
    "normalize_type_name",
    "render_source_data_type",
    # MongoDB observed-schema inference (Phase 5)
    "MongoInferenceOptions",
    "MongoDiscoveryResult",
    "MongoInferenceSummary",
    "CollectionInferenceSummary",
    "FieldObservation",
    "SYSTEM_COLLECTION_PREFIXES",
    "is_system_collection",
    "MongoDBSchemaInference",
    "MongoDBInferenceService",
    "infer_mongodb_schema",
    "DocumentStructureInference",
    "ARRAY_ELEMENT_SEGMENT",
    "BSON_ALIAS_TO_FIELD_TYPE",
    "bson_type_alias",
    "normalize_bson_alias",
    "resolve_normalized_type",
    "render_path",
]
