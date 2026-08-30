"""Source-native transformation for ERP entities the canonical model lacks.

WHY THIS EXISTS
---------------
The curated canonical model covers three entities - invoice, customer,
purchase_order. A real ERP holds dozens more: employees, assets, warehouses,
vendor ledgers, machine maintenance logs, and whatever a given business
invented in 2011. None of those can produce a ``MappingProfile``, so before
this module none of them could become an ``AIRepresentation`` at all.

This path makes an uncovered entity AI-ready **on its own terms**: its fields
keep the names the source gave them, and nothing is renamed to a canonical
concept, because there is no canonical concept to rename it to.

WHAT IT IS NOT
--------------
It is NOT a fallback for a mapping that went wrong. An entity the canonical
model DOES cover, whose fields came out ambiguous, must go to a human - that
refusal is one of the component's strongest properties, and routing around it
would quietly destroy it. Admission to this path is decided by
``mapping.MappingResult.unmatched_entities`` ("source entities no canonical
entity could be matched to"), never by whether mapping happened to fail.

WHY THE RESULT IS STILL A CanonicalRecord
-----------------------------------------
Because the contract says it can be. ``CanonicalRecord`` documents
``entity_type`` as *"an open normalized string - invoice, customer,
purchase_order, goods_receipt, whatever the domain needs… a new ERP domain
object requires no change to this file"*, and ``normalized_data`` as an open
JSON object whose keys "are decided by a mapping profile, not by this
contract". "Canonical" in that contract means NORMALIZED AND
TECHNOLOGY-INDEPENDENT, not "drawn from the curated vocabulary".

Reusing it is therefore honest rather than convenient, and it means the entire
downstream chain - representation, embedding, tier routing, the record store,
search, ``GET /v1/records/{id}`` - works unchanged, with no parallel model and
no second storage path.

BINARY IS DESCRIBED, NEVER RENDERED
-----------------------------------
A BLOB column base64-encoded into the AI text would be embedded as thousands of
meaningless characters, displacing the fields that carry actual meaning. Binary
fields are therefore recorded structurally and excluded from the text. Reading
their CONTENT - OCR, PDF extraction - is Phase 3 and is deliberately not done
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from erp_pipeline.schemas.canonical_models import (
    CanonicalRecord,
    RecordProvenance,
    SourceReference,
)
from erp_pipeline.schemas.enums import FieldDataType, SensitivityLevel, SourceType
from erp_pipeline.schemas.identity import looks_like_surrogate_key
from erp_pipeline.schemas.search_fields import normalize_filter_attributes
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.transformation.errors import TransformationError
from erp_pipeline.transformation.models import SourceRecord, TransformationOptions
from erp_pipeline.transformation.type_converter import convert

#: Metadata key under which a record lists the binary fields it carries but does
#: not render. Phase 3 reads this to decide what to extract; until then it is
#: the honest record that something exists and was deliberately not opened.
BINARY_FIELDS_KEY = "binary_fields"

#: Marks a record as having come through this path rather than through a
#: mapping profile, so nothing downstream mistakes it for curated-vocabulary
#: data.
SOURCE_NATIVE_KEY = "source_native"

#: Where the business key is preserved. Phase 4 will surface this as a
#: retrieval filter; Phase 2 only has to avoid throwing it away.
BUSINESS_KEY_NAME = "business_key_name"
BUSINESS_KEY_VALUE = "business_key_value"

#: Joins the parts of a composite key. Chosen because ``normalize_identifier``
#: leaves it intact, so a two-column key survives into the canonical id
#: unambiguously.
COMPOSITE_KEY_SEPARATOR = "|"


def qualified_source_entity(entity: SourceEntity) -> str:
    """Return the source object's stable address, including its namespace."""
    return (
        f"{entity.namespace}.{entity.source_name}"
        if entity.namespace
        else entity.source_name
    )


class SourceIdentityUnavailableError(TransformationError):
    """The source entity declares no key, so no stable identity exists.

    Refused rather than invented. A generated id would differ on every
    ingestion, so the same ERP row would accumulate a new vector each run and
    the store would silently fill with duplicates that nothing could reconcile.
    Following the same rule the canonical path already applies to surrogate
    keys: an identity that is not stable is not an identity.
    """

    def __init__(self, entity: str, detail: str) -> None:
        super().__init__(
            f"{entity!r} has no usable record identity: {detail}. Declare a "
            "primary key on the source entity, or supply one explicitly."
        )
        self.entity = entity
        self.detail = detail


@dataclass(frozen=True)
class SourceNativeResult:
    """One source-native transformation run."""

    records: tuple[CanonicalRecord, ...] = ()
    rejected: tuple[str, ...] = ()
    binary_fields_omitted: tuple[str, ...] = ()

    @property
    def record_count(self) -> int:
        return len(self.records)


