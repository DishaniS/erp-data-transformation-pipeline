"""The public entry point for universal file ingestion.

One function, one class, one result contract:

    result = ingest_file("invoices.csv")

    if result.is_tabular:
        result.schema                 # Phase 1 SourceSchema
        for row in result.iter_records():
            ...                       # raw source rows, streamed
    else:
        result.document.pages         # page-level extracted content

The dispatch is deliberately concentrated here so that the parsers stay
ignorant of each other and a new format is added by writing one module plus one
line in ``_INGESTORS``.

This layer owns NO versioning logic. Phase 2's ``SchemaCatalogService`` remains
solely responsible for idempotency, ``catalog_version`` assignment, snapshot
immutability and history - the same boundary Phase 4 and Phase 5 respect.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from erp_pipeline.ingestion.csv_ingestion import CsvFileIngestion
from erp_pipeline.ingestion.detection import DetectionResult, detect_file_type
from erp_pipeline.ingestion.errors import IngestionError, UnsupportedFileTypeError
from erp_pipeline.ingestion.hashing import hash_file, make_file_id
from erp_pipeline.ingestion.image_ingestion import ImageFileIngestion
from erp_pipeline.ingestion.models import (
    DocumentFileResult,
    ExtractionWarning,
    FileIngestionResult,
    FileSource,
    FileType,
    IngestionOptions,
    TabularFileResult,
)
from erp_pipeline.ingestion.pdf_ingestion import PdfFileIngestion
from erp_pipeline.ingestion.safety import validate_file_size, validate_source_path
from erp_pipeline.schemas.source_models import SourceSystem


class FileIngestionService:
    """Detects, validates and ingests local source files.

    Stateless apart from its options, so one instance can safely ingest many
    files in sequence.
    """

    def __init__(self, options: IngestionOptions | None = None) -> None:
        self._options = options or IngestionOptions()

    @property
    def options(self) -> IngestionOptions:
        return self._options

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def ingest(self, path: str | os.PathLike[str]) -> FileIngestionResult:
        """Ingest one file and return a ``TabularFileResult`` or a
        ``DocumentFileResult``.

        Order of operations matters and is fixed:

        1. validate the path (real, readable, regular file);
        2. enforce the size limit from filesystem metadata, BEFORE opening;
        3. detect the type from content and extension;
        4. hash the content and derive the deterministic file identity;
        5. dispatch to the format's parser.

        Steps 1-2 happen before any content is read, so an oversized or
        unsuitable file never reaches memory or a parser.
        """
        resolved = validate_source_path(path)
        size_bytes = validate_file_size(resolved, self._options.max_file_size_bytes)

        detection = detect_file_type(
            resolved, allow_mismatch=self._options.allow_type_mismatch
        )

        file = self._build_file_source(resolved, detection, size_bytes)
        warnings = self._detection_warnings(detection)

        ingestor = _INGESTORS.get(file.file_type)
        if ingestor is None:  # pragma: no cover - FileType is exhaustive
            raise UnsupportedFileTypeError(
                f"No ingestor is registered for {file.file_type.value!r}."
            )

        return ingestor(self, file, warnings)

    def describe(self, path: str | os.PathLike[str]) -> FileSource:
        """Identify a file without parsing it.

        Useful for deduplication: a caller can compute a file's identity and
        check whether that content has already been ingested before paying for
        extraction.
        """
        resolved = validate_source_path(path)
        size_bytes = validate_file_size(resolved, self._options.max_file_size_bytes)
        detection = detect_file_type(
            resolved, allow_mismatch=self._options.allow_type_mismatch
        )
        return self._build_file_source(resolved, detection, size_bytes)

    def source_system(self, name: str | None = None) -> SourceSystem:
        """Build the Phase 1 ``SourceSystem`` these files belong to.

        Offered because publishing a CSV schema requires a registered source
        system, and constructing one correctly (normalized id, right
        ``SourceType``, no credentials) should not be left to every caller.
        """
        return SourceSystem(
            source_system_id=self._options.source_system_id,
            name=name or f"File source ({self._options.source_system_id})",
            source_type=FileType.CSV.to_source_type(),
            description=(
                "Files ingested through erp_pipeline.ingestion. Structure is "
                "inferred from file content, not from declared metadata."
            ),
        )

    def ingest_and_publish(
        self,
        path: str | os.PathLike[str],
        catalog_service: Any,
        register_source_system: SourceSystem | None = None,
    ) -> tuple[FileIngestionResult, Any]:
        """Ingest a structured file and publish its schema through Phase 2.

        Returns ``(result, SchemaSnapshotResult)``. Whether a new
        ``catalog_version`` was created is decided entirely by the catalog;
        this method only forwards the schema.

        Rejects PDFs and images deliberately. The schema catalog stores
        STRUCTURAL descriptions, and a document has no structure to store -
        publishing an empty schema for one would put a meaningless row in the
        catalog and imply a capability that does not exist. Document content
        belongs in a document store, which is a later phase's concern.
        """
        result = self.ingest(path)

        if not isinstance(result, TabularFileResult):
            raise UnsupportedFileTypeError(
                f"{result.file.original_filename!r} is a "
                f"{result.file_type.value} file, which produces extracted "
                "document content rather than a SourceSchema. Only structured "
                "files (CSV) can be published to the schema catalog."
            )

        if register_source_system is not None:
            catalog_service.register_source_system(register_source_system)

        snapshot_result = catalog_service.publish_schema(result.schema)

        return result, snapshot_result

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _build_file_source(
        self, resolved: Path, detection: DetectionResult, size_bytes: int
    ) -> FileSource:
        """Hash the content and assemble the identified file.

        Identity is the content hash. The filename is retained as provenance
        because a human needs it, and the local path is retained only as a
        runtime handle for the parser - neither participates in identity.
        """
        content_hash = hash_file(resolved)

        return FileSource(
            file_id=make_file_id(content_hash),
            content_hash=content_hash,
            original_filename=resolved.name,
            file_type=detection.file_type,
            media_type=detection.media_type,
            size_bytes=size_bytes,
            local_path=resolved,
        )

    @staticmethod
    def _detection_warnings(
        detection: DetectionResult,
    ) -> tuple[ExtractionWarning, ...]:
        if not detection.mismatch:
            return ()

        return (
            ExtractionWarning(
                category="file_type_mismatch",
                message=(
                    f"The file's extension suggests "
                    f"{detection.extension_type.value if detection.extension_type else 'nothing'}"
                    f" but its content is {detection.file_type.value}; the "
                    "content was trusted because allow_type_mismatch is set."
                ),
            ),
        )

    def _ingest_csv(
        self, file: FileSource, warnings: tuple[ExtractionWarning, ...]
    ) -> TabularFileResult:
        result = CsvFileIngestion(
            file, self._options.source_system_id, self._options.csv
        ).ingest()

        if not warnings:
            return result

        return TabularFileResult(
            file=result.file,
            provenance=result.provenance,
            status=result.status,
            warnings=warnings + result.warnings,
            schema=result.schema,
            observations=result.observations,
            header=result.header,
            rows_sampled=result.rows_sampled,
            data_row_count=result.data_row_count,
            _row_reader=result._row_reader,  # noqa: SLF001 - same package
        )

    def _ingest_pdf(
        self, file: FileSource, warnings: tuple[ExtractionWarning, ...]
    ) -> DocumentFileResult:
        ingestion = PdfFileIngestion(
            file, self._options.pdf, self._options.tesseract_cmd
        )
        document = ingestion.ingest()

        return DocumentFileResult(
            file=file,
            provenance=document.provenance,
            status=document.status,
            warnings=warnings + document.warnings,
            document=document,
        )

    def _ingest_image(
        self, file: FileSource, warnings: tuple[ExtractionWarning, ...]
    ) -> DocumentFileResult:
        ingestion = ImageFileIngestion(
            file, self._options.image, self._options.tesseract_cmd
        )
        document = ingestion.ingest()

        return DocumentFileResult(
            file=file,
            provenance=document.provenance,
            status=document.status,
            warnings=warnings + document.warnings,
            document=document,
        )


#: Dispatch table. Adding a format means adding a parser module and one entry.
_INGESTORS: Mapping[
    FileType,
    Callable[[FileIngestionService, FileSource, tuple[ExtractionWarning, ...]],
             FileIngestionResult],
] = {
    FileType.CSV: FileIngestionService._ingest_csv,
    FileType.PDF: FileIngestionService._ingest_pdf,
    FileType.IMAGE: FileIngestionService._ingest_image,
}


def ingest_file(
    path: str | os.PathLike[str], options: IngestionOptions | None = None
) -> FileIngestionResult:
    """Module-level convenience: ingest one file with the given options."""
    return FileIngestionService(options).ingest(path)


def describe_file(
    path: str | os.PathLike[str], options: IngestionOptions | None = None
) -> FileSource:
    """Module-level convenience: identify a file without parsing it."""
    return FileIngestionService(options).describe(path)


__all__ = [
    "FileIngestionService",
    "ingest_file",
    "describe_file",
    "IngestionError",
]
