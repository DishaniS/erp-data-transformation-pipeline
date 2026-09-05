"""Representations for documents attached to an ERP record (Phase 3).

WHY THIS IS NOT ``chunk_to_representation``
-------------------------------------------
An uploaded document is identified by its content, and that is right: the same
PDF uploaded twice is the same document. ``make_chunk_id`` therefore derives a
chunk's identity from ``document_id`` + index + chunking config, and
``chunk_to_representation`` uses that chunk id as the representation id.

A document attached to an ERP row is a different kind of thing. Consider two
employees issued the same standard-form certificate:

    EMP002.birth_certificate = bytes X
    EMP003.birth_certificate = bytes X

Identical bytes, so identical ``document_id``, so identical ``chunk_id``, so
identical ``representation_id`` - and therefore, since ``vector_id`` is derived
from the representation id, **identical vector**. One employee's certificate
would silently overwrite the other's, and a search for EMP002 would return a
vector that now belongs to EMP003.

So content identity stays shared - the document genuinely IS the same document -
while ATTACHMENT identity is made distinct by including the parent record and
the source field. Nothing about ordinary uploaded documents changes; this is an
additional builder, not a replacement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from erp_pipeline.ai.chunking import ChunkingConfig, chunk_document
from erp_pipeline.ai.hashing import representation_content_hash
from erp_pipeline.ai.models import make_representation_id
from erp_pipeline.schemas.enums import ContentKind
from erp_pipeline.sync.propagation import AIRepresentation

#: Distinguishes a chunk of an attached document from the scalar record it
#: hangs off. Phase 3 recorded it; Phase 4 made it filterable.
#:
#: Kept as module constants pointing at the enum, so existing imports keep
#: working and the vocabulary has exactly one definition.
CONTENT_KIND_DOCUMENT_CHUNK = ContentKind.DOCUMENT_CHUNK.value
CONTENT_KIND_STRUCTURED_RECORD = ContentKind.STRUCTURED_RECORD.value

#: The entity type carried by an attached document's representation. Kept
#: separate from the parent's entity type so a search over documents is not
#: confused with a search over employees.
DOCUMENT_ENTITY_TYPE = "document"

#: Joins the parts of an attachment identity. Survives
#: ``normalize_identifier`` unambiguously.
ATTACHMENT_SEPARATOR = "|"


@dataclass(frozen=True)
class DocumentAttachment:
    """Where an attached document came from, in ERP terms.

    Every field here exists because Phase 4 will need it as a retrieval filter
    and Phase 5 will need it to resolve text. Phase 3's obligation is only to
    make sure none of it is thrown away between extraction and the vector.
    """

    #: The ERP record this document hangs off, when one is KNOWN.
    #:
    #: ``None`` for an uploaded document whose caller declared no parent. That
    #: is reported as an absence rather than derived from the business key: an
    #: ``employee_id`` is not a canonical record id, and manufacturing one here
    #: would put a fabricated reference into a field consumers resolve.
    parent_record_id: str | None = None
    #: Where this document provably came from, when that is KNOWN.
    #:
    #: ``None`` - never a stand-in like "unknown_source" or "uploaded". These
    #: three fields are two thirds of the canonical identity triple
    #: (``source_system_id`` + ``source_entity`` + ``record_key``) and are
    #: filterable Qdrant payload keys. A placeholder here is indistinguishable
    #: from a real source system of that name: a caller filtering on it would
    #: get every document whose origin was merely UNSTATED, and two unrelated
    #: uploads would share a synthetic identity. Absent is the honest answer,
    #: and ``to_metadata`` omits the key entirely - exactly as it already does
    #: for ``parent_record_id``.
    source_system_id: str | None = None
    source_entity: str | None = None
    source_field: str | None = None
    document_id: str = ""
    #: What makes this attachment distinct from another attachment of the same
    #: document, when there is no parent record to do it.
    #:
    #: Defaults to ``parent_record_id``, so a database BLOB behaves exactly as
    #: it did in Phase 3. An upload declaring only a business key supplies the
    #: key here instead - without which the same certificate uploaded for two
    #: employees would produce one vector and lose one of them.
    attachment_scope: str | None = None
    business_key_name: str | None = None
    business_key_value: str | None = None
    #: The ERP's own name for this kind of document. Taken from the column name
    #: because that IS deterministic ERP context - `birth_certificate` is what
    #: the business calls it. It is never inferred from the content, which
    #: would be a guess dressed up as a classification.
    document_type: str | None = None
    sensitivity: str | None = None
    media_type: str | None = None
    #: Dynamic scalar attributes inherited from the parent employee record.
    #: They let a document search remain inside the same department/status
    #: scope as its parent without joining on names.
    filter_attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def scope(self) -> str:
        """Whatever distinguishes this attachment from another of the same doc."""
        return self.attachment_scope or self.parent_record_id or self.document_id

    def attachment_key(self, chunk_id: str) -> str:
        """The identity that keeps two ERP attachments of one document apart.

        ``source_field`` participates because one ERP row can carry the same
        bytes in two columns. It is coerced to "" when absent: this is an
        opaque internal discriminator, not a payload field, so an empty
        segment states nothing about the document - unlike the payload, where
        a stand-in would be read as a business fact.
        """
        return ATTACHMENT_SEPARATOR.join(
            (self.scope, self.source_field or "", chunk_id)
        )

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content_kind": CONTENT_KIND_DOCUMENT_CHUNK,
            "document_id": self.document_id,
            "filter_attributes": dict(self.filter_attributes),
        }

        for key, value in (
            # Absent, not null, when the caller declared nothing - so a Phase 4
            # filter on any of these correctly EXCLUDES this document rather
            # than matching a placeholder that was never a real ERP value.
            ("source_system_id", self.source_system_id),
            ("source_entity", self.source_entity),
            ("source_field", self.source_field),
            # For a database BLOB the ERP column name IS what the business
            # calls the document, so it stands in when no explicit type was
            # given. That is real ERP context, not a guess. When neither
            # exists - an upload that declared no type - the key is omitted
            # rather than invented.
            ("document_type", self.document_type or self.source_field),
            ("parent_record_id", self.parent_record_id),
            ("record_key", self.business_key_value),
            ("business_key_name", self.business_key_name),
            ("business_key_value", self.business_key_value),
            ("sensitivity", self.sensitivity),
            ("media_type", self.media_type),
        ):
            if value is not None:
                payload[key] = value

        return payload


def attached_document_to_representations(
    document: Any,
    attachment: DocumentAttachment,
    config: ChunkingConfig | None = None,
) -> tuple[AIRepresentation, ...]:
    """Chunk an attached document and build association-safe representations.

    Reuses ``chunk_document`` unchanged - the chunking itself is identical, and
    a second chunker would drift from the first. Only the IDENTITY of the
    resulting representations differs, and only for this path.
    """
    config = config or ChunkingConfig()
    chunks = chunk_document(document, config, document_id=attachment.document_id)

    return tuple(
        _representation_for(chunk, attachment) for chunk in chunks
    )


def _representation_for(chunk: Any, attachment: DocumentAttachment) -> AIRepresentation:
    structured = {
        "document_id": attachment.document_id,
        "chunk_index": chunk.chunk_index,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "source_field": attachment.source_field,
    }

    metadata = attachment.to_metadata()
    metadata.update(
        {
            "chunk_index": chunk.chunk_index,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            # The content-addressed chunk id is kept alongside the attachment
            # id, so two ERP attachments of one document remain recognisable as
            # the same underlying content.
            "content_chunk_id": chunk.chunk_id,
        }
    )

    representation_id = make_representation_id(
        DOCUMENT_ENTITY_TYPE, attachment.attachment_key(chunk.chunk_id)
    )

    return AIRepresentation(
        representation_id=representation_id,
        entity_type=DOCUMENT_ENTITY_TYPE,
        text_for_ai=chunk.text,
        content=structured,
        # The ERP record this document hangs off, so a vector can always be
        # traced back to the row that carried it. Empty when the document
        # belongs to no record - an uploaded policy PDF, say.
        source_record_ids=(
            (attachment.parent_record_id,) if attachment.parent_record_id else ()
        ),
        metadata=metadata,
        content_hash=representation_content_hash(
            representation_id, text_for_ai=chunk.text, content=structured
        ),
    )


def representations_for_attachments(
    extracted: Iterable[tuple[Any, DocumentAttachment]],
    config: ChunkingConfig | None = None,
) -> tuple[AIRepresentation, ...]:
    """Every attachment on a batch of records, flattened."""
    built: list[AIRepresentation] = []

    for document, attachment in extracted:
        built.extend(attached_document_to_representations(document, attachment, config))

    return tuple(built)


__all__ = [
    "CONTENT_KIND_DOCUMENT_CHUNK",
    "CONTENT_KIND_STRUCTURED_RECORD",
    "DOCUMENT_ENTITY_TYPE",
    "DocumentAttachment",
    "attached_document_to_representations",
    "representations_for_attachments",
]
