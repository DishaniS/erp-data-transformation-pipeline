"""Ingestion options and source-level result models.

Two kinds of thing live here, exactly as in ``discovery.models``: input
configuration (``IngestionOptions`` and its per-format companions) and the
supplemental results ingestion produces.

Why there is an ``ExtractedDocument`` and not a ``CanonicalDocument``
---------------------------------------------------------------------
``erp_pipeline.schemas.canonical_models.CanonicalDocument`` is a CANONICAL
artifact: it subclasses ``CanonicalEnvelope``, carries a canonical ``erp:``
record id, a ``RecordType`` and a ``SensitivityLevel``, and lives in a module
documented as "the canonical ERP representation every source converges on".
Producing one here would mean Phase 6 had already performed the convergence -
which is Phase 8's job, not this phase's.

So Phase 6 stops one step short and emits ``ExtractedDocument``: a SOURCE-level
description of what was extracted from a file, with page provenance intact. It
is deliberately shaped so a later phase can build a ``CanonicalDocument`` from
it (content hash -> ``document_id``, pages -> text, provenance ->
``RecordProvenance``), but that conversion is not written here and must not be.

CSV is different, and correctly so: a CSV genuinely HAS a structure, so it
produces a real Phase 1 ``SourceSchema`` just as PostgreSQL, MySQL, SQL Server
and MongoDB do. No fake tabular schema is invented for PDFs or images.

THE PRIVACY SPLIT, which is the most important thing in this module
-------------------------------------------------------------------
Source values must survive ingestion - a later mapping phase cannot transform
data it never received. But those same values must not leak into operational
output. The two needs are separated structurally rather than by convention:

    CONTENT (may hold source values)
        TabularFileResult.iter_records()      raw CSV rows, streamed
        ExtractedPage.text                    extracted / OCR'd text
        ExtractedDocument.document_text       deterministic join of pages
        ExtractedDocument.document_metadata   PDF title/author, etc.

    OPERATIONAL (may never hold source values)
        every to_dict()                       excludes text unless a caller
                                              passes include_text=True
        SourceSchema and all field metadata   counts and categories only
        ExtractionWarning                     positional context only
        every exception message               positional context only

``to_dict()`` defaults to the operational form because that is what gets
logged, diffed, summarized and published. Obtaining content requires asking
for it explicitly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from erp_pipeline.schemas.enums import SourceType

#: Version of the extraction behaviour itself, recorded in provenance. Bumped
#: when a change to this package would make the same bytes extract differently,
#: so a stored result always states which extractor produced it.
EXTRACTOR_VERSION = "1.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# File type (Step 4)
# ============================================================

class FileType(str, Enum):
    """What KIND of file this is, for dispatch purposes.

    Deliberately NOT added to ``schemas.enums.SourceType``, which is a frozen
    Phase 1 contract vocabulary and already carries ``CSV``, ``PDF`` and
    ``IMAGE``. This enum is the ingestion layer's internal discriminator - it
    exists because detection needs to talk about a file before anyone has
    decided it is a valid source system, and because the two vocabularies
    should be free to diverge (a future ``XLSX`` file type could map onto the
    existing ``CSV`` structural handling).

    ``to_source_type()`` is the one place the mapping between them is defined.
    """

    CSV = "csv"
    PDF = "pdf"
    IMAGE = "image"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    def to_source_type(self) -> SourceType:
        return _FILE_TYPE_TO_SOURCE_TYPE[self]

    @property
    def is_tabular(self) -> bool:
        """True when this file type carries structure a SourceSchema can hold."""
        return self is FileType.CSV

    @property
    def is_document(self) -> bool:
        """True when this file type carries unstructured document content."""
        return self in (FileType.PDF, FileType.IMAGE)


_FILE_TYPE_TO_SOURCE_TYPE: Mapping[FileType, SourceType] = {
    FileType.CSV: SourceType.CSV,
    FileType.PDF: SourceType.PDF,
    FileType.IMAGE: SourceType.IMAGE,
}


class ExtractionStatus(str, Enum):
    """The explicit outcome of an extraction (Step 27).

    ``OCR_UNAVAILABLE`` exists so that "we could not read this" is never
    reported as "there was nothing to read". An empty string returned as a
    successful extraction would be a lie, and a downstream phase would have no
    way to tell the difference.
    """

    EXTRACTED = "extracted"
    NO_CONTENT_DETECTED = "no_content_detected"
    PARTIAL = "partial"
    OCR_UNAVAILABLE = "ocr_unavailable"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ============================================================
# Safety defaults (Steps 13, 24, 29)
# ============================================================

#: 256 MiB. Large enough for a realistic ERP export or scanned document set,
#: small enough that one bad upload cannot exhaust a worker.
DEFAULT_MAX_FILE_SIZE_BYTES = 256 * 1024 * 1024

DEFAULT_MAX_ROWS_FOR_SCHEMA_INFERENCE = 1000
DEFAULT_MAX_COLUMNS = 512
DEFAULT_MAX_FIELD_LENGTH = 128 * 1024
DEFAULT_MAX_CSV_ERRORS = 100
DEFAULT_DELIMITER_SNIFF_LINES = 20

DEFAULT_MAX_PDF_PAGES = 500
DEFAULT_MAX_TEXT_CHARS_PER_PAGE = 200_000
DEFAULT_MAX_TOTAL_TEXT_CHARS = 5_000_000
#: A page yielding fewer characters than this from its text layer is treated as
#: image-only and becomes an OCR candidate.
DEFAULT_OCR_MIN_TEXT_CHARS = 16
DEFAULT_OCR_RENDER_DPI = 200

#: ~64 megapixels. A decompression-bomb guard: a tiny file can declare enormous
#: dimensions, and decoding it would exhaust memory.
DEFAULT_MAX_IMAGE_PIXELS = 64_000_000

#: Textual tokens treated as null ONLY when a caller opts in (Step 16). Empty
#: by default: silently reading "N/A" as null would destroy the distinction
#: between "no value" and "the literal text N/A", which some ERP exports use
#: as a real value.
DEFAULT_NULL_TOKENS: tuple[str, ...] = ()

#: Delimiters considered during detection, in priority order for tie-breaks.
CANDIDATE_DELIMITERS: tuple[str, ...] = (",", ";", "\t", "|")


@dataclass(frozen=True)
class CsvOptions:
    """CSV parsing and inference configuration.

    Every default is conservative: nothing is guessed that can be observed,
    nothing is read unboundedly, and no textual token is treated as null
    unless the caller says so.
    """

    encoding: str | None = None          # None -> detect (BOM) then UTF-8
    delimiter: str | None = None         # None -> deterministic detection
    quote_char: str = '"'
    has_header: bool = True
    null_tokens: Sequence[str] = DEFAULT_NULL_TOKENS
    case_insensitive_null_tokens: bool = True

    max_rows_for_schema_inference: int = DEFAULT_MAX_ROWS_FOR_SCHEMA_INFERENCE
    max_columns: int = DEFAULT_MAX_COLUMNS
    max_field_length: int = DEFAULT_MAX_FIELD_LENGTH
    max_errors: int = DEFAULT_MAX_CSV_ERRORS
    delimiter_sniff_lines: int = DEFAULT_DELIMITER_SNIFF_LINES

    def __post_init__(self) -> None:
        _require_positive(self, ("max_rows_for_schema_inference", "max_columns",
                                 "max_field_length", "max_errors",
                                 "delimiter_sniff_lines"), "CsvOptions")

        if self.delimiter is not None and len(self.delimiter) != 1:
            raise ValueError(
                f"CsvOptions.delimiter must be a single character, got "
                f"{self.delimiter!r}."
            )
        if len(self.quote_char) != 1:
            raise ValueError(
                f"CsvOptions.quote_char must be a single character, got "
                f"{self.quote_char!r}."
            )

    def normalized_null_tokens(self) -> frozenset[str]:
        if self.case_insensitive_null_tokens:
            return frozenset(token.strip().lower() for token in self.null_tokens)
        return frozenset(self.null_tokens)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["null_tokens"] = list(self.null_tokens)
        return payload


@dataclass(frozen=True)
class PdfOptions:
    """PDF extraction configuration and safety budget."""

    max_pages: int = DEFAULT_MAX_PDF_PAGES
    max_text_chars_per_page: int = DEFAULT_MAX_TEXT_CHARS_PER_PAGE
    max_total_text_chars: int = DEFAULT_MAX_TOTAL_TEXT_CHARS
    #: OCR a page whose text layer is empty or near-empty (a scanned page).
    ocr_fallback: bool = True
    ocr_min_text_chars: int = DEFAULT_OCR_MIN_TEXT_CHARS
    ocr_render_dpi: int = DEFAULT_OCR_RENDER_DPI
    ocr_language: str = "eng"
    include_document_metadata: bool = True

    def __post_init__(self) -> None:
        _require_positive(self, ("max_pages", "max_text_chars_per_page",
                                 "max_total_text_chars", "ocr_render_dpi"),
                          "PdfOptions")
        if isinstance(self.ocr_min_text_chars, bool) or self.ocr_min_text_chars < 0:
            raise ValueError("PdfOptions.ocr_min_text_chars must not be negative.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImageOptions:
    """Image validation and OCR configuration."""

    ocr_enabled: bool = True
    ocr_language: str = "eng"
    max_pixels: int = DEFAULT_MAX_IMAGE_PIXELS
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS_PER_PAGE

    def __post_init__(self) -> None:
        _require_positive(self, ("max_pixels", "max_text_chars"), "ImageOptions")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IngestionOptions:
    """One options object for the whole ingestion service.

    Format-specific settings are nested rather than flattened, so a caller
    tuning PDF page budgets is never confronted with CSV delimiters, and a new
    format adds one nested object instead of a dozen prefixed fields.

    ``source_system_id`` is the logical system uploaded files belong to. It is
    a caller decision, not something derived from a path: two files dropped by
    the same integration belong to the same source system even though their
    filenames differ.
    """

    source_system_id: str = "file_source"
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES
    #: When the extension and the file's actual content disagree: fail (False,
    #: the default) or trust the content and record a warning (True).
    allow_type_mismatch: bool = False
    #: Path to the Tesseract executable. ``None`` falls back to the
    #: TESSERACT_CMD / TESSERACT_PATH environment variables, then PATH.
    tesseract_cmd: str | None = None

    csv: CsvOptions = field(default_factory=CsvOptions)
    pdf: PdfOptions = field(default_factory=PdfOptions)
    image: ImageOptions = field(default_factory=ImageOptions)

    def __post_init__(self) -> None:
        _require_positive(self, ("max_file_size_bytes",), "IngestionOptions")

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot, recorded so a result states how it was produced."""
        return {
            "source_system_id": self.source_system_id,
            "max_file_size_bytes": self.max_file_size_bytes,
            "allow_type_mismatch": self.allow_type_mismatch,
            "csv": self.csv.to_dict(),
            "pdf": self.pdf.to_dict(),
            "image": self.image.to_dict(),
        }


