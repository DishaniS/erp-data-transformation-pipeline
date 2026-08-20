"""Deterministic, content-based file identity.

A file's identity is what is INSIDE it, never where it happens to sit or what
it happens to be called. Two uploads of the same bytes are the same file even
under different names; an edited file is a different file even at the same
path.

This is the same principle Phase 1's ``compute_content_hash`` applies to
records, and the reason both exist rather than a random UUID: re-ingesting an
unchanged file must be recognizable as a no-op, and that is only possible if
identity is derived from content.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: SHA-256: collision-resistant, universally available, and cheap enough to
#: run over an entire upload. MD5/SHA-1 are unsuitable for an identity that
#: may later be used to decide two files are "the same".
HASH_ALGORITHM = "sha256"

#: Read in 1 MiB blocks so hashing a large file never loads it into memory.
_CHUNK_SIZE = 1024 * 1024

#: Namespace prefix, so a file id is self-describing and can never be confused
#: with a bare hash or with a Phase 1 canonical id (which uses "erp:").
FILE_ID_PREFIX = "file"


def hash_bytes(payload: bytes) -> str:
    """SHA-256 hex digest of an in-memory payload."""
    return hashlib.sha256(payload).hexdigest()


def hash_file(path: Path) -> str:
    """SHA-256 hex digest of a file, streamed in bounded chunks."""
    digest = hashlib.sha256()

    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)

    return digest.hexdigest()


def make_file_id(content_hash: str) -> str:
    """Build the deterministic file identity from a content hash.

    Format: ``file.sha256.<hex>``.

    Every character is drawn from ``[a-z0-9.]``, so the result is already a
    valid normalized identifier under ``schemas.identity.normalize_identifier``
    and can be embedded in composite ids without being rewritten.

    Deliberately excludes the filename, the path and the ingestion time. Same
    bytes -> same id, always.
    """
    if not content_hash or not isinstance(content_hash, str):
        raise ValueError("A file id requires a non-empty content hash.")

    return f"{FILE_ID_PREFIX}.{HASH_ALGORITHM}.{content_hash.lower()}"


def parse_file_id(file_id: str) -> str:
    """Recover the content hash from a file id.

    Proves the format is unambiguous, which is what lets a stored file id be
    matched against a freshly hashed upload without re-deriving conventions.
    """
    prefix = f"{FILE_ID_PREFIX}.{HASH_ALGORITHM}."

    if not isinstance(file_id, str) or not file_id.startswith(prefix):
        raise ValueError(
            f"{file_id!r} is not a file id. Expected '{prefix}<hex digest>'."
        )

    return file_id[len(prefix):]


__all__ = [
    "HASH_ALGORITHM",
    "FILE_ID_PREFIX",
    "hash_bytes",
    "hash_file",
    "make_file_id",
    "parse_file_id",
]
