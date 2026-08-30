"""Phase 8 - ERP rows that point at a document instead of carrying it.

Every test here injects a fetcher. Nothing in this file opens a socket, and the
production package ships no HTTP client at all, so importing it cannot cause a
request either.

The tests that matter most are the refusals. A pipeline that fetches URLs chosen
by database rows is sitting in the SSRF position, and the value of this phase is
almost entirely in what it declines to do.
"""

from __future__ import annotations

import io
import json

import pytest

from erp_pipeline.ingestion.binary_assets import BinaryAssetOutcome
from erp_pipeline.ingestion.remote_assets import (
    NON_ASSET_MEDIA_TYPES,
    RemoteAssetOutcome,
    coerce_url,
    declared_asset_fields,
    describe_url,
    fetch_remote_asset,
    redact_url,
    url_reference_hash,
)
from erp_pipeline.response_adaptation.assets import FetchedAsset, UrlSafetyPolicy

SECRET = "SUPERSECRET"
SIGNED_URL = f"https://assets.example.test/cert.pdf?token={SECRET}&expires=1735689600"
PUBLIC_ADDRESS = "93.184.216.34"


def pdf_bytes(text: str = "BIRTH CERTIFICATE Nimal Silva") -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    document.new_page().insert_text((72, 96), text)
    payload = document.tobytes()
    document.close()

    return payload


def image_of_text(text: str) -> bytes:
    fitz = pytest.importorskip("pymupdf")
    typed = fitz.open()
    typed.new_page(width=420, height=180).insert_text((28, 100), text, fontsize=26)
    bitmap = typed.load_page(0).get_pixmap(dpi=300).tobytes("png")
    typed.close()

    return bitmap


@pytest.fixture
def enabled_policy():
    """Fetching ON, https only, one redirect. What a deployment opts into."""
    return UrlSafetyPolicy(enabled=True, max_redirects=1)


class RecordingFetcher:
    """A fetcher that remembers whether it was ever called.

    The recording is the point: a test asserting "refused" must also prove no
    socket would have been opened, not merely that the result said no.
    """

    def __init__(self, body: bytes = b"", content_type=None, final_url=None):
        self.body = body
        self.content_type = content_type
        self.final_url = final_url
        self.calls: list[str] = []

    def __call__(self, validated):
        self.calls.append(validated.url)

        return FetchedAsset(
            body=self.body,
            content_type=self.content_type,
            final_url=self.final_url,
        )

    @property
    def called(self) -> bool:
        return bool(self.calls)


def public_resolver(host):
    return (PUBLIC_ADDRESS,)


def resolver_for(address):
    def resolve(host):
        return (address,)

    return resolve


# ======================================================================
# TEST A / B / D - the paths that should work
# ======================================================================


