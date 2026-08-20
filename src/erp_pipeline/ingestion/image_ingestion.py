"""Image ingestion: validation, metadata, and OCR.

Uses Pillow, already a project dependency, imported lazily so that importing
``erp_pipeline.ingestion`` never requires it.

An image produces an ``ExtractedDocument`` with exactly one page. That is not
a workaround - a single-page document IS what a scanned receipt is - and it
means PDF and image results are the same shape, so a consumer handling
extracted documents needs no special case for either.

Images are never forced into a ``SourceSchema``. A photograph of an invoice
has no columns.

READ-ONLY: images are opened for reading; nothing is written or re-encoded to
disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from erp_pipeline.ingestion.errors import ImageDecodeError
from erp_pipeline.ingestion.models import (
    ExtractedDocument,
    ExtractedPage,
    ExtractionStatus,
    ExtractionWarning,
    FileProvenance,
    FileSource,
    ImageOptions,
)
from erp_pipeline.ingestion.ocr import OcrCapability, probe_ocr, run_ocr
from erp_pipeline.ingestion.safety import truncate_text

EXTRACTOR_NAME = "pillow"


class ImageFileIngestion:
    """Validates one image, records its properties, and OCRs it if possible."""

    def __init__(
        self,
        file: FileSource,
        options: ImageOptions | None = None,
        tesseract_cmd: str | None = None,
    ) -> None:
        self._file = file
        self._options = options or ImageOptions()
        self._tesseract_cmd = tesseract_cmd
        self._warnings: list[ExtractionWarning] = []
        self._ocr: OcrCapability | None = None

    @property
    def warnings(self) -> tuple[ExtractionWarning, ...]:
        return tuple(self._warnings)

    def ingest(self) -> ExtractedDocument:
        image_module = _import_pillow()
        path = self._require_local_path()

        properties = self._inspect(image_module, path)
        page = self._extract_text(image_module, path)

        return ExtractedDocument(
            file=self._file,
            provenance=self._provenance(),
            pages=(page,),
            status=page.status,
            page_count=properties["frame_count"],
            document_metadata=properties,
            warnings=self.warnings,
        )

    # ------------------------------------------------------------
    # Validation and properties (Step 25)
    # ------------------------------------------------------------

    def _inspect(self, image_module: Any, path: Path) -> dict[str, Any]:
        """Validate the image and read its structural properties.

        ``Image.open`` is lazy - it parses the header without decoding pixels -
        so the dimension check below happens BEFORE any large allocation. That
        ordering is the decompression-bomb defence: a 40 KB file can declare
        50 000 x 50 000 pixels, and decoding it first would defeat the guard.
        """
        try:
            with image_module.open(path) as image:
                width, height = image.size
                self._require_sane_dimensions(width, height)

                return {
                    "format": image.format,
                    "mode": image.mode,
                    "width": width,
                    "height": height,
                    "frame_count": int(getattr(image, "n_frames", 1) or 1),
                }
        except ImageDecodeError:
            raise
        except Exception as exc:
            raise ImageDecodeError(
                f"{self._file.original_filename!r} could not be decoded as an "
                f"image ({type(exc).__name__}). The file is corrupt or "
                "truncated."
            ) from exc

    def _require_sane_dimensions(self, width: int, height: int) -> None:
        pixels = width * height

        if pixels > self._options.max_pixels:
            raise ImageDecodeError(
                f"{self._file.original_filename!r} declares {width}x{height} "
                f"({pixels} pixels), which exceeds the configured limit of "
                f"{self._options.max_pixels}. Decoding it could exhaust memory."
            )

    # ------------------------------------------------------------
    # OCR (Steps 26, 27)
    # ------------------------------------------------------------

    def _extract_text(self, image_module: Any, path: Path) -> ExtractedPage:
        if not self._options.ocr_enabled:
            self._warn(
                "ocr_disabled",
                "OCR is disabled in options; only image metadata was extracted.",
            )
            return ExtractedPage(
                page_number=1, text="", status=ExtractionStatus.OCR_UNAVAILABLE,
                extraction_method="none",
            )

        capability = self._ocr_capability()

        if not capability.available:
            # Explicitly OCR_UNAVAILABLE, never an empty string reported as a
            # successful extraction: "we could not read this" and "there was
            # nothing to read" have different remedies.
            self._warn("ocr_unavailable", f"OCR is unavailable: {capability.reason}")
            return ExtractedPage(
                page_number=1, text="", status=ExtractionStatus.OCR_UNAVAILABLE,
                extraction_method="none",
            )

        try:
            with image_module.open(path) as image:
                recognized = run_ocr(
                    image, self._options.ocr_language, self._tesseract_cmd
                )
        except RuntimeError as exc:
            self._warn("ocr_failed", str(exc))
            return ExtractedPage(
                page_number=1, text="", status=ExtractionStatus.FAILED,
                extraction_method="none",
            )
        except Exception as exc:
            self._warn(
                "ocr_failed",
                f"The image could not be read for OCR ({type(exc).__name__}).",
            )
            return ExtractedPage(
                page_number=1, text="", status=ExtractionStatus.FAILED,
                extraction_method="none",
            )

        text, truncated = truncate_text(recognized, self._options.max_text_chars)

        if truncated:
            self._warn(
                "text_truncated",
                "Recognized text exceeded the configured limit of "
                f"{self._options.max_text_chars} characters.",
            )

        if not text.strip():
            # A blank page really did OCR successfully - it just had no text.
            return ExtractedPage(
                page_number=1, text="", status=ExtractionStatus.NO_CONTENT_DETECTED,
                extraction_method="ocr",
            )

        return ExtractedPage(
            page_number=1,
            text=text,
            status=ExtractionStatus.PARTIAL if truncated else ExtractionStatus.EXTRACTED,
            extraction_method="ocr",
            char_count=len(text),
            truncated=truncated,
        )

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    def _provenance(self) -> FileProvenance:
        capability = self._ocr
        available = capability is not None and capability.available

        return FileProvenance(
            file_id=self._file.file_id,
            content_hash=self._file.content_hash,
            original_filename=self._file.original_filename,
            file_type=self._file.file_type,
            media_type=self._file.media_type,
            size_bytes=self._file.size_bytes,
            extractor=EXTRACTOR_NAME,
            page_count=1,
            ocr_engine=capability.engine if available else None,
            ocr_engine_version=capability.version if available else None,
        )

    def _ocr_capability(self) -> OcrCapability:
        if self._ocr is None:
            self._ocr = probe_ocr(self._tesseract_cmd)
        return self._ocr

    def _require_local_path(self) -> Path:
        if self._file.local_path is None:  # pragma: no cover - guarded upstream
            raise ImageDecodeError("This FileSource carries no readable local path.")
        return self._file.local_path

    def _warn(self, category: str, message: str) -> None:
        """Record a non-fatal problem.

        No recognized text ever reaches this method - not even a preview of
        the first few characters, which would be exactly the kind of "helpful"
        log line that leaks a customer's address.
        """
        self._warnings.append(
            ExtractionWarning(category=category, message=message, page_number=1)
        )


def _import_pillow() -> Any:
    """Import Pillow's ``Image`` lazily, with an actionable message."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImageDecodeError(
            "The 'Pillow' package is required to ingest image files but is not "
            "installed. Install it with: pip install pillow"
        ) from exc

    return Image


def ingest_image_file(
    file: FileSource,
    options: ImageOptions | None = None,
    tesseract_cmd: str | None = None,
) -> ExtractedDocument:
    """Convenience wrapper around ``ImageFileIngestion``."""
    return ImageFileIngestion(file, options, tesseract_cmd).ingest()


__all__ = [
    "EXTRACTOR_NAME",
    "ImageFileIngestion",
    "ingest_image_file",
]
