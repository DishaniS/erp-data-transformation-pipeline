"""The ERP's STRUCTURE as retrievable knowledge (Phase 7).

WHAT THIS INDEXES, AND WHAT IT REFUSES TO
-----------------------------------------
A schema representation answers *"which table stores employee birth
certificates?"* It contains column names, vendor types, normalized types, keys,
constraints and discovered relationships.

It contains NO business values. ``employees`` has a ``salary`` column, and that
fact is structure; ``250000`` is data. The distinction is not stylistic - a
schema vector that carried sampled rows would turn a metadata search into an
unaudited data-export channel, and would leak values that never passed through
the sensitivity routing the record path applies.

WHY REPRESENTATIONS ARE CHUNKED BY FIELD GROUP
----------------------------------------------
``all-MiniLM-L6-v2`` reads 256 tokens - about 1,024 characters. Measured, not
assumed. Everything past that contributes NOTHING to the embedding.

So a 200-column table written as one representation would store all 200 columns
and make roughly the first eight of them findable. The other 192 would sit in
the text, look indexed, and never match a query. That is worse than an obvious
truncation, because nothing reports it.

Fields are therefore grouped into representations that fit the window a field
at a time - never splitting a field definition - and every chunk repeats the
entity header, so a hit on the fifth chunk still says which table it is. A
small table produces exactly one representation and the chunking is invisible.

IDENTITY: STABLE ENTITY, VERSIONED CATALOG
------------------------------------------
``schema_id`` is content-addressed: it changes every time the schema changes,
which is what gives the catalog its version history. ``entity_id`` does not -
``legacy_hr.public.employees`` stays that whatever its columns become.

Representation identity is therefore derived from ``entity_id``, so
rediscovering a changed table UPDATES its searchable representation instead of
accumulating ``employees-v1``, ``employees-v2``, ``employees-v3`` as competing
search results. The PostgreSQL catalog keeps every historical snapshot; the
vector index holds the current structure. Those are different jobs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.ai.hashing import representation_content_hash
from erp_pipeline.ai.models import make_representation_id
from erp_pipeline.schemas.enums import ContentKind
from erp_pipeline.schemas.sensitivity import describe as describe_sensitivity
from erp_pipeline.sync.propagation import AIRepresentation

#: The entity type carried by a schema representation. One value, not
#: ``schema_table`` / ``schema_field`` / ``schema_view``: the ENTITY KIND is
#: metadata, and splitting it into entity types would fragment retrieval over a
#: distinction callers express as a filter.
SCHEMA_ENTITY_TYPE = "schema"

#: Measured from ``SentenceTransformer.max_seq_length`` (256 tokens) at roughly
#: four characters per token, with margin for the header repeated on every
#: chunk. A representation longer than this is not "a bit long" - its tail is
#: invisible to the model.
SCHEMA_MAX_CHARACTERS = 900

#: Never split a field definition across two representations: half a field
#: block matches nothing and reads as corruption.
MIN_FIELDS_PER_CHUNK = 1


@dataclass(frozen=True)
class SchemaRepresentationConfig:
    """How a source entity becomes retrievable schema text."""

    max_characters: int = SCHEMA_MAX_CHARACTERS
    include_relationships: bool = True
    #: Field descriptions come from the source's own comments. Included because
    #: they are structural documentation, not row data.
    include_descriptions: bool = True

    def fingerprint(self) -> str:
        return (
            f"schema@1.0/max={self.max_characters}"
            f"/rel={int(self.include_relationships)}"
            f"/desc={int(self.include_descriptions)}"
        )


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _database_of(schema: Any) -> str | None:
    """The database/catalog name, when discovery recorded one separately.

    Kept distinct from ``schema_name``: in PostgreSQL ``public`` is a schema
    inside a database, and collapsing the two would lose which database a table
    lives in. Never invented from the other.
    """
    metadata = getattr(schema, "metadata", None) or {}
    database = metadata.get("database")

    return str(database) if database else None


def entity_header_lines(schema: Any, entity: Any) -> list[str]:
    """The lines that identify this entity, repeated on every chunk."""
    lines = [
        "Content Kind: ERP Schema",
        f"Source System: {schema.source_system_id}",
    ]

    database = _database_of(schema)

    if database:
        lines.append(f"Database: {database}")

    lines.append(f"Schema: {schema.schema_name}")
    lines.append(f"Entity: {entity.source_name}")
    lines.append(f"Entity Kind: {getattr(entity.entity_kind, 'value', entity.entity_kind)}")

    namespace = getattr(entity, "namespace", None)

    if namespace and namespace != schema.schema_name:
        lines.append(f"Namespace: {namespace}")

    if getattr(entity, "description", None):
        lines.append(f"Description: {entity.description}")

    primary_key = tuple(getattr(entity, "primary_key_fields", ()) or ())

    if primary_key:
        # Stated as declared, in declared order: a composite key's order is
        # part of the key.
        lines.append("Primary Key: " + ", ".join(primary_key))

    return lines


def field_block(field: Any, config: SchemaRepresentationConfig) -> list[str]:
    """One field, as deterministic lines.

    Only facts discovery actually established. ``is_primary_key`` false means
    the source did not declare it a key - it is not an invitation to notice
    that a column is called ``employee_id`` and guess.
    """
    lines = [f"- {field.source_name}"]

    if getattr(field, "source_data_type", None):
        # The vendor's own spelling, preserved: BYTEA, LONGBLOB and
        # VARBINARY(MAX) all normalize to `binary`, and a caller converting
        # types faithfully needs the original back.
        lines.append(f"  Source Type: {field.source_data_type}")

    normalized = getattr(field, "normalized_data_type", None)
    lines.append(
        f"  Normalized Type: {getattr(normalized, 'value', normalized)}"
    )

    if getattr(field, "is_primary_key", False):
        lines.append("  Primary Key: yes")

    if getattr(field, "is_unique", False):
        lines.append("  Unique: yes")

    if getattr(field, "required", False):
        lines.append("  Required: yes")
    else:
        lines.append(f"  Nullable: {_yes_no(getattr(field, 'nullable', True))}")

    if getattr(field, "is_array", False):
        lines.append("  Array: yes")

    nested = getattr(field, "nested_path", None)

    if nested:
        lines.append("  Nested Path: " + ".".join(nested))

    semantic = getattr(field, "semantic_type", None)

    if semantic:
        # Only if a discovery step actually determined one. Phase 1 leaves this
        # None on purpose.
        lines.append(f"  Semantic Type: {semantic}")

    if config.include_descriptions and getattr(field, "description", None):
        lines.append(f"  Description: {field.description}")

    return lines


def relationship_lines(entity: Any, relationships: Sequence[Any]) -> list[str]:
    """Discovered relationships involving this entity. Never inferred ones."""
    # SourceSchema validates relationship endpoints against ENTITY
    # NORMALIZED NAMES, not entity ids - matching on entity_id here would
    # silently find nothing and quietly drop every relationship.
    name = entity.normalized_name
    relevant = [
        item
        for item in relationships or ()
        if item.from_entity == name or item.to_entity == name
    ]

    if not relevant:
        return []

    lines = ["Relationships:"]

    for item in relevant:
        from_fields = ", ".join(item.from_fields or ()) or "?"
        to_fields = ", ".join(item.to_fields or ()) or "?"
        kind = getattr(item.relationship_type, "value", item.relationship_type)
        line = (
            f"- {_short(item.from_entity)}.{from_fields}"
            f" -> {_short(item.to_entity)}.{to_fields} ({kind})"
        )

        # Confidence is reported only when discovery expressed uncertainty.
        # Printing "1.0" on every declared foreign key is noise.
        if getattr(item, "confidence", 1.0) < 1.0:
            line += f" confidence={item.confidence:.2f}"

        lines.append(line)

    return lines


def _short(entity_id: str) -> str:
    """The readable tail of a qualified entity id."""
    return entity_id.rsplit(".", 1)[-1] if entity_id else entity_id


def _ordered_fields(entity: Any) -> list[Any]:
    """Fields in the source's own order, deterministically.

    ``ordinal`` when discovery supplied it - that is the ERP's column order and
    a reader comparing this to the real table expects to see it. Declaration
    order otherwise. Never alphabetical: sorting would make the representation
    disagree with the table it describes.
    """
    fields = list(getattr(entity, "fields", ()) or ())

    if all(getattr(item, "ordinal", None) is not None for item in fields):
        return sorted(fields, key=lambda item: item.ordinal)

    return fields


def _blocks_for(entity: Any, config: SchemaRepresentationConfig) -> list[list[str]]:
    return [field_block(item, config) for item in _ordered_fields(entity)]


def _length(lines: Iterable[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def build_entity_texts(
    schema: Any, entity: Any, config: SchemaRepresentationConfig | None = None
) -> list[dict[str, Any]]:
    """One entity as one or more bounded, independently meaningful texts.

    Returns dicts carrying the text and the field range it covers, so a chunk
    can say which columns it describes rather than leaving a reader to guess.
    """
    config = config or SchemaRepresentationConfig()
    header = entity_header_lines(schema, entity)
    relationships = (
        relationship_lines(entity, getattr(schema, "relationships", ()) or ())
        if config.include_relationships
        else []
    )
    blocks = _blocks_for(entity, config)
    fields = _ordered_fields(entity)

    if not blocks:
        lines = list(header)

        if relationships:
            lines += ["", *relationships]

        return [
            {
                "text": "\n".join(lines),
                "field_start": 0,
                "field_end": 0,
                "field_names": (),
            }
        ]

    chunks: list[dict[str, Any]] = []
    current: list[list[str]] = []
    start = 0
    # Relationships ride with the FIRST chunk: they describe the entity, and
    # repeating them on every chunk would spend the window that fields need.
    extra = relationships

    def flush(end_index: int) -> None:
        nonlocal current, start, extra

        if not current:
            return

        lines = list(header) + ["", "Fields:"]

        for block in current:
            lines += block

        if extra:
            lines += ["", *extra]
            extra = []

        chunks.append(
            {
                "text": "\n".join(lines),
                "field_start": start,
                "field_end": end_index,
                "field_names": tuple(
                    item.source_name for item in fields[start:end_index]
                ),
            }
        )
        current = []
        start = end_index

    budget = config.max_characters

    for index, block in enumerate(blocks):
        overhead = _length(header) + len("\nFields:\n")
        projected = overhead + _length(
            [line for item in current + [block] for line in item]
        )

        if current and projected > budget:
            flush(index)

        current.append(block)

    flush(len(blocks))

    # Relationships on an entity whose fields exactly filled the last chunk.
    if extra:
        chunks.append(
            {
                "text": "\n".join(list(header) + ["", *extra]),
                "field_start": len(blocks),
                "field_end": len(blocks),
                "field_names": (),
            }
        )

    return chunks


def source_entity_to_representations(
    schema: Any,
    entity: Any,
    config: SchemaRepresentationConfig | None = None,
    sensitivity: Any = None,
) -> tuple[AIRepresentation, ...]:
    """One source entity as retrievable schema representations.

    Identity derives from ``entity_id``, which is stable across schema
    versions, so a rediscovered table updates its representation rather than
    competing with its own history.
    """
    config = config or SchemaRepresentationConfig()
    texts = build_entity_texts(schema, entity, config)
    total = len(texts)
    built: list[AIRepresentation] = []

    for index, chunk in enumerate(texts):
        representation_id = make_representation_id(
            SCHEMA_ENTITY_TYPE, f"{entity.entity_id}#{index}"
        )
        content = {
            "entity_id": entity.entity_id,
            "schema_chunk_index": index,
            "schema_chunk_count": total,
            "field_start": chunk["field_start"],
            "field_end": chunk["field_end"],
            "field_names": list(chunk["field_names"]),
        }
        metadata = {
            "content_kind": ContentKind.SCHEMA.value,
            "source_system_id": schema.source_system_id,
            "source_entity": entity.source_name,
            "schema_id": schema.schema_id,
            "schema_name": schema.schema_name,
            "schema_version": str(getattr(schema, "schema_version", "") or ""),
            "entity_id": entity.entity_id,
            "entity_kind": getattr(
                entity.entity_kind, "value", entity.entity_kind
            ),
            "schema_chunk_index": index,
            "schema_chunk_count": total,
            "representation_config": config.fingerprint(),
            # Phase 10. Schema STRUCTURE is not automatically public: a table
            # layout can disclose what an organisation holds. The declared
            # class applies, or the system default - never an assumption that
            # metadata is harmless.
            "sensitivity": describe_sensitivity(sensitivity),
        }

        schema_hash = getattr(schema, "schema_hash", None)

        if schema_hash:
            metadata["schema_hash"] = schema_hash

        database = _database_of(schema)

        if database:
            metadata["database_name"] = database

        built.append(
            AIRepresentation(
                representation_id=representation_id,
                entity_type=SCHEMA_ENTITY_TYPE,
                text_for_ai=chunk["text"],
                content=content,
                # A schema describes structure, not a canonical record.
                source_record_ids=(),
                metadata=metadata,
                content_hash=representation_content_hash(
                    representation_id, text_for_ai=chunk["text"], content=content
                ),
            )
        )

    return tuple(built)


def schema_to_representations(
    schema: Any,
    config: SchemaRepresentationConfig | None = None,
    sensitivity: Any = None,
) -> tuple[AIRepresentation, ...]:
    """Every entity in a schema, in declared order."""
    built: list[AIRepresentation] = []

    for entity in getattr(schema, "entities", ()) or ():
        built.extend(
            source_entity_to_representations(schema, entity, config, sensitivity)
        )

    return tuple(built)


def representation_ids_for_entity(
    entity_id: str, count: int
) -> tuple[str, ...]:
    """The ids an entity's representations occupy, for pruning a shrunk entity.

    A table that needed four chunks and now needs two leaves two behind. They
    describe columns that no longer exist, so they are removed rather than left
    to answer queries about a schema that changed.
    """
    return tuple(
        make_representation_id(SCHEMA_ENTITY_TYPE, f"{entity_id}#{index}")
        for index in range(count)
    )


__all__ = [
    "MIN_FIELDS_PER_CHUNK",
    "SCHEMA_ENTITY_TYPE",
    "SCHEMA_MAX_CHARACTERS",
    "SchemaRepresentationConfig",
    "build_entity_texts",
    "entity_header_lines",
    "field_block",
    "relationship_lines",
    "representation_ids_for_entity",
    "schema_to_representations",
    "source_entity_to_representations",
]
