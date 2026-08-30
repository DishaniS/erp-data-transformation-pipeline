"""ERP rows that point at a document instead of carrying it (Phase 8).

    employees.birth_certificate      = <BYTEA bytes>          Phase 3
    employees.birth_certificate_url  = "https://.../cert.pdf"  this module

Once the bytes are safely in hand the two are the same thing, and this module
deliberately produces the SAME ``BinaryAssetResult`` Phase 3 produces, so
nothing downstream - detection, extraction, OCR, chunking, attachment identity,
persistence, embedding, storage, search - can tell or care where they came
from.

WHAT THIS IS NOT
----------------
It is static asset retrieval, not ERP API execution. It fetches a document the
ERP DATA explicitly points at. It does not choose endpoints, authenticate as
anyone, follow hyperlinks, crawl, or perform business operations - those belong
to Member 2 and stay there.

WHY THE FIELD MUST BE DECLARED
------------------------------
A URL in a row is not permission to make a request. Scanning every text column
for something starting with ``http`` would turn an ordinary ``website`` or
``notes`` field into outbound traffic chosen by whoever wrote the row - which is
the SSRF position, reached through the database instead of through a request
parameter. Column NAMES are no better: ``document_url`` is a naming convention,
not an authorisation.

So a field is fetchable only when a caller names it explicitly, and everything
else is an ordinary string.

WHY THE POLICY IS PHASE 14'S
----------------------------
``response_adaptation.assets`` already implements the whole thing: schemes,
credential rejection, port allow-list, host allow-list, DNS resolution of EVERY
address, loopback / RFC1918 / link-local / multicast / reserved rejection
including IPv4-mapped IPv6 forms, redirect re-validation, size ceilings, and
fetching disabled by default with no HTTP client shipped at all. A second
policy would be a second thing to keep correct, and the weaker of the two would
become the one an attacker uses.

WHY THE URL NEVER TRAVELS WITH THE CONTENT
------------------------------------------
An ERP asset URL is frequently signed:

    https://storage.example/cert.pdf?token=SECRET&expires=1735689600

That token is a bearer credential. Carrying the raw URL into representation
text, vector payloads, warnings, logs or job reports would scatter it across
every surface this pipeline writes to. Only redacted provenance leaves here:
scheme, host, path, and a hash of the full URL for correlation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from erp_pipeline.ingestion.binary_assets import (
    BinaryAssetOptions,
    BinaryAssetOutcome,
    BinaryAssetResult,
    extract_binary_asset,
)

#: Outcomes a remote reference can reach that a BLOB never can. Added to
#: Phase 3's vocabulary rather than replacing it: everything after the fetch is
#: identical, so the shared outcomes stay shared.
class RemoteAssetOutcome(BinaryAssetOutcome):
    """What happened to one declared remote reference."""

    DISABLED = "remote_fetch_disabled"
    INVALID_URL = "invalid_url"
    NOT_A_URL_VALUE = "not_a_url_value"
    REFUSED = "remote_fetch_refused"
    FAILED = "remote_fetch_failed"
    TOO_LARGE = "response_too_large"


#: Media types a remote server may claim that are never document assets. HTML
#: is the one that matters: a 200 page of markup is not a certificate, and
#: reading links out of it would be crawling.
NON_ASSET_MEDIA_TYPES: frozenset[str] = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain", "application/json"}
)


@dataclass(frozen=True)
class RemoteAssetProvenance:
    """Where a fetched asset came from, with nothing secret in it.

    ``reference_hash`` is over the FULL url including its query, so two rows
    pointing at the same signed URL correlate without either being readable.
    """

    origin: str = "remote_url"
    scheme: str | None = None
    host: str | None = None
    path: str | None = None
    reference_hash: str | None = None
    declared_media_type: str | None = None
    detected_media_type: str | None = None
    redirected: bool | None = None

    def to_metadata(self) -> dict[str, Any]:
        """Representation metadata. Absent keys rather than null ones, so a
        document that did not come from a URL carries no remote fields."""
        declared = {
            "asset_origin": self.origin,
            "source_url_scheme": self.scheme,
            "source_url_host": self.host,
            "source_url_path": self.path,
            "url_reference_hash": self.reference_hash,
            "declared_media_type": self.declared_media_type,
            "detected_media_type": self.detected_media_type,
            "source_url_redirected": self.redirected,
        }

        return {key: value for key, value in declared.items() if value is not None}


def redact_url(url: str) -> str:
    """A URL safe to print. Query, fragment and credentials removed.

    The query string is where signed access lives, so it is dropped entirely
    rather than trimmed: a truncated token is still a leaked prefix.
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:  # pragma: no cover - defensive
        return "<unparseable-url>"

    host = (parts.hostname or "").lower()

    if not host:
        return "<no-host>"

    netloc = f"{host}:{parts.port}" if parts.port else host

    return urlunsplit((parts.scheme, netloc, parts.path or "", "", ""))


