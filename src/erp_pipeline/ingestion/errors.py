"""Domain error hierarchy for universal file ingestion.

Mirrors the pattern Phase 2 (``catalog.exceptions``), Phase 3
(``connectors.errors``) and Phase 4/5 (``discovery.errors``) established: every
ingestion failure surfaces as one of these instead of a raw ``csv``, PyMuPDF,
Pillow or ``pytesseract`` exception, and ``__cause__`` always preserves the
original for debugging.

PRIVACY RULE, enforced by test
------------------------------
No message constructed here may contain source CONTENT - not a CSV field, not
a row, not a line of extracted or OCR'd text, not a PDF title. An ingestion
error may name only structural context: a row number, a page number, a byte
offset, a column count, a limit that was exceeded.

That rule is why several of these errors carry structured attributes (
``row_number``, ``page_number``, ``limit``) rather than a formatted message
built from the offending data. The context a developer needs to find the
problem is positional, and the position is never sensitive.
"""

from __future__ import annotations


class IngestionError(Exception):
    """Base class for every file-ingestion error."""


# ============================================================
# File-level problems (Steps 4, 29, 30)
# ============================================================

class FileAccessError(IngestionError):
    """Raised when the path is missing, unreadable, or not a regular file.

    Directories, broken paths, and device/special files are rejected here
    rather than being handed to a parser that would fail confusingly later.
    """


class UnsupportedFileTypeError(IngestionError):
    """Raised when a file is neither CSV, PDF, nor a supported image.

    Phase 6 refuses to guess. An unrecognized file is rejected rather than
    speculatively parsed as text, which is what stops arbitrary binary content
    being processed as though it were a document.
    """


class FileTypeMismatchError(UnsupportedFileTypeError):
    """Raised when a file's extension and its actual content disagree.

    A ``.csv`` file whose bytes begin with ``%PDF-`` is the canonical example.
    Silently trusting either side would be wrong: trusting the extension feeds
    binary to the CSV parser, and trusting the content lets a misnamed file be
    processed as something the caller did not intend. The default is to fail;
    ``IngestionOptions.allow_type_mismatch`` lets a caller opt into trusting
    the detected content instead, which downgrades this to a warning.
    """

    def __init__(
        self,
        message: str,
        extension_type: str | None = None,
        content_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.extension_type = extension_type
        self.content_type = content_type


class FileTooLargeError(IngestionError):
    """Raised when a file exceeds ``max_file_size_bytes``.

    Checked against the filesystem's own size metadata BEFORE the file is
    opened or read, so an oversized file never reaches memory.
    """

    def __init__(self, message: str, size_bytes: int | None = None,
                 limit_bytes: int | None = None) -> None:
        super().__init__(message)
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


# ============================================================
# CSV (Steps 12, 13, 31)
# ============================================================

class MalformedCSVError(IngestionError):
    """Raised when a CSV cannot be parsed within the configured budget.

    Covers an undecodable byte sequence, a field or column count beyond the
    configured limits, and accumulating more malformed rows than
    ``max_errors`` allows. Individual recoverable problems do NOT raise: they
    are recorded as ``ExtractionWarning`` entries so a mostly-good file still
    yields a usable result.
    """

    def __init__(
        self,
        message: str,
        row_number: int | None = None,
        byte_offset: int | None = None,
    ) -> None:
        super().__init__(message)
        self.row_number = row_number
        self.byte_offset = byte_offset


# ============================================================
# PDF (Steps 24, 31)
# ============================================================

class MalformedPDFError(IngestionError):
    """Raised when a PDF is corrupt or structurally unreadable."""


class EncryptedPDFError(IngestionError):
    """Raised when a PDF is password-protected.

    Phase 6 makes no attempt to bypass, crack or guess a password. The file is
    reported as encrypted and left alone.
    """


# ============================================================
# Image and OCR (Steps 25, 27, 31)
# ============================================================

class ImageDecodeError(IngestionError):
    """Raised when an image is corrupt, truncated, or implausibly large.

    The size guard is a decompression-bomb defence: a small file can declare
    enormous dimensions, and decoding it would exhaust memory.
    """


class OCRUnavailableError(IngestionError):
    """Raised only when OCR was explicitly REQUIRED but is not installed.

    Ordinary ingestion never raises this. Missing OCR is a capability fact,
    not a file defect, so by default it produces the explicit
    ``ExtractionStatus.OCR_UNAVAILABLE`` state plus a warning - never an empty
    string presented as a successful extraction.
    """


__all__ = [
    "IngestionError",
    "FileAccessError",
    "UnsupportedFileTypeError",
    "FileTypeMismatchError",
    "FileTooLargeError",
    "MalformedCSVError",
    "MalformedPDFError",
    "EncryptedPDFError",
    "ImageDecodeError",
    "OCRUnavailableError",
]
