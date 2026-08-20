"""Real fixture files for Phase 6 ingestion tests.

Two kinds of fixture, split on whether a human can review the file in a diff:

* **CSV** fixtures are committed as text under ``tests/fixtures/ingestion/``.
  They are the interesting cases (BOM, quoting, duplicate headers, malformed
  rows), and being able to read them in the repository is worth more than
  generating them.
* **Binary** fixtures - PDFs and images - are built at session scope into a
  temporary directory. Committing opaque blobs would make it impossible to
  review what is being tested, and both PyMuPDF and Pillow can produce them
  deterministically.

Nothing here is mocked. Every test parses a real file through the real parser
stack; the only thing these fixtures do is put real bytes on disk first.

All content is synthetic. The ``SECRET_*`` sentinels exist precisely so the
privacy tests can prove they never reach operational output.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _load_project_env() -> None:
    """Load ``.env`` at import time, before any OCR capability probe runs.

    This project configures the Tesseract executable through ``TESSERACT_CMD``
    (with ``TESSERACT_PATH`` as its older name), and the live database suites
    already load ``.env`` the same way. Doing it here - at module import rather
    than in a fixture - matters because ``test_image_ingestion`` probes OCR
    capability at import time to decide what it can assert, and a fixture would
    run too late to influence that.

    Production code never loads ``.env``: reading configuration files is an
    application concern, so ``ingestion.ocr`` only ever consults
    ``os.environ``.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a dependency
        return

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")


_load_project_env()

#: Synthetic sentinels planted in fixture content. They must appear in source
#: content (rows, page text) and never in schemas, warnings, logs or errors.
SENTINEL_EMAIL = "SECRET_CUSTOMER_EMAIL_92831"
SENTINEL_IBAN = "SECRET_IBAN_55231"
SENTINEL_INVOICE = "SECRET_INVOICE_88192"
SENTINELS = (SENTINEL_EMAIL, SENTINEL_IBAN, SENTINEL_INVOICE)

