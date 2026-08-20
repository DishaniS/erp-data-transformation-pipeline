"""OCR capability detection and invocation, shared by PDF and image ingestion.

Separated from both callers because OCR is a CAPABILITY, not a file format:
whether text can be recovered from pixels depends on an external binary being
installed, and both the PDF fallback path and image ingestion need the same
answer to the same question.

The central rule (Step 27)
--------------------------
Missing OCR is never reported as "no text found". Those are different facts
with different remedies - one needs a Tesseract install, the other needs a
better scan - and collapsing them would leave a downstream phase unable to
tell whether re-running would help. ``OcrCapability`` therefore carries an
explicit ``reason`` when unavailable, and callers set
``ExtractionStatus.OCR_UNAVAILABLE`` rather than returning an empty string.

Configuration (Step 26)
-----------------------
No developer-specific path is hardcoded. The executable is resolved in this
order:

    1. an explicit ``IngestionOptions.tesseract_cmd``
    2. the ``TESSERACT_CMD`` environment variable  (this repository's
       convention, already used by ``.env.example``)
    3. the ``TESSERACT_PATH`` environment variable (the repository's older
       name, still honoured as a fallback)
    4. ordinary PATH discovery via ``shutil.which``

PRIVACY
-------
OCR output is source content and frequently contains names, addresses and
financial details. Nothing in this module logs, prints, truncates-for-preview
or embeds recognized text in an exception. Text is returned to the caller and
nowhere else.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any

#: This repository's environment variable for the Tesseract executable, with
#: the older name kept as a fallback (see src/bpi2020/common/config.py, which
#: established the same pair for the Phase 0 prototype).
TESSERACT_ENV_VARS: tuple[str, ...] = ("TESSERACT_CMD", "TESSERACT_PATH")

OCR_ENGINE_NAME = "tesseract"


@dataclass(frozen=True)
class OcrCapability:
    """Whether OCR can run, and why not when it cannot.

    ``reason`` is populated only when ``available`` is False, and states which
    step failed - driver missing, binary missing, or binary unusable - so the
    fix is obvious from the result alone.
    """

    available: bool
    engine: str | None = None
    version: str | None = None
    command: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "engine": self.engine,
            "version": self.version,
            # The resolved command is a local filesystem path, so it is
            # reported only as present/absent in the portable payload.
            "command_configured": self.command is not None,
            "reason": self.reason,
        }


def resolve_tesseract_command(explicit: str | None = None) -> str | None:
    """Locate the Tesseract executable, or return ``None``."""
    if explicit:
        return explicit

    for variable in TESSERACT_ENV_VARS:
        configured = os.environ.get(variable)
        if configured and configured.strip():
            return configured.strip()

    return shutil.which("tesseract")


def probe_ocr(tesseract_cmd: str | None = None) -> OcrCapability:
    """Determine whether OCR is usable right now.

    Deliberately not cached. A cached negative would survive an operator
    installing Tesseract mid-session, and the probe is a single cheap
    subprocess call.
    """
    try:
        import pytesseract
    except ImportError:
        return OcrCapability(
            available=False,
            reason=(
                "The 'pytesseract' package is not installed. Install it with: "
                "pip install pytesseract"
            ),
        )

    command = resolve_tesseract_command(tesseract_cmd)

    if command is None:
        return OcrCapability(
            available=False,
            engine=OCR_ENGINE_NAME,
            reason=(
                "The Tesseract executable was not found. Install Tesseract and "
                "set TESSERACT_CMD, or put it on PATH."
            ),
        )

    pytesseract.pytesseract.tesseract_cmd = command

    try:
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:
        return OcrCapability(
            available=False,
            engine=OCR_ENGINE_NAME,
            command=command,
            reason=(
                "The configured Tesseract executable could not be run "
                f"({type(exc).__name__}). Check the path and permissions."
            ),
        )

    return OcrCapability(
        available=True,
        engine=OCR_ENGINE_NAME,
        version=version,
        command=command,
    )


def run_ocr(image: Any, language: str = "eng",
            tesseract_cmd: str | None = None) -> str:
    """Recognize text in a PIL image.

    Returns the recognized text and nothing else. The caller decides what to
    do with it; this function neither logs it nor inspects it.

    Raises ``RuntimeError`` on engine failure - deliberately a plain error
    caught and converted into a warning by the callers, because one page
    failing to OCR must not abort a whole document.
    """
    import pytesseract

    command = resolve_tesseract_command(tesseract_cmd)
    if command is not None:
        pytesseract.pytesseract.tesseract_cmd = command

    try:
        return pytesseract.image_to_string(image, lang=language)
    except Exception as exc:
        # The message names the engine and the exception class only - never
        # any part of the image or of whatever was partially recognized.
        raise RuntimeError(
            f"The OCR engine failed ({type(exc).__name__})."
        ) from exc


__all__ = [
    "TESSERACT_ENV_VARS",
    "OCR_ENGINE_NAME",
    "OcrCapability",
    "resolve_tesseract_command",
    "probe_ocr",
    "run_ocr",
]
