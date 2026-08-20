"""Field/entity normalization and the datatype compatibility matrix.

Both are pure, deterministic and exercised directly with plain strings and
enum members - no schema, no engine.
"""

from __future__ import annotations

import pytest

from erp_pipeline.mapping import (
    NormalizationConfig,
    TypeCompatibility,
    canonical_tokens,
    compare_types,
    compatibility_matrix,
    normalized_key,
    path_tokens,
    shared_tokens,
    split_tokens,
    token_similarity,
)
from erp_pipeline.schemas.enums import FieldDataType

T = FieldDataType


# ============================================================
# Tokenization (Step 6)
# ============================================================

@pytest.mark.parametrize(
    "name,expected",
    [
        ("customer_id", ("customer", "id")),
        ("customerId", ("customer", "id")),
        ("CustomerID", ("customer", "id")),
        ("customer-id", ("customer", "id")),
        ("CUSTOMER ID", ("customer", "id")),
        ("customer.id", ("customer", "id")),
        ("totalAmount", ("total", "amount")),
        ("XMLHttpRequest", ("xml", "http", "request")),
        ("line1", ("line", "1")),
        ("invoice__no", ("invoice", "no")),
        ("", ()),
    ],
)
def test_names_split_into_comparable_tokens(name, expected):
    assert split_tokens(name) == expected


def test_every_spelling_of_one_concept_reaches_one_key():
    """The point of normalization: six conventions, one comparable form."""
    spellings = [
        "customer_id", "customerId", "CustomerID", "customer-id",
        "CUSTOMER ID", "customer.id",
    ]

    keys = {normalized_key(spelling) for spelling in spellings}

    assert keys == {"customer_id"}


def test_configured_abbreviations_expand():
    assert canonical_tokens("cust_no") == ("customer", "number")
    assert canonical_tokens("total_amt") == ("total", "amount")
    assert canonical_tokens("inv_id") == ("invoice", "id")


def test_configured_synonyms_fold():
    assert canonical_tokens("email_addr") == ("email", "address")
    assert canonical_tokens("client_id") == ("customer", "id")


def test_abbreviations_expand_before_synonyms_fold():
    """`clnt` -> `client` -> `customer` in two documented hops, so the table
    needs no entry for every combination."""
    assert canonical_tokens("clnt_id") == ("customer", "id")


def test_expansion_can_be_switched_off():
    config = NormalizationConfig(expand_abbreviations=False, apply_synonyms=False)

    assert canonical_tokens("cust_no", config) == ("cust", "no")


def test_noise_tokens_are_dropped():
    assert canonical_tokens("tbl_customer") == ("customer",)
    assert canonical_tokens("customer_master") == ("customer",)


def test_a_name_is_never_normalized_away_entirely():
    """Dropping every token would make a field unmatchable against anything."""
    assert canonical_tokens("tbl") == ("tbl",)
    assert canonical_tokens("master") == ("master",)


def test_normalization_is_deterministic():
    for _ in range(5):
        assert canonical_tokens("CustomerID") == ("customer", "id")


# ============================================================
# Similarity (Step 10)
# ============================================================

def test_token_similarity_is_order_insensitive():
    """`total_amount` and `amount_total` are the same concept."""
    left = canonical_tokens("total_amount")
    right = canonical_tokens("amount_total")

    assert token_similarity(left, right) == 1.0


@pytest.mark.parametrize(
    "left,right,expected",
    [
        (("a",), ("a",), 1.0),
        (("a", "b"), ("a",), 0.5),
        (("a",), ("b",), 0.0),
        ((), ("a",), 0.0),
        ((), (), 0.0),
    ],
)
def test_token_similarity_is_bounded_jaccard(left, right, expected):
    assert token_similarity(left, right) == expected


def test_shared_tokens_are_reported_sorted():
    assert shared_tokens(("b", "a"), ("a", "b", "c")) == ("a", "b")


def test_path_tokens_include_the_whole_access_path():
    assert path_tokens(("customer", "contact"), "email") == (
        "customer", "contact", "email",
    )


def test_path_tokens_ignore_the_array_element_marker():
    """`[]` is structural punctuation from Phases 5-7, not a name."""
    assert path_tokens(("lines", "[]"), "sku") == ("lines", "sku")


# ============================================================
# Type compatibility (Steps 11, 12)
# ============================================================

