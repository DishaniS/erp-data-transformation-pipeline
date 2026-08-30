"""Contracts for ERP-aware adaptive response transformation.

WHERE THIS SITS IN THE GROUP ARCHITECTURE
-----------------------------------------
    Member 2's MCP layer  chooses an ERP API and EXECUTES it
              │
              ▼
    raw ERP API response
              │
              ▼
    THIS COMPONENT        adapts the response for an LLM
              │
              ▼
    downstream AI / LLM

This package begins when a response already exists. It never chooses an ERP
endpoint and never executes an ERP action; both remain Member 2's.

WHAT THE ADAPTATION IS FOR
--------------------------
A legacy ERP endpoint answering "invoice INV-204" may return seventy-four
fields wrapped in three envelopes, using vendor field names, alongside a
scanned attachment. Handing that to a model wastes context, buries the answer,
loses ERP semantics, and can expose fields nobody meant to send.

Adaptation produces the smallest faithful representation that still answers the
query, keeps the record identifiable, and says exactly what it dropped.

EVERY NUMBER HERE IS MEASURED
-----------------------------
``TransformationMetrics`` is computed from the actual payloads, never
estimated. A reduction ratio that was guessed would make the whole evaluation
worthless.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from erp_pipeline.response_adaptation.errors import AdaptationConfigurationError
from erp_pipeline.schemas.enums import SensitivityLevel

#: Version of the adaptation contracts and of the default configuration.
#: Recorded in provenance so a stored result can be traced to the behaviour
#: that produced it.
ADAPTATION_ENGINE_VERSION = "1.0"


# ============================================================
# Response classification
# ============================================================


class ResponseType(str, Enum):
    """What KIND of thing an ERP endpoint returned.

    Deliberately small. Each member is a genuinely different adaptation path,
    and a vocabulary with more members than paths would imply distinctions the
    code does not make.
    """

    STRUCTURED = "structured"
    IMAGE = "image"
    DOCUMENT = "document"
    BINARY = "binary"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class DetectionEvidence(str, Enum):
    """What the classification was actually based on.

    Reported alongside the type because "the server said so" and "the bytes
    say so" are different strengths of claim, and a mismatch between them is
    information a caller should see.
    """

    MAGIC_BYTES = "magic_bytes"
    PAYLOAD_STRUCTURE = "payload_structure"
    CONTENT_TYPE = "content_type"
    FALLBACK = "fallback"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class DetectionResult:
    """One classification, with the evidence that produced it."""

    response_type: ResponseType
    evidence: DetectionEvidence
    media_type: str | None = None
    declared_content_type: str | None = None
    #: True when the declared content type and the actual bytes disagree.
    #: Never silently resolved - the bytes win, and the disagreement is
    #: reported, exactly as file ingestion already does.
    content_type_mismatch: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "response_type": self.response_type.value,
            "evidence": self.evidence.value,
            "media_type": self.media_type,
            "declared_content_type": self.declared_content_type,
            "content_type_mismatch": self.content_type_mismatch,
            "detail": self.detail,
        }


# ============================================================
# Configuration
# ============================================================


@dataclass(frozen=True)
class RelevanceWeights:
    """How much each signal contributes to a field's relevance score.

    THE DEFAULTS, AND WHY
    ---------------------
    ``alias`` is the heaviest because it is the ERP-aware signal: it fires when
    a query word matches a canonical field's curated ERP vocabulary, so
    "total" finds ``invoice.amount`` even though the two share no characters.
    That is the mechanism this phase is contributing, so it carries the most
    weight.

    ``name`` is next: a query naming a source field literally ("approval
    status") is strong evidence, but weaker than the canonical vocabulary
    because vendor names are arbitrary.

    ``entity`` is small and corroborating. A field belonging to the entity the
    response is about is slightly more likely to be wanted, but that is true of
    every field in the response, so it must not dominate.

    ``identity`` is small here because identifiers are protected by the
    mandatory-field rule instead - scoring them highly as well would
    double-count and crowd out genuinely requested fields.

    These are defensible starting points, not tuned values. They are
    configuration precisely so an evaluation can vary them.
    """

    alias: float = 0.45
    name: float = 0.30
    entity: float = 0.15
    identity: float = 0.10

    def __post_init__(self) -> None:
        for name in ("alias", "name", "entity", "identity"):
            value = getattr(self, name)

            if not 0.0 <= value <= 1.0:
                raise AdaptationConfigurationError(
                    f"RelevanceWeights.{name} must be in [0, 1], got {value}."
                )

        if self.total <= 0:
            raise AdaptationConfigurationError(
                "RelevanceWeights must have at least one positive weight, "
                "otherwise every field scores zero and selection is arbitrary."
            )

    @property
    def total(self) -> float:
        return self.alias + self.name + self.entity + self.identity

    def as_mapping(self) -> dict[str, float]:
        return {
            "alias": self.alias,
            "name": self.name,
            "entity": self.entity,
            "identity": self.identity,
        }

    def fingerprint(self) -> str:
        return "/".join(
            f"{key}={value}" for key, value in sorted(self.as_mapping().items())
        )


DEFAULT_WEIGHTS = RelevanceWeights()


@dataclass(frozen=True)
class AdaptationPolicy:
    """What the adapted output is allowed to contain.

    Configuration, not hard-coded field names. A deployment declares which
    sensitivity levels must never leave, and which field names are withheld
    regardless of relevance; the engine has no opinion of its own about what
    "sensitive" means.

    NOTE: this phase CONSUMES an existing sensitivity classification. It does
    not infer one. Every record is whatever the canonical layer declared it to
    be, and today that is almost always ``INTERNAL``.
    """

    #: Sensitivity levels whose records are withheld entirely.
    blocked_sensitivities: frozenset[SensitivityLevel] = frozenset()
    #: Canonical or source field names withheld regardless of relevance,
    #: matched case-insensitively against both spellings.
    blocked_fields: frozenset[str] = frozenset()
    #: Response headers copied into provenance. An allow-list, never a copy of
    #: everything: `Authorization`, `Cookie` and `Set-Cookie` must never reach
    #: a stored provenance record.
    allowed_headers: frozenset[str] = frozenset(
        {"content-type", "content-length", "date", "etag", "last-modified"}
    )

    def blocks(self, sensitivity: SensitivityLevel) -> bool:
        return sensitivity in self.blocked_sensitivities

    def withholds_field(self, *names: str | None) -> bool:
        lowered = {name.lower() for name in names if name}

        return bool(lowered & {name.lower() for name in self.blocked_fields})

    def fingerprint(self) -> str:
        return (
            f"sens={sorted(s.value for s in self.blocked_sensitivities)}"
            f"/fields={sorted(n.lower() for n in self.blocked_fields)}"
        )


DEFAULT_POLICY = AdaptationPolicy()


@dataclass(frozen=True)
class AdaptationOptions:
    """Everything that can change an adaptation's output, in one versioned object."""

    weights: RelevanceWeights = DEFAULT_WEIGHTS
    policy: AdaptationPolicy = DEFAULT_POLICY

    #: Below this score a field is dropped unless it is mandatory.
    #:
    #: 0.25 is not arbitrary. A field that maps cleanly onto the queried entity
    #: but that the question never mentions scores ``entity / total`` = 0.15 on
    #: the default weights - the entity signal alone. A threshold at or below
    #: that floor would admit every well-mapped field regardless of the
    #: question, which would make query relevance decorative. The default sits
    #: above the floor so that entity membership alone is NOT enough, while any
    #: real alias or name evidence (>= 0.5 coverage, worth 0.225 on its own)
    #: clears it comfortably.
    minimum_relevance_score: float = 0.25
    #: Hard cap on selected business fields. Mandatory fields are counted
    #: against it but never dropped by it.
    max_fields: int = 24
    #: Hard cap on the serialized business payload. Character-based, not
    #: token-based, and openly so: no tokenizer dependency is added for this,
    #: and a character budget is one a reader can verify by counting.
    max_output_characters: int = 8000
    #: Cap on one string value before it is truncated with a visible marker.
    max_value_characters: int = 2000
    #: Turn relevance selection off entirely. Exists for the ablation that asks
    #: whether query relevance is what produces the reduction.
    enable_relevance_selection: bool = True
    #: Run the ERP mapping engine. Off yields a passthrough of normalized
    #: source fields - the "generic" evaluation baseline.
    enable_erp_mapping: bool = True
    #: Cap on per-field decisions kept in the report, so an explanation can
    #: never grow into a second copy of the payload.
    max_reported_fields: int = 60
    version: str = ADAPTATION_ENGINE_VERSION

    def __post_init__(self) -> None:
        if not 0.0 <= self.minimum_relevance_score <= 1.0:
            raise AdaptationConfigurationError(
                "minimum_relevance_score must be in [0, 1], got "
                f"{self.minimum_relevance_score}."
            )

        for name in ("max_fields", "max_output_characters", "max_value_characters",
                     "max_reported_fields"):
            if getattr(self, name) < 1:
                raise AdaptationConfigurationError(
                    f"AdaptationOptions.{name} must be at least 1."
                )

    def fingerprint(self) -> str:
        """Everything that could change an output, in one string."""
        return "/".join(
            (
                f"adapt@{self.version}",
                f"w({self.weights.fingerprint()})",
                f"policy({self.policy.fingerprint()})",
                f"min={self.minimum_relevance_score}",
                f"max_fields={self.max_fields}",
                f"max_chars={self.max_output_characters}",
                f"max_value={self.max_value_characters}",
                f"relevance={int(self.enable_relevance_selection)}",
                f"mapping={int(self.enable_erp_mapping)}",
            )
        )

    def without_relevance(self) -> "AdaptationOptions":
        """The ablation variant: ERP mapping, no query-relevance selection."""
        return replace(self, enable_relevance_selection=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "weights": self.weights.as_mapping(),
            "minimum_relevance_score": self.minimum_relevance_score,
            "max_fields": self.max_fields,
            "max_output_characters": self.max_output_characters,
            "max_value_characters": self.max_value_characters,
            "enable_relevance_selection": self.enable_relevance_selection,
            "enable_erp_mapping": self.enable_erp_mapping,
            "policy": {
                "blocked_sensitivities": sorted(
                    s.value for s in self.policy.blocked_sensitivities
                ),
                "blocked_fields": sorted(self.policy.blocked_fields),
            },
            "fingerprint": self.fingerprint(),
        }


DEFAULT_OPTIONS = AdaptationOptions()


# ============================================================
# Input contract
# ============================================================


@dataclass(frozen=True)
class AssetReference:
    """A URL an ERP response pointed at, rather than inlined.

    Kept as a declared structure rather than a bare string so the resolver has
    the declared type available as evidence - and so a caller cannot smuggle a
    URL into a field the engine would follow by accident.
    """

    url: str
    #: What the ERP claims is at that URL. A claim, verified against the
    #: fetched bytes before it is believed.
    declared_content_type: str | None = None
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "declared_content_type": self.declared_content_type,
            "label": self.label,
        }