def _require_positive(instance: Any, names: Sequence[str], model: str) -> None:
    for name in names:
        value = getattr(instance, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                f"{model}.{name} must be a positive integer, got {value!r}."
            )


# ============================================================
# File identity and provenance (Steps 3, 7, 8)
# ============================================================

@dataclass(frozen=True)
class FileSource:
    """The file being ingested, identified by its CONTENT.

    ``local_path`` is runtime-only. It is deliberately excluded from
    ``to_dict()`` because a developer workstation path
    (``C:\\Users\\...\\Desktop\\invoices.csv``) is not identity: it is not
    stable, not portable, not meaningful on another machine, and it leaks the
    local filesystem layout into anything the result is published to. Identity
    is ``content_hash``.

    No open file handle is ever stored on this model - only a path a parser can
    re-open. That keeps the model serializable and stops a result object
    silently holding an OS resource open.
    """

    file_id: str
    content_hash: str
    original_filename: str
    file_type: FileType
    media_type: str
    size_bytes: int
    local_path: Path | None = None
    #: Content held in memory instead of on disk, for bytes that never were a
    #: file - a database BLOB, most of all. Runtime-only and excluded from
    #: ``to_dict()`` for the same reason ``local_path`` is: it is not identity.
    #:
    #: This exists so an ERP document extracted from a BLOB never has to be
    #: spilled to a temporary file. A birth certificate written to the system
    #: temp directory would sit there in plaintext, outside every access
    #: control and encryption guarantee the storage tiers provide, for the sake
    #: of handing a parser a path it did not need.
    payload: bytes | None = None

    @property
    def source_type(self) -> SourceType:
        return self.file_type.to_source_type()

    def to_dict(self, include_local_path: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "file_id": self.file_id,
            "content_hash": self.content_hash,
            "original_filename": self.original_filename,
            "file_type": self.file_type.value,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
        }
        if include_local_path and self.local_path is not None:
            # Explicit opt-in only, and named so nobody mistakes it for
            # portable identity.
            payload["runtime_local_path"] = str(self.local_path)
        return payload


