"""Database BLOBs as ERP documents (Phase 3).

THE GAP THIS CLOSES
-------------------
A legacy ERP keeps scanned certificates, signed contracts and profile photos in
BLOB columns. Until now the pipeline could read a PDF a user UPLOADED, but not
the identical PDF sitting in ``employees.birth_certificate`` - the structured
path base64-encoded it into a string, and Phase 2 excluded it from the AI text
entirely. Either way, nobody ever opened it.

This module opens it, and it does so by CONNECTING existing engines rather than
writing new ones:

    ingestion.detection      magic bytes -> what these bytes actually are
    ingestion.pdf_ingestion  PDF -> page text, with OCR fallback
    ingestion.image_ingestion image -> dimensions + OCR
    ingestion.hashing        content-addressed identity

Nothing here parses a PDF, decodes an image or performs OCR. Doing any of that
a second time would mean two implementations drifting apart, and the existing
ones are the heavily-tested ones.

WHY THE COLUMN NAME IS NOT TRUSTED
----------------------------------
``birth_certificate`` is a perfectly good name for a column holding a JPEG, a
PDF, a ZIP of both, or - in one real migration - a TIFF someone renamed. The
bytes decide, exactly as they do for an uploaded file and for a Phase 14
response. The column name is retained as ERP CONTEXT (it is genuinely what the
business calls this document) but never as evidence of format.

WHY NOTHING IS WRITTEN TO DISK
-----------------------------
An earlier version of this module spilled each BLOB to a temporary file,
because both extractors took a path. That was wrong twice over: it broke the
ingestion package's standing guarantee that it never writes to the filesystem,
and it put employee birth certificates into the system temp directory in
plaintext - outside every access control and encryption guarantee the storage
tiers otherwise provide, for the sake of handing a parser a path it did not
need.

Both underlying libraries read bytes natively, so ``FileSource`` now carries an
optional in-memory ``payload`` and the extractors open from it. Nothing about
the file-based path changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

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

#: Ceiling on a single BLOB. A mislabelled 2 GB export must not be pulled into
#: memory and handed to a parser; it is refused with its size reported.
DEFAULT_MAX_BLOB_BYTES = 32 * 1024 * 1024

#: Ceiling on text carried out of one asset, so a 400-page contract cannot
#: become the entire vector corpus for one employee.
DEFAULT_MAX_ASSET_TEXT_CHARS = 40_000

#: Pages read from one document asset.
DEFAULT_MAX_PAGES = 40

#: The per-page marker the extractors set when OCR produced the text. Read from
#: their vocabulary rather than re-deciding what "OCR happened" means.
OCR_EXTRACTION_METHOD = "ocr"

#: Only ever used to give a synthesised filename a plausible extension in
#: extractor messages. Never used to decide a format - the bytes do that.
_SUFFIX_BY_MEDIA_TYPE: Mapping[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tif",
    "image/webp": ".webp",
}


class BinaryAssetOutcome:
    """What happened to one BLOB. Stable strings, never free text."""

    EXTRACTED = "extracted"
    UNSUPPORTED = "unsupported_binary"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    EMPTY = "empty"


@dataclass(frozen=True)
class BinaryAssetOptions:
    """Budgets for the BLOB path."""

    max_bytes: int = DEFAULT_MAX_BLOB_BYTES
    max_text_chars: int = DEFAULT_MAX_ASSET_TEXT_CHARS
    max_pages: int = DEFAULT_MAX_PAGES
    ocr_enabled: bool = True


@dataclass(frozen=True)
class BinaryAssetResult:
    """One BLOB, described.

    Carries NO bytes. A result object that held the original value would put it
    into every log line, job report and exception traceback that touched it,
    which is the leak this phase exists to prevent.
    """

    source_field: str
    outcome: str
    document_id: str | None = None
    media_type: str | None = None
    file_type: str | None = None
    size_bytes: int = 0
    document: Any = None
    ocr_used: bool = False
    page_count: int = 0
    extraction_status: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.outcome == BinaryAssetOutcome.EXTRACTED and self.document is not None

    def to_dict(self) -> dict[str, Any]:
        """Report shape. Never includes bytes, never includes extracted text."""
        return {
            "source_field": self.source_field,
            "outcome": self.outcome,
            "document_id": self.document_id,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "ocr_used": self.ocr_used,
            "page_count": self.page_count,
            "extraction_status": self.extraction_status,
            "warnings": list(self.warnings),
        }


def binary_field_names_for_entity(entity: Any) -> tuple[str, ...]:
    """Fields the DISCOVERED SCHEMA says hold bytes.

    The schema is the first and only admission signal: discovery already
    normalised every dialect's binary spelling (BYTEA, LONGBLOB, VARBINARY,
    IMAGE, binData) onto ``FieldDataType.BINARY``. Sniffing every string column
    for a magic byte instead would be slower, would re-answer a question already
    answered, and would eventually misfire on a text field that happens to start
    with the wrong two characters.
    """
    from erp_pipeline.schemas.enums import FieldDataType

    return tuple(
        field.source_name
        for field in (getattr(entity, "fields", ()) or ())
        if getattr(field, "normalized_data_type", None) is FieldDataType.BINARY
    )


def coerce_binary(value: Any) -> bytes | None:
    """Normalise whatever the driver handed back into ``bytes``.

    psycopg returns ``memoryview``, PyMySQL returns ``bytes``, pyodbc returns
    ``bytearray``, and pymongo returns a ``Binary`` subclass of ``bytes``. A
    ``str`` is deliberately NOT accepted: a text value in a binary column is a
    schema disagreement, not a document, and guessing that it might be base64
    would be exactly the kind of invention this codebase refuses.
    """
    if value is None:
        return None

    if isinstance(value, bytes):
        return value

    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)

    read = getattr(value, "read", None)

    if callable(read):
        # A LOB handle from a driver that streams.
        data = read()
        return bytes(data) if data is not None else None

    return None


def _file_source(payload: bytes, file_type: FileType, media_type: str,
                 document_id: str) -> FileSource:
    """Identity for a payload that never was a file.

    ``original_filename`` is synthesised from the content hash rather than from
    the column name, so the ERP's own vocabulary never leaks into an
    extractor's error message. The bytes travel in ``payload``; no path exists.
    """
    suffix = _SUFFIX_BY_MEDIA_TYPE.get(media_type, ".bin")

    return FileSource(
        file_id=make_file_id(document_id),
        content_hash=document_id,
        original_filename=f"blob_{document_id[:12]}{suffix}",
        file_type=file_type,
        media_type=media_type,
        size_bytes=len(payload),
        payload=payload,
    )


def extract_binary_asset(
    value: Any,
    source_field: str,
    options: BinaryAssetOptions | None = None,
) -> BinaryAssetResult:
    """Turn one BLOB into an extracted document, or explain why not.

    Never raises for a bad BLOB. A corrupt certificate must not fail the job
    that was also indexing the employee's name and department - the scalar
    record is still perfectly good data, and refusing all of it because one
    attachment is broken would be a worse answer than saying so.
    """
    options = options or BinaryAssetOptions()
    payload = coerce_binary(value)

    if payload is None or not payload:
        return BinaryAssetResult(
            source_field=source_field,
            outcome=BinaryAssetOutcome.EMPTY,
            warnings=("the field held no binary value",),
        )

    size = len(payload)

    if size > options.max_bytes:
        return BinaryAssetResult(
            source_field=source_field,
            outcome=BinaryAssetOutcome.TOO_LARGE,
            size_bytes=size,
            warnings=(
                f"the value is {size} bytes, above the {options.max_bytes}-byte "
                "limit for a single asset",
            ),
        )

    # Content-addressed, matching the identity `ai.chunking.chunk_document`
    # already derives, so the same bytes are the same document wherever they
    # came from.
    document_id = hash_bytes(payload)
    detected = detect_from_signature(payload[:64])

    if detected is None:
        return BinaryAssetResult(
            source_field=source_field,
            outcome=BinaryAssetOutcome.UNSUPPORTED,
            document_id=document_id,
            size_bytes=size,
            warnings=(
                "the bytes match no supported document or image signature; "
                "only the fact that content exists was recorded",
            ),
        )

    file_type, media_type = detected
    warnings: list[str] = []
    source = _file_source(payload, file_type, media_type, document_id)

    try:
        if file_type is FileType.PDF:
            document = ingest_pdf_file(
                source,
                PdfOptions(
                    max_pages=options.max_pages,
                    max_total_text_chars=options.max_text_chars,
                    ocr_fallback=options.ocr_enabled,
                ),
            )
        elif file_type is FileType.IMAGE:
            document = ingest_image_file(
                source,
                ImageOptions(
                    ocr_enabled=options.ocr_enabled,
                    max_text_chars=options.max_text_chars,
                ),
            )
        else:
            return BinaryAssetResult(
                source_field=source_field,
                outcome=BinaryAssetOutcome.UNSUPPORTED,
                document_id=document_id,
                media_type=media_type,
                size_bytes=size,
                warnings=(f"{media_type} is not a document or image",),
            )
    except IngestionError as error:
        # Corrupt, truncated or password-protected. The type name is reported;
        # the exception's own message is not, because an extractor is free to
        # include file details in it.
        return BinaryAssetResult(
            source_field=source_field,
            outcome=BinaryAssetOutcome.UNREADABLE,
            document_id=document_id,
            media_type=media_type,
            size_bytes=size,
            warnings=(f"the content could not be read ({type(error).__name__})",),
        )

    pages = tuple(getattr(document, "pages", ()) or ())
    warnings.extend(
        f"{item.category}: {item.message}"
        for item in (getattr(document, "warnings", ()) or ())
    )

    return BinaryAssetResult(
        source_field=source_field,
        outcome=BinaryAssetOutcome.EXTRACTED,
        document_id=document_id,
        media_type=media_type,
        file_type=file_type.value,
        size_bytes=size,
        document=document,
        # Derived from the extractors' own per-page marker, never from whether
        # OCR was merely enabled or available.
        ocr_used=any(
            getattr(page, "extraction_method", None) == OCR_EXTRACTION_METHOD
            for page in pages
        ),
        page_count=len(pages),
        extraction_status=str(getattr(document, "status", "") or "") or None,
        warnings=tuple(warnings),
    )


__all__ = [
    "DEFAULT_MAX_BLOB_BYTES",
    "DEFAULT_MAX_ASSET_TEXT_CHARS",
    "DEFAULT_MAX_PAGES",
    "OCR_EXTRACTION_METHOD",
    "BinaryAssetOutcome",
    "BinaryAssetOptions",
    "BinaryAssetResult",
    "binary_field_names_for_entity",
    "coerce_binary",
    "extract_binary_asset",
]
