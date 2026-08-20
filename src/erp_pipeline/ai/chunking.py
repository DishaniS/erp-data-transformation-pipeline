"""Deterministic document chunking with page provenance preserved.

    ExtractedDocument (PDF / image OCR)
            │
            ▼
        page spans          page numbers kept, not merged away
            │
            ▼
      DocumentChunk[]       bounded, overlapping, stable ids
            │
            ▼
     AIRepresentation[]     one per chunk, ready for the same engine

WHY CHUNK AT ALL
----------------
A six-page policy document embedded as one vector answers no question well: the
vector is an average of everything it says. Chunking gives retrieval something
specific to match, and page provenance gives an answer somewhere to point.

DUCK-TYPED INPUT
----------------
This module does not import ``erp_pipeline.ingestion``. It accepts anything
exposing ``pages`` (each with ``page_number`` and ``text``) and a ``file`` with
``file_id`` / ``content_hash``. Phase 6's ``ExtractedDocument`` satisfies that,
and so would any other extractor - which is what keeps the AI layer independent
of how a document was obtained.

DETERMINISM
-----------
Same text plus same configuration gives the same chunks, the same order, the
same ids and the same hashes (Step 14). Boundary selection is a search over a
fixed window with a fixed preference order - never a heuristic that could tie
differently on two runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.ai.errors import ChunkingError
from erp_pipeline.ai.hashing import (
    chunk_content_hash,
    make_chunk_id,
    representation_content_hash,
)
from erp_pipeline.ai.models import ChunkingConfig, DocumentChunk
from erp_pipeline.sync.propagation import AIRepresentation

#: Separator placed between pages when they are concatenated. A form feed
#: matches what Phase 6's ``document_text`` already uses, so page boundaries
#: stay recoverable from the joined string.
PAGE_SEPARATOR = "\f"

#: Boundary preferences, strongest first. A paragraph break is a better place
#: to end a chunk than a sentence break, which is better than a line break.
_BOUNDARIES = ("\n\n", ". ", ".\n", "\n", " ")


@dataclass(frozen=True)
class _PageSpan:
    """Where one page's text sits inside the concatenated document string."""

    page_number: int
    start: int
    end: int


def _build_document_text(pages: Sequence[Any]) -> tuple[str, list[_PageSpan]]:
    """Concatenate page text while remembering where each page lives."""
    parts: list[str] = []
    spans: list[_PageSpan] = []
    cursor = 0

    for page in pages:
        text = getattr(page, "text", "") or ""
        number = int(getattr(page, "page_number", len(spans) + 1))

        if parts:
            parts.append(PAGE_SEPARATOR)
            cursor += len(PAGE_SEPARATOR)

        start = cursor
        parts.append(text)
        cursor += len(text)

        spans.append(_PageSpan(page_number=number, start=start, end=cursor))

    return "".join(parts), spans


def _pages_for(spans: Sequence[_PageSpan], start: int, end: int) -> tuple[int, int]:
    """Which page numbers a character range touches (Step 13)."""
    touched = [
        span.page_number
        for span in spans
        if span.start < end and span.end > start
    ]

    if not touched:
        # A chunk lying entirely inside a separator; attribute it to the
        # nearest preceding page rather than losing provenance entirely.
        preceding = [span.page_number for span in spans if span.start <= start]
        fallback = preceding[-1] if preceding else (
            spans[0].page_number if spans else 1
        )
        return fallback, fallback

    return min(touched), max(touched)


def _find_break(text: str, window_start: int, hard_end: int) -> int:
    """Pick a readable end position, deterministically.

    Searches backwards from the hard limit for the strongest boundary within
    the window. Preference order is fixed, and ``rfind`` is exact - two runs
    over the same text cannot choose differently.
    """
    for boundary in _BOUNDARIES:
        found = text.rfind(boundary, window_start, hard_end)
        if found > window_start:
            return found + len(boundary)

    return hard_end


