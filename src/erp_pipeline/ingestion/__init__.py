"""Universal file ingestion for the generic ERP pipeline.

Phase 6 answers "WHAT data or content exists in this uploaded file, and how can
the rest of the pipeline consume it safely?" It does not answer "what canonical
ERP field does this value map to?" - that is Phase 8.

Structure versus content, which is the whole design
---------------------------------------------------
Databases and CSV files expose STRUCTURE. PDFs and images expose DOCUMENT
CONTENT. Both are legitimate ERP sources, and the pipeline supports both
without pretending they are the same thing::

    File
     |
     +-- CSV ----> encoding/delimiter/type inference --> SourceSchema
     |                                                   + streamed SourceRows
     |
     +-- PDF ----> page text extraction, OCR fallback -> ExtractedDocument
     |
     +-- IMAGE --> validation, metadata, OCR ----------> ExtractedDocument

A CSV genuinely has columns, so it produces the same Phase 1 ``SourceSchema``
that PostgreSQL, MySQL, SQL Server and MongoDB discovery produce. A PDF does
not, so no fake tabular schema is invented for it - that would be a fabrication
every later phase would have to work around.

Position in the architecture::

    CSV / PDF / Image
           |
           v
    Phase 6 Ingestion          THIS PACKAGE
           |
           +---- SourceSchema (CSV only) ---> Phase 2 Schema Catalog
           |
           +---- ExtractedDocument --------> later document processing

Source values and operational metadata are separated structurally, not by
convention: raw values live in ``TabularFileResult.iter_records()`` and
``ExtractedPage.text``, while every ``to_dict()``, warning, exception message
and piece of schema metadata carries counts and positions only. See
``models`` for the full rule.

This package never imports ``bpi2020``.
"""

from __future__ import annotations

from erp_pipeline.ingestion.csv_inference import (
    CsvStructureInference,
    build_source_fields,
    classify_value,
    render_source_data_type,
    resolve_field_type,
)
from erp_pipeline.ingestion.csv_ingestion import (
    CsvFileIngestion,
    detect_delimiter,
    detect_encoding,
    ingest_csv_file,
)
from erp_pipeline.ingestion.detection import (
    EXTENSION_MAP,
    DetectionResult,
    detect_file_type,
)
from erp_pipeline.ingestion.errors import (
    EncryptedPDFError,
    FileAccessError,
    FileTooLargeError,
    FileTypeMismatchError,
    ImageDecodeError,
    IngestionError,
    MalformedCSVError,
    MalformedPDFError,
    OCRUnavailableError,
    UnsupportedFileTypeError,
)
from erp_pipeline.ingestion.hashing import (
    HASH_ALGORITHM,
    hash_bytes,
    hash_file,
    make_file_id,
    parse_file_id,
)
from erp_pipeline.ingestion.image_ingestion import ImageFileIngestion, ingest_image_file
from erp_pipeline.ingestion.models import (
    EXTRACTOR_VERSION,
    CsvOptions,
    DocumentFileResult,
    ExtractedDocument,
    ExtractedPage,
    ExtractionStatus,
    ExtractionWarning,
    FieldObservation,
    FileIngestionResult,
    FileProvenance,
    FileSource,
    FileType,
    ImageOptions,
    IngestionOptions,
    PdfOptions,
    SourceRow,
    TabularFileResult,
)
from erp_pipeline.ingestion.ocr import OcrCapability, probe_ocr, resolve_tesseract_command
from erp_pipeline.ingestion.pdf_ingestion import PdfFileIngestion, ingest_pdf_file
from erp_pipeline.ingestion.service import (
    FileIngestionService,
    describe_file,
    ingest_file,
)

__all__ = [
    # service
    "FileIngestionService",
    "ingest_file",
    "describe_file",
    # options
    "IngestionOptions",
    "CsvOptions",
    "PdfOptions",
    "ImageOptions",
    # identity
    "HASH_ALGORITHM",
    "hash_file",
    "hash_bytes",
    "make_file_id",
    "parse_file_id",
    # detection
    "FileType",
    "DetectionResult",
    "detect_file_type",
    "EXTENSION_MAP",
    # results
    "FileIngestionResult",
    "TabularFileResult",
    "DocumentFileResult",
    "FileSource",
    "FileProvenance",
    "ExtractedDocument",
    "ExtractedPage",
    "ExtractionStatus",
    "ExtractionWarning",
    "FieldObservation",
    "SourceRow",
    "EXTRACTOR_VERSION",
    # per-format entry points
    "CsvFileIngestion",
    "ingest_csv_file",
    "PdfFileIngestion",
    "ingest_pdf_file",
    "ImageFileIngestion",
    "ingest_image_file",
    # CSV inference internals, exposed for testing and reuse
    "CsvStructureInference",
    "build_source_fields",
    "classify_value",
    "resolve_field_type",
    "render_source_data_type",
    "detect_encoding",
    "detect_delimiter",
    # OCR capability
    "OcrCapability",
    "probe_ocr",
    "resolve_tesseract_command",
    # errors
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