def test_a_remote_pdf_is_fetched_and_extracted(enabled_policy):
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, provenance = fetch_remote_asset(
        "https://assets.example.test/cert.pdf", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert fetcher.called
    assert result.outcome == BinaryAssetOutcome.EXTRACTED
    assert result.media_type == "application/pdf"
    assert "BIRTH CERTIFICATE" in "".join(p.text for p in result.document.pages)
    assert provenance.host == "assets.example.test"


def test_a_remote_image_is_ocred(enabled_policy):
    from erp_pipeline.ingestion.ocr import probe_ocr

    if not probe_ocr().available:
        pytest.skip("OCR is unavailable on this machine")

    fetcher = RecordingFetcher(image_of_text("BIRTH CERTIFICATE"), "image/png")
    result, _ = fetch_remote_asset(
        "https://assets.example.test/scan.png", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert result.outcome == BinaryAssetOutcome.EXTRACTED
    assert result.media_type == "image/png"
    assert result.ocr_used is True


def test_an_octet_stream_pdf_is_classified_by_its_bytes(enabled_policy):
    """TEST D: a generic content type does not prevent correct routing."""
    fetcher = RecordingFetcher(pdf_bytes(), "application/octet-stream")
    result, provenance = fetch_remote_asset(
        "https://assets.example.test/blob", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert result.media_type == "application/pdf"
    assert provenance.declared_media_type == "application/octet-stream"
    assert provenance.detected_media_type == "application/pdf"


# ======================================================================
# TEST C - the server's claim is not authoritative
# ======================================================================


def test_a_lying_content_type_does_not_decide_the_format(enabled_policy):
    """Declared image/jpeg, actually a PDF. The bytes win."""
    fetcher = RecordingFetcher(pdf_bytes(), "image/jpeg")
    result, provenance = fetch_remote_asset(
        "https://assets.example.test/photo.jpg", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert result.media_type == "application/pdf"
    assert provenance.declared_media_type == "image/jpeg"


def test_a_zip_claiming_to_be_a_pdf_is_refused(enabled_policy):
    """TEST N."""
    fetcher = RecordingFetcher(b"PK\x03\x04" + b"\x00" * 200, "application/pdf")
    result, _ = fetch_remote_asset(
        "https://assets.example.test/c.pdf", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert result.outcome == BinaryAssetOutcome.UNSUPPORTED
    assert result.document is None


# ======================================================================
# TEST E / F / G - private and internal targets
# ======================================================================


@pytest.mark.parametrize(
    "label, address",
    [
        ("loopback v4", "127.0.0.1"),
        ("loopback v6", "::1"),
        ("cloud metadata", "169.254.169.254"),
        ("rfc1918 10/8", "10.0.0.5"),
        ("rfc1918 192.168", "192.168.1.10"),
        ("rfc1918 172.16", "172.16.0.9"),
        ("link-local v6", "fe80::1"),
        ("ipv4-mapped loopback", "::ffff:127.0.0.1"),
        ("unspecified", "0.0.0.0"),
    ],
)
def test_a_host_resolving_to_an_internal_address_is_refused_before_any_socket(
    enabled_policy, label, address
):
    """The URL is https, so this proves the ADDRESS check - not the scheme check.

    ``fetcher.called`` is asserted false: the refusal must happen before a
    connection, not after one that returned something harmless.
    """
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        "https://assets.internal.test/c.pdf", "birth_certificate_url",
        enabled_policy, fetcher, resolver_for(address),
    )

    assert result.outcome == RemoteAssetOutcome.REFUSED
    assert fetcher.called is False, f"{label}: a socket would have been opened"


def test_a_public_address_is_permitted(enabled_policy):
    """The negative tests would pass trivially if everything were refused."""
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        "https://assets.public.test/c.pdf", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert fetcher.called is True
    assert result.outcome == BinaryAssetOutcome.EXTRACTED


def test_one_bad_address_among_several_refuses_the_host(enabled_policy):
    """DNS returning a public AND a loopback address is a rebinding setup."""
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        "https://assets.internal.test/c.pdf", "birth_certificate_url",
        enabled_policy, fetcher, lambda host: (PUBLIC_ADDRESS, "127.0.0.1"),
    )

    assert result.outcome == RemoteAssetOutcome.REFUSED
    assert fetcher.called is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://assets.example.test/c.pdf",
        "gopher://assets.example.test/1",
        "data:application/pdf;base64,JVBERi0x",
        "javascript:alert(1)",
        "s3://bucket/key.pdf",
    ],
)
def test_a_non_https_scheme_is_refused(enabled_policy, url):
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        url, "birth_certificate_url", enabled_policy, fetcher, public_resolver
    )

    assert result.outcome in {
        RemoteAssetOutcome.REFUSED, RemoteAssetOutcome.INVALID_URL
    }
    assert fetcher.called is False


def test_credentials_in_the_url_are_refused(enabled_policy):
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        "https://user:pass@assets.example.test/c.pdf", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert result.outcome == RemoteAssetOutcome.REFUSED
    assert fetcher.called is False


def test_a_host_allow_list_excludes_everything_else():
    policy = UrlSafetyPolicy(
        enabled=True, allowed_hosts=frozenset({"assets.example.test"})
    )
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        "https://somewhere.else.test/c.pdf", "birth_certificate_url",
        policy, fetcher, public_resolver,
    )

    assert result.outcome == RemoteAssetOutcome.REFUSED
    assert fetcher.called is False


# ======================================================================
# TEST H / I - redirects
# ======================================================================


def test_a_redirect_to_an_internal_address_is_refused(enabled_policy):
    """The second destination gets the same scrutiny as the first."""
    fetcher = RecordingFetcher(
        pdf_bytes(), "application/pdf",
        final_url="https://internal.test/private.pdf",
    )

    def resolve(host):
        return ("127.0.0.1",) if host == "internal.test" else (PUBLIC_ADDRESS,)

    result, _ = fetch_remote_asset(
        "https://assets.public.test/c.pdf", "birth_certificate_url",
        enabled_policy, fetcher, resolve,
    )

    assert result.outcome == RemoteAssetOutcome.REFUSED
    assert result.document is None


def test_a_redirect_is_refused_when_redirects_are_not_permitted():
    policy = UrlSafetyPolicy(enabled=True, max_redirects=0)
    fetcher = RecordingFetcher(
        pdf_bytes(), "application/pdf",
        final_url="https://elsewhere.public.test/c.pdf",
    )
    result, _ = fetch_remote_asset(
        "https://assets.public.test/c.pdf", "birth_certificate_url",
        policy, fetcher, public_resolver,
    )

    assert result.outcome == RemoteAssetOutcome.REFUSED