#: A minimal, hand-written PDF. Not produced by PyMuPDF, so at least one PDF
#: test exercises a file this project's own libraries did not author.
MINIMAL_PDF_BYTES = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 62>>stream
BT /F1 18 Tf 20 100 Td (HANDWRITTEN PDF FIXTURE) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
"""


@pytest.fixture(scope="session")
def pipeline_connector():
    """Connector to the pipeline database, for the catalog integration tests.

    A local twin of the discovery suite's fixture of the same name: pytest
    conftest files are directory-scoped, so a fixture defined under
    ``discovery/`` is not visible here. Skips - never fails, never fakes - when
    PostgreSQL is unreachable.
    """
    import os

    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.connectors.postgresql import PostgreSQLConnector
    from erp_pipeline.schemas.enums import SourceType

    password = os.getenv("AI_DB_PASSWORD")
    if not password:
        pytest.skip("AI_DB_PASSWORD is not configured in .env")

    settings = ConnectionSettings(
        source_system_id="file_ingestion_probe",
        source_type=SourceType.POSTGRESQL,
        host=os.getenv("AI_DB_HOST", "localhost"),
        port=int(os.getenv("AI_DB_PORT", "5432")),
        database=os.getenv("AI_DB_NAME", "erp_ai_native_db"),
        username=os.getenv("AI_DB_USER", "postgres"),
        password=password,
        connect_timeout_seconds=10,
    )

    connector = PostgreSQLConnector(settings)
    try:
        connector.test_connection()
    except Exception as exc:  # noqa: BLE001 - availability probe
        connector.close()
        pytest.skip(f"Pipeline PostgreSQL unreachable: {exc}")

    yield connector
    connector.close()


@pytest.fixture(scope="session")
def csv_fixtures() -> Path:
    """Directory of committed CSV fixture files."""
    directory = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "ingestion"

    if not directory.is_dir():  # pragma: no cover - repository layout guard
        pytest.fail(f"CSV fixture directory is missing: {directory}")

    return directory


@pytest.fixture(scope="session")
def binary_fixtures(tmp_path_factory) -> Path:
    """Build real PDF and image files once per session."""
    directory = tmp_path_factory.mktemp("ingestion_binary_fixtures")

    _write_minimal_pdf(directory)
    _write_pdf_fixtures(directory)
    _write_image_fixtures(directory)
    _write_corrupt_fixtures(directory)

    return directory


# ============================================================
# PDF fixtures
# ============================================================

def _write_minimal_pdf(directory: Path) -> None:
    (directory / "handwritten.pdf").write_bytes(MINIMAL_PDF_BYTES)


def _write_pdf_fixtures(directory: Path) -> None:
    """Real PDFs with a genuine text layer, written by PyMuPDF."""
    fitz = pytest.importorskip("fitz", reason="pymupdf is not installed")

    single = fitz.open()
    page = single.new_page(width=400, height=400)
    page.insert_text((50, 100), "INVOICE SUMMARY", fontsize=16)
    page.insert_text((50, 130), "Total due 4200", fontsize=12)
    single.set_metadata({"title": "Phase 6 Text PDF", "author": "Synthetic Fixture"})
    single.save(directory / "text_single_page.pdf")
    single.close()

    multi = fitz.open()
    for number in range(1, 4):
        page = multi.new_page(width=400, height=400)
        # Page-unique text, so page ORDER can be asserted rather than assumed.
        page.insert_text((50, 100), f"PAGE MARKER {number}", fontsize=16)
    multi.save(directory / "text_multi_page.pdf")
    multi.close()

    # Sentinels for the privacy tests.
    secret = fitz.open()
    page = secret.new_page(width=500, height=400)
    page.insert_text((30, 100), SENTINEL_EMAIL, fontsize=12)
    page.insert_text((30, 130), SENTINEL_IBAN, fontsize=12)
    page.insert_text((30, 160), SENTINEL_INVOICE, fontsize=12)
    secret.set_metadata({"title": SENTINEL_INVOICE})
    secret.save(directory / "sentinels.pdf")
    secret.close()

    # A structurally valid PDF with no text at all - the "no text detected"
    # case, distinct from "OCR unavailable".
    blank = fitz.open()
    blank.new_page(width=200, height=200)
    blank.save(directory / "blank.pdf")
    blank.close()

    # A page whose only content is a rasterized image: the scanned-PDF case
    # that exercises the OCR fallback path.
    scanned = fitz.open()
    page = scanned.new_page(width=400, height=200)
    page.insert_image(
        fitz.Rect(0, 0, 400, 200),
        stream=_render_text_png("SCANNED PAGE TEXT", width=400, height=200),
    )
    scanned.save(directory / "scanned_image_only.pdf")
    scanned.close()

    encrypted = fitz.open()
    page = encrypted.new_page(width=200, height=200)
    page.insert_text((20, 100), "LOCKED", fontsize=14)
    encrypted.save(
        directory / "encrypted.pdf",
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    encrypted.close()


# ============================================================
# Image fixtures
# ============================================================

def _render_text_png(text: str, width: int = 600, height: int = 200) -> bytes:
    """Render black text on white at a size Tesseract can actually read.

    OCR accuracy depends on glyph height in pixels, so the default font is
    scaled up rather than used at its tiny built-in size - otherwise a "real
    OCR" test would be testing nothing but a failure path.
    """
    import io

    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    font = None
    for candidate in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            font = ImageFont.truetype(candidate, 40)
            break
        except OSError:
            continue

    if font is None:  # pragma: no cover - depends on installed system fonts
        # The bitmap default font is small; render it scaled so OCR still has
        # usable glyph heights.
        small = Image.new("RGB", (width // 3, height // 3), "white")
        ImageDraw.Draw(small).text((5, 5), text, fill="black")
        image = small.resize((width, height), Image.LANCZOS)
    else:
        draw.text((20, height // 3), text, fill="black", font=font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_image_fixtures(directory: Path) -> None:
    pytest.importorskip("PIL", reason="Pillow is not installed")

    import io

    from PIL import Image

    (directory / "text.png").write_bytes(_render_text_png("INVOICE 4200"))

    # The same rendering as a JPEG, so both formats are exercised for real.
    png = Image.open(io.BytesIO(_render_text_png("INVOICE 4200")))
    png.convert("RGB").save(directory / "text.jpg", format="JPEG", quality=95)

    (directory / "sentinels.png").write_bytes(
        _render_text_png(SENTINEL_INVOICE, width=900, height=200)
    )

    Image.new("RGB", (320, 240), "white").save(directory / "blank.png")

    # WEBP, to prove format coverage beyond PNG/JPEG.
    Image.new("RGB", (100, 80), "white").save(directory / "small.webp", format="WEBP")


# ============================================================
# Corrupt and misnamed fixtures
# ============================================================

def _write_corrupt_fixtures(directory: Path) -> None:
    # Right signature, truncated body: passes detection, fails decoding - which
    # is exactly the case that must produce a controlled error.
    (directory / "corrupt.pdf").write_bytes(b"%PDF-1.4\n truncated garbage")
    (directory / "corrupt.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    )

    # A PDF wearing a .csv extension: the type-mismatch case.
    (directory / "actually_a_pdf.csv").write_bytes(MINIMAL_PDF_BYTES)

    # Binary content with no recognizable signature and no known extension.
    (directory / "mystery.bin").write_bytes(bytes(range(256)))

    # A .png that carries no PNG signature.
    (directory / "not_really.png").write_bytes(b"this is plain text, not a png")
