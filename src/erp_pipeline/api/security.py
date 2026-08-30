"""Minimal API-key protection.

SCOPE
-----
This is a local research API, not an identity platform. There is no OAuth
server here and there should not be: a half-built one would be worse than an
honest API key behind a gateway.

What this does provide: an optional shared key on mutating routes, compared in
constant time, never logged, never echoed, and never written into the generated
OpenAPI document.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

LOGGER = logging.getLogger("erp_pipeline.api.security")

API_KEY_HEADER = "X-API-Key"

#: Methods that change state. Read routes are protected only when the operator
#: asks for it, so an unauthenticated demo can still browse.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Never guarded. A liveness probe that needs a credential is a liveness probe
#: that will page you at 3am for the wrong reason.
PUBLIC_PATHS = (
    "/v1/health/live",
    "/v1/health/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
)

#: Search used to be POST and was therefore protected whenever API-key auth
#: was enabled. Keep that security boundary when the read-only GET route is
#: used, even in deployments that otherwise permit unauthenticated browsing.
SENSITIVE_READ_PATHS = ("/v1/search",)


def keys_match(supplied: str | None, configured: str | None) -> bool:
    """Constant-time comparison.

    A plain ``==`` on a secret leaks its length and prefix through timing.
    """
    if not configured:
        return True

    if not supplied:
        return False

    return hmac.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8"))


def requires_key(method: str, path: str, protect_reads: bool) -> bool:
    if any(path.startswith(public) for public in PUBLIC_PATHS):
        return False

    if method.upper() in MUTATING_METHODS:
        return True

    if path in SENSITIVE_READ_PATHS:
        return True

    return protect_reads


def redact(value: str | None) -> str:
    """What to put in a log where a key would otherwise go."""
    return "<redacted>" if value else "<absent>"


__all__ = [
    "API_KEY_HEADER",
    "MUTATING_METHODS",
    "PUBLIC_PATHS",
    "SENSITIVE_READ_PATHS",
    "keys_match",
    "requires_key",
    "redact",
]