def test_a_permitted_redirect_to_a_public_host_succeeds(enabled_policy):
    fetcher = RecordingFetcher(
        pdf_bytes(), "application/pdf",
        final_url="https://cdn.public.test/c.pdf",
    )
    result, provenance = fetch_remote_asset(
        "https://assets.public.test/c.pdf", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert result.outcome == BinaryAssetOutcome.EXTRACTED
    assert provenance.redirected is True


# ======================================================================
# TEST J / K / L - failure handling
# ======================================================================


def test_an_oversized_response_is_refused(enabled_policy):
    policy = UrlSafetyPolicy(enabled=True, max_bytes=64)
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        "https://assets.public.test/c.pdf", "birth_certificate_url",
        policy, fetcher, public_resolver,
    )

    assert result.outcome == RemoteAssetOutcome.TOO_LARGE
    assert result.document is None


def test_a_timeout_is_reported_as_a_timeout(enabled_policy):
    def slow(validated):
        raise TimeoutError("read timed out")

    result, _ = fetch_remote_asset(
        "https://assets.public.test/c.pdf", "birth_certificate_url",
        enabled_policy, slow, public_resolver,
    )

    assert result.outcome == RemoteAssetOutcome.FAILED
    assert "timeout" in result.warnings[0]
    assert result.document is None


def test_a_remote_404_is_not_indexed_as_content(enabled_policy):
    def not_found(validated):
        raise RuntimeError("unexpected status 404 for the asset")

    result, _ = fetch_remote_asset(
        "https://assets.public.test/missing.pdf", "birth_certificate_url",
        enabled_policy, not_found, public_resolver,
    )

    assert result.outcome == RemoteAssetOutcome.FAILED
    assert result.document is None


def test_a_client_exception_never_leaks_its_message(enabled_policy):
    """A client's error text can quote the full URL."""
    def explode(validated):
        raise RuntimeError(f"connection failed for {SIGNED_URL}")

    result, _ = fetch_remote_asset(
        SIGNED_URL, "birth_certificate_url", enabled_policy, explode,
        public_resolver,
    )

    assert SECRET not in " ".join(result.warnings)


# ======================================================================
# TEST M - HTML is not a document asset
# ======================================================================


@pytest.mark.parametrize("media_type", sorted(NON_ASSET_MEDIA_TYPES))
def test_a_web_page_is_not_indexed(enabled_policy, media_type):
    """Phase 8 is asset retrieval, not web crawling."""
    fetcher = RecordingFetcher(
        b"<html><body><a href='/next'>link</a></body></html>", media_type
    )
    result, _ = fetch_remote_asset(
        "https://assets.public.test/page", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert result.outcome == BinaryAssetOutcome.UNSUPPORTED
    assert result.document is None


def test_html_with_a_charset_parameter_is_still_refused(enabled_policy):
    fetcher = RecordingFetcher(b"<html></html>", "text/html; charset=utf-8")
    result, _ = fetch_remote_asset(
        "https://assets.public.test/page", "birth_certificate_url",
        enabled_policy, fetcher, public_resolver,
    )

    assert result.outcome == BinaryAssetOutcome.UNSUPPORTED


# ======================================================================
# TEST X - secure by default
# ======================================================================


def test_fetching_is_disabled_unless_a_deployment_enables_it():
    """The safe configuration is the one you get by configuring nothing."""
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        "https://assets.public.test/c.pdf", "birth_certificate_url",
        None, fetcher, public_resolver,
    )

    assert result.outcome == RemoteAssetOutcome.DISABLED
    assert fetcher.called is False


def test_no_fetcher_configured_is_a_refusal_not_a_request(enabled_policy):
    result, _ = fetch_remote_asset(
        "https://assets.public.test/c.pdf", "birth_certificate_url",
        enabled_policy, None, public_resolver,
    )

    assert result.outcome == RemoteAssetOutcome.DISABLED


def test_the_package_ships_no_http_client():
    """Importing this module must not be able to cause a request."""
    import ast
    import inspect

    from erp_pipeline.ingestion import remote_assets

    tree = ast.parse(inspect.getsource(remote_assets))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    for client in ("requests", "httpx", "urllib3", "aiohttp"):
        assert client not in imported


# ======================================================================
# TEST O / Y - the URL never travels with the content
# ======================================================================


def test_a_signed_url_is_redacted_everywhere_on_success(enabled_policy):
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, provenance = fetch_remote_asset(
        SIGNED_URL, "birth_certificate_url", enabled_policy, fetcher,
        public_resolver,
    )
    surface = json.dumps(
        {
            "result": result.to_dict(),
            "provenance": provenance.to_metadata(),
            "warnings": list(result.warnings),
            "text": "".join(p.text for p in result.document.pages),
        }
    )

    assert SECRET not in surface
    assert "token=" not in surface
    assert "expires=" not in surface


