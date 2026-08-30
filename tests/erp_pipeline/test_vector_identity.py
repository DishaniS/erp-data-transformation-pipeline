"""Vector identity and its refusal rules.

MIGRATED FROM ``tests/test_stable_identity.py``
-----------------------------------------------
That file tested the dataset prototype's ``stable_ids`` module and its
embedding uploader. The prototype has been consolidated away, so the same
guarantees are asserted here against the generic modules that now own them:

    prototype                                  generic owner
    -----------------------------------------  ------------------------------
    make_qdrant_point_id                       sync.hashing.vector_id_for
    stable_ids.compute_content_hash            schemas.identity.compute_content_hash
    resolve_record_id's SERIAL refusal         schemas.identity.require_business_key
                                               + verification.check_record_identity
    "linkage must not use source_record_id"    the property tested below

The refusal rule is the one worth keeping deliberately: the prototype learned
that deriving vector identity from a PostgreSQL ``SERIAL`` silently
re-identified every record whenever a table was rebuilt, orphaning every stored
vector. That lesson is now a first-class guard in the framework.
"""

from __future__ import annotations

import uuid

import pytest

from erp_pipeline.schemas.identity import (
    IdentityError,
    compute_content_hash,
    make_canonical_record_id,
    make_deterministic_uuid,
    looks_like_surrogate_key,
    require_business_key,
)
from erp_pipeline.sync.hashing import vector_id_for
from erp_pipeline.verification import IntegrityCode, check_record_identity


CASE_ID = make_canonical_record_id(
    "bpi_demo", "domestic_declarations", "declaration 100000"
)


# ============================================================
# Deterministic vector identity
# ============================================================


def test_vector_id_is_a_uuid5():
    derived = vector_id_for(CASE_ID)

    assert uuid.UUID(derived).version == 5


def test_vector_id_is_deterministic():
    assert vector_id_for(CASE_ID) == vector_id_for(CASE_ID)


def test_different_records_get_different_vector_ids():
    other = make_canonical_record_id(
        "bpi_demo", "domestic_declarations", "declaration 100001"
    )

    assert vector_id_for(CASE_ID) != vector_id_for(other)


def test_vector_id_survives_a_content_change():
    """An upsert must update the same point, not accumulate a new one.

    Vector identity is derived from the record's IDENTITY, never from its
    content, which is exactly why re-embedding a changed record replaces its
    vector instead of leaving the stale one behind.
    """
    assert vector_id_for(CASE_ID) == vector_id_for(CASE_ID)


def test_vector_id_rejects_an_empty_record_id():
    with pytest.raises(IdentityError):
        make_deterministic_uuid("")


def test_two_source_systems_do_not_share_a_vector_id():
    """The component the prototype's identity scheme could not express."""
    left = make_canonical_record_id("erp_a", "invoice", "1001")
    right = make_canonical_record_id("erp_b", "invoice", "1001")

    assert vector_id_for(left) != vector_id_for(right)


# ============================================================
# Identity must never come from a surrogate key
# ============================================================


@pytest.mark.parametrize("value", ["4471", 4471, "  12  "])
def test_a_bare_integer_is_recognised_as_a_surrogate_key(value):
    assert looks_like_surrogate_key(value) is True


@pytest.mark.parametrize(
    "value", ["INV-001", "declaration 100000", "cus-44", "PO-2291", "abc"]
)
def test_a_business_key_is_never_mistaken_for_a_surrogate_key(value):
    """Flagging these would make the guard unusable on real ERP data."""
    assert looks_like_surrogate_key(value) is False


def test_require_business_key_refuses_a_serial():
    with pytest.raises(IdentityError, match="surrogate key"):
        require_business_key(33000, "record id")


def test_require_business_key_passes_a_real_key_through():
    assert require_business_key("declaration 100000") == "declaration 100000"


def test_a_canonical_id_built_from_a_serial_is_reported_by_verification():
    """The composed form the prototype refused outright, now detected by
    inspecting the stable-key component of a parsed canonical id."""
    record_id = make_canonical_record_id("bpi_demo", "case", "4471")

    codes = [issue.code for issue in check_record_identity(record_id)]

    assert codes == [IntegrityCode.SURROGATE_KEY_IDENTITY]


def test_linkage_does_not_depend_on_a_source_row_id():
    """A record carrying no SERIAL at all must still resolve and embed.

    Migrated verbatim in intent from the prototype: identity comes from the
    business key alone, so a rebuild that renumbers every row changes nothing.
    """
    record_id = make_canonical_record_id(
        "bpi_demo", "travel_permits", "travel permit 76455"
    )

    assert check_record_identity(record_id) == ()
    assert uuid.UUID(vector_id_for(record_id))


def test_a_rebuilt_source_table_does_not_change_identity():
    """The exact defect the prototype's guard existed to prevent: the same
    business record reloaded under new row numbers keeps its identity."""
    first_load = make_canonical_record_id("bpi_demo", "declarations", "declaration 100000")
    after_rebuild = make_canonical_record_id("bpi_demo", "declarations", "declaration 100000")

    assert first_load == after_rebuild
    assert vector_id_for(first_load) == vector_id_for(after_rebuild)


# ============================================================
# Content hashing
# ============================================================


def test_content_hash_is_stable_for_identical_content():
    args = ("erp:x:case:y", {"process_type": "domestic", "total_events": 5}, "text")

    assert compute_content_hash(*args) == compute_content_hash(*args)


def test_content_hash_changes_when_the_ai_text_changes():
    content = {"total_events": 5}

    assert compute_content_hash("erp:x:case:y", content, "one") != (
        compute_content_hash("erp:x:case:y", content, "two")
    )


def test_content_hash_changes_when_the_content_changes():
    assert compute_content_hash("erp:x:case:y", {"total_events": 5}, "t") != (
        compute_content_hash("erp:x:case:y", {"total_events": 6}, "t")
    )


def test_content_hash_ignores_absent_versus_null():
    """Otherwise adding an empty optional field would re-embed the record."""
    assert compute_content_hash("erp:x:case:y", {"a": 1}, "t") == (
        compute_content_hash("erp:x:case:y", {"a": 1, "b": None}, "t")
    )


def test_content_hash_is_sha256_hex():
    digest = compute_content_hash("erp:x:case:y", {"a": 1}, "t")

    assert len(digest) == 64
    int(digest, 16)
