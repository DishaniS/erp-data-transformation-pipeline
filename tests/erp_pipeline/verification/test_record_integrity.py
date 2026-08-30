"""Record-level integrity checks.

Migrated from the identity checks in ``bpi2020.verification.
verify_cross_store_integrity``, generalized to the framework's own contracts.
Every check here is pure, so the whole rule set is provable without a database.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.schemas.identity import make_canonical_record_id
from erp_pipeline.sync.hashing import vector_id_for
from erp_pipeline.sync.propagation import AIRepresentation
from erp_pipeline.verification import (
    IntegrityCode,
    IntegritySeverity,
    VerificationReport,
    build_report,
    check_duplicate_ids,
    check_embedding,
    check_metadata_agreement,
    check_record_identity,
    check_records,
    check_representation,
    check_vector_identity,
    make_issue,
)


def codes(issues):
    return [issue.code for issue in issues]


# ============================================================
# Identity
# ============================================================


def test_a_well_formed_canonical_id_produces_no_findings():
    record_id = make_canonical_record_id("erp_a", "invoice", "INV-001")

    assert check_record_identity(record_id) == ()


def test_a_malformed_id_is_reported():
    assert codes(check_record_identity("not-an-id")) == [
        IntegrityCode.MALFORMED_RECORD_ID
    ]


def test_an_id_with_the_wrong_prefix_is_reported():
    assert codes(check_record_identity("case:declarations:100000")) == [
        IntegrityCode.MALFORMED_RECORD_ID
    ]


def test_identity_built_from_a_surrogate_key_is_reported():
    """The defect the prototype learned the hard way: a SERIAL as identity
    silently re-identifies every record when the source table is rebuilt."""
    record_id = make_canonical_record_id("erp_a", "case", "4471")

    assert codes(check_record_identity(record_id)) == [
        IntegrityCode.SURROGATE_KEY_IDENTITY
    ]


@pytest.mark.parametrize(
    "business_key", ["INV-001", "declaration 100000", "cus-44", "PO-2291"]
)
def test_ordinary_business_keys_are_never_flagged(business_key):
    """Flagging these would make the guard unusable on real ERP data."""
    record_id = make_canonical_record_id("erp_a", "invoice", business_key)

    assert check_record_identity(record_id) == ()


def test_duplicate_ids_are_reported_with_their_count():
    issues = check_duplicate_ids(["a", "b", "a", "a"])

    assert len(issues) == 1
    assert issues[0].code is IntegrityCode.DUPLICATE_RECORD_ID
    assert issues[0].context["occurrences"] == 3


def test_unique_ids_produce_no_duplicate_findings():
    assert check_duplicate_ids(["a", "b", "c"]) == ()


def test_check_records_runs_both_identity_rules():
    good = make_canonical_record_id("erp_a", "invoice", "INV-1")
    bad = make_canonical_record_id("erp_a", "case", "12")

    found = set(codes(check_records([good, bad, bad])))

    assert IntegrityCode.SURROGATE_KEY_IDENTITY in found
    assert IntegrityCode.DUPLICATE_RECORD_ID in found


# ============================================================
# Representations
# ============================================================


def representation(content_hash=None, **overrides):
    payload = {
        "representation_id": "ai:invoice:erp_a_invoice_inv-1",
        "entity_type": "invoice",
        "text_for_ai": "Entity: Invoice",
        "content": {"invoice_id": "INV-1"},
        "source_record_ids": ("erp:erp_a:invoice:inv-1",),
    }
    payload.update(overrides)
    representation = AIRepresentation(**payload)

    if content_hash is not None:
        from dataclasses import replace

        return replace(representation, content_hash=content_hash)

    from dataclasses import replace

    return replace(representation, content_hash=representation.compute_hash())


class FakeRecord:
    def __init__(self, record_id):
        self.record_id = record_id


def test_a_consistent_representation_produces_no_findings():
    item = representation()
    record = FakeRecord("erp:erp_a:invoice:inv-1")

    assert check_representation(item, record) == ()


def test_a_tampered_representation_hash_is_recomputed_and_caught():
    """The verifier recomputes rather than trusting: comparing a stored value
    against itself would catch nothing."""
    item = representation(content_hash="0" * 64)

    assert IntegrityCode.CONTENT_HASH_MISMATCH in codes(
        check_representation(item, FakeRecord("erp:erp_a:invoice:inv-1"))
    )


def test_a_representation_whose_record_is_gone_is_reported():
    assert codes(check_representation(representation(), None)) == [
        IntegrityCode.CANONICAL_RECORD_MISSING
    ]


def test_a_representation_matched_to_the_wrong_record_is_reported():
    issues = check_representation(representation(), FakeRecord("erp:erp_a:invoice:other"))

    assert IntegrityCode.CANONICAL_REFERENCE_MISMATCH in codes(issues)


# ============================================================
# Embeddings
# ============================================================


def embedding(**overrides):
    payload = {
        "embedding_id": "emb.abc",
        "representation_id": "ai:invoice:erp_a_invoice_inv-1",
        "content_hash": representation().resolved_hash(),
        "model_id": "sentence-transformers/all-MiniLM-L6-v2",
        "dimension": 4,
        "status": EmbeddingStatus.GENERATED,
        "entity_type": "invoice",
        "vector": (0.1, 0.2, 0.3, 0.4),
    }
    payload.update(overrides)

    return EmbeddingRecord(**payload)


def test_a_consistent_embedding_produces_no_findings():
    assert check_embedding(embedding(), representation()) == ()


def test_a_vector_shorter_than_its_declared_dimension_is_caught():
    issues = check_embedding(embedding(vector=(0.1, 0.2)))

    assert IntegrityCode.DIMENSION_MISMATCH in codes(issues)


def test_a_dimension_that_disagrees_with_the_configured_model_is_caught():
    issues = check_embedding(embedding(), expected_dimension=384)

    assert IntegrityCode.DIMENSION_MISMATCH in codes(issues)


def test_a_vector_from_a_different_model_is_caught():
    """Vectors from two models are not comparable, so mixing them silently
    corrupts every ranking."""
    issues = check_embedding(embedding(), expected_model_id="some-other-model")

    assert IntegrityCode.MODEL_ID_MISMATCH in codes(issues)


def test_a_stale_embedding_is_caught():
    issues = check_embedding(embedding(content_hash="0" * 64), representation())

    assert IntegrityCode.EMBEDDING_STALE in codes(issues)


def test_a_status_that_produced_no_vector_is_a_warning_not_a_failure():
    issues = check_embedding(
        embedding(status=EmbeddingStatus.SKIPPED_UNCHANGED, vector=None)
    )

    assert codes(issues) == [IntegrityCode.EMBEDDING_NOT_GENERATED]
    assert issues[0].severity is IntegritySeverity.WARNING


def test_a_generated_status_with_no_vector_is_a_failure():
    issues = check_embedding(embedding(vector=None))

    assert IntegrityCode.VECTOR_MISSING in codes(issues)
    assert issues[0].is_failure


def test_an_entity_type_disagreement_is_reported():
    issues = check_embedding(embedding(entity_type="purchase_order"), representation())

    assert IntegrityCode.ENTITY_TYPE_MISMATCH in codes(issues)


# ============================================================
# Vector identity
# ============================================================


def test_a_derived_vector_id_is_accepted():
    representation_id = "ai:invoice:erp_a_invoice_inv-1"

    assert check_vector_identity(
        representation_id, vector_id_for(representation_id)
    ) == ()


def test_a_hand_written_vector_id_is_rejected():
    issues = check_vector_identity("ai:invoice:x", "not-derived")

    assert codes(issues) == [IntegrityCode.VECTOR_ID_MISMATCH]


def test_an_absent_vector_id_is_reported():
    assert codes(check_vector_identity("ai:invoice:x", None)) == [
        IntegrityCode.VECTOR_MISSING
    ]


# ============================================================
# Tier metadata agreement
# ============================================================


class FakeMetadata:
    def __init__(self, **fields):
        self.representation_id = "ai:invoice:erp_a_invoice_inv-1"
        self.content_hash = fields.get("content_hash", embedding().content_hash)
        self.model_id = fields.get("model_id", embedding().model_id)
        self.dimension = fields.get("dimension", 4)
        self.embedding_id = fields.get("embedding_id", "emb.abc")


def test_agreeing_tier_state_produces_no_findings():
    assert check_metadata_agreement(FakeMetadata(), embedding()) == ()


def test_tier_state_with_a_stale_hash_is_reported():
    issues = check_metadata_agreement(FakeMetadata(content_hash="0" * 64), embedding())

    assert IntegrityCode.CONTENT_HASH_MISMATCH in codes(issues)


def test_tier_state_pointing_at_another_embedding_is_reported():
    issues = check_metadata_agreement(FakeMetadata(embedding_id="emb.other"), embedding())

    assert IntegrityCode.TIER_METADATA_MISMATCH in codes(issues)


# ============================================================
# Report contract
# ============================================================


def test_the_verdict_is_derived_from_the_findings():
    failing = build_report(
        [make_issue(IntegrityCode.VECTOR_MISSING, "x", "gone")], checks_run=1
    )
    clean = build_report([], checks_run=1)

    assert failing.passed is False
    assert clean.passed is True


def test_warnings_alone_do_not_fail_a_report():
    report = build_report(
        [make_issue(IntegrityCode.EMBEDDING_MISSING, "x", "not yet embedded")],
        checks_run=1,
    )

    assert report.passed is True
    assert len(report.warnings) == 1


def test_findings_never_grow_into_a_copy_of_the_data():
    issue = make_issue(IntegrityCode.VECTOR_MISSING, "x", "y" * 5000)

    assert len(issue.detail) <= 300


def test_reports_merge_into_one_verdict():
    left = build_report([], checks_run=2, counts={"a": 1})
    right = build_report(
        [make_issue(IntegrityCode.ORPHANED_VECTOR, "v", "orphan")],
        checks_run=3,
        counts={"b": 2},
    )

    merged = left.merged(right)

    assert merged.checks_run == 5
    assert merged.passed is False
    assert merged.counts == {"a": 1, "b": 2}


def test_report_serializes_without_business_values():
    report = build_report(
        [make_issue(IntegrityCode.VECTOR_MISSING, "ai:invoice:x", "gone")],
        checks_run=1,
    )

    payload = report.to_dict()

    assert payload["passed"] is False
    assert payload["count_by_code"] == {"VECTOR_MISSING": 1}
    assert "render" not in payload


def test_report_renders_a_readable_verdict():
    report = build_report(
        [make_issue(IntegrityCode.VECTOR_MISSING, "ai:invoice:x", "gone")],
        checks_run=1,
    )

    assert "FAIL" in report.render()