@dataclass(frozen=True)
class FileProvenance:
    """Where this content came from and how it was extracted.

    A pointer plus extraction facts, never a second copy of the data - the
    same rule Phase 1's ``RecordProvenance`` follows. Everything here is
    structural: counts, formats, encodings. Nothing here is a source value.
    """

    file_id: str
    content_hash: str
    original_filename: str
    file_type: FileType
    media_type: str
    size_bytes: int
    extractor: str
    extractor_version: str = EXTRACTOR_VERSION
    encoding: str | None = None
    delimiter: str | None = None
    row_count: int | None = None
    column_count: int | None = None
    page_count: int | None = None
    ocr_engine: str | None = None
    ocr_engine_version: str | None = None
    extracted_at: datetime = field(default_factory=utc_now)

    def to_dict(self, include_timestamp: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "file_id": self.file_id,
            "content_hash": self.content_hash,
            "original_filename": self.original_filename,
            "file_type": self.file_type.value,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "extractor": self.extractor,
            "extractor_version": self.extractor_version,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "page_count": self.page_count,
            "ocr_engine": self.ocr_engine,
            "ocr_engine_version": self.ocr_engine_version,
        }
        if include_timestamp:
            # Operational only. Never an input to identity or any hash.
            payload["extracted_at"] = self.extracted_at.isoformat()
        return payload