def binary_field_names(entity: SourceEntity) -> tuple[str, ...]:
    """Fields the discovered schema says hold bytes.

    Read from the schema rather than sniffed from values: discovery already
    normalized every dialect's binary spelling (BYTEA, LONGBLOB, VARBINARY,
    IMAGE, binData) onto ``FieldDataType.BINARY``, and re-deciding it here from
    a value would be a second, weaker answer to a question already answered.
    """
    return tuple(
        field.source_name
        for field in entity.fields
        if field.normalized_data_type is FieldDataType.BINARY
    )


def resolve_business_key(
    entity: SourceEntity,
    record: SourceRecord,
    key_fields: Sequence[str] | None = None,
) -> tuple[str, str]:
    """The record's stable key, as ``(key_name, key_value)``.

    Preference order, and the reason for it:

    1. ``key_fields`` supplied by the caller - an explicit human decision
       outranks anything inferred.
    2. ``SourceEntity.primary_key_fields`` - the source system's own
       declaration, which is a fact rather than a guess. Composite keys are
       joined in declared order so the value is order-stable.
    3. ``SourceRecord.record_key`` - the extractor's own key for the row, when
       it has one.

    There is deliberately no fourth option. The first field of a table is not a
    primary key just because it is first.
    """
    declared = tuple(key_fields or entity.primary_key_fields or ())

    if declared:
        missing = [name for name in declared if name not in record.values]

        if missing:
            raise SourceIdentityUnavailableError(
                entity.source_name,
                f"key field(s) {missing} are absent from the record",
            )

        parts = [_key_part(record.values[name]) for name in declared]

        if any(part == "" for part in parts):
            raise SourceIdentityUnavailableError(
                entity.source_name,
                f"key field(s) {list(declared)} are empty in this record",
            )

        return COMPOSITE_KEY_SEPARATOR.join(declared), COMPOSITE_KEY_SEPARATOR.join(parts)

    if record.record_key is not None:
        candidate = str(record.record_key).strip()

        # The SAME refusal the canonical path applies. An extractor's record key
        # is often a row offset - a CSV row number, a cursor position - and a
        # digits-only key changes the moment the source is reordered or
        # reloaded, silently re-identifying every derived record and orphaning
        # every vector already stored against the old id.
        #
        # This was not hypothetical: before this guard, an employees CSV with no
        # declared primary key produced `erp:file_source:employees:1`, keyed on
        # the row number.
        if candidate and not looks_like_surrogate_key(candidate):
            return "record_key", candidate

        raise SourceIdentityUnavailableError(
            entity.source_name,
            "the entity declares no primary key and the extractor's record key "
            f"({candidate!r}) is a bare number, which identifies a POSITION "
            "rather than a record",
        )

    raise SourceIdentityUnavailableError(
        entity.source_name,
        "the entity declares no primary key and the extractor supplied no "
        "record key",
    )


def _key_part(value: Any) -> str:
    """One component of a key, rendered stably.

    ``str`` on the already-normalized value: a key must render identically on
    every ingestion, so no locale-sensitive or type-dependent formatting is
    involved.
    """
    if value is None:
        return ""

    return str(value).strip()