@dataclass(frozen=True)
class ResponseEnvelope:
    """One already-executed ERP API response, plus the question it answers.

    ``query`` is what makes the adaptation ADAPTIVE. Without it the engine can
    still canonicalize and bound the payload, but it cannot decide which of
    seventy-four fields the caller actually wanted, and it says so rather than
    guessing.
    """

    #: The natural-language question the response is meant to answer.
    query: str | None = None
    source_system_id: str = "unknown_erp"
    endpoint: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    #: A decoded JSON body: object or array.
    body: Any = None
    #: Raw bytes, for image/PDF/binary responses.
    raw: bytes | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    #: URLs the response referred to. Resolved only when policy permits.
    asset_urls: Sequence[AssetReference] = ()
    #: The entity the caller was asking about, when known. A hint, never an
    #: override: the mapping engine still decides what the payload IS.
    entity_hint: str | None = None
    #: Sensitivity declared by the caller for this response. CONSUMED, never
    #: inferred - see AdaptationPolicy.
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL

    def __post_init__(self) -> None:
        if self.body is None and self.raw is None and not self.asset_urls:
            raise AdaptationConfigurationError(
                "a ResponseEnvelope must carry a body, raw bytes, or at least "
                "one asset reference; there is nothing to adapt otherwise."
            )

    @property
    def has_structured_body(self) -> bool:
        return isinstance(self.body, (Mapping, list, tuple))


