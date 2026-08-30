"""Multimodal assets and outbound-URL safety (Phase 14).

NO TEST HERE TOUCHES THE NETWORK. The resolver and the fetcher are both
injected, which is also how the production code is built - the package ships no
HTTP client at all, so importing it can never cause a request.

The images and PDFs are real bytes produced by Pillow and PyMuPDF at test time
rather than committed blobs, matching how the Phase 6 ingestion fixtures work.
"""

from __future__ import annotations

import io

import pytest

from erp_pipeline.response_adaptation.assets import (
    AssetAdapter,
    AssetOptions,
    FetchedAsset,
    UrlSafetyPolicy,
    fetch_asset,
    refused_asset,
    validate_asset_url,
)
from erp_pipeline.response_adaptation.errors import (
    AssetFetchFailedError,
    AssetFetchRefusedError,
    AssetTooLargeError,
)
from erp_pipeline.response_adaptation.models import AssetKind

PUBLIC = "93.184.216.34"


def resolver_for(*addresses: str):
    """A stand-in DNS resolver. Never queries anything."""
    return lambda host: list(addresses)


@pytest.fixture
def open_policy() -> UrlSafetyPolicy:
    return UrlSafetyPolicy(enabled=True)


@pytest.fixture
def png_bytes() -> bytes:
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (160, 60), "white").save(buffer, "PNG")

    return buffer.getvalue()


@pytest.fixture
def pdf_bytes() -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 96), "Invoice INV-204 total 45000.00 LKR")
    payload = document.tobytes()
    document.close()

    return payload


# ----------------------------------------------------------------------
# URL safety - the SSRF surface
# ----------------------------------------------------------------------


def test_fetching_is_refused_by_default():
    """The safe configuration is the one an operator gets by doing nothing."""
    with pytest.raises(AssetFetchRefusedError) as caught:
        validate_asset_url("https://cdn.example.com/a.png",
                           resolver=resolver_for(PUBLIC))

    assert caught.value.rule == "url_fetching_disabled"


def test_a_public_https_url_is_allowed(open_policy):
    result = validate_asset_url(
        "https://cdn.example.com/a.png", open_policy, resolver_for(PUBLIC)
    )

    assert result.host == "cdn.example.com"
    assert result.port == 443


@pytest.mark.parametrize(
    "url, addresses, rule",
    [
        # The cloud instance-metadata endpoint: the single highest-value SSRF
        # target, and the reason link-local is blocked rather than just private.
        ("https://169.254.169.254/latest/meta-data/", ["169.254.169.254"],
         "private_or_reserved_address"),
        ("https://localhost/a.png", ["127.0.0.1"], "private_or_reserved_address"),
        ("https://internal.erp/a.png", ["10.0.0.5"], "private_or_reserved_address"),
        ("https://intranet/a.png", ["192.168.1.10"], "private_or_reserved_address"),
        ("https://svc/a.png", ["172.16.4.4"], "private_or_reserved_address"),
        ("https://v6/a.png", ["::1"], "private_or_reserved_address"),
        # IPv4-mapped IPv6 is how a naive loopback check gets bypassed.
        ("https://mapped/a.png", ["::ffff:127.0.0.1"],
         "private_or_reserved_address"),
        # file:// and friends turn a fetcher into a local file reader.
        ("file:///etc/passwd", [], "scheme_not_allowed"),
        ("ftp://example.com/a.png", [PUBLIC], "scheme_not_allowed"),
        ("gopher://example.com/a", [PUBLIC], "scheme_not_allowed"),
        ("http://example.com/a.png", [PUBLIC], "scheme_not_allowed"),
        # A database port is not an asset port.
        ("https://example.com:5432/a.png", [PUBLIC], "port_not_allowed"),
        ("https://example.com:6379/a.png", [PUBLIC], "port_not_allowed"),
        ("https://user:pass@example.com/a.png", [PUBLIC], "credentials_in_url"),
        ("https:///a.png", [], "no_host"),
    ],
)
def test_dangerous_urls_are_refused_with_a_named_rule(
    url, addresses, rule, open_policy
):
    with pytest.raises(AssetFetchRefusedError) as caught:
        validate_asset_url(url, open_policy, resolver_for(*addresses))

    assert caught.value.rule == rule


def test_every_resolved_address_is_checked_not_just_the_first(open_policy):
    """A DNS entry mixing a public and a loopback address would otherwise pass
    validation and then connect to whichever the OS happened to choose."""
    with pytest.raises(AssetFetchRefusedError) as caught:
        validate_asset_url(
            "https://mixed.example.com/a.png",
            open_policy,
            resolver_for(PUBLIC, "127.0.0.1"),
        )

    assert caught.value.rule == "private_or_reserved_address"


def test_a_host_allow_list_refuses_everything_else(open_policy):
    from dataclasses import replace

    policy = replace(open_policy, allowed_hosts=frozenset({"cdn.example.com"}))

    validate_asset_url("https://cdn.example.com/a.png", policy,
                       resolver_for(PUBLIC))

    with pytest.raises(AssetFetchRefusedError) as caught:
        validate_asset_url("https://other.example.com/a.png", policy,
                           resolver_for(PUBLIC))

    assert caught.value.rule == "host_not_in_allow_list"


def test_a_missing_fetcher_refuses_rather_than_improvising(open_policy):
    with pytest.raises(AssetFetchRefusedError):
        fetch_asset("https://cdn.example.com/a.png", open_policy,
                    fetcher=None, resolver=resolver_for(PUBLIC))