@pytest.mark.parametrize(
    "source,target,expected",
    [
        (T.STRING, T.STRING, TypeCompatibility.EXACT),
        (T.INTEGER, T.INTEGER, TypeCompatibility.EXACT),
        (T.DECIMAL, T.DECIMAL, TypeCompatibility.EXACT),
        # lossless widening
        (T.INTEGER, T.DECIMAL, TypeCompatibility.WIDENING),
        (T.DATE, T.DATETIME, TypeCompatibility.WIDENING),
        # convertible but lossy or fallible
        (T.DECIMAL, T.INTEGER, TypeCompatibility.LOSSY),
        (T.DATETIME, T.DATE, TypeCompatibility.LOSSY),
        (T.STRING, T.INTEGER, TypeCompatibility.LOSSY),
        (T.STRING, T.DATE, TypeCompatibility.LOSSY),
        (T.INTEGER, T.STRING, TypeCompatibility.LOSSY),
        # unproven
        (T.UNKNOWN, T.STRING, TypeCompatibility.UNKNOWN),
        (T.UNKNOWN, T.UNKNOWN, TypeCompatibility.UNKNOWN),
        (T.STRING, T.UNKNOWN, TypeCompatibility.UNKNOWN),
        # impossible
        (T.OBJECT, T.DECIMAL, TypeCompatibility.INCOMPATIBLE),
        (T.OBJECT, T.STRING, TypeCompatibility.INCOMPATIBLE),
        (T.ARRAY, T.STRING, TypeCompatibility.INCOMPATIBLE),
        (T.STRING, T.ARRAY, TypeCompatibility.INCOMPATIBLE),
        (T.BOOLEAN, T.DATETIME, TypeCompatibility.INCOMPATIBLE),
    ],
)
def test_the_compatibility_matrix(source, target, expected):
    assert compare_types(source, target).compatibility is expected


def test_widening_is_asymmetric():
    """Integer to decimal is lossless; the reverse truncates."""
    assert compare_types(T.INTEGER, T.DECIMAL).compatibility is (
        TypeCompatibility.WIDENING
    )
    assert compare_types(T.DECIMAL, T.INTEGER).compatibility is (
        TypeCompatibility.LOSSY
    )


def test_an_array_source_cannot_feed_a_scalar_target():
    comparison = compare_types(T.STRING, T.STRING, source_is_array=True)

    assert comparison.compatibility is TypeCompatibility.INCOMPATIBLE
    assert comparison.cardinality_conflict is True


def test_an_array_target_accepts_an_array_source():
    comparison = compare_types(T.ARRAY, T.ARRAY)

    assert comparison.compatibility is TypeCompatibility.EXACT


def test_object_versus_scalar_is_a_structural_conflict():
    comparison = compare_types(T.OBJECT, T.STRING)

    assert comparison.structural_conflict is True
    assert comparison.blocks_auto_selection is True


def test_only_incompatible_blocks_auto_selection():
    """Lossy and unknown score low but stay eligible; refusing them outright
    would leave most CSV and Postman fields permanently unmapped."""
    assert compare_types(T.STRING, T.INTEGER).blocks_auto_selection is False
    assert compare_types(T.UNKNOWN, T.STRING).blocks_auto_selection is False
    assert compare_types(T.OBJECT, T.STRING).blocks_auto_selection is True


def test_compatibility_scores_are_ordered_sensibly():
    assert (
        TypeCompatibility.EXACT.score
        > TypeCompatibility.WIDENING.score
        > TypeCompatibility.LOSSY.score
        > TypeCompatibility.UNKNOWN.score
        > TypeCompatibility.INCOMPATIBLE.score
    )


def test_every_comparison_explains_itself_without_values():
    comparison = compare_types(T.OBJECT, T.DECIMAL)

    assert comparison.explain()
    assert "object" in comparison.explain()
    assert "decimal" in comparison.explain()


def test_the_published_matrix_is_generated_from_the_implementation():
    """So documentation can never disagree with behaviour."""
    matrix = compatibility_matrix()

    assert matrix["integer"]["decimal"] == "widening"
    assert matrix["object"]["string"] == "incompatible"
    assert set(matrix) == {member.value for member in FieldDataType}


def test_the_matrix_is_total():
    """Every pair has an answer; nothing falls through to a crash."""
    matrix = compatibility_matrix()

    for source in FieldDataType:
        for target in FieldDataType:
            assert matrix[source.value][target.value]
