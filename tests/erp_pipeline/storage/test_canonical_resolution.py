"""The canonical record reference must survive the whole storage path.

THE DEFECT THIS PINS
--------------------
A representation id is normalized (``:`` becomes ``_``), so
``erp:finance_erp:invoice:inv-001`` becomes
``ai:invoice:erp_finance_erp_invoice_inv-001`` and the original CANNOT be
recovered by parsing it back - a source system id may itself contain
underscores, so the split is ambiguous.

Before this fix, storage kept no canonical reference at all, so a search hit
named a vector nobody could resolve to a record. These tests prove the
reference is carried FORWARD explicitly at every hop, and that nothing
reconstructs it.
"""

from __future__ import annotations

import json

import pytest

from erp_pipeline.ai.embedding import DeterministicTestModel
from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import CARRIED_IDENTITY_KEYS, EmbeddingService
from erp_pipeline.schemas.canonical_models import CanonicalRecord, SourceReference
from erp_pipeline.schemas.enums import SensitivityLevel, SourceType
from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import TierSet, _payload_for
from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier
from erp_pipeline.storage.service import StorageService
from erp_pipeline.storage.state import InMemoryTierStateStore

SYSTEM = "finance_erp"


def canonical_record(key: str = "INV-001", entity: str = "invoice"):
    return CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id=SYSTEM,
            source_type=SourceType.POSTGRESQL,
            source_entity="fin_invoice",
            source_record_key=key,
        ),
        entity_type=entity,
        stable_source_key=key,
        normalized_data={"invoice_id": key, "amount": 25000.0},
        sensitivity=SensitivityLevel.INTERNAL,
    )


class RecordingTier:
    """An in-process tier that records exactly what payload it was handed."""

    def __init__(self) -> None:
        self.written: dict[str, EmbeddingRecord] = {}
        self.payloads: dict[str, dict] = {}

    def upsert(self, record, payload=None):
        self.written[record.representation_id] = record
        self.payloads[record.representation_id] = dict(payload or {})
        return True

    def get_vector(self, representation_id):
        record = self.written.get(representation_id)
        return record.vector if record else None

    def exists(self, representation_id):
        return representation_id in self.written

    def delete(self, representation_id):
        return self.written.pop(representation_id, None) is not None

    def search(self, vector, limit=5, query_filter=None):
        return [
            (f"vec-{index}", 0.9 - index * 0.01)
            for index, _ in enumerate(list(self.written)[:limit])
        ]

    def count(self):
        return len(self.written)


@pytest.fixture
def state():
    return InMemoryTierStateStore()


@pytest.fixture
def hot():
    return RecordingTier()


@pytest.fixture
def store(hot, state):
    return HybridVectorStore(TierSet(hot=hot), state)


def embedding_for(representation, vector=(0.1, 0.2, 0.3, 0.4)):
    """Embed one representation through the real service."""
    service = EmbeddingService(DeterministicTestModel(dimension=len(vector)))

    return service.embed_one(representation)


# ============================================================
# The id genuinely is not reversible
# ============================================================


def test_the_canonical_id_cannot_be_recovered_from_the_representation_id():
    """Establishes WHY explicit propagation is required, not merely tidier."""
    record = canonical_record()
    representation = canonical_record_to_representation(record)

    assert record.record_id == "erp:finance_erp:invoice:inv-001"
    assert ":" not in representation.representation_id.split(":", 2)[2]
    assert record.record_id not in representation.representation_id


# ============================================================
# Representation -> embedding
# ============================================================


def test_the_representation_carries_the_canonical_id():
    representation = canonical_record_to_representation(canonical_record())

    assert representation.metadata["canonical_record_id"] == (
        "erp:finance_erp:invoice:inv-001"
    )


def test_the_embedding_carries_the_canonical_id_forward():
    """The hop that previously dropped it."""
    representation = canonical_record_to_representation(canonical_record())
    embedding = embedding_for(representation)

    assert embedding.metadata["canonical_record_id"] == (
        "erp:finance_erp:invoice:inv-001"
    )


def test_the_embedding_carries_provenance_and_sensitivity_too():
    representation = canonical_record_to_representation(canonical_record())
    embedding = embedding_for(representation)

    assert embedding.metadata["source_system_id"] == SYSTEM
    assert embedding.metadata["source_entity"] == "fin_invoice"
    assert embedding.metadata["sensitivity"] == "internal"


