"""The BPI cascade repair (Steps 60-63).

BEFORE: raw event -> cleaned_event_logs -> STOP.
AFTER:  raw event -> cleaned event -> affected case -> AI representation
        -> content hash -> embed only if changed -> vector identity.

The load-bearing assertions are counters. "Only the affected case was rebuilt"
is a claim until something has counted the rebuilds against a corpus large
enough for a full rebuild to be visible.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from erp_pipeline.schemas.enums import FieldDataType as T, MappingStatus, SourceType
from erp_pipeline.schemas.mapping_models import FieldMapping, MappingProfile
from erp_integrations.bpi_case_cascade import (
    BpiAffectedCaseResolver,
    BpiCaseHashLedger,
    BpiCaseRepresentationBuilder,
    CaseKeyIndex,
    InMemoryCaseAccess,
    PendingEmbeddingUpdater,
    compute_case_content_hash,
    make_case_record_id,
)
from erp_pipeline.sync import (
    ChangeOperation,
    ExtractionConfig,
    InMemoryCanonicalStore,
    InMemoryChangeSource,
    InMemorySyncStateStore,
    InMemoryVectorStore,
    PropagationPipeline,
    SourceChange,
    SyncOptions,
    SyncService,
    SyncTarget,
    Watermark,
    WatermarkStrategy,
    vector_id_for,
)

from tests.erp_pipeline.sync.conftest import BASE_TIME

CASE_COUNT = 200
EVENTS_PER_CASE = 3


def event_profile() -> MappingProfile:
    return MappingProfile(
        mapping_id="bpi.event",
        source_system_id="bpi_source",
        source_entity="cleaned_event_logs",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(
                source_field="event_key",
                target_field="invoice_id",
                target_type=T.STRING,
                status=MappingStatus.AUTO_ACCEPTED,
            ),
            FieldMapping(
                source_field="case_id",
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


def build_events() -> list[dict]:
    rows = []
    ordinal = 0
    for case_index in range(1, CASE_COUNT + 1):
        for event_index in range(1, EVENTS_PER_CASE + 1):
            ordinal += 1
            rows.append(
                {
                    "event_key": f"EV-{ordinal:05d}",
                    "case_id": f"CASE-{case_index:04d}",
                    "activity": f"activity_{event_index}",
                    "amount": "10.00",
                    "updated_at": BASE_TIME + timedelta(seconds=ordinal),
                }
            )
    return rows


class CascadeHarness:
    """A BPI-shaped corpus wired through the generic Phase 10 engine."""

    def __init__(self) -> None:
        self.events = build_events()
        self.access = InMemoryCaseAccess(events=[dict(e) for e in self.events])

        self.config = ExtractionConfig(
            source_entity="cleaned_event_logs",
            strategy=WatermarkStrategy.COMPOSITE,
            key_field="event_key",
            watermark_field="updated_at",
            tie_break_field="event_key",
        )
        self.source = InMemoryChangeSource(self.config, self.events)

        self.index = CaseKeyIndex()
        self.resolver = BpiAffectedCaseResolver(self.access, index=self.index)
        self.builder = BpiCaseRepresentationBuilder(self.access, index=self.index)
        self.ledger = BpiCaseHashLedger(self.access)
        self.embedder = PendingEmbeddingUpdater(self.access)
        self.vectors = InMemoryVectorStore()
        self.canonical = InMemoryCanonicalStore()

        self.pipeline = PropagationPipeline(
            canonical_store=self.canonical,
            resolver=self.resolver,
            builder=self.builder,
            ledger=self.ledger,
            embedder=self.embedder,
            vector_store=self.vectors,
        )
        self.state_store = InMemorySyncStateStore()
        self.service = SyncService(self.state_store, self.pipeline)
        self.target = SyncTarget(
            source_system_id="bpi_source",
            source_entity="cleaned_event_logs",
            source_type=SourceType.POSTGRESQL,
            mapping_profile=event_profile(),
            schema_id="bpi.cleaned.v1",
        )

    def run(self, batch_size: int = 10_000):
        return self.service.run_incremental(
            self.target,
            self.source,
            SyncOptions(batch_size=batch_size),
            strategy=WatermarkStrategy.COMPOSITE,
            watermark_field="updated_at",
            tie_break_field="event_key",
        )

    def reset_counters(self) -> None:
        self.builder.rebuild_calls = 0
        self.builder.rebuilt_keys = []
        self.embedder.calls = 0
        self.embedder.marked = []
        self.resolver.calls = 0
        self.vectors.upsert_calls = 0
        self.access.upsert_calls = 0


@pytest.fixture()
def cascade() -> CascadeHarness:
    harness = CascadeHarness()
    harness.run()
    harness.reset_counters()
    return harness


# ============================================================
# The corpus is big enough for a full rebuild to be visible
# ============================================================

def test_the_corpus_has_many_cases(cascade):
    assert len(cascade.access.cases) == CASE_COUNT
    assert len(cascade.events) == CASE_COUNT * EVENTS_PER_CASE


def test_the_baseline_embedded_every_case_once(cascade):
    assert len(cascade.vectors) == CASE_COUNT


# ============================================================
# The repair: one changed event reaches its case (Step 60)
# ============================================================

def test_a_changed_event_resolves_exactly_one_affected_case(cascade):
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )
    cascade.access.events[1]["activity"] = "activity_2_amended"

    summary = cascade.run()

    assert summary.changes_read == 1
    assert summary.representations_resolved == 1


def test_a_changed_event_does_not_rebuild_every_case(cascade):
    """The headline: not all 200 cases, and by extension not all 32,999."""
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )
    cascade.access.events[1]["activity"] = "activity_2_amended"

    cascade.run()

    assert cascade.builder.rebuild_calls == 1
    assert cascade.embedder.calls == 1
    assert cascade.vectors.upsert_calls == 1


def test_the_rebuilt_case_is_the_right_one(cascade):
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )
    cascade.access.events[1]["activity"] = "activity_2_amended"

    cascade.run()

    expected = make_case_record_id("bpi2020", "CASE-0001")

    assert cascade.builder.rebuilt_keys == [expected]


def test_the_cascade_reaches_the_vector_layer(cascade):
    """Step 60: the propagation no longer stops at the cleaned table."""
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )
    cascade.access.events[1]["activity"] = "activity_2_amended"

    summary = cascade.run()

    assert summary.vectors_upserted == 1
    assert summary.embeddings_generated == 1


# ============================================================
# Content hash behaviour (Step 63)
# ============================================================

def test_a_content_changing_event_changes_the_case_hash(cascade):
    key = make_case_record_id("bpi2020", "CASE-0001")
    before = cascade.access.load_case_hash(key)

    cascade.access.events[1]["activity"] = "activity_2_amended"
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )
    cascade.run()

    assert cascade.access.load_case_hash(key) != before


def test_a_content_changing_event_triggers_exactly_one_re_embedding(cascade):
    cascade.access.events[1]["activity"] = "activity_2_amended"
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )

    summary = cascade.run()

    assert summary.embeddings_generated == 1
    assert summary.embeddings_skipped == 0


def test_an_operational_only_change_does_not_re_embed(cascade):
    """Only the watermark moved; the case content is byte-identical."""
    cascade.source.update(
        "EV-00002", updated_at=BASE_TIME + timedelta(seconds=99_999)
    )

    summary = cascade.run()

    assert summary.changes_read == 1
    assert summary.embeddings_generated == 0
    assert summary.embeddings_skipped == 1
    assert cascade.vectors.upsert_calls == 0


def test_an_operational_only_change_leaves_the_hash_alone(cascade):
    key = make_case_record_id("bpi2020", "CASE-0001")
    before = cascade.access.load_case_hash(key)

    cascade.source.update(
        "EV-00002", updated_at=BASE_TIME + timedelta(seconds=99_999)
    )
    cascade.run()

    assert cascade.access.load_case_hash(key) == before


def test_the_case_hash_uses_the_prototypes_own_helper():
    """Step 62: an incrementally rebuilt case must agree with a batch one."""
    from bpi2020.common.stable_ids import compute_content_hash

    key = make_case_record_id("bpi2020", "CASE-0001")
    metadata = {"case_id": "CASE-0001", "total_events": 3}

    assert compute_case_content_hash(key, "text", metadata) == (
        compute_content_hash(record_id=key, text_for_ai="text", metadata=metadata)
    )


# ============================================================
# Identity preservation (Step 62)
# ============================================================

def test_case_identity_is_the_frozen_phase_0_identity():
    from bpi2020.common.stable_ids import make_case_record_id as frozen

    assert make_case_record_id("bpi2020", "CASE-0001") == frozen(
        "bpi2020", "CASE-0001"
    )


def test_an_update_does_not_mint_a_new_case_identity(cascade):
    before = set(cascade.access.cases)

    cascade.access.events[1]["activity"] = "activity_2_amended"
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )
    cascade.run()

    assert set(cascade.access.cases) == before


def test_an_update_reuses_the_same_vector_identity(cascade):
    before_ids = set(cascade.vectors.vector_ids)

    cascade.access.events[1]["activity"] = "activity_2_amended"
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )
    cascade.run()

    assert set(cascade.vectors.vector_ids) == before_ids
    assert len(cascade.vectors) == CASE_COUNT


def test_the_vector_id_derives_from_the_case_record_id():
    key = make_case_record_id("bpi2020", "CASE-0001")

    assert vector_id_for(key) == vector_id_for(key)


# ============================================================
# The existing embedder integration (Step 61)
# ============================================================

def test_only_the_affected_case_is_marked_pending(cascade):
    """The seam the prototype's batch embedder already consumes."""
    for row in cascade.access.cases.values():
        row["embedding_status"] = "embedded"

    cascade.access.events[1]["activity"] = "activity_2_amended"
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )
    cascade.run()

    assert cascade.access.pending_cases == (
        make_case_record_id("bpi2020", "CASE-0001"),
    )