def chunk_text(
    text: str,
    document_id: str,
    config: ChunkingConfig | None = None,
    page_spans: Sequence[_PageSpan] | None = None,
) -> tuple[DocumentChunk, ...]:
    """Split text into bounded, overlapping, page-traceable chunks."""
    config = config or ChunkingConfig()
    spans = list(page_spans or [])

    if not text.strip():
        return ()

    chunks: list[DocumentChunk] = []
    position = 0
    index = 0
    length = len(text)
    fingerprint = config.fingerprint()

    while position < length:
        hard_end = min(position + config.max_characters, length)

        if hard_end < length:
            window_start = max(
                position + 1, hard_end - config.boundary_search_window
            )
            end = _find_break(text, window_start, hard_end)
        else:
            end = hard_end

        piece = text[position:end]
        stripped = piece.strip()

        if stripped and len(stripped) >= config.min_characters:
            page_start, page_end = (
                _pages_for(spans, position, end) if spans else (1, 1)
            )
            chunk_id = make_chunk_id(document_id, index, fingerprint)

            chunks.append(
                DocumentChunk(
                    document_id=document_id,
                    chunk_id=chunk_id,
                    chunk_index=index,
                    text=stripped,
                    page_start=page_start,
                    page_end=page_end,
                    char_count=len(stripped),
                    content_hash=chunk_content_hash(chunk_id, stripped),
                    metadata={"chunking_config": fingerprint},
                )
            )
            index += 1

        if end >= length:
            break

        advance = end - config.overlap_characters

        if advance <= position:
            # Guarantees forward progress even for pathological text with no
            # usable boundary; without it a document could loop for ever.
            advance = position + max(1, config.max_characters // 2)

        position = advance

    if not chunks and text.strip():
        # The whole document was shorter than min_characters. Emitting nothing
        # would silently drop it, so it is kept as one chunk (Step 12: do not
        # silently truncate the complete document).
        stripped = text.strip()
        chunk_id = make_chunk_id(document_id, 0, fingerprint)
        page_start, page_end = (
            _pages_for(spans, 0, len(text)) if spans else (1, 1)
        )
        chunks.append(
            DocumentChunk(
                document_id=document_id,
                chunk_id=chunk_id,
                chunk_index=0,
                text=stripped,
                page_start=page_start,
                page_end=page_end,
                char_count=len(stripped),
                content_hash=chunk_content_hash(chunk_id, stripped),
                metadata={"chunking_config": fingerprint},
            )
        )

    return tuple(chunks)


def chunk_document(
    document: Any,
    config: ChunkingConfig | None = None,
    document_id: str | None = None,
) -> tuple[DocumentChunk, ...]:
    """Chunk an extracted document, preserving its page provenance.

    ``document_id`` defaults to the file's CONTENT hash rather than its name:
    the same bytes uploaded twice under different filenames are the same
    document, and re-chunking them under one identity is correct.
    """
    config = config or ChunkingConfig()

    # Phase 6 returns a DocumentFileResult that WRAPS the ExtractedDocument.
    # Accepting either saves every caller an unwrap and keeps this module
    # duck-typed rather than importing the ingestion package to name the type.
    if not getattr(document, "pages", None) and getattr(document, "document", None):
        document = document.document

    pages = list(getattr(document, "pages", ()) or ())

    if not pages:
        raise ChunkingError(
            "the document exposes no pages, so it cannot be chunked"
        )

    file_source = getattr(document, "file", None)
    resolved_id = (
        document_id
        or getattr(file_source, "content_hash", None)
        or getattr(file_source, "file_id", None)
    )

    if not resolved_id:
        raise ChunkingError(
            "the document carries no content hash or file id to derive a "
            "stable document identity from"
        )

    text, spans = _build_document_text(pages)

    return chunk_text(text, str(resolved_id), config, spans)


def chunk_to_representation(
    chunk: DocumentChunk,
    entity_type: str = "document",
    metadata: Mapping[str, Any] | None = None,
) -> AIRepresentation:
    """One chunk as an AI-ready representation, ready for the same engine.

    Identity is the chunk id, which already encodes the document, the ordinal
    and the chunking configuration - so a chunk keeps its vector across runs and
    loses it only when the configuration genuinely changes.
    """
    structured = {
        "document_id": chunk.document_id,
        "chunk_index": chunk.chunk_index,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
    }

    return AIRepresentation(
        representation_id=chunk.chunk_id,
        entity_type=entity_type,
        text_for_ai=chunk.text,
        content=structured,
        source_record_ids=(chunk.document_id,),
        metadata={
            "document_id": chunk.document_id,
            "chunk_index": chunk.chunk_index,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            **dict(metadata or {}),
        },
        content_hash=representation_content_hash(
            chunk.chunk_id, text_for_ai=chunk.text, content=structured
        ),
    )


def document_to_representations(
    document: Any,
    config: ChunkingConfig | None = None,
    entity_type: str = "document",
    metadata: Mapping[str, Any] | None = None,
) -> tuple[AIRepresentation, ...]:
    """Extracted document -> chunks -> representations, in one call."""
    if not getattr(document, "pages", None) and getattr(document, "document", None):
        document = document.document

    file_source = getattr(document, "file", None)

    enriched = {
        "source_type": getattr(
            getattr(file_source, "file_type", None), "value", None
        ),
        "original_filename": getattr(file_source, "original_filename", None),
        **dict(metadata or {}),
    }

    return tuple(
        chunk_to_representation(chunk, entity_type, enriched)
        for chunk in chunk_document(document, config)
    )


__all__ = [
    "PAGE_SEPARATOR",
    "chunk_text",
    "chunk_document",
    "chunk_to_representation",
    "document_to_representations",
]
