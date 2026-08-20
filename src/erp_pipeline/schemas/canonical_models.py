"""The canonical ERP representation every source converges on.

"Canonical" here is a *logical* model, not a physical database schema. It says
nothing about how records are stored. PostgreSQL may later be the physical
engine, but a canonical record is equally valid held in memory, written to
JSONL, posted to an API or indexed in a vector store.

Two record shapes share one envelope:

    CanonicalRecord     normalized structured data from a table, collection,
                        CSV row or API payload
    CanonicalDocument   unstructured knowledge from a PDF or a scanned image

They share identity, provenance, versioning, sensitivity and hashing through
``CanonicalEnvelope``, and diverge where the semantics genuinely differ: a
record carries ``normalized_data``, a document carries ``text`` plus document
properties. Forcing extracted document text into a ``normalized_data``
dictionary would make both models poorer, so the base class stops exactly
where the shared meaning stops.

Nothing in this module performs I/O, and no field describes storage, indexing
or embeddings. A vector id can be derived on demand from ``record_id``
(``make_deterministic_uuid``), which keeps the canonical model independent of
whichever vector store a later phase chooses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from erp_pipeline.schemas.enums import RecordType, SensitivityLevel, SourceType
from erp_pipeline.schemas.identity import (
    compute_content_hash,
    make_canonical_document_id,
    make_canonical_record_id,
    make_deterministic_uuid,
)
from erp_pipeline.schemas.serialization import JsonModel, utc_now
from erp_pipeline.schemas.validation import (
    ValidationError,
    optional_identifier,
    optional_text,
    require_aware_datetime,
    require_identifier,
    require_json_object,
    require_metadata,
    require_non_negative_int,
    require_text,
)
from erp_pipeline.version import CANONICAL_MODEL_VERSION


@dataclass(kw_only=True)
class SourceReference(JsonModel):
    """Where a canonical record came from - the identity-bearing part.

    Small and required. These are exactly the facts needed to say which system
    and which source object a record represents, and they are the inputs to the
    canonical id, which is why they are separated from the richer, optional
    ``RecordProvenance``.

    Carries no credentials and no connection details.
    """

    source_system_id: str
    source_type: SourceType
    source_entity: str | None = None
    source_record_key: str | None = None

    def __post_init__(self) -> None:
        self.source_system_id = require_identifier(
            self.source_system_id, "SourceReference.source_system_id"
        )
        self.source_type = SourceType.from_value(self.source_type)
        self.source_entity = optional_text(
            self.source_entity, "SourceReference.source_entity"
        )
        self.source_record_key = optional_text(
            self.source_record_key, "SourceReference.source_record_key"
        )


@dataclass(kw_only=True)
class RecordProvenance(JsonModel):
    """How and when a canonical record was obtained - the optional detail.

    Together with ``SourceReference`` this answers "where did this record come
    from?" precisely enough to go back to the origin: which schema snapshot
    described it, how it was ingested, and - for documents and APIs - which
    file page or which API operation produced it.

    Deliberately excluded:

    * the original record's contents. Provenance is a pointer, not a second
      copy of the data, and duplicating payloads here would multiply both
      storage and the blast radius of a leak.
    * credentials of any kind. ``metadata`` is validated against a
      credential-key denylist.
    """

    schema_id: str | None = None
    schema_version: str | None = None
    ingestion_method: str | None = None
    original_record_id: str | None = None
    source_file_path: str | None = None
    page_number: int | None = None
    api_operation: str | None = None
    extracted_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.schema_id = optional_identifier(
            self.schema_id, "RecordProvenance.schema_id"
        )
        self.schema_version = optional_text(
            self.schema_version, "RecordProvenance.schema_version"
        )
        self.ingestion_method = optional_identifier(
            self.ingestion_method, "RecordProvenance.ingestion_method"
        )
        self.original_record_id = optional_text(
            self.original_record_id, "RecordProvenance.original_record_id"
        )
        self.source_file_path = optional_text(
            self.source_file_path, "RecordProvenance.source_file_path"
        )
        self.api_operation = optional_text(
            self.api_operation, "RecordProvenance.api_operation"
        )
        self.metadata = require_metadata(
            self.metadata, "RecordProvenance.metadata"
        )

        if self.extracted_at is not None:
            self.extracted_at = require_aware_datetime(
                self.extracted_at, "RecordProvenance.extracted_at"
            )

        if self.page_number is not None:
            self.page_number = require_non_negative_int(
                self.page_number, "RecordProvenance.page_number"
            )


@dataclass(kw_only=True)
class CanonicalEnvelope(JsonModel):
    """Fields every canonical artifact carries, whatever its shape.

    Keyword-only throughout, which also sidesteps the dataclass rule that a
    field with a default cannot precede one without: subclasses may declare
    required fields freely.
    """

    record_id: str
    record_type: RecordType
    source: SourceReference
    schema_version: str = CANONICAL_MODEL_VERSION
    content_hash: str | None = None
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    provenance: RecordProvenance | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        model = type(self).__name__

        self.record_id = require_text(self.record_id, f"{model}.record_id")
        self.record_type = RecordType.from_value(self.record_type)
        self.sensitivity = SensitivityLevel.from_value(self.sensitivity)
        self.schema_version = require_text(
            self.schema_version, f"{model}.schema_version"
        )
        self.content_hash = optional_text(self.content_hash, f"{model}.content_hash")
        self.metadata = require_metadata(self.metadata, f"{model}.metadata")
        self.created_at = require_aware_datetime(self.created_at, f"{model}.created_at")
        self.updated_at = require_aware_datetime(self.updated_at, f"{model}.updated_at")

        if not isinstance(self.source, SourceReference):
            raise ValidationError(
                f"{model}.source must be a SourceReference, got "
                f"{type(self.source).__name__}. Source information is a typed "
                "contract, not a free-form dictionary."
            )

        if self.provenance is not None and not isinstance(
            self.provenance, RecordProvenance
        ):
            raise ValidationError(
                f"{model}.provenance must be a RecordProvenance, got "
                f"{type(self.provenance).__name__}."
            )

    @property
    def source_system_id(self) -> str:
        """Convenience accessor for the owning source system."""
        return self.source.source_system_id

    def deterministic_uuid(self) -> str:
        """UUIDv5 derived from ``record_id``, for stores that require a UUID.

        ``record_id`` remains the authoritative identity; this is a projection
        for a specific external consumer.
        """
        return make_deterministic_uuid(self.record_id)


@dataclass(kw_only=True)
class CanonicalRecord(CanonicalEnvelope):
    """One normalized ERP record, independent of the technology it came from.

    ``entity_type`` is an open normalized string - ``invoice``, ``customer``,
    ``purchase_order``, ``goods_receipt``, whatever the domain needs. It is
    deliberately not an enum: the framework's purpose is that a new ERP domain
    object requires no change to this file.

    ``normalized_data`` is an open JSON object for the same reason. Which keys
    belong in it for a given ``entity_type`` is decided by a mapping profile,
    not by this contract. The contract guarantees only that it is an object and
    that everything inside it is JSON-representable.
    """

    entity_type: str
    normalized_data: Mapping[str, Any] = field(default_factory=dict)
    text_for_ai: str | None = None
    record_type: RecordType = RecordType.STRUCTURED_RECORD

    def __post_init__(self) -> None:
        super().__post_init__()

        self.entity_type = require_identifier(
            self.entity_type, "CanonicalRecord.entity_type"
        )
        self.normalized_data = require_json_object(
            self.normalized_data, "CanonicalRecord.normalized_data"
        )
        self.text_for_ai = optional_text(
            self.text_for_ai, "CanonicalRecord.text_for_ai"
        )

    @classmethod
    def from_source(
        cls,
        *,
        source: SourceReference,
        entity_type: str,
        stable_source_key: Any,
        normalized_data: Mapping[str, Any] | None = None,
        text_for_ai: str | None = None,
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
        provenance: RecordProvenance | None = None,
        record_type: RecordType = RecordType.STRUCTURED_RECORD,
        metadata: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        compute_hash: bool = True,
    ) -> "CanonicalRecord":
        """Build a record with its canonical id derived, not hand-written.

        This is the intended construction path. Deriving the id here means a
        caller cannot accidentally invent an id that disagrees with the record's
        own provenance, and it guarantees the ``source_system_id`` component is
        always present - which is what stops two ERP systems colliding on the
        same business key.
        """
        record_id = make_canonical_record_id(
            source_system_id=source.source_system_id,
            entity_type=entity_type,
            stable_source_key=stable_source_key,
        )

        record = cls(
            record_id=record_id,
            record_type=record_type,
            source=source,
            entity_type=entity_type,
            normalized_data=normalized_data or {},
            text_for_ai=text_for_ai,
            sensitivity=sensitivity,
            provenance=provenance,
            metadata=metadata or {},
            created_at=created_at or utc_now(),
            updated_at=updated_at or utc_now(),
        )

        if compute_hash:
            record.content_hash = record.compute_content_hash()

        return record

    def compute_content_hash(self) -> str:
        """Deterministic hash of this record's AI-relevant content.

        Covers identity, the normalized business data and the AI text. Excludes
        timestamps and pipeline bookkeeping, so re-ingesting an unchanged
        source record yields an unchanged hash.
        """
        return compute_content_hash(
            record_id=self.record_id,
            content=dict(self.normalized_data),
            text_for_ai=self.text_for_ai,
        )


@dataclass(kw_only=True)
class CanonicalDocument(CanonicalEnvelope):
    """One unstructured ERP document - a PDF, a scanned image, and later more.

    Kept distinct from ``CanonicalRecord`` because the meaning is genuinely
    different: a document's payload is text plus rendering facts (page count,
    mime type, language), not a set of normalized business fields. Both still
    share the envelope, so identity, provenance, sensitivity and versioning
    behave identically for both.

    Chunking is explicitly out of scope for Phase 1. The model is compatible
    with it - a future chunk can reference this ``record_id`` as its parent and
    carry its own ordinal - but no chunk model, splitter or overlap policy is
    defined here.
    """

    document_id: str
    title: str | None = None
    document_type: str | None = None
    mime_type: str | None = None
    text: str | None = None
    page_count: int | None = None
    language: str | None = None
    record_type: RecordType = RecordType.DOCUMENT

    def __post_init__(self) -> None:
        super().__post_init__()

        self.document_id = require_text(
            self.document_id, "CanonicalDocument.document_id"
        )
        self.title = optional_text(self.title, "CanonicalDocument.title")
        self.document_type = optional_identifier(
            self.document_type, "CanonicalDocument.document_type"
        )
        self.mime_type = optional_text(self.mime_type, "CanonicalDocument.mime_type")
        self.language = optional_text(self.language, "CanonicalDocument.language")

        if self.text is not None and not isinstance(self.text, str):
            raise ValidationError(
                "CanonicalDocument.text must be a string when provided, got "
                f"{type(self.text).__name__}."
            )

        if self.page_count is not None:
            self.page_count = require_non_negative_int(
                self.page_count, "CanonicalDocument.page_count"
            )

    @classmethod
    def from_source(
        cls,
        *,
        source: SourceReference,
        document_id: str,
        title: str | None = None,
        document_type: str | None = None,
        mime_type: str | None = None,
        text: str | None = None,
        page_count: int | None = None,
        language: str | None = None,
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
        provenance: RecordProvenance | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        compute_hash: bool = True,
    ) -> "CanonicalDocument":
        """Build a document with its canonical id derived from its identity.

        ``document_id`` should be content-derived - a hash over the file bytes
        is ideal - so the same file always produces the same canonical id.
        """
        record_id = make_canonical_document_id(
            source_system_id=source.source_system_id,
            document_id=document_id,
        )

        document = cls(
            record_id=record_id,
            source=source,
            document_id=document_id,
            title=title,
            document_type=document_type,
            mime_type=mime_type,
            text=text,
            page_count=page_count,
            language=language,
            sensitivity=sensitivity,
            provenance=provenance,
            metadata=metadata or {},
            created_at=created_at or utc_now(),
            updated_at=updated_at or utc_now(),
        )

        if compute_hash:
            document.content_hash = document.compute_content_hash()

        return document

    def compute_content_hash(self) -> str:
        """Deterministic hash over the document's identity and extracted text."""
        return compute_content_hash(
            record_id=self.record_id,
            content={
                "document_id": self.document_id,
                "document_type": self.document_type,
                "mime_type": self.mime_type,
                "page_count": self.page_count,
                "language": self.language,
                "title": self.title,
            },
            text_for_ai=self.text,
        )


__all__ = [
    "SourceReference",
    "RecordProvenance",
    "CanonicalEnvelope",
    "CanonicalRecord",
    "CanonicalDocument",
]
