"""Phase 9 - which version of an ERP slot is the current one.

The gap this closes: replacing a certificate produces a NEW representation id
(content identity changed), so nothing overwrites the old one and both stay
searchable. A query then returns the superseded certificate beside the real one
with nothing to tell them apart.

``test_a_replaced_certificate_stops_being_current`` is the core of the phase.
``test_emp003_is_untouched_when_emp002s_certificate_changes`` is the one that
would be easiest to get catastrophically wrong: two employees can share
identical certificate bytes, and cleanup must never cross that boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from erp_pipeline.ai.attached_documents import (
    DocumentAttachment,
    attached_document_to_representations,
)
from erp_pipeline.ingestion.binary_assets import extract_binary_asset
from erp_pipeline.orchestration.lifecycle import (
    SLOT_ATTACHMENT,
    SLOT_RECORD,
    SLOT_SCHEMA,
    InMemoryLifecycleRegistry,
    content_generation,
    group_by_slot,
    logical_key_for,
)
from erp_pipeline.sync.propagation import AIRepresentation

NOW = datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)


def pdf(text: str, lines: int = 1) -> bytes:
    """A PDF holding ``text``, optionally as many rendered lines.

    ``lines`` matters: PyMuPDF clips one very long ``insert_text`` call at the
    page edge, so a long single line yields SHORT extracted text and only one
    chunk. Real multi-chunk documents need real rendered lines.
    """
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    page = document.new_page()

    for index in range(lines):
        page.insert_text(
            (56, 60 + (index % 30) * 24),
            f"{text} line {index + 1} the parties agree to the terms herein",
            fontsize=9,
        )

        if index % 30 == 29 and index + 1 < lines:
            page = document.new_page()

    payload = document.tobytes()
    document.close()

    return payload


def certificate_reps(text: str, employee="EMP002", field="birth_certificate",
                     lines: int = 1):
    asset = extract_binary_asset(pdf(text, lines), field)
    attachment = DocumentAttachment(
        parent_record_id=f"erp:legacy_hr:employees:{employee.lower()}",
        source_system_id="legacy_hr",
        source_entity="employees",
        source_field=field,
        document_id=asset.document_id or "",
        business_key_name="employee_id",
        business_key_value=employee,
        document_type=field,
    )

    return attached_document_to_representations(asset.document, attachment)


def structured_rep(record_id: str, text: str = "Employee EMP002") -> AIRepresentation:
    return AIRepresentation(
        representation_id=f"ai:employees:{record_id.replace(':', '_')}",
        entity_type="employees",
        text_for_ai=text,
        metadata={
            "content_kind": "structured_record",
            "canonical_record_id": record_id,
        },
    )


def schema_rep(entity_id: str, index: int) -> AIRepresentation:
    return AIRepresentation(
        representation_id=f"ai:schema:{entity_id}#{index}",
        entity_type="schema",
        text_for_ai=f"fields group {index}",
        metadata={
            "content_kind": "schema",
            "entity_id": entity_id,
            "schema_chunk_index": index,
        },
    )


def anonymous_document_rep(document_id: str) -> AIRepresentation:
    return AIRepresentation(
        representation_id=f"ai:document:{document_id}",
        entity_type="document",
        text_for_ai="COMPANY POLICY",
        metadata={"content_kind": "document_chunk", "document_id": document_id},
    )


# ======================================================================
# Slot identity
# ======================================================================


def test_a_structured_record_slot_is_its_canonical_id():
    key = logical_key_for(structured_rep("erp:legacy_hr:employees:emp002"))

    assert key == f"{SLOT_RECORD}:erp:legacy_hr:employees:emp002"


def test_an_attachment_slot_is_the_parent_and_the_field():
    key = logical_key_for(certificate_reps("A")[0])

    assert key == (
        f"{SLOT_ATTACHMENT}:erp:legacy_hr:employees:emp002|birth_certificate"
    )


def test_a_schema_slot_is_the_stable_entity_id():
    key = logical_key_for(schema_rep("legacy_hr.public.employees", 0))

    assert key == f"{SLOT_SCHEMA}:legacy_hr.public.employees"


def test_every_chunk_of_one_document_shares_a_slot():
    """A three-chunk contract is one slot, not three."""
    reps = certificate_reps("CONTRACT", lines=60)

    assert len(reps) > 1
    assert len({logical_key_for(item) for item in reps}) == 1


def test_two_fields_on_one_record_are_different_slots():
    """Updating a certificate must not disturb the contract beside it."""
    certificate = logical_key_for(certificate_reps("A", field="birth_certificate")[0])
    contract = logical_key_for(certificate_reps("A", field="employment_contract")[0])

    assert certificate != contract


def test_two_employees_are_different_slots():
    first = logical_key_for(certificate_reps("A", "EMP002")[0])
    second = logical_key_for(certificate_reps("A", "EMP003")[0])

    assert first != second


# ======================================================================
# DR24 - anonymous documents are NOT guessed to be replacements
# ======================================================================


def test_an_anonymous_document_has_no_slot():
    """Two unrelated uploaded PDFs are not versions of each other."""
    assert logical_key_for(anonymous_document_rep("doc-a")) is None


def test_anonymous_documents_are_left_out_of_lifecycle_management():
    grouped = group_by_slot(
        [anonymous_document_rep("doc-a"), anonymous_document_rep("doc-b")]
    )

    assert grouped == {}


def test_an_upload_with_a_business_identity_does_have_a_slot():
    """A caller who declared the association gets replacement semantics."""
    declared = AIRepresentation(
        representation_id="ai:document:x",
        entity_type="document",
        text_for_ai="BIRTH CERTIFICATE",
        metadata={
            "content_kind": "document_chunk",
            "business_key_name": "employee_id",
            "business_key_value": "EMP002",
            "document_type": "birth_certificate",
            "source_field": "birth_certificate",
        },
    )

    assert logical_key_for(declared) is not None


# ======================================================================
# TEST H - the core replacement
# ======================================================================


@pytest.fixture
def registry():
    return InMemoryLifecycleRegistry()


def promote(registry, reps, run="job_1"):
    key = logical_key_for(reps[0])

    return registry.replace_current(
        key, [item.representation_id for item in reps],
        content_generation(reps), sync_run_id=run,
    )


def test_a_replaced_certificate_stops_being_current(registry):
    version_a = certificate_reps("BIRTH CERTIFICATE version A")
    version_b = certificate_reps("BIRTH CERTIFICATE version B amended")

    # Different content, so genuinely different representations.
    assert {r.representation_id for r in version_a} != {
        r.representation_id for r in version_b
    }

    promote(registry, version_a)
    result = promote(registry, version_b)

    assert result.superseded
    assert registry.is_current(version_a[0].representation_id) is False
    assert registry.is_current(version_b[0].representation_id) is True


def test_the_superseded_version_is_queued_for_cleanup(registry):
    promote(registry, certificate_reps("version A"))
    promote(registry, certificate_reps("version B"))

    pending = registry.pending_cleanup()

    assert len(pending) == 1
    assert pending[0].is_current is False
    assert pending[0].cleanup_pending is True


def test_the_current_set_is_exactly_the_new_version(registry):
    promote(registry, certificate_reps("version A"))
    version_b = certificate_reps("version B")
    promote(registry, version_b)

    key = logical_key_for(version_b[0])

    assert set(registry.current_ids(key)) == {
        item.representation_id for item in version_b
    }


# ======================================================================
# TEST I / DR20 - shrink and grow
# ======================================================================


def test_a_document_that_shrinks_leaves_no_stale_chunks(registry):
    long_version = certificate_reps("CONTRACT", lines=60)
    short_version = certificate_reps("CONTRACT brief")

    assert len(long_version) > len(short_version)

    promote(registry, long_version)
    result = promote(registry, short_version)
    key = logical_key_for(short_version[0])

    assert len(registry.current_ids(key)) == len(short_version)
    assert len(result.superseded) == len(long_version)

    for stale in long_version:
        assert registry.is_current(stale.representation_id) is False


def test_a_document_that_grows_makes_every_new_chunk_current(registry):
    short_version = certificate_reps("CONTRACT brief")
    long_version = certificate_reps("CONTRACT", lines=60)

    promote(registry, short_version)
    promote(registry, long_version)
    key = logical_key_for(long_version[0])
    current = set(registry.current_ids(key))

    assert current == {item.representation_id for item in long_version}
    assert len(current) == len(long_version)


def test_a_schema_entity_that_shrinks_drops_its_extra_chunks(registry):
    entity = "legacy_hr.public.employees"
    wide = [schema_rep(entity, index) for index in range(23)]
    narrow = [schema_rep(entity, index) for index in range(3)]

    promote(registry, wide)
    promote(registry, narrow)

    assert len(registry.current_ids(f"{SLOT_SCHEMA}:{entity}")) == 3
    assert registry.is_current(wide[20].representation_id) is False


def test_a_schema_entity_that_grows_makes_every_group_current(registry):
    entity = "legacy_hr.public.employees"
    narrow = [schema_rep(entity, index) for index in range(3)]
    wide = [schema_rep(entity, index) for index in range(23)]

    promote(registry, narrow)
    promote(registry, wide)

    assert len(registry.current_ids(f"{SLOT_SCHEMA}:{entity}")) == 23


# ======================================================================
# TEST S - shared content must not leak across parents
# ======================================================================


def test_emp003_is_untouched_when_emp002s_certificate_changes(registry):
    """Two employees can hold identical certificate bytes.

    Cleanup keyed on content rather than slot would delete EMP003's perfectly
    valid representation because EMP002's changed. That is the failure this
    test exists to make impossible.
    """
    shared_text = "BIRTH CERTIFICATE standard form"
    emp002_a = certificate_reps(shared_text, "EMP002")
    emp003 = certificate_reps(shared_text, "EMP003")

    promote(registry, emp002_a)
    promote(registry, emp003)

    # EMP002 gets a new certificate; EMP003 keeps the old one.
    promote(registry, certificate_reps("BIRTH CERTIFICATE reissued", "EMP002"))

    assert registry.is_current(emp002_a[0].representation_id) is False
    assert registry.is_current(emp003[0].representation_id) is True


def test_retiring_one_slot_leaves_every_other_slot_alone(registry):
    emp002 = certificate_reps("A", "EMP002")
    emp003 = certificate_reps("A", "EMP003")

    promote(registry, emp002)
    promote(registry, emp003)
    registry.retire_slot(logical_key_for(emp002[0]))

    assert registry.is_current(emp002[0].representation_id) is False
    assert registry.is_current(emp003[0].representation_id) is True


def test_updating_one_field_leaves_the_other_field_current(registry):
    certificate = certificate_reps("CERT", field="birth_certificate")
    contract = certificate_reps("CONTRACT", field="employment_contract")

    promote(registry, certificate)
    promote(registry, contract)
    promote(registry, certificate_reps("CERT v2", field="birth_certificate"))

    assert registry.is_current(contract[0].representation_id) is True


# ======================================================================
# TEST Q / DR31 - idempotence
# ======================================================================


def test_reindexing_an_unchanged_source_changes_nothing(registry):
    version = certificate_reps("BIRTH CERTIFICATE stable")

    promote(registry, version)
    before = registry.count()
    result = promote(registry, version)

    assert result.unchanged is True
    assert result.superseded == ()
    assert registry.count() == before
    assert registry.pending_cleanup() == ()


def test_repeated_reindexing_never_accumulates(registry):
    version = certificate_reps("BIRTH CERTIFICATE stable")

    for _ in range(5):
        promote(registry, version)

    assert registry.count() == len(version)
    assert len(registry.current_ids(logical_key_for(version[0]))) == len(version)


# ======================================================================
# DR18 - a deleted record retires its slot
# ======================================================================


def test_a_deleted_record_retires_everything_in_its_slot(registry):
    reps = certificate_reps("CERT")
    promote(registry, reps)

    retired = registry.retire_slot(logical_key_for(reps[0]))

    assert set(retired) == {item.representation_id for item in reps}
    assert registry.current_ids(logical_key_for(reps[0])) == ()

    for item in reps:
        assert registry.is_current(item.representation_id) is False


def test_a_retired_slot_is_queued_for_cleanup(registry):
    reps = certificate_reps("CERT")
    promote(registry, reps)
    registry.retire_slot(logical_key_for(reps[0]))

    assert len(registry.pending_cleanup()) == len(reps)


# ======================================================================
# TEST K - a failed physical delete must not become a wrong answer
# ======================================================================


def test_a_superseded_entry_stays_non_current_until_cleaned(registry):
    """Physical cleanup is allowed to lag. Correctness is not."""
    version_a = certificate_reps("A")
    promote(registry, version_a)
    promote(registry, certificate_reps("B"))

    stale = version_a[0].representation_id

    # Cleanup has not run - the entry is still pending.
    assert registry.is_current(stale) is False
    assert registry.pending_cleanup()

    registry.mark_cleaned(logical_key_for(version_a[0]), stale)

    # Cleaned, and STILL not current. Cleanup does not resurrect anything.
    assert registry.is_current(stale) is False
    assert registry.pending_cleanup() == ()


def test_marking_something_clean_twice_is_harmless(registry):
    reps = certificate_reps("A")
    promote(registry, reps)
    promote(registry, certificate_reps("B"))
    key = logical_key_for(reps[0])

    assert registry.mark_cleaned(key, reps[0].representation_id) is True
    assert registry.mark_cleaned(key, reps[0].representation_id) is False


def test_an_unmanaged_representation_reports_no_lifecycle_opinion(registry):
    """``None`` means "no row", which is different from "not current"."""
    assert registry.is_current("ai:document:never-registered") is None


# ======================================================================
# Storage backstop
# ======================================================================


def test_the_storage_state_defaults_to_current():
    """Every vector written before Phase 9 keeps behaving as it did."""
    from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier

    metadata = StorageRecordMetadata(
        representation_id="r", embedding_id="e", vector_id="v",
        current_tier=StorageTier.HOT, content_hash="h", model_id="m",
        dimension=4,
    )

    assert metadata.is_current is True
    assert metadata.logical_key is None


def test_a_superseded_vector_is_excluded_from_search():
    """The backstop: PostgreSQL decides, whatever Qdrant still holds."""
    from dataclasses import replace

    from erp_pipeline.storage.hybrid_store import HybridVectorStore
    from erp_pipeline.storage.migration import TierSet
    from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier
    from erp_pipeline.storage.state import InMemoryTierStateStore
    from erp_pipeline.sync.hashing import vector_id_for

    class Tier:
        dimension = 4

        def __init__(self):
            self.points = []

        def upsert(self, record, payload=None):
            self.points.append((vector_id_for(record.representation_id), {}))
            return True

        def get_vector(self, representation_id):
            return (0.1, 0.2, 0.3, 0.4)

        def exists(self, representation_id):
            return True

        def delete(self, representation_id):
            return True

        def count(self):
            return len(self.points)

        def search(self, vector, limit=5, query_filter=None):
            return [(vector_id, 0.9) for vector_id, _ in self.points][:limit]

    state = InMemoryTierStateStore()
    tier = Tier()
    store = HybridVectorStore(TierSet(hot=tier), state)

    for representation_id, current in (("ai:doc:new", True), ("ai:doc:old", False)):
        metadata = StorageRecordMetadata(
            representation_id=representation_id,
            embedding_id=f"emb.{representation_id}",
            vector_id=vector_id_for(representation_id),
            current_tier=StorageTier.HOT, content_hash="h", model_id="m",
            dimension=4, is_current=current,
        )
        state.save(metadata)
        # The stale vector is STILL physically present - its delete "failed".
        tier.points.append((metadata.vector_id, {}))

    hits = store.search([0.1, 0.2, 0.3, 0.4], limit=10).hits
    returned = {hit.representation_id for hit in hits}

    assert "ai:doc:new" in returned
    assert "ai:doc:old" not in returned


# ======================================================================
# Generations
# ======================================================================


def test_the_generation_changes_with_the_content(registry):
    assert content_generation(certificate_reps("A")) != content_generation(
        certificate_reps("B")
    )


def test_the_generation_is_stable_for_the_same_set():
    reps = certificate_reps("A")

    assert content_generation(reps) == content_generation(list(reversed(reps)))


def test_grouping_buckets_representations_by_slot():
    grouped = group_by_slot(
        list(certificate_reps("A", "EMP002"))
        + list(certificate_reps("A", "EMP003"))
        + [structured_rep("erp:legacy_hr:employees:emp002")]
    )

    assert len(grouped) == 3
