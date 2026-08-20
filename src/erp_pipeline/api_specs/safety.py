"""Safe document loading and resource guards.

Two responsibilities: get a specification off disk without executing anything,
and keep a hostile document from exhausting the process.

YAML SAFETY (Step 45)
---------------------
``yaml.safe_load`` is the only loader used, and it is the only one that may
ever be used here. ``yaml.load`` with the default or ``UnsafeLoader`` will
instantiate arbitrary Python objects from tags such as
``!!python/object/apply:os.system``, which turns "parse this API spec" into
"run this code". A static test asserts no unsafe loader appears anywhere in
this package.

NO NETWORK (Step 42)
--------------------
This module reads local files only. Nothing in this package opens a socket -
not to a documented endpoint, and not to fetch a remote ``$ref``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from erp_pipeline.api_specs.errors import (
    MalformedSpecError,
    SpecFileError,
    UnsafeSpecContentError,
)

#: Markers of a YAML tag that would construct a Python object. Detected before
#: parsing so the refusal is explicit and attributable, rather than surfacing
#: as a generic "could not construct object" further down.
_UNSAFE_YAML_MARKERS: tuple[str, ...] = (
    "!!python/",
    "!!java/",
    "!!ruby/",
    "tag:yaml.org,2002:python/",
)


def validate_spec_path(path: str | os.PathLike[str]) -> Path:
    """Resolve and validate a local specification path.

    Rejects directories, missing paths and anything that is not a regular
    file. Mirrors ``ingestion.safety.validate_source_path``; the two are kept
    separate only because their error types differ, and a caller should get an
    ``ApiSpecError`` from this package.
    """
    if path is None:
        raise SpecFileError("A specification path is required.")

    try:
        candidate = Path(path).expanduser()
    except (TypeError, ValueError) as exc:
        raise SpecFileError(
            f"Not a usable path: {type(path).__name__}."
        ) from exc

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SpecFileError(f"No such file: {candidate.name!r}.") from exc
    except OSError as exc:
        raise SpecFileError(f"Could not resolve path {candidate.name!r}.") from exc

    if resolved.is_dir():
        raise SpecFileError(
            f"{resolved.name!r} is a directory. Parse one specification at a "
            "time."
        )

    if not resolved.is_file():
        raise SpecFileError(
            f"{resolved.name!r} is not a regular file."
        )

    if not os.access(resolved, os.R_OK):
        raise SpecFileError(f"{resolved.name!r} is not readable.")

    return resolved


def validate_spec_size(path: Path, max_bytes: int) -> int:
    """Enforce the size limit from filesystem metadata, BEFORE reading."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SpecFileError(f"Could not stat {path.name!r}.") from exc

    if size > max_bytes:
        raise SpecFileError(
            f"{path.name!r} is {size} bytes, which exceeds the configured "
            f"limit of {max_bytes} bytes. Raise "
            "ApiSpecOptions.max_spec_size_bytes to accept it."
        )

    return size


def read_spec_text(path: Path) -> str:
    """Read a specification as UTF-8 text.

    A UTF-8 BOM is stripped (``utf-8-sig``): exported Swagger files from
    Windows tooling routinely carry one, and it would otherwise break the
    leading ``{`` of a JSON document.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MalformedSpecError(
            f"{path.name!r} is not valid UTF-8: an undecodable byte sequence "
            f"begins at byte offset {exc.start}."
        ) from exc
    except OSError as exc:
        raise SpecFileError(f"Could not read {path.name!r}.") from exc


def load_document(text: str, filename: str = "<spec>") -> Any:
    """Parse a specification document from text.

    JSON first, then YAML. That order is deliberate and not merely a
    preference: every JSON document is also valid YAML 1.2, but the JSON
    parser is stricter, faster, and gives better error positions - so a
    malformed JSON file reports a JSON error rather than a confusing YAML one.
    """
    stripped = text.lstrip()

    if stripped.startswith(("{", "[")):
        return _load_json(text, filename)

    return _load_yaml(text, filename)


def _load_json(text: str, filename: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedSpecError(
            f"{filename!r} is not valid JSON: {exc.msg} at line {exc.lineno} "
            f"column {exc.colno}.",
            line=exc.lineno,
            column=exc.colno,
        ) from exc


def _load_yaml(text: str, filename: str) -> Any:
    guard_against_unsafe_yaml(text, filename)

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a dependency
        raise MalformedSpecError(
            "The 'PyYAML' package is required to read YAML specifications but "
            "is not installed. Install it with: pip install PyYAML"
        ) from exc

    try:
        # safe_load ONLY. See the module docstring.
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise MalformedSpecError(
            f"{filename!r} is not valid YAML"
            + (f" at line {mark.line + 1} column {mark.column + 1}." if mark
               else "."),
            line=(mark.line + 1) if mark else None,
            column=(mark.column + 1) if mark else None,
        ) from exc


def guard_against_unsafe_yaml(text: str, filename: str = "<spec>") -> None:
    """Refuse a YAML document that asks for arbitrary object construction.

    ``safe_load`` would refuse these anyway, but it reports them as an ordinary
    parse error. A specification containing ``!!python/object/apply`` is a
    security event and deserves to be named as one - and the caller gets a
    distinct error type they can alert on.
    """
    lowered = text.lower()

    for marker in _UNSAFE_YAML_MARKERS:
        if marker in lowered:
            raise UnsafeSpecContentError(
                f"{filename!r} contains a {marker!r} YAML tag, which would "
                "construct arbitrary objects while parsing. Refusing to load "
                "it. Only plain YAML data is accepted."
            )


def truncate_description(value: Any, max_length: int) -> str | None:
    """Bound an authored description, or return ``None``.

    A description is documentation written for consumers, so it is retained -
    it is also the single most useful signal a later semantic-mapping phase
    has. It is length-bounded so a specification cannot smuggle a megabyte of
    prose (or a pasted payload) into catalog metadata.
    """
    if value is None:
        return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if len(text) <= max_length:
        return text

    marker = "...[truncated]"
    return text[: max_length - len(marker)] + marker


class WarningBudget:
    """Bounded warning collection.

    A pathological document can produce a warning per field. Collecting them
    unboundedly would turn a safety feature into a memory leak, so the budget
    caps them and records that it did - the count is never silently wrong.
    """

    def __init__(self, max_warnings: int) -> None:
        self._max = max_warnings
        self._items: list[Any] = []
        self._suppressed = 0

    def add(self, warning: Any) -> None:
        if len(self._items) >= self._max:
            self._suppressed += 1
            return
        self._items.append(warning)

    @property
    def suppressed_count(self) -> int:
        return self._suppressed

    def items(self) -> tuple[Any, ...]:
        return tuple(self._items)


__all__ = [
    "validate_spec_path",
    "validate_spec_size",
    "read_spec_text",
    "load_document",
    "guard_against_unsafe_yaml",
    "truncate_description",
    "WarningBudget",
]
