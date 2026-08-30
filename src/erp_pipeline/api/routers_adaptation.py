"""The response-adaptation route (Phase 14).

ONE ENDPOINT, DELIBERATELY
--------------------------
``POST /v1/responses/adapt`` is the whole surface. Adaptation is one operation
with one input and one output; splitting it per response type would push the
detection decision onto the caller, who is the party least able to make it -
they are handing over bytes precisely because they do not know what those bytes
are.

WHAT THIS ROUTE DOES NOT DO
---------------------------
It does not call an ERP system. The response it adapts has already been
fetched by whoever is calling. This service is not, and must not become, an
outbound HTTP proxy.
"""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, Depends

from erp_pipeline.api.routers import get_service
from erp_pipeline.api.schemas import ResponseAdaptRequest, ResponseAdaptResponse
from erp_pipeline.orchestration import InvalidPipelineRequestError, OrchestrationService
from erp_pipeline.response_adaptation import (
    AdaptationOptions,
    AssetReference,
    ResponseAdaptationService,
    ResponseAdaptationError,
    ResponseEnvelope,
)

LOGGER = logging.getLogger("erp_pipeline.api.routers_adaptation")

responses_router = APIRouter(prefix="/v1/responses", tags=["responses"])


def get_adaptation_service(
    service: OrchestrationService = Depends(get_service),
) -> ResponseAdaptationService:
    """The adaptation service, reused across requests when one is wired up.

    Falls back to a fresh default instance rather than failing: adaptation
    needs no database, no vector store and no embedding model, so it is the one
    capability that should still work when the rest of the stack is not
    configured. A per-request instance costs an alias-index build, which is why
    the wired-up one is preferred when present.
    """
    existing = getattr(service.services, "response_adaptation", None)

    if existing is not None:
        return existing

    return ResponseAdaptationService()


def _apply_options(payload: ResponseAdaptRequest, base: AdaptationOptions
                   ) -> AdaptationOptions:
    """Overlay the request's budgets onto the engine defaults.

    Only fields the caller actually sent are overridden - an omitted budget
    keeps the deployment's configured value rather than resetting to a library
    default the operator never chose.
    """
    from dataclasses import replace

    overrides = {
        name: value
        for name, value in (
            ("minimum_relevance_score", payload.options.minimum_relevance_score),
            ("max_fields", payload.options.max_fields),
            ("max_output_characters", payload.options.max_output_characters),
            ("max_value_characters", payload.options.max_value_characters),
        )
        if value is not None
    }

    return replace(
        base,
        enable_relevance_selection=payload.options.enable_relevance_selection,
        enable_erp_mapping=payload.options.enable_erp_mapping,
        **overrides,
    )


def _decode_raw(payload: ResponseAdaptRequest) -> bytes | None:
    """Decode the base64 body, refusing malformed input explicitly."""
    if not payload.body_base64:
        return None

    try:
        return base64.b64decode(payload.body_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise InvalidPipelineRequestError(
            "body_base64 is not valid base64"
        ) from exc


@responses_router.post(
    "/adapt",
    response_model=ResponseAdaptResponse,
    operation_id="adaptResponse",
)
def adapt_response(
    payload: ResponseAdaptRequest,
    service: ResponseAdaptationService = Depends(get_adaptation_service),
) -> ResponseAdaptResponse:
    """Transform an already-executed ERP API response into LLM-ready context.

    Returns 200 even when the adaptation only partly succeeded - a refused
    asset URL or a truncating budget is reported in ``warnings`` and
    ``partial``, not as an HTTP error, because the fields that DID adapt are
    still the answer the caller needs. A 422 means the request itself could not
    be interpreted.
    """
    if payload.body is None and not payload.body_base64:
        raise InvalidPipelineRequestError(
            "the request must carry either a decoded body or body_base64"
        )

    envelope = ResponseEnvelope(
        query=payload.query,
        source_system_id=payload.source_system_id,
        endpoint=payload.endpoint,
        http_status=payload.http_status,
        content_type=payload.content_type,
        body=payload.body,
        raw=_decode_raw(payload),
        headers=dict(payload.headers),
        asset_urls=tuple(
            AssetReference(
                url=reference.url,
                declared_content_type=reference.declared_content_type,
                label=reference.label,
            )
            for reference in payload.asset_urls
        ),
        entity_hint=payload.entity_hint,
        sensitivity=payload.sensitivity,
    )

    try:
        result = service.adapt(envelope, _apply_options(payload, service.options))
    except ResponseAdaptationError as exc:
        # Typed adaptation failures are the caller's input being unusable, not
        # a server fault. The message is the engine's own wording, which
        # describes the payload's shape and never its values.
        raise InvalidPipelineRequestError(
            str(exc), error_type=type(exc).__name__
        ) from exc

    body = result.to_dict()

    return ResponseAdaptResponse(
        response_type=body["response_type"],
        entity_type=body["entity_type"],
        llm_ready=body["llm_ready"],
        assets=body["assets"],
        provenance=body["provenance"],
        transformation=body["transformation"],
        report=body.get("report"),
        warnings=body["warnings"],
        success=body["success"],
        partial=result.is_partial,
    )


__all__ = ["responses_router", "get_adaptation_service"]
