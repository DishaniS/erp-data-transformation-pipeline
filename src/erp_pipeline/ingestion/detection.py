"""File-type detection from extension AND content.

An extension is a claim, not a fact. ``report.csv`` may hold a PDF, and a
scanner may emit ``scan`` with no extension at all. Detection therefore reads
both signals and reports when they disagree, rather than silently believing
whichever one happened to be checked first.

Detection strategy
------------------
1. **Signature.** Read a small prefix and match it against known magic bytes -
   ``%PDF-``, the PNG 8-byte signature, the JPEG SOI marker, RIFF/WEBP, the
   TIFF byte-order marks. This is authoritative when it matches: those bytes
   are not an accident.
2. **Text probe.** CSV has no signature, so a file is CSV-eligible only if its
   prefix decodes as text and contains no NUL byte. That is what stops
   arbitrary binary content being fed to the CSV parser as "probably text".
3. **Extension.** Used to choose between text formats and to catch the
   mismatch case, never as the sole basis for treating a file as a PDF or an
   image.

``python-magic`` is deliberately not used: it is not installed in this project,
and it would add a libmagic system dependency to detect five formats whose
signatures fit comfortably in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from erp_pipeline.ingestion.errors import FileTypeMismatchError, UnsupportedFileTypeError
from erp_pipeline.ingestion.models import FileType

#: Bytes read for signature and text probing. Large enough to cover every
#: signature below and to make the text probe meaningful, small enough to be
#: free.
SNIFF_BYTES = 4096

#: Extension -> (FileType, media type). Extensions are matched case-insensitively.
EXTENSION_MAP: Mapping[str, tuple[FileType, str]] = {
    ".csv": (FileType.CSV, "text/csv"),
    ".tsv": (FileType.CSV, "text/tab-separated-values"),
    ".pdf": (FileType.PDF, "application/pdf"),
    ".png": (FileType.IMAGE, "image/png"),
    ".jpg": (FileType.IMAGE, "image/jpeg"),
    ".jpeg": (FileType.IMAGE, "image/jpeg"),
    ".webp": (FileType.IMAGE, "image/webp"),
    ".tif": (FileType.IMAGE, "image/tiff"),
    ".tiff": (FileType.IMAGE, "image/tiff"),
}

#: Byte signatures that positively identify a format.
_SIGNATURES: tuple[tuple[bytes, FileType, str], ...] = (
    (b"%PDF-", FileType.PDF, "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", FileType.IMAGE, "image/png"),
    (b"\xff\xd8\xff", FileType.IMAGE, "image/jpeg"),
    (b"II*\x00", FileType.IMAGE, "image/tiff"),
    (b"MM\x00*", FileType.IMAGE, "image/tiff"),
)

#: Encodings tried by the text probe, in order. A UTF-16 CSV decodes here but
#: is left to the CSV reader's own encoding handling to accept or reject.
_TEXT_PROBE_ENCODINGS = ("utf-8-sig", "utf-8")


@dataclass(frozen=True)
class DetectionResult:
    """What detection concluded, and on what evidence.

    Both signals are reported even when they agree, so a caller can always
    audit why a file was treated the way it was.
    """

    file_type: FileType
    media_type: str
    detected_by: str                      # "signature" | "extension" | "content"
    extension_type: FileType | None = None
    content_type: FileType | None = None
    mismatch: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "file_type": self.file_type.value,
            "media_type": self.media_type,
            "detected_by": self.detected_by,
            "extension_type": self.extension_type.value if self.extension_type else None,
            "content_type": self.content_type.value if self.content_type else None,
            "mismatch": self.mismatch,
        }


def read_signature(path: Path, size: int = SNIFF_BYTES) -> bytes:
    """Read the leading bytes used for detection."""
    with open(path, "rb") as handle:
        return handle.read(size)


def detect_from_extension(path: Path) -> tuple[FileType, str] | None:
    """Map a filename extension to a type, or ``None`` if unrecognized."""
    return EXTENSION_MAP.get(path.suffix.lower())


def detect_from_signature(prefix: bytes) -> tuple[FileType, str] | None:
    """Match magic bytes, or ``None`` when no signature applies."""
    for signature, file_type, media_type in _SIGNATURES:
        if prefix.startswith(signature):
            return file_type, media_type

    # WEBP is "RIFF????WEBP" - the size field sits between the two markers.
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return FileType.IMAGE, "image/webp"

    return None


def looks_like_text(prefix: bytes) -> bool:
    """Whether a prefix is plausibly a text file.

    A NUL byte is the decisive negative signal: it does not occur in the text
    encodings this project accepts, and it is present in essentially every
    binary format. Combined with a successful decode, this is what keeps
    arbitrary binary content out of the CSV parser.
    """
    if not prefix:
        # An empty file is text-eligible; the CSV reader reports it as empty.
        return True

    # A UTF-16 BOM declares a text file, even though UTF-16 is full of NUL
    # bytes. Accepting it here means the caller gets the CSV reader's
    # actionable "set encoding='utf-16'" message instead of a generic
    # "this looks like binary" rejection.
    if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True

    if b"\x00" in prefix:
        return False

    for encoding in _TEXT_PROBE_ENCODINGS:
        try:
            prefix.decode(encoding)
            return True
        except UnicodeDecodeError:
            # A multi-byte character straddling the probe boundary is not
            # evidence of binary content, so retry without the tail.
            try:
                prefix[:-4].decode(encoding)
                return True
            except UnicodeDecodeError:
                continue

    return False


def detect_file_type(path: Path, allow_mismatch: bool = False) -> DetectionResult:
    """Determine a file's type from its content and its name.

    Raises ``UnsupportedFileTypeError`` when neither signal yields a supported
    type, and ``FileTypeMismatchError`` when the two disagree - unless
    ``allow_mismatch`` is set, in which case the CONTENT wins and the
    disagreement is reported on the result for the caller to warn about.
    """
    prefix = read_signature(path)

    extension_hit = detect_from_extension(path)
    signature_hit = detect_from_signature(prefix)

    extension_type = extension_hit[0] if extension_hit else None
    content_type = signature_hit[0] if signature_hit else None

    # 1. A positive signature is the strongest evidence available.
    if signature_hit is not None:
        signature_file_type, signature_media_type = signature_hit

        if extension_type is not None and extension_type is not signature_file_type:
            if not allow_mismatch:
                raise FileTypeMismatchError(
                    f"{path.name!r} has a {extension_type.value} extension but "
                    f"its content is {signature_file_type.value}. Refusing to "
                    "guess which is correct; set "
                    "IngestionOptions.allow_type_mismatch=True to trust the "
                    "content.",
                    extension_type=extension_type.value,
                    content_type=signature_file_type.value,
                )

            return DetectionResult(
                file_type=signature_file_type,
                media_type=signature_media_type,
                detected_by="signature",
                extension_type=extension_type,
                content_type=signature_file_type,
                mismatch=True,
            )

        return DetectionResult(
            file_type=signature_file_type,
            media_type=signature_media_type,
            detected_by="signature",
            extension_type=extension_type,
            content_type=signature_file_type,
        )

    # 2. No signature. A text-format extension is believable only if the
    #    content is actually text.
    if extension_hit is not None:
        extension_file_type, extension_media_type = extension_hit

        if extension_file_type is FileType.CSV:
            if not looks_like_text(prefix):
                raise FileTypeMismatchError(
                    f"{path.name!r} has a {extension_file_type.value} extension "
                    "but its content is not decodable text. Refusing to parse "
                    "binary content as CSV.",
                    extension_type=extension_file_type.value,
                    content_type=None,
                )

            return DetectionResult(
                file_type=FileType.CSV,
                media_type=extension_media_type,
                detected_by="extension",
                extension_type=FileType.CSV,
                content_type=FileType.CSV,
            )

        # A .png/.pdf extension with no matching signature is a corrupt or
        # misnamed file. Handing it to a decoder would produce a confusing
        # failure deep inside a third-party library instead of here.
        raise FileTypeMismatchError(
            f"{path.name!r} has a {extension_file_type.value} extension but "
            f"carries no valid {extension_file_type.value} signature. The file "
            "is misnamed, truncated or corrupt.",
            extension_type=extension_file_type.value,
            content_type=None,
        )

    # 3. No signature and no known extension.
    raise UnsupportedFileTypeError(
        f"{path.name!r} is not a supported source file. Phase 6 accepts CSV, "
        f"PDF and image files ({', '.join(sorted(EXTENSION_MAP))}); its "
        "extension is unrecognized and its content matches no known signature."
    )


__all__ = [
    "SNIFF_BYTES",
    "EXTENSION_MAP",
    "DetectionResult",
    "detect_file_type",
    "detect_from_extension",
    "detect_from_signature",
    "looks_like_text",
    "read_signature",
]