def url_reference_hash(url: str) -> str:
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()


def describe_url(url: str) -> RemoteAssetProvenance:
    """Redacted provenance for a URL, whether or not it was ever fetched."""
    try:
        parts = urlsplit(str(url))
    except ValueError:  # pragma: no cover - defensive
        return RemoteAssetProvenance(reference_hash=url_reference_hash(url))

    return RemoteAssetProvenance(
        scheme=(parts.scheme or "").lower() or None,
        host=(parts.hostname or "").lower() or None,
        path=parts.path or None,
        reference_hash=url_reference_hash(url),
    )


def coerce_url(value: Any) -> str | None:
    """A declared asset field's value as a URL string, or nothing.

    ``None``, empty and whitespace mean "this row has no asset" - a fact, not a
    failure. A number, list or dict means the declaration disagrees with the
    data; that is reported rather than stringified into a request.
    """
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, str):
        return None

    text = value.strip()

    return text or None


def _result(
    source_field: str,
    outcome: str,
    warning: str,
    provenance: RemoteAssetProvenance | None = None,
    size_bytes: int = 0,
) -> BinaryAssetResult:
    """A refusal or failure, carrying no URL beyond redacted provenance."""
    return BinaryAssetResult(
        source_field=source_field,
        outcome=outcome,
        size_bytes=size_bytes,
        warnings=(warning,),
    )


def fetch_remote_asset(
    value: Any,
    source_field: str,
    policy: Any = None,
    fetcher: Any = None,
    resolver: Any = None,
    options: BinaryAssetOptions | None = None,
) -> tuple[BinaryAssetResult, RemoteAssetProvenance | None]:
    """Resolve one declared remote reference into an extracted document.

    Never raises. A refused, unreachable or oversized asset is one field of one
    row; the row's scalar data is still perfectly good, and failing the job over
    it would be a worse answer than reporting it.

    Returns ``(result, provenance)``. The provenance is ``None`` when nothing
    was ever fetched, so a caller cannot accidentally attach remote metadata to
    a document that did not come from a URL.
    """
    from erp_pipeline.response_adaptation.assets import (
        DEFAULT_URL_POLICY,
        fetch_asset,
    )
    from erp_pipeline.response_adaptation.errors import (
        AssetFetchFailedError,
        AssetFetchRefusedError,
        AssetTooLargeError,
    )

    policy = policy if policy is not None else DEFAULT_URL_POLICY
    url = coerce_url(value)

    if url is None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return (
                BinaryAssetResult(
                    source_field=source_field,
                    outcome=RemoteAssetOutcome.EMPTY,
                    warnings=("the field held no asset reference",),
                ),
                None,
            )

        return (
            _result(
                source_field,
                RemoteAssetOutcome.NOT_A_URL_VALUE,
                f"the declared asset field held a {type(value).__name__} "
                "rather than a URL string; nothing was requested",
            ),
            None,
        )

    provenance = describe_url(url)

    # Refused here rather than by the policy, so an obviously malformed value
    # never reaches DNS resolution.
    if not provenance.scheme or not provenance.host:
        return (
            _result(
                source_field,
                RemoteAssetOutcome.INVALID_URL,
                "the declared asset reference is not an absolute URL with a "
                "scheme and host; nothing was requested",
                provenance,
            ),
            provenance,
        )

    try:
        fetched = fetch_asset(url, policy, fetcher=fetcher, resolver=resolver)
    except AssetFetchRefusedError as error:
        # `error.url` holds the RAW url, query string included. It is never
        # propagated: the rule name says which policy refused, and the redacted
        # provenance says where it pointed.
        rule = getattr(error, "rule", None) or "refused"
        outcome = (
            RemoteAssetOutcome.DISABLED
            if rule == "url_fetching_disabled"
            else RemoteAssetOutcome.REFUSED
        )

        return (
            _result(
                source_field,
                outcome,
                f"the asset URL was refused by policy ({rule})",
                provenance,
            ),
            provenance,
        )
    except AssetTooLargeError as error:
        return (
            _result(
                source_field,
                RemoteAssetOutcome.TOO_LARGE,
                "the fetched asset exceeds the configured size limit "
                f"({getattr(error, 'limit_bytes', None)} bytes)",
                provenance,
                size_bytes=int(getattr(error, "size_bytes", 0) or 0),
            ),
            provenance,
        )
    except AssetFetchFailedError as error:
        # The exception type only. A client's message can quote the URL.
        return (
            _result(
                source_field,
                RemoteAssetOutcome.FAILED,
                "the asset could not be retrieved "
                f"({_failure_label(error)})",
                provenance,
            ),
            provenance,
        )
    except Exception as error:  # noqa: BLE001 - one bad URL is one field
        return (
            _result(
                source_field,
                RemoteAssetOutcome.FAILED,
                f"the asset could not be retrieved ({type(error).__name__})",
                provenance,
            ),
            provenance,
        )

    declared = (getattr(fetched, "content_type", None) or "").split(";")[0].strip()
    redirected = bool(
        getattr(fetched, "final_url", None)
        and fetched.final_url != url
    )

    if declared.lower() in NON_ASSET_MEDIA_TYPES:
        # Refused on the CLAIM, before the bytes are classified. A server
        # returning an HTML error page is not offering a document, and reading
        # links out of it would be crawling.
        return (
            _result(
                source_field,
                RemoteAssetOutcome.UNSUPPORTED,
                f"the remote server returned {declared!r}, which is not a "
                "document or image asset",
                provenance,
                size_bytes=len(getattr(fetched, "body", b"") or b""),
            ),
            _with(provenance, declared_media_type=declared, redirected=redirected),
        )

    # From here the origin stops mattering: identical in-memory extraction to a
    # database BLOB, with the BYTES deciding the type rather than the header.
    result = extract_binary_asset(
        getattr(fetched, "body", b"") or b"", source_field, options
    )

    return (
        result,
        _with(
            provenance,
            declared_media_type=declared or None,
            detected_media_type=result.media_type,
            redirected=redirected,
        ),
    )


