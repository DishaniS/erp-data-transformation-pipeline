"""Turn the non-JSON parts of an ERP response into something an LLM can use.

WHAT LEAVES THIS MODULE
-----------------------
Text, dimensions, page counts, hashes. Never bytes. An ``AdaptedAsset`` is a
description of a payload plus whatever text could be recovered from it, and the
``llm_directly_readable`` flag tells the caller which of those two they are
holding. Putting the bytes in the contract would push a base64 blob through the
API layer, the logs and the LLM context, which is the exact cost this phase
exists to remove.

WHAT IT REUSES
--------------
    ingestion.image_ingestion   image -> dimensions + OCR text
    ingestion.pdf_ingestion     PDF   -> page text, with OCR fallback
    ingestion.detection         magic bytes -> what this actually is
    ingestion.hashing           content identity
    ai.chunking                 long document text -> page-anchored chunks

None of that is reimplemented here. This module's own work is the two things
file ingestion never had to deal with: bytes that arrived over the wire with no
file behind them, and URLs supplied by a remote system.

WHY THE URL RULES ARE THIS STRICT
---------------------------------
An asset URL is chosen by the ERP system, not by us. Fetching it unconditionally
would turn this service into a request proxy sitting inside the network
perimeter - the classic SSRF position, where ``http://169.254.169.254/`` returns
cloud credentials and ``http://127.0.0.1:5432/`` reaches a database that trusts
local connections. Every fetch is therefore validated against an explicit policy
BEFORE a socket is opened, redirects are re-validated rather than followed
blindly, and a refusal is reported as a warning on an otherwise successful
adaptation rather than as a failure.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from erp_pipeline.ai.chunking import ChunkingConfig, chunk_document
from erp_pipeline.ingestion.detection import detect_from_signature
from erp_pipeline.ingestion.errors import IngestionError
from erp_pipeline.ingestion.hashing import hash_bytes, make_file_id
from erp_pipeline.ingestion.image_ingestion import ingest_image_file
from erp_pipeline.ingestion.models import (
    FileSource,
    FileType,
    ImageOptions,
    PdfOptions,
)
from erp_pipeline.ingestion.pdf_ingestion import ingest_pdf_file
from erp_pipeline.response_adaptation.errors import (
    AssetFetchFailedError,
    AssetFetchRefusedError,
    AssetTooLargeError,
    InvalidAssetContentError,
)
from erp_pipeline.response_adaptation.models import AdaptedAsset, AssetKind

#: Default ceiling on a single asset. Generous enough for a scanned invoice,
#: small enough that a mislabelled 500 MB export cannot exhaust memory.
DEFAULT_MAX_ASSET_BYTES = 12 * 1024 * 1024

#: Default ceiling on text carried out of one asset, so a 400-page PDF cannot
#: silently become the whole LLM context.
DEFAULT_MAX_ASSET_TEXT_CHARS = 20_000

#: Ports a fetch may target. An ERP that serves assets from 5432 or 6379 is not
#: serving assets.
DEFAULT_ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})


class _Refusal:
    """Named refusal rules, so a caller learns which policy stopped a fetch."""

    SCHEME = "scheme_not_allowed"
    NO_HOST = "no_host"
    UNRESOLVABLE = "host_unresolvable"
    PRIVATE_ADDRESS = "private_or_reserved_address"
    PORT = "port_not_allowed"
    HOST_NOT_ALLOWED = "host_not_in_allow_list"
    CREDENTIALS = "credentials_in_url"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    FETCHING_DISABLED = "url_fetching_disabled"


def _is_forbidden_address(address: str) -> bool:
    """Whether an IP is one an outbound asset fetch must never reach.

    Covers loopback, RFC1918 private space, link-local (which is where every
    major cloud's instance-metadata service lives), multicast, reserved and
    unspecified ranges, plus IPv4-mapped IPv6 forms of all of them - the last
    being how ``http://[::ffff:127.0.0.1]/`` slips past a naive check.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        # Not an address at all: caller decides, this function does not
        # pretend the value was safe.
        return True

    if getattr(parsed, "ipv4_mapped", None) is not None:
        parsed = parsed.ipv4_mapped

    return bool(
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


@dataclass(frozen=True)
class UrlSafetyPolicy:
    """What an asset URL must satisfy before anything is fetched.

    Fetching is DISABLED by default. A deployment that wants remote assets
    turns it on deliberately, which means the safe configuration is the one a
    caller gets by forgetting to configure anything.
    """

    enabled: bool = False
    allowed_schemes: frozenset[str] = frozenset({"https"})
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS
    #: When non-empty, ONLY these hosts may be fetched. The strongest control
    #: available and the one a production deployment should use: an ERP's asset
    #: host is known in advance.
    allowed_hosts: frozenset[str] = frozenset()
    allow_private_addresses: bool = False
    max_bytes: int = DEFAULT_MAX_ASSET_BYTES
    timeout_seconds: float = 10.0
    max_redirects: int = 0

    def fingerprint(self) -> str:
        hosts = ",".join(sorted(self.allowed_hosts)) or "*"
        return (
            f"enabled={int(self.enabled)};"
            f"schemes={','.join(sorted(self.allowed_schemes))};"
            f"hosts={hosts};"
            f"private={int(self.allow_private_addresses)};"
            f"max={self.max_bytes}"
        )


#: The default is refusal. Nothing is fetched unless a deployment says so.
DEFAULT_URL_POLICY = UrlSafetyPolicy()

#: Resolves a hostname to addresses. Injected so a unit test can exercise the
#: policy without DNS, and so no test ever needs a network.
Resolver = Callable[[str], Sequence[str]]

#: Performs a validated fetch. Injected for the same reason: this package ships
#: NO default HTTP client, so importing it can never cause a request.
Fetcher = Callable[["ValidatedUrl"], "FetchedAsset"]


def default_resolver(host: str) -> tuple[str, ...]:
    """Every address a hostname resolves to.

    ALL of them are checked, not just the first. A DNS entry that returns one
    public and one loopback address would otherwise pass validation and then
    connect to whichever the OS picked.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:  # pragma: no cover - depends on the resolver
        raise AssetFetchRefusedError(
            f"the asset host {host!r} could not be resolved",
            rule=_Refusal.UNRESOLVABLE,
        ) from exc

    return tuple(dict.fromkeys(info[4][0] for info in infos))


@dataclass(frozen=True)
class ValidatedUrl:
    """A URL that has passed policy. Carries what the fetcher needs."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]
    timeout_seconds: float
    max_bytes: int


@dataclass(frozen=True)
class FetchedAsset:
    """What a fetcher returns: bytes plus what the server claimed they are."""

    body: bytes
    content_type: str | None = None
    final_url: str | None = None


def validate_asset_url(
    url: str,
    policy: UrlSafetyPolicy = DEFAULT_URL_POLICY,
    resolver: Resolver | None = None,
) -> ValidatedUrl:
    """Check a URL against the policy. Raises rather than returning a verdict.

    Every rejection carries its rule name, so an operator reading a warning
    learns which setting to change instead of only that the fetch did not
    happen.
    """
    if not policy.enabled:
        raise AssetFetchRefusedError(
            "asset URL fetching is disabled for this deployment",
            url=url,
            rule=_Refusal.FETCHING_DISABLED,
        )

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()

    if scheme not in policy.allowed_schemes:
        # This is also what refuses file://, ftp:// and gopher:// - the schemes
        # that turn a fetcher into a local file reader or a protocol smuggler.
        raise AssetFetchRefusedError(
            f"scheme {scheme or '(none)'!r} is not permitted for asset URLs",
            url=url,
            rule=_Refusal.SCHEME,
        )

    if parts.username or parts.password:
        raise AssetFetchRefusedError(
            "asset URLs must not carry credentials",
            url=url,
            rule=_Refusal.CREDENTIALS,
        )

    host = (parts.hostname or "").lower()

    if not host:
        raise AssetFetchRefusedError(
            "the asset URL names no host", url=url, rule=_Refusal.NO_HOST
        )

    port = parts.port or (443 if scheme == "https" else 80)

    if port not in policy.allowed_ports:
        raise AssetFetchRefusedError(
            f"port {port} is not permitted for asset URLs",
            url=url,
            rule=_Refusal.PORT,
        )

    if policy.allowed_hosts and host not in policy.allowed_hosts:
        raise AssetFetchRefusedError(
            f"host {host!r} is not in the asset host allow-list",
            url=url,
            rule=_Refusal.HOST_NOT_ALLOWED,
        )

    resolve = resolver or default_resolver
    addresses = tuple(resolve(host))

    if not addresses:
        raise AssetFetchRefusedError(
            f"the asset host {host!r} resolved to no addresses",
            url=url,
            rule=_Refusal.UNRESOLVABLE,
        )

    if not policy.allow_private_addresses:
        for address in addresses:
            if _is_forbidden_address(address):
                raise AssetFetchRefusedError(
                    f"the asset host {host!r} resolves to the non-public "
                    f"address {address}",
                    url=url,
                    rule=_Refusal.PRIVATE_ADDRESS,
                )

    return ValidatedUrl(
        url=url,
        scheme=scheme,
        host=host,
        port=port,
        addresses=addresses,
        timeout_seconds=policy.timeout_seconds,
        max_bytes=policy.max_bytes,
    )


def fetch_asset(
    url: str,
    policy: UrlSafetyPolicy = DEFAULT_URL_POLICY,
    fetcher: Fetcher | None = None,
    resolver: Resolver | None = None,
) -> FetchedAsset:
    """Validate, then fetch through the injected client.

    There is no default fetcher on purpose. A deployment supplies its own HTTP
    client, which keeps this package free of a networking dependency and makes
    "no fetcher configured" a refusal rather than an accidental request.
    """
    validated = validate_asset_url(url, policy, resolver)

    if fetcher is None:
        raise AssetFetchRefusedError(
            "no asset fetcher is configured, so no URL can be retrieved",
            url=url,
            rule=_Refusal.FETCHING_DISABLED,
        )

    try:
        result = fetcher(validated)
    except AssetFetchRefusedError:
        raise
    except Exception as exc:  # noqa: BLE001 - any client failure is one outcome
        raise AssetFetchFailedError(
            f"the asset fetch failed: {type(exc).__name__}", url=url
        ) from exc

    if result.final_url and result.final_url != url:
        # A redirect is a NEW destination and gets the same scrutiny as the
        # first: an allowed host redirecting to 169.254.169.254 is the standard
        # way an SSRF filter is bypassed.
        if policy.max_redirects <= 0:
            raise AssetFetchRefusedError(
                "the asset URL redirected and redirects are not permitted",
                url=result.final_url,
                rule=_Refusal.TOO_MANY_REDIRECTS,
            )

        validate_asset_url(result.final_url, policy, resolver)

    if len(result.body) > policy.max_bytes:
        raise AssetTooLargeError(
            "the fetched asset exceeds the configured size limit",
            size_bytes=len(result.body),
            limit_bytes=policy.max_bytes,
        )

    return result


@dataclass(frozen=True)
class AssetOptions:
    """Budgets and toggles for the asset path."""

    max_bytes: int = DEFAULT_MAX_ASSET_BYTES
    max_text_chars: int = DEFAULT_MAX_ASSET_TEXT_CHARS
    ocr_enabled: bool = True
    #: Pages read from a document response. Bounded because a caller's question
    #: is answered by the first pages far more often than by the four hundredth.
    max_pages: int = 20
    url_policy: UrlSafetyPolicy = DEFAULT_URL_POLICY
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)


def _file_source(payload: bytes, file_type: FileType, media_type: str,
                 label: str | None) -> FileSource:
    """Identity for a payload that never was a file.

    ``original_filename`` is synthesised from the content hash rather than from
    the URL: a URL path is attacker-influenced and would end up in provenance,
    and identity here is the bytes anyway.

    PHASE 10: the bytes travel IN MEMORY via ``payload``. They used to be
    written to a temporary file because both extractors took a path - the same
    trade Phase 3 originally made and then reversed, for the same reason. An
    ERP response asset is a scanned invoice or a signed certificate, and
    spilling it to the system temp directory puts it on disk in plaintext,
    outside every control the rest of the pipeline applies. Phase 3 gave
    ``FileSource`` an in-memory payload precisely so this would not be
    necessary; this is that fix, applied here.

    Nothing else changes: the same extractors, the same options, the same
    output. It is a transport change, not a behaviour change.
    """
    digest = hash_bytes(payload)

    return FileSource(
        file_id=make_file_id(digest),
        content_hash=digest,
        original_filename=label or f"response_asset_{digest[:12]}",
        file_type=file_type,
        media_type=media_type,
        size_bytes=len(payload),
        payload=payload,
    )


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Bound extracted text, reporting whether anything was cut."""
    if len(text) <= limit:
        return text, False

    return text[:limit], True


class AssetAdapter:
    """Adapts one binary payload into an ``AdaptedAsset``."""

    def __init__(self, options: AssetOptions | None = None) -> None:
        self.options = options or AssetOptions()

    def adapt_bytes(
        self,
        payload: bytes,
        declared_content_type: str | None = None,
        source_url: str | None = None,
        label: str | None = None,
    ) -> AdaptedAsset:
        """Classify a payload by its bytes and route it to the right extractor.

        The declared content type is checked against the bytes and DISAGREEMENT
        IS REPORTED, but the bytes decide. A PDF labelled ``image/png`` is a
        PDF, and sending it to the image extractor because a header said so
        would produce a confident failure instead of a correct answer.
        """
        if len(payload) > self.options.max_bytes:
            raise AssetTooLargeError(
                "the asset exceeds the configured size limit",
                size_bytes=len(payload),
                limit_bytes=self.options.max_bytes,
            )

        # ``detect_from_signature`` rather than ``detect_file_type``: the
        # latter also consults a filename extension, and a payload that arrived
        # over HTTP has no filename to consult. Bytes are the only evidence
        # here, which is the stronger position anyway.
        detected = detect_from_signature(payload[:64])
        warnings: list[str] = []
        declared = (declared_content_type or "").split(";", 1)[0].strip().lower()
        detected_type, detected_media = detected if detected else (None, None)

        if declared and detected_media and declared != detected_media:
            warnings.append(
                f"declared content type {declared!r} does not match the "
                f"detected {detected_media!r}; the bytes were trusted"
            )

        if detected_type is FileType.IMAGE:
            return self._adapt_image(
                payload, detected_media, source_url, label, warnings
            )

        if detected_type is FileType.PDF:
            return self._adapt_document(
                payload, detected_media, source_url, label, warnings
            )

        return self._unsupported(
            payload, detected_media or declared or None,
            source_url, label, warnings,
        )

    # -- per-kind handlers ------------------------------------------------

    def _adapt_image(
        self, payload: bytes, media_type: str | None, source_url: str | None,
        label: str | None, warnings: list[str],
    ) -> AdaptedAsset:
        try:
            document = ingest_image_file(
                _file_source(payload, FileType.IMAGE,
                             media_type or "application/octet-stream",
                             label),
                ImageOptions(
                    ocr_enabled=self.options.ocr_enabled,
                    max_text_chars=self.options.max_text_chars,
                ),
            )
        except IngestionError as exc:
            # Bytes that carry an image signature but will not decode. The
            # response as a whole is still perfectly good, so this degrades to
            # a described-but-unread asset instead of failing the adaptation.
            warnings.append(
                f"the image could not be decoded ({type(exc).__name__})"
            )
            return self._unsupported(
                payload, media_type, source_url, label, warnings
            )

        # Dimensions live on ``document_metadata``: ``ExtractedPage`` records
        # text and how it was obtained, not the geometry of the source image.
        properties = document.document_metadata or {}
        page = document.pages[0] if document.pages else None
        text, truncated = _clip(document.document_text, self.options.max_text_chars)
        truncated = truncated or any(page.truncated for page in document.pages)
        warnings.extend(
            f"{warning.category}: {warning.message}" for warning in document.warnings
        )

        return AdaptedAsset(
            kind=AssetKind.IMAGE,
            mime_type=media_type,
            size_bytes=len(payload),
            content_hash=hash_bytes(payload),
            # An image is the one asset an LLM can genuinely take as-is. The
            # OCR text is carried ALONGSIDE it rather than instead of it, so a
            # caller with a vision-capable model is not forced to accept a
            # lossy transcription of a document it could have read.
            llm_directly_readable=True,
            width=_as_int(properties.get("width")),
            height=_as_int(properties.get("height")),
            text=text or None,
            ocr_used=(
                page is not None and page.extraction_method == "ocr"
            ),
            extraction_status=document.status.value,
            source_url=source_url,
            label=label,
            warnings=tuple(warnings),
            truncated=truncated,
        )

    def _adapt_document(
        self, payload: bytes, media_type: str | None, source_url: str | None,
        label: str | None, warnings: list[str],
    ) -> AdaptedAsset:
        try:
            document = ingest_pdf_file(
                _file_source(payload, FileType.PDF,
                             media_type or "application/pdf", label),
                PdfOptions(
                    max_pages=self.options.max_pages,
                    max_total_text_chars=self.options.max_text_chars,
                    ocr_fallback=self.options.ocr_enabled,
                ),
            )
        except IngestionError as exc:
            # Corrupt, truncated or encrypted. Same reasoning as the image
            # path: an unreadable attachment does not invalidate the JSON that
            # came with it.
            warnings.append(
                f"the document could not be read ({type(exc).__name__})"
            )
            return self._unsupported(
                payload, media_type, source_url, label, warnings
            )

        warnings.extend(
            f"{warning.category}: {warning.message}" for warning in document.warnings
        )

        text, truncated = _clip(document.document_text, self.options.max_text_chars)

        # The extractor applies its OWN page and character budgets before this
        # code sees the text. When it truncated, the asset must say so even
        # though the outer clip found nothing left to cut - otherwise a
        # shortened document is reported as complete, which is the one thing
        # the truncation flag exists to prevent.
        truncated = truncated or any(page.truncated for page in document.pages)

        page_range = None

        if document.pages:
            numbers = [page.page_number for page in document.pages]
            page_range = (min(numbers), max(numbers))

        if document.pages and self.options.chunking is not None:
            # Chunking is used for its PAGE ANCHORING, not to split the output:
            # it is what lets the result say which pages the text came from
            # when a page budget cut the document short.
            try:
                chunks = chunk_document(document, self.options.chunking)
            except Exception as exc:  # noqa: BLE001 - never fatal to an asset
                warnings.append(f"chunking skipped: {type(exc).__name__}")
            else:
                if chunks:
                    warnings.append(
                        f"text spans {len(chunks)} chunk(s) of the source document"
                    )

        return AdaptedAsset(
            kind=AssetKind.DOCUMENT,
            mime_type=media_type or "application/pdf",
            size_bytes=len(payload),
            content_hash=hash_bytes(payload),
            # A PDF is not directly readable: what reaches the model is the
            # extracted text, and saying otherwise would invite a caller to
            # hand over bytes no model accepts.
            llm_directly_readable=False,
            text=text or None,
            ocr_used=any(
                page.extraction_method == "ocr" for page in document.pages
            ),
            page_count=document.page_count,
            page_range=page_range,
            extraction_status=document.status.value,
            source_url=source_url,
            label=label,
            warnings=tuple(warnings),
            truncated=truncated,
        )

    def _unsupported(
        self, payload: bytes, media_type: str | None, source_url: str | None,
        label: str | None, warnings: list[str],
    ) -> AdaptedAsset:
        """A payload this phase cannot read - described, never guessed at.

        The honest outcome, and deliberately NOT an error: a response whose JSON
        adapted correctly should not be discarded because it also carried a ZIP
        attachment. The caller receives a truthful description saying the
        content is unavailable, which a model can relay, rather than a
        hallucination-inviting silence.
        """
        warnings.append(
            "the payload is not a supported image or PDF; only its metadata "
            "was adapted"
        )

        return AdaptedAsset(
            kind=AssetKind.UNSUPPORTED_BINARY,
            mime_type=media_type or "application/octet-stream",
            size_bytes=len(payload),
            content_hash=hash_bytes(payload),
            llm_directly_readable=False,
            text=None,
            extraction_status="unsupported",
            source_url=source_url,
            label=label,
            warnings=tuple(warnings),
        )

    # -- URL entry point --------------------------------------------------

    def adapt_url(
        self,
        url: str,
        fetcher: Fetcher | None = None,
        resolver: Resolver | None = None,
        label: str | None = None,
        declared_content_type: str | None = None,
    ) -> AdaptedAsset:
        """Fetch a policy-approved URL and adapt what comes back."""
        result = fetch_asset(url, self.options.url_policy, fetcher, resolver)

        return self.adapt_bytes(
            result.body,
            declared_content_type=result.content_type or declared_content_type,
            source_url=url,
            label=label,
        )


def refused_asset(url: str, reason: str, label: str | None = None) -> AdaptedAsset:
    """A placeholder for an asset that was never retrieved.

    Recorded in the output rather than omitted, because a caller comparing a
    response against its asset list needs to see that something was there and
    was deliberately not fetched.
    """
    return AdaptedAsset(
        kind=AssetKind.REFUSED,
        llm_directly_readable=False,
        source_url=url,
        label=label,
        extraction_status="refused",
        warnings=(reason,),
    )


def _as_int(value: Any) -> int | None:
    """A dimension, or ``None`` when the extractor did not record one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return int(value)


__all__ = [
    "DEFAULT_MAX_ASSET_BYTES",
    "DEFAULT_MAX_ASSET_TEXT_CHARS",
    "DEFAULT_ALLOWED_PORTS",
    "DEFAULT_URL_POLICY",
    "UrlSafetyPolicy",
    "ValidatedUrl",
    "FetchedAsset",
    "AssetOptions",
    "AssetAdapter",
    "validate_asset_url",
    "fetch_asset",
    "default_resolver",
    "refused_asset",
]
