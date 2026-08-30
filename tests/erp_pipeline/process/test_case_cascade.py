"""Incremental propagation from one changed event to one case's vector.

Migrated from ``tests/erp_pipeline/sync/test_bpi_cascade_repair.py`` and
``test_bpi_embedding_vector_adapters.py``, which tested the same behaviour
through the ``erp_integrations`` adapters. The adapters existed only because
the case logic lived in a separate dataset package; now that it is generic,
the cascade is generic too and the adapters are gone.

The behaviour asserted is unchanged: one changed event rebuilds exactly one
case, an unchanged rebuild produces an unchanged hash, and a case whose events
have all gone is reported as deleted rather than silently kept.
"""

from __future__ import annotations

import pytest

from erp_pipeline.process import (
    CaseKeyIndex,
    EventLogConfig,
    InMemoryCaseEventSource,
    ProcessCaseRepresentationBuilder,
    ProcessCaseResolver,
    ProcessCaseService,
    build_case_cascade,
    make_case_record_id,
)
from erp_pipeline.sync.propagation import (
    EmbeddingResult,
    InMemoryHashLedger,
    InMemoryVectorStore,
)

SYSTEM = "erp_demo"
PROCESS = "declarations"


class FakeChange:
    """The shape ``IncrementalCoordinator`` hands a resolver."""

    def __init__(self, record_key, payload=None):
        self.record_key = record_key
        self.payload = payload or {}


@pytest.fixture
def config():
    return EventLogConfig(
        case_id_field="case_id",
        activity_field="activity",
        timestamp_field="ts",
        event_key_field="event_key",
        process_type=PROCESS,
    )


@pytest.fixture
def source():
    access = InMemoryCaseEventSource()
    access.add(
        {
            "case_id": "Declaration 100000",
            "activity": "SUBMITTED",
            "ts": "2026-01-01 09:00:00",
            "event_key": "e1",
        }
    )
    access.add(
        {
            "case_id": "Declaration 100000",
            "activity": "APPROVED",
            "ts": "2026-01-03 09:00:00",
            "event_key": "e2",
        }
    )
    access.add(
        {
            "case_id": "Declaration 100001",
            "activity": "SUBMITTED",
            "ts": "2026-01-02 09:00:00",
            "event_key": "e3",
        }
    )
    return access


@pytest.fixture
def service(config):
    return ProcessCaseService(SYSTEM, config)


@pytest.fixture
def cascade(source, service):
    return build_case_cascade(source, service, process_type=PROCESS)


# ============================================================
# Resolution
# ============================================================


def test_one_changed_event_affects_exactly_one_case(cascade):
    resolver, _builder = cascade

    affected = resolver.resolve_affected(FakeChange("e1"))

    assert affected == (
        make_case_record_id(SYSTEM, PROCESS, "Declaration 100000"),
    )


def test_the_case_id_is_read_from_the_payload_when_present(cascade):
    """Avoids a database round trip on every change."""
    resolver, _builder = cascade
    source = resolver.access
    before = len(source.rows)

    affected = resolver.resolve_affected(
        FakeChange("unknown-key", {"case_id": "Declaration 100001"})
    )

    assert affected == (
        make_case_record_id(SYSTEM, PROCESS, "Declaration 100001"),
    )
    assert len(source.rows) == before


def test_an_event_belonging_to_no_case_yields_no_work(cascade):
    resolver, _builder = cascade

    assert resolver.resolve_affected(FakeChange("does-not-exist")) == ()


def test_resolution_records_the_source_key_for_the_builder(cascade):
    """Normalization is lossy, so the reverse mapping must be remembered."""
    resolver, builder = cascade
    record_id = resolver.resolve_affected(FakeChange("e1"))[0]

    assert builder.index.resolve(record_id) == "Declaration 100000"


def test_the_resolver_and_builder_share_one_index(cascade):
    resolver, builder = cascade

    assert resolver.index is builder.index


# ============================================================
# Rebuilding
# ============================================================


def test_rebuilding_reads_only_the_affected_case(cascade):
    resolver, builder = cascade
    record_id = resolver.resolve_affected(FakeChange("e1"))[0]

    representation = builder.rebuild(record_id)

    assert representation is not None
    assert representation.content["case_id"] == "Declaration 100000"
    assert representation.content["total_events"] == 2


def test_a_rebuilt_representation_carries_the_case_identity(cascade):
    resolver, builder = cascade
    record_id = resolver.resolve_affected(FakeChange("e1"))[0]

    representation = builder.rebuild(record_id)

    assert representation.source_record_ids == (record_id,)
    assert representation.metadata["canonical_record_id"] == record_id


def test_rebuilding_an_unchanged_case_produces_an_unchanged_hash(cascade):
    """If this drifted, every incremental run would re-embed the world."""
    resolver, builder = cascade
    record_id = resolver.resolve_affected(FakeChange("e1"))[0]

    first = builder.rebuild(record_id)
    second = builder.rebuild(record_id)

    assert first.resolved_hash() == second.resolved_hash()


def test_an_incremental_rebuild_matches_a_full_rebuild(cascade, source, service):
    """The property the whole cascade rests on: a case rebuilt from one changed
    event must be byte-identical to the same case rebuilt in a batch run."""
    resolver, builder = cascade
    record_id = resolver.resolve_affected(FakeChange("e1"))[0]

    incremental = builder.rebuild(record_id)

    batch_cases = service.build_cases(source.rows, derive_process_model=False)
    batch = next(
        service.to_representation(case)
        for case in batch_cases
        if case.case_record_id == record_id
    )

    assert incremental.resolved_hash() == batch.resolved_hash()
    assert incremental.text_for_ai == batch.text_for_ai


