"""``$ref`` resolution, bounded and offline.

Three rules govern everything here:

1. **Local only.** ``#/components/schemas/Invoice`` is resolved from the
   document already in memory. ``https://example.com/common.yaml#/Customer``
   is NOT fetched - it is recorded as unresolved. Phase 7 opens no sockets,
   and a specification must never be able to make it do so.
2. **Cycles terminate.** ``Employee.manager -> Employee`` is a legitimate,
   common model. Resolution detects the revisit, stops, and marks the point of
   recursion so the structure stays describable.
3. **Depth is bounded.** ``max_reference_depth`` caps how many hops a single
   chain may take, so a document with a thousand chained refs cannot exhaust
   the stack.

Resolution never mutates the document. It returns the target node plus a
descriptor of what happened, which is what lets the caller record honest
metadata instead of silently substituting an empty object.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence
from urllib.parse import unquote

from erp_pipeline.api_specs.errors import ReferenceResolutionError

#: Prefixes that make a reference remote. Any of these means "somewhere else",
#: and somewhere else is never fetched.
REMOTE_REF_PREFIXES: tuple[str, ...] = ("http://", "https://", "//", "ftp://")


class RefStatus(str, Enum):
    """What happened when a reference was followed."""

    RESOLVED = "resolved"
    #: The chain came back to a node already being resolved.
    CIRCULAR = "circular"
    #: ``max_reference_depth`` was reached.
    DEPTH_EXCEEDED = "depth_exceeded"
    #: A remote URL. Recorded, never fetched.
    REMOTE_NOT_FETCHED = "remote_not_fetched"
    #: A local pointer naming something the document does not contain.
    NOT_FOUND = "not_found"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class ResolvedReference:
    """The outcome of following one ``$ref``."""

    status: RefStatus
    pointer: str
    target: Any = None
    #: Last path segment of a local pointer - the schema's declared name.
    target_name: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.status is RefStatus.RESOLVED and self.target is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "pointer": self.pointer,
            "target_name": self.target_name,
        }


def is_remote_reference(pointer: str) -> bool:
    """Whether a ``$ref`` points outside this document."""
    if not isinstance(pointer, str):
        return False

    stripped = pointer.strip()

    if stripped.startswith(REMOTE_REF_PREFIXES):
        return True

    # "common.yaml#/Customer" - a relative file reference. Also not fetched:
    # reading a sibling file is still following a pointer the document chose,
    # and this phase resolves only what it was handed.
    if stripped and not stripped.startswith("#"):
        return True

    return False


def reference_target_name(pointer: str) -> str | None:
    """The declared name a local pointer refers to.

    ``#/components/schemas/Invoice`` -> ``Invoice``. This is what lets a
    ``$ref`` become a link to an already-known entity instead of a duplicated
    inline copy of it.
    """
    if not isinstance(pointer, str) or "#" not in pointer:
        return None

    fragment = pointer.split("#", 1)[1]
    segments = [segment for segment in fragment.split("/") if segment]

    if not segments:
        return None

    return _unescape_pointer_segment(segments[-1])


def _unescape_pointer_segment(segment: str) -> str:
    """Decode JSON-pointer escaping (RFC 6901) plus percent-encoding.

    ``~1`` is ``/`` and ``~0`` is ``~``; the order matters, because decoding
    ``~0`` first would turn ``~01`` into ``~1`` and then into ``/``.
    """
    return unquote(segment.replace("~1", "/").replace("~0", "~"))


class ReferenceResolver:
    """Resolves local ``$ref`` pointers within one document.

    Stateful only in its cycle-detection stack, which is scoped to a single
    resolution chain and cleared as the chain unwinds - so two independent
    fields referring to the same schema both resolve, while a self-referential
    chain terminates.
    """

    def __init__(self, document: Mapping[str, Any], max_depth: int) -> None:
        self._document = document
        self._max_depth = max_depth
        self._active: list[str] = []

    @property
    def active_chain(self) -> tuple[str, ...]:
        return tuple(self._active)

    def resolve(self, pointer: str) -> ResolvedReference:
        """Follow one pointer without entering it (no cycle bookkeeping).

        Use this for a single lookup. Use ``enter``/``leave`` around recursive
        descent so a cycle can be detected.
        """
        name = reference_target_name(pointer)

        if is_remote_reference(pointer):
            return ResolvedReference(
                status=RefStatus.REMOTE_NOT_FETCHED,
                pointer=pointer,
                target_name=name,
            )

        if pointer in self._active:
            return ResolvedReference(
                status=RefStatus.CIRCULAR, pointer=pointer, target_name=name
            )

        if len(self._active) >= self._max_depth:
            return ResolvedReference(
                status=RefStatus.DEPTH_EXCEEDED, pointer=pointer, target_name=name
            )

        target = self._lookup(pointer)

        if target is None:
            return ResolvedReference(
                status=RefStatus.NOT_FOUND, pointer=pointer, target_name=name
            )

        return ResolvedReference(
            status=RefStatus.RESOLVED,
            pointer=pointer,
            target=target,
            target_name=name,
        )

    def enter(self, pointer: str) -> None:
        """Mark a pointer as being resolved, for cycle detection."""
        self._active.append(pointer)

    def leave(self) -> None:
        if self._active:
            self._active.pop()

    def _lookup(self, pointer: str) -> Any:
        """Walk a local JSON pointer, or return ``None``."""
        fragment = pointer.split("#", 1)[1] if "#" in pointer else pointer
        segments = [
            _unescape_pointer_segment(segment)
            for segment in fragment.split("/")
            if segment
        ]

        node: Any = self._document

        for segment in segments:
            if isinstance(node, Mapping):
                if segment not in node:
                    return None
                node = node[segment]
            elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
                try:
                    node = node[int(segment)]
                except (ValueError, IndexError):
                    return None
            else:
                return None

        return node

    def require(self, pointer: str) -> Any:
        """Resolve a pointer that MUST exist, or raise.

        Used where a missing target means the document is broken rather than
        merely incomplete.
        """
        resolved = self.resolve(pointer)

        if not resolved.is_usable:
            raise ReferenceResolutionError(
                f"Reference {pointer!r} could not be resolved "
                f"({resolved.status.value}).",
                pointer=pointer,
            )

        return resolved.target


__all__ = [
    "REMOTE_REF_PREFIXES",
    "RefStatus",
    "ResolvedReference",
    "ReferenceResolver",
    "is_remote_reference",
    "reference_target_name",
]
