"""Phase 0 unit tests for deterministic cross-layer identity.

These tests need no database, no Qdrant, and no BPI dataset. They pin down the
identity rules that the whole pipeline depends on, so a regression shows up in
under a second instead of after a 33,000-record embedding run.
"""

import hashlib
import uuid

import pytest

from bpi2020.common.stable_ids import (
    SOURCE_SYSTEM,
    StableIdError,
    compute_content_hash,
    make_case_record_id,
    make_document_record_id,
    make_event_record_id,
    make_qdrant_point_id,
    normalize_key_component,
)


# ============================================================
# 1. Deterministic case record ID
# ============================================================

def test_case_record_id_is_deterministic():
    first = make_case_record_id("domestic_declarations", "declaration 100000")
    second = make_case_record_id("domestic_declarations", "declaration 100000")

    assert first == second
    assert first == "case:domestic_declarations:declaration_100000"


def test_case_record_id_ignores_incidental_formatting():
    """Whitespace and case differences must not create a second identity."""
    assert make_case_record_id("Domestic_Declarations", "  Declaration 100000 ") == (
        make_case_record_id("domestic_declarations", "declaration 100000")
    )


def test_case_record_id_distinguishes_process_types():
    domestic = make_case_record_id("domestic_declarations", "declaration 1")
    international = make_case_record_id("international_declarations", "declaration 1")

    assert domestic != international


def test_case_record_id_rejects_empty_identity():
    with pytest.raises(StableIdError):
        make_case_record_id("domestic_declarations", "")

    with pytest.raises(StableIdError):
        make_case_record_id("domestic_declarations", None)


# ============================================================
# 2. Deterministic document record ID
# ============================================================

def test_document_record_id_is_deterministic():
    document_id = "a102d03b6986f92816520534"

    assert make_document_record_id(document_id) == make_document_record_id(document_id)
    assert make_document_record_id(document_id) == f"document:{document_id}"


def test_document_record_id_follows_content_identity():
    """A different content hash must yield a different logical document."""
    assert make_document_record_id("aaaa1111") != make_document_record_id("bbbb2222")


# ============================================================
# 3. Deterministic event ID
# ============================================================

def test_event_record_id_format_and_determinism():
    first = make_event_record_id("domestic_declarations_raw", 12345)
    second = make_event_record_id("domestic_declarations_raw", "12345")

    assert first == second
    assert first == f"event:{SOURCE_SYSTEM}:domestic_declarations_raw:12345"


def test_event_record_id_is_unique_per_source_entity():
    """The same row number in two legacy tables must not collide."""
    domestic = make_event_record_id("domestic_declarations_raw", 1)
    permits = make_event_record_id("travel_permit_raw", 1)

    assert domestic != permits


# ============================================================
# 4. Deterministic Qdrant point ID
# ============================================================

def test_qdrant_point_id_is_deterministic_uuid5():
    record_id = "case:domestic_declarations:declaration_100000"
    point_id = make_qdrant_point_id(record_id)

    assert point_id == make_qdrant_point_id(record_id)
    assert point_id == str(uuid.uuid5(uuid.NAMESPACE_URL, f"bpi2020/{record_id}"))
    # Must be a valid UUID so Qdrant accepts it as a point ID.
    assert uuid.UUID(point_id)


def test_qdrant_point_id_differs_per_record():
    a = make_qdrant_point_id("case:domestic_declarations:declaration_1")
    b = make_qdrant_point_id("case:domestic_declarations:declaration_2")

    assert a != b


def test_qdrant_point_id_rejects_empty_record_id():
    with pytest.raises(StableIdError):
        make_qdrant_point_id("")


# ============================================================
# 5 & 6. Rebuilds do not change identity
# ============================================================

def test_rebuilding_a_case_does_not_change_its_identity():
    """
    Simulate a rebuild: the row is deleted and re-inserted, so the SERIAL
    changes from 12 to 40012. Identity must not move with it.
    """
    before = make_case_record_id("travel_permit", "travel permit 76455")
    after_rebuild = make_case_record_id("travel_permit", "travel permit 76455")

    assert before == after_rebuild
    assert make_qdrant_point_id(before) == make_qdrant_point_id(after_rebuild)