def test_a_changed_event_changes_the_case_hash(cascade, source):
    resolver, builder = cascade
    record_id = resolver.resolve_affected(FakeChange("e1"))[0]

    before = builder.rebuild(record_id).resolved_hash()

    source.add(
        {
            "case_id": "Declaration 100000",
            "activity": "PAID",
            "ts": "2026-01-09 09:00:00",
            "event_key": "e4",
        }
    )

    assert builder.rebuild(record_id).resolved_hash() != before


def test_a_case_whose_events_all_vanished_is_reported_as_deleted(cascade, source):
    """Returning None is load-bearing: it tells the coordinator to drop the
    vector, rather than leaving an orphan behind."""
    resolver, builder = cascade
    record_id = resolver.resolve_affected(FakeChange("e1"))[0]

    source.remove_case("Declaration 100000")

    assert builder.rebuild(record_id) is None


def test_rebuilding_an_unknown_key_falls_back_without_crashing(source, service):
    builder = ProcessCaseRepresentationBuilder(
        access=source, service=service, index=CaseKeyIndex()
    )

    assert builder.rebuild("erp:erp_demo:declarations:never_seen") is None


def test_the_vector_id_is_stable_across_rebuilds(cascade, source):
    """Vector identity must survive a content change, or every edit would
    accumulate a new point instead of updating one."""
    resolver, builder = cascade
    record_id = resolver.resolve_affected(FakeChange("e1"))[0]

    before = builder.rebuild(record_id).vector_id

    source.add(
        {
            "case_id": "Declaration 100000",
            "activity": "PAID",
            "ts": "2026-01-09 09:00:00",
            "event_key": "e4",
        }
    )

    assert builder.rebuild(record_id).vector_id == before


# ============================================================
# Ledger interaction (skip-if-unchanged)
# ============================================================


def test_an_unchanged_case_is_recognised_by_its_hash(cascade):
    resolver, builder = cascade
    ledger = InMemoryHashLedger()

    record_id = resolver.resolve_affected(FakeChange("e1"))[0]
    representation = builder.rebuild(record_id)
    ledger.set_hash(record_id, representation.resolved_hash())

    rebuilt = builder.rebuild(record_id)

    assert ledger.get_hash(record_id) == rebuilt.resolved_hash()


def test_a_changed_case_is_recognised_by_its_hash(cascade, source):
    resolver, builder = cascade
    ledger = InMemoryHashLedger()

    record_id = resolver.resolve_affected(FakeChange("e1"))[0]
    ledger.set_hash(record_id, builder.rebuild(record_id).resolved_hash())

    source.add(
        {
            "case_id": "Declaration 100000",
            "activity": "PAID",
            "ts": "2026-01-09 09:00:00",
            "event_key": "e9",
        }
    )

    assert ledger.get_hash(record_id) != builder.rebuild(record_id).resolved_hash()


def test_a_rebuilt_case_upserts_rather_than_duplicating_a_vector(cascade, source):
    """One case must own one vector for its whole life, however often its
    content changes. Anything else accumulates a point per edit."""
    resolver, builder = cascade
    store = InMemoryVectorStore()

    record_id = resolver.resolve_affected(FakeChange("e1"))[0]
    first = builder.rebuild(record_id)
    store.upsert(
        first,
        EmbeddingResult(
            representation_id=first.representation_id,
            content_hash=first.resolved_hash(),
            vector=(0.1, 0.2),
            model_id="test-model",
        ),
    )

    source.add(
        {
            "case_id": "Declaration 100000",
            "activity": "PAID",
            "ts": "2026-01-09 09:00:00",
            "event_key": "e9",
        }
    )
    second = builder.rebuild(record_id)
    store.upsert(
        second,
        EmbeddingResult(
            representation_id=second.representation_id,
            content_hash=second.resolved_hash(),
            vector=(0.3, 0.4),
            model_id="test-model",
        ),
    )

    assert store.upsert_calls == 2
    assert len(store) == 1


# ============================================================
# Multi-process behaviour
# ============================================================


def test_two_processes_reusing_a_case_number_do_not_collide(config, service):
    """The failure mode the prototype's identity scheme could not express."""
    source = InMemoryCaseEventSource()
    source.add({"case_id": "1", "activity": "a", "ts": "2026-01-01", "event_key": "x"})

    resolver = ProcessCaseResolver(
        access=source,
        source_system_id=SYSTEM,
        config=config,
        process_type="declarations",
    )
    other = ProcessCaseResolver(
        access=source,
        source_system_id=SYSTEM,
        config=config,
        process_type="permits",
    )

    assert resolver.resolve_affected(FakeChange("x")) != other.resolve_affected(
        FakeChange("x")
    )


def test_the_process_type_can_come_from_the_change_payload():
    config = EventLogConfig(
        case_id_field="case_id",
        activity_field="activity",
        process_type_field="process",
        process_type="fallback",
    )
    source = InMemoryCaseEventSource()
    resolver = ProcessCaseResolver(
        access=source, source_system_id=SYSTEM, config=config
    )

    affected = resolver.resolve_affected(
        FakeChange("x", {"case_id": "1", "process": "permits"})
    )

    assert affected == (make_case_record_id(SYSTEM, "permits", "1"),)


def test_the_cascade_module_never_imports_a_dataset_module():
    import pathlib

    module = (
        pathlib.Path(__file__).resolve().parents[3]
        / "src"
        / "erp_pipeline"
        / "process"
        / "cascade.py"
    )
    source = module.read_text(encoding="utf-8")

    assert "bpi2020" not in source
    assert "erp_integrations" not in source
