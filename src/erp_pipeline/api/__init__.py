"""REST control plane for the ERP transformation pipeline (Phase 13).

        client
          |
          v
      FastAPI /v1          validate, authenticate, shape
          |
          v
   OrchestrationService    plan, persist, enqueue
          |
          v
     existing phases       discovery, mapping, transformation, sync,
                           embedding, hybrid tiered storage

Importing this package does NOT load an embedding model, open a database or
connect to a vector store. Call ``create_app()`` with injected services, or
``build_services()`` when you explicitly want the real ones.
"""

from __future__ import annotations

from erp_pipeline.api.config import (
    API_PREFIX,
    API_TITLE,
    API_VERSION,
    ApiSettings,
)
from erp_pipeline.api.main import build_services, create_app
from erp_pipeline.api.responses import (
    ERROR_STATUS,
    error_body,
    failure,
    status_for,
    success,
)
from erp_pipeline.api.security import API_KEY_HEADER, keys_match, requires_key

__all__ = [
    "API_VERSION",
    "API_PREFIX",
    "API_TITLE",
    "ApiSettings",
    "create_app",
    "build_services",
    "success",
    "failure",
    "error_body",
    "status_for",
    "ERROR_STATUS",
    "API_KEY_HEADER",
    "keys_match",
    "requires_key",
]