# ============================================================
# Warnings (Step 32)
# ============================================================

@dataclass(frozen=True)
class ExtractionWarning:
    """One non-fatal problem, described POSITIONALLY.

    ``category`` is a stable machine-readable slug so a caller can react
    programmatically; ``message`` explains it to a human. Neither may contain a
    source value - the location (row, page, column index) is what identifies
    the problem, and a location is never sensitive.
    """

    category: str
    message: str
    row_number: int | None = None
    page_number: int | None = None
    column_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "message": self.message,
            "row_number": self.row_number,
            "page_number": self.page_number,
            "column_index": self.column_index,
        }


# ============================================================
# Document extraction (Steps 6, 21, 22, 25)
# ============================================================

@dataclass(frozen=True)
class ExtractedPage:
    """One page of a document, with its provenance preserved.

    Page boundaries survive extraction because a later retrieval phase needs
    to cite them: "this answer came from page 4" is only possible if page 4 was
    never merged away.
    """

    page_number: int
    text: str
    status: ExtractionStatus
    extraction_method: str  # "text_layer" | "ocr" | "none"
    char_count: int = 0
    truncated: bool = False

    def to_dict(self, include_text: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "page_number": self.page_number,
            "status": self.status.value,
            "extraction_method": self.extraction_method,
            "char_count": self.char_count,
            "truncated": self.truncated,
        }
        if include_text:
            payload["text"] = self.text
        return payload


@dataclass(frozen=True)
class ExtractedDocument:
    """Source-level extraction result for a PDF or an image.

    NOT a ``CanonicalDocument`` - see the module docstring. This describes what
    was pulled out of a file; it makes no claim about what any of it MEANS.

    ``document_metadata`` holds format-declared metadata such as a PDF's title
    and author. That is treated as CONTENT, not as operational metadata,
    because a document title routinely contains a customer or project name -
    so it is excluded from ``to_dict()`` on the same terms as page text.
    """

    file: FileSource
    provenance: FileProvenance
    pages: tuple[ExtractedPage, ...]
    status: ExtractionStatus
    page_count: int
    document_metadata: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[ExtractionWarning, ...] = ()

    @property
    def document_text(self) -> str:
        """Deterministic join of page text, in page order.

        A form-feed separator keeps page boundaries recoverable from the joined
        string, and the join is pure ordering - no reflow, no normalization, no
        interpretation.
        """
        return "\f".join(page.text for page in self.pages)

    @property
    def total_char_count(self) -> int:
        return sum(page.char_count for page in self.pages)

    @property
    def has_text(self) -> bool:
        return any(page.char_count > 0 for page in self.pages)

    def to_dict(self, include_text: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "file": self.file.to_dict(),
            "provenance": self.provenance.to_dict(),
            "status": self.status.value,
            "page_count": self.page_count,
            "total_char_count": self.total_char_count,
            "pages": [page.to_dict(include_text=include_text) for page in self.pages],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }
        if include_text:
            payload["document_text"] = self.document_text
            payload["document_metadata"] = dict(self.document_metadata)
        return payload


