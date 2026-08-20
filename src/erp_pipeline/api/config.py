"""API configuration. Safe defaults, explicit opt-out.

The defaults here assume the least trusted environment: bound to loopback, CORS
closed, uploads capped. A research prototype that defaulted to 0.0.0.0 with
``allow_origins=["*"]`` would be one command away from exposing an ERP
transformation pipeline to a network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from erp_pipeline.orchestration.upload_store import DEFAULT_MAX_UPLOAD_BYTES

API_VERSION = "1.0"
API_PREFIX = "/v1"
API_TITLE = "ERP-Aware Data Transformation Pipeline API"


@dataclass(frozen=True)
class ApiSettings:
    """Everything the API needs to know that is not a service."""

    #: Loopback by default. Binding publicly must be a deliberate act.
    host: str = "127.0.0.1"
    port: int = 8000

    #: When set, mutating requests must present it. Read routes stay open
    #: unless `protect_reads` is on, so a demo is usable without a key.
    api_key: str | None = None
    protect_reads: bool = False

    #: Empty means no cross-origin browser access. Never "*" with credentials.
    cors_origins: tuple[str, ...] = ()
    cors_allow_credentials: bool = False

    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    upload_dir: Path = field(default_factory=lambda: Path("var/uploads"))

    max_page_size: int = 100
    default_page_size: int = 50

    #: Advertised in /v1/capabilities so a client can see the truth.
    sql_server_live_verified: bool = False

    docs_enabled: bool = True

    @classmethod
    def from_environment(cls) -> "ApiSettings":
        """Read overrides from the environment.

        The API key is read but never echoed anywhere: not into logs, not into
        /v1/capabilities, not into the OpenAPI document.
        """
        origins = os.getenv("ERP_API_CORS_ORIGINS", "")

        return cls(
            host=os.getenv("ERP_API_HOST", "127.0.0.1"),
            port=int(os.getenv("ERP_API_PORT", "8000")),
            api_key=os.getenv("ERP_API_KEY") or None,
            protect_reads=os.getenv("ERP_API_PROTECT_READS", "").lower()
            in {"1", "true", "yes"},
            cors_origins=tuple(
                origin.strip() for origin in origins.split(",") if origin.strip()
            ),
            max_upload_bytes=int(
                os.getenv("ERP_API_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))
            ),
            upload_dir=Path(os.getenv("ERP_API_UPLOAD_DIR", "var/uploads")),
            sql_server_live_verified=os.getenv(
                "ERP_SQL_SERVER_LIVE_VERIFIED", ""
            ).lower()
            in {"1", "true", "yes"},
        )

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        # An ApiSettings repr will end up in a debug log eventually. The key
        # must not travel with it.
        return (
            f"ApiSettings(host={self.host!r}, port={self.port}, "
            f"auth_enabled={self.auth_enabled}, "
            f"cors_origins={self.cors_origins!r}, "
            f"max_upload_bytes={self.max_upload_bytes}, api_key=<redacted>)"
        )


__all__ = ["API_VERSION", "API_PREFIX", "API_TITLE", "ApiSettings"]
