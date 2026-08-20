"""Resource guards applied before and during extraction.

Every function here exists to make one promise: a hostile or merely unlucky
file cannot exhaust this process. The guards are deliberately applied as early
as possible - a size check that happens after the file is in memory has not
protected anything.

Order of operations for any ingested file:

    1. validate_source_path   the path is a real, readable, regular file
    2. validate_file_size     the filesystem's own size metadata, BEFORE open
    3. per-format budgets     rows, columns, field length, pages, characters,
                              pixels - enforced during parsing

Text budgets return a truncation flag rather than raising, because a truncated
extraction that says so is more useful than no extraction at all. Structural
budgets (file size, page count, column count) do raise, because exceeding them
means the caller asked for something outside the configured envelope.
"""

from __future__ import annotations

import os
from pathlib import Path

from erp_pipeline.ingestion.errors import FileAccessError, FileTooLargeError


def validate_source_path(path: str | os.PathLike[str]) -> Path:
    """Resolve and validate a local path, or raise ``FileAccessError``.

    Rejects directories, missing paths, and anything that is not a regular
    file - a FIFO, a device node or a socket would otherwise block forever or
    stream unbounded data into a parser.

    Symlinks are followed (``resolve()``), which is intentional: this API is
    called by trusted application code with a path it chose, not by an
    untrusted uploader choosing server-side paths. Phase 6 builds no HTTP
    upload endpoint, and the moment one is added it - not this function -
    becomes responsible for deciding which paths a remote caller may name.
    """
    if path is None:
        raise FileAccessError("A file path is required.")

    try:
        candidate = Path(path).expanduser()
    except (TypeError, ValueError) as exc:
        raise FileAccessError(f"Not a usable file path: {type(path).__name__}.") from exc

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileAccessError(f"No such file: {candidate.name!r}.") from exc
    except OSError as exc:
        raise FileAccessError(f"Could not resolve path {candidate.name!r}.") from exc

    if resolved.is_dir():
        raise FileAccessError(
            f"{resolved.name!r} is a directory. Ingestion accepts one file at a "
            "time; enumerating a folder is the caller's decision."
        )

    if not resolved.is_file():
        raise FileAccessError(
            f"{resolved.name!r} is not a regular file. Device files, sockets "
            "and FIFOs are refused because they have no bounded size."
        )

    if not os.access(resolved, os.R_OK):
        raise FileAccessError(f"{resolved.name!r} is not readable.")

    return resolved


def file_size_bytes(path: Path) -> int:
    """Size from filesystem metadata, without opening the file."""
    try:
        return path.stat().st_size
    except OSError as exc:
        raise FileAccessError(f"Could not stat {path.name!r}.") from exc


def validate_file_size(path: Path, max_bytes: int) -> int:
    """Enforce the file-size limit BEFORE any content is read.

    Returns the size so a caller does not have to stat the file twice.
    """
    size = file_size_bytes(path)

    if size > max_bytes:
        raise FileTooLargeError(
            f"{path.name!r} is {size} bytes, which exceeds the configured "
            f"limit of {max_bytes} bytes. Raise "
            "IngestionOptions.max_file_size_bytes to accept it.",
            size_bytes=size,
            limit_bytes=max_bytes,
        )

    return size


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Apply a character budget, reporting whether it bit.

    Returns ``(text, truncated)``. Truncation is always reported so a caller is
    never told that a partial extraction was complete.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False

    return text[:max_chars], True


class TextBudget:
    """A running character budget shared across the pages of one document.

    Per-page limits alone are not enough: 500 pages each just under a
    200 000-character cap would still produce 100 million characters. This
    tracks the document-wide total and hands out what remains.
    """

    def __init__(self, max_total_chars: int) -> None:
        self._max_total = max_total_chars
        self._used = 0
        self._exhausted = False

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return max(self._max_total - self._used, 0)

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def take(self, text: str) -> tuple[str, bool]:
        """Consume as much of ``text`` as the budget allows."""
        if self.remaining <= 0:
            self._exhausted = True
            return "", True

        accepted, truncated = truncate_text(text, self.remaining)
        self._used += len(accepted)

        if truncated:
            self._exhausted = True

        return accepted, truncated


__all__ = [
    "validate_source_path",
    "file_size_bytes",
    "validate_file_size",
    "truncate_text",
    "TextBudget",
]
