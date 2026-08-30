"""Cross-store scans.

Migrated from the prototype's PostgreSQL/Qdrant integrity script, which could
only run against live stores. These run against protocol implementations, so
the same rules are provable in milliseconds and in CI.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync.hashing import vector_id_for
from erp_pipeline.verification import (
    IntegrityCode,
    IntegrityVerificationService,
    InMemoryVectorIndex,
    verify_canonical_records,
    verify_embeddings_against_state,
    verify_orphaned_vectors,
    verify_tier_state,
)

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DIMENSION = 384


def metadata(representation_id, vector_id=None, **overrides):
    payload = {
        "representation_id": representation_id,
        "embedding_id": "emb.abc",
        "vector_id": vector_id or vector_id_for(representation_id),
        "current_tier": StorageTier.HOT,
        "content_hash": "hash-1",
        "model_id": MODEL,
        "dimension": DIMENSION,
    }
    payload.update(overrides)

    return StorageRecordMetadata(**payload)


@pytest.fixture
def state():
    return InMemoryTierStateStore()


def codes(report):
    return [issue.code for issue in report.issues]


# ============================================================
# Tier state
# ============================================================


def test_a_consistent_deployment_passes(state):
    representation_id = "ai:invoice:erp_a_invoice_inv-1"
    state.save(metadata(representation_id))
    index = InMemoryVectorIndex([representation_id])

    report = verify_tier_state(
        state, index, expected_model_id=MODEL, expected_dimension=DIMENSION
    )

    assert report.passed is True
    assert report.counts["tier_state_entries"] == 1


def test_state_pointing_at_a_vector_the_index_does_not_have_is_caught(state):
    """The failure a retrieval consumer actually hits."""
    state.save(metadata("ai:invoice:erp_a_invoice_inv-1"))

    report = verify_tier_state(state, InMemoryVectorIndex([]))

    assert IntegrityCode.VECTOR_MISSING in codes(report)
    assert report.counts["missing_vectors"] == 1


def test_a_hand_written_vector_id_is_caught(state):
    state.save(metadata("ai:invoice:x", vector_id="hand-written"))

    report = verify_tier_state(state, InMemoryVectorIndex(["ai:invoice:x"]))

    assert IntegrityCode.VECTOR_ID_MISMATCH in codes(report)


def test_a_vector_from_another_model_is_caught(state):
    state.save(metadata("ai:invoice:x", model_id="other-model"))

    report = verify_tier_state(
        state, InMemoryVectorIndex(["ai:invoice:x"]), expected_model_id=MODEL
    )

    assert IntegrityCode.MODEL_ID_MISMATCH in codes(report)


def test_a_dimension_disagreement_is_caught(state):
    state.save(metadata("ai:invoice:x", dimension=768))

    report = verify_tier_state(
        state, InMemoryVectorIndex(["ai:invoice:x"]), expected_dimension=DIMENSION
    )

    assert IntegrityCode.DIMENSION_MISMATCH in codes(report)


def test_scanning_an_empty_deployment_passes(state):
    report = verify_tier_state(state, InMemoryVectorIndex([]))

    assert report.passed is True
    assert report.subjects_examined == 0


# ============================================================
# Orphans
# ============================================================


def test_a_vector_no_state_accounts_for_is_reported(state):
    state.save(metadata("ai:invoice:known"))

    report = verify_orphaned_vectors(["ai:invoice:known", "ai:invoice:ghost"], state)

    assert codes(report) == [IntegrityCode.ORPHANED_VECTOR]
    assert report.issues[0].subject_id == "ai:invoice:ghost"


def test_no_orphans_passes(state):
    state.save(metadata("ai:invoice:known"))

    assert verify_orphaned_vectors(["ai:invoice:known"], state).passed is True


# ============================================================
# Canonical records
# ============================================================


class FakeRecordStore:
    def __init__(self, ids, records=None):
        self._ids = list(ids)
        self._records = records if records is not None else {rid: object() for rid in ids}

    def record_ids(self, limit: int = 100):
        return self._ids[:limit]

    def get(self, canonical_id):
        return self._records.get(canonical_id)


def test_canonical_scan_reports_malformed_and_surrogate_identity():
    store = FakeRecordStore(
        ["erp:a:invoice:inv-1", "erp:a:case:4471", "garbage"]
    )

    report = verify_canonical_records(store)

    found = set(codes(report))
    assert IntegrityCode.SURROGATE_KEY_IDENTITY in found
    assert IntegrityCode.MALFORMED_RECORD_ID in found


def test_canonical_scan_reports_duplicates():
    store = FakeRecordStore(["erp:a:invoice:inv-1", "erp:a:invoice:inv-1"])

    assert IntegrityCode.DUPLICATE_RECORD_ID in codes(verify_canonical_records(store))


class StateEntryCarryingCanonicalId:
    """A tier-state entry that knows which canonical record it derives from.

    ``StorageRecordMetadata`` does not carry a ``canonical_record_id`` today,
    which is why ``_canonical_id_for`` reads it duck-typed and skips the check
    when it is absent. This fake stands in for a state store that does record
    it, so the orphan rule itself is still proved.
    """

    def __init__(self, representation_id, canonical_record_id):
        self.representation_id = representation_id
        self.vector_id = vector_id_for(representation_id)
        self.current_tier = StorageTier.HOT
        self.content_hash = "hash-1"
        self.model_id = MODEL
        self.dimension = DIMENSION
        self.canonical_record_id = canonical_record_id


class StaticStateStore:
    def __init__(self, entries):
        self._entries = list(entries)

    def list_all(self, *args, **kwargs):
        return self._entries

    def load(self, representation_id):
        for entry in self._entries:
            if entry.representation_id == representation_id:
                return entry
        return None


def test_tier_state_whose_canonical_record_vanished_is_reported():
    """Only checkable when the canonical id is recorded; the scan skips the
    check rather than guessing when it is not."""
    store = FakeRecordStore([], records={})
    state = StaticStateStore(
        [
            StateEntryCarryingCanonicalId(
                "ai:invoice:x", "erp:a:invoice:gone"
            )
        ]
    )

    report = verify_tier_state(
        state, InMemoryVectorIndex(["ai:invoice:x"]), canonical_records=store
    )

    assert IntegrityCode.ORPHANED_TIER_STATE in codes(report)


def test_state_without_a_recorded_canonical_id_is_not_falsely_orphaned(state):
    state.save(metadata("ai:invoice:x"))
    store = FakeRecordStore([], records={})

    report = verify_tier_state(
        state, InMemoryVectorIndex(["ai:invoice:x"]), canonical_records=store
    )

    assert IntegrityCode.ORPHANED_TIER_STATE not in codes(report)


# ============================================================
# Embeddings against state
# ============================================================


def embedding(representation_id, **overrides):
    payload = {
        "embedding_id": "emb.abc",
        "representation_id": representation_id,
        "content_hash": "hash-1",
        "model_id": MODEL,
        "dimension": DIMENSION,
        "status": EmbeddingStatus.GENERATED,
        "vector": tuple([0.0] * 4),
    }
    payload.update(overrides)

    return EmbeddingRecord(**payload)


def test_embeddings_agreeing_with_state_pass(state):
    state.save(metadata("ai:invoice:x"))

    report = verify_embeddings_against_state([embedding("ai:invoice:x")], state)

    assert report.passed is True


def test_an_embedding_with_no_state_is_reported(state):
    report = verify_embeddings_against_state([embedding("ai:invoice:x")], state)

    assert codes(report) == [IntegrityCode.VECTOR_MISSING]


def test_state_that_disagrees_with_its_embedding_is_reported(state):
    state.save(metadata("ai:invoice:x", content_hash="stale"))

    report = verify_embeddings_against_state([embedding("ai:invoice:x")], state)

    assert IntegrityCode.CONTENT_HASH_MISMATCH in codes(report)


# ============================================================
# Service
# ============================================================


def test_service_runs_every_configured_scan_as_one_verdict(state):
    good = "ai:invoice:erp_a_invoice_inv-1"
    state.save(metadata(good))
    index = InMemoryVectorIndex([good, "ai:invoice:ghost"])

    service = IntegrityVerificationService(
        canonical_records=FakeRecordStore(["erp:a:invoice:inv-1"]),
        tier_state=state,
        vector_index=index,
        expected_model_id=MODEL,
        expected_dimension=DIMENSION,
    )

    report = service.verify_all(vector_ids=index.all_ids())

    assert report.passed is False
    assert IntegrityCode.ORPHANED_VECTOR in codes(report)


def test_service_with_no_stores_reports_zero_checks_rather_than_a_pass():
    """A missing store means the question could not be asked. Reporting that
    as a pass would be the most dangerous possible answer."""
    report = IntegrityVerificationService().verify_all()

    assert report.checks_run == 0


def test_service_verifies_representations_and_their_embeddings():
    from erp_pipeline.sync.propagation import AIRepresentation
    from dataclasses import replace

    representation = AIRepresentation(
        representation_id="ai:invoice:erp_a_invoice_inv-1",
        entity_type="invoice",
        text_for_ai="Entity: Invoice",
        content={"invoice_id": "INV-1"},
    )
    representation = replace(
        representation, content_hash=representation.compute_hash()
    )

    service = IntegrityVerificationService(
        expected_model_id=MODEL, expected_dimension=DIMENSION
    )

    stale = embedding(representation.representation_id, content_hash="0" * 64)
    report = service.verify_representations([representation], [stale])

    assert IntegrityCode.EMBEDDING_STALE in codes(report)


def test_the_verification_package_never_imports_a_dataset_module():
    import pathlib

    package = pathlib.Path(
        __file__
    ).resolve().parents[3] / "src" / "erp_pipeline" / "verification"

    for module in package.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert "bpi2020" not in source, module.name
        assert "erp_integrations" not in source, module.name