def test_only_the_declared_identity_keys_are_carried():
    """Business content must not leak onto the embedding record."""
    representation = canonical_record_to_representation(canonical_record())
    embedding = embedding_for(representation)

    carried = set(embedding.metadata) - {"engine_version"}

    assert carried <= set(CARRIED_IDENTITY_KEYS)
    assert "invoice_id" not in embedding.metadata
    assert "amount" not in embedding.metadata


def test_an_absent_key_is_omitted_rather_than_stored_as_none():
    from dataclasses import replace

    representation = canonical_record_to_representation(canonical_record())
    stripped = replace(representation, metadata={})

    assert "canonical_record_id" not in embedding_for(stripped).metadata


# ============================================================
# Embedding -> storage state
# ============================================================


def test_storage_state_records_the_canonical_id(store):
    representation = canonical_record_to_representation(canonical_record())
    embedding = embedding_for(representation)

    metadata, _decision = store.store(embedding)

    assert metadata.canonical_record_id == "erp:finance_erp:invoice:inv-001"
    assert metadata.source_system_id == SYSTEM
    assert metadata.source_entity == "fin_invoice"


def test_an_explicit_argument_wins_over_carried_metadata(store):
    representation = canonical_record_to_representation(canonical_record())
    embedding = embedding_for(representation)

    metadata, _ = store.store(
        embedding, canonical_record_id="erp:other:invoice:override"
    )

    assert metadata.canonical_record_id == "erp:other:invoice:override"


def test_a_re_store_without_the_reference_does_not_erase_it(store):
    """A later write that happens to carry no reference must not blank one an
    earlier write already established."""
    representation = canonical_record_to_representation(canonical_record())
    embedding = embedding_for(representation)

    store.store(embedding)

    from dataclasses import replace

    bare = replace(embedding, metadata={"engine_version": "1.0"})
    metadata, _ = store.store(bare)

    assert metadata.canonical_record_id == "erp:finance_erp:invoice:inv-001"


def test_an_embedding_with_no_reference_reports_none_rather_than_guessing(store):
    """Honest absence. The alternative - deriving something from the
    representation id - would produce an id that resolves to nothing."""
    embedding = EmbeddingRecord(
        embedding_id="emb.x",
        representation_id="ai:invoice:standalone",
        content_hash="h",
        model_id="m",
        dimension=4,
        status=EmbeddingStatus.GENERATED,
        vector=(0.1, 0.2, 0.3, 0.4),
    )

    metadata, _ = store.store(embedding)

    assert metadata.canonical_record_id is None


# ============================================================
# Storage state -> vector payload
# ============================================================


def test_the_vector_payload_carries_the_identity_fields(store, hot):
    representation = canonical_record_to_representation(canonical_record())
    store.store(embedding_for(representation))

    payload = hot.payloads[representation.representation_id]

    assert payload["canonical_record_id"] == "erp:finance_erp:invoice:inv-001"
    assert payload["source_system_id"] == SYSTEM
    assert payload["entity_type"] == "invoice"


def test_absent_identity_keys_are_omitted_from_the_payload():
    """A key present-and-null and a key absent behave differently under a
    Qdrant match, so absence is encoded by omission."""
    payload = _payload_for(
        StorageRecordMetadata(
            representation_id="ai:invoice:x",
            embedding_id="emb.x",
            vector_id="v",
            current_tier=StorageTier.HOT,
            content_hash="h",
            model_id="m",
            dimension=4,
        )
    )

    assert "canonical_record_id" not in payload
    assert "document_id" not in payload


def test_the_payload_never_contains_business_content(store, hot):
    representation = canonical_record_to_representation(canonical_record())
    store.store(embedding_for(representation))

    payload = hot.payloads[representation.representation_id]

    assert "amount" not in payload
    assert "text_for_ai" not in payload
    assert "invoice_id" not in payload


def test_business_content_stays_absent_even_with_dynamic_filtering_enabled():
    """The harder version of the test above.

    A ``CanonicalRecord`` built with no schema behind it - the exact shape
    ``canonical_record()`` here produces - carries no ``filter_attributes``
    at all (see ``canonical_record_to_representation``): only a
    schema/catalog-aware ingestion path (``SourceNativeTransformer``) ever
    declares them. So even with dynamic filtering fully enabled - a real
    secret configured, not the absent one the test above relies on - this
    payload has nothing to tokenize, and the business content it never
    carried stays exactly as absent.
    """
    representation = canonical_record_to_representation(canonical_record())
    service = EmbeddingService(
        DeterministicTestModel(dimension=4), filter_token_secret="a-real-secret"
    )
    hot = RecordingTier()
    store = HybridVectorStore(TierSet(hot=hot), InMemoryTierStateStore())

    store.store(service.embed_one(representation))

    payload = hot.payloads[representation.representation_id]
    dumped = json.dumps(payload, default=str)

    assert "amount" not in payload
    assert "invoice_id" not in payload
    assert "text_for_ai" not in payload
    # The dollar figure - business content - is absent. "INV-001" itself
    # legitimately appears as record_key: a safe identity field, not
    # business content, and requirement 2/8 keep it plaintext on purpose.
    assert "25000" not in dumped
    assert payload.get("record_key") == "INV-001"


