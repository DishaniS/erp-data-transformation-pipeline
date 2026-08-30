"""Generic ERP document classification.

Replaces the prototype's ``infer_document_type``, which was a fixed if-chain of
dataset keywords. The rules are now data, the evidence is reported, and the
default vocabulary contains no dataset-specific terms.
"""

from __future__ import annotations

import pathlib

import pytest

from erp_pipeline.ingestion.document_classification import (
    DEFAULT_RULES,
    UNCLASSIFIED,
    ClassificationConfig,
    ClassificationRule,
    classify_document,
    classify_extracted_document,
)
from erp_pipeline.ingestion.models import FileType


# ============================================================
# Filename evidence
# ============================================================


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("travel_claim_policy.pdf", "policy_document"),
        ("finance_reimbursement_policy.pdf", "policy_document"),
        ("procurement_procedure.pdf", "policy_document"),
        ("supplier_invoice_4471.pdf", "invoice"),
        ("payment_receipt.png", "receipt"),
        ("purchase_order_88213.pdf", "purchase_order"),
        ("approval_form_scan.png", "approval_form"),
        ("master_service_agreement.pdf", "contract"),
        ("supplier_statement_march.pdf", "statement"),
        ("expense_reimbursement.pdf", "claim"),
        ("user_manual.pdf", "manual"),
    ],
)
def test_common_erp_document_names_are_classified(filename, expected):
    assert classify_document(filename).document_type == expected


def test_separators_do_not_hide_a_keyword():
    """``invoice_2026`` and ``invoice-2026`` must classify identically."""
    left = classify_document("invoice_2026.pdf").document_type
    right = classify_document("invoice-2026.pdf").document_type

    assert left == right == "invoice"


def test_classification_is_case_insensitive():
    assert (
        classify_document("INVOICE.PDF").document_type
        == classify_document("invoice.pdf").document_type
    )


# ============================================================
# Negative keywords
# ============================================================


def test_a_policy_about_invoices_is_still_a_policy():
    """The exact false positive a plain keyword search produces."""
    result = classify_document("invoice_approval_policy.pdf")

    assert result.document_type == "policy_document"


def test_a_procedure_about_purchase_orders_is_still_a_procedure():
    assert (
        classify_document("purchase_order_procedure.pdf").document_type
        == "policy_document"
    )


# ============================================================
# Whole-word matching
# ============================================================


def test_a_keyword_is_not_matched_inside_a_longer_word():
    """``bill`` must not match ``billable``, and ``po`` must not match
    ``policy`` - both were real false positives from substring matching."""
    result = classify_document("billable_hours_summary.pdf")

    assert result.document_type != "invoice"


# ============================================================
# Body text
# ============================================================


def test_body_text_classifies_a_document_whose_name_says_nothing():
    result = classify_document(
        "scan_0001.pdf",
        text="Supplier Invoice\nVAT 21%\nTotal amount due",
        file_type=FileType.PDF,
    )

    assert result.document_type == "invoice"


def test_the_filename_outweighs_the_body():
    """An ERP export names its files deliberately; one word in a body does not
    outrank that."""
    result = classify_document(
        "expense_policy.pdf",
        text="This document explains how to submit an invoice.",
    )

    assert result.document_type == "policy_document"


def test_body_scanning_can_be_switched_off():
    config = ClassificationConfig(use_body_text=False)

    result = classify_document(
        "scan_0001.pdf",
        text="Supplier Invoice",
        file_type=FileType.PDF,
        config=config,
    )

    assert result.document_type == "pdf_document"


def test_only_a_bounded_prefix_of_the_body_is_scanned():
    config = ClassificationConfig(body_scan_chars=20)

    result = classify_document(
        "scan.pdf", text=("x" * 500) + " invoice", file_type=FileType.PDF, config=config
    )

    assert result.document_type == "pdf_document"


# ============================================================
# Fallbacks
# ============================================================


def test_an_unrecognized_pdf_falls_back_to_its_file_type():
    result = classify_document("export_2026_q1.pdf", file_type=FileType.PDF)

    assert result.document_type == "pdf_document"
    assert result.confidence == 0.0