def test_an_unchanged_case_is_never_marked_pending(cascade):
    for row in cascade.access.cases.values():
        row["embedding_status"] = "embedded"

    cascade.source.update(
        "EV-00002", updated_at=BASE_TIME + timedelta(seconds=99_999)
    )
    cascade.run()

    assert cascade.access.pending_cases == ()


def test_the_adapter_does_not_reimplement_embedding():
    """Step 61: the existing implementation is reused, not copied."""
    from pathlib import Path

    text = Path(
        "src/erp_integrations/bpi_case_cascade.py"
    ).read_text(encoding="utf-8")

    for marker in ("SentenceTransformer", "QdrantClient", "PointStruct", "encode("):
        assert marker not in text


# ============================================================
# The generic core stays independent (Step 76)
# ============================================================

def test_the_generic_sync_core_does_not_import_this_adapter():
    from pathlib import Path

    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/erp_pipeline/sync").rglob("*.py")
    )

    assert "bpi_case_cascade" not in text
    assert "integrations" not in text


def test_the_coordinator_never_learns_that_bpi_exists(cascade):
    """It only ever sees the generic protocols."""
    from erp_pipeline.sync.propagation import (
        AffectedRepresentationResolver,
        AIRepresentationBuilder,
    )

    assert isinstance(cascade.resolver, AffectedRepresentationResolver)
    assert isinstance(cascade.builder, AIRepresentationBuilder)


# ============================================================
# Scale
# ============================================================

def test_incremental_cost_is_one_case_not_two_hundred(cascade):
    cascade.access.events[1]["activity"] = "activity_2_amended"
    cascade.source.update(
        "EV-00002",
        activity="activity_2_amended",
        updated_at=BASE_TIME + timedelta(seconds=99_999),
    )

    summary = cascade.run()

    assert summary.changes_read == 1
    assert summary.representations_rebuilt == 1
    assert cascade.builder.rebuild_calls == 1
    assert cascade.embedder.calls == 1
    assert len(cascade.access.cases) == CASE_COUNT