# ============================================================
# Relevance evidence
# ============================================================


@dataclass(frozen=True)
class FieldRelevance:
    """One field's relevance decision, with the evidence behind it.

    Every selected AND rejected field can explain itself. A selection
    mechanism that cannot say why it dropped the field a user asked about is
    not auditable, and this phase's whole claim is that it drops the right
    things.
    """

    source_field: str
    canonical_target: str | None
    score: float
    signals: Mapping[str, float]
    selected: bool
    reason: str
    mandatory: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.source_field,
            "canonical_target": self.canonical_target,
            "score": round(self.score, 6),
            "signals": {k: round(v, 6) for k, v in sorted(self.signals.items())},
            "selected": self.selected,
            "mandatory": self.mandatory,
            "reason": self.reason,
        }


# ============================================================
# Assets
# ============================================================


class AssetKind(str, Enum):
    IMAGE = "image"
    DOCUMENT = "document"
    UNSUPPORTED_BINARY = "unsupported_binary"
    REFUSED = "refused"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class AdaptedAsset:
    """One non-structured part of a response, in an LLM-consumable shape.

    ``llm_directly_readable`` is the field a downstream caller actually acts
    on: it says whether the model can be handed this content, or whether only
    the extracted text and metadata are usable. Raw bytes are NEVER placed in
    this contract.
    """

    kind: AssetKind
    mime_type: str | None = None
    size_bytes: int | None = None
    content_hash: str | None = None
    llm_directly_readable: bool = False
    #: Images.
    width: int | None = None
    height: int | None = None
    #: Text recovered from an image or document.
    text: str | None = None
    ocr_used: bool = False
    #: Documents.
    page_count: int | None = None
    page_range: tuple[int, int] | None = None
    extraction_status: str | None = None
    source_url: str | None = None
    label: str | None = None
    warnings: tuple[str, ...] = ()
    truncated: bool = False

    def to_dict(self, include_text: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.kind.value,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "llm_directly_readable": self.llm_directly_readable,
        }

        for key, value in (
            ("width", self.width),
            ("height", self.height),
            ("page_count", self.page_count),
            ("extraction_status", self.extraction_status),
            ("source_url", self.source_url),
            ("label", self.label),
        ):
            if value is not None:
                payload[key] = value

        if self.page_range is not None:
            payload["page_start"], payload["page_end"] = self.page_range

        if self.ocr_used:
            payload["ocr_used"] = True

        if include_text and self.text is not None:
            payload["text"] = self.text

        if self.truncated:
            payload["truncated"] = True

        if self.warnings:
            payload["warnings"] = list(self.warnings)

        return payload


