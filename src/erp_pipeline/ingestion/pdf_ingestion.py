"""PDF ingestion: text extraction with page provenance and OCR fallback.

Uses PyMuPDF (``fitz``), already a project dependency and already the PDF
engine of the Phase 0 prototype. It is imported lazily so that importing
``erp_pipeline.ingestion`` never requires it, matching how every optional
driver is handled in the connectors package.

What this does and does not do
------------------------------
It extracts text, page by page, and preserves which page each piece came from.
It makes no attempt to classify a document, detect an invoice, locate a total,
or identify a table. A PDF is not forced into a ``SourceSchema``: it has no
columns, and inventing some would be a fabrication every later phase would
then have to work around.

Page boundaries are preserved because a later retrieval phase must be able to
cite them. Merging pages into one string is a one-way loss.

READ-ONLY: files are opened for reading; nothing is written, rendered to disk,
or modified.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from erp_pipeline.ingestion.errors import EncryptedPDFError, MalformedPDFError
from erp_pipeline.ingestion.models import (
    ExtractedDocument,
    ExtractedPage,
    ExtractionStatus,
    ExtractionWarning,
    FileProvenance,
    FileSource,
    PdfOptions,
)
from erp_pipeline.ingestion.ocr import OcrCapability, probe_ocr, run_ocr
from erp_pipeline.ingestion.safety import TextBudget, truncate_text

EXTRACTOR_NAME = "pymupdf"

#: PDF metadata keys worth keeping. Treated as CONTENT rather than operational
#: metadata - a document title routinely contains a customer or project name -
#: so they live on ExtractedDocument.document_metadata and are excluded from
#: to_dict() unless text is explicitly requested.
_METADATA_KEYS = ("title", "author", "subject", "keywords", "creator", "producer")


class PdfFileIngestion:
    """Extracts text and page structure from one PDF."""

    def __init__(
        self,
        file: FileSource,
        options: PdfOptions | None = None,
        tesseract_cmd: str | None = None,
    ) -> None:
        self._file = file
        self._options = options or PdfOptions()
        self._tesseract_cmd = tesseract_cmd
        self._warnings: list[ExtractionWarning] = []
        self._ocr: OcrCapability | None = None

    @property
    def warnings(self) -> tuple[ExtractionWarning, ...]:
        return tuple(self._warnings)

    def ingest(self) -> ExtractedDocument:
        fitz = _import_pymupdf()
        options = self._options

        document = self._open(fitz)

        try:
            if document.needs_pass:
                # No attempt is made to bypass, crack or guess the password.
                raise EncryptedPDFError(
                    f"{self._file.original_filename!r} is password-protected. "
                    "Phase 6 does not attempt to decrypt PDFs; supply an "
                    "already-decrypted copy."
                )

            total_pages = document.page_count
            page_limit = min(total_pages, options.max_pages)

            if total_pages > options.max_pages:
                self._warn(
                    "page_limit",
                    f"The document has {total_pages} pages; only the first "
                    f"{options.max_pages} were extracted.",
                )

            budget = TextBudget(options.max_total_text_chars)
            pages = tuple(
                self._extract_page(document, index, budget)
                for index in range(page_limit)
            )

            if budget.exhausted:
                self._warn(
                    "text_budget",
                    "Extraction stopped early: the document exceeded the "
                    f"configured budget of {options.max_total_text_chars} "
                    "characters.",
                )

            metadata = self._document_metadata(document)

        finally:
            document.close()

        return ExtractedDocument(
            file=self._file,
            provenance=self._provenance(total_pages),
            pages=pages,
            status=self._overall_status(pages, total_pages > page_limit or
                                        budget.exhausted),
            page_count=total_pages,
            document_metadata=metadata,
            warnings=self.warnings,
        )

    # ------------------------------------------------------------
    # Page extraction
    # ------------------------------------------------------------

    def _extract_page(self, document: Any, index: int,
                      budget: TextBudget) -> ExtractedPage:
        page_number = index + 1

        try:
            page = document.load_page(index)
            raw_text = page.get_text("text") or ""
        except Exception as exc:
            # One unreadable page must not lose the other 200.
            self._warn(
                "page_extraction_failed",
                f"The page could not be read ({type(exc).__name__}).",
                page_number=page_number,
            )
            return ExtractedPage(
                page_number=page_number, text="", status=ExtractionStatus.FAILED,
                extraction_method="none",
            )

        method = "text_layer"
        stripped = raw_text.strip()

        if len(stripped) < self._options.ocr_min_text_chars:
            # A page with little or no text layer is probably a scan. OCR is
            # the only way to read one, and its absence must be reported -
            # but NOT at the cost of whatever the text layer did yield.
            ocr_text, ocr_status = self._ocr_page(page, page_number, bool(stripped))

            if ocr_status is not None:
                # Terminal only when the text layer was empty too. A page that
                # produced real text keeps it even though OCR could not run.
                return ExtractedPage(
                    page_number=page_number, text="", status=ocr_status,
                    extraction_method="none",
                )

            if ocr_text is not None and len(ocr_text.strip()) > len(stripped):
                # OCR is preferred only when it actually recovered more than
                # the text layer did; a worse OCR pass never overwrites real
                # embedded text.
                raw_text = ocr_text
                method = "ocr"

        text, page_truncated = truncate_text(
            raw_text, self._options.max_text_chars_per_page
        )
        if page_truncated:
            self._warn(
                "page_text_truncated",
                "The page's text exceeded the configured per-page character "
                f"limit of {self._options.max_text_chars_per_page}.",
                page_number=page_number,
            )

        text, budget_truncated = budget.take(text)

        status = (
            ExtractionStatus.EXTRACTED if text.strip()
            else ExtractionStatus.NO_CONTENT_DETECTED
        )

        return ExtractedPage(
            page_number=page_number,
            text=text,
            status=status,
            extraction_method=method if text else "none",
            char_count=len(text),
            truncated=page_truncated or budget_truncated,
        )

    def _ocr_page(
        self, page: Any, page_number: int, has_text_layer: bool
    ) -> tuple[str | None, ExtractionStatus | None]:
        """Attempt OCR on a page whose text layer looks too thin.

        Returns ``(text, terminal_status)``. A non-None status means the page
        is finished and unreadable - which is only ever returned when the text
        layer produced nothing either. ``has_text_layer`` is what keeps a
        partially-readable page from being thrown away because OCR happened to
        be unavailable.
        """
        if not self._options.ocr_fallback:
            return None, None

        capability = self._ocr_capability()

        if not capability.available:
            if has_text_layer:
                # Some text WAS recovered. Report the missed opportunity and
                # keep what the text layer gave us.
                self._warn(
                    "ocr_unavailable_low_text",
                    "The page has only a small amount of embedded text and OCR "
                    f"is unavailable, so any scanned portion was not read: "
                    f"{capability.reason}",
                    page_number=page_number,
                )
                return None, None

            self._warn(
                "ocr_unavailable",
                f"The page has no text layer and OCR is unavailable: "
                f"{capability.reason}",
                page_number=page_number,
            )
            return None, ExtractionStatus.OCR_UNAVAILABLE

        try:
            image = self._render_page(page)
        except Exception as exc:
            self._warn(
                "page_render_failed",
                f"The page could not be rendered for OCR "
                f"({type(exc).__name__}).",
                page_number=page_number,
            )
            return None, None if has_text_layer else ExtractionStatus.FAILED

        try:
            return (
                run_ocr(image, self._options.ocr_language, self._tesseract_cmd),
                None,
            )
        except RuntimeError as exc:
            # The message names the engine and exception class only.
            self._warn("ocr_failed", str(exc), page_number=page_number)
            return None, None if has_text_layer else ExtractionStatus.FAILED

    def _render_page(self, page: Any) -> Any:
        """Rasterize a page in memory for OCR.

        Nothing is written to disk: the pixmap goes straight into a PIL image
        through a bytes buffer, so a scanned page's contents never touch the
        filesystem.
        """
        import io

        from PIL import Image

        pixmap = page.get_pixmap(dpi=self._options.ocr_render_dpi)
        return Image.open(io.BytesIO(pixmap.tobytes("png")))

    # ------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------

    def _document_metadata(self, document: Any) -> dict[str, Any]:
        if not self._options.include_document_metadata:
            return {}

        try:
            raw = document.metadata or {}
        except Exception:  # pragma: no cover - defensive
            return {}

        return {
            key: str(raw[key])
            for key in _METADATA_KEYS
            if raw.get(key)
        }

    def _provenance(self, page_count: int) -> FileProvenance:
        capability = self._ocr
        return FileProvenance(
            file_id=self._file.file_id,
            content_hash=self._file.content_hash,
            original_filename=self._file.original_filename,
            file_type=self._file.file_type,
            media_type=self._file.media_type,
            size_bytes=self._file.size_bytes,
            extractor=EXTRACTOR_NAME,
            page_count=page_count,
            ocr_engine=capability.engine if capability and capability.available else None,
            ocr_engine_version=(
                capability.version if capability and capability.available else None
            ),
        )

    def _overall_status(
        self, pages: tuple[ExtractedPage, ...], budget_hit: bool
    ) -> ExtractionStatus:
        """Summarize page outcomes into one honest document-level state."""
        if not pages:
            return ExtractionStatus.NO_CONTENT_DETECTED

        statuses = {page.status for page in pages}

        if statuses == {ExtractionStatus.OCR_UNAVAILABLE}:
            return ExtractionStatus.OCR_UNAVAILABLE

        extracted_any = any(page.char_count > 0 for page in pages)

        if not extracted_any:
            if ExtractionStatus.OCR_UNAVAILABLE in statuses:
                return ExtractionStatus.OCR_UNAVAILABLE
            if ExtractionStatus.FAILED in statuses:
                return ExtractionStatus.FAILED
            return ExtractionStatus.NO_CONTENT_DETECTED

        partial = budget_hit or bool(
            statuses & {ExtractionStatus.FAILED, ExtractionStatus.OCR_UNAVAILABLE}
        ) or any(page.truncated for page in pages)

        return ExtractionStatus.PARTIAL if partial else ExtractionStatus.EXTRACTED

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    def _ocr_capability(self) -> OcrCapability:
        if self._ocr is None:
            self._ocr = probe_ocr(self._tesseract_cmd)
        return self._ocr

    def _open(self, fitz: Any) -> Any:
        """Open from memory when the content never was a file, else from disk.

        ``fitz`` reads a byte stream natively, so a BLOB needs no temporary
        file. Both branches raise the same error, so a corrupt PDF is reported
        identically whichever way it arrived.
        """
        try:
            if self._file.payload is not None:
                return fitz.open(stream=self._file.payload, filetype="pdf")

            return fitz.open(self._require_local_path())
        except Exception as exc:
            raise MalformedPDFError(
                f"{self._file.original_filename!r} could not be opened as a "
                f"PDF ({type(exc).__name__}). The file is corrupt or truncated."
            ) from exc

    def _require_local_path(self) -> Path:
        if self._file.local_path is None:  # pragma: no cover - guarded upstream
            raise MalformedPDFError("This FileSource carries no readable local path.")
        return self._file.local_path

    def _warn(self, category: str, message: str,
              page_number: int | None = None) -> None:
        """Record a non-fatal problem, positionally.

        No extracted or recognized text ever reaches this method.
        """
        self._warnings.append(
            ExtractionWarning(
                category=category, message=message, page_number=page_number
            )
        )


def _import_pymupdf() -> Any:
    """Import PyMuPDF lazily, with an actionable message when it is absent."""
    try:
        import fitz
    except ImportError as exc:
        raise MalformedPDFError(
            "The 'pymupdf' package is required to ingest PDF files but is not "
            "installed. Install it with: pip install pymupdf"
        ) from exc

    return fitz


def ingest_pdf_file(
    file: FileSource,
    options: PdfOptions | None = None,
    tesseract_cmd: str | None = None,
) -> ExtractedDocument:
    """Convenience wrapper around ``PdfFileIngestion``."""
    return PdfFileIngestion(file, options, tesseract_cmd).ingest()


__all__ = [
    "EXTRACTOR_NAME",
    "PdfFileIngestion",
    "ingest_pdf_file",
]
