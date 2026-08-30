"""Verify that the canonical store, the tier state and the vector index agree.

THE PROBLEM THIS SOLVES
-----------------------
The pipeline writes the same logical record into three places: a canonical row
in PostgreSQL, a tier-state row recording where its vector lives, and a point
in a vector index. Nothing in normal operation cross-checks them. An
interrupted job, a failed migration or a manual deletion leaves them
disagreeing, and the symptom is silent: retrieval returns a hit that resolves
to nothing, or a record that has no retrievable vector.

WHY PROTOCOLS RATHER THAN CONCRETE STORES
-----------------------------------------
Each store is reached through a narrow ``Protocol`` describing only what the
scan needs. That keeps this module free of SQLAlchemy and of the Qdrant client,
makes the whole scan testable with dictionaries, and means a deployment can
verify a store this package has never heard of.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol, Sequence

from erp_pipeline.verification.models import (
    IntegrityCode,
    IntegrityIssue,
    VerificationReport,
    build_report,
    make_issue,
)
from erp_pipeline.verification.record_integrity import (
    check_metadata_agreement,
    check_record_identity,
    check_vector_identity,
)


class CanonicalRecordSource(Protocol):
    """The canonical store, reduced to what a verification scan needs."""

    def record_ids(self, limit: int = 100) -> Sequence[str]:
        ...  # pragma: no cover - protocol declaration

    def get(self, canonical_id: str) -> Any:
        ...  # pragma: no cover - protocol declaration


class TierStateSource(Protocol):
    """The vector tier-state store."""

    def list_all(self, *args: Any, **kwargs: Any) -> Sequence[Any]:
        ...  # pragma: no cover - protocol declaration

    def load(self, representation_id: str) -> Any:
        ...  # pragma: no cover - protocol declaration


class VectorIndexSource(Protocol):
    """A vector index, reduced to existence and enumeration."""

    def exists(self, representation_id: str) -> bool:
        ...  # pragma: no cover - protocol declaration

    def count(self) -> int:
        ...  # pragma: no cover - protocol declaration


class InMemoryVectorIndex:
    """A ``VectorIndexSource`` backed by a set of representation ids.

    Documents exactly what a real index adapter has to provide, and lets the
    whole cross-store scan be proved without a running vector database.
    """

    def __init__(self, representation_ids: Iterable[str] = ()) -> None:
        self._ids = set(representation_ids)

    def add(self, representation_id: str) -> None:
        self._ids.add(representation_id)

    def remove(self, representation_id: str) -> None:
        self._ids.discard(representation_id)

    def exists(self, representation_id: str) -> bool:
        return representation_id in self._ids

    def count(self) -> int:
        return len(self._ids)

    def all_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._ids))


def verify_tier_state(
    state: TierStateSource,
    vector_index: VectorIndexSource | None = None,
    canonical_records: CanonicalRecordSource | None = None,
    expected_model_id: str | None = None,
    expected_dimension: int | None = None,
) -> VerificationReport:
    """Scan every tier-state entry and check what it points at.

    Runs from the tier state outward because that store is the authority on
    where a vector is supposed to be. A record it lists that does not exist in
    the index is the failure a retrieval consumer would actually hit.
    """
    issues: list[IntegrityIssue] = []
    checks = 0
    entries = list(state.list_all())

    missing_vectors = 0
    missing_records = 0

    for metadata in entries:
        representation_id = getattr(metadata, "representation_id", None)

        if not representation_id:
            issues.append(
                make_issue(
                    IntegrityCode.MALFORMED_RECORD_ID,
                    "<empty>",
                    "a tier-state entry carries no representation id",
                )
            )
            continue

        # -- the vector id must be derivable from the representation id --
        checks += 1
        issues.extend(
            check_vector_identity(
                representation_id, getattr(metadata, "vector_id", None)
            )
        )

        # -- model and dimension must match the configured model --
        if expected_model_id is not None:
            checks += 1
            stored_model = getattr(metadata, "model_id", None)

            if stored_model is not None and stored_model != expected_model_id:
                issues.append(
                    make_issue(
                        IntegrityCode.MODEL_ID_MISMATCH,
                        representation_id,
                        f"tier state records model {stored_model!r} but the "
                        f"configured model is {expected_model_id!r}",
                        stored=stored_model,
                        expected=expected_model_id,
                    )
                )

        if expected_dimension is not None:
            checks += 1
            stored_dimension = getattr(metadata, "dimension", None)

            if stored_dimension is not None and stored_dimension != expected_dimension:
                issues.append(
                    make_issue(
                        IntegrityCode.DIMENSION_MISMATCH,
                        representation_id,
                        f"tier state records dimension {stored_dimension} but "
                        f"the configured model produces {expected_dimension}",
                        stored=stored_dimension,
                        expected=expected_dimension,
                    )
                )

        # -- the vector must actually be in the index --
        if vector_index is not None:
            checks += 1

            if not vector_index.exists(representation_id):
                missing_vectors += 1
                issues.append(
                    make_issue(
                        IntegrityCode.VECTOR_MISSING,
                        representation_id,
                        "tier state says this record has a stored vector, but "
                        "the index has no point for it",
                        tier=getattr(
                            getattr(metadata, "current_tier", None), "value", None
                        ),
                    )
                )

        # -- the canonical record it derives from must still exist --
        if canonical_records is not None:
            canonical_id = _canonical_id_for(metadata)

            if canonical_id:
                checks += 1

                if canonical_records.get(canonical_id) is None:
                    missing_records += 1
                    issues.append(
                        make_issue(
                            IntegrityCode.ORPHANED_TIER_STATE,
                            representation_id,
                            f"derives from canonical record {canonical_id!r}, "
                            "which no longer exists",
                            canonical_record_id=canonical_id,
                        )
                    )

    counts = {
        "tier_state_entries": len(entries),
        "missing_vectors": missing_vectors,
        "orphaned_tier_state": missing_records,
    }

    if vector_index is not None:
        counts["vector_index_points"] = vector_index.count()

    return build_report(
        issues, checks_run=checks, subjects_examined=len(entries), counts=counts
    )


def _canonical_id_for(metadata: Any) -> str | None:
    """Recover the canonical record id a tier-state entry derives from.

    Tier state does not carry the canonical id as a column today, so a caller
    may supply it through the metadata mapping. Returning ``None`` when it is
    absent is deliberate: the scan skips a check it cannot perform rather than
    guessing, because guessing would produce a false ``ORPHANED_TIER_STATE``.
    """
    extra = getattr(metadata, "metadata", None)

    if isinstance(extra, Mapping):
        value = extra.get("canonical_record_id")

        if value:
            return str(value)

    value = getattr(metadata, "canonical_record_id", None)

    return str(value) if value else None


def verify_orphaned_vectors(
    vector_ids: Iterable[str],
    state: TierStateSource,
) -> VerificationReport:
    """Find points in the index that no tier-state entry accounts for.

    The mirror of ``verify_tier_state``: that scan finds state without a
    vector, this one finds a vector without state. An orphan is not merely
    untidy - it can be returned by a search and then resolve to nothing.
    """
    known = {
        getattr(metadata, "representation_id", None)
        for metadata in state.list_all()
    }
    known.discard(None)

    orphans = [
        representation_id
        for representation_id in sorted(set(vector_ids))
        if representation_id not in known
    ]

    issues = [
        make_issue(
            IntegrityCode.ORPHANED_VECTOR,
            representation_id,
            "the index holds a point for this representation, but no tier "
            "state accounts for it; a search could return a hit that resolves "
            "to nothing",
        )
        for representation_id in orphans
    ]

    return build_report(
        issues,
        checks_run=len(orphans) or 1,
        subjects_examined=len(known),
        counts={"orphaned_vectors": len(orphans), "known_representations": len(known)},
    )


def verify_canonical_records(
    canonical_records: CanonicalRecordSource,
    limit: int = 1000,
) -> VerificationReport:
    """Check identity grammar and uniqueness across the canonical store."""
    record_ids = list(canonical_records.record_ids(limit=limit))
    issues: list[IntegrityIssue] = []

    for record_id in record_ids:
        issues.extend(check_record_identity(record_id))

    from erp_pipeline.verification.record_integrity import check_duplicate_ids

    issues.extend(check_duplicate_ids(record_ids))

    return build_report(
        issues,
        checks_run=len(record_ids),
        subjects_examined=len(record_ids),
        counts={"canonical_records_examined": len(record_ids)},
    )


def verify_embeddings_against_state(
    embeddings: Iterable[Any],
    state: TierStateSource,
) -> VerificationReport:
    """Check that stored tier state agrees with the embeddings it describes."""
    issues: list[IntegrityIssue] = []
    checks = 0
    examined = 0

    for embedding in embeddings:
        representation_id = getattr(embedding, "representation_id", None)

        if not representation_id:
            continue

        examined += 1
        metadata = state.load(representation_id)

        if metadata is None:
            issues.append(
                make_issue(
                    IntegrityCode.VECTOR_MISSING,
                    representation_id,
                    "an embedding exists but no tier state records where its "
                    "vector was stored",
                )
            )
            continue

        checks += 1
        issues.extend(check_metadata_agreement(metadata, embedding))

    return build_report(
        issues,
        checks_run=checks,
        subjects_examined=examined,
        counts={"embeddings_examined": examined},
    )


__all__ = [
    "CanonicalRecordSource",
    "TierStateSource",
    "VectorIndexSource",
    "InMemoryVectorIndex",
    "verify_tier_state",
    "verify_orphaned_vectors",
    "verify_canonical_records",
    "verify_embeddings_against_state",
]
