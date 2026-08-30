"""The one entry point: a raw ERP response in, an LLM-ready context out.

WHERE THIS SITS
---------------
    Member 2   decides which ERP API to call, and calls it
    -------------------------------------------------------------
    Member 4   receives what came back, and makes it usable   <- here
    -------------------------------------------------------------
    Member 3   puts the result in front of a language model

This service never chooses an endpoint, never issues an ERP request and never
retries one. It is handed a response that has already happened.

THE PIPELINE
------------
    detect -> unwrap -> infer schema -> map -> transform
           -> score against the query -> apply budgets -> measure

Only the middle three steps are pre-existing pipeline machinery, and that is
the point: an API response is absorbed by the SAME ERP mapping engine that
absorbs a CSV or a MongoDB collection, rather than by a parallel one written
for HTTP.

PARTIAL SUCCESS IS THE NORMAL CASE
----------------------------------
A response can carry perfectly good JSON and an image URL that policy refuses
to fetch. Discarding the JSON over the image would be the wrong trade every
time, so asset failures become warnings on a successful result. ``success`` is
false only when nothing usable could be produced at all.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Mapping, Sequence

from erp_pipeline.response_adaptation.assets import (
    AssetAdapter,
    AssetOptions,
    Fetcher,
    Resolver,
    refused_asset,
)
from erp_pipeline.response_adaptation.detector import detect_response_type
from erp_pipeline.response_adaptation.errors import (
    AssetError,
    MalformedResponseError,
    MappingUnavailableError,
)
from erp_pipeline.response_adaptation.formatter import (
    apply_budget_to_decisions,
    build_payload,
    limit_decisions,
)
from erp_pipeline.response_adaptation.models import (
    AdaptationOptions,
    AdaptationProvenance,
    AdaptationReport,
    AdaptedAsset,
    AdaptedResponse,
    DetectionResult,
    FieldRelevance,
    ResponseEnvelope,
    ResponseType,
    TransformationMetrics,
    serialized_size,
)
from erp_pipeline.response_adaptation.relevance import RelevanceScorer, removal_summary
from erp_pipeline.response_adaptation.structured import (
    StructuredResponseAdapter,
    count_leaf_fields,
    flatten_record,
    infer_response_schema,
    unwrap_payload,
)


def _allowed_headers(
    headers: Mapping[str, str], allowed: frozenset[str]
) -> dict[str, str]:
    """Keep only the headers policy names, matched case-insensitively.

    An ALLOW-list, never a deny-list. A deny-list has to anticipate every header
    that might carry a secret, and gets it wrong the first time an ERP invents
    ``X-Vendor-Session``. Provenance is stored and logged, so anything that
    reaches it must have been chosen deliberately.
    """
    lowered = {name.lower() for name in allowed}

    return {
        name: value
        for name, value in sorted(headers.items())
        if name.lower() in lowered
    }


class ResponseAdaptationService:
    """Adapts one ERP response at a time. Stateless and reusable."""

    def __init__(
        self,
        options: AdaptationOptions | None = None,
        asset_options: AssetOptions | None = None,
        scorer: RelevanceScorer | None = None,
        structured: StructuredResponseAdapter | None = None,
        fetcher: Fetcher | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.options = options or AdaptationOptions()
        self.assets = AssetAdapter(asset_options)
        self.scorer = scorer or RelevanceScorer(self.options.weights)
        self.structured = structured or StructuredResponseAdapter()
        #: Supplied by the deployment. Absent means no URL is ever fetched -
        #: see ``assets`` for why that is the default rather than an oversight.
        self.fetcher = fetcher
        self.resolver = resolver

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def adapt(
        self, envelope: ResponseEnvelope, options: AdaptationOptions | None = None
    ) -> AdaptedResponse:
        """Adapt one response.

        ``options`` overrides the service default for this call only, which is
        what lets the ablation run both configurations through one instance
        without rebuilding the scorer between cases.
        """
        active = options or self.options
        scorer = (
            self.scorer
            if active.weights == self.options.weights
            else RelevanceScorer(active.weights)
        )
        started = time.perf_counter()
        warnings: list[str] = []

        detection = detect_response_type(
            content_type=envelope.content_type,
            body=envelope.body,
            raw=envelope.raw,
        )

        if detection.content_type_mismatch and detection.detail:
            warnings.append(detection.detail)

        if detection.response_type is ResponseType.STRUCTURED:
            result = self._adapt_structured(
                envelope, detection, active, scorer, warnings
            )
        else:
            result = self._adapt_non_structured(
                envelope, detection, active, warnings
            )

        assets = list(result.assets) + self._adapt_asset_urls(envelope, warnings)

        elapsed_ms = (time.perf_counter() - started) * 1000.0

        return replace(
            result,
            assets=tuple(assets),
            warnings=tuple(dict.fromkeys([*result.warnings, *warnings])),
            transformation=replace(
                result.transformation, processing_ms=elapsed_ms
            ),
        )

    # ------------------------------------------------------------------
    # Structured
    # ------------------------------------------------------------------

    def _adapt_structured(
        self,
        envelope: ResponseEnvelope,
        detection: DetectionResult,
        options: AdaptationOptions,
        scorer: RelevanceScorer,
        warnings: list[str],
    ) -> AdaptedResponse:
        body = envelope.body

        if body is None and envelope.raw:
            body = _decode_json(envelope.raw)

        try:
            records, wrapper_path = unwrap_payload(body)
        except MalformedResponseError as exc:
            return self._failure(envelope, detection, str(exc))

        record = records[0]

        if len(records) > 1:
            # A collection response. Only the first record is adapted, and the
            # caller is TOLD so - silently adapting one row of forty would look
            # like a complete answer.
            warnings.append(
                f"the response carried {len(records)} records; the first was "
                "adapted and the rest were not"
            )

        input_fields = count_leaf_fields(body)
        input_bytes = serialized_size(body if body is not None else envelope.raw)

        entity_type: str | None = None
        canonical_data: Mapping[str, Any] | None = None
        canonical_record_id: str | None = None
        entity_confidence: float | None = None
        candidates: list[tuple[str, str | None]]

        if options.enable_erp_mapping:
            try:
                schema = infer_response_schema(
                    records,
                    envelope.source_system_id,
                    envelope.entity_hint,
                    envelope.endpoint,
                )
                adaptation = self.structured.adapt(
                    record, schema, envelope.source_system_id, envelope.endpoint
                )
            except (MalformedResponseError, MappingUnavailableError) as exc:
                # The payload was fine; the canonical model simply has no
                # vocabulary for it. Falling through to a passthrough keeps the
                # response usable and records why it was not mapped.
                warnings.append(f"ERP mapping unavailable: {exc}")
                candidates = _passthrough_candidates(record)
            else:
                entity_type = adaptation.entity_type
                canonical_data = adaptation.canonical_data
                canonical_record_id = adaptation.canonical_record_id
                entity_confidence = adaptation.entity_confidence
                candidates = _mapped_candidates(adaptation.decisions)
                warnings.extend(adaptation.issues)
        else:
            candidates = _passthrough_candidates(record)

        decisions = scorer.rank(
            envelope.query,
            candidates,
            entity_type=entity_type,
            minimum_score=options.minimum_relevance_score,
            max_fields=options.max_fields,
            enabled=options.enable_relevance_selection,
            blocked_fields=options.policy.blocked_fields,
        )

        formatted = build_payload(
            decisions,
            canonical_data,
            _flat_source(record),
            options,
            envelope.sensitivity,
        )

        if formatted.withheld_fields:
            warnings.append(
                f"the response is classified {envelope.sensitivity.value} and "
                f"{len(formatted.withheld_fields)} field(s) were withheld by "
                "policy"
            )

        if formatted.dropped_fields:
            warnings.append(
                f"{len(formatted.dropped_fields)} field(s) were removed to fit "
                f"the {options.max_output_characters}-character budget"
            )

        decisions = apply_budget_to_decisions(decisions, formatted)
        reported, decisions_truncated = limit_decisions(
            decisions, options.max_reported_fields
        )

        return AdaptedResponse(
            response_type=ResponseType.STRUCTURED,
            entity_type=entity_type,
            llm_ready=formatted.payload,
            provenance=self._provenance(
                envelope, options, canonical_record_id, entity_type
            ),
            transformation=TransformationMetrics(
                input_bytes=input_bytes,
                output_bytes=serialized_size(formatted.payload),
                input_fields=input_fields,
                selected_fields=formatted.field_count,
                truncated=formatted.truncated,
            ),
            report=AdaptationReport(
                detection=detection,
                detected_entity=entity_type,
                entity_confidence=entity_confidence,
                input_field_count=input_fields,
                selected_field_count=formatted.field_count,
                field_decisions=reported,
                removed_by_reason=removal_summary(decisions),
                wrapper_path=wrapper_path,
                decisions_truncated=decisions_truncated,
            ),
            warnings=(),
        )

    # ------------------------------------------------------------------
    # Images, documents, binary
    # ------------------------------------------------------------------

    def _adapt_non_structured(
        self,
        envelope: ResponseEnvelope,
        detection: DetectionResult,
        options: AdaptationOptions,
        warnings: list[str],
    ) -> AdaptedResponse:
        assets: tuple[AdaptedAsset, ...] = ()
        input_bytes = serialized_size(envelope.raw)

        if envelope.raw:
            try:
                asset = self.assets.adapt_bytes(
                    envelope.raw,
                    declared_content_type=envelope.content_type,
                    label=envelope.endpoint,
                )
            except AssetError as exc:
                warnings.append(f"{type(exc).__name__}: {exc}")
            else:
                assets = (asset,)
                warnings.extend(asset.warnings)
        elif detection.response_type is not ResponseType.UNKNOWN:
            warnings.append(
                "the response was classified as non-structured but carried no "
                "bytes to extract"
            )

        text_size = sum(
            len(asset.text or "") for asset in assets
        )

        return AdaptedResponse(
            response_type=detection.response_type,
            entity_type=None,
            llm_ready={},
            assets=assets,
            provenance=self._provenance(envelope, options, None, None),
            transformation=TransformationMetrics(
                input_bytes=input_bytes,
                output_bytes=text_size,
                input_fields=0,
                selected_fields=0,
                truncated=any(asset.truncated for asset in assets),
            ),
            report=AdaptationReport(detection=detection),
            # An unreadable binary is still a successful adaptation: the caller
            # receives a truthful description of content that cannot be read,
            # which is exactly what stops a model inventing its contents.
            success=bool(assets) or detection.response_type is ResponseType.UNKNOWN,
            warnings=(),
        )

    def _adapt_asset_urls(
        self, envelope: ResponseEnvelope, warnings: list[str]
    ) -> list[AdaptedAsset]:
        """Adapt any asset URLs the response referenced.

        Every failure here is recorded and survived. A refused URL produces a
        placeholder asset rather than an omission, so a caller can see that
        something was referenced and deliberately not retrieved.
        """
        adapted: list[AdaptedAsset] = []

        for reference in envelope.asset_urls:
            try:
                adapted.append(
                    self.assets.adapt_url(
                        reference.url,
                        fetcher=self.fetcher,
                        resolver=self.resolver,
                        label=reference.label,
                        declared_content_type=reference.declared_content_type,
                    )
                )
            except AssetError as exc:
                reason = f"{type(exc).__name__}: {exc}"
                warnings.append(reason)
                adapted.append(refused_asset(reference.url, reason, reference.label))

        return adapted

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------

    def _provenance(
        self,
        envelope: ResponseEnvelope,
        options: AdaptationOptions,
        canonical_record_id: str | None,
        entity_type: str | None,
    ) -> AdaptationProvenance:
        return AdaptationProvenance(
            source_system_id=envelope.source_system_id,
            endpoint=envelope.endpoint,
            http_status=envelope.http_status,
            content_type=envelope.content_type,
            engine_version=options.version,
            config_fingerprint=options.fingerprint(),
            sensitivity=envelope.sensitivity,
            headers=_allowed_headers(
                envelope.headers, options.policy.allowed_headers
            ),
            canonical_record_id=canonical_record_id,
            source_entity=entity_type,
        )

    def _failure(
        self,
        envelope: ResponseEnvelope,
        detection: DetectionResult,
        message: str,
    ) -> AdaptedResponse:
        """The only genuinely failed outcome: nothing usable could be produced."""
        return AdaptedResponse(
            response_type=detection.response_type,
            llm_ready={},
            provenance=self._provenance(envelope, self.options, None, None),
            transformation=TransformationMetrics(
                input_bytes=serialized_size(envelope.body or envelope.raw)
            ),
            report=AdaptationReport(detection=detection),
            warnings=(message,),
            success=False,
        )


def _decode_json(raw: bytes) -> Any:
    """Decode a byte body the caller did not pre-parse."""
    import json

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MalformedResponseError(
            "the response bytes were classified as JSON but did not parse",
            detail=str(exc),
        ) from exc


def _mapped_candidates(
    decisions: Sequence[Any],
) -> list[tuple[str, str | None]]:
    """Mapping decisions reduced to what the scorer needs.

    A field the mapping engine could not place keeps its ``None`` target rather
    than being dropped: it is still part of the response, it still costs
    context, and a question may still be asking for it by its literal name.
    """
    return [
        (
            decision.source_field,
            decision.selected.qualified_target if decision.selected else None,
        )
        for decision in decisions
    ]


def _passthrough_candidates(record: Mapping[str, Any]) -> list[tuple[str, None]]:
    """Field candidates with no canonical target.

    Used when ERP mapping is off or unavailable. The fields still get scored -
    on their literal names only - so the query-relevance mechanism is measured
    on the same footing as the mapped path rather than being skipped.
    """
    return [(name, None) for name in flatten_record(record)]


def _flat_source(record: Mapping[str, Any]) -> dict[str, Any]:
    """The record as dotted paths, so a nested field can be emitted by name."""
    return flatten_record(record)


__all__ = ["ResponseAdaptationService"]