def normalize_values(
    entity: SourceEntity,
    record: SourceRecord,
    options: TransformationOptions,
    asset_url_fields: Sequence[str] = (),
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    """Convert a record's values using the schema's own declared types.

    Returns ``(values, binary_omitted, conversion_issues)``.

    Reuses ``type_converter.convert`` rather than repeating the conversion
    rules, so a source-native record gets exactly the same Decimal, date and
    boolean handling a canonical record does. The only difference is where the
    target type comes from: the canonical path reads it from a mapping profile,
    this path reads it from the discovered ``SourceField``.
    """
    by_name: Mapping[str, SourceField] = {
        field.source_name: field for field in entity.fields
    }

    values: dict[str, Any] = {}
    binary_omitted: list[str] = []
    issues: list[str] = []
    # Phase 8: a declared asset URL is a POINTER, not content. Its literal
    # value adds nothing an embedding can use and may carry a signed token, so
    # it is kept out of the scalar text exactly as binary bytes are - while the
    # field itself stays visible as structure.
    declared_assets = frozenset(asset_url_fields or ())

    for name in sorted(record.values):
        if name in declared_assets:
            binary_omitted.append(name)
            continue

        field = by_name.get(name)
        raw = record.values[name]

        if field is not None and field.normalized_data_type is FieldDataType.BINARY:
            # Described in metadata, never rendered. Phase 3 opens it.
            binary_omitted.append(name)
            continue

        if raw is None:
            values[name] = None
            continue

        target = field.normalized_data_type if field is not None else None
        result = convert(raw, target, options)

        if result.ok:
            values[name] = result.value
        else:
            # The source's own value is kept as text rather than dropped: this
            # path exists precisely for data the framework has no model for, so
            # refusing a value because it does not fit an INFERRED type would
            # lose information the caller can still read. The reason is recorded
            # so the loss of typing is visible rather than silent.
            values[name] = raw if isinstance(raw, (str, int, float, bool)) else str(raw)
            issues.append(f"{name}: {result.reason}")

    return values, tuple(binary_omitted), tuple(issues)


class SourceNativeTransformer:
    """Turns records of an uncovered ERP entity into canonical records.

    Holds no state beyond its options, so one instance serves every entity.
    """

    def __init__(self, options: TransformationOptions | None = None) -> None:
        self.options = options or TransformationOptions()

    def transform_record(
        self,
        record: SourceRecord,
        entity: SourceEntity,
        source_system_id: str,
        source_type: SourceType,
        schema_id: str | None = None,
        schema_version: str | None = None,
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
        key_fields: Sequence[str] | None = None,
        asset_url_fields: Sequence[str] = (),
    ) -> CanonicalRecord:
        """One record. Raises rather than inventing an identity it cannot find."""
        key_name, key_value = resolve_business_key(entity, record, key_fields)
        values, binary_omitted, issues = normalize_values(
            entity, record, self.options, asset_url_fields
        )
        fields_by_source = {field.source_name: field for field in entity.fields}
        filter_values = {
            (
                fields_by_source[name].normalized_name
                if name in fields_by_source
                else name
            ): value
            for name, value in values.items()
        }

        metadata: dict[str, Any] = {
            SOURCE_NATIVE_KEY: True,
            # Preserved for Phase 4, which will expose these as retrieval
            # filters. Phase 2's only obligation is not to discard them.
            BUSINESS_KEY_NAME: key_name,
            BUSINESS_KEY_VALUE: key_value,
            "filter_attributes": normalize_filter_attributes(
                filter_values,
                excluded_fields=tuple(
                    field.normalized_name
                    for field in entity.fields
                    if (field.metadata or {}).get("filterable") is False
                ),
            ),
        }

        if binary_omitted:
            metadata[BINARY_FIELDS_KEY] = list(binary_omitted)

        if issues:
            metadata["conversion_notes"] = issues

        # MongoDB's ObjectId is useful provenance but is not the business
        # identity of an employee.  Keep it attached to the canonical record
        # without ever promoting it to ``source_record_key``.
        if record.metadata.get("source_object_id") is not None:
            metadata["source_object_id"] = str(record.metadata["source_object_id"])

        source_entity = qualified_source_entity(entity)

        return CanonicalRecord.from_source(
            source=SourceReference(
                source_system_id=source_system_id,
                source_type=source_type,
                source_entity=source_entity,
                source_record_key=key_value,
            ),
            # The source's own normalized name. English plurals are NOT guessed
            # into singulars: "address" would become "addres" and the wrong
            # answer would be baked into a stable record id. The canonical model
            # relates "invoices" to "invoice" through DECLARED aliases, and an
            # uncovered entity has none - so its own name is the honest answer.
            entity_type=entity.normalized_name,
            stable_source_key=key_value,
            identity_entity=source_entity,
            normalized_data=values,
            sensitivity=sensitivity,
            provenance=RecordProvenance(
                schema_id=schema_id,
                schema_version=schema_version,
                ingestion_method="source_native_transformation",
            ),
            metadata=metadata,
        )

    def transform_records(
        self,
        records: Sequence[SourceRecord],
        entity: SourceEntity,
        source_system_id: str,
        source_type: SourceType,
        schema_id: str | None = None,
        schema_version: str | None = None,
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
        key_fields: Sequence[str] | None = None,
        asset_url_fields: Sequence[str] = (),
    ) -> SourceNativeResult:
        """A batch. One unusable record does not stop the rest.

        A record without an identity is REPORTED, not silently dropped and not
        given a manufactured key: the caller learns exactly which rows their
        source cannot identify.
        """
        transformed: list[CanonicalRecord] = []
        rejected: list[str] = []
        omitted: set[str] = set()

        for ordinal, record in enumerate(records, start=1):
            try:
                canonical = self.transform_record(
                    record,
                    entity,
                    source_system_id,
                    source_type,
                    schema_id=schema_id,
                    schema_version=schema_version,
                    sensitivity=sensitivity,
                    key_fields=key_fields,
                    asset_url_fields=asset_url_fields,
                )
            except SourceIdentityUnavailableError as error:
                rejected.append(f"record {record.ordinal or ordinal}: {error}")
                continue

            omitted.update(canonical.metadata.get(BINARY_FIELDS_KEY, ()) or ())
            transformed.append(canonical)

        return SourceNativeResult(
            records=tuple(transformed),
            rejected=tuple(rejected),
            binary_fields_omitted=tuple(sorted(omitted)),
        )


__all__ = [
    "BINARY_FIELDS_KEY",
    "SOURCE_NATIVE_KEY",
    "BUSINESS_KEY_NAME",
    "BUSINESS_KEY_VALUE",
    "COMPOSITE_KEY_SEPARATOR",
    "SourceIdentityUnavailableError",
    "SourceNativeResult",
    "SourceNativeTransformer",
    "qualified_source_entity",
    "binary_field_names",
    "resolve_business_key",
    "normalize_values",
]
