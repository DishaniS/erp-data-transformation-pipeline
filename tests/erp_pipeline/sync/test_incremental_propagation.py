"""The mandatory incremental proofs: one change, all the way down, and no more.

Proofs A, B, E and Steps 14-31, 37-40, 64-66, 77. The recurring assertion is a
COUNTER: "only the affected representation was rebuilt" is a claim until
something has counted the rebuilds.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from erp_pipeline.schemas.canonical_models import CanonicalRecord
from erp_pipeline.sync import (
    ChangeOperation,
    FailingStage,
    FailurePolicy,
    PropagationPipeline,
    SyncOptions,
    SyncRunStatus,
    SyncStage,
    SyncService,
    Watermark,
    vector_id_for,
)

from tests.erp_pipeline.sync.conftest import (
    BASE_TIME,
    Harness,
    invoice_row,
)


# ============================================================
# PROOF A - one new record (Step 27)
# ============================================================

def test_proof_a_one_new_record_travels_alone(harness):
    """100 synchronized records, one added. Exactly one of everything."""
    harness.source.add(invoice_row(101, offset_seconds=500))

    summary = harness.run()

    assert harness.source.total_rows == 101
    assert summary.changes_read == 1
    assert summary.changes_processed == 1
    assert summary.canonical_upserts == 1
    assert harness.builder.rebuild_calls == 1
    assert summary.embeddings_generated == 1
    assert summary.vectors_upserted == 1


def test_proof_a_does_not_rebuild_everything(harness):
    """Step 26: no full rebuild, proved by instrumentation."""
    harness.source.add(invoice_row(101, offset_seconds=500))

    harness.run()

    assert harness.builder.rebuild_calls == 1
    assert harness.embedder.calls == 1
    assert harness.canonical.upsert_calls == 1
    assert len(harness.canonical) == 101


def test_proof_a_only_the_affected_representation_is_touched(harness):
    harness.source.add(invoice_row(101, offset_seconds=500))

    summary = harness.run()

    assert summary.representations_changed == 1
    assert harness.embedder.embedded_ids == list(harness.builder.rebuilt_keys)


def test_proof_a_counters_balance(harness):
    harness.source.add(invoice_row(101, offset_seconds=500))

    summary = harness.run()

    assert summary.counters_balance
    assert summary.embedding_counters_balance
    assert summary.status is SyncRunStatus.SUCCEEDED


def test_proof_a_advances_the_checkpoint(harness):
    before = harness.state.watermark
    harness.source.add(invoice_row(101, offset_seconds=500))

    summary = harness.run()

    assert summary.checkpoint_advanced
    assert summary.watermark_after.is_after(before)


# ============================================================
# UPDATE proof (Step 28)
# ============================================================

def test_an_update_reuses_the_same_canonical_identity(harness):
    before_count = len(harness.canonical)
    before_ids = set(harness.canonical.record_ids)

    harness.source.update("INV-050", amount="999.99", updated_at=BASE_TIME + timedelta(seconds=900))
    harness.run()

    assert len(harness.canonical) == before_count
    assert set(harness.canonical.record_ids) == before_ids


def test_an_update_that_changes_content_re_embeds_once(harness):
    harness.source.update(
        "INV-050", amount="999.99", updated_at=BASE_TIME + timedelta(seconds=900)
    )

    summary = harness.run()

    assert summary.changes_read == 1
    assert summary.embeddings_generated == 1
    assert summary.vectors_upserted == 1
    assert summary.representations_changed == 1


def test_an_update_reuses_the_same_vector_identity(harness):
    before = len(harness.vectors)
    before_ids = set(harness.vectors.vector_ids)

    harness.source.update(
        "INV-050", amount="999.99", updated_at=BASE_TIME + timedelta(seconds=900)
    )
    harness.run()

    assert len(harness.vectors) == before
    assert set(harness.vectors.vector_ids) == before_ids


def test_the_stored_vector_content_hash_moves_with_the_content(harness):
    key = harness.canonical.record_ids[0]
    vector_id = vector_id_for(key)
    before = harness.vectors.get(vector_id)["content_hash"]

    harness.source.update(
        "INV-001", amount="777.77", updated_at=BASE_TIME + timedelta(seconds=900)
    )
    harness.run()

    assert harness.vectors.get(vector_id)["content_hash"] != before


# ============================================================
# PROOF B - no content change (Step 29)
# ============================================================

def test_proof_b_a_metadata_only_change_does_not_re_embed(harness):
    """Only the watermark moved; the AI-ready content is byte-identical."""
    harness.source.update("INV-050", updated_at=BASE_TIME + timedelta(seconds=900))

    summary = harness.run()

    assert summary.changes_read == 1
    assert summary.changes_processed == 1
    assert summary.embeddings_generated == 0
    assert summary.vectors_upserted == 0


def test_proof_b_reports_the_representation_as_unchanged(harness):
    harness.source.update("INV-050", updated_at=BASE_TIME + timedelta(seconds=900))

    summary = harness.run()

    assert summary.representations_unchanged == 1
    assert summary.embeddings_skipped == 1


def test_proof_b_still_rebuilds_to_find_out(harness):
    """The rebuild is what produces the hash to compare - it is not skipped."""
    harness.source.update("INV-050", updated_at=BASE_TIME + timedelta(seconds=900))

    harness.run()

    assert harness.builder.rebuild_calls == 1
    assert harness.embedder.calls == 0


def test_forcing_re_embedding_is_possible_but_not_the_default(harness):
    harness.source.update("INV-050", updated_at=BASE_TIME + timedelta(seconds=900))

    summary = harness.run(SyncOptions(batch_size=500, force_reembed=True))

    assert summary.embeddings_generated == 1


def test_a_content_hash_is_stable_across_runs(harness):
    key = harness.canonical.record_ids[0]
    first = harness.builder.rebuild(key).resolved_hash()
    second = harness.builder.rebuild(key).resolved_hash()

    assert first == second


def test_a_content_hash_excludes_operational_noise():
    from erp_pipeline.sync import AIRepresentation

    quiet = AIRepresentation(
        representation_id="r1", entity_type="invoice", text_for_ai="x",
        content={"amount": "1.00"},
    )
    noisy = AIRepresentation(
        representation_id="r1", entity_type="invoice", text_for_ai="x",
        content={"amount": "1.00", "updated_at": "2026-08-14", "run_id": "abc"},
    )

    assert quiet.resolved_hash() == noisy.resolved_hash()


# ============================================================
# DELETE proof (Steps 17, 30)
# ============================================================

def _delete_harness() -> Harness:
    rows = [invoice_row(i, deleted=False) for i in range(1, 11)]
    harness = Harness(rows=rows, deleted_flag_field="deleted")
    harness.catch_up()
    harness.reset_counters()
    return harness


def test_a_soft_deleted_row_becomes_a_delete_change():
    harness = _delete_harness()
    harness.source.update(
        "INV-005", deleted=True, updated_at=BASE_TIME + timedelta(seconds=900)
    )

    summary = harness.run()

    assert summary.changes_read == 1
    assert summary.results[0].change.operation is ChangeOperation.DELETE


def test_a_delete_removes_the_canonical_record():
    harness = _delete_harness()
    before = len(harness.canonical)

    harness.source.update(
        "INV-005", deleted=True, updated_at=BASE_TIME + timedelta(seconds=900)
    )
    summary = harness.run()

    assert summary.canonical_deletes == 1
    assert len(harness.canonical) == before - 1


def test_a_delete_removes_the_stale_vector():
    """Step 17: no stale AI/vector record is left behind."""
    harness = _delete_harness()
    before = len(harness.vectors)

    harness.source.update(
        "INV-005", deleted=True, updated_at=BASE_TIME + timedelta(seconds=900)
    )
    summary = harness.run()

    assert summary.vectors_deleted == 1
    assert len(harness.vectors) == before - 1


def test_delete_processing_can_be_switched_off():
    harness = _delete_harness()
    harness.source.update(
        "INV-005", deleted=True, updated_at=BASE_TIME + timedelta(seconds=900)
    )

    summary = harness.run(SyncOptions(batch_size=500, process_deletes=False))

    assert summary.changes_read == 0


def test_a_source_without_a_delete_marker_cannot_report_deletions(harness):
    """An honest capability limit, not a silent gap."""
    harness.source.remove("INV-050")

    summary = harness.run()

    assert summary.changes_read == 0
    assert harness.canonical.get("erp:erp_pg:invoice:inv-050") is not None


# ============================================================
# PROOF E - retry safety (Steps 31, 64, 65)
# ============================================================

def test_a_vector_failure_does_not_advance_the_checkpoint(harness):
    """Step 65."""
    before = harness.state.watermark
    harness.pipeline.vector_store = FailingStage(harness.vectors, fail_times=1)

    harness.source.add(invoice_row(101, offset_seconds=500))
    summary = harness.run()

    assert summary.changes_failed == 1
    assert not summary.checkpoint_advanced
    assert harness.state.watermark.tie_breaker == before.tie_breaker


def test_the_failing_change_is_retried_on_the_next_run(harness):
    harness.pipeline.vector_store = FailingStage(harness.vectors, fail_times=1)
    harness.source.add(invoice_row(101, offset_seconds=500))

    first = harness.run()
    second = harness.run()

    assert first.changes_failed == 1
    assert second.changes_read == 1
    assert second.changes_processed == 1


def test_a_retry_creates_no_duplicate_canonical_or_vector(harness):
    harness.pipeline.vector_store = FailingStage(harness.vectors, fail_times=1)
    harness.source.add(invoice_row(101, offset_seconds=500))

    harness.run()
    harness.run()

    assert len(harness.canonical) == 101
    assert len(harness.vectors) == 101


def test_an_embedding_failure_records_the_stage(harness):
    """Step 64."""
    harness.pipeline.embedder = FailingStage(harness.embedder, fail_times=1)
    harness.source.add(invoice_row(101, offset_seconds=500))

    summary = harness.run()

    assert summary.quarantined
    assert summary.quarantined[0].stage is SyncStage.REPRESENTATION


def test_an_embedding_failure_leaves_the_ledger_untouched(harness):
    """So the retry genuinely redoes the work rather than believing it done."""
    harness.pipeline.embedder = FailingStage(harness.embedder, fail_times=1)
    harness.source.add(invoice_row(101, offset_seconds=500))

    harness.run()
    second = harness.run()

    assert second.embeddings_generated == 1


def test_a_checkpoint_stops_before_the_first_failure(harness):
    """Step 31: A succeeds, B fails - the checkpoint must not pass B."""
    harness.source.add(invoice_row(101, offset_seconds=500))
    harness.source.add(invoice_row(102, offset_seconds=501))
    harness.pipeline.vector_store = FailingStage(harness.vectors, fail_times=99)

    summary = harness.run()

    assert summary.changes_failed >= 1
    assert not summary.checkpoint_advanced


def test_block_is_the_default_failure_policy():
    assert SyncOptions().failure_policy is FailurePolicy.BLOCK


def test_block_stops_the_run_at_the_first_failure(harness):
    harness.source.add(invoice_row(101, offset_seconds=500))
    harness.source.add(invoice_row(102, offset_seconds=501))
    harness.pipeline.vector_store = FailingStage(harness.vectors, fail_times=99)

    summary = harness.run(
        SyncOptions(batch_size=500, failure_policy=FailurePolicy.BLOCK)
    )

    assert summary.changes_read == 1


def test_quarantine_collects_every_failure_without_losing_position(harness):
    harness.source.add(invoice_row(101, offset_seconds=500))
    harness.source.add(invoice_row(102, offset_seconds=501))
    harness.pipeline.vector_store = FailingStage(harness.vectors, fail_times=99)

    summary = harness.run(
        SyncOptions(batch_size=500, failure_policy=FailurePolicy.QUARANTINE)
    )

    assert summary.changes_read == 2
    assert summary.changes_failed == 2
    assert not summary.checkpoint_advanced


def test_every_quarantined_change_states_a_reason(harness):
    harness.source.add(invoice_row(101, offset_seconds=500))
    harness.pipeline.vector_store = FailingStage(harness.vectors, fail_times=99)

    summary = harness.run(
        SyncOptions(batch_size=500, failure_policy=FailurePolicy.QUARANTINE)
    )

    for item in summary.quarantined:
        assert item.reasons


def test_a_quarantined_change_cannot_be_built_without_a_reason():
    from erp_pipeline.sync import QuarantinedChange, SyncError

    with pytest.raises(SyncError):
        QuarantinedChange(
            reference="e:1",
            record_key="1",
            source_entity="e",
            stage=SyncStage.TRANSFORM,
            reasons=(),
            watermark=Watermark(),
        )


# ============================================================
# Phase 9 failure inside a sync (Step 39)
# ============================================================

def test_a_transformation_failure_quarantines_the_change(harness):
    harness.source.add(
        invoice_row(101, amount="hello", offset_seconds=500)
    )

    summary = harness.run(
        SyncOptions(batch_size=500, failure_policy=FailurePolicy.QUARANTINE)
    )

    assert summary.changes_failed == 1
    assert summary.quarantined[0].stage is SyncStage.TRANSFORM


def test_a_transformation_failure_carries_the_phase_9_issue_codes(harness):
    harness.source.add(invoice_row(101, amount="hello", offset_seconds=500))

    summary = harness.run(
        SyncOptions(batch_size=500, failure_policy=FailurePolicy.QUARANTINE)
    )

    assert "TYPE_CONVERSION_FAILED" in summary.quarantined[0].issue_codes


def test_a_transformation_failure_writes_no_canonical_record(harness):
    before = len(harness.canonical)
    harness.source.add(invoice_row(101, amount="hello", offset_seconds=500))

    harness.run(SyncOptions(batch_size=500, failure_policy=FailurePolicy.QUARANTINE))

    assert len(harness.canonical) == before


# ============================================================
# Phase 9 reuse (Step 14)
# ============================================================

def test_the_coordinator_uses_the_phase_9_transformation_service(harness):
    from erp_pipeline.transformation import TransformationService

    assert isinstance(
        harness.service.coordinator.transformation_service, TransformationService
    )


def test_the_sync_package_contains_no_transformation_logic():
    """Step 14: no duplicate conversion or validation implementation."""
    from pathlib import Path

    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/erp_pipeline/sync").rglob("*.py")
    )

    for marker in ("def convert(", "def _to_decimal", "def validate_record"):
        assert marker not in text


def test_the_emitted_record_is_the_frozen_canonical_record(harness):
    harness.source.add(invoice_row(101, offset_seconds=500))
    harness.run()

    record = harness.canonical.get("erp:erp_pg:invoice:inv-101")

    assert type(record) is CanonicalRecord
    assert record.normalized_data["amount"] == Decimal("100.00")


# ============================================================
# Performance shape (Step 77)
# ============================================================

def test_incremental_cost_scales_with_changes_not_source_size():
    """10,000 records available, one changed. One unit of work."""
    rows = [invoice_row(i) for i in range(1, 10_001)]
    harness = Harness(rows=rows)
    harness.catch_up(SyncOptions(batch_size=10_000))
    harness.reset_counters()

    harness.source.add(invoice_row(10_001, offset_seconds=99_999))
    summary = harness.run()

    assert harness.source.total_rows == 10_001
    assert summary.changes_read == 1
    assert summary.changes_processed == 1
    assert harness.builder.rebuild_calls == 1
    assert harness.embedder.calls == 1


def test_an_unchanged_source_does_no_downstream_work(harness):
    summary = harness.run()

    assert summary.changes_read == 0
    assert harness.builder.rebuild_calls == 0
    assert harness.embedder.calls == 0
    assert harness.vectors.upsert_calls == 0


# ============================================================
# Run summary (Steps 37, 38, 70)
# ============================================================

def test_the_summary_reports_every_required_metric(harness):
    harness.source.add(invoice_row(101, offset_seconds=500))
    payload = harness.run().to_dict()

    for key in (
        "changes_read", "changes_processed", "changes_failed", "changes_skipped",
        "canonical_upserts", "canonical_deletes", "representations_rebuilt",
        "representations_changed", "representations_unchanged",
        "embeddings_generated", "embeddings_skipped", "vectors_upserted",
        "vectors_deleted", "watermark_before", "watermark_after",
        "duration_seconds", "status", "checkpoint_advanced",
    ):
        assert key in payload


def test_the_summary_records_reproducibility_metadata(harness):
    """Step 53."""
    harness.source.add(invoice_row(101, offset_seconds=500))
    summary = harness.run()

    assert summary.schema_id == "erp_pg.phase10.v1"
    assert summary.mapping_id == "p10.inv"
    assert summary.transformation_engine_version
    assert summary.sync_engine_version


def test_an_empty_run_reports_a_defined_state(harness):
    summary = harness.run()

    assert summary.changes_read == 0
    assert summary.counters_balance
    assert summary.duration_seconds >= 0.0
