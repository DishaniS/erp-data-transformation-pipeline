"""The public entry point: records in, canonical records and a run out.

    result  = transform_record(source_record, profile, context=ctx)
    summary = TransformationService().transform_records(rows, profile, ctx)

    summary.run                  # the frozen Phase 1 TransformationRun
    summary.successful_records   # CanonicalRecord objects
    summary.rejected_records     # every rejection, with its reasons
    summary.skipped_records      # duplicates and filtered records
    summary.issues               # every DataQualityIssue raised

STREAMING (Steps 71, 72)
------------------------
``transform_records`` consumes an ITERABLE and never calls ``list()`` on it. A
generator of ten million rows is processed one at a time, and under
``FAIL_FAST`` the iterator is abandoned the moment a threshold is breached - so
the source is not drained just to discover the run had already failed.

WHAT THIS MODULE DOES NOT DO (Step 64)
--------------------------------------
It writes nothing. No database, no file, no vector store, no network. Canonical
records are returned to the caller, and where they are put is a later phase's
decision. A static test asserts the package imports no database or network
client.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Sequence

from erp_pipeline.mapping.canonical_model import CanonicalTargetModel
from erp_pipeline.schemas.enums import QualitySeverity, RunStatus
from erp_pipeline.schemas.identity import normalize_identifier
from erp_pipeline.schemas.mapping_models import MappingProfile
from erp_pipeline.schemas.run_models import DataQualityIssue, TransformationRun
from erp_pipeline.transformation.errors import (
    ComputedFieldCycleError,
    TransformationConfigurationError,
    UnsupportedOperationError,
)
from erp_pipeline.transformation.models import (
    DEFAULT_OPTIONS,
    TRANSFORMATION_ENGINE_VERSION,
    DuplicatePolicy,
    FailurePolicy,
    IssueCode,
    RecordOutcome,
    RecordTransformationResult,
    RejectedRecord,
    SkipReason,
    SkippedRecord,
    SourceRecord,
    TransformationOptions,
    TransformationRunSummary,
    deterministic_suffix,
)
from erp_pipeline.transformation.quality import make_issue
from erp_pipeline.transformation.transformer import (
    RecordTransformer,
    TransformationContext,
)
from erp_pipeline.transformation.validator import (
    MISSING,
    ReferenceResolver,
    resolve_path,
)


class TransformationService:
    """Executes mapping profiles against source records, one batch at a time."""

    def __init__(
        self,
        canonical_model: CanonicalTargetModel | None = None,
        options: TransformationOptions | None = None,
        resolver: ReferenceResolver | None = None,
    ) -> None:
        self._options = options or DEFAULT_OPTIONS
        self._transformer = RecordTransformer(
            canonical_model=canonical_model,
            options=self._options,
            resolver=resolver,
        )

    @property
    def options(self) -> TransformationOptions:
        return self._options

    @property
    def transformer(self) -> RecordTransformer:
        return self._transformer

    # ------------------------------------------------------------
    # One record
    # ------------------------------------------------------------

    def transform_record(
        self,
        source_record: SourceRecord,
        mapping_profile: MappingProfile,
        context: TransformationContext,
        run_id: str | None = None,
    ) -> RecordTransformationResult:
        """Transform exactly one record. No counters, no thresholds."""
        return self._transformer.transform(
            source_record, mapping_profile, context, run_id=run_id
        )

    # ------------------------------------------------------------
    # A batch
    # ------------------------------------------------------------

    def transform_records(
        self,
        source_records: Iterable[SourceRecord],
        mapping_profile: MappingProfile,
        context: TransformationContext,
        run_id: str | None = None,
    ) -> TransformationRunSummary:
        """Transform an iterable of records into one run.

        Input order is preserved in every output collection (Step 67): the
        n-th successful record is the n-th record that succeeded, which makes a
        result diffable against a previous run.
        """
        resolved_run_id = run_id or self._default_run_id(mapping_profile)

        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()

        successful: list[Any] = []
        rejected: list[RejectedRecord] = []
        skipped: list[SkippedRecord] = []
        issues: list[DataQualityIssue] = []
        seen_keys: dict[str, str] = {}

        records_read = 0
        stopped_early = False
        threshold_reasons: tuple[str, ...] = ()

        for source_record in source_records:
            records_read += 1

            result = self._transform_one(
                source_record, mapping_profile, context, resolved_run_id
            )

            result = self._apply_duplicate_policy(
                result, source_record, mapping_profile, seen_keys, resolved_run_id
            )

            issues.extend(result.issues)

            if result.outcome is RecordOutcome.TRANSFORMED:
                successful.append(result.record)
            elif result.outcome is RecordOutcome.REJECTED:
                assert result.rejected is not None
                rejected.append(result.rejected)
            else:
                assert result.skipped is not None
                skipped.append(result.skipped)

            threshold_reasons = self._evaluate_thresholds(
                records_read=records_read,
                records_failed=len(rejected),
                records_skipped=len(skipped),
                records_transformed=len(successful),
                issues=tuple(issues),
            )

            if threshold_reasons and self._should_stop(tuple(issues)):
                stopped_early = True
                break

        duration = round(time.monotonic() - started_monotonic, 6)
        completed_at = datetime.now(timezone.utc)

        # Recomputed after the loop so a run that never entered it (an empty
        # batch) still reports a defined threshold state.
        threshold_reasons = self._evaluate_thresholds(
            records_read=records_read,
            records_failed=len(rejected),
            records_skipped=len(skipped),
            records_transformed=len(successful),
            issues=tuple(issues),
        )

        if threshold_reasons:
            issues.append(
                make_issue(
                    IssueCode.QUALITY_THRESHOLD_EXCEEDED,
                    "the run breached a configured quality threshold: "
                    + "; ".join(threshold_reasons),
                    record_reference=None,
                    run_id=resolved_run_id,
                    expected="all configured thresholds to hold",
                    options=self._options,
                )
            )

        run = self._build_run(
            run_id=resolved_run_id,
            mapping_profile=mapping_profile,
            started_at=started_at,
            completed_at=completed_at,
            records_read=records_read,
            records_transformed=len(successful),
            records_failed=len(rejected),
            records_skipped=len(skipped),
            issues=tuple(issues),
            threshold_reasons=threshold_reasons,
            stopped_early=stopped_early,
            duration=duration,
            context=context,
        )

        return TransformationRunSummary(
            run=run,
            successful_records=tuple(successful),
            rejected_records=tuple(rejected),
            skipped_records=tuple(skipped),
            issues=tuple(issues),
            threshold_exceeded=bool(threshold_reasons),
            threshold_reasons=threshold_reasons,
            stopped_early=stopped_early,
            duration_seconds=duration,
        )

    # ------------------------------------------------------------
    # Per-record safety (Step 43)
    # ------------------------------------------------------------

    def _transform_one(
        self,
        source_record: SourceRecord,
        mapping_profile: MappingProfile,
        context: TransformationContext,
        run_id: str,
    ) -> RecordTransformationResult:
        """Transform one record without letting it take the batch down.

        ``KeyboardInterrupt`` and ``SystemExit`` derive from ``BaseException``
        and are therefore untouched by ``except Exception`` - a Ctrl-C stays a
        Ctrl-C rather than becoming a data-quality finding.

        Configuration defects are re-raised deliberately: an unimplemented
        transformation operation would produce the same failure on every
        record, and turning it into ten million identical issues would bury it.
        """
        try:
            return self._transformer.transform(
                source_record, mapping_profile, context, run_id=run_id
            )
        except (
            UnsupportedOperationError,
            TransformationConfigurationError,
            ComputedFieldCycleError,
        ):
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate per-record barrier
            issue = make_issue(
                IssueCode.INTERNAL_TRANSFORMATION_ERROR,
                "an unexpected internal error occurred while transforming this "
                f"record ({type(exc).__name__}); the record was rejected and the "
                "batch continued",
                record_reference=source_record.reference(),
                source_entity=mapping_profile.source_entity,
                run_id=run_id,
                options=self._options,
            )
            return RecordTransformationResult(
                outcome=RecordOutcome.REJECTED,
                issues=(issue,),
                rejected=RejectedRecord(
                    record_reference=source_record.reference(),
                    reasons=(IssueCode.INTERNAL_TRANSFORMATION_ERROR.value,),
                    issues=(issue,),
                    source_entity=mapping_profile.source_entity,
                    ordinal=source_record.ordinal,
                    mapping_id=mapping_profile.mapping_id,
                    source_record=(
                        source_record
                        if self._options.retain_source_on_rejection
                        else None
                    ),
                ),
            )

    # ------------------------------------------------------------
    # Duplicates (Step 29)
    # ------------------------------------------------------------

    def _apply_duplicate_policy(
        self,
        result: RecordTransformationResult,
        source_record: SourceRecord,
        mapping_profile: MappingProfile,
        seen_keys: dict[str, str],
        run_id: str,
    ) -> RecordTransformationResult:
        """Detect a repeated canonical key within this run.

        Duplicate detection is OFF unless ``duplicate_key_fields`` is declared:
        the engine never infers which fields identify a record. Nothing is
        deduplicated silently under any policy - even ``ALLOW`` and ``WARN``
        leave a trace.
        """
        key_fields = self._options.validation.duplicate_key_fields

        if not key_fields or result.outcome is not RecordOutcome.TRANSFORMED:
            return result

        assert result.record is not None
        parts: list[str] = []

        for field_name in key_fields:
            value = resolve_path(result.record.normalized_data, field_name)
            if value is MISSING or value is None:
                # An incomplete key cannot identify anything; treat the record
                # as unique rather than colliding every such record together.
                return result
            parts.append(str(value))

        key = "".join(parts)
        policy = self._options.duplicate_policy

        if key not in seen_keys:
            seen_keys[key] = source_record.reference()
            return result

        first_seen = seen_keys[key]
        issue = make_issue(
            IssueCode.DUPLICATE_RECORD,
            "this record repeats a canonical key already seen in this run "
            f"(first seen at {first_seen}); duplicate policy is "
            f"{policy.value!r}",
            severity=(
                QualitySeverity.WARNING
                if policy in (DuplicatePolicy.WARN, DuplicatePolicy.ALLOW)
                else QualitySeverity.ERROR
            ),
            record_reference=source_record.reference(),
            source_entity=mapping_profile.source_entity,
            field_name=".".join(key_fields),
            expected="a canonical key unique within the run",
            run_id=run_id,
            options=self._options,
        )

        if policy is DuplicatePolicy.ALLOW:
            return RecordTransformationResult(
                outcome=RecordOutcome.TRANSFORMED,
                record=result.record,
                issues=result.issues + (issue,),
            )

        if policy is DuplicatePolicy.WARN:
            return RecordTransformationResult(
                outcome=RecordOutcome.TRANSFORMED,
                record=result.record,
                issues=result.issues + (issue,),
            )

        if policy is DuplicatePolicy.SKIP:
            return RecordTransformationResult(
                outcome=RecordOutcome.SKIPPED,
                issues=result.issues + (issue,),
                skipped=SkippedRecord(
                    record_reference=source_record.reference(),
                    reason=SkipReason.DUPLICATE,
                    detail=f"canonical key already seen at {first_seen}",
                    source_entity=mapping_profile.source_entity,
                    ordinal=source_record.ordinal,
                ),
            )

        return RecordTransformationResult(
            outcome=RecordOutcome.REJECTED,
            issues=result.issues + (issue,),
            rejected=RejectedRecord(
                record_reference=source_record.reference(),
                reasons=(IssueCode.DUPLICATE_RECORD.value,),
                issues=result.issues + (issue,),
                source_entity=mapping_profile.source_entity,
                ordinal=source_record.ordinal,
                mapping_id=mapping_profile.mapping_id,
                source_record=(
                    source_record
                    if self._options.retain_source_on_rejection
                    else None
                ),
            ),
        )

    # ------------------------------------------------------------
    # Thresholds (Steps 36, 37, 38)
    # ------------------------------------------------------------

    def _evaluate_thresholds(
        self,
        records_read: int,
        records_failed: int,
        records_skipped: int,
        records_transformed: int,
        issues: tuple[DataQualityIssue, ...],
    ) -> tuple[str, ...]:
        """Return one safe sentence per breached threshold, or ``()``.

        Ratios divide by ``records_read``. An empty batch breaches nothing:
        0 failures out of 0 records is not a quality problem, and reporting it
        as one would make every empty run look broken (Step 70).
        """
        thresholds = self._options.thresholds
        reasons: list[str] = []

        if thresholds.max_failed_records is not None:
            if records_failed > thresholds.max_failed_records:
                reasons.append(
                    f"failed records {records_failed} exceeds the configured "
                    f"maximum of {thresholds.max_failed_records}"
                )

        if thresholds.max_error_issues is not None:
            errors = sum(
                1 for issue in issues
                if issue.severity
                in (QualitySeverity.ERROR, QualitySeverity.CRITICAL)
            )
            if errors > thresholds.max_error_issues:
                reasons.append(
                    f"error-level issues {errors} exceeds the configured "
                    f"maximum of {thresholds.max_error_issues}"
                )

        if thresholds.max_warning_issues is not None:
            warnings = sum(
                1 for issue in issues
                if issue.severity is QualitySeverity.WARNING
            )
            if warnings > thresholds.max_warning_issues:
                reasons.append(
                    f"warning-level issues {warnings} exceeds the configured "
                    f"maximum of {thresholds.max_warning_issues}"
                )

        if thresholds.stop_on_critical_issue and any(
            issue.severity is QualitySeverity.CRITICAL for issue in issues
        ):
            reasons.append(
                "a critical issue was raised and stop_on_critical_issue is set"
            )

        if records_read > 0:
            if thresholds.max_failure_ratio is not None:
                ratio = records_failed / records_read
                if ratio > thresholds.max_failure_ratio:
                    reasons.append(
                        f"failure ratio {round(ratio, 6)} exceeds the configured "
                        f"maximum of {thresholds.max_failure_ratio}"
                    )

            if thresholds.max_duplicate_ratio is not None:
                duplicates = sum(
                    1 for issue in issues
                    if issue.code == IssueCode.DUPLICATE_RECORD.value
                )
                ratio = duplicates / records_read
                if ratio > thresholds.max_duplicate_ratio:
                    reasons.append(
                        f"duplicate ratio {round(ratio, 6)} exceeds the "
                        f"configured maximum of {thresholds.max_duplicate_ratio}"
                    )

            if thresholds.minimum_success_ratio is not None:
                ratio = records_transformed / records_read
                if ratio < thresholds.minimum_success_ratio:
                    reasons.append(
                        f"success ratio {round(ratio, 6)} is below the configured "
                        f"minimum of {thresholds.minimum_success_ratio}"
                    )

        return tuple(reasons)

    def _should_stop(self, issues: tuple[DataQualityIssue, ...]) -> bool:
        """Whether a breached threshold should end the run now.

        Under ``CONTINUE`` the run keeps going and reports the breach at the
        end, because a reviewer wants to see all of a bad batch's problems at
        once rather than only the first.

        A CRITICAL issue is the exception: it means the ENGINE is in trouble,
        not the data, so with ``stop_on_critical_issue`` set the run stops
        under either policy.
        """
        if self._options.thresholds.stop_on_critical_issue and any(
            issue.severity is QualitySeverity.CRITICAL for issue in issues
        ):
            return True

        return self._options.failure_policy is FailurePolicy.FAIL_FAST

    # ------------------------------------------------------------
    # The frozen run contract (Step 39)
    # ------------------------------------------------------------

    def _build_run(
        self,
        run_id: str,
        mapping_profile: MappingProfile,
        started_at: datetime,
        completed_at: datetime,
        records_read: int,
        records_transformed: int,
        records_failed: int,
        records_skipped: int,
        issues: tuple[DataQualityIssue, ...],
        threshold_reasons: tuple[str, ...],
        stopped_early: bool,
        duration: float,
        context: TransformationContext,
    ) -> TransformationRun:
        """Populate the Phase 1 ``TransformationRun``, unmodified.

        ``error_count`` folds CRITICAL into ERROR: the contract has two
        counters, and a critical finding is certainly not a warning. The exact
        critical count is available on the summary.
        """
        warning_count = sum(
            1 for issue in issues if issue.severity is QualitySeverity.WARNING
        )
        error_count = sum(
            1 for issue in issues
            if issue.severity in (QualitySeverity.ERROR, QualitySeverity.CRITICAL)
        )

        status = self._run_status(
            records_failed=records_failed,
            threshold_reasons=threshold_reasons,
            stopped_early=stopped_early,
        )

        return TransformationRun(
            run_id=run_id,
            source_system_id=mapping_profile.source_system_id,
            status=status,
            mapping_id=mapping_profile.mapping_id,
            started_at=started_at,
            completed_at=completed_at,
            records_read=records_read,
            records_transformed=records_transformed,
            records_failed=records_failed,
            records_skipped=records_skipped,
            warning_count=warning_count,
            error_count=error_count,
            message=(
                "; ".join(threshold_reasons) if threshold_reasons else None
            ),
            metadata={
                "transformation_engine_version": TRANSFORMATION_ENGINE_VERSION,
                "transformation_config": self._options.fingerprint(),
                "validation_profile_version": self._options.validation.version,
                "mapping_id": mapping_profile.mapping_id,
                "source_schema_id": mapping_profile.source_schema_id,
                "target_entity_type": mapping_profile.target_entity_type,
                "source_type": context.source_type.value,
                "failure_policy": self._options.failure_policy.value,
                "duplicate_policy": self._options.duplicate_policy.value,
                "threshold_exceeded": bool(threshold_reasons),
                "stopped_early": stopped_early,
                "duration_seconds": duration,
            },
        )

    def _run_status(
        self,
        records_failed: int,
        threshold_reasons: tuple[str, ...],
        stopped_early: bool,
    ) -> RunStatus:
        """A run that breached a threshold is never reported as succeeded.

        Step 37 is explicit: 90 of 100 records transforming is not success when
        the configured limit was 5% failures. Saying otherwise would let a bad
        migration pass a green check.

        An empty batch SUCCEEDS: nothing was asked of it and nothing went wrong.
        """
        if threshold_reasons or stopped_early:
            return RunStatus.FAILED

        if records_failed > 0:
            return RunStatus.PARTIAL

        return RunStatus.SUCCEEDED

    def _default_run_id(self, mapping_profile: MappingProfile) -> str:
        """A deterministic default run id.

        Derived from the mapping and the configuration rather than from a
        timestamp or a random UUID, so two runs over identical inputs are
        comparable field-by-field in a determinism test. Callers that need
        per-execution uniqueness pass their own ``run_id``.
        """
        return normalize_identifier(
            "run."
            + mapping_profile.mapping_id
            + "."
            + deterministic_suffix(
                {
                    "mapping_id": mapping_profile.mapping_id,
                    "engine": TRANSFORMATION_ENGINE_VERSION,
                    "config": self._options.fingerprint(),
                }
            )
        )


def transform_record(
    source_record: SourceRecord,
    mapping_profile: MappingProfile,
    options: TransformationOptions | None = None,
    context: TransformationContext | None = None,
    canonical_model: CanonicalTargetModel | None = None,
    resolver: ReferenceResolver | None = None,
    run_id: str | None = None,
) -> RecordTransformationResult:
    """Module-level convenience for transforming a single record.

    ``context`` is required in practice: ``SourceReference`` demands the source
    technology, and this engine will not invent one. Omitting it raises rather
    than writing a false statement into a record's permanent provenance.
    """
    if context is None:
        raise TransformationConfigurationError(
            "transform_record needs a TransformationContext declaring the "
            "source technology. SourceReference requires source_type, and "
            "guessing it would put a false claim into the record's provenance."
        )

    service = TransformationService(
        canonical_model=canonical_model, options=options, resolver=resolver
    )

    return service.transform_record(
        source_record, mapping_profile, context, run_id=run_id
    )


def transform_records(
    source_records: Iterable[SourceRecord],
    mapping_profile: MappingProfile,
    context: TransformationContext,
    options: TransformationOptions | None = None,
    canonical_model: CanonicalTargetModel | None = None,
    resolver: ReferenceResolver | None = None,
    run_id: str | None = None,
) -> TransformationRunSummary:
    """Module-level convenience for transforming a batch."""
    service = TransformationService(
        canonical_model=canonical_model, options=options, resolver=resolver
    )

    return service.transform_records(
        source_records, mapping_profile, context, run_id=run_id
    )


__all__ = [
    "TransformationService",
    "transform_record",
    "transform_records",
]