# ============================================================
# Metrics, provenance, report
# ============================================================


def serialized_size(payload: Any) -> int:
    """Byte length of a payload in one canonical JSON encoding.

    One encoding for every measurement, so an input and an output size are
    comparable. ``default=str`` keeps a datetime or Decimal from making the
    measurement itself fail.
    """
    if payload is None:
        return 0

    if isinstance(payload, (bytes, bytearray)):
        return len(payload)

    return len(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")
    )


@dataclass(frozen=True)
class TransformationMetrics:
    """What the adaptation actually cost and actually saved.

    Every value is MEASURED from the real payloads. Ratios are derived, never
    supplied, so a caller cannot report a reduction that did not happen.
    """

    input_bytes: int = 0
    output_bytes: int = 0
    input_fields: int = 0
    selected_fields: int = 0
    processing_ms: float = 0.0
    truncated: bool = False

    @property
    def field_reduction_ratio(self) -> float:
        if self.input_fields <= 0:
            return 0.0

        return round(1.0 - (self.selected_fields / self.input_fields), 6)

    @property
    def size_reduction_ratio(self) -> float:
        if self.input_bytes <= 0:
            return 0.0

        return round(1.0 - (self.output_bytes / self.input_bytes), 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "input_fields": self.input_fields,
            "selected_fields": self.selected_fields,
            "field_reduction_ratio": self.field_reduction_ratio,
            "size_reduction_ratio": self.size_reduction_ratio,
            "processing_ms": round(self.processing_ms, 3),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class AdaptationProvenance:
    """Where an adapted response came from, and what produced it.

    Answers, without a second lookup: which ERP, which endpoint, what content
    type, when, and under which configuration. Headers are ALLOW-LISTED by
    policy - copying them wholesale would put an ``Authorization`` value into
    a record that may be stored or logged.
    """

    source_system_id: str
    endpoint: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    adapted_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    engine_version: str = ADAPTATION_ENGINE_VERSION
    config_fingerprint: str | None = None
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    headers: Mapping[str, str] = field(default_factory=dict)
    #: Set when the mapping engine produced a canonical record for this
    #: response, so a hit can be joined to the rest of the pipeline.
    canonical_record_id: str | None = None
    source_entity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_system_id": self.source_system_id,
            "endpoint": self.endpoint,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "adapted_at": self.adapted_at.isoformat(),
            "engine_version": self.engine_version,
            "config_fingerprint": self.config_fingerprint,
            "sensitivity": self.sensitivity.value,
            "canonical_record_id": self.canonical_record_id,
            "source_entity": self.source_entity,
            "headers": dict(self.headers),
        }