def test_a_redirect_to_a_forbidden_address_is_refused(open_policy, png_bytes):
    """An allowed host redirecting to the metadata endpoint is the standard
    way an SSRF filter that only validates the FIRST url gets bypassed."""
    def fetcher(validated):
        return FetchedAsset(png_bytes, "image/png",
                            "https://169.254.169.254/latest/meta-data/")

    with pytest.raises(AssetFetchRefusedError) as caught:
        fetch_asset("https://cdn.example.com/a.png", open_policy, fetcher,
                    resolver_for(PUBLIC))

    assert caught.value.rule == "too_many_redirects"


def test_an_oversized_fetch_is_refused(open_policy):
    from dataclasses import replace

    policy = replace(open_policy, max_bytes=64)

    with pytest.raises(AssetTooLargeError):
        fetch_asset(
            "https://cdn.example.com/a.png",
            policy,
            lambda validated: FetchedAsset(b"\x00" * 500, "image/png"),
            resolver_for(PUBLIC),
        )


def test_a_client_failure_becomes_a_typed_asset_error(open_policy):
    def broken(validated):
        raise TimeoutError("connection timed out")

    with pytest.raises(AssetFetchFailedError):
        fetch_asset("https://cdn.example.com/a.png", open_policy, broken,
                    resolver_for(PUBLIC))


# ----------------------------------------------------------------------
# Adapting bytes
# ----------------------------------------------------------------------


def test_an_image_is_described_and_marked_directly_readable(png_bytes):
    asset = AssetAdapter().adapt_bytes(png_bytes, "image/png", label="receipt")

    assert asset.kind is AssetKind.IMAGE
    assert asset.mime_type == "image/png"
    assert (asset.width, asset.height) == (160, 60)
    assert asset.llm_directly_readable is True
    assert asset.content_hash


def test_a_pdf_yields_text_and_is_not_directly_readable(pdf_bytes):
    """What reaches the model is the extracted text. Claiming otherwise would
    invite a caller to hand over bytes no model accepts."""
    asset = AssetAdapter().adapt_bytes(pdf_bytes, "application/pdf")

    assert asset.kind is AssetKind.DOCUMENT
    assert asset.llm_directly_readable is False
    assert "INV-204" in (asset.text or "")
    assert asset.page_count == 1
    assert asset.page_range == (1, 1)


def test_no_asset_ever_carries_raw_bytes(png_bytes):
    """The whole point of the phase is to stop a blob reaching the context."""
    asset = AssetAdapter().adapt_bytes(png_bytes, "image/png")
    payload = asset.to_dict()

    assert not any(
        isinstance(value, (bytes, bytearray)) for value in payload.values()
    )


def test_an_unsupported_binary_is_described_rather_than_guessed_at():
    asset = AssetAdapter().adapt_bytes(b"PK\x03\x04" + b"\x00" * 100,
                                       "application/zip")

    assert asset.kind is AssetKind.UNSUPPORTED_BINARY
    assert asset.llm_directly_readable is False
    assert asset.text is None
    assert asset.size_bytes == 104
    assert asset.warnings


def test_mislabelled_bytes_are_routed_by_content_and_the_lie_is_reported(pdf_bytes):
    asset = AssetAdapter().adapt_bytes(pdf_bytes, "image/png")

    assert asset.kind is AssetKind.DOCUMENT
    assert any("does not match" in warning for warning in asset.warnings)


def test_an_undecodable_image_degrades_instead_of_failing():
    """A corrupt attachment must not invalidate the response it arrived with."""
    asset = AssetAdapter().adapt_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
                                       "image/png")

    assert asset.kind is AssetKind.UNSUPPORTED_BINARY
    assert any("could not be decoded" in warning for warning in asset.warnings)


def test_an_oversized_payload_is_refused_before_extraction():
    from dataclasses import replace

    adapter = AssetAdapter(replace(AssetOptions(), max_bytes=32))

    with pytest.raises(AssetTooLargeError):
        adapter.adapt_bytes(b"\x00" * 100)


def test_extracted_text_is_bounded(pdf_bytes):
    from dataclasses import replace

    adapter = AssetAdapter(replace(AssetOptions(), max_text_chars=12))
    asset = adapter.adapt_bytes(pdf_bytes, "application/pdf")

    assert len(asset.text or "") <= 12
    assert asset.truncated


def test_a_refused_url_becomes_a_placeholder_not_an_omission():
    """A caller comparing a response against its asset list has to be able to
    see that something was referenced and deliberately not retrieved."""
    asset = refused_asset("https://internal/a.png", "blocked by policy")

    assert asset.kind is AssetKind.REFUSED
    assert asset.source_url == "https://internal/a.png"
    assert asset.llm_directly_readable is False


def test_a_permitted_url_is_fetched_through_the_injected_client(
    open_policy, png_bytes
):
    from dataclasses import replace

    adapter = AssetAdapter(replace(AssetOptions(), url_policy=open_policy))
    asset = adapter.adapt_url(
        "https://cdn.example.com/a.png",
        fetcher=lambda validated: FetchedAsset(png_bytes, "image/png"),
        resolver=resolver_for(PUBLIC),
        label="receipt",
    )

    assert asset.kind is AssetKind.IMAGE
    assert asset.source_url == "https://cdn.example.com/a.png"
    assert asset.label == "receipt"
