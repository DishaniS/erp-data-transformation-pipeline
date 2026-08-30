"""Pairing ERP rows with the documents their binary columns carried (Phase 3).

This module does no extraction and builds no representations - both already
exist. Its whole job is the piece that did not: deciding WHICH ERP record each
BLOB belongs to, so the resulting vector can be traced back to the row that
carried it.

WHY PAIRING NEEDS CARE
----------------------
``EXTRACT`` produces raw ``SourceRecord``s; ``TRANSFORM`` produces
``CanonicalRecord``s. The two lists are usually parallel, but not always - a row
whose identity could not be resolved, or whose values failed validation, is
absent from the second. Zipping them blindly would attach EMP003's certificate
to EMP002 the first time any row was rejected, which is the worst possible
failure: silent, plausible, and wrong.

So pairing is positional ONLY when the lists are the same length, and matches on
the canonical record's own ``source_record_key`` otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from erp_pipeline.ai.attached_documents import (
    DocumentAttachment,
    attached_document_to_representations,
)
from erp_pipeline.schemas.sensitivity import resolve as resolve_sensitivity
from erp_pipeline.ingestion.binary_assets import (
    BinaryAssetOptions,
    BinaryAssetResult,
    extract_binary_asset,
)
from erp_pipeline.transformation.source_native import (
    BUSINESS_KEY_NAME,
    BUSINESS_KEY_VALUE,
)


@dataclass(frozen=True)
class MultimodalExtractionResult:
    """What one MULTIMODAL_EXTRACT stage produced."""

    representations: tuple[Any, ...] = ()
    assets: tuple[BinaryAssetResult, ...] = ()
    fields_seen: int = 0
    extracted: int = 0
    skipped: int = 0
    ocr_assets: int = 0
    #: Declared remote references this run attempted. Counted whether or not
    #: the fetch was permitted, so a refusal is visible rather than silent.
    remote_assets: int = 0
    warnings: tuple[str, ...] = ()


def pair_records(
    source_records: Sequence[Any], canonical_records: Sequence[Any]
) -> tuple[tuple[Any, Any], ...]:
    """Match each raw row to the canonical record it became.

    Positional when the lists are parallel - the normal case, and exact. When
    they are not, the canonical record's ``source_record_key`` is matched
    against the row's values, so a rejected row cannot shift every subsequent
    pairing by one.
    """
    if len(source_records) == len(canonical_records):
        return tuple(zip(source_records, canonical_records))

    pairs: list[tuple[Any, Any]] = []

    for canonical in canonical_records:
        key = getattr(getattr(canonical, "source", None), "source_record_key", None)

        if key is None:
            continue

        for record in source_records:
            values = getattr(record, "values", {}) or {}

            if any(str(value) == str(key) for value in values.values()):
                pairs.append((record, canonical))
                break

    return tuple(pairs)


def _business_identity(canonical: Any) -> tuple[str | None, str | None]:
    """The business key a source-native record preserved, when it has one."""
    metadata: Mapping[str, Any] = getattr(canonical, "metadata", {}) or {}

    return metadata.get(BUSINESS_KEY_NAME), metadata.get(BUSINESS_KEY_VALUE)


def extract_record_assets(
    source_records: Sequence[Any],
    canonical_records: Sequence[Any],
    entity: Any,
    binary_fields: Sequence[str],
    options: BinaryAssetOptions | None = None,
    asset_url_fields: Mapping[str, Any] | None = None,
    field_sensitivity: Mapping[str, Any] | None = None,
    job_sensitivity: Any = None,
    url_policy: Any = None,
    fetcher: Any = None,
    resolver: Any = None,
) -> MultimodalExtractionResult:
    """Open every declared attachment on every paired record.

    Two kinds of attachment, one path. A BLOB carries its bytes; a declared
    remote reference points at them. Once the bytes are in hand the origin stops
    mattering - detection, extraction, OCR, chunking and attachment identity are
    identical, which is why they share this loop rather than having one each.

    Each field is processed INDEPENDENTLY: a refused URL does not stop the
    profile photo beside it, and neither stops the scalar record that was
    already built.
    """
    from erp_pipeline.ingestion.remote_assets import fetch_remote_asset

    representations: list[Any] = []
    assets: list[BinaryAssetResult] = []
    warnings: list[str] = []
    counts = {"seen": 0, "extracted": 0, "skipped": 0, "ocr": 0, "remote": 0}
    declared_urls = dict(asset_url_fields or {})

    def attach(
        asset: BinaryAssetResult,
        parent_id: str,
        source: Any,
        canonical: Any,
        key_name: str | None,
        key_value: str | None,
        field_name: str,
        document_type: str | None,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Turn one extracted asset into representations, or say why not."""
        assets.append(asset)

        if not asset.succeeded:
            counts["skipped"] += 1
            warnings.append(
                f"{parent_id}.{field_name}: {asset.outcome}"
                + (f" - {asset.warnings[0]}" if asset.warnings else "")
            )
            return

        counts["extracted"] += 1
        attachment = DocumentAttachment(
            parent_record_id=parent_id,
            source_system_id=getattr(source, "source_system_id", "unknown_source"),
            source_entity=getattr(source, "source_entity", None)
            or getattr(entity, "source_name", "unknown_entity"),
            source_field=field_name,
            document_id=asset.document_id or "",
            business_key_name=key_name,
            business_key_value=key_value,
            # The ERP's own word for this document. Deterministic context,
            # not a content-derived guess.
            document_type=document_type or field_name,
            # Phase 10: the STRICTEST of what applies - a per-field override,
            # the job's declaration, and whatever the parent record already
            # carries. A field declared `internal` can never downgrade a record
            # declared `restricted`.
            sensitivity=resolve_sensitivity(
                artifact=(field_sensitivity or {}).get(field_name),
                job=job_sensitivity,
                inherited=getattr(canonical, "sensitivity", None),
            ).value,
            media_type=asset.media_type,
            # Inherited from the PARENT record's own already-curated filter
            # attributes, never re-derived from its normalized_data here: a
            # document belongs to a record whose schema-aware ingestion path
            # already decided which of its fields are safe to expose as
            # filters (see ai.representation.canonical_record_to_representation).
            # A blind re-derivation would dump arbitrary business content -
            # exactly the defect this boundary exists to close.
            filter_attributes=dict(
                (getattr(canonical, "metadata", None) or {}).get(
                    "filter_attributes"
                )
                or {}
            ),
        )
        built = attached_document_to_representations(asset.document, attachment)

        if not built:
            # Extraction succeeded but yielded no text - a blank scan, or an
            # image OCR could not read. Reported rather than counted as an
            # indexed document, because nothing was indexed.
            counts["skipped"] += 1
            counts["extracted"] -= 1
            warnings.append(
                f"{parent_id}.{field_name}: extracted but produced no text "
                f"to index (status: {asset.extraction_status})"
            )
            return

        # Counted only once the asset is actually indexed. An image OCR ran
        # over and found nothing in is not an "OCR asset" in any sense a
        # reader of this number would mean - it produced no vector at all.
        if asset.ocr_used:
            counts["ocr"] += 1

        if extra_metadata:
            # Redacted remote provenance, merged onto every chunk of this
            # document. Never the raw URL.
            built = tuple(
                replace(item, metadata={**dict(item.metadata), **dict(extra_metadata)})
                for item in built
            )

        representations.extend(built)

    for record, canonical in pair_records(source_records, canonical_records):
        parent_id = getattr(canonical, "record_id", None)

        if not parent_id:
            continue

        source = getattr(canonical, "source", None)
        key_name, key_value = _business_identity(canonical)
        values = getattr(record, "values", {}) or {}

        # -- bytes the row carried --
        for field_name in binary_fields:
            if field_name not in values or values[field_name] is None:
                continue

            counts["seen"] += 1
            attach(
                extract_binary_asset(values[field_name], field_name, options),
                parent_id, source, canonical, key_name, key_value,
                field_name, None,
            )

        # -- bytes the row POINTED at, and only where a caller declared it --
        for field_name, document_type in declared_urls.items():
            if field_name not in values or values[field_name] is None:
                continue

            counts["seen"] += 1
            counts["remote"] += 1
            asset, provenance = fetch_remote_asset(
                values[field_name], field_name, url_policy, fetcher, resolver,
                options,
            )
            attach(
                asset, parent_id, source, canonical, key_name, key_value,
                field_name, document_type,
                provenance.to_metadata() if provenance else None,
            )

    return MultimodalExtractionResult(
        representations=tuple(representations),
        assets=tuple(assets),
        fields_seen=counts["seen"],
        extracted=counts["extracted"],
        skipped=counts["skipped"],
        ocr_assets=counts["ocr"],
        remote_assets=counts["remote"],
        warnings=tuple(warnings),
    )


__all__ = [
    "MultimodalExtractionResult",
    "extract_record_assets",
    "pair_records",
]