# ============================================================
# Storage state -> search hit
# ============================================================


def test_a_search_hit_carries_the_canonical_id(state):
    hot = RecordingTier()
    store = HybridVectorStore(TierSet(hot=hot), state)

    representation = canonical_record_to_representation(canonical_record())
    embedding = embedding_for(representation)
    metadata, _ = store.store(embedding)

    # The recording tier returns synthetic ids; align them with reality.
    hot.search = lambda vector, limit=5, query_filter=None: [
        (metadata.vector_id, 0.93)
    ]

    result = store.search([0.1, 0.2, 0.3, 0.4], limit=5)

    assert len(result.hits) == 1
    assert result.hits[0].canonical_record_id == (
        "erp:finance_erp:invoice:inv-001"
    )
    assert result.hits[0].entity_type == "invoice"


def test_a_hit_for_a_vector_with_no_state_reports_no_canonical_id(state):
    hot = RecordingTier()
    store = HybridVectorStore(TierSet(hot=hot), state)
    hot.search = lambda vector, limit=5, query_filter=None: [("orphan-vec", 0.5)]

    result = store.search([0.1, 0.2, 0.3, 0.4], limit=5)

    assert result.hits[0].canonical_record_id is None


# ============================================================
# StorageService passes it through
# ============================================================


def test_the_service_layer_preserves_the_canonical_id():
    hot = RecordingTier()
    service = StorageService(hot=hot, state_store=InMemoryTierStateStore())

    representation = canonical_record_to_representation(canonical_record())
    metadata, _ = service.store(embedding_for(representation))

    assert metadata.canonical_record_id == "erp:finance_erp:invoice:inv-001"


def test_metadata_serialization_includes_the_canonical_id():
    payload = StorageRecordMetadata(
        representation_id="ai:invoice:x",
        embedding_id="emb.x",
        vector_id="v",
        current_tier=StorageTier.HOT,
        content_hash="h",
        model_id="m",
        dimension=4,
        canonical_record_id="erp:a:invoice:1",
    ).to_dict()

    assert payload["canonical_record_id"] == "erp:a:invoice:1"


# ============================================================
# Backward compatibility
# ============================================================


def test_metadata_built_without_the_new_fields_still_works():
    """Old call sites construct StorageRecordMetadata positionally/partially;
    the new fields are additive and default to None."""
    metadata = StorageRecordMetadata(
        representation_id="ai:invoice:legacy",
        embedding_id="emb.legacy",
        vector_id="v",
        current_tier=StorageTier.HOT,
        content_hash="h",
        model_id="m",
        dimension=384,
    )

    assert metadata.canonical_record_id is None
    assert metadata.source_system_id is None
    assert metadata.document_id is None


def test_a_row_missing_the_new_columns_is_read_safely():
    """A database that has not had bootstrap run since these columns were
    added must not crash every read."""
    from erp_pipeline.storage.state import _row_to_metadata

    legacy_row = {
        "representation_id": "ai:invoice:legacy",
        "embedding_id": "emb.legacy",
        "vector_id": "v",
        "current_tier": "hot",
        "content_hash": "h",
        "model_id": "m",
        "dimension": 384,
        "sensitivity": "internal",
        "business_criticality": "normal",
        "latency_requirement": "standard",
        "entity_type": "invoice",
        "access_count": 0,
        "recent_access_count": 0,
        "last_accessed_at": None,
        "created_at": None,
        "content_updated_at": None,
        "retention_until": None,
        "legal_hold": False,
        "tier_since": None,
        "policy_id": None,
        "policy_version": None,
        "state_version": 0,
        "updated_at": None,
    }

    metadata = _row_to_metadata(legacy_row)

    assert metadata.representation_id == "ai:invoice:legacy"
    assert metadata.canonical_record_id is None
