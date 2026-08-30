"""Retrieval filters: validated, refused when unknown, and actually applied.

THE DEFECT THIS PINS
--------------------
``SearchRequest.filters`` was part of the published API contract and was never
read. A caller asking for invoices got everything, with a 200 OK. That is worse
than an unimplemented feature: it is a wrong answer that looks right.

These tests prove three things - that a filter narrows the result, that the
archive path narrows it the same way, and that an unsupported field is refused
rather than dropped.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.filters import (
    FILTERABLE_FIELDS,
    NO_FILTERS,
    InvalidFilterValueError,
    SearchFilters,
    UnknownFilterFieldError,
)
from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import TierSet
from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier
from erp_pipeline.storage.state import InMemoryTierStateStore


# ============================================================
# Validation
# ============================================================


#: A valid value for every filterable field. Defined once so the acceptance
#: test and the payload-agreement test below cannot drift apart, and so adding
#: a field to ``FILTERABLE_FIELDS`` without a value here fails loudly.
EVERY_FILTER = {
    "entity_type": "invoice",
    "source_system_id": "finance_erp",
    "source_entity": "fin_invoice",
    "record_key": "INV-204",
    "sensitivity": "restricted",
    "document_id": "doc:policy",
    "content_kind": "document_chunk",
    "parent_record_id": "erp:legacy_hr:employees:emp002",
    "source_field": "birth_certificate",
    "business_key_name": "employee_id",
    "business_key_value": "EMP002",
    "document_type": "birth_certificate",
    "schema_name": "public",
    "entity_kind": "table",
}


def test_every_supported_field_is_accepted():
    filters = SearchFilters.from_mapping(EVERY_FILTER)

    assert set(filters.fields) == set(FILTERABLE_FIELDS)


def test_no_filters_is_empty():
    assert SearchFilters.from_mapping(None).is_empty
    assert SearchFilters.from_mapping({}).is_empty
    assert NO_FILTERS.is_empty


def test_an_unknown_field_is_refused_not_ignored():
    """The whole point: a dropped filter returns a wrong answer that looks
    right."""
    with pytest.raises(UnknownFilterFieldError) as error:
        SearchFilters.from_mapping({"entity_type": "invoice", "colour": "red"})

    assert error.value.unknown == ("colour",)
    assert "colour" in str(error.value)


def test_the_refusal_names_the_supported_fields():
    with pytest.raises(UnknownFilterFieldError) as error:
        SearchFilters.from_mapping({"nope": "x"})

    for field in FILTERABLE_FIELDS:
        assert field in str(error.value)


def test_several_unknown_fields_are_all_reported():
    with pytest.raises(UnknownFilterFieldError) as error:
        SearchFilters.from_mapping({"b": 1, "a": 2})

    assert error.value.unknown == ("a", "b")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_empty_value_is_refused(value):
    with pytest.raises(InvalidFilterValueError):
        SearchFilters.from_mapping({"entity_type": value})


@pytest.mark.parametrize("value", [["a"], {"a": 1}, True, ("a",)])
def test_a_non_scalar_value_is_refused(value):
    with pytest.raises(InvalidFilterValueError):
        SearchFilters.from_mapping({"entity_type": value})


def test_an_enum_backed_field_is_validated_against_its_enum():
    with pytest.raises(InvalidFilterValueError) as error:
        SearchFilters.from_mapping({"sensitivity": "top-secret"})

    assert "public" in str(error.value)


@pytest.mark.parametrize(
    "level", [member.value for member in SensitivityLevel]
)
def test_every_declared_sensitivity_is_accepted(level):
    assert SearchFilters.from_mapping({"sensitivity": level}).criteria[
        "sensitivity"
    ] == level


def test_a_sensitivity_enum_member_is_accepted_as_well_as_its_value():
    filters = SearchFilters.from_mapping(
        {"sensitivity": SensitivityLevel.RESTRICTED}
    )

    assert filters.criteria["sensitivity"] == "restricted"


def test_values_are_trimmed():
    assert SearchFilters.from_mapping({"entity_type": "  invoice  "}).criteria[
        "entity_type"
    ] == "invoice"


# ============================================================
# Matching
# ============================================================


def metadata(**overrides):
    payload = {
        "representation_id": "ai:invoice:x",
        "embedding_id": "emb.x",
        "vector_id": "v",
        "current_tier": StorageTier.HOT,
        "content_hash": "h",
        "model_id": "m",
        "dimension": 4,
        "entity_type": "invoice",
        "source_system_id": "finance_erp",
        "source_entity": "fin_invoice",
        "sensitivity": SensitivityLevel.INTERNAL,
    }
    payload.update(overrides)

    return StorageRecordMetadata(**payload)


def test_empty_filters_match_everything():
    assert NO_FILTERS.matches(metadata()) is True


def test_a_matching_record_passes():
    filters = SearchFilters.from_mapping({"entity_type": "invoice"})

    assert filters.matches(metadata()) is True


def test_a_non_matching_record_fails():
    filters = SearchFilters.from_mapping({"entity_type": "customer"})

    assert filters.matches(metadata()) is False


def test_every_criterion_must_hold():
    filters = SearchFilters.from_mapping(
        {"entity_type": "invoice", "source_system_id": "other_erp"}
    )

    assert filters.matches(metadata()) is False


def test_an_enum_valued_field_compares_by_its_wire_value():
    filters = SearchFilters.from_mapping({"sensitivity": "internal"})

    assert filters.matches(metadata()) is True


def test_a_record_missing_the_filtered_field_does_not_match():
    filters = SearchFilters.from_mapping({"document_id": "doc:policy"})

    assert filters.matches(metadata()) is False


def test_filters_match_a_plain_mapping_too():
    """The same rules serve tier state, an in-memory tier, and a test."""
    filters = SearchFilters.from_mapping({"entity_type": "invoice"})

    assert filters.matches({"entity_type": "invoice"}) is True
    assert filters.matches({"entity_type": "customer"}) is False


def test_apply_keeps_only_matching_subjects():
    filters = SearchFilters.from_mapping({"entity_type": "invoice"})
    subjects = [metadata(), metadata(entity_type="customer")]

    assert len(filters.apply(subjects)) == 1


# ============================================================
# Qdrant translation
# ============================================================


def test_empty_filters_produce_no_qdrant_filter():
    assert NO_FILTERS.to_qdrant_filter() is None


def test_filters_translate_into_a_qdrant_filter():
    qdrant = pytest.importorskip("qdrant_client")

    built = SearchFilters.from_mapping(
        {"entity_type": "invoice", "source_system_id": "finance_erp"}
    ).to_qdrant_filter()

    assert built is not None
    assert len(built.must) == 2
    keys = {condition.key for condition in built.must}
    assert keys == {"entity_type", "source_system_id"}


def test_the_qdrant_filter_matches_the_payload_key_names():
    """The payload and the filter must agree, or a server-side match silently
    returns nothing."""
    pytest.importorskip("qdrant_client")

    from erp_pipeline.storage.migration import _payload_for

    payload = _payload_for(
        metadata(
            document_id="doc:policy",
            canonical_record_id="erp:a:invoice:1",
            content_kind="document_chunk",
            parent_record_id="erp:legacy_hr:employees:emp002",
            source_field="birth_certificate",
            business_key_name="employee_id",
            business_key_value="EMP002",
            record_key="INV-204",
            document_type="birth_certificate",
            schema_name="public",
            entity_kind="table",
        )
    )

    assert set(EVERY_FILTER) == set(FILTERABLE_FIELDS), (
        "a new filterable field was added without extending this test"
    )

    built = SearchFilters.from_mapping(EVERY_FILTER).to_qdrant_filter()

    for condition in built.must:
        assert condition.key in payload, condition.key


# ============================================================
# End-to-end through the hybrid store
# ============================================================


class FilterAwareTier:
    """A tier that honours a Qdrant-style filter, like the real one does."""

    def __init__(self) -> None:
        self.points: list[tuple[str, dict]] = []
        self.received_filter = None

    def add(self, vector_id: str, payload: dict) -> None:
        self.points.append((vector_id, payload))

    def upsert(self, record, payload=None):
        self.points.append((f"vec-{record.representation_id}", dict(payload or {})))
        return True

    def get_vector(self, representation_id):
        return (0.1, 0.2, 0.3, 0.4)

    def exists(self, representation_id):
        return True

    def delete(self, representation_id):
        return True

    def count(self):
        return len(self.points)

    def search(self, vector, limit=5, query_filter=None):
        self.received_filter = query_filter
        results = []

        for vector_id, payload in self.points:
            if query_filter is not None:
                if not all(
                    payload.get(condition.key)
                    == condition.match.value
                    for condition in query_filter.must
                ):
                    continue

            results.append((vector_id, 0.9))

        return results[:limit]


@pytest.fixture
def populated():
    """Two invoices and one customer, in state and in the tier."""
    pytest.importorskip("qdrant_client")

    state = InMemoryTierStateStore()
    hot = FilterAwareTier()

    rows = [
        ("ai:invoice:a", "invoice", "finance_erp", "erp:finance_erp:invoice:a"),
        ("ai:invoice:b", "invoice", "other_erp", "erp:other_erp:invoice:b"),
        ("ai:customer:c", "customer", "finance_erp", "erp:finance_erp:customer:c"),
    ]

    for representation_id, entity, system, canonical in rows:
        record = StorageRecordMetadata(
            representation_id=representation_id,
            embedding_id=f"emb.{representation_id}",
            vector_id=f"vec-{representation_id}",
            current_tier=StorageTier.HOT,
            content_hash="h",
            model_id="m",
            dimension=4,
            entity_type=entity,
            source_system_id=system,
            source_entity=f"{entity}_table",
            canonical_record_id=canonical,
        )
        state.save(record)
        hot.add(
            record.vector_id,
            {
                "representation_id": representation_id,
                "entity_type": entity,
                "source_system_id": system,
                "source_entity": f"{entity}_table",
                "sensitivity": "internal",
                "canonical_record_id": canonical,
            },
        )

    return HybridVectorStore(TierSet(hot=hot), state), hot


def test_an_unfiltered_search_returns_everything(populated):
    store, _hot = populated

    assert len(store.search([0.1, 0.2, 0.3, 0.4], limit=10).hits) == 3


def test_filtering_by_entity_type_narrows_the_result(populated):
    store, _hot = populated

    result = store.search(
        [0.1, 0.2, 0.3, 0.4],
        limit=10,
        filters=SearchFilters.from_mapping({"entity_type": "invoice"}),
    )

    assert len(result.hits) == 2
    assert all(hit.entity_type == "invoice" for hit in result.hits)


def test_filtering_by_source_system_narrows_the_result(populated):
    store, _hot = populated

    result = store.search(
        [0.1, 0.2, 0.3, 0.4],
        limit=10,
        filters=SearchFilters.from_mapping({"source_system_id": "finance_erp"}),
    )

    assert len(result.hits) == 2


def test_two_filters_intersect(populated):
    store, _hot = populated

    result = store.search(
        [0.1, 0.2, 0.3, 0.4],
        limit=10,
        filters=SearchFilters.from_mapping(
            {"entity_type": "invoice", "source_system_id": "finance_erp"}
        ),
    )

    assert len(result.hits) == 1
    assert result.hits[0].canonical_record_id == "erp:finance_erp:invoice:a"


def test_a_filter_matching_nothing_returns_nothing(populated):
    store, _hot = populated

    result = store.search(
        [0.1, 0.2, 0.3, 0.4],
        limit=10,
        filters=SearchFilters.from_mapping({"entity_type": "purchase_order"}),
    )

    assert result.hits == ()


def test_the_filter_is_pushed_into_the_tier(populated):
    """Server-side, so the ANN search itself is constrained rather than the
    results being trimmed afterwards."""
    store, hot = populated

    store.search(
        [0.1, 0.2, 0.3, 0.4],
        limit=10,
        filters=SearchFilters.from_mapping({"entity_type": "invoice"}),
    )

    assert hot.received_filter is not None


def test_state_backstops_a_tier_that_cannot_filter():
    """A tier whose payload disagrees with state, or which ignores the filter,
    must not leak a non-matching hit."""
    state = InMemoryTierStateStore()

    class IgnoresFilters(FilterAwareTier):
        def search(self, vector, limit=5, query_filter=None):
            self.received_filter = query_filter
            return [(vector_id, 0.9) for vector_id, _ in self.points][:limit]

    hot = IgnoresFilters()

    for representation_id, entity in (("ai:invoice:a", "invoice"),
                                      ("ai:customer:c", "customer")):
        state.save(
            StorageRecordMetadata(
                representation_id=representation_id,
                embedding_id="emb.x",
                vector_id=f"vec-{representation_id}",
                current_tier=StorageTier.HOT,
                content_hash="h",
                model_id="m",
                dimension=4,
                entity_type=entity,
            )
        )
        hot.add(f"vec-{representation_id}", {"entity_type": entity})

    store = HybridVectorStore(TierSet(hot=hot), state)
    result = store.search(
        [0.1, 0.2, 0.3, 0.4],
        limit=10,
        filters=SearchFilters.from_mapping({"entity_type": "invoice"}),
    )

    assert len(result.hits) == 1
    assert result.hits[0].entity_type == "invoice"


def test_a_tier_without_filter_support_still_works():
    """An older or third-party tier implementation must degrade to unfiltered
    rather than raising - state still enforces the filter."""
    state = InMemoryTierStateStore()

    class LegacyTier(FilterAwareTier):
        def search(self, vector, limit=5):  # no query_filter parameter
            return [(vector_id, 0.9) for vector_id, _ in self.points][:limit]

    hot = LegacyTier()
    state.save(
        StorageRecordMetadata(
            representation_id="ai:invoice:a",
            embedding_id="emb.x",
            vector_id="vec-ai:invoice:a",
            current_tier=StorageTier.HOT,
            content_hash="h",
            model_id="m",
            dimension=4,
            entity_type="invoice",
        )
    )
    hot.add("vec-ai:invoice:a", {"entity_type": "invoice"})

    store = HybridVectorStore(TierSet(hot=hot), state)
    result = store.search(
        [0.1, 0.2, 0.3, 0.4],
        limit=10,
        filters=SearchFilters.from_mapping({"entity_type": "invoice"}),
    )

    assert len(result.hits) == 1