def test_an_unrecognized_image_falls_back_to_its_file_type():
    assert (
        classify_document("img_0042.png", file_type=FileType.IMAGE).document_type
        == "scanned_image_document"
    )


def test_the_fallback_can_be_disabled():
    config = ClassificationConfig(fall_back_to_file_type=False)

    result = classify_document("export.pdf", file_type=FileType.PDF, config=config)

    assert result.document_type == UNCLASSIFIED


def test_an_unrecognized_document_is_never_guessed_into_a_business_type():
    """A wrong label is worse than no label."""
    result = classify_document("zzz.bin")

    assert result.document_type == UNCLASSIFIED


# ============================================================
# Evidence reporting
# ============================================================


def test_the_matched_keywords_are_reported_so_a_decision_is_auditable():
    result = classify_document("supplier_invoice.pdf")

    assert "invoice" in result.matched_keywords


def test_a_close_second_is_surfaced_rather_than_discarded():
    """A near tie is exactly when a human should look."""
    result = classify_document("invoice_travel_claim_001.png")

    assert result.runner_up is not None
    assert result.is_confident is False


def test_a_clear_single_match_is_confident():
    assert classify_document("travel_claim_policy.pdf").is_confident is True


def test_the_result_serializes_with_its_evidence():
    payload = classify_document("supplier_invoice.pdf").to_dict()

    assert payload["document_type"] == "invoice"
    assert "matched_keywords" in payload
    assert "classifier" in payload


def test_the_configuration_fingerprint_changes_with_the_rules():
    custom = ClassificationConfig(rules=DEFAULT_RULES[:2])

    assert custom.fingerprint() != ClassificationConfig().fingerprint()


# ============================================================
# Custom rules
# ============================================================


def test_a_deployment_can_supply_its_own_vocabulary():
    """A new document vocabulary is configuration, never a code change."""
    config = ClassificationConfig(
        rules=(
            ClassificationRule(
                document_type="werkbon", keywords=("werkbon", "opdracht")
            ),
        )
    )

    assert classify_document("werkbon_881.pdf", config=config).document_type == (
        "werkbon"
    )


def test_a_rule_without_keywords_is_refused():
    with pytest.raises(ValueError):
        ClassificationRule(document_type="x", keywords=())


def test_a_rule_without_a_type_is_refused():
    with pytest.raises(ValueError):
        ClassificationRule(document_type="", keywords=("x",))


def test_rule_weight_breaks_a_tie():
    config = ClassificationConfig(
        rules=(
            ClassificationRule(document_type="light", keywords=("shared",), weight=1.0),
            ClassificationRule(document_type="heavy", keywords=("shared",), weight=2.0),
        )
    )

    assert classify_document("shared.pdf", config=config).document_type == "heavy"


# ============================================================
# Integration with ingestion results
# ============================================================


def test_an_extracted_document_can_be_classified_directly():
    class FakeSource:
        filename = "reimbursement_policy.pdf"
        file_type = FileType.PDF

    class FakeDocument:
        source = FakeSource()
        has_text = True
        document_text = "Reimbursement policy for staff travel."

    assert (
        classify_extracted_document(FakeDocument()).document_type
        == "policy_document"
    )


def test_the_default_rules_contain_no_dataset_specific_vocabulary():
    """The whole reason this replaced the prototype's if-chain."""
    forbidden = {"bpi", "bpi2020", "travel_permit", "permit log", "declaration"}

    for rule in DEFAULT_RULES:
        for keyword in rule.keywords + rule.negative_keywords:
            assert keyword.lower() not in forbidden, keyword


def test_the_module_never_imports_a_dataset_module():
    module = (
        pathlib.Path(__file__).resolve().parents[3]
        / "src"
        / "erp_pipeline"
        / "ingestion"
        / "document_classification.py"
    )
    source = module.read_text(encoding="utf-8")

    assert "bpi2020" not in source
    assert "erp_integrations" not in source