# ============================================================
# Tabular source records (Step 20)
# ============================================================

@dataclass(frozen=True)
class SourceRow:
    """One raw source row, before any mapping whatsoever.

    Values are the source's own strings, unconverted and uninterpreted. That is
    deliberate: a later mapping phase must decide how to convert them, and a
    conversion applied here would be an irreversible guess made by the wrong
    layer.

    ``row_number`` is the 1-based position in the data section of the file
    (excluding the header), so a downstream error can always be traced back to
    a physical location in the original upload.
    """

    row_number: int
    values: Mapping[str, str | None]
    file_id: str
    #: Fields present in the header but absent from this physical row.
    missing_fields: tuple[str, ...] = ()
    #: Values present in this row beyond the header's column count.
    extra_value_count: int = 0

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields and self.extra_value_count == 0

    def to_dict(self, include_values: bool = True) -> dict[str, Any]:
        """Serialize this row.

        Unlike every other ``to_dict()`` in this module, values are INCLUDED by
        default: a source row is content by definition, and a caller holding
        one has already chosen to work with content. Pass
        ``include_values=False`` for the positional shape alone.
        """
        payload: dict[str, Any] = {
            "row_number": self.row_number,
            "file_id": self.file_id,
            "missing_fields": list(self.missing_fields),
            "extra_value_count": self.extra_value_count,
        }
        if include_values:
            payload["values"] = dict(self.values)
        return payload


# ============================================================
# Ingestion results (Steps 5, 6)
# ============================================================

@dataclass(frozen=True)
class FileIngestionResult:
    """Common shape of every ingestion outcome.

    One understandable top-level contract, rather than each parser returning
    an unrelated dictionary. Callers discriminate on ``file.file_type`` or with
    ``isinstance``, and both subclasses expose ``file``, ``provenance``,
    ``status``, ``warnings`` and ``to_dict()`` identically.
    """

    file: FileSource
    provenance: FileProvenance
    status: ExtractionStatus
    warnings: tuple[ExtractionWarning, ...] = ()

    @property
    def file_type(self) -> FileType:
        return self.file.file_type

    @property
    def content_hash(self) -> str:
        return self.file.content_hash

    @property
    def is_tabular(self) -> bool:
        return isinstance(self, TabularFileResult)

    @property
    def is_document(self) -> bool:
        return isinstance(self, DocumentFileResult)

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass(frozen=True)
class FieldObservation:
    """Aggregate observation of one CSV column across the sampled rows.

    Counts and categories only. Exactly like ``discovery.models.
    FieldObservation``, no attribute here can hold a value drawn from the file:
    ``category_counts`` maps a value CATEGORY (``"integer"``, ``"string"``) to
    how many values fell into it, and the values themselves are counted and
    discarded.
    """

    source_name: str
    column_index: int
    rows_sampled: int
    present_count: int
    empty_count: int
    null_marker_count: int
    category_counts: Mapping[str, int] = field(default_factory=dict)
    max_observed_length: int = 0

    @property
    def missing_count(self) -> int:
        return max(self.rows_sampled - self.present_count, 0)

    @property
    def value_count(self) -> int:
        """Non-empty, non-null values - the ones that carry type evidence."""
        return sum(self.category_counts.values())

    @property
    def presence_ratio(self) -> float:
        if self.rows_sampled <= 0:
            return 0.0
        return round(self.present_count / self.rows_sampled, 6)

    @property
    def null_ratio(self) -> float:
        if self.rows_sampled <= 0:
            return 0.0
        return round((self.empty_count + self.null_marker_count) / self.rows_sampled, 6)

    @property
    def observed_always_populated(self) -> bool:
        return (
            self.rows_sampled > 0
            and self.present_count == self.rows_sampled
            and self.empty_count == 0
            and self.null_marker_count == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "column_index": self.column_index,
            "rows_sampled": self.rows_sampled,
            "present_count": self.present_count,
            "missing_count": self.missing_count,
            "empty_count": self.empty_count,
            "null_marker_count": self.null_marker_count,
            "value_count": self.value_count,
            "presence_ratio": self.presence_ratio,
            "null_ratio": self.null_ratio,
            "category_counts": dict(sorted(self.category_counts.items())),
            "max_observed_length": self.max_observed_length,
        }