@pytest.mark.parametrize(
    "policy_kwargs, fetcher_body",
    [
        ({"enabled": False}, b""),
        ({"enabled": True, "max_bytes": 8}, b"%PDF-1.7 xxxxxxxxxxxxxxxx"),
    ],
)
def test_a_signed_url_is_redacted_on_the_failure_paths_too(
    policy_kwargs, fetcher_body
):
    policy = UrlSafetyPolicy(**policy_kwargs)
    fetcher = RecordingFetcher(fetcher_body, "application/pdf")
    result, provenance = fetch_remote_asset(
        SIGNED_URL, "birth_certificate_url", policy, fetcher, public_resolver
    )
    surface = json.dumps(
        {
            "result": result.to_dict(),
            "provenance": provenance.to_metadata() if provenance else {},
            "warnings": list(result.warnings),
        }
    )

    assert SECRET not in surface


def test_redact_url_drops_the_whole_query_not_part_of_it():
    """A truncated token is still a leaked prefix."""
    redacted = redact_url(SIGNED_URL)

    assert redacted == "https://assets.example.test/cert.pdf"
    assert "?" not in redacted
    assert SECRET not in redacted


def test_redact_url_removes_embedded_credentials():
    redacted = redact_url("https://user:hunter2@assets.example.test/c.pdf")

    assert "hunter2" not in redacted
    assert "user" not in redacted


def test_the_reference_hash_covers_the_full_url():
    """Two rows pointing at the same signed URL correlate without being read."""
    assert url_reference_hash(SIGNED_URL) == url_reference_hash(SIGNED_URL)
    assert url_reference_hash(SIGNED_URL) != url_reference_hash(
        "https://assets.example.test/cert.pdf"
    )
    assert SECRET not in url_reference_hash(SIGNED_URL)


def test_provenance_never_contains_the_query_string():
    provenance = describe_url(SIGNED_URL)

    assert provenance.path == "/cert.pdf"
    assert SECRET not in json.dumps(provenance.to_metadata())


# ======================================================================
# TEST Q / value handling
# ======================================================================


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t "])
def test_an_absent_reference_is_not_a_failure(enabled_policy, value):
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, provenance = fetch_remote_asset(
        value, "birth_certificate_url", enabled_policy, fetcher, public_resolver
    )

    assert result.outcome == BinaryAssetOutcome.EMPTY
    assert provenance is None
    assert fetcher.called is False


@pytest.mark.parametrize("value", [123, 4.5, True, ["https://x/y"], {"url": "x"}])
def test_a_non_string_value_is_refused_not_stringified(enabled_policy, value):
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        value, "birth_certificate_url", enabled_policy, fetcher, public_resolver
    )

    assert result.outcome == RemoteAssetOutcome.NOT_A_URL_VALUE
    assert fetcher.called is False


@pytest.mark.parametrize(
    "value", ["not a url", "/documents/cert.pdf", "certificate.pdf", "://broken"]
)
def test_a_value_that_is_not_an_absolute_url_is_refused(enabled_policy, value):
    """Relative paths are NOT resolved against a guessed ERP host."""
    fetcher = RecordingFetcher(pdf_bytes(), "application/pdf")
    result, _ = fetch_remote_asset(
        value, "birth_certificate_url", enabled_policy, fetcher, public_resolver
    )

    assert result.outcome == RemoteAssetOutcome.INVALID_URL
    assert fetcher.called is False


def test_coerce_url_accepts_only_strings():
    assert coerce_url("  https://x/y  ") == "https://x/y"
    assert coerce_url(None) is None
    assert coerce_url(123) is None
    assert coerce_url(True) is None


# ======================================================================
# Declaration parsing
# ======================================================================


def test_a_list_declares_fields_with_no_document_type():
    """``passport_url`` stripped of ``_url`` is a guess, not a classification."""
    declared = declared_asset_fields(
        {"asset_url_fields": ["birth_certificate_url", "contract_url"]}
    )

    assert declared == {"birth_certificate_url": None, "contract_url": None}


def test_a_mapping_declares_an_explicit_document_type():
    declared = declared_asset_fields(
        {
            "asset_url_fields": {
                "birth_certificate_url": {"document_type": "birth_certificate"}
            }
        }
    )

    assert declared == {"birth_certificate_url": "birth_certificate"}


def test_a_shorthand_mapping_value_is_the_document_type():
    declared = declared_asset_fields(
        {"asset_url_fields": {"birth_certificate_url": "birth_certificate"}}
    )

    assert declared == {"birth_certificate_url": "birth_certificate"}


@pytest.mark.parametrize(
    "options", [None, {}, {"asset_url_fields": []}, {"asset_url_fields": None},
                {"key_fields": ["employee_id"]}]
)
def test_nothing_is_declared_by_default(options):
    assert declared_asset_fields(options) == {}
