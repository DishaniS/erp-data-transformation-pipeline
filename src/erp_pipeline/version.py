"""Model and schema version constants for the generic ERP pipeline.

Every versioned model reads its version from here. No model module defines a
version string literal of its own, so bumping a contract is a one-line change
in one file.

What a version change means
---------------------------
These versions describe the *shape of the contract*, not the version of the
data or of the software release. They follow semantic-versioning intent:

PATCH (1.0.0 -> 1.0.1)
    Documentation, validation-message, or non-structural clarification.
    A consumer written against the previous version keeps working unchanged.

MINOR (1.0.0 -> 1.1.0)
    A backwards-compatible addition, typically a new optional field or a new
    enum member. Old records remain valid and readable. A consumer that does
    not know the new field must ignore it rather than fail.

MAJOR (1.0.0 -> 2.0.0)
    A breaking change: a field removed or renamed, a field made required, a
    field's type or meaning changed, or an identity rule changed. Stored
    records carrying the old version cannot be assumed readable, and a
    migration path must be provided by the phase that introduces the break.

Records persist the version that produced them (``schema_version``), so a
future reader can branch on it. Phase 1 deliberately does NOT implement a
migration framework - it only makes the version explicit and recorded.

Identity rules are treated as part of the MAJOR contract: changing how a
canonical record ID is derived would silently re-identify every stored record
and every derived vector, so it can never be a MINOR change.
"""

from __future__ import annotations

# Canonical ERP representation: CanonicalRecord, CanonicalDocument and the
# provenance models they embed.
CANONICAL_MODEL_VERSION = "1.0.0"

# Source-side description: SourceSystem, SourceSchema, SourceEntity,
# SourceField, SourceRelationship.
SOURCE_MODEL_VERSION = "1.0.0"

# Mapping contracts: MappingProfile, FieldMapping, TransformationRule.
MAPPING_MODEL_VERSION = "1.0.0"

# Execution and data-quality contracts: TransformationRun, DataQualityIssue.
RUN_MODEL_VERSION = "1.0.0"

__all__ = [
    "CANONICAL_MODEL_VERSION",
    "SOURCE_MODEL_VERSION",
    "MAPPING_MODEL_VERSION",
    "RUN_MODEL_VERSION",
]