def _with(provenance: RemoteAssetProvenance, **updates: Any) -> RemoteAssetProvenance:
    from dataclasses import replace

    return replace(provenance, **updates)


def _failure_label(error: Any) -> str:
    """A safe reason for a failed fetch.

    The message a client raised may contain the URL, so only the exception's
    own recognisable shape is reported. A timeout is named because an operator
    reading a warning genuinely needs to tell it from a 404.
    """
    text = str(error).lower()

    for needle, label in (
        ("timeout", "timeout"),
        ("timed out", "timeout"),
        ("404", "not found"),
        ("403", "forbidden"),
        ("401", "unauthorized"),
        ("500", "server error"),
        ("status", "unexpected HTTP status"),
    ):
        if needle in text:
            return label

    return type(error).__name__


def declared_asset_fields(options: Mapping[str, Any] | None) -> dict[str, str | None]:
    """The fields a caller explicitly declared fetchable, and their document type.

    Accepts either shape, because both read naturally in a job request::

        "asset_url_fields": ["birth_certificate_url"]

        "asset_url_fields": {
            "birth_certificate_url": {"document_type": "birth_certificate"}
        }

    A bare list declares the field with NO document type rather than guessing
    one by stripping ``_url``: ``passport_url`` would survive that rule and
    ``photo_url_2019`` would not, and a silently wrong ``document_type`` is a
    filter that returns the wrong documents.
    """
    declared = (options or {}).get("asset_url_fields")

    if not declared:
        return {}

    if isinstance(declared, str):
        return {declared: None}

    if isinstance(declared, Mapping):
        resolved: dict[str, str | None] = {}

        for name, config in declared.items():
            if isinstance(config, Mapping):
                document_type = config.get("document_type")
                resolved[str(name)] = (
                    str(document_type) if document_type else None
                )
            elif isinstance(config, str) and config:
                resolved[str(name)] = config
            else:
                resolved[str(name)] = None

        return resolved

    if isinstance(declared, Sequence):
        return {str(name): None for name in declared if name}

    return {}


__all__ = [
    "NON_ASSET_MEDIA_TYPES",
    "RemoteAssetOutcome",
    "RemoteAssetProvenance",
    "coerce_url",
    "declared_asset_fields",
    "describe_url",
    "fetch_remote_asset",
    "redact_url",
    "url_reference_hash",
]
