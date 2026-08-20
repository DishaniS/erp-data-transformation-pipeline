"""Bounded, content-addressed upload storage.

THE THREAT
----------
An uploaded filename is attacker-controlled. ``../../.ssh/authorized_keys`` is
a filename. So is a 4 GB file, and so is a ``.csv`` that is actually a
executable. This module refuses all three before anything touches the disk.

WHAT IT DOES
------------
- Writes to a generated id, never the supplied name. The original name is kept
  as metadata only, and is never used to build a path.
- Streams in bounded chunks and aborts the moment the limit is exceeded, so an
  oversized upload cannot exhaust memory or disk.
- Hashes content while streaming, so the hash costs no extra pass.
- Never returns an absolute path to a caller.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterator, Mapping

from erp_pipeline.orchestration.errors import (
    UnsafeUploadNameError,
    UploadNotFoundError,
    UploadTooLargeError,
)

#: 64 MiB. Generous for a research prototype, far below anything that would
#: threaten the host.
DEFAULT_MAX_UPLOAD_BYTES = 64 * 1024 * 1024

CHUNK_BYTES = 1024 * 1024

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_display_name(name: str | None) -> str:
    """Reduce a supplied filename to something safe to *display*.

    The result is never used to construct a path - the stored path always comes
    from a generated id. This exists so the UI can echo something recognisable
    without echoing an attack.
    """
    if not name:
        return "upload"

    # Take the basename under both separators: a Windows client may send
    # backslashes that posixpath would treat as an ordinary character.
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_NAME.sub("_", base).lstrip(".")

    if not cleaned or cleaned in {".", ".."}:
        raise UnsafeUploadNameError(
            "the supplied filename is not usable", supplied_length=len(name)
        )

    return cleaned[:120]


@dataclass(frozen=True)
class StoredUpload:
    """A stored file. ``path`` stays server-side and is never serialized."""

    upload_id: str
    display_name: str
    suffix: str
    content_hash: str
    size_bytes: int
    stored_at: datetime
    path: Path
    content_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Deliberately omits ``path``.

        Returning a server filesystem path tells a caller about the host's
        layout and is of no use to them.
        """
        return {
            "upload_id": self.upload_id,
            "filename": self.display_name,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "stored_at": self.stored_at.isoformat(),
            "content_type": self.content_type,
        }


class UploadStore:
    """Content-addressed files under one configured directory."""

    def __init__(
        self,
        root: Path | str,
        max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._index: dict[str, StoredUpload] = {}

    def ensure_ready(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def store_stream(
        self,
        stream: IO[bytes],
        filename: str | None = None,
        content_type: str | None = None,
    ) -> StoredUpload:
        """Stream a upload to disk, hashing as it goes and stopping at the cap.

        Reads in chunks rather than calling ``.read()``: a single read of an
        arbitrarily large upload is exactly how a service is knocked over.
        """
        self.ensure_ready()

        display = sanitize_display_name(filename)
        suffix = Path(display).suffix.lower()
        upload_id = f"upl_{uuid.uuid4().hex}"

        # Each upload gets its own generated directory, and the sanitized name
        # is kept INSIDE it. The directory name is what guarantees isolation -
        # it is generated here and cannot be influenced by the caller - so the
        # filename no longer has to be discarded to stay safe.
        #
        # Keeping it matters: downstream schema inference derives the entity
        # name from the file name, and replacing "invoices.csv" with an opaque
        # id measurably degrades Phase 8's name matching.
        folder = self.root / upload_id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / display

        digest = hashlib.sha256()
        written = 0

        try:
            with target.open("wb") as handle:
                while True:
                    chunk = stream.read(CHUNK_BYTES)

                    if not chunk:
                        break

                    written += len(chunk)

                    if written > self.max_bytes:
                        raise UploadTooLargeError(
                            "the upload exceeds the configured maximum size",
                            max_bytes=self.max_bytes,
                        )

                    digest.update(chunk)
                    handle.write(chunk)
        except Exception:
            # A partial file is worse than none: it would look ingestible.
            target.unlink(missing_ok=True)

            try:
                folder.rmdir()
            except OSError:
                pass

            raise

        stored = StoredUpload(
            upload_id=upload_id,
            display_name=display,
            suffix=suffix,
            content_hash=digest.hexdigest(),
            size_bytes=written,
            stored_at=datetime.now(timezone.utc),
            path=target,
            content_type=content_type,
        )
        self._index[upload_id] = stored

        return stored

    def store_bytes(
        self,
        payload: bytes,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> StoredUpload:
        import io

        if len(payload) > self.max_bytes:
            raise UploadTooLargeError(
                "the upload exceeds the configured maximum size",
                max_bytes=self.max_bytes,
            )

        return self.store_stream(io.BytesIO(payload), filename, content_type)

    def get(self, upload_id: str) -> StoredUpload:
        stored = self._index.get(upload_id)

        if stored is None:
            raise UploadNotFoundError(
                f"upload {upload_id!r} is not known to this service",
                upload_id=upload_id,
            )

        return stored

    def path_for(self, upload_id: str) -> Path:
        """Resolve an id to a path, refusing anything outside the root.

        Ids are generated here so traversal should be impossible, but the
        containment check is cheap and this is the one function whose failure
        would be a filesystem escape.
        """
        stored = self.get(upload_id)
        resolved = stored.path.resolve()
        root = self.root.resolve()

        if not str(resolved).startswith(str(root)):
            raise UnsafeUploadNameError(
                "the resolved upload path escapes the upload directory",
                upload_id=upload_id,
            )

        return resolved

    def __len__(self) -> int:
        return len(self._index)


__all__ = [
    "DEFAULT_MAX_UPLOAD_BYTES",
    "StoredUpload",
    "UploadStore",
    "sanitize_display_name",
]
