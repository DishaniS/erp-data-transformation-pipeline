"""Phase 1 FIX 8 - alphanumeric identifiers must survive normalization.

THE DEFECT
----------
``split_tokens`` broke every letter/digit boundary, so an ERP identifier like
``E002`` became ``("e", "002")``. The synonym table then folded the stranded
single letter ``e`` onto ``email`` - an entry that exists so ``e_mail`` splits
and folds correctly - and an employee code silently acquired an ``email`` token
it never contained. Any field named ``email`` then scored a false match against
a query naming that employee.

WHY THE SYNONYM WAS NOT DELETED
-------------------------------
``e -> email`` is CORRECT for ``e_mail`` and for the several real ERP spellings
that depend on it. The bug was never the synonym; it was manufacturing a
one-letter "word" out of an identifier that contains no words. So the split is
refused instead, and the synonym table is untouched.

WHAT DELIBERATELY DID NOT CHANGE
--------------------------------
Prefixes of two letters or more still split, so ``inv204`` still reaches
``invoice`` + ``204`` and ``line1`` still reaches ``line`` + ``1``. Those are
real abbreviations and real words; a single letter glued to digits is neither.
"""

from __future__ import annotations

import pytest

from erp_pipeline.mapping.normalization import (
    DEFAULT_SYNONYMS,
    canonical_tokens,
    split_tokens,
)


# ----------------------------------------------------------------------
# Identifiers keep their identity
# ----------------------------------------------------------------------


@pytest.mark.parametrize("identifier", ["E002", "EMP002", "INV204", "PO1007", "CUS17"])
def test_an_identifier_never_acquires_an_email_token(identifier):
    """The headline regression: no ERP identifier may imply "email"."""
    assert "email" not in canonical_tokens(identifier)


@pytest.mark.parametrize(
    "identifier, expected",
    [
        ("E002", ("e002",)),
        ("A1", ("a1",)),
        ("X9", ("x9",)),
    ],
)
def test_a_single_letter_prefix_stays_glued_to_its_digits(identifier, expected):
    """One letter plus digits is an identifier, not a word plus a number."""
    assert split_tokens(identifier) == expected


def test_e002_no_longer_becomes_email():
    """The exact reported defect, pinned by value."""
    assert canonical_tokens("E002") == ("e002",)
    # ...and the synonym that caused it is still present and untouched.
    assert DEFAULT_SYNONYMS["e"] == "email"


@pytest.mark.parametrize(
    "identifier",
    ["E002", "EMP002", "INV204", "PO1007", "CUS17", "e002", "emp002"],
)
def test_an_identifier_produces_at_least_one_token(identifier):
    """Identity must never be normalized away entirely.

    ``A1`` used to reduce to ``("1",)`` because the stranded ``a`` was dropped
    as a noise token, discarding the alphabetic half of the identifier.
    """
    assert split_tokens(identifier)
    assert canonical_tokens(identifier)


def test_a_single_letter_identifier_is_not_dropped_as_noise():
    assert canonical_tokens("A1") == ("a1",)


# ----------------------------------------------------------------------
# Ordinary language behaviour is preserved
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("email", ("email",)),
        ("e_mail", ("email", "email")),
        ("email_address", ("email", "address")),
        ("customer email", ("customer", "email")),
        ("contact_email", ("contact", "email")),
    ],
)
def test_email_vocabulary_still_resolves(name, expected):
    assert canonical_tokens(name) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("line1", ("line", "1")),
        ("address_line_1", ("address", "line", "1")),
        ("customer_id", ("customer", "id")),
        ("totalAmount", ("total", "amount")),
    ],
)
def test_multi_letter_prefixes_still_split(name, expected):
    """Two or more letters before a digit is a word, and still splits."""
    assert canonical_tokens(name) == expected


@pytest.mark.parametrize(
    "name, expected_token",
    [
        ("INV204", "invoice"),
        ("INV-204", "invoice"),
        ("inv_no", "invoice"),
        ("PO1007", "purchase"),
    ],
)
def test_real_abbreviations_still_expand(name, expected_token):
    """``inv`` and ``po`` ARE words ERP systems abbreviate, unlike ``e``.

    Phase 14's relevance scoring depends on this expansion, so the fix must not
    disturb it.
    """
    assert expected_token in canonical_tokens(name)


# ----------------------------------------------------------------------
# The fix is a tokenizer correctness change, not benchmark tuning
# ----------------------------------------------------------------------


def test_the_synonym_and_abbreviation_tables_are_unmodified():
    """Phase 1 changed WHERE a name is split, never WHAT a token means.

    Guards against the temptation to make an evaluation look better by editing
    the vocabulary, which would invalidate every published mapping result.
    """
    from erp_pipeline.mapping.normalization import DEFAULT_ABBREVIATIONS

    assert DEFAULT_SYNONYMS["e"] == "email"
    assert DEFAULT_SYNONYMS["client"] == "customer"
    assert DEFAULT_ABBREVIATIONS["inv"] == "invoice"
    assert DEFAULT_ABBREVIATIONS["cust"] == "customer"
    assert DEFAULT_ABBREVIATIONS["po"] == "purchase_order"


def test_a_query_naming_an_employee_no_longer_matches_an_email_field():
    """The end-to-end consequence, expressed the way it was discovered.

    A Phase 14 query mentioning ``E002`` selected a customer's email field at a
    high score despite never mentioning email.
    """
    from erp_pipeline.response_adaptation.relevance import query_tokens

    tokens = query_tokens("Who is customer E002?")

    assert "email" not in tokens
    assert "customer" in tokens