@dataclass(frozen=True)
class AdaptationReport:
    """The explanation. Bounded, and never a second copy of the payload."""

    detection: DetectionResult
    detected_entity: str | None = None
    entity_confidence: float | None = None
    input_field_count: int = 0
    selected_field_count: int = 0
    field_decisions: tuple[FieldRelevance, ...] = ()
    #: ``reason category -> count`` over every dropped field, so a caller sees
    #: the shape of what was removed without the report listing all of it.
    removed_by_reason: Mapping[str, int] = field(default_factory=dict)
    wrapper_path: tuple[str, ...] = ()
    decisions_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "detection": self.detection.to_dict(),
            "detected_entity": self.detected_entity,
            "entity_confidence": (
                round(self.entity_confidence, 6)
                if self.entity_confidence is not None
                else None
            ),
            "input_field_count": self.input_field_count,
            "selected_field_count": self.selected_field_count,
            "wrapper_path": list(self.wrapper_path),
            "removed_by_reason": dict(sorted(self.removed_by_reason.items())),
            "decisions_truncated": self.decisions_truncated,
            "field_decisions": [d.to_dict() for d in self.field_decisions],
        }


# ============================================================
# Output contract
# ============================================================


@dataclass(frozen=True)
class AdaptedResponse:
    """The LLM-ready result.

    ``llm_ready`` is the business payload and nothing else. Identity,
    provenance, metrics and explanation live in their own blocks, so a caller
    can hand ``llm_ready`` to a model without also handing it pipeline
    bookkeeping.
    """

    response_type: ResponseType
    entity_type: str | None = None
    llm_ready: Mapping[str, Any] = field(default_factory=dict)
    assets: tuple[AdaptedAsset, ...] = ()
    provenance: AdaptationProvenance | None = None
    transformation: TransformationMetrics = field(
        default_factory=TransformationMetrics
    )
    report: AdaptationReport | None = None
    #: Non-fatal problems: a refused URL, an unavailable OCR engine, a
    #: truncated value. Their presence does not make the result unsuccessful.
    warnings: tuple[str, ...] = ()
    #: False only when nothing usable could be produced at all.
    success: bool = True

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    @property
    def is_partial(self) -> bool:
        """Succeeded, but something in it did not."""
        return self.success and bool(self.warnings)

    def to_dict(self, include_report: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "response_type": self.response_type.value,
            "entity_type": self.entity_type,
            "llm_ready": dict(self.llm_ready),
            "assets": [asset.to_dict() for asset in self.assets],
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "transformation": self.transformation.to_dict(),
            "warnings": list(self.warnings),
            "success": self.success,
        }

        if include_report and self.report is not None:
            payload["report"] = self.report.to_dict()

        return payload


__all__ = [
    "ADAPTATION_ENGINE_VERSION",
    "ResponseType",
    "DetectionEvidence",
    "DetectionResult",
    "RelevanceWeights",
    "DEFAULT_WEIGHTS",
    "AdaptationPolicy",
    "DEFAULT_POLICY",
    "AdaptationOptions",
    "DEFAULT_OPTIONS",
    "AssetReference",
    "ResponseEnvelope",
    "FieldRelevance",
    "AssetKind",
    "AdaptedAsset",
    "TransformationMetrics",
    "AdaptationProvenance",
    "AdaptationReport",
    "AdaptedResponse",
    "serialized_size",
]
