"""Classify an ERP document into a business document type.

WHAT THIS ADDS
--------------
The rest of the ingestion package answers *what is in this file* - pages, text,
provenance, OCR state. It deliberately does not answer *what kind of business
document is this*, and a downstream consumer needs that: a governance model
retrieving policy clauses must not be handed a scanned receipt, and a retrieval
filter on ``document_type`` needs something to filter on.

HOW IT DECIDES
--------------
Weighted keyword evidence over the filename and, optionally, a bounded prefix
of the extracted text. Filename evidence is weighted higher than body evidence
because an ERP export names its files deliberately, while the word "invoice"
appearing once in a policy document means very little.

Deliberately NOT a machine-learning classifier. A rule set a reader can inspect
and a deployment can extend is worth more here than an opaque model trained on
data this project does not have, and the confidence it reports is an evidence
ratio a reader can verify by counting rather than a calibrated probability.

CONFIGURABLE, WITH GENERIC DEFAULTS
-----------------------------------
The default rules use ordinary ERP business vocabulary - invoice, purchase
order, policy, contract - and no dataset-specific terms. A deployment whose
documents use different words supplies its own ``ClassificationRule`` set; no
code change is needed, and no dataset knowledge lives in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from erp_pipeline.ingestion.models import FileType

#: Version of the classification contract and of the default rule set.
CLASSIFIER_VERSION = "1.0"

#: Returned when no rule matches. An explicit "unclassified" is far better than
#: guessing the most common type, because a wrong label is worse than no label.
UNCLASSIFIED = "unclassified_document"

#: How much of the document body is examined. Business documents declare what
#: they are in their first page; scanning further mostly adds noise and cost.
DEFAULT_BODY_SCAN_CHARS = 2000

#: Filename evidence counts for this much more than body evidence.
FILENAME_WEIGHT = 3.0
BODY_WEIGHT = 1.0


@dataclass(frozen=True)
class ClassificationRule:
    """One document type and the vocabulary that indicates it."""

    document_type: str
    #: Keywords matched as whole words, case-insensitively.
    keywords: tuple[str, ...]
    #: Keywords that, if present, rule this type OUT. Cheap way to stop
    #: "purchase order policy" being classified as a purchase order.
    negative_keywords: tuple[str, ...] = ()
    #: Relative importance when two rules both match.
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not self.document_type:
            raise ValueError("ClassificationRule.document_type must not be empty")
        if not self.keywords:
            raise ValueError(
                f"ClassificationRule {self.document_type!r} has no keywords, so "
                "it can never match"
            )
        if self.weight <= 0:
            raise ValueError("ClassificationRule.weight must be positive")


#: Generic ERP document vocabulary. No dataset-specific terms appear here.
DEFAULT_RULES: tuple[ClassificationRule, ...] = (
    ClassificationRule(
        document_type="policy_document",
        keywords=(
            "policy", "policies", "procedure", "procedures", "guideline",
            "guidelines", "regulation", "compliance", "standard",
        ),
        # A policy ABOUT invoices is still a policy, so policy wins ties.
        weight=1.2,
    ),
    ClassificationRule(
        document_type="invoice",
        keywords=("invoice", "bill", "billing", "vat", "tax invoice"),
        negative_keywords=("policy", "procedure", "guideline"),
    ),
    ClassificationRule(
        document_type="receipt",
        keywords=("receipt", "proof of payment", "paid"),
        negative_keywords=("policy", "procedure"),
    ),
    ClassificationRule(
        document_type="purchase_order",
        keywords=("purchase order", "purchase-order", "po number", "order form"),
        negative_keywords=("policy", "procedure"),
    ),
    ClassificationRule(
        document_type="approval_form",
        keywords=("approval", "authorisation", "authorization", "sign-off", "form"),
        negative_keywords=("policy", "procedure"),
    ),
    ClassificationRule(
        document_type="contract",
        keywords=("contract", "agreement", "terms and conditions", "sla"),
    ),
    ClassificationRule(
        document_type="statement",
        keywords=("statement", "remittance", "reconciliation", "ledger"),
    ),
    ClassificationRule(
        document_type="claim",
        # "declaration" is deliberately absent. It is a plausible generic
        # keyword, but it is also the vocabulary of one particular research
        # dataset, and dataset vocabulary does not belong in the core defaults.
        # A deployment whose documents use it supplies its own rule set.
        keywords=("claim", "reimbursement", "expense report"),
        negative_keywords=("policy", "procedure"),
    ),
    ClassificationRule(
        document_type="manual",
        keywords=("manual", "handbook", "user guide", "instructions"),
    ),
)


@dataclass(frozen=True)
class ClassificationConfig:
    """How classification behaves for one deployment."""

    rules: tuple[ClassificationRule, ...] = DEFAULT_RULES
    #: Also scan a bounded prefix of the extracted text, not only the filename.
    use_body_text: bool = True
    body_scan_chars: int = DEFAULT_BODY_SCAN_CHARS
    #: Minimum score before a type is asserted at all.
    minimum_score: float = 1.0
    #: Fallback naming when nothing matches: derive a type from the file type
    #: (``pdf_document``, ``scanned_image_document``) instead of UNCLASSIFIED.
    fall_back_to_file_type: bool = True
    version: str = CLASSIFIER_VERSION

    def __post_init__(self) -> None:
        if self.body_scan_chars < 0:
            raise ValueError("body_scan_chars must not be negative")
        if self.minimum_score < 0:
            raise ValueError("minimum_score must not be negative")

    def fingerprint(self) -> str:
        """Folded into the result so a reclassification is visible."""
        return (
            f"docclass@{self.version}/rules={len(self.rules)}"
            f"/body={int(self.use_body_text)}/scan={self.body_scan_chars}"
            f"/min={self.minimum_score}"
        )


DEFAULT_CONFIG = ClassificationConfig()


@dataclass(frozen=True)
class ClassificationResult:
    """What the classifier decided, and on what evidence."""

    document_type: str
    score: float
    #: ``score / total score across all matching rules``. An evidence ratio,
    #: not a calibrated probability, and named ``confidence`` only because that
    #: is what a consumer will look for.
    confidence: float
    #: Which keywords fired, so a reader can audit the decision.
    matched_keywords: tuple[str, ...] = ()
    #: Runner-up type and score, when one exists. Surfaced rather than
    #: discarded, because a close second is exactly when a human should look.
    runner_up: str | None = None
    runner_up_score: float = 0.0
    config_fingerprint: str = ""

    @property
    def is_confident(self) -> bool:
        """Whether the winner clearly beat the runner-up."""
        return self.document_type != UNCLASSIFIED and self.confidence >= 0.6

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_type": self.document_type,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "matched_keywords": list(self.matched_keywords),
            "runner_up": self.runner_up,
            "runner_up_score": round(self.runner_up_score, 4),
            "is_confident": self.is_confident,
            "classifier": self.config_fingerprint,
        }


def _normalize(text: str) -> str:
    """Lower-case and collapse separators so ``invoice_2026`` matches."""
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def _count_keyword(haystack: str, keyword: str) -> int:
    """Whole-word occurrences of ``keyword`` in an already-normalized string.

    Whole-word so ``bill`` does not match ``billable`` and ``po`` does not
    match ``policy`` - the substring version of this check produced exactly
    those false positives.
    """
    pattern = r"\b" + re.escape(_normalize(keyword)) + r"\b"

    return len(re.findall(pattern, haystack))


def classify_document(
    filename: str,
    text: str | None = None,
    file_type: FileType | None = None,
    config: ClassificationConfig | None = None,
) -> ClassificationResult:
    """Classify one document from its name and, optionally, its content."""
    config = config or DEFAULT_CONFIG

    name_text = _normalize(filename or "")
    body_text = ""

    if config.use_body_text and text:
        body_text = _normalize(text[: config.body_scan_chars])

    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}

    for rule in config.rules:
        if any(_count_keyword(name_text, word) for word in rule.negative_keywords):
            continue
        if any(_count_keyword(body_text, word) for word in rule.negative_keywords):
            continue

        score = 0.0
        hits: list[str] = []

        for keyword in rule.keywords:
            in_name = _count_keyword(name_text, keyword)
            in_body = _count_keyword(body_text, keyword)

            if in_name:
                score += FILENAME_WEIGHT * rule.weight
                hits.append(keyword)
            if in_body:
                # Capped at one body hit per keyword: a policy that says
                # "policy" forty times is not forty times more a policy.
                score += BODY_WEIGHT * rule.weight
                if keyword not in hits:
                    hits.append(keyword)

        if score > 0:
            scores[rule.document_type] = score
            matched[rule.document_type] = hits

    if not scores:
        return ClassificationResult(
            document_type=_fallback_type(file_type, config),
            score=0.0,
            confidence=0.0,
            config_fingerprint=config.fingerprint(),
        )

    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    best_type, best_score = ranked[0]
    runner_up, runner_up_score = ranked[1] if len(ranked) > 1 else (None, 0.0)

    if best_score < config.minimum_score:
        return ClassificationResult(
            document_type=_fallback_type(file_type, config),
            score=best_score,
            confidence=0.0,
            matched_keywords=tuple(matched.get(best_type, ())),
            runner_up=best_type,
            runner_up_score=best_score,
            config_fingerprint=config.fingerprint(),
        )

    total = sum(scores.values())

    return ClassificationResult(
        document_type=best_type,
        score=best_score,
        confidence=best_score / total if total else 0.0,
        matched_keywords=tuple(sorted(matched.get(best_type, ()))),
        runner_up=runner_up,
        runner_up_score=runner_up_score,
        config_fingerprint=config.fingerprint(),
    )


def _fallback_type(
    file_type: FileType | None, config: ClassificationConfig
) -> str:
    """What to call a document no rule recognized."""
    if not config.fall_back_to_file_type or file_type is None:
        return UNCLASSIFIED

    if file_type is FileType.PDF:
        return "pdf_document"

    if file_type is FileType.IMAGE:
        return "scanned_image_document"

    return UNCLASSIFIED


def classify_extracted_document(
    document: Any, config: ClassificationConfig | None = None
) -> ClassificationResult:
    """Classify a document produced by file ingestion.

    Accepts either a ``DocumentFileResult`` (what ``FileIngestionService``
    returns) or the ``ExtractedDocument`` inside it, because a caller
    reasonably holds one or the other and should not have to know which
    attribute chain leads to the filename.
    """
    extracted = getattr(document, "document", None) or document
    source = getattr(document, "file", None) or getattr(extracted, "file", None)

    filename = (
        getattr(source, "original_filename", None)
        or getattr(source, "filename", None)
        or ""
    )
    file_type = getattr(document, "file_type", None) or getattr(
        source, "file_type", None
    )

    text = None

    if getattr(extracted, "has_text", False):
        text = extracted.document_text

    return classify_document(
        filename=filename, text=text, file_type=file_type, config=config
    )


__all__ = [
    "CLASSIFIER_VERSION",
    "UNCLASSIFIED",
    "DEFAULT_BODY_SCAN_CHARS",
    "FILENAME_WEIGHT",
    "BODY_WEIGHT",
    "ClassificationRule",
    "ClassificationConfig",
    "ClassificationResult",
    "DEFAULT_RULES",
    "DEFAULT_CONFIG",
    "classify_document",
    "classify_extracted_document",
]
