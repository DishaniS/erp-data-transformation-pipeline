"""CSV ingestion: encoding, delimiter, streaming, and ``SourceSchema``.

The I/O half of CSV handling. Every structural rule lives in
``csv_inference``; this module owns files, encodings, the delimiter decision
and the streaming reader, and assembles the Phase 1 contract.

Why the standard library and not pandas
---------------------------------------
``pandas`` is a project dependency, but it is the wrong tool here. It reads a
frame into memory, which defeats the streaming requirement; it applies its own
type coercion, which would pre-empt the inference this phase is supposed to
perform explicitly and document; and it rewrites duplicate headers
(``amount``, ``amount.1``) before this code ever sees them, destroying exactly
the source fidelity Step 10 requires. ``csv`` from the standard library reads
one row at a time and changes nothing.

READ-ONLY
---------
This module opens files for reading only. It writes nothing, creates nothing
and deletes nothing; ``tests/erp_pipeline/ingestion/test_ingestion_safety.py``
proves it by AST inspection.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from erp_pipeline.ingestion.csv_inference import (
    CsvStructureInference,
    build_source_fields,
)
from erp_pipeline.ingestion.errors import MalformedCSVError
from erp_pipeline.ingestion.models import (
    CANDIDATE_DELIMITERS,
    CsvOptions,
    ExtractionStatus,
    ExtractionWarning,
    FieldObservation,
    FileProvenance,
    FileSource,
    SourceRow,
    TabularFileResult,
)
from erp_pipeline.schemas.enums import EntityKind, SchemaOrigin
from erp_pipeline.schemas.identity import IdentityError, normalize_identifier
from erp_pipeline.schemas.source_models import SourceEntity, SourceSchema

EXTRACTOR_NAME = "python-csv"

#: Byte-order marks recognized when choosing an encoding. Only the UTF-8 BOM
#: is handled transparently; the UTF-16 marks are detected so that a UTF-16
#: file produces a clear, actionable error instead of mojibake.
_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"

_PROVISIONAL_SCHEMA_ID = "provisional.schema.id"


# ============================================================
# Encoding (Step 12)
# ============================================================

def detect_encoding(path: Path, override: str | None = None) -> str:
    """Choose the encoding to read a CSV with.

    Deliberately narrow. No statistical encoding detector is installed in this
    project, and guessing an encoding from byte frequencies is exactly the kind
    of silent, occasionally-wrong decision that corrupts ERP data in ways
    nobody notices until much later. So:

    * an explicit ``CsvOptions.encoding`` always wins;
    * a UTF-8 BOM selects ``utf-8-sig``, which strips it;
    * everything else is read as UTF-8, and a file that is not UTF-8 produces a
      controlled ``MalformedCSVError`` naming the byte offset - never a
      best-effort decode with replacement characters presented as success.
    """
    if override:
        return override

    with open(path, "rb") as handle:
        prefix = handle.read(4)

    if prefix.startswith(_BOM_UTF8):
        return "utf-8-sig"

    if prefix.startswith(_BOM_UTF16_LE) or prefix.startswith(_BOM_UTF16_BE):
        raise MalformedCSVError(
            f"{path.name!r} begins with a UTF-16 byte-order mark. Set "
            "CsvOptions.encoding='utf-16' explicitly to read it; it is not "
            "decoded automatically because a wrong guess corrupts data "
            "silently."
        )

    return "utf-8"


@contextmanager
def _open_csv(path: Path, encoding: str):
    """Open a CSV for reading, translating decode failures.

    ``newline=""`` is required by the ``csv`` module so quoted fields
    containing newlines survive. ``errors="strict"`` is the privacy- and
    correctness-relevant choice: replacing undecodable bytes would silently
    alter source values.
    """
    handle = None
    try:
        handle = open(path, "r", encoding=encoding, newline="", errors="strict")
        yield handle
    except UnicodeDecodeError as exc:
        raise MalformedCSVError(
            f"{path.name!r} is not valid {encoding}: an undecodable byte "
            f"sequence begins at byte offset {exc.start}. Set "
            "CsvOptions.encoding to the file's real encoding.",
            byte_offset=exc.start,
        ) from exc
    finally:
        if handle is not None:
            handle.close()


@contextmanager
def _field_size_limit(limit: int):
    """Bound the ``csv`` module's per-field size for the duration of a parse.

    ``csv.field_size_limit`` is process-global, so it is saved and restored.
    Setting it is what makes a single pathological field a controlled error
    rather than an unbounded allocation.
    """
    previous = csv.field_size_limit()
    try:
        csv.field_size_limit(limit)
        yield
    finally:
        csv.field_size_limit(previous)


# ============================================================
# Delimiter (Step 11)
# ============================================================

def detect_delimiter(
    path: Path, encoding: str, options: CsvOptions
) -> tuple[str, str]:
    """Choose a delimiter deterministically. Returns ``(delimiter, how)``.

    ``csv.Sniffer`` is not trusted on its own: it raises on single-column
    files, is confused by punctuation inside quoted text, and its choice is
    not stable across similar inputs. Instead each candidate is scored by
    parsing the first N lines WITH that delimiter and asking two questions
    that actually matter:

    1. Does it split the header into more than one field? A delimiter that
       yields one column has not been found in the file.
    2. Do the data rows agree with the header's field count? A real delimiter
       produces a consistent shape; an accidental one does not.

    Ties break on ``CANDIDATE_DELIMITERS`` order (comma first), so the same
    file always yields the same answer.
    """
    if options.delimiter is not None:
        return options.delimiter, "explicit"

    with _open_csv(path, encoding) as handle:
        sample_lines = []
        for line in handle:
            sample_lines.append(line)
            if len(sample_lines) >= options.delimiter_sniff_lines:
                break

    if not sample_lines:
        return ",", "default_empty_file"

    sample = "".join(sample_lines)
    best_delimiter = ","
    best_score = (-1, 0)

    for candidate in CANDIDATE_DELIMITERS:
        try:
            rows = list(
                csv.reader(sample.splitlines(), delimiter=candidate,
                           quotechar=options.quote_char)
            )
        except csv.Error:
            continue

        rows = [row for row in rows if row]
        if not rows:
            continue

        header_width = len(rows[0])
        if header_width < 2:
            continue

        body = rows[1:]
        consistent = sum(1 for row in body if len(row) == header_width)
        # Primary signal: how many columns it finds. Secondary: how many rows
        # agree with that shape.
        score = (header_width, consistent)

        if score > best_score:
            best_score = score
            best_delimiter = candidate

    if best_score == (-1, 0):
        # No candidate split anything. A genuinely single-column CSV is
        # legitimate, so comma is used and the fact is reported.
        return ",", "single_column_fallback"

    return best_delimiter, "detected"


# ============================================================
# Reading (Steps 13, 20, 43)
# ============================================================

def read_header(path: Path, encoding: str, delimiter: str,
                options: CsvOptions) -> tuple[str, ...]:
    """Read the header row exactly as the file states it.

    Names are NOT normalized, deduplicated or cleaned here. Whatever the export
    wrote - spaces, punctuation, duplicates, an empty cell - is preserved as
    ``SourceField.source_name``; disambiguation happens only on the normalized
    name, in ``csv_inference``.
    """
    with _field_size_limit(options.max_field_length):
        with _open_csv(path, encoding) as handle:
            reader = csv.reader(handle, delimiter=delimiter,
                                quotechar=options.quote_char)
            try:
                for row in reader:
                    return tuple(row)
            except csv.Error as exc:
                raise MalformedCSVError(
                    f"{path.name!r} could not be parsed: its header row is "
                    f"malformed or exceeds the configured field-size limit of "
                    f"{options.max_field_length} characters.",
                    row_number=0,
                ) from exc

    return ()


def iter_source_rows(
    path: Path,
    encoding: str,
    delimiter: str,
    options: CsvOptions,
    header: Sequence[str],
    field_names: Sequence[str],
    file_id: str,
) -> Iterator[SourceRow]:
    """Stream every data row as a ``SourceRow``.

    Lazy and bounded: one physical row is held at a time, so a multi-gigabyte
    CSV is processed with a flat memory profile. Values are the source's own
    strings, unconverted - the whole point of this iterator is to hand a later
    mapping phase the data it needs to transform.

    ``field_names`` are the deduplicated normalized names, so a row dictionary
    from a file with duplicate headers still has one entry per physical column.

    A row the parser cannot read raises ``MalformedCSVError`` here, whereas
    schema inference merely warns and skips it. The asymmetry is deliberate:
    inference reads a SAMPLE to describe structure, so skipping one row costs
    nothing and is reported. This iterator is the DATA handoff, and silently
    dropping a row would lose business data that a mapping phase would then
    never know was missing.
    """
    with _field_size_limit(options.max_field_length):
        with _open_csv(path, encoding) as handle:
            reader = csv.reader(handle, delimiter=delimiter,
                                quotechar=options.quote_char)

            if options.has_header:
                next(reader, None)

            width = len(field_names)
            row_number = 0

            while True:
                row_number += 1
                try:
                    values = next(reader)
                except StopIteration:
                    break
                except csv.Error as exc:
                    # The message names the position and the limit only - never
                    # any part of the offending row.
                    raise MalformedCSVError(
                        f"Row {row_number} of {path.name!r} could not be parsed: "
                        "it is malformed or contains a field longer than the "
                        f"configured limit of {options.max_field_length} "
                        "characters.",
                        row_number=row_number,
                    ) from exc

                if not values:
                    row_number -= 1
                    continue

                mapped: dict[str, str | None] = {}
                missing: list[str] = []

                for index, name in enumerate(field_names):
                    if index < len(values):
                        mapped[name] = values[index]
                    else:
                        mapped[name] = None
                        missing.append(name)

                yield SourceRow(
                    row_number=row_number,
                    values=mapped,
                    file_id=file_id,
                    missing_fields=tuple(missing),
                    extra_value_count=max(len(values) - width, 0),
                )


# ============================================================
# Ingestion
# ============================================================

class CsvFileIngestion:
    """Parses one CSV file into a Phase 1 ``SourceSchema`` plus streamed rows."""

    def __init__(
        self,
        file: FileSource,
        source_system_id: str,
        options: CsvOptions | None = None,
    ) -> None:
        self._file = file
        self._source_system_id = source_system_id
        self._options = options or CsvOptions()
        self._warnings: list[ExtractionWarning] = []

    @property
    def warnings(self) -> tuple[ExtractionWarning, ...]:
        return tuple(self._warnings)

    def ingest(self) -> TabularFileResult:
        path = self._require_local_path()
        options = self._options

        encoding = detect_encoding(path, options.encoding)
        delimiter, delimiter_source = detect_delimiter(path, encoding, options)

        if delimiter_source == "single_column_fallback":
            self._warn(
                "delimiter_not_detected",
                "No delimiter split this file into multiple columns; treating "
                "it as a single-column CSV.",
            )

        header = read_header(path, encoding, delimiter, options)

        if not header:
            return self._empty_result(encoding, delimiter)

        if len(header) > options.max_columns:
            raise MalformedCSVError(
                f"{path.name!r} declares {len(header)} columns, which exceeds "
                f"the configured limit of {options.max_columns}.",
                row_number=0,
            )

        if not options.has_header:
            # Positional names, so a headerless export is still describable.
            header = tuple(f"column_{index + 1}" for index in range(len(header)))

        observations, rows_sampled = self._infer_structure(
            path, encoding, delimiter, header
        )
        inferred = build_source_fields(observations, options)

        for note in inferred.notes:
            self._warn("header_normalization", note)

        field_names = tuple(field.normalized_name for field in inferred.fields)
        schema = self._build_schema(inferred.fields, encoding, delimiter, rows_sampled)

        provenance = FileProvenance(
            file_id=self._file.file_id,
            content_hash=self._file.content_hash,
            original_filename=self._file.original_filename,
            file_type=self._file.file_type,
            media_type=self._file.media_type,
            size_bytes=self._file.size_bytes,
            extractor=EXTRACTOR_NAME,
            encoding=encoding,
            delimiter=delimiter,
            row_count=rows_sampled,
            column_count=len(header),
        )

        def row_reader() -> Iterator[SourceRow]:
            return iter_source_rows(
                path, encoding, delimiter, options, header, field_names,
                self._file.file_id,
            )

        return TabularFileResult(
            file=self._file,
            provenance=provenance,
            status=(
                ExtractionStatus.EXTRACTED
                if rows_sampled
                else ExtractionStatus.NO_CONTENT_DETECTED
            ),
            warnings=self.warnings,
            schema=schema,
            observations=observations,
            header=tuple(header),
            rows_sampled=rows_sampled,
            _row_reader=row_reader,
        )

    # ------------------------------------------------------------
    # Structure inference
    # ------------------------------------------------------------

    def _infer_structure(
        self, path: Path, encoding: str, delimiter: str, header: Sequence[str]
    ) -> tuple[tuple[FieldObservation, ...], int]:
        """Sample a bounded number of data rows and observe their shape.

        Bounded by ``max_rows_for_schema_inference``: inferring a schema does
        not require reading a 10-million-row export, and reading one would make
        an interactive upload unusable. The full file remains available through
        ``iter_records()``.
        """
        options = self._options
        inference = CsvStructureInference(header, options)
        malformed = 0

        with _field_size_limit(options.max_field_length):
            with _open_csv(path, encoding) as handle:
                reader = csv.reader(handle, delimiter=delimiter,
                                    quotechar=options.quote_char)

                if options.has_header:
                    next(reader, None)

                row_number = 0
                while inference.rows_sampled < options.max_rows_for_schema_inference:
                    try:
                        values = next(reader)
                    except StopIteration:
                        break
                    except csv.Error as exc:
                        malformed += 1
                        row_number += 1
                        self._warn(
                            "malformed_row",
                            "Row could not be parsed and was skipped.",
                            row_number=row_number,
                        )
                        if malformed > options.max_errors:
                            raise MalformedCSVError(
                                f"{path.name!r} produced more than "
                                f"{options.max_errors} malformed rows; giving "
                                "up rather than inferring a schema from a file "
                                "this damaged.",
                                row_number=row_number,
                            ) from exc
                        continue

                    row_number += 1

                    if not values:
                        continue

                    if len(values) != len(header):
                        malformed += 1
                        self._warn(
                            "row_width_mismatch",
                            f"Row has {len(values)} values but the header "
                            f"declares {len(header)} columns.",
                            row_number=row_number,
                        )
                        if malformed > options.max_errors:
                            raise MalformedCSVError(
                                f"{path.name!r} produced more than "
                                f"{options.max_errors} malformed rows; giving "
                                "up rather than inferring a schema from a file "
                                "this damaged.",
                                row_number=row_number,
                            )

                    inference.observe(values)

        return inference.observations(), inference.rows_sampled

    # ------------------------------------------------------------
    # Phase 1 contract assembly (Steps 17, 18)
    # ------------------------------------------------------------

    def _build_schema(
        self,
        fields: Sequence[Any],
        encoding: str,
        delimiter: str,
        rows_sampled: int,
    ) -> SourceSchema:
        entity_name = self._entity_name()

        entity = SourceEntity(
            entity_id=self._entity_id(entity_name),
            source_name=self._file.original_filename,
            normalized_name=entity_name,
            # A CSV is a dataset, not a table: it has no engine, no catalog and
            # no declared constraints.
            entity_kind=EntityKind.DATASET,
            namespace=None,
            fields=tuple(fields),
            # A CSV declares no key. Inventing one would assert a constraint
            # the file does not have.
            primary_key_fields=(),
            description=None,
            metadata={
                "source_filename": self._file.original_filename,
                "content_hash": self._file.content_hash,
                "inference_method": "bounded_row_sample",
                "schema_claim": "observed",
                "encoding": encoding,
                "delimiter": delimiter,
                "has_header": self._options.has_header,
                "column_count": len(fields),
                "sample": {
                    "rows_sampled": rows_sampled,
                    "max_rows_for_schema_inference": (
                        self._options.max_rows_for_schema_inference
                    ),
                    "full_scan": False,
                },
            },
        )

        # Two-pass build, exactly as in Phase 4/5: compute_schema_hash()
        # excludes schema_id, so a provisional id computes the hash and the
        # final content-addressed id derives from it.
        provisional = self._assemble(_PROVISIONAL_SCHEMA_ID, entity_name, entity, None)
        structural_hash = provisional.compute_schema_hash()

        return self._assemble(
            self._schema_id(entity_name, structural_hash),
            entity_name,
            entity,
            structural_hash,
        )

    def _assemble(
        self,
        schema_id: str,
        schema_name: str,
        entity: SourceEntity,
        schema_hash: str | None,
    ) -> SourceSchema:
        return SourceSchema(
            schema_id=schema_id,
            source_system_id=self._source_system_id,
            schema_name=schema_name,
            # Inferred from rows, never read from declared metadata. A CSV has
            # no catalog to discover.
            origin=SchemaOrigin.INFERRED,
            entities=(entity,),
            # A single CSV file describes no relationships. Two files that look
            # joinable are not evidence of a declared constraint.
            relationships=(),
            schema_hash=schema_hash,
            metadata={
                "file_type": self._file.file_type.value,
                "media_type": self._file.media_type,
                "content_hash": self._file.content_hash,
                "original_filename": self._file.original_filename,
                "inference_method": "bounded_row_sample",
                "schema_claim": "observed",
                "observed_schema_note": (
                    "Observed/inferred schema. Derived from a bounded sample of "
                    "rows in a delimited text file, which declares no types, no "
                    "keys and no constraints."
                ),
                "relationship_inference": "disabled",
            },
        )

    def _entity_name(self) -> str:
        """Deterministic entity name from the filename stem.

        This is the STABLE logical scope Phase 2 versions snapshots within, so
        it must NOT move when the file's content changes - otherwise an edited
        CSV would start a fresh version-1 history instead of incrementing the
        existing one. That is why the content hash is deliberately absent here
        even though it IS the file's identity: identity and scope answer
        different questions.

            file_id       which bytes are these?        content hash
            entity name   which dataset is this?        filename stem
        """
        stem = Path(self._file.original_filename).stem

        try:
            return normalize_identifier(stem)
        except IdentityError:
            # A filename that normalizes to nothing ("---.csv") still needs a
            # name; deriving it from the content hash keeps it deterministic.
            return f"dataset.{self._file.content_hash[:12]}"

    def _entity_id(self, entity_name: str) -> str:
        return normalize_identifier(f"{self._source_system_id}.{entity_name}")

    def _schema_id(self, entity_name: str, structural_hash: str) -> str:
        """Content-addressed snapshot identity, as in Phase 4 and Phase 5.

            unchanged structure -> identical id -> still catalog version 1
            changed structure   -> new id       -> catalog version N+1
        """
        return normalize_identifier(
            f"{self._source_system_id}.{entity_name}.{structural_hash[:12]}"
        )

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    def _empty_result(self, encoding: str, delimiter: str) -> TabularFileResult:
        """A file with no header row at all - reported, never guessed at."""
        self._warn(
            "empty_file",
            "The file contains no header row, so no columns could be observed.",
        )

        entity_name = self._entity_name()
        entity = SourceEntity(
            entity_id=self._entity_id(entity_name),
            source_name=self._file.original_filename,
            normalized_name=entity_name,
            entity_kind=EntityKind.DATASET,
            fields=(),
            metadata={
                "source_filename": self._file.original_filename,
                "schema_claim": "observed",
                "empty": True,
            },
        )
        provisional = self._assemble(_PROVISIONAL_SCHEMA_ID, entity_name, entity, None)
        structural_hash = provisional.compute_schema_hash()
        schema = self._assemble(
            self._schema_id(entity_name, structural_hash), entity_name, entity,
            structural_hash,
        )

        provenance = FileProvenance(
            file_id=self._file.file_id,
            content_hash=self._file.content_hash,
            original_filename=self._file.original_filename,
            file_type=self._file.file_type,
            media_type=self._file.media_type,
            size_bytes=self._file.size_bytes,
            extractor=EXTRACTOR_NAME,
            encoding=encoding,
            delimiter=delimiter,
            row_count=0,
            column_count=0,
        )

        return TabularFileResult(
            file=self._file,
            provenance=provenance,
            status=ExtractionStatus.NO_CONTENT_DETECTED,
            warnings=self.warnings,
            schema=schema,
            observations=(),
            header=(),
            rows_sampled=0,
            _row_reader=lambda: iter(()),
        )

    def _require_local_path(self) -> Path:
        if self._file.local_path is None:  # pragma: no cover - guarded upstream
            raise MalformedCSVError(
                "This FileSource carries no readable local path."
            )
        return self._file.local_path

    def _warn(
        self,
        category: str,
        message: str,
        row_number: int | None = None,
    ) -> None:
        """Record a non-fatal problem.

        Callers pass a message built from POSITIONS and COUNTS only. No cell
        contents ever reach this method.
        """
        self._warnings.append(
            ExtractionWarning(
                category=category, message=message, row_number=row_number
            )
        )


def ingest_csv_file(
    file: FileSource,
    source_system_id: str,
    options: CsvOptions | None = None,
) -> TabularFileResult:
    """Convenience wrapper around ``CsvFileIngestion``."""
    return CsvFileIngestion(file, source_system_id, options).ingest()


__all__ = [
    "EXTRACTOR_NAME",
    "CsvFileIngestion",
    "ingest_csv_file",
    "detect_encoding",
    "detect_delimiter",
    "read_header",
    "iter_source_rows",
]
