"""The verification layer's public entry point.

Composes the individual scans into one verdict, so a caller runs one method and
gets one report rather than stitching four together and deciding for itself
what "passed" means.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from erp_pipeline.verification.cross_store import (
    CanonicalRecordSource,
    TierStateSource,
    VectorIndexSource,
    verify_canonical_records,
    verify_embeddings_against_state,
    verify_orphaned_vectors,
    verify_tier_state,
)
from erp_pipeline.verification.models import VerificationReport, build_report
from erp_pipeline.verification.record_integrity import (
    check_embedding,
    check_representation,
)


class IntegrityVerificationService:
    """Runs cross-store integrity verification over the configured stores.

    Every store is optional. A deployment with no vector index still gets its
    canonical identity checked, and the report says which scans ran rather than
    silently reporting a pass for checks that never happened.
    """

    def __init__(
        self,
        canonical_records: CanonicalRecordSource | None = None,
        tier_state: TierStateSource | None = None,
        vector_index: VectorIndexSource | None = None,
        expected_model_id: str | None = None,
        expected_dimension: int | None = None,
    ) -> None:
        self._records = canonical_records
        self._state = tier_state
        self._index = vector_index
        self._model_id = expected_model_id
        self._dimension = expected_dimension

    # -- individual scans ----------------------------------------------

    def verify_identity(self, limit: int = 1000) -> VerificationReport:
        if self._records is None:
            return build_report((), checks_run=0)

        return verify_canonical_records(self._records, limit=limit)

    def verify_storage(self) -> VerificationReport:
        if self._state is None:
            return build_report((), checks_run=0)

        return verify_tier_state(
            self._state,
            vector_index=self._index,
            canonical_records=self._records,
            expected_model_id=self._model_id,
            expected_dimension=self._dimension,
        )

    def verify_orphans(self, vector_ids: Iterable[str]) -> VerificationReport:
        if self._state is None:
            return build_report((), checks_run=0)

        return verify_orphaned_vectors(vector_ids, self._state)

    def verify_embeddings(
        self, embeddings: Iterable[Any]
    ) -> VerificationReport:
        if self._state is None:
            return build_report((), checks_run=0)

        return verify_embeddings_against_state(embeddings, self._state)

    def verify_representations(
        self,
        representations: Sequence[Any],
        embeddings: Sequence[Any] | None = None,
    ) -> VerificationReport:
        """Check representations, and their embeddings when supplied.

        Kept separate from the store scans because it needs objects in memory
        rather than a live store, which is what a pipeline stage has to hand
        immediately after building them.
        """
        issues = []
        checks = 0
        by_id = {
            getattr(item, "representation_id", None): item
            for item in (embeddings or ())
        }

        for representation in representations:
            checks += 1
            record = None

            if self._records is not None:
                declared = tuple(
                    getattr(representation, "source_record_ids", ()) or ()
                )

                if declared:
                    record = self._records.get(declared[0])

            issues.extend(check_representation(representation, record))

            embedding = by_id.get(
                getattr(representation, "representation_id", None)
            )

            if embedding is not None:
                checks += 1
                issues.extend(
                    check_embedding(
                        embedding,
                        representation=representation,
                        expected_dimension=self._dimension,
                        expected_model_id=self._model_id,
                    )
                )

        return build_report(
            issues,
            checks_run=checks,
            subjects_examined=len(representations),
            counts={"representations_examined": len(representations)},
        )

    # -- everything at once --------------------------------------------

    def verify_all(
        self,
        limit: int = 1000,
        vector_ids: Iterable[str] | None = None,
    ) -> VerificationReport:
        """Run every scan the configured stores support, as one verdict."""
        report = self.verify_identity(limit=limit)
        report = report.merged(self.verify_storage())

        if vector_ids is not None:
            report = report.merged(self.verify_orphans(vector_ids))

        return report


__all__ = ["IntegrityVerificationService"]
