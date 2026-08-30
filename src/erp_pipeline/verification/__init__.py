"""Cross-store integrity verification.

WHY THIS PACKAGE EXISTS
-----------------------
One logical record ends up in three places: a canonical row, a tier-state row
saying where its vector lives, and a point in a vector index. Normal operation
never cross-checks them, so an interrupted job, a failed migration or a manual
deletion leaves them disagreeing - and the symptom is silent. A search returns
a hit that resolves to nothing, or a record turns out to have no retrievable
vector, and nothing reports it until a user notices.

This package asks the question directly and answers it with a verdict that is
DERIVED from its findings, never asserted.

WHAT IT CHECKS
--------------
    identity      malformed canonical ids, surrogate-key identity, duplicates
    presence      missing canonical record, representation, embedding, vector
    agreement     content hash, model id, dimension, vector id, tier metadata
    orphans       vectors with no state, state with no canonical record
    freshness     a vector produced from content that has since changed

WHAT IT IS NOT
--------------
Not a second identity system and not a second metadata system. Every check
reuses the contracts in ``schemas``, ``ai``, ``sync`` and ``storage``, and
every hash is RECOMPUTED rather than trusted - comparing a stored value against
itself would catch nothing.

Stores are reached through narrow protocols, so the whole scan is testable with
dictionaries and carries no dependency on SQLAlchemy or a vector-store client.
"""

from __future__ import annotations

from erp_pipeline.verification.cross_store import (
    CanonicalRecordSource,
    InMemoryVectorIndex,
    TierStateSource,
    VectorIndexSource,
    verify_canonical_records,
    verify_embeddings_against_state,
    verify_orphaned_vectors,
    verify_tier_state,
)
from erp_pipeline.verification.errors import (
    StoreUnavailableError,
    VerificationConfigurationError,
    VerificationError,
)
from erp_pipeline.verification.models import (
    DEFAULT_SEVERITY,
    VERIFICATION_ENGINE_VERSION,
    IntegrityCode,
    IntegrityIssue,
    IntegritySeverity,
    VerificationReport,
    build_report,
    make_issue,
)
from erp_pipeline.verification.record_integrity import (
    check_duplicate_ids,
    check_embedding,
    check_metadata_agreement,
    check_record_identity,
    check_records,
    check_representation,
    check_vector_identity,
)
from erp_pipeline.verification.service import IntegrityVerificationService

__all__ = [
    "VERIFICATION_ENGINE_VERSION",
    # contracts
    "IntegrityCode",
    "IntegritySeverity",
    "IntegrityIssue",
    "VerificationReport",
    "DEFAULT_SEVERITY",
    "make_issue",
    "build_report",
    # record-level checks
    "check_record_identity",
    "check_duplicate_ids",
    "check_representation",
    "check_embedding",
    "check_vector_identity",
    "check_metadata_agreement",
    "check_records",
    # cross-store scans
    "CanonicalRecordSource",
    "TierStateSource",
    "VectorIndexSource",
    "InMemoryVectorIndex",
    "verify_tier_state",
    "verify_orphaned_vectors",
    "verify_canonical_records",
    "verify_embeddings_against_state",
    # service
    "IntegrityVerificationService",
    # errors
    "VerificationError",
    "VerificationConfigurationError",
    "StoreUnavailableError",
]
