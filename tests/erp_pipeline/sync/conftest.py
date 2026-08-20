"""Shared fixtures for the Phase 10 incremental sync tests.

The representation builder here is deliberately a real one rather than a stub:
it derives its AI text from canonical content, so the content-hash behaviour
under test is genuine rather than arranged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import pytest

from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType as T,
    MappingStatus,
    SchemaOrigin,
    SourceType,
)
from erp_pipeline.schemas.mapping_models import FieldMapping, MappingProfile
from erp_pipeline.schemas.source_models import (
    SourceEntity,
    SourceField,
    SourceSchema,
)
from erp_pipeline.schemas.identity import normalize_identifier
from erp_pipeline.sync import (
    AIRepresentation,
    CountingEmbeddingUpdater,
    ExtractionConfig,
    InMemoryCanonicalStore,
    InMemoryChangeSource,
    InMemoryHashLedger,
    InMemorySyncStateStore,
    InMemoryVectorStore,
    PropagationPipeline,
    StaticAffectedResolver,
    SyncService,
    SyncTarget,
    WatermarkStrategy,
)

BASE_TIME = datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc)

#: Planted in source rows that legitimately carry them. They may reach a
#: canonical record; they must never reach a report, a log or an error.
SECRET_CUSTOMER = "SECRET_CUSTOMER_93821"
SECRET_ACCOUNT = "SECRET_ACCOUNT_22118"
SECRET_EMAIL = "SECRET_EMAIL_44519"


# ============================================================
# Mapping profile and schemas
# ============================================================

def invoice_profile(
    mapping_id: str = "p10.inv", source_system_id: str = "erp_pg"
) -> MappingProfile:
    return MappingProfile(
        mapping_id=mapping_id,
        source_system_id=source_system_id,
        source_entity="phase10_invoice",
        target_entity_type="invoice",
        source_schema_id="erp_pg.phase10.v1",
        field_mappings=(
            FieldMapping(
                source_field="id",
                target_field="invoice_id",
                target_type=T.STRING,
                status=MappingStatus.AUTO_ACCEPTED,
            ),
            FieldMapping(
                source_field="customer_id",
                target_field="customer_id",
                target_type=T.STRING,
                status=MappingStatus.AUTO_ACCEPTED,
            ),
            FieldMapping(
                source_field="amount",
                target_field="amount",
                target_type=T.DECIMAL,
                status=MappingStatus.AUTO_ACCEPTED,
            ),
        ),
    )


def make_field(
    name: str,
    data_type: T,
    *,
    nullable: bool = True,
    required: bool = False,
    is_primary_key: bool = False,
    source_data_type: str | None = None,
) -> SourceField:
    return SourceField(
        source_name=name,
        normalized_name=normalize_identifier(name),
        source_data_type=source_data_type or data_type.value,
        normalized_data_type=data_type,
        nullable=nullable,
        required=required,
        is_primary_key=is_primary_key,
    )


def make_schema(
    fields: Sequence[SourceField],
    schema_id: str = "erp_pg.phase10.v1",
    entity_name: str = "phase10_invoice",
) -> SourceSchema:
    entity = SourceEntity(
        entity_id=normalize_identifier(f"e.{entity_name}"),
        source_name=entity_name,
        normalized_name=normalize_identifier(entity_name),
        entity_kind=EntityKind.TABLE,
        fields=tuple(fields),
    )

    return SourceSchema(
        schema_id=schema_id,
        source_system_id="erp_pg",
        schema_name="phase10",
        origin=SchemaOrigin.DISCOVERED,
        entities=(entity,),
        schema_hash="0" * 64,
    )


def schema_v1() -> SourceSchema:
    """``invoice_id STRING`` + ``invoice_amount DECIMAL`` (Step 54)."""
    return make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING),
            make_field("amount", T.DECIMAL),
        )
    )


def schema_v2_type_changed_and_field_added() -> SourceSchema:
    """``amount`` becomes STRING, ``tax_amount`` appears (Step 54)."""
    return make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING),
            make_field("amount", T.STRING),
            make_field("tax_amount", T.DECIMAL),
        ),
        schema_id="erp_pg.phase10.v2",
    )


def schema_v3_field_removed() -> SourceSchema:
    """``amount`` - a mapped field - disappears (Step 55)."""
    return make_schema(
        (
            make_field("id", T.STRING, nullable=False, is_primary_key=True),
            make_field("customer_id", T.STRING),
        ),
        schema_id="erp_pg.phase10.v3",
    )


# ============================================================
# Source rows
# ============================================================

def invoice_row(
    index: int,
    customer_id: str = "C001",
    amount: str = "100.00",
    offset_seconds: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "id": f"INV-{index:03d}",
        "customer_id": customer_id,
        "amount": amount,
        "updated_at": BASE_TIME
        + timedelta(seconds=index if offset_seconds is None else offset_seconds),
    }
    row.update(extra)
    return row


def extraction_config(
    strategy: WatermarkStrategy = WatermarkStrategy.COMPOSITE,
    deleted_flag_field: str | None = None,
) -> ExtractionConfig:
    return ExtractionConfig(
        source_entity="phase10_invoice",
        strategy=strategy,
        key_field="id",
        watermark_field=(
            "updated_at"
            if strategy
            in (WatermarkStrategy.TIMESTAMP, WatermarkStrategy.COMPOSITE)
            else None
        ),
        tie_break_field=(
            "id"
            if strategy
            in (WatermarkStrategy.COMPOSITE, WatermarkStrategy.MONOTONIC_ID)
            else None
        ),
        deleted_flag_field=deleted_flag_field,
    )


# ============================================================
# A real representation builder
# ============================================================

class CanonicalRepresentationBuilder:
    """Builds one AI-ready representation per canonical record.

    Real, not stubbed: the AI text is derived from the canonical content, so
    "content changed" and "content did not change" are genuine properties of
    the data rather than something the test arranged.
    """

    def __init__(self, store: InMemoryCanonicalStore) -> None:
        self._store = store
        self.rebuild_calls = 0
        self.rebuilt_keys: list[str] = []

    def rebuild(self, key: str) -> AIRepresentation | None:
        self.rebuild_calls += 1
        self.rebuilt_keys.append(key)

        record = self._store.get(key)
        if record is None:
            return None

        data = dict(record.normalized_data)

        return AIRepresentation(
            representation_id=key,
            entity_type=record.entity_type,
            text_for_ai=(
                f"Invoice {data.get('invoice_id')} for customer "
                f"{data.get('customer_id')} amount {data.get('amount')}"
            ),
            content=data,
            source_record_ids=(record.record_id,),
        )

    def reset_counters(self) -> None:
        self.rebuild_calls = 0
        self.rebuilt_keys = []


class Harness:
    """Everything one incremental scenario needs, with counters."""

    def __init__(
        self,
        rows: Sequence[Mapping[str, Any]] = (),
        strategy: WatermarkStrategy = WatermarkStrategy.COMPOSITE,
        deleted_flag_field: str | None = None,
    ) -> None:
        self.config = extraction_config(strategy, deleted_flag_field)
        self.source = InMemoryChangeSource(self.config, rows)
        self.state_store = InMemorySyncStateStore()
        self.canonical = InMemoryCanonicalStore()
        self.resolver = StaticAffectedResolver()
        self.builder = CanonicalRepresentationBuilder(self.canonical)
        self.ledger = InMemoryHashLedger()
        self.embedder = CountingEmbeddingUpdater()
        self.vectors = InMemoryVectorStore()
        self.pipeline = PropagationPipeline(
            canonical_store=self.canonical,
            resolver=self.resolver,
            builder=self.builder,
            ledger=self.ledger,
            embedder=self.embedder,
            vector_store=self.vectors,
        )
        self.service = SyncService(self.state_store, self.pipeline)
        self.target = SyncTarget(
            source_system_id="erp_pg",
            source_entity="phase10_invoice",
            source_type=SourceType.POSTGRESQL,
            mapping_profile=invoice_profile(),
            schema_id="erp_pg.phase10.v1",
            schema_hash="0" * 64,
        )
        self.strategy = strategy

    def run(self, options=None):
        from erp_pipeline.sync import SyncOptions

        return self.service.run_incremental(
            self.target,
            self.source,
            options or SyncOptions(batch_size=500),
            strategy=self.strategy,
            watermark_field=self.config.watermark_field,
            tie_break_field=self.config.tie_break_field,
        )

    def catch_up(self, options=None):
        from erp_pipeline.sync import SyncOptions

        return self.service.catch_up(
            self.target,
            self.source,
            options or SyncOptions(batch_size=500),
            strategy=self.strategy,
            watermark_field=self.config.watermark_field,
            tie_break_field=self.config.tie_break_field,
        )

    def reset_counters(self) -> None:
        self.builder.reset_counters()
        self.embedder.calls = 0
        self.embedder.embedded_ids = []
        self.vectors.upsert_calls = 0
        self.vectors.delete_calls = 0
        self.canonical.upsert_calls = 0
        self.canonical.delete_calls = 0

    @property
    def state(self):
        return self.state_store.load("erp_pg", "phase10_invoice")


@pytest.fixture()
def harness() -> Harness:
    """A harness with 100 synchronized invoices."""
    instance = Harness(rows=[invoice_row(i) for i in range(1, 101)])
    instance.catch_up()
    instance.reset_counters()
    return instance


@pytest.fixture()
def empty_harness() -> Harness:
    return Harness(rows=[])