def test_rebuilding_a_document_does_not_change_its_identity():
    document_id = "a102d03b6986f92816520534"

    before = make_document_record_id(document_id)
    after_rebuild = make_document_record_id(document_id)

    assert before == after_rebuild
    assert make_qdrant_point_id(before) == make_qdrant_point_id(after_rebuild)


# ============================================================
# 7. Identical embedding preparations produce the same point ID
# ============================================================

def test_identical_embedding_preparations_produce_the_same_point_id():
    from bpi2020.embeddings.generate_and_store_embeddings import make_point_id

    record = {
        "record_id": "case:domestic_declarations:declaration_100000",
        "unified_record_id": "case:domestic_declarations:declaration_100000",
        "record_type": "erp_case",
        "source_record_id": 33000,
    }
    rerun_after_rebuild = dict(record, source_record_id=65998)

    assert make_point_id(record) == make_point_id(rerun_after_rebuild)


# ============================================================
# 8. Stale SERIAL identifiers are rejected, not silently used
# ============================================================

def test_legacy_serial_record_id_is_rejected():
    from bpi2020.embeddings.generate_and_store_embeddings import (
        EmbeddingLinkageError,
        resolve_record_id,
    )

    with pytest.raises(EmbeddingLinkageError, match="EMBEDDING_STALE_SERIAL_RECORD_ID"):
        resolve_record_id({"unified_record_id": "case_1", "record_type": "erp_case"})

    with pytest.raises(EmbeddingLinkageError, match="EMBEDDING_STALE_SERIAL_RECORD_ID"):
        resolve_record_id({"record_id": "document_4", "record_type": "erp_document"})


def test_missing_record_id_is_rejected():
    from bpi2020.embeddings.generate_and_store_embeddings import (
        EmbeddingLinkageError,
        resolve_record_id,
    )

    with pytest.raises(EmbeddingLinkageError, match="EMBEDDING_SOURCE_RECORD_NOT_FOUND"):
        resolve_record_id({"record_type": "erp_case", "source_record_id": 42})


def test_linkage_does_not_depend_on_source_record_id():
    """A record with no SERIAL at all must still resolve and embed."""
    from bpi2020.embeddings.generate_and_store_embeddings import (
        make_point_id,
        resolve_record_id,
    )

    record = {
        "record_id": "case:travel_permit:travel_permit_76455",
        "record_type": "erp_case",
    }

    assert resolve_record_id(record) == "case:travel_permit:travel_permit_76455"
    assert uuid.UUID(make_point_id(record))


# ============================================================
# 9. Content hash behaviour
# ============================================================

def test_content_hash_is_stable_for_identical_content():
    args = ("case:x:y", "some ai text", {"process_type": "domestic", "total_events": 5})

    assert compute_content_hash(*args) == compute_content_hash(*args)


def test_content_hash_changes_when_ai_text_changes():
    base = compute_content_hash("case:x:y", "text one", {"total_events": 5})
    changed = compute_content_hash("case:x:y", "text two", {"total_events": 5})

    assert base != changed


def test_content_hash_changes_when_metadata_changes():
    base = compute_content_hash("case:x:y", "text", {"total_events": 5})
    changed = compute_content_hash("case:x:y", "text", {"total_events": 6})

    assert base != changed


def test_content_hash_ignores_absent_versus_null_metadata():
    with_null = compute_content_hash("case:x:y", "text", {"a": 1, "b": None})
    without_key = compute_content_hash("case:x:y", "text", {"a": 1})

    assert with_null == without_key


def test_content_hash_is_sha256():
    digest = compute_content_hash("case:x:y", "text", {})

    assert len(digest) == len(hashlib.sha256(b"").hexdigest())
    int(digest, 16)  # raises if it is not hexadecimal


# ============================================================
# Normalization guards
# ============================================================

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("declaration 100000", "declaration_100000"),
        ("travel permit 76455", "travel_permit_76455"),
        ("Request For Payment 73550", "request_for_payment_73550"),
        ("  padded  value  ", "padded_value"),
        ("weird/chars*here", "weird_chars_here"),
    ],
)
def test_normalize_key_component(raw, expected):
    assert normalize_key_component(raw) == expected


def test_normalized_components_never_contain_separator():
    """A ':' inside a component would make the identifier ambiguous to parse."""
    assert ":" not in normalize_key_component("a:b:c")
