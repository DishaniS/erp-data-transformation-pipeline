"""ERP-aware adaptive transformation of already-executed API responses.

The pipeline's other packages prepare ERP data for retrieval. This one closes
the loop at the other end: when an ERP API has been called and has answered,
something has to turn what came back - a wrapped JSON envelope, a scanned PDF,
a photographed receipt - into context a language model can actually use.

    ResponseEnvelope  ->  ResponseAdaptationService.adapt  ->  AdaptedResponse

WHAT MAKES IT ERP-AWARE RATHER THAN GENERIC
-------------------------------------------
A generic adapter can only pretty-print what it was given. This one runs the
response through the SAME canonical mapping engine the rest of the pipeline
uses, so ``cust_ref``, ``KUNNR`` and ``customer_no`` all arrive at the model as
``customer_id`` - and that shared vocabulary is also what lets a question about
"the customer" find a field that never contains the word.

WHAT IT DOES NOT DO
-------------------
It does not choose an ERP endpoint, execute an ERP call, or use a language
model to make any of its decisions. Field selection is a deterministic weighted
score, explainable field by field, which is what makes the mechanism
measurable.
"""

from erp_pipeline.response_adaptation.assets import (
    AssetAdapter,
    AssetOptions,
    FetchedAsset,
    UrlSafetyPolicy,
    ValidatedUrl,
    fetch_asset,
    refused_asset,
    validate_asset_url,
)
from erp_pipeline.response_adaptation.detector import detect_response_type
from erp_pipeline.response_adaptation.errors import (
    AdaptationConfigurationError,
    AssetError,
    AssetFetchFailedError,
    AssetFetchRefusedError,
    AssetTooLargeError,
    BudgetExceededError,
    InvalidAssetContentError,
    MalformedResponseError,
    MappingUnavailableError,
    ResponseAdaptationError,
    UnsupportedResponseTypeError,
)
from erp_pipeline.response_adaptation.formatter import (
    FormattedPayload,
    build_payload,
)
from erp_pipeline.response_adaptation.models import (
    ADAPTATION_ENGINE_VERSION,
    AdaptationOptions,
    AdaptationPolicy,
    AdaptationProvenance,
    AdaptationReport,
    AdaptedAsset,
    AdaptedResponse,
    AssetKind,
    AssetReference,
    DetectionEvidence,
    DetectionResult,
    FieldRelevance,
    RelevanceWeights,
    ResponseEnvelope,
    ResponseType,
    TransformationMetrics,
    serialized_size,
)
from erp_pipeline.response_adaptation.relevance import (
    QUERY_INTENT_TERMS,
    RelevanceScorer,
    query_tokens,
    removal_summary,
)
from erp_pipeline.response_adaptation.service import ResponseAdaptationService
from erp_pipeline.response_adaptation.structured import (
    StructuredResponseAdapter,
    count_leaf_fields,
    flatten_record,
    infer_response_schema,
    unwrap_payload,
)

__all__ = [
    "ADAPTATION_ENGINE_VERSION",
    # service
    "ResponseAdaptationService",
    # contracts
    "ResponseEnvelope",
    "AdaptedResponse",
    "AdaptationOptions",
    "AdaptationPolicy",
    "AdaptationProvenance",
    "AdaptationReport",
    "AdaptedAsset",
    "AssetKind",
    "AssetReference",
    "DetectionEvidence",
    "DetectionResult",
    "FieldRelevance",
    "RelevanceWeights",
    "ResponseType",
    "TransformationMetrics",
    "serialized_size",
    # stages
    "detect_response_type",
    "unwrap_payload",
    "infer_response_schema",
    "StructuredResponseAdapter",
    "count_leaf_fields",
    "flatten_record",
    "RelevanceScorer",
    "QUERY_INTENT_TERMS",
    "query_tokens",
    "removal_summary",
    "FormattedPayload",
    "build_payload",
    # assets
    "AssetAdapter",
    "AssetOptions",
    "UrlSafetyPolicy",
    "ValidatedUrl",
    "FetchedAsset",
    "validate_asset_url",
    "fetch_asset",
    "refused_asset",
    # errors
    "ResponseAdaptationError",
    "AdaptationConfigurationError",
    "UnsupportedResponseTypeError",
    "MalformedResponseError",
    "MappingUnavailableError",
    "AssetError",
    "AssetFetchRefusedError",
    "AssetTooLargeError",
    "InvalidAssetContentError",
    "AssetFetchFailedError",
    "BudgetExceededError",
]