@dataclass(frozen=True)
class TabularFileResult(FileIngestionResult):
    """A structured file (CSV) reduced to a Phase 1 ``SourceSchema`` plus rows.

    ``schema`` is authoritative and is the same contract relational and MongoDB
    discovery produce. ``observations`` is supplemental evidence kept OUTSIDE
    the schema, for the same reason profiling and Mongo sampling statistics are
    kept outside theirs: row counts change with the sample and must not perturb
    the structural hash.

    Rows are not held in memory. ``iter_records()`` re-opens the file and
    streams, so a multi-gigabyte CSV is ingested with a bounded footprint.
    """

    schema: Any = None                    # SourceSchema, typed loosely to keep
    observations: tuple[FieldObservation, ...] = ()   # this module import-light
    header: tuple[str, ...] = ()
    rows_sampled: int = 0
    data_row_count: int | None = None
    _row_reader: Any = None               # callable() -> Iterator[SourceRow]

    @property
    def schema_hash(self) -> str:
        return self.schema.compute_schema_hash()

    def iter_records(self) -> Iterator[SourceRow]:
        """Stream every data row as a ``SourceRow``.

        Re-reads the file from disk on each call, so the result object stays
        small and the iterator stays lazy. Raw values ARE present here - that
        is the point: this is the handoff to a future mapping phase, which
        cannot transform data it never received.

        Nothing here builds a ``CanonicalRecord``. Converting a source row into
        a canonical record requires a mapping profile, which is Phase 8.
        """
        if self._row_reader is None:
            raise RuntimeError(
                "This TabularFileResult was constructed without a row reader, "
                "so its source rows cannot be streamed."
            )
        return self._row_reader()

    def to_dict(self) -> dict[str, Any]:
        """Operational summary. Contains no source values, by construction."""
        return {
            "file": self.file.to_dict(),
            "provenance": self.provenance.to_dict(),
            "status": self.status.value,
            "kind": "tabular",
            "schema": self.schema.to_json_dict() if self.schema is not None else None,
            "header_column_count": len(self.header),
            "rows_sampled": self.rows_sampled,
            "data_row_count": self.data_row_count,
            "observations": [item.to_dict() for item in self.observations],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(frozen=True)
class DocumentFileResult(FileIngestionResult):
    """An unstructured file (PDF or image) reduced to an ``ExtractedDocument``.

    No ``SourceSchema`` is produced, deliberately. A PDF has no columns, and
    inventing some so it resembles a table would be a fabrication that every
    later phase would then have to work around.
    """

    document: ExtractedDocument = None  # type: ignore[assignment]

    @property
    def page_count(self) -> int:
        return self.document.page_count

    def to_dict(self, include_text: bool = False) -> dict[str, Any]:
        """Operational summary. Text is excluded unless explicitly requested."""
        return {
            "file": self.file.to_dict(),
            "provenance": self.provenance.to_dict(),
            "status": self.status.value,
            "kind": "document",
            "document": self.document.to_dict(include_text=include_text),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


__all__ = [
    "EXTRACTOR_VERSION",
    "FileType",
    "ExtractionStatus",
    "CANDIDATE_DELIMITERS",
    "DEFAULT_MAX_FILE_SIZE_BYTES",
    "DEFAULT_MAX_ROWS_FOR_SCHEMA_INFERENCE",
    "DEFAULT_MAX_COLUMNS",
    "DEFAULT_MAX_FIELD_LENGTH",
    "DEFAULT_MAX_PDF_PAGES",
    "DEFAULT_MAX_TEXT_CHARS_PER_PAGE",
    "DEFAULT_MAX_TOTAL_TEXT_CHARS",
    "DEFAULT_MAX_IMAGE_PIXELS",
    "CsvOptions",
    "PdfOptions",
    "ImageOptions",
    "IngestionOptions",
    "FileSource",
    "FileProvenance",
    "ExtractionWarning",
    "ExtractedPage",
    "ExtractedDocument",
    "SourceRow",
    "FieldObservation",
    "FileIngestionResult",
    "TabularFileResult",
    "DocumentFileResult",
    "utc_now",
]
